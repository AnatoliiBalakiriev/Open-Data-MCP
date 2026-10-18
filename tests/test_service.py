from __future__ import annotations

import pytest

from open_data_mcp.errors import InvalidInput, OpenDataError, UnsafeSql
from open_data_mcp.service import safe_call

from .conftest import BUD, REQ


def test_search_clamps_limit_and_returns_compact_fields(service):
    out = service.search_datasets("synthetic", limit=10_000)
    assert out["limit_applied"] == 20
    first = out["datasets"][0]
    assert {
        "dataset_id",
        "title",
        "description",
        "organization",
        "license",
        "dataset_url",
    } <= first.keys()
    assert first["dataset_url"].startswith("https://portal.fixtures.invalid/dataset/")
    assert service.search_datasets("synthetic", limit=0)["limit_applied"] == 1
    assert service.search_datasets("budget", limit=5)["returned"] == 1
    assert service.search_datasets("no-such-dataset-xyz")["datasets"] == []


def test_search_rejects_bad_types(service):
    with pytest.raises(InvalidInput):
        service.search_datasets("x", limit="ten")  # type: ignore[arg-type]
    with pytest.raises(InvalidInput):
        service.search_datasets(None)  # type: ignore[arg-type]


def test_get_schema_for_file_is_labelled_inferred(service):
    out = service.get_schema(*REQ)
    assert out["schema_source"] == "inferred_from_file" and out["authoritative"] is False
    names = [c["name"] for c in out["schema"]]
    assert names[:3] == ["request_id", "district", "category"]
    assert out["row_count"] == 120 and len(out["sample_rows"]) == 3
    assert out["source"]["access_method"] == "file_download"


def test_get_schema_for_datastore_is_authoritative(service):
    out = service.get_schema(*BUD)
    assert out["schema_source"] == "datastore_metadata" and out["authoritative"] is True
    assert {"name": "planned_uah", "type": "int8"} in out["schema"]
    assert out["row_count"] == 24
    assert out["source"]["access_method"] == "datastore"


def test_get_schema_without_resource_id_autoselects_single_queryable(service):
    assert service.get_schema("syn-service-requests-2024")["resource_id"] == "syn-res-requests-csv"


def test_get_schema_without_resource_id_lists_when_ambiguous(service):
    out = service.get_schema("syn-file-variants")
    assert out["schema"] is None and out["schema_source"] == "unavailable"
    by_id = {r["resource_id"]: r for r in out["resources"]}
    assert by_id["syn-res-xlsx"]["queryable"] is False
    assert by_id["syn-res-huge-csv"]["queryable"] is True


def test_get_schema_unsupported_format(service):
    result = safe_call(service.get_schema, "syn-file-variants", "syn-res-xlsx")
    assert result["ok"] is False and result["error"]["code"] == "unsupported_resource"


def test_resource_must_belong_to_dataset(service):
    result = safe_call(service.query, REQ[0], "syn-res-budget-datastore", "SELECT 1 FROM data")
    assert result["error"]["code"] == "invalid_input"


@pytest.mark.parametrize(
    "bad",
    ["https://evil.test/a.csv", "../../etc/passwd", "/etc/passwd", "a b", "x;y", "", "a" * 300],
)
def test_ids_that_look_like_urls_or_paths_are_rejected(service, bad):
    assert (
        safe_call(service.query, bad, "r", "SELECT 1 FROM data")["error"]["code"] == "invalid_input"
    )
    assert (
        safe_call(service.query, REQ[0], bad, "SELECT 1 FROM data")["error"]["code"]
        == "invalid_input"
    )


def test_unsafe_sql_is_rejected_before_any_network_access(service, portal):
    with pytest.raises(UnsafeSql):
        service.query(*REQ, "DROP TABLE data")
    assert portal.calls == []


def test_query_result_has_complete_provenance(service):
    out = service.query(*REQ, "SELECT count(*) FROM data")
    src = out["source"]
    for key in (
        "portal",
        "dataset_id",
        "dataset_title",
        "dataset_url",
        "resource_id",
        "resource_name",
        "resource_url",
        "retrieved_at_utc",
        "dataset_modified_at",
        "license",
    ):
        assert src[key], key
    assert src["dataset_modified_at"] == "2024-12-31T10:00:00.000000"
    assert src["retrieved_at_utc"] != src["dataset_modified_at"]
    assert out["citation"].startswith("Source:")


def test_missing_source_dates_stay_null_in_query_result(service):
    out = service.query("syn-minimal-metadata", "syn-res-minimal-csv", "SELECT count(*) FROM data")
    assert out["rows"] == [[2]]
    assert out["source"]["dataset_modified_at"] is None
    assert out["source"]["license"] is None


def test_datastore_is_paginated_and_complete(service, portal):
    out = service.query(*BUD, "SELECT count(*) FROM data")
    assert out["rows"] == [[24]]
    assert sum("datastore_search" in c for c in portal.calls) == 3  # page size 10 -> 10+10+4


def test_empty_result_carries_actionable_note(service):
    out = service.query(*REQ, "SELECT * FROM data WHERE district = 'Nowhere'")
    assert out["rows"] == [] and "no rows" in out["note"]


def test_failed_query_returns_no_rows_or_numbers(service):
    result = safe_call(service.query, *REQ, "SELECT sum(nonexistent) FROM data")
    assert result["ok"] is False and "rows" not in result
    assert result["error"]["code"] == "sql_error"


@pytest.mark.parametrize(
    ("resource", "code"),
    [
        ("syn-res-xlsx", "unsupported_resource"),
        ("syn-res-huge-csv", "resource_too_large"),
        ("syn-res-private-csv", "unsafe_url"),
        ("syn-res-http-csv", "unsafe_url"),
    ],
)
def test_bad_resources_are_refused_safely(service, resource, code):
    result = safe_call(service.query, "syn-file-variants", resource, "SELECT count(*) FROM data")
    assert result["ok"] is False and result["error"]["code"] == code


def test_unknown_dataset_is_not_found(service):
    result = safe_call(service.query, "does-not-exist", "r1", "SELECT 1 FROM data")
    assert result["error"]["code"] == "not_found"


def test_result_cap_applies_to_service_output(service):
    out = service.query(*REQ, "SELECT * FROM data", limit=10_000)
    assert out["returned_rows"] == 100 and out["truncated"] is True and out["row_limit"] == 100
    assert out["rows_in_resource"] == 120


def test_oversized_download_declared_by_settings(portal):
    from dataclasses import replace

    from open_data_mcp.fakeportal import OFFLINE_SETTINGS, build_offline_service

    tiny = replace(OFFLINE_SETTINGS, max_download_bytes=100)
    svc, _ = build_offline_service(tiny)
    result = safe_call(svc.query, *REQ, "SELECT count(*) FROM data")
    assert result["error"]["code"] == "resource_too_large"


def test_safe_call_only_swallows_typed_errors():
    def boom():
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        safe_call(boom)
    assert issubclass(UnsafeSql, OpenDataError)


@pytest.mark.parametrize("fmt", ["сsv", "СSV", " Csv ", "csv"])
def test_cyrillic_lookalike_format_names_are_recognised(fmt):
    from open_data_mcp.service import classify_resource

    assert classify_resource({"format": fmt, "url": "https://x.test/a"})[0] == "csv"
    assert classify_resource({"format": "xlsx"})[0] is None


def test_degenerate_single_column_schema_gets_a_warning(service, portal):
    # a non-tabular file parses into one 'column' whose name holds delimiters
    portal.requests_csv = b'";;;;;;ZATV"\nx\ny\n'
    out = service.get_schema(*REQ)
    assert len(out["schema"]) == 1 and "WARNING" in out["note"]
