# Evaluation

Two data sources, labelled everywhere (including in `source.portal`):

- **`synthetic`** — invented by `make_fixture.py` with fixed seeds, served by a fake CKAN portal.
  Nothing in it is real government data.
- **`real_snapshot`** — one real resource from data.gov.ua (education institutions of
  Ivano-Frankivsk, CC BY), committed byte-for-byte under `src/open_data_mcp/fixtures/real/` with
  the metadata captured at snapshot time (`SNAPSHOT.json`) and attribution (`ATTRIBUTION.md`).
  It is replayed offline: provenance reports the real dataset/resource URLs, license and the
  portal's own modification date, while `portal` says `OFFLINE REPLAY` and `retrieved_at_utc` is the
  replay time. It was captured with `scripts/capture_snapshot.py` (needs network); the sha256 is
  verified by a test.

## Files

- `make_fixture.py` generates the synthetic fixtures and `questions.jsonl`. Expected answers —
  for both sources — are computed with plain Python (`csv`, `collections`), independently of
  DuckDB. The real snapshot is *input* to it and is never regenerated.
- `questions.jsonl` holds 42 cases: 31 answerable (20 synthetic + 11 real), 8 that must be
  refused (7 + 1), and 3 agent-only (2 + 1).
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

Results are reported per source and in total.

The three `live_only` cases (forecasts, opinion) are skipped at Level 1 because only a model can
answer them (correctly: by declining to give a number).

## Limitations

The suite is small and the real part rests on a single 128-row file; the synthetic fixtures are tiny and clean, and Level 1 uses gold SQL, so a high Level 1
score says the plumbing is sound, not that a model will choose the right SQL. The tests in
`tests/test_eval.py` check that the scorer actually fails on wrong numbers, wrong or missing
provenance and wrong refusals.

Regenerate fixtures: `uv run python evals/make_fixture.py` (a test checks the committed files
equal a fresh generation). Update the README table:
`uv run python evals/run_eval.py --update-readme`.

## About the real snapshot's questions

The source file has quirks (repeated `Identifier` values; rows where `numberofchildren` is larger
than `numberofseats`) and the portal does not define what the columns mean. The questions therefore
refer to columns literally ("rows where `numberofchildren` > `numberofseats`"), and nothing here
claims what those numbers represent. Several questions use Cyrillic strings, exercising the
Windows-1251 decoding path on real data.
