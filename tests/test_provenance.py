from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from open_data_mcp.errors import ProvenanceError
from open_data_mcp.provenance import assert_provenance, build_source, utc_now_iso

PKG = {
    "id": "d1",
    "title": "Title",
    "name": "d1-name",
    "license_title": "CC-BY",
    "metadata_modified": "2024-05-01T00:00:00",
}
RES = {"id": "r1", "name": "file.csv", "url": "https://x.test/f.csv", "last_modified": "2024-04-30"}


def make(pkg=PKG, res=RES):
    return build_source(
        portal="p",
        package=pkg,
        resource=res,
        dataset_url="https://p.test/dataset/d1-name",
        retrieved_at_utc="2026-10-18T10:00:00Z",
        metadata_retrieved_at_utc="2026-10-18T09:59:00Z",
        access_method="file_download",
    )


def test_all_fields_present_and_distinct_dates():
    s = make().to_dict()
    assert s["dataset_id"] == "d1" and s["resource_id"] == "r1"
    assert s["resource_url"] == "https://x.test/f.csv"
    assert s["license"] == "CC-BY"
    assert s["dataset_modified_at"] == "2024-05-01T00:00:00"
    assert s["retrieved_at_utc"] == "2026-10-18T10:00:00Z"
    assert s["dataset_modified_at"] != s["retrieved_at_utc"]  # never conflated


def test_missing_source_dates_are_null_not_invented():
    s = make({"id": "d1"}, {"id": "r1"}).to_dict()
    assert s["dataset_modified_at"] is None
    assert s["resource_modified_at"] is None
    assert s["license"] is None and s["dataset_title"] is None
    assert "unknown" in make({"id": "d1"}, {"id": "r1"}).citation()


def test_blank_strings_become_none():
    s = make({**PKG, "license_title": "  ", "license_id": ""}, RES).to_dict()
    assert s["license"] is None


def test_assert_provenance_rejects_incomplete_sources():
    with pytest.raises(ProvenanceError):
        assert_provenance(None)
    with pytest.raises(ProvenanceError, match="resource_id"):
        assert_provenance({"portal": "p", "dataset_id": "d", "retrieved_at_utc": "x"})
    assert_provenance(make().to_dict())


def test_build_source_requires_ids():
    with pytest.raises(ProvenanceError):
        make({"title": "no id"}, RES)


def test_citation_contains_the_essentials():
    c = make().citation()
    for part in (
        "Title",
        "https://p.test/dataset/d1-name",
        "file.csv",
        "CC-BY",
        "2024-05-01",
        "2026-10-18T10:00:00Z",
        "file_download",
    ):
        assert part in c


def test_utc_now_iso_format_and_timezone():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", utc_now_iso())
    assert utc_now_iso(datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2025-01-02T03:04:05Z"
