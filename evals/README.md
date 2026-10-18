# Evaluation

All data here is **synthetic** (invented by `make_fixture.py` with fixed seeds) and is labelled
as such everywhere, including in the `source.portal` field. Nothing is real Ukrainian government
data, and no figure here says anything about data.gov.ua.

## Files

- `make_fixture.py` generates `src/open_data_mcp/fixtures/*` and `questions.jsonl`. Expected
  answers are computed with plain Python (`csv`, `collections`), independently of DuckDB.
- `questions.jsonl` holds 29 cases: 20 answerable, 7 that must be refused, 2 agent-only.
- `run_eval.py` runs them.

## Levels

| Level | Command | Measures | Needs |
|---|---|---|---|
| 1 (default) | `uv run python evals/run_eval.py` | The data path: gold SQL → MCP `query` tool → compare with the independent answer; provenance correctness; correct refusals. **Not** LLM accuracy. | nothing (offline, deterministic; used in CI) |
| 2 (optional) | `uv run python evals/run_eval.py --live` | The reference agent with a real model on the natural-language questions (substring check for numbers, dataset URL in the answer, no successful query for abstention cases). Heuristic. | `ANTHROPIC_API_KEY`, network, **costs money**. Never run in CI. Not run for this repository's README. |

## Metrics

- **Answer correctness**: numeric (explicit tolerance per case; 0 for counts), categorical and
  tabular results, including `NULL` and zero-row edge cases.
- **Provenance present and correct**: required fields exist *and* match the fixture portal's own
  metadata (ids, title, resource URL, license, `dataset_modified_at`), and `retrieved_at_utc` is a
  UTC timestamp distinct from the portal's date.
- **Correct abstention**: the tool refuses with the expected error code and returns no rows
  (missing column, XLSX, oversized file, private-network URL, plain-HTTP URL, DDL, file read).

The two `live_only` cases (forecast, opinion) are skipped at Level 1 because only a model can
answer them (correctly: by declining to give a number).

## Limitations

The suite is small, the fixtures are tiny and clean, and Level 1 uses gold SQL, so a high Level 1
score says the plumbing is sound, not that a model will choose the right SQL. The tests in
`tests/test_eval.py` check that the scorer actually fails on wrong numbers, wrong or missing
provenance and wrong refusals.

Regenerate fixtures: `uv run python evals/make_fixture.py` (a test checks the committed files
equal a fresh generation). Update the README table:
`uv run python evals/run_eval.py --update-readme`.
