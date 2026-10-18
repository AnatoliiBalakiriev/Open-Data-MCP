from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

from mcp import Client

from open_data_mcp.agent import SYSTEM_PROMPT, McpBackend, run_agent
from open_data_mcp.server import create_server

from .conftest import REQ


def text(t: str) -> NS:
    return NS(type="text", text=t)


def tool_use(id_: str, name: str, **args: Any) -> NS:
    return NS(type="tool_use", id=id_, name=name, input=args)


def reply(stop: str, *blocks: NS) -> NS:
    return NS(stop_reason=stop, content=list(blocks))


class ScriptedModel:
    """Stands in for the Anthropic Messages API; records every request."""

    def __init__(self, *responses: NS) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> NS:
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        return self._responses.pop(0)


QUERY = {"dataset_id": REQ[0], "resource_id": REQ[1], "sql": "SELECT count(*) FROM data"}


async def test_tool_loop_end_to_end_and_provenance_is_appended(service):
    model = ScriptedModel(
        reply("tool_use", text("Let me check."), tool_use("t1", "query", **QUERY)),
        reply("end_turn", text("There are 120 requests.")),
    )
    async with Client(create_server(service)) as mcp:
        result = await run_agent("How many requests?", client=model, backend=McpBackend(mcp))
    assert result.stop_reason == "end_turn" and result.iterations == 2
    assert [c.name for c in result.tool_calls] == ["query"]
    assert result.tool_calls[0].result["rows"] == [[120]]
    assert "120 requests" in result.answer
    assert "Sources (from tool output)" in result.answer  # model forgot; client adds it
    assert "https://portal.fixtures.invalid/dataset/syn-service-requests-2024" in result.answer
    assert result.sources[0]["resource_id"] == REQ[1]
    # tool schemas and system prompt reached the model; tool result fed back
    first, second = model.requests
    assert {t["name"] for t in first["tools"]} == {"search_datasets", "get_schema", "query"}
    assert first["system"] == SYSTEM_PROMPT
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "t1"
    assert '"rows"' in tool_result["content"] and "is_error" not in tool_result


async def test_citation_not_duplicated_when_model_already_cites(service):
    url = "https://portal.fixtures.invalid/dataset/syn-service-requests-2024"
    model = ScriptedModel(
        reply("tool_use", tool_use("t1", "query", **QUERY)),
        reply("end_turn", text(f"120 requests. Source: {url}")),
    )
    async with Client(create_server(service)) as mcp:
        result = await run_agent("q", client=model, backend=McpBackend(mcp))
    assert "Sources (from tool output)" not in result.answer


async def test_failed_tool_is_flagged_and_no_citation_invented(service):
    bad = {**QUERY, "sql": "DROP TABLE data"}
    model = ScriptedModel(
        reply("tool_use", tool_use("t1", "query", **bad)),
        reply("end_turn", text("I could not retrieve the data.")),
    )
    async with Client(create_server(service)) as mcp:
        result = await run_agent("delete", client=model, backend=McpBackend(mcp))
    assert result.tool_calls[0].is_error
    assert model.requests[1]["messages"][-1]["content"][0]["is_error"] is True
    assert "Sources" not in result.answer and result.sources == []


async def test_parallel_tool_calls_return_in_one_user_message(service):
    model = ScriptedModel(
        reply(
            "tool_use",
            tool_use("a", "search_datasets", query="budget"),
            tool_use("b", "query", **QUERY),
        ),
        reply("end_turn", text("done")),
    )
    async with Client(create_server(service)) as mcp:
        await run_agent("q", client=model, backend=McpBackend(mcp))
    results = model.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


async def test_loop_is_bounded(service):
    forever = [
        reply("tool_use", tool_use(f"t{i}", "search_datasets", query="x")) for i in range(20)
    ]
    model = ScriptedModel(*forever)
    async with Client(create_server(service)) as mcp:
        result = await run_agent("q", client=model, backend=McpBackend(mcp), max_iterations=3)
    assert result.stop_reason == "max_iterations" and len(model.requests) == 3
    assert "without a final answer" in result.answer


async def test_max_tokens_and_refusal_do_not_pass_off_partial_text(service):
    async with Client(create_server(service)) as mcp:
        backend = McpBackend(mcp)
        cut = await run_agent(
            "q", client=ScriptedModel(reply("max_tokens", text("The answer is 4"))), backend=backend
        )
        refused = await run_agent("q", client=ScriptedModel(reply("refusal")), backend=backend)
    assert cut.stop_reason == "max_tokens" and "The answer is 4" not in cut.answer
    assert refused.stop_reason == "refusal"


def test_system_prompt_states_the_non_negotiables():
    for phrase in ("Never guess", "source", "unknown", "future", "partial"):
        assert phrase.lower() in SYSTEM_PROMPT.lower()


async def test_agent_over_real_stdio_subprocess_with_offline_fixtures():
    """Spawns `python -m open_data_mcp serve --offline-fixtures` and talks MCP over stdio."""
    from open_data_mcp.cli import _server_params

    model = ScriptedModel(
        reply("tool_use", tool_use("t1", "query", **QUERY)),
        reply("end_turn", text("120.")),
    )
    async with Client(_server_params(offline=True)) as mcp:
        result = await run_agent("count", client=model, backend=McpBackend(mcp))
    assert result.tool_calls[0].result["rows"] == [[120]]
    assert result.tool_calls[0].result["source"]["portal"].startswith("offline-fixtures")
