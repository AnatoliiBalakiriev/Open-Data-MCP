"""Evaluation runner.

Level 1 (default, offline, deterministic): for every question, the gold SQL is sent through the
real MCP `query` tool (in-memory MCP client/server) and the result is compared with an expected
value computed independently in plain Python (evals/make_fixture.py). Two data sources:

- `synthetic`: invented fixtures served by a fake CKAN portal;
- `real_snapshot`: a committed, byte-exact snapshot of one real data.gov.ua resource (CC BY),
  replayed offline with the metadata captured at snapshot time.

Level 1 validates the data path (SQL safety, loading, provenance, refusals). It does NOT measure
the accuracy of any LLM.

Level 2 (`--live`, optional, needs ANTHROPIC_API_KEY, costs money): the reference agent answers
the natural-language question with tools; answers are checked heuristically. Never run in CI.

Usage:
    uv run python evals/run_eval.py [--markdown FILE] [--update-readme] [--live]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client

from open_data_mcp.fakeportal import (
    FAKE_BASE,
    build_offline_service,
    build_snapshot_service,
)
from open_data_mcp.provenance import REQUIRED_FIELDS
from open_data_mcp.server import create_server
from open_data_mcp.service import OpenDataService

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "evals" / "questions.jsonl"
README = ROOT / "README.md"
MARK_START, MARK_END = "<!-- eval-results:start -->", "<!-- eval-results:end -->"
UTC_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
FIXTURES = ("synthetic", "real_snapshot")
FIXTURE_LABELS = {"synthetic": "Synthetic fixtures", "real_snapshot": "Real data.gov.ua snapshot"}


@dataclass
class CaseResult:
    case_id: str
    category: str
    kind: str  # answer | abstain
    correct: bool
    provenance_ok: bool | None  # None when not applicable (no successful query)
    detail: str = ""
    fixture: str = "synthetic"


@dataclass
class Report:
    level: str
    results: list[CaseResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def counts(self, fixture: str | None = None) -> dict[str, tuple[int, int]]:
        rows = [r for r in self.results if fixture in (None, r.fixture)]
        ans = [r for r in rows if r.kind == "answer"]
        abst = [r for r in rows if r.kind == "abstain"]
        prov = [r for r in rows if r.provenance_ok is not None]
        return {
            "answer_correctness": (sum(r.correct for r in ans), len(ans)),
            "provenance": (sum(bool(r.provenance_ok) for r in prov), len(prov)),
            "abstention": (sum(r.correct for r in abst), len(abst)),
        }

    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.correct or r.provenance_ok is False]


def load_cases() -> list[dict[str, Any]]:
    return [json.loads(x) for x in QUESTIONS.read_text("utf-8").splitlines() if x.strip()]


def values_match(actual: Any, expected: Any, tol: float) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual == expected
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return abs(float(actual) - float(expected)) <= max(tol, 0.0)
    return str(actual) == str(expected)


def build_worlds() -> dict[str, tuple[OpenDataService, list[dict[str, Any]], dict[str, str]]]:
    """fixture -> (service, CKAN packages served, {dataset_id: expected dataset_url})."""
    synth_service, synth_portal = build_offline_service()
    snap_service, snap_portal = build_snapshot_service()
    return {
        "synthetic": (
            synth_service,
            synth_portal.packages,
            {p["id"]: f"{FAKE_BASE}/dataset/{p['name']}" for p in synth_portal.packages},
        ),
        "real_snapshot": (
            snap_service,
            [snap_portal.package],
            {snap_portal.package["id"]: snap_portal.meta["dataset_url"]},
        ),
    }


def truth_for(
    case: dict[str, Any],
    packages: list[dict[str, Any]],
    urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    pkg = next(p for p in packages if p["id"] == case["dataset_id"])
    res = next(r for r in pkg["resources"] if r["id"] == case["resource_id"])
    return {"pkg": pkg, "res": res, "dataset_url": (urls or {}).get(pkg["id"])}


def check_provenance(source: dict[str, Any] | None, truth: dict[str, Any]) -> tuple[bool, str]:
    """Presence AND correctness against the portal metadata the fixture was built from."""
    if not source:
        return False, "no source object"
    missing = [f for f in REQUIRED_FIELDS if not source.get(f)]
    if missing:
        return False, f"missing {missing}"
    pkg, res = truth["pkg"], truth["res"]
    expected = {
        "dataset_id": pkg["id"],
        "dataset_title": pkg.get("title"),
        "resource_id": res["id"],
        "resource_url": res.get("url"),
        "license": pkg.get("license_title"),
        "dataset_modified_at": pkg.get("metadata_modified"),
    }
    if truth.get("dataset_url"):
        expected["dataset_url"] = truth["dataset_url"]
    if res.get("last_modified"):
        expected["resource_modified_at"] = res["last_modified"]
    wrong = {k: (source.get(k), v) for k, v in expected.items() if source.get(k) != v}
    if wrong:
        return False, f"mismatch {wrong}"
    if not UTC_RE.match(source["retrieved_at_utc"]):
        return False, "retrieved_at_utc is not an ISO UTC timestamp"
    return True, ""


def score_level1(case: dict[str, Any], out: dict[str, Any], truth: dict[str, Any]) -> CaseResult:
    exp, fixture = case["expect"], case.get("fixture", "synthetic")
    if exp["type"] == "abstain":
        code = (out.get("error") or {}).get("code")
        ok = out.get("ok") is False and code in exp["error_codes"] and not out.get("rows")
        detail = f"expected {exp['error_codes']}, got ok={out.get('ok')} code={code}"
        return CaseResult(case["id"], case["category"], "abstain", ok, None, detail, fixture)
    if not out.get("ok"):
        detail = f"tool error: {out.get('error')}"
        return CaseResult(case["id"], case["category"], "answer", False, None, detail, fixture)
    rows, tol = out["rows"], exp.get("tolerance", 0.0)
    if exp["type"] == "scalar":
        correct = (
            len(rows) == 1 and len(rows[0]) == 1 and values_match(rows[0][0], exp["value"], tol)
        )
    else:
        correct = len(rows) == len(exp["rows"]) and all(
            len(a) == len(b) and all(values_match(x, y, tol) for x, y in zip(a, b, strict=True))
            for a, b in zip(rows, exp["rows"], strict=True)
        )
    prov_ok, prov_detail = check_provenance(out.get("source"), truth)
    detail = "" if correct else f"expected {exp.get('value', exp.get('rows'))!r}, got {rows!r}"
    detail = "; ".join(x for x in (detail, prov_detail) if x)
    return CaseResult(case["id"], case["category"], "answer", correct, prov_ok, detail, fixture)


async def run_level1() -> Report:
    worlds = build_worlds()
    report = Report("Level 1 - tool pipeline (offline, deterministic)")
    async with AsyncExitStack() as stack:
        clients = {
            name: await stack.enter_async_context(Client(create_server(service)))
            for name, (service, _pk, _urls) in worlds.items()
        }
        for case in load_cases():
            if case.get("live_only"):
                report.skipped.append(case["id"])
                continue
            fixture = case.get("fixture", "synthetic")
            _service, packages, urls = worlds[fixture]
            result = await clients[fixture].call_tool(
                "query",
                {
                    "dataset_id": case["dataset_id"],
                    "resource_id": case["resource_id"],
                    "sql": case["sql"],
                    "limit": 100,
                },
            )
            out = result.structured_content or {}
            report.results.append(score_level1(case, out, truth_for(case, packages, urls)))
    return report


def _numbers_in(text: str) -> set[str]:
    cleaned = text.replace("\u00a0", " ").replace(" ", "").replace(",", "")
    return set(re.findall(r"-?\d+(?:\.\d+)?", cleaned))


async def run_level2() -> Report:  # pragma: no cover - needs API key and network
    import os

    import anthropic

    from open_data_mcp.agent import McpBackend, run_agent

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is required for --live (this level calls a paid API).")

    class Adapter:
        def __init__(self) -> None:
            self.inner = anthropic.AsyncAnthropic()

        async def create(self, **kw: Any) -> Any:
            return await self.inner.messages.create(**kw)

    worlds = build_worlds()
    report = Report("Level 2 - live LLM agent (offline data, real model)")
    async with AsyncExitStack() as stack:
        clients = {
            name: await stack.enter_async_context(Client(create_server(service)))
            for name, (service, _pk, _urls) in worlds.items()
        }
        for case in load_cases():
            fixture = case.get("fixture", "synthetic")
            _service, packages, urls = worlds[fixture]
            truth = truth_for(case, packages, urls)
            res = await run_agent(
                case["question"], client=Adapter(), backend=McpBackend(clients[fixture])
            )
            ok_queries = [c for c in res.tool_calls if c.name == "query" and c.result.get("ok")]
            exp = case["expect"]
            if exp["type"] in ("abstain", "abstain_no_number"):
                report.results.append(
                    CaseResult(
                        case["id"],
                        case["category"],
                        "abstain",
                        not ok_queries,
                        None,
                        res.answer[:120],
                        fixture,
                    )
                )
                continue
            if exp["type"] == "scalar" and exp["value"] is not None:
                value = exp["value"]
                want = (
                    str(value).rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
                )
                correct = want in _numbers_in(res.answer) or want in res.answer
            else:
                correct = bool(ok_queries)
            src = ok_queries[-1].result.get("source") if ok_queries else None
            prov, why = check_provenance(src, truth)
            prov = prov and bool(src and src.get("dataset_url") in res.answer)
            report.results.append(
                CaseResult(
                    case["id"],
                    case["category"],
                    "answer",
                    correct,
                    prov,
                    why or res.answer[:120],
                    fixture,
                )
            )
    return report


def _cell(passed_total: tuple[int, int]) -> str:
    passed, total = passed_total
    return f"{passed} / {total}" if total else "n/a"


def render_markdown(report: Report) -> str:
    present = [f for f in FIXTURES if any(r.fixture == f for r in report.results)]
    head = "| Metric | " + " | ".join(FIXTURE_LABELS[f] for f in present) + " | Total |"
    lines = [
        f"**{report.level}**",
        "",
        "Synthetic fixtures are invented test data; the real snapshot is one data.gov.ua "
        "resource (CC BY) replayed offline. Details: `evals/README.md`.",
        "",
        head,
        "|---|" + "---|" * (len(present) + 1),
    ]
    metrics = (
        ("answer_correctness", "Answer correctness (numeric/categorical)"),
        ("provenance", "Provenance present and correct"),
        ("abstention", "Correct abstention / refusal"),
    )
    for key, label in metrics:
        cells = [_cell(report.counts(f)[key]) for f in present]
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {_cell(report.counts()[key])} |")
    if report.skipped:
        lines += ["", f"Skipped (agent-level only, need a live LLM): {', '.join(report.skipped)}."]
    failed = report.failures()
    tail = " — " + ", ".join(r.case_id for r in failed) if failed else ""
    lines += ["", f"Failed cases: {len(failed)}{tail}"]
    return "\n".join(lines)


def print_report(report: Report) -> None:
    print(render_markdown(report))
    print("\nPer-case results:")
    for r in report.results:
        flag = "PASS" if r.correct and r.provenance_ok is not False else "FAIL"
        extra = f"  [{r.detail}]" if flag == "FAIL" else ""
        print(f"  {flag}  {r.case_id:30} {r.fixture:14} {r.category:8} {r.kind}{extra}")


def update_readme(markdown: str) -> None:
    text = README.read_text("utf-8")
    if MARK_START not in text or MARK_END not in text:
        sys.exit("README.md does not contain the eval-results markers.")
    head, rest = text.split(MARK_START, 1)
    _, tail = rest.split(MARK_END, 1)
    README.write_text(f"{head}{MARK_START}\n{markdown}\n{MARK_END}{tail}", "utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Level 2: real LLM (costs money).")
    parser.add_argument("--markdown", type=Path, help="Write the summary table to this file.")
    parser.add_argument("--update-readme", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run_level2() if args.live else run_level1())
    print_report(report)
    md = render_markdown(report)
    if args.markdown:
        args.markdown.write_text(md + "\n", "utf-8")
    if args.update_readme:
        update_readme(md)
    return 1 if report.failures() else 0


if __name__ == "__main__":
    raise SystemExit(main())
