"""The evaluation must be able to fail, and its fixtures must be reproducible."""

from __future__ import annotations

import asyncio
import copy
import csv
import importlib
import json
from collections import Counter

import pytest

run_eval = importlib.import_module("run_eval")
make_fixture = importlib.import_module("make_fixture")


def test_level1_eval_passes_on_the_committed_fixtures():
    report = asyncio.run(run_eval.run_level1())
    failed = [r for r in report.results if not r.correct or r.provenance_ok is False]
    assert failed == []
    counts = report.counts()
    assert counts["answer_correctness"] == (31, 31)
    assert counts["provenance"] == (31, 31)
    assert counts["abstention"] == (8, 8)
    assert report.counts("synthetic")["answer_correctness"] == (20, 20)
    assert report.counts("real_snapshot")["answer_correctness"] == (11, 11)
    assert report.counts("real_snapshot")["abstention"] == (1, 1)
    assert report.skipped == ["live-forecast", "live-opinion", "real-live-forecast"]


def test_there_are_about_twenty_answerable_cases_and_categories_are_mixed():
    cases = run_eval.load_cases()
    offline = [c for c in cases if not c.get("live_only")]
    assert 20 <= len(offline) <= 40
    assert {"count", "sum", "avg", "group", "date", "filter", "edge", "abstain"} <= {
        c["category"] for c in cases
    }


def truth_and_case(case_id: str):
    from open_data_mcp.fakeportal import build_packages

    case = next(c for c in run_eval.load_cases() if c["id"] == case_id)
    return case, run_eval.truth_for(case, build_packages())


def test_scorer_detects_a_wrong_number():
    case, truth = truth_and_case("count-total")
    out = {"ok": True, "rows": [[119]], "source": good_source(truth)}
    assert run_eval.score_level1(case, out, truth).correct is False
    out["rows"] = [[120]]
    assert run_eval.score_level1(case, out, truth).correct is True


def good_source(truth):
    pkg, res = truth["pkg"], truth["res"]
    return {
        "portal": "p",
        "dataset_id": pkg["id"],
        "dataset_title": pkg["title"],
        "resource_id": res["id"],
        "resource_url": res["url"],
        "license": pkg["license_title"],
        "dataset_modified_at": pkg["metadata_modified"],
        "retrieved_at_utc": "2026-01-01T00:00:00Z",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.pop("license"),
        lambda s: s.update(license="MIT"),
        lambda s: s.update(dataset_modified_at=s["retrieved_at_utc"]),  # confusing the two dates
        lambda s: s.update(retrieved_at_utc="yesterday"),
        lambda s: s.pop("retrieved_at_utc"),
    ],
)
def test_provenance_check_detects_missing_or_wrong_metadata(mutate):
    case, truth = truth_and_case("count-total")
    source = copy.deepcopy(good_source(truth))
    mutate(source)
    res = run_eval.score_level1(case, {"ok": True, "rows": [[120]], "source": source}, truth)
    assert res.correct and res.provenance_ok is False


def test_abstention_scoring_requires_the_right_refusal_and_no_data():
    case, truth = truth_and_case("abstain-ddl")
    right = {"ok": False, "error": {"code": "unsafe_sql"}, "rows": []}
    assert run_eval.score_level1(case, right, truth).correct
    wrong_code = {"ok": False, "error": {"code": "sql_error"}, "rows": []}
    assert not run_eval.score_level1(case, wrong_code, truth).correct
    answered = {"ok": True, "rows": [[1]], "source": good_source(truth)}
    assert not run_eval.score_level1(case, answered, truth).correct


def test_null_expectation_and_tolerance():
    assert run_eval.values_match(None, None, 0)
    assert not run_eval.values_match(0, None, 0)
    assert run_eval.values_match(10.004, 10.0, 0.01) and not run_eval.values_match(
        10.02, 10.0, 0.01
    )
    assert run_eval.values_match("2024-01-02", "2024-01-02", 0)


def test_expected_values_match_an_independent_recount():
    """Recount a few answers from the raw CSV with plain Python, separately from the generator."""
    rows = list(
        csv.DictReader((make_fixture.FIXTURES / "service_requests.csv").open(encoding="utf-8"))
    )
    cases = {c["id"]: c for c in run_eval.load_cases()}
    assert cases["count-total"]["expect"]["value"] == len(rows)
    assert cases["count-open"]["expect"]["value"] == sum(r["status"] == "open" for r in rows)
    per_district = Counter(r["district"] for r in rows)
    assert cases["count-district"]["expect"]["value"] == per_district["Центральний"]
    assert cases["edge-zero-count"]["expect"]["value"] == 0
    assert cases["date-earliest"]["expect"]["value"] == min(r["received_date"] for r in rows)
    assert not any(r["category"] == "Parks" for r in rows)  # backs the null-average edge case


def test_fixture_generation_is_reproducible(tmp_path, monkeypatch):
    monkeypatch.setattr(make_fixture, "FIXTURES", tmp_path / "fx")
    monkeypatch.setattr(make_fixture, "ROOT", tmp_path)
    (tmp_path / "evals").mkdir()
    make_fixture.main()
    committed = make_fixture.__file__
    assert committed
    repo_fx = importlib.import_module("open_data_mcp.fakeportal").FIXTURE_DIR
    for name in ("service_requests.csv", "budget_records.json"):
        assert (tmp_path / "fx" / name).read_bytes() == (repo_fx / name).read_bytes()
    regenerated = (tmp_path / "evals" / "questions.jsonl").read_text("utf-8")
    assert regenerated == run_eval.QUESTIONS.read_text("utf-8")
    json.loads(regenerated.splitlines()[0])


def test_markdown_report_includes_denominators():
    report = asyncio.run(run_eval.run_level1())
    md = run_eval.render_markdown(report)
    assert "20 / 20" in md and "11 / 11" in md and "31 / 31" in md and "8 / 8" in md
    assert "Real data.gov.ua snapshot" in md and "invented test data" in md


def real_world():
    worlds = run_eval.build_worlds()
    service, packages, urls = worlds["real_snapshot"]
    return service, packages, urls


def test_real_cases_use_the_real_snapshot_and_real_urls():
    cases = [c for c in run_eval.load_cases() if c.get("fixture") == "real_snapshot"]
    assert len(cases) == 13 and sum(1 for c in cases if c.get("live_only")) == 1
    _service, packages, urls = real_world()
    for case in cases:
        truth = run_eval.truth_for(case, packages, urls)
        assert truth["dataset_url"].startswith("https://data.gov.ua/dataset/")
        assert truth["res"]["url"].startswith("https://data.gov.ua/dataset/")


def test_real_provenance_is_checked_against_captured_metadata_including_dataset_url():
    _service, packages, urls = real_world()
    case = next(c for c in run_eval.load_cases() if c["id"] == "real-count-total")
    truth = run_eval.truth_for(case, packages, urls)
    source = {
        "portal": "p", "dataset_id": truth["pkg"]["id"], "dataset_title": truth["pkg"]["title"],
        "resource_id": truth["res"]["id"], "resource_url": truth["res"]["url"],
        "license": truth["pkg"]["license_title"],
        "dataset_modified_at": truth["pkg"]["metadata_modified"],
        "resource_modified_at": truth["res"]["last_modified"], "dataset_url": truth["dataset_url"],
        "retrieved_at_utc": "2026-01-01T00:00:00Z",
    }  # fmt: skip
    assert run_eval.check_provenance(source, truth) == (True, "")
    wrong_url = {**source, "dataset_url": "https://fixtures.invalid/dataset/x"}
    assert run_eval.check_provenance(wrong_url, truth)[0] is False


def test_real_scorer_fails_on_a_wrong_real_number():
    _service, packages, urls = real_world()
    case = next(c for c in run_eval.load_cases() if c["id"] == "real-sum-seats")
    truth = run_eval.truth_for(case, packages, urls)
    out = {"ok": True, "rows": [[case["expect"]["value"] + 1]], "source": None}
    assert run_eval.score_level1(case, out, truth).correct is False
