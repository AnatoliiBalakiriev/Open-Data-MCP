"""Command line entry point: `open-data-mcp serve | ask | tools`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters

SETUP_HINT = (
    "ANTHROPIC_API_KEY is not set. Copy .env.example to .env, put your key there "
    "(or `export ANTHROPIC_API_KEY=...`) and retry. `serve`, the tests and the offline "
    "evaluation do not need it."
)


def load_dotenv(path: Path = Path(".env")) -> None:
    """Tiny .env reader (KEY=VALUE lines); never overrides variables already set."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# Only these reach the server subprocess. ANTHROPIC_* is deliberately absent: the server does not
# need the API key. Proxy/CA variables are passed so the server works behind a corporate proxy.
_PASSTHROUGH_PREFIXES = (
    "OPEN_DATA_",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)  # fmt: skip


def _server_params(offline: bool) -> StdioServerParameters:
    args = ["-m", "open_data_mcp", "serve"] + (["--offline-fixtures"] if offline else [])
    env = {k: v for k, v in os.environ.items() if k.startswith(_PASSTHROUGH_PREFIXES)}
    return StdioServerParameters(command=sys.executable, args=args, env=env or None)


async def _ask(question: str, model: str, offline: bool, max_iterations: int, as_json: bool) -> int:
    import anthropic
    from mcp import Client

    from .agent import McpBackend, run_agent

    class _Adapter:
        def __init__(self, inner: anthropic.AsyncAnthropic) -> None:
            self._inner = inner

        async def create(self, **kwargs: Any) -> Any:
            return await self._inner.messages.create(**kwargs)

    async with Client(_server_params(offline)) as mcp_client:
        result = await run_agent(
            question,
            client=_Adapter(anthropic.AsyncAnthropic()),
            backend=McpBackend(mcp_client),
            model=model,
            max_iterations=max_iterations,
        )
    if as_json:
        print(
            json.dumps(
                {
                    "answer": result.answer,
                    "stop_reason": result.stop_reason,
                    "iterations": result.iterations,
                    "tool_calls": [c.name for c in result.tool_calls],
                    "sources": result.sources,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(result.answer)
        print(
            f"\n[{result.iterations} model call(s); tools: "
            f"{', '.join(c.name for c in result.tool_calls) or 'none'}]",
            file=sys.stderr,
        )
    return 0 if result.stop_reason == "end_turn" else 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    from .agent import DEFAULT_MODEL

    parser = argparse.ArgumentParser(prog="open-data-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the MCP server over stdio.")
    serve.add_argument(
        "--offline-fixtures",
        action="store_true",
        help="Serve SYNTHETIC fixtures instead of the live portal (no network).",
    )

    ask = sub.add_parser("ask", help="Ask a question through the reference agent.")
    ask.add_argument("question")
    ask.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL))
    ask.add_argument("--max-iterations", type=int, default=8)
    ask.add_argument(
        "--offline-fixtures",
        action="store_true",
        help="Use SYNTHETIC fixtures (still calls the Anthropic API).",
    )
    ask.add_argument("--json", action="store_true", help="Print a JSON result.")

    args = parser.parse_args(argv)
    if args.command == "serve":
        from .server import run_stdio

        run_stdio(offline_fixtures=args.offline_fixtures)
        return 0
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print(SETUP_HINT, file=sys.stderr)
        return 2
    return asyncio.run(
        _ask(args.question, args.model, args.offline_fixtures, args.max_iterations, args.json)
    )


if __name__ == "__main__":
    raise SystemExit(main())
