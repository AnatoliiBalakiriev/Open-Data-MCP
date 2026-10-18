from __future__ import annotations

from typing import Any

import pytest
from mcp import Client

from open_data_mcp.server import create_server

from .conftest import BUD, REQ


async def call(service: Any, name: str, args: dict[str, Any]):
    async with Client(create_server(service)) as client:
        return await client.call_tool(name, args)


async def test_three_read_only_tools_with_schemas(service):
    async with Client(create_server(service)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"search_datasets", "get_schema", "query"}
    for tool in tools.values():
        assert tool.annotations and tool.annotations.read_only_hint is True
        assert tool.input_schema["type"] == "object" and tool.output_schema
        assert tool.description
    assert tools["query"].input_schema["required"] == ["dataset_id", "resource_id", "sql"]
    assert "source" in tools["query"].output_schema["properties"]
    assert tools["get_schema"].input_schema["required"] == ["dataset_id"]
    assert "limit" in tools["search_datasets"].input_schema["properties"]


async def test_search_tool(service):
    out = (
        await call(service, "search_datasets", {"query": "budget", "limit": 3})
    ).structured_content
    assert out["ok"] and out["datasets"][0]["dataset_id"] == "syn-regional-budget"


async def test_schema_tool(service):
    r = await call(service, "get_schema", {"dataset_id": BUD[0], "resource_id": BUD[1]})
    out = r.structured_content
    assert out["ok"] and out["schema"] and out["schema_source"] == "datastore_metadata"
    assert out["source"]["resource_id"] == BUD[1]


async def test_query_tool_returns_rows_and_provenance(service):
    args = {"dataset_id": REQ[0], "resource_id": REQ[1], "sql": "SELECT count(*) AS n FROM data"}
    out = (await call(service, "query", args)).structured_content
    assert out["ok"] and out["columns"] == ["n"] and out["rows"] == [[120]]
    assert out["source"]["dataset_id"] == REQ[0] and out["source"]["retrieved_at_utc"]
    assert out["truncated"] is False and out["row_limit"] == 20


async def test_query_tool_failure_has_no_rows_and_stable_error(service):
    args = {"dataset_id": REQ[0], "resource_id": REQ[1], "sql": "DROP TABLE data"}
    out = (await call(service, "query", args)).structured_content
    assert out["ok"] is False and out["error"]["code"] == "unsafe_sql" and out["rows"] == []
    assert out["source"] is None


async def test_invalid_arguments_are_rejected_by_schema(service):
    assert (await call(service, "query", {"dataset_id": REQ[0]})).is_error


async def test_unexpected_exception_becomes_generic_internal_error(service):
    def explode(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("secret internal detail")

    service.query = explode  # type: ignore[method-assign]
    args = {"dataset_id": "a", "resource_id": "b", "sql": "SELECT 1"}
    out = (await call(service, "query", args)).structured_content
    assert out["ok"] is False and out["error"]["code"] == "internal_error"
    assert "secret" not in str(out)


@pytest.mark.parametrize("limit", [1, 5000])
async def test_row_cap_through_mcp(service, limit):
    args = {
        "dataset_id": REQ[0],
        "resource_id": REQ[1],
        "sql": "SELECT * FROM data",
        "limit": limit,
    }
    out = (await call(service, "query", args)).structured_content
    assert out["returned_rows"] == min(limit, 100) and out["truncated"] is True
