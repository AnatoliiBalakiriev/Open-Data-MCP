"""Turn profile numbers into a ranked list of findings (the "quality passport").

The thresholds below are judgement calls, not facts about the data. They are kept in one place and
published in `METHOD_NOTE`, so a reader can disagree with a finding and see why it was raised.
"""

from __future__ import annotations

from typing import Any

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

MISSING_MEDIUM = 0.30
MISSING_HIGH = 0.80
PLACEHOLDER_MEDIUM_SHARE = 0.05
SPARSE_HIGH_SHARE = 0.10
DUPLICATE_HIGH_SHARE = 0.05
BROKEN_DATES_HIGH_SHARE = 0.01
PADDED_LOW_SHARE = 0.01

METHOD_NOTE = (
    "Missing = empty cell or a placeholder such as 'null', '-', 'н/д', 'немає' (list in the "
    "docs). Numbers: a comma is read as the decimal separator; integers with a leading zero are "
    "treated as codes. Outliers use Tukey fences (1.5 x IQR mild, 3 x IQR extreme) and are "
    "candidates for review, not errors: skewed data legitimately has many. Dates: day-first order "
    f"is assumed; years outside 1900-2100 count as out of range. Severity thresholds: a column "
    f"is a finding from {MISSING_MEDIUM:.0%} missing (high from {MISSING_HIGH:.0%}); duplicates "
    f"are high from {DUPLICATE_HIGH_SHARE:.0%} of rows; broken dates are high from "
    f"{BROKEN_DATES_HIGH_SHARE:.0%} of date values. Everything describes the rows actually "
    "profiled (see `coverage`)."
)


def _issue(severity: str, check: str, message: str, column: str | None = None) -> dict[str, Any]:
    return {"severity": severity, "check": check, "column": column, "message": message}


def _pct(share: float) -> str:
    return f"{share:.1%}"


def build_issues(
    profile: dict[str, Any],
    *,
    coverage: dict[str, Any],
    encoding: dict[str, Any] | None,
    drift: dict[str, Any] | None,
    malformed_rows: int | None,
    single_column_warning: bool,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    rows = profile["rows"]
    if not coverage["complete"]:
        issues.append(
            _issue(
                "medium",
                "partial_coverage",
                f"Only the first {rows} rows were profiled, not the whole resource. Every number "
                "below describes that prefix (file order, not a random sample).",
            )
        )
    if single_column_warning:
        issues.append(
            _issue(
                "high",
                "not_a_clean_table",
                "The file parsed into a single column whose name contains delimiter characters; "
                "it is probably not a clean table (title rows, a wrong delimiter).",
            )
        )
    if rows == 0:
        issues.append(_issue("high", "no_rows", "The table has a header but no data rows."))
    if malformed_rows:
        share = malformed_rows / max(rows + malformed_rows, 1)
        issues.append(
            _issue(
                "high" if share >= 0.01 else "medium",
                "malformed_rows",
                f"{malformed_rows} CSV lines do not fit the table (wrong number of fields or "
                "broken quoting) and were left out of the profile.",
            )
        )
    _row_level(profile, issues)
    _grouped_column_findings(profile["columns"], issues)
    for column in profile["columns"]:
        _column_level(column, issues)
    if encoding is not None:
        _encoding_level(encoding, issues)
    if drift is not None:
        _drift_level(drift, issues)
    issues.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], i["check"], i["column"] or ""))
    return issues


def _row_level(profile: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    dup = profile["duplicates"]
    if dup["exact_duplicate_rows"]:
        share = dup["exact_duplicate_share"]
        issues.append(
            _issue(
                "high" if share >= DUPLICATE_HIGH_SHARE else "medium",
                "exact_duplicate_rows",
                f"{dup['exact_duplicate_rows']} rows ({_pct(share)}) repeat an earlier row in "
                f"every column, in {dup['duplicated_groups']} groups.",
            )
        )
    for key in dup["key_candidates"]:
        issues.append(
            _issue(
                "medium",
                "repeated_identifier",
                f"{key['rows_beyond_first']} rows repeat an identifier already used "
                f"(e.g. {', '.join(key['examples'])}).",
                key["column"],
            )
        )
    sparse = profile["sparse_rows"]
    if sparse["count"]:
        issues.append(
            _issue(
                "high" if sparse["share"] >= SPARSE_HIGH_SHARE else "medium",
                "sparse_rows",
                f"{sparse['count']} rows ({_pct(sparse['share'])}) are at least "
                f"{sparse['threshold']:.0%} empty.",
            )
        )


def _listed(items: list[str], limit: int = 8) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _grouped_column_findings(columns: list[dict[str, Any]], issues: list[dict[str, Any]]) -> None:
    """One finding for many similar columns; the per-column numbers stay in `columns`."""
    sparse = [c for c in columns if c["kind"] != "empty" and c["missing_share"] >= MISSING_MEDIUM]
    if sparse:
        worst = max(c["missing_share"] for c in sparse)
        issues.append(
            _issue(
                "high" if worst >= MISSING_HIGH else "medium",
                "missing_values",
                f"{len(sparse)} columns are at least {MISSING_MEDIUM:.0%} missing: "
                + _listed([f"{c['name']} {_pct(c['missing_share'])}" for c in sparse])
                + ".",
            )
        )
    constant = [c["name"] for c in columns if c["constant"] and c["kind"] != "empty"]
    if constant:
        issues.append(
            _issue(
                "low",
                "constant_column",
                f"{len(constant)} columns hold one single value in every present cell: "
                + _listed(constant)
                + ".",
            )
        )


def _column_level(column: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    name = column["name"]
    if column["kind"] == "empty":
        issues.append(_issue("high", "column_empty", "No usable value in any row.", name))
    if column["placeholders"]:
        share = column["placeholders"] / max(column["non_missing"] + column["placeholders"], 1)
        shown = ", ".join(f"'{v}' x{n}" for v, n in column["placeholder_values"].items())
        issues.append(
            _issue(
                "medium" if share >= PLACEHOLDER_MEDIUM_SHARE else "low",
                "placeholder_values",
                f"{column['placeholders']} cells hold text that stands for 'no value' ({shown}); "
                "they are counted as missing.",
                name,
            )
        )
    if column["non_numeric_values"]:
        issues.append(
            _issue(
                "medium",
                "numbers_mixed_with_text",
                f"{column['non_numeric_values']} values in a mostly numeric column are not "
                f"numbers (e.g. {', '.join(repr(e) for e in column['non_numeric_examples'][:3])}).",
                name,
            )
        )
    numeric = column["numeric"]
    if numeric and numeric["outliers"] and numeric["outliers"]["extreme"]:
        out = numeric["outliers"]
        issues.append(
            _issue(
                "low",
                "numeric_outliers",
                f"{out['extreme']} extreme and {out['mild']} mild outliers outside "
                f"[{out['low_fence']}, {out['high_fence']}] (e.g. "
                f"{', '.join(str(x) for x in out['examples'][:3])}); review before using.",
                name,
            )
        )
    if column["dates"]:
        _date_level(name, column, issues)
    _damage_level(name, column, issues)


def _date_level(name: str, column: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    d = column["dates"]
    bad = d["impossible"] + d["not_a_date"] + d["out_of_range"] + d["ambiguous_two_digit_year"]
    if bad:
        parts = [
            f"{d[k]} {label}"
            for k, label in (
                ("impossible", "impossible calendar dates"),
                ("not_a_date", "not dates at all"),
                ("out_of_range", f"outside {d['valid_year_range'][0]}-{d['valid_year_range'][1]}"),
                ("ambiguous_two_digit_year", "with a two-digit year"),
            )
            if d[k]
        ]
        samples = [e for v in d["examples"].values() for e in v][:4]
        share = bad / max(column["non_missing"], 1)
        issues.append(
            _issue(
                "high" if share >= BROKEN_DATES_HIGH_SHARE else "medium",
                "broken_dates",
                f"{bad} of {column['non_missing']} date values are unusable: "
                f"{', '.join(parts)} (e.g. {', '.join(repr(s) for s in samples)}).",
                name,
            )
        )
    if d["sentinel"]:
        issues.append(
            _issue(
                "low",
                "sentinel_dates",
                f"{d['sentinel']} values are 1900-01-01 or 1970-01-01, common stand-ins for "
                "'unknown'.",
                name,
            )
        )
    if d["mixed_layouts"]:
        issues.append(
            _issue(
                "medium",
                "mixed_date_formats",
                f"Several date layouts in one column: {', '.join(d['layouts'])}.",
                name,
            )
        )


def _damage_level(name: str, column: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    damage = column["text_damage"]
    if not damage:
        return
    if damage["mojibake_values"]:
        issues.append(
            _issue(
                "high",
                "mojibake",
                f"{damage['mojibake_values']} values look like UTF-8 text read as a single-byte "
                f"code page (e.g. {', '.join(repr(e) for e in damage['mojibake_examples'][:2])}).",
                name,
            )
        )
    if damage["replacement_character_values"]:
        issues.append(
            _issue(
                "medium",
                "replacement_characters",
                f"{damage['replacement_character_values']} values contain U+FFFD, left by a "
                "lossy conversion; the original text is gone.",
                name,
            )
        )
    if damage["control_character_values"]:
        issues.append(
            _issue(
                "low",
                "control_characters",
                f"{damage['control_character_values']} values contain control characters.",
                name,
            )
        )
    present = max(column["non_missing"], 1)
    if damage["padded_values"] / present >= PADDED_LOW_SHARE:
        issues.append(
            _issue(
                "low",
                "padded_values",
                f"{damage['padded_values']} values have leading or trailing whitespace, which "
                "breaks exact matching and grouping.",
                name,
            )
        )


def _encoding_level(encoding: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    detected = encoding["detected"]
    if detected in ("mixed", "undecodable"):
        issues.append(_issue("high", "mixed_encoding", encoding["note"]))
    elif detected == "windows-1251":
        issues.append(_issue("low", "legacy_encoding", encoding["note"]))
    if encoding["replacement_characters"]:
        issues.append(
            _issue(
                "medium",
                "replacement_characters_in_file",
                f"The file already contains {encoding['replacement_characters']} U+FFFD "
                "replacement characters.",
            )
        )
    if encoding["control_characters"]:
        issues.append(
            _issue(
                "low",
                "control_characters_in_file",
                f"The file contains {encoding['control_characters']} control characters.",
            )
        )
    if encoding["bom"]:
        issues.append(_issue("info", "byte_order_mark", "The file starts with a UTF-8 BOM."))


def _drift_level(drift: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    for version in drift["compared"]:
        if version["same_schema"]:
            continue
        changes = []
        if version["columns_added"]:
            changes.append(f"added {version['columns_added']}")
        if version["columns_removed"]:
            changes.append(f"removed {version['columns_removed']}")
        if version["possible_renames"]:
            changes.append(
                "renamed "
                + ", ".join(f"{r['from']!r}->{r['to']!r}" for r in version["possible_renames"])
            )
        if version["column_order_changed"]:
            changes.append("column order changed")
        if version["type_changes"]:
            changes.append(
                "type changed "
                + ", ".join(
                    f"{t['column']!r} {t['reference_kind']}->{t['other_kind']}"
                    for t in version["type_changes"]
                )
            )
        issues.append(
            _issue(
                "medium",
                "schema_drift",
                f"Sibling file {version['name'] or version['resource_id']!r} differs from this "
                f"one: {'; '.join(changes)}.",
            )
        )
