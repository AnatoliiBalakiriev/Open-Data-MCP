"""The real-data snapshot: integrity, licensing metadata and the offline replay."""

from __future__ import annotations

import hashlib
import json

import pytest
from mcp import Client

from open_data_mcp.agent import McpBackend, run_agent
from open_data_mcp.cli import _server_params
from open_data_mcp.fakeportal import (
    SNAPSHOT_DIR,
    build_snapshot_service,
    load_snapshot_meta,
    snapshot_package,
)
from open_data_mcp.service import safe_call

from .test_agent import QUERY, ScriptedModel, reply, text, tool_use


@pytest.fixture
def meta():
    return load_snapshot_meta()


def test_snapshot_file_matches_its_recorded_hash_and_size(meta):
    data = (SNAPSHOT_DIR / meta["file"]["path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == meta["file"]["sha256"]
    assert len(data) == meta["file"]["bytes"] == meta["resource"]["size"]


def test_snapshot_is_a_real_non_utf8_semicolon_csv(meta):
    """Exercises the cp1251 / delimiter paths with data nobody here wrote."""
    data = (SNAPSHOT_DIR / meta["file"]["path"]).read_bytes()
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")
    assert data.decode("cp1251").splitlines()[0].count(";") == 4


def test_snapshot_records_license_and_attribution_and_no_personal_fields(meta):
    assert meta["dataset"]["license_id"] == "cc-by"
    assert meta["dataset"]["license_url"].startswith("http://www.opendefinition.org/licenses/cc-by")
    assert meta["dataset"]["organization"] in meta["attribution"]
    assert "CC BY" in meta["attribution"] and "data.gov.ua" in meta["attribution"]
    assert meta["snapshot_captured_at_utc"].endswith("Z")
    dumped = json.dumps(meta, ensure_ascii=False)
    for personal in ("maintainer", "author", "email"):
        assert personal not in dumped
    assert meta["dataset_url"] == f"https://data.gov.ua/dataset/{meta['dataset']['name']}"


def test_snapshot_package_has_no_personal_fields(meta):
    package = snapshot_package(meta)
    assert not {"maintainer", "author", "maintainer_email", "author_email"} & package.keys()


def test_replayed_query_returns_real_provenance_not_synthetic_labels(meta):
    service, _portal = build_snapshot_service()
    ds, res = meta["dataset"], meta["resource"]
    out = safe_call(service.query, ds["id"], res["id"], "SELECT count(*) FROM data")
    assert out["ok"] and out["rows"] == [[128]]
    src = out["source"]
    assert src["dataset_url"] == meta["dataset_url"]
    assert src["resource_url"] == res["url"]
    assert src["license"] == ds["license_title"]
    assert src["dataset_modified_at"] == ds["metadata_modified"]
    assert "OFFLINE REPLAY" in src["portal"] and meta["snapshot_captured_at_utc"] in src["portal"]
    assert src["retrieved_at_utc"] != meta["snapshot_captured_at_utc"]  # replay time, honestly


def test_replay_never_touches_the_network_and_only_serves_this_dataset(meta):
    service, portal = build_snapshot_service()
    ds, res = meta["dataset"], meta["resource"]
    service.query(ds["id"], res["id"], "SELECT 1 FROM data")
    assert {c.split("/")[0] for c in portal.calls} == {"data.gov.ua"}
    unknown = safe_call(service.query, "some-other-dataset", res["id"], "SELECT 1 FROM data")
    assert unknown["error"]["code"] == "not_found"


def test_schema_of_the_real_file(meta):
    service, _ = build_snapshot_service()
    out = service.get_schema(meta["dataset"]["id"], meta["resource"]["id"])
    assert [c["name"] for c in out["schema"]] == [
        "Identifier", "prefLabel", "description", "numberofseats", "numberofchildren",
    ]  # fmt: skip
    assert out["schema_source"] == "inferred_from_file" and out["authoritative"] is False
    assert "WARNING" not in out["note"] and out["row_count"] == 128


def test_server_params_flag():
    assert "--offline-snapshot" in (_server_params(False, True).args)
    assert "--offline-snapshot" not in _server_params(False).args


async def test_agent_over_real_stdio_subprocess_with_the_snapshot(meta):
    args = {**QUERY, "dataset_id": meta["dataset"]["id"], "resource_id": meta["resource"]["id"]}
    model = ScriptedModel(
        reply("tool_use", tool_use("t1", "query", **args)), reply("end_turn", text("128."))
    )
    async with Client(_server_params(offline=False, snapshot=True)) as mcp:
        result = await run_agent("count", client=model, backend=McpBackend(mcp))
    call = result.tool_calls[0]
    assert call.result["rows"] == [[128]]
    assert call.result["source"]["dataset_url"] == meta["dataset_url"]
    assert meta["dataset_url"] in result.answer
