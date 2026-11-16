"""Data-quality statistics for one loaded table.

Everything is measured on the table exactly as loaded; nothing is guessed about what the data
"should" look like. Cells are examined as raw text so that a value such as "31.02.2023" or "null"
is seen as the publisher wrote it, not as something DuckDB already coerced or dropped.

Judgement calls (which share counts as "mostly missing", whether an outlier is an error) are not
made here: `profile_issues` turns these numbers into findings and says which thresholds it used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .config import Settings
from .errors import SqlError
from .query_engine import ColumnInfo, TableSession, quote_identifier, sql_literal

# Text that stands for "no value" in published tables (compared after trim + lower-casing).
PLACEHOLDER_VALUES = frozenset(
    {
        "null", "none", "nan", "nil", "n/a", "#n/a", "undefined",
        "-", "--", "---", "—", "–", "?", "??",
        "немає", "нема", "н/д", "н.д.", "відсутній", "відсутня", "відсутнє",
        "не вказано", "не зазначено", "не визначено", "невідомо",
        "0000-00-00",
    }
)  # fmt: skip

# Plain number, or a number grouped with spaces/NBSP ("1 234,5"). A comma is read as the decimal
# separator. Integers with a leading zero ("04057287") are codes, not numbers.
_INT_RE = r"[+-]?(0|[1-9][0-9]*)"
_NUM_PLAIN_RE = _INT_RE + r"([.,][0-9]+)?"
_NUM_GROUPED_RE = r"[+-]?[1-9][0-9]{0,2}([ \x{00A0}][0-9]{3})+([.,][0-9]+)?"
_DATE_LIKE_RE = (
    r"\d{1,4}[-./]\d{1,2}[-./]\d{1,4}([ T]\d{1,2}:\d{2}(:\d{2}([.,]\d{1,6})?)?)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?"
)
_PADDED_RE = r"^[\s\x{00A0}]|[\s\x{00A0}]$"
_CONTROL_RE = r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]"
_NON_ASCII_RE = r"[^\x00-\x7F]"


def _mojibake_pairs() -> str:
    """Lead character + continuation character of UTF-8 Cyrillic read as a 1-byte code page.

    Cyrillic in UTF-8 is `D0|D1 80..BF`. Read as Windows-1251 that is "Р"/"С" plus a symbol; read
    as Windows-1252 it is "Ð"/"Ñ" plus a symbol. Real mojibake is made almost entirely of such
    pairs, while a legitimate "Рівне" contains one.
    """

    def tail(codec: str) -> str:
        chars = []
        for byte in range(0x80, 0xC0):
            try:
                chars.append(bytes([byte]).decode(codec))
            except UnicodeDecodeError:
                continue
        return "".join(chars)

    return f"([РС][{tail('cp1251')}])|([ÐÑ][{tail('cp1252')}])"


_MOJIBAKE_RE = _mojibake_pairs()
_MOJIBAKE_MIN_PAIRS = 3
_MOJIBAKE_MIN_PAIR_SHARE = 0.8  # share of non-ASCII characters that must sit inside such pairs

_KEY_NAME_TOKENS = frozenset({"id", "uid", "uuid", "guid", "pk", "key", "код", "ід", "номер"})
_YEAR_RANGE = (1900, 2100)
_SENTINEL_DATES = frozenset({date(1900, 1, 1), date(1970, 1, 1)})
_MIN_VALUES_FOR_OUTLIERS = 20
SPARSE_ROW_MISSING_SHARE = 0.8
_EXAMPLES = 3
_EXAMPLE_CHARS = 60


# --------------------------------------------------------------------------- date judgement


_DATE_PARSE = re.compile(
    r"^(\d{1,4})([-./])(\d{1,2})\2(\d{1,4})"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:[.,]\d{1,6})?)?)?\s*(?:Z|[+-]\d{2}:?\d{2})?$"
)


@dataclass(frozen=True)
class DateVerdict:
    status: str  # valid | impossible | ambiguous | not_a_date
    layout: str | None  # e.g. "DD.MM.YYYY"; None when not a date
    time_label: str | None  # "HH:MM" | "HH:MM:SS" | None
    value: date | None


def classify_date(text: str) -> DateVerdict:
    """Judge one date-shaped string. Day-first is assumed for `D.M.Y`, `D/M/Y` and `D-M-Y`."""
    match = _DATE_PARSE.match(text.strip())
    if match is None:
        return DateVerdict("not_a_date", None, None, None)
    first, sep, second, third, hour, minute, second_of_minute = match.groups()
    time_label = None
    if hour is not None:
        time_label = "HH:MM:SS" if second_of_minute is not None else "HH:MM"
    if len(first) == 4 and len(second) <= 2 and len(third) <= 2:
        layout = f"YYYY{sep}MM{sep}DD"
        year, month, day = int(first), int(second), int(third)
    elif len(third) == 4 and len(first) <= 2 and len(second) <= 2:
        layout = f"DD{sep}MM{sep}YYYY"
        year, month, day = int(third), int(second), int(first)
    elif len(third) == 2 and len(first) <= 2 and len(second) <= 2:
        return DateVerdict("ambiguous", f"DD{sep}MM{sep}YY", time_label, None)
    else:
        return DateVerdict("not_a_date", None, None, None)
    try:
        value = date(year, month, day)
    except ValueError:
        return DateVerdict("impossible", layout, time_label, None)
    if hour is not None and (
        int(hour) > 23 or int(minute) > 59 or (second_of_minute and int(second_of_minute) > 59)
    ):
        return DateVerdict("impossible", layout, time_label, None)
    return DateVerdict("valid", layout, time_label, value)


# --------------------------------------------------------------------------- SQL fragments


class _Col:
    """SQL fragments for column `i` of the text view `p`."""

    def __init__(self, i: int) -> None:
        self.raw = f"c{i}"
        self.value = f"trim(replace({self.raw}, chr(160), ' '))"
        self.empty = f"({self.raw} IS NULL OR {self.value} = '')"
        placeholders = ", ".join(sql_literal(v) for v in sorted(PLACEHOLDER_VALUES))
        self.placeholder = f"(lower({self.value}) IN ({placeholders}))"
        self.missing = f"({self.empty} OR {self.placeholder})"
        self.present = f"(NOT {self.missing})"
        plain, grouped = sql_literal(_NUM_PLAIN_RE), sql_literal(_NUM_GROUPED_RE)
        self.number = (
            f"CASE WHEN regexp_full_match({self.value}, {plain}) "
            f"THEN try_cast(replace({self.value}, ',', '.') AS DOUBLE) "
            f"WHEN regexp_full_match({self.value}, {grouped}) THEN try_cast(replace(replace("
            f"replace({self.value}, ' ', ''), chr(160), ''), ',', '.') AS DOUBLE) END"
        )
        pairs = f"len(regexp_extract_all({self.raw}, {sql_literal(_MOJIBAKE_RE)}))"
        non_ascii = f"len(regexp_extract_all({self.raw}, {sql_literal(_NON_ASCII_RE)}))"
        self.mojibake = (
            f"({pairs} >= {_MOJIBAKE_MIN_PAIRS} AND "
            f"{pairs} * 2 >= {_MOJIBAKE_MIN_PAIR_SHARE} * {non_ascii})"
        )


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= _EXAMPLE_CHARS else text[: _EXAMPLE_CHARS - 1] + "…"


def _number(value: Any) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() and abs(number) < 1e15 else round(number, 6)


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


# --------------------------------------------------------------------------- the profile


def profile_table(session: TableSession, settings: Settings) -> dict[str, Any]:
    """Profile the table `data` held by `session`. Columns beyond the configured cap are skipped."""
    columns = session.columns[: settings.profile_max_columns]
    notes: list[str] = []
    if len(session.columns) > len(columns):
        notes.append(
            f"The table has {len(session.columns)} columns; only the first {len(columns)} were "
            "profiled (and duplicates/sparse rows were judged on those)."
        )
    rows = session.row_count
    with session.time_budget(settings.profile_timeout_s):
        _create_view(session, columns)
        profiled = [_profile_column(session, c.name, _Col(i), rows) for i, c in enumerate(columns)]
        duplicates = _duplicates(session, columns_count=len(columns), profiled=profiled, rows=rows)
        sparse = _sparse_rows(session, len(columns), rows)
    return {
        "rows": rows,
        "columns_total": len(session.columns),
        "columns_profiled": len(columns),
        "columns": profiled,
        "duplicates": duplicates,
        "sparse_rows": sparse,
        "notes": notes,
    }


def column_kinds(session: TableSession, settings: Settings) -> dict[str, str]:
    """Column name -> inferred kind (cheap: no examples, no outliers). Used for schema drift."""
    columns = session.columns[: settings.profile_max_columns]
    _create_view(session, columns)
    kinds: dict[str, str] = {}
    for i, c in enumerate(columns):
        *_, present, _distinct, n_int, n_num, n_date = _count_column(
            session, _Col(i), session.row_count
        )
        kinds[c.name] = _kind(present, n_int, n_num, n_date)
    return kinds


def _create_view(session: TableSession, columns: list[ColumnInfo]) -> None:
    selects = ", ".join(
        f"CAST({quote_identifier(c.name)} AS VARCHAR) AS c{i}" for i, c in enumerate(columns)
    )
    session.execute_trusted(f"CREATE OR REPLACE VIEW p AS SELECT {selects} FROM data")


def _count_column(
    session: TableSession, col: _Col, rows: int
) -> tuple[int, int, int, int, int, int, int]:
    """(empty, placeholders, present, distinct, n_int, n_num, n_date) for one column."""
    (counts,) = session.execute_trusted(
        f"""
        SELECT
          count(*) FILTER (WHERE {col.empty}),
          count(*) FILTER (WHERE {col.placeholder}),
          count(DISTINCT {col.value}) FILTER (WHERE {col.present}),
          count(*) FILTER (WHERE {col.present}
                           AND regexp_full_match({col.value}, {sql_literal(_INT_RE)})),
          count(*) FILTER (WHERE {col.present} AND ({col.number}) IS NOT NULL),
          count(*) FILTER (WHERE {col.present}
                           AND regexp_full_match({col.value}, {sql_literal(_DATE_LIKE_RE)}))
        FROM p
        """
    )
    empty, placeholders, distinct, n_int, n_num, n_date = (int(x) for x in counts)
    return empty, placeholders, rows - empty - placeholders, distinct, n_int, n_num, n_date


def _profile_column(session: TableSession, name: str, col: _Col, rows: int) -> dict[str, Any]:
    empty, placeholders, present, distinct, n_int, n_num, n_date = _count_column(session, col, rows)
    kind = _kind(present, n_int, n_num, n_date)
    result: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "non_missing": present,
        "empty": empty,
        "placeholders": placeholders,
        "placeholder_values": {},
        "missing_share": _share(empty + placeholders, rows),
        "distinct": distinct,
        "constant": distinct == 1 and present >= 2,
        "numeric": None,
        "non_numeric_values": None,
        "non_numeric_examples": [],
        "dates": None,
        "text_damage": None,
    }
    if placeholders:
        result["placeholder_values"] = dict(
            session.execute_trusted(
                f"SELECT {col.value}, count(*) FROM p WHERE {col.placeholder} "
                "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5"
            )
        )
    if present and n_num / present >= 0.5:
        if n_num < present:
            result["non_numeric_values"] = present - n_num
            result["non_numeric_examples"] = [
                _short(r[0])
                for r in session.execute_trusted(
                    f"SELECT {col.value} FROM p WHERE {col.present} AND ({col.number}) IS NULL "
                    "GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 5"
                )
            ]
        if n_num >= _MIN_VALUES_FOR_OUTLIERS:
            identifier_like = _name_is_key(name) or (kind == "integer" and distinct / n_num >= 0.9)
            result["numeric"] = _numeric_profile(session, col, identifier_like)
    if kind == "date":
        result["dates"] = _date_profile(session, col, present)
    if kind in ("text", "numeric_with_text", "date"):
        result["text_damage"] = _text_damage(session, col)
    if kind == "text" or _name_is_key(name):
        result["repeats"] = _repeats(session, col, name, present, distinct)
    return result


def _kind(present: int, n_int: int, n_num: int, n_date: int) -> str:
    if present == 0:
        return "empty"
    if n_date / present >= 0.5:
        return "date"
    if n_num / present >= 0.9:
        return "integer" if n_int == n_num else "decimal"
    if n_num / present >= 0.5:
        return "numeric_with_text"
    return "text"


def _name_is_key(name: str) -> bool:
    tokens = [t for t in re.split(r"[^\w]+|_", name.lower()) if t]
    return any(t in _KEY_NAME_TOKENS for t in tokens) or any(
        len(t) > 2 and t.endswith("id") for t in tokens
    )


def _repeats(
    session: TableSession, col: _Col, name: str, present: int, distinct: int
) -> dict[str, Any] | None:
    """Repeated values of an identifier-like column (None for ordinary text columns)."""
    if not _name_is_key(name) or present < 2 or distinct / present < 0.5:
        return None
    extra = present - distinct
    if extra == 0:
        return None
    top = session.execute_trusted(
        f"SELECT {col.value}, count(*) FROM p WHERE {col.present} GROUP BY 1 "
        "HAVING count(*) > 1 ORDER BY 2 DESC, 1 LIMIT ?",
        [_EXAMPLES],
    )
    return {"rows_beyond_first": extra, "examples": [_short(r[0]) for r in top]}


def _numeric_profile(session: TableSession, col: _Col, identifier_like: bool) -> dict[str, Any]:
    source = f"(SELECT {col.number} AS x FROM p) WHERE x IS NOT NULL"
    ((count, low, q1, median, q3, high),) = session.execute_trusted(
        f"SELECT count(*), min(x), quantile_cont(x, 0.25), quantile_cont(x, 0.5), "
        f"quantile_cont(x, 0.75), max(x) FROM {source}"
    )
    q1, q3, median = float(q1), float(q3), float(median)
    profile: dict[str, Any] = {
        "count": int(count),
        "min": _number(low),
        "q1": _number(q1),
        "median": _number(median),
        "q3": _number(q3),
        "max": _number(high),
        "outliers": None,
        "outliers_skipped": None,
    }
    iqr = q3 - q1
    if identifier_like:
        profile["outliers_skipped"] = "identifier-like column: outliers are meaningless"
    elif iqr == 0:
        profile["outliers_skipped"] = "IQR is 0 (over half of the values are identical)"
    else:
        mild = (q1 - 1.5 * iqr, q3 + 1.5 * iqr)
        extreme = (q1 - 3 * iqr, q3 + 3 * iqr)
        ((n_mild, n_extreme),) = session.execute_trusted(
            "SELECT count(*) FILTER (WHERE x < ? OR x > ?), "
            f"count(*) FILTER (WHERE x < ? OR x > ?) FROM {source}",
            [*mild, *extreme],
        )
        examples = session.execute_trusted(
            f"SELECT x FROM {source} AND (x < ? OR x > ?) GROUP BY x "
            "ORDER BY abs(x - ?) DESC, x LIMIT 5",
            [*mild, median],
        )
        profile["outliers"] = {
            "rule": "Tukey fences: beyond 1.5 x IQR (mild) or 3 x IQR (extreme) from the quartiles",
            "low_fence": _number(mild[0]),
            "high_fence": _number(mild[1]),
            "mild": int(n_mild),
            "extreme": int(n_extreme),
            "examples": [_number(r[0]) for r in examples],
        }
    return profile


def _date_profile(session: TableSession, col: _Col, present: int) -> dict[str, Any]:
    like = f"regexp_full_match({col.value}, {sql_literal(_DATE_LIKE_RE)})"
    values = session.execute_trusted(
        f"SELECT {col.value}, count(*) FROM p WHERE {col.present} AND {like} GROUP BY 1"
    )
    counts = {"valid": 0, "impossible": 0, "ambiguous": 0, "out_of_range": 0, "sentinel": 0}
    layouts: dict[str, int] = {}
    labelled: dict[str, int] = {}
    examples: dict[str, list[tuple[int, str]]] = {k: [] for k in counts}
    shaped_but_rejected = 0  # matched the SQL date shape, failed the stricter Python check
    earliest: date | None = None
    latest: date | None = None
    for text, n in values:
        verdict = classify_date(str(text))
        n = int(n)
        if verdict.status == "not_a_date":
            shaped_but_rejected += n
            continue
        status = verdict.status
        if status == "valid" and verdict.value is not None:
            year = verdict.value.year
            if not _YEAR_RANGE[0] <= year <= _YEAR_RANGE[1]:
                status = "out_of_range"
            else:
                counts["valid"] += n
                if verdict.value in _SENTINEL_DATES:
                    counts["sentinel"] += n
                    examples["sentinel"].append((n, str(text)))
                earliest = min(earliest or verdict.value, verdict.value)
                latest = max(latest or verdict.value, verdict.value)
                assert verdict.layout is not None
                layouts[verdict.layout] = layouts.get(verdict.layout, 0) + n
                label = verdict.layout + (f" {verdict.time_label}" if verdict.time_label else "")
                labelled[label] = labelled.get(label, 0) + n
        if status != "valid":
            counts[status] = counts.get(status, 0) + n
            examples[status].append((n, str(text)))
    shape_total = sum(counts[k] for k in ("valid", "impossible", "ambiguous", "out_of_range"))
    not_a_date = present - shape_total  # includes values that never looked like dates
    plain = session.execute_trusted(
        f"SELECT {col.value} FROM p WHERE {col.present} AND NOT {like} "
        "GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT ?",
        [_EXAMPLES],
    )
    not_date_examples = [_short(r[0]) for r in plain]
    return {
        "date_like": shape_total + shaped_but_rejected,
        "valid": counts["valid"],
        "impossible": counts["impossible"],
        "ambiguous_two_digit_year": counts["ambiguous"],
        "out_of_range": counts["out_of_range"],
        "sentinel": counts["sentinel"],
        "not_a_date": not_a_date,
        "layouts": dict(sorted(labelled.items(), key=lambda kv: -kv[1])),
        "mixed_layouts": len(layouts) > 1,
        "earliest": earliest.isoformat() if earliest else None,
        "latest": latest.isoformat() if latest else None,
        "assumption": "Day-first order is assumed for D.M.Y, D/M/Y and D-M-Y values.",
        "valid_year_range": list(_YEAR_RANGE),
        "examples": {
            **{
                k: [_short(t) for _, t in sorted(v, key=lambda x: (-x[0], x[1]))[:_EXAMPLES]]
                for k, v in examples.items()
                if v
            },
            **({"not_a_date": not_date_examples} if not_a_date else {}),
        },
    }


def _text_damage(session: TableSession, col: _Col) -> dict[str, Any] | None:
    ((padded, replacement, control, mojibake),) = session.execute_trusted(
        f"""
        SELECT
          count(*) FILTER (WHERE NOT {col.empty} AND regexp_matches({col.raw}, {sql_literal(_PADDED_RE)})),
          count(*) FILTER (WHERE {col.raw} IS NOT NULL AND contains({col.raw}, chr(65533))),
          count(*) FILTER (WHERE {col.raw} IS NOT NULL
                           AND regexp_matches({col.raw}, {sql_literal(_CONTROL_RE)})),
          count(*) FILTER (WHERE {col.raw} IS NOT NULL AND {col.mojibake})
        FROM p
        """  # noqa: E501
    )
    if not (padded or replacement or control or mojibake):
        return None
    damage: dict[str, Any] = {
        "padded_values": int(padded),
        "replacement_character_values": int(replacement),
        "control_character_values": int(control),
        "mojibake_values": int(mojibake),
        "mojibake_examples": [],
    }
    if mojibake:
        damage["mojibake_examples"] = [
            _short(r[0])
            for r in session.execute_trusted(
                f"SELECT {col.raw} FROM p WHERE {col.raw} IS NOT NULL AND {col.mojibake} "
                "GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT ?",
                [_EXAMPLES],
            )
        ]
    return damage


def _duplicates(
    session: TableSession, *, columns_count: int, profiled: list[dict[str, Any]], rows: int
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exact_duplicate_rows": None,
        "exact_duplicate_share": None,
        "duplicated_groups": None,
        "note": "Rows that are identical in every profiled column, counted beyond the first copy.",
        "key_candidates": [
            {"column": c["name"], **c["repeats"]} for c in profiled if c.get("repeats")
        ],
    }
    if not columns_count or not rows:
        return result
    group = ", ".join(f"c{i}" for i in range(columns_count))
    try:
        ((extra, groups),) = session.execute_trusted(
            f"SELECT coalesce(sum(n - 1), 0), count(*) FROM "
            f"(SELECT count(*) AS n FROM p GROUP BY {group} HAVING count(*) > 1)"
        )
    except SqlError:  # e.g. out of memory on a very wide table: say so instead of failing
        result["note"] += " Could not be computed for this table (resource limits)."
        return result
    result["exact_duplicate_rows"] = int(extra)
    result["exact_duplicate_share"] = _share(int(extra), rows)
    result["duplicated_groups"] = int(groups)
    return result


def _sparse_rows(session: TableSession, columns_count: int, rows: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "threshold": SPARSE_ROW_MISSING_SHARE,
        "count": 0,
        "share": 0.0,
        "note": f"Rows in which at least {SPARSE_ROW_MISSING_SHARE:.0%} of the cells are missing.",
    }
    if columns_count < 3 or not rows:
        result["note"] += " Not evaluated for tables with fewer than 3 columns."
        return result
    cells = " + ".join(
        f"CASE WHEN {_Col(i).missing} THEN 1 ELSE 0 END" for i in range(columns_count)
    )
    needed = -(-columns_count * 8 // 10)  # ceil(0.8 * columns)
    ((count,),) = session.execute_trusted(f"SELECT count(*) FROM p WHERE ({cells}) >= {needed}")
    result["count"] = int(count)
    result["share"] = _share(int(count), rows)
    return result
