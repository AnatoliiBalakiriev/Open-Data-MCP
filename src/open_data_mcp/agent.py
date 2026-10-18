"""Reference agent: Anthropic Messages API + MCP tools in an explicit, bounded tool loop."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "claude-sonnet-5-5"  # verified against the official models overview page
MAX_TOOL_RESULT_CHARS = 20_000

SYSTEM_PROMPT = """\
You answer questions about open datasets (Ukrainian government open data) using three tools:
search_datasets, get_schema and query.

Rules:
- For any factual or numeric question about a dataset, use the tools. Never guess counts, sums,
  dates, units or names, and never rely on memory for dataset contents.
- Typical flow: search_datasets -> get_schema (check column names/types and whether the schema is
  authoritative) -> query with one read-only SELECT over the table `data`. Quote column names that
  contain spaces or non-ASCII characters with double quotes.
- Base every claim on actual tool results. If a tool returns ok=false, an empty result, a
  truncated result you depend on, or the data cannot answer the question, say so plainly and do
  not produce a number.
- `truncated` only limits the returned rows, not the SQL scope; aggregates cover the whole
  resource. Never present a partial sample as if it were the complete dataset.
- Every data-backed answer must name its source: dataset title, dataset URL, resource, license,
  the portal's last-modified date (say "unknown" if it is null) and the retrieval time in UTC.
  The retrieval time is when we fetched the data, not when it was published.
- Questions about the future, opinions, or things the data does not contain: explain that the
  data cannot answer them instead of inventing an answer.
- Answer in the user's language."""


class ToolBackend(Protocol):
    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Return (structured_result, is_error)."""
        ...


class MessagesClient(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    is_error: bool


@dataclass
class AgentResult:
    answer: str
    stop_reason: str  # end_turn | max_iterations | max_tokens | refusal | unexpected
    iterations: int
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    @property
    def sources(self) -> list[dict[str, Any]]:
        return [
            c.result["source"]
            for c in self.tool_calls
            if c.name == "query" and c.result.get("ok") and c.result.get("source")
        ]


def _text_of(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


def _truncate(payload: str) -> str:
    if len(payload) <= MAX_TOOL_RESULT_CHARS:
        return payload
    return payload[:MAX_TOOL_RESULT_CHARS] + "\n…[tool output truncated by the client]"


def append_citations(answer: str, calls: list[ToolCallRecord]) -> str:
    """Guarantee that every successful query's provenance is visible in the final answer."""
    seen: set[str] = set()
    lines: list[str] = []
    for call in calls:
        if call.name != "query" or not call.result.get("ok"):
            continue
        citation = call.result.get("citation")
        source = call.result.get("source") or {}
        key = f"{source.get('dataset_id')}/{source.get('resource_id')}"
        url = source.get("dataset_url")
        if not citation or key in seen:
            continue
        seen.add(key)
        if url and url in answer:
            continue  # the model already cited this dataset
        lines.append(f"- {citation}")
    if not lines:
        return answer
    return answer.rstrip() + "\n\nSources (from tool output):\n" + "\n".join(lines)


async def run_agent(
    question: str,
    *,
    client: MessagesClient,
    backend: ToolBackend,
    model: str = DEFAULT_MODEL,
    max_iterations: int = 8,
    max_tokens: int = 8000,
) -> AgentResult:
    tools = [
        {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in await backend.list_tools()
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    calls: list[ToolCallRecord] = []

    for iteration in range(1, max_iterations + 1):
        response = await client.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        stop = getattr(response, "stop_reason", None)
        if stop == "tool_use":
            # Keep the assistant turn exactly as returned (thinking blocks must round-trip).
            messages.append({"role": "assistant", "content": response.content})
            results: list[dict[str, Any]] = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                args = block.input if isinstance(block.input, dict) else {}
                result, is_error = await backend.call_tool(block.name, args)
                calls.append(ToolCallRecord(block.name, args, result, is_error))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _truncate(json.dumps(result, ensure_ascii=False)),
                        **({"is_error": True} if is_error else {}),
                    }
                )
            messages.append({"role": "user", "content": results})
            continue
        text = _text_of(response.content)
        if stop == "end_turn":
            return AgentResult(append_citations(text, calls), "end_turn", iteration, calls)
        if stop == "max_tokens":
            note = "The model's answer was cut off (max_tokens); no reliable answer was produced."
            return AgentResult(note, "max_tokens", iteration, calls)
        if stop == "refusal":
            return AgentResult(
                "The model declined to answer this request.", "refusal", iteration, calls
            )
        return AgentResult(
            text or f"Unexpected stop reason: {stop}", "unexpected", iteration, calls
        )

    return AgentResult(
        f"Stopped after {max_iterations} model calls without a final answer. "
        "No result is reported because the tool loop did not finish.",
        "max_iterations",
        max_iterations,
        calls,
    )


class McpBackend:
    """Adapts an open `mcp.Client` to the ToolBackend protocol."""

    def __init__(self, mcp_client: Any) -> None:
        self._client = mcp_client

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._client.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        result = await self._client.call_tool(name, arguments)
        if result.structured_content is not None:
            payload = dict(result.structured_content)
        else:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            payload = {"ok": False, "error": {"code": "tool_error", "message": text[:500]}}
        return payload, bool(result.is_error) or payload.get("ok") is False
