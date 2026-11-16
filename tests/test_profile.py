"""profile_dataset against SYNTHETIC files with planted defects.

Expected values come from `messy_fixture.registry_truth` (plain Python: csv, datetime, statistics),
and the headline counts are also asserted as literals, because they were planted by hand.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from mcp import Client

from open_data_mcp.errors import (
    AnalysisTimeout,
    InvalidInput,
    ResourceTooLarge,
    UnsupportedResource,
    UpstreamNotFound,
)
from open_data_mcp.fakeportal import OFFLINE_SETTINGS, FakePortal, build_offline_service
from open_data_mcp.messy_fixture import registry_truth
from open_data_mcp.profiling import classify_date
from open_data_mcp.query_engine import TableSession
from open_data_mcp.server import create_server

REGISTRY = "syn-quality-registry"
DEFECTS = "syn-file-defects"
Q2 = "syn-res-q2"
TRUTH = registry_truth()


def by_name(profile: dict, name: str) -> dict:
    return next(c for c in profile["columns"] if c["name"] == name)


def checks(profile: dict, check: str, column: str | None = None) -> list[dict]:
    return [
        i
        for i in profile["issues"]
        if i["check"] == check and (column is None or i["column"] == column)
    ]


@pytest.fixture
def registry(service):
    return service.profile_dataset(REGISTRY, Q2)


# --------------------------------------------------------------------------- planted defects


def test_planted_counts_are_what_the_generator_says():
    # Guards the fixture itself: these were planted by hand in messy_fixture.registry_rows.
    assert TRUTH.rows == 214
    assert (TRUTH.date_impossible, TRUTH.date_not_a_date) == (6, 4)
    assert (TRUTH.date_sentinel, TRUTH.date_out_of_range) == (3, 2)
    # five planted "1 000 000,00" amounts plus three reused-id rows with 999,99
    assert TRUTH.amount_extreme_outliers == 8 and TRUTH.amount_non_numeric == 3


def test_rows_missing_values_and_placeholders_match_the_truth(registry):
    assert registry["rows"] == TRUTH.rows and registry["coverage"]["complete"] is True
    for name in ("id", "registered_at", "amount", "status", "note", "source"):
        column = by_name(registry, name)
        assert column["empty"] == TRUTH.empty[name], name
        assert column["placeholders"] == TRUTH.placeholders[name], name
        share = (TRUTH.empty[name] + TRUTH.placeholders[name]) / TRUTH.rows
        assert column["missing_share"] == pytest.approx(share, abs=1e-4)
    status = by_name(registry, "status")
    assert status["placeholder_values"] == {"-": 8, "немає": 4, "null": 3}
    assert by_name(registry, "note")["kind"] == "empty"


def test_duplicates_identifiers_and_sparse_rows(registry):
    dup = registry["duplicates"]
    assert dup["exact_duplicate_rows"] == TRUTH.exact_duplicate_extra_rows == 10
    assert dup["exact_duplicate_share"] == pytest.approx(10 / 214, abs=1e-4)
    (key,) = dup["key_candidates"]
    assert key["column"] == "id" and key["rows_beyond_first"] == TRUTH.id_rows_beyond_first == 7
    assert registry["sparse_rows"]["count"] == TRUTH.sparse_rows == 7


def test_broken_dates(registry):
    dates = by_name(registry, "registered_at")["dates"]
    assert dates["valid"] == TRUTH.date_valid
    assert dates["impossible"] == TRUTH.date_impossible == 6
    assert dates["not_a_date"] == TRUTH.date_not_a_date == 4
    assert dates["sentinel"] == TRUTH.date_sentinel == 3
    assert dates["out_of_range"] == TRUTH.date_out_of_range == 2
    assert dates["mixed_layouts"] is True and len(dates["layouts"]) == TRUTH.date_layout_count
    planted = {"31.02.2023", "29.02.2023", "15.13.2023", "00.05.2023", "32.01.2023", "31.04.2023"}
    assert (
        len(dates["examples"]["impossible"]) == 3
        and set(dates["examples"]["impossible"]) <= planted
    )
    assert any("вчора" in e or "Подано" in e for e in dates["examples"]["not_a_date"])


def test_numeric_outliers_use_independent_quartiles(registry):
    amount = by_name(registry, "amount")
    numeric = amount["numeric"]
    assert numeric["count"] == TRUTH.amount_numeric
    assert numeric["q1"] == pytest.approx(TRUTH.amount_q1, abs=1e-6)
    assert numeric["q3"] == pytest.approx(TRUTH.amount_q3, abs=1e-6)
    assert numeric["outliers"]["mild"] == TRUTH.amount_mild_outliers
    assert numeric["outliers"]["extreme"] == TRUTH.amount_extreme_outliers == 8
    assert 1_000_000 in numeric["outliers"]["examples"]  # "1 000 000,00" with NBSP groups
    assert amount["non_numeric_values"] == TRUTH.amount_non_numeric
    assert "обмін" in amount["non_numeric_examples"]


def test_identifier_column_gets_no_outlier_verdict(registry):
    numeric = by_name(registry, "id")["numeric"]
    assert numeric["outliers"] is None and "identifier" in numeric["outliers_skipped"]


def test_issues_are_ranked_and_name_the_planted_problems(registry):
    severities = [i["severity"] for i in registry["issues"]]
    assert severities == sorted(severities, key=["high", "medium", "low", "info"].index)
    assert checks(registry, "broken_dates", "registered_at")[0]["severity"] == "high"
    assert checks(registry, "exact_duplicate_rows") and checks(
        registry, "repeated_identifier", "id"
    )
    assert checks(registry, "sparse_rows") and checks(registry, "column_empty", "note")
    assert checks(registry, "mixed_date_formats") and checks(registry, "sentinel_dates")
    assert checks(registry, "numbers_mixed_with_text", "amount")
    assert checks(registry, "numeric_outliers", "amount")
    (constant,) = checks(registry, "constant_column")
    assert "source" in constant["message"]
    assert checks(registry, "placeholder_values", "status")
    assert registry["issue_counts"]["high"] == sum(
        1 for i in registry["issues"] if i["severity"] == "high"
    )
    assert checks(registry, "byte_order_mark")  # the fixture files start with a UTF-8 BOM


# --------------------------------------------------------------------------- schema drift


def test_schema_drift_against_sibling_files(service, registry):
    drift = registry["schema_drift"]
    by_id = {v["resource_id"]: v for v in drift["compared"]}
    assert set(by_id) == {"syn-res-q1", "syn-res-q3"}  # ".csv" typed as the format still counts
    q1 = by_id["syn-res-q1"]
    assert q1["same_schema"] is True and q1["rows_sampled"] == 40
    q3 = by_id["syn-res-q3"]
    assert q3["same_schema"] is False
    assert q3["columns_removed"] == ["registered_at", "note"]
    assert q3["columns_added"] == ["registration_date", "region"]
    assert [
        (r["from_"] if "from_" in r else r["from"], r["to"]) for r in q3["possible_renames"]
    ] == [("status", "Status")]
    assert q3["column_order_changed"] is True
    assert q3["type_changes"] == [
        {"column": "id", "reference_kind": "integer", "other_kind": "text"}
    ]
    reasons = {s["resource_id"]: s["reason"] for s in drift["skipped"]}
    assert "XLSX" in reasons["syn-res-q4-xlsx"]
    assert "404" in reasons["syn-res-q5-gone"]
    assert [i["column"] for i in checks(registry, "schema_drift")] == [None]  # one per bad sibling


def test_drift_can_be_switched_off_and_then_reads_no_sibling(service, portal):
    out = service.profile_dataset(REGISTRY, Q2, compare_versions=False)
    assert out["schema_drift"] is None
    assert not any("q1" in c or "q3" in c for c in portal.calls)


def test_drift_is_limited_to_the_configured_number_of_siblings():
    service, _ = build_offline_service(replace(OFFLINE_SETTINGS, drift_max_resources=1))
    drift = service.profile_dataset(REGISTRY, Q2)["schema_drift"]
    assert len(drift["compared"]) == 1
    assert any("limit" in s["reason"] for s in drift["skipped"])


# --------------------------------------------------------------------------- encodings and files


def test_windows_1251_file_is_converted_and_reported(service):
    out = service.profile_dataset(DEFECTS, "syn-res-cp1251", compare_versions=False)
    assert (
        out["encoding"]["detected"] == "windows-1251" and out["encoding"]["windows1251_lines"] == 20
    )
    assert by_name(out, "name")["text_damage"] is None  # decoded correctly, not garbled
    assert checks(out, "legacy_encoding")


def test_mixed_encoding_is_counted_per_line(service):
    out = service.profile_dataset(DEFECTS, "syn-res-mixed", compare_versions=False)
    enc = out["encoding"]
    assert enc["detected"] == "mixed" and (enc["utf8_lines"], enc["windows1251_lines"]) == (30, 4)
    assert out["rows"] == 34 and by_name(out, "name")["text_damage"] is None
    assert checks(out, "mixed_encoding")[0]["severity"] == "high"


def test_mojibake_and_replacement_characters(service):
    out = service.profile_dataset(DEFECTS, "syn-res-mojibake", compare_versions=False)
    damage = by_name(out, "name")["text_damage"]
    assert damage["mojibake_values"] == 6 and damage["replacement_character_values"] == 2
    assert damage["mojibake_examples"] == ["РџСЂРёРІС–С‚ СЃРІС–С‚Рµ"]
    assert out["encoding"]["replacement_characters"] == 2
    assert checks(out, "mojibake", "name")[0]["severity"] == "high"
    assert checks(out, "replacement_characters", "name")


def test_ragged_rows_are_counted_not_fatal(service):
    out = service.profile_dataset(DEFECTS, "syn-res-ragged", compare_versions=False)
    assert out["malformed_rows"] == 3 and out["rows"] == 10
    assert checks(out, "malformed_rows")


# --------------------------------------------------------------------------- coverage


def test_oversized_file_is_profiled_as_a_labelled_prefix():
    cap = 20_000
    service, portal = build_offline_service(replace(OFFLINE_SETTINGS, max_download_bytes=cap))
    out = service.profile_dataset(DEFECTS, "syn-res-biglog", compare_versions=False)
    raw = portal.files["big_log.csv"]
    head = raw[:cap]
    expected_rows = head[: head.rfind(b"\n")].count(b"\n")  # complete lines, minus the header
    cov = out["coverage"]
    assert cov["complete"] is False and cov["mode"] == "prefix_sample"
    assert out["rows"] == cov["rows_profiled"] == expected_rows < 3000
    assert cov["rows_in_resource"] is None and cov["resource_size_bytes"] == len(raw)
    assert cov["bytes_read"] <= cap
    assert checks(out, "partial_coverage")
    assert f"first {expected_rows} rows" in out["citation"]
    with pytest.raises(ResourceTooLarge):  # `query` keeps refusing: partial data is never queried
        service.query(DEFECTS, "syn-res-biglog", "SELECT count(*) FROM data")


def test_row_limit_cuts_and_flags_the_table():
    service, _ = build_offline_service(replace(OFFLINE_SETTINGS, max_table_rows=50))
    out = service.profile_dataset(DEFECTS, "syn-res-biglog", compare_versions=False)
    assert out["rows"] == 50 and out["coverage"]["complete"] is False


def test_datastore_resource_is_profiled_and_can_be_partial(service):
    full = service.profile_dataset("syn-regional-budget", "syn-res-budget-datastore")
    assert full["rows"] == 24 and full["coverage"]["complete"] is True
    assert full["encoding"] is None and full["source"]["access_method"] == "datastore"
    small, _ = build_offline_service(replace(OFFLINE_SETTINGS, max_table_rows=10))
    part = small.profile_dataset("syn-regional-budget", "syn-res-budget-datastore")
    assert part["rows"] == 10 and part["coverage"]["complete"] is False
    assert part["coverage"]["rows_in_resource"] == 24


# --------------------------------------------------------------------------- tool behaviour


def test_provenance_is_always_present(registry):
    src = registry["source"]
    assert src["resource_id"] == Q2 and src["retrieved_at_utc"] and src["license"]
    assert src["access_method"] == "file_download" and registry["citation"].startswith("Source:")


def test_single_queryable_resource_is_picked_automatically(service):
    out = service.profile_dataset("syn-service-requests-2024")
    assert out["resource_id"] == "syn-res-requests-csv" and out["rows"] > 0


def test_ambiguous_dataset_returns_the_resource_list_not_a_guess(service):
    out = service.profile_dataset(REGISTRY)
    assert "columns" not in out and "source" not in out
    assert {r["resource_id"] for r in out["resources"]} >= {Q2, "syn-res-q4-xlsx"}


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ((REGISTRY, "syn-res-q4-xlsx"), UnsupportedResource),
        ((REGISTRY, "nope"), InvalidInput),
        (("not a valid id!", None), InvalidInput),
        (("no-such-dataset", None), UpstreamNotFound),
    ],
)
def test_errors_are_typed(service, args, error):
    with pytest.raises(error):
        service.profile_dataset(*args)


async def test_through_the_mcp_server_the_output_matches_its_schema(service):
    async with Client(create_server(service)) as client:
        result = await client.call_tool(
            "profile_dataset", {"dataset_id": REGISTRY, "resource_id": Q2}
        )
        failing = await client.call_tool("profile_dataset", {"dataset_id": "no-such-dataset"})
    out = result.structured_content
    assert out["ok"] is True and out["source"]["resource_id"] == Q2
    assert out["issues"][0]["severity"] == "high"
    assert failing.structured_content["ok"] is False
    assert failing.structured_content["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- units


@pytest.mark.parametrize(
    ("text", "status", "layout"),
    [
        ("02.11.2022 11:15", "valid", "DD.MM.YYYY"),
        ("2022-11-02T11:15:30Z", "valid", "YYYY-MM-DD"),
        ("29.02.2024", "valid", "DD.MM.YYYY"),  # leap year
        ("29.02.2023", "impossible", "DD.MM.YYYY"),
        ("2023-13-01", "impossible", "YYYY-MM-DD"),
        ("01.01.2023 25:00", "impossible", "DD.MM.YYYY"),
        ("01.02.23", "ambiguous", "DD.MM.YY"),
        ("Петиція подано: 06.09.2016 17:46", "not_a_date", None),
        ("1.2-2020", "not_a_date", None),
    ],
)
def test_classify_date(text, status, layout):
    verdict = classify_date(text)
    assert (verdict.status, verdict.layout) == (status, layout)


def table(csv_text: str) -> TableSession:
    session = TableSession(OFFLINE_SETTINGS)
    session.load_csv(csv_text.encode(), lenient=True)
    return session


def test_cyrillic_placeholders_are_case_insensitive_and_legit_names_are_not_mojibake():
    from open_data_mcp.profiling import profile_table

    rows = "\n".join(["НЕМАЄ", "Н/Д", "Рівне", "Сєвєродонецьк", "Київ", "Рівне"] * 5)
    with table("city\n" + rows + "\n") as session:
        column = profile_table(session, OFFLINE_SETTINGS)["columns"][0]
    assert column["placeholders"] == 10 and column["text_damage"] is None


def test_codes_with_leading_zeros_are_text_not_numbers():
    from open_data_mcp.profiling import profile_table

    body = "\n".join(f"{i:08d}" for i in range(30))
    with table("code\n" + body + "\n") as session:
        column = profile_table(session, OFFLINE_SETTINGS)["columns"][0]
    assert column["kind"] == "text" and column["numeric"] is None


def test_a_header_only_file_profiles_without_dividing_by_zero():
    from open_data_mcp.profiling import profile_table

    with table("a;b;c\n") as session:
        out = profile_table(session, OFFLINE_SETTINGS)
    assert out["rows"] == 0 and out["duplicates"]["exact_duplicate_rows"] is None


def test_runaway_internal_sql_hits_the_time_budget():
    with table("a\n1\n") as session, pytest.raises(AnalysisTimeout), session.time_budget(0.3):
        session.execute_trusted(
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r"
        )


def test_fixture_files_are_deterministic():
    assert FakePortal().files == FakePortal().files
