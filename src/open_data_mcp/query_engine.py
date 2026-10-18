"""Read-only SQL over one explicitly registered in-memory table (`data`) using DuckDB.

Defence in depth:
1. SQL is parsed with SQLGlot and checked against an AST allowlist (single SELECT, only the
   `data` relation and CTEs, no table functions, no unknown functions).
2. The statement that runs is regenerated from the validated AST, with a server-side row cap.
3. Data is loaded first, then DuckDB external access is disabled and the configuration locked,
   so even a validator bug cannot read files or URLs.
4. A wall-clock timeout interrupts runaway queries.
"""

from __future__ import annotations

import csv
import io
import json
import math
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .config import Settings
from .errors import InvalidInput, ResourceTooLarge, SqlError, UnsafeSql, UnsupportedResource

TABLE_NAME = "data"

# DuckDB functions that SQLGlot does not model as typed expressions (they parse as
# `exp.Anonymous`). Anything not listed here is rejected, which keeps file/system functions out.
ALLOWED_ANONYMOUS_FUNCTIONS = frozenset(
    {
        "arg_max",
        "arg_min",
        "max_by",
        "min_by",
        "approx_count_distinct",
        "bool_and",
        "bool_or",
        "count_if",
        "date_diff",
        "date_part",
        "date_sub",
        "date_trunc",
        "datediff",
        "dayname",
        "dayofmonth",
        "dayofweek",
        "dayofyear",
        "epoch",
        "epoch_ms",
        "greatest",
        "histogram",
        "isfinite",
        "isinf",
        "isnan",
        "least",
        "len",
        "list_contains",
        "list_value",
        "make_date",
        "median",
        "mode",
        "monthname",
        "quarter",
        "quantile_cont",
        "quantile_disc",
        "regexp_extract_all",
        "regexp_full_match",
        "regexp_matches",
        "string_agg",
        "strlen",
        "try_strptime",
        "weekofyear",
        "yearweek",
    }
)

PARTIAL_DATA_HINT = "Partial data is never queried, so aggregates always cover the whole resource."

# Typed SQLGlot functions that read from outside the registered relation.
DENIED_TYPED_FUNCTIONS = frozenset({"ReadCSV", "ReadParquet", "Glob"})


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str


@dataclass(frozen=True)
class QueryOutput:
    columns: list[str]
    rows: list[list[Any]]
    returned_rows: int
    truncated: bool
    row_limit: int
    scanned_rows: int


# --------------------------------------------------------------------------- SQL validation


def validate_select(sql: str) -> exp.Expression:
    """Return the parsed tree if `sql` is exactly one allowed read-only SELECT, else raise."""
    if not isinstance(sql, str) or not sql.strip():
        raise InvalidInput("`sql` must be a non-empty string.")
    if len(sql) > 5000:
        raise UnsafeSql("SQL is longer than 5000 characters.")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except SqlglotError as exc:
        raise SqlError(f"SQL could not be parsed: {str(exc)[:200]}") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSql("Exactly one SQL statement is allowed.")
    tree = statements[0]
    if not isinstance(tree, exp.Select | exp.Union | exp.Except | exp.Intersect):
        raise UnsafeSql(
            f"Only SELECT queries are allowed (got {type(tree).__name__.upper()}).",
            hint="Use SELECT ... FROM data ... Optionally with WITH (CTEs).",
        )
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if (
            not isinstance(table.this, exp.Identifier)
            or table.args.get("db")
            or table.args.get("catalog")
        ):
            raise UnsafeSql(
                "Table functions and qualified table names are not allowed.",
                hint=f"Query the single relation named `{TABLE_NAME}`.",
            )
        name = table.name.lower()
        if name != TABLE_NAME and name not in cte_names:
            raise UnsafeSql(
                f"Unknown relation {table.name!r}: only `{TABLE_NAME}` (and your own CTEs) exist.",
                hint="File paths, URLs and other tables cannot be queried.",
            )
    for node in tree.walk():
        if isinstance(node, exp.Placeholder | exp.Parameter):
            raise UnsafeSql("Query parameters are not supported.")
        if isinstance(node, exp.Anonymous):
            name = str(node.name).lower()
            if name not in ALLOWED_ANONYMOUS_FUNCTIONS:
                raise UnsafeSql(f"Function {node.name!r} is not allowed.")
        elif isinstance(node, exp.Func) and type(node).__name__ in DENIED_TYPED_FUNCTIONS:
            raise UnsafeSql(f"Function {type(node).__name__} is not allowed.")
    return tree


def _with_row_cap(tree: exp.Expression, cap: int) -> exp.Expression:
    """Return a copy of `tree` whose LIMIT is at most `cap` (an existing smaller LIMIT wins)."""
    capped = tree.copy()
    existing = capped.args.get("limit")
    limit_value = cap
    if existing is not None:
        inner = existing.expression
        if isinstance(inner, exp.Literal) and inner.is_int:
            limit_value = min(cap, int(inner.this))
    return capped.limit(limit_value)  # type: ignore[attr-defined, no-any-return]


# --------------------------------------------------------------------------- data loading


def _decode_text(payload: bytes) -> bytes:
    """Return UTF-8 bytes. Ukrainian portals still publish cp1251 CSVs."""
    if payload.startswith(b"\xef\xbb\xbf"):
        payload = payload[3:]
    try:
        payload.decode("utf-8")
        return payload
    except UnicodeDecodeError:
        try:
            return payload.decode("cp1251").encode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsupportedResource("CSV is neither UTF-8 nor Windows-1251 text.") from exc


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_DATASTORE_TYPES = {
    "int": "BIGINT",
    "int4": "INTEGER",
    "int8": "BIGINT",
    "float8": "DOUBLE",
    "float": "DOUBLE",
    "numeric": "DOUBLE",
    "bool": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "json": "VARCHAR",
    "text": "VARCHAR",
}


class TableSession:
    """One in-memory DuckDB connection holding exactly one table, `data`."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn = duckdb.connect(":memory:")
        self._conn.execute(f"SET memory_limit={_sql_str(settings.duckdb_memory_limit)}")
        self._conn.execute("SET threads=2")
        self.columns: list[ColumnInfo] = []
        self.row_count = 0

    # -- loaders (trusted SQL only; run before the lockdown) ------------------------------

    def load_csv(self, payload: bytes) -> None:
        text = _decode_text(payload)
        if not text.strip():
            raise UnsupportedResource("CSV resource is empty.")
        with tempfile.TemporaryDirectory(prefix="odm-") as tmp:
            path = Path(tmp) / "resource.csv"
            path.write_bytes(text)
            try:
                self._conn.execute(
                    f"CREATE TABLE {TABLE_NAME} AS SELECT * FROM "
                    f"read_csv({_sql_str(str(path))}, header=true, sample_size=-1, "
                    "ignore_errors=false)"
                )
            except duckdb.Error as exc:
                raise UnsupportedResource(f"CSV could not be parsed: {str(exc)[:200]}") from exc
        self._finish_load()

    def load_json(self, payload: bytes) -> None:
        try:
            parsed = json.loads(_decode_text(payload))
        except ValueError as exc:
            raise UnsupportedResource("JSON resource is not valid JSON.") from exc
        if isinstance(parsed, dict):
            for value in parsed.values():  # common `{"records": [...]}` wrapper
                if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
                    parsed = value
                    break
        if (
            not isinstance(parsed, list)
            or not parsed
            or not all(isinstance(r, dict) for r in parsed)
        ):
            raise UnsupportedResource(
                "JSON resource must be a non-empty array of flat objects (records)."
            )
        with tempfile.TemporaryDirectory(prefix="odm-") as tmp:
            path = Path(tmp) / "resource.ndjson"
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in parsed), "utf-8")
            try:
                self._conn.execute(
                    f"CREATE TABLE {TABLE_NAME} AS SELECT * FROM "
                    f"read_json({_sql_str(str(path))}, format='newline_delimited', "
                    "sample_size=-1)"
                )
            except duckdb.Error as exc:
                raise UnsupportedResource(f"JSON could not be parsed: {str(exc)[:200]}") from exc
        self._finish_load()

    def load_records(self, fields: list[dict[str, str]], rows: list[dict[str, Any]]) -> None:
        """Load DataStore records. Column types come from the portal's own field metadata."""
        columns = [f for f in fields if f.get("id") not in ("_full_text",)]
        if not columns:
            raise UnsupportedResource("DataStore resource has no fields.")
        defs = ", ".join(
            f'"{f["id"].replace(chr(34), chr(34) * 2)}" '
            f"{_DATASTORE_TYPES.get(str(f.get('type', 'text')).lower(), 'VARCHAR')}"
            for f in columns
        )
        self._conn.execute(f"CREATE TABLE {TABLE_NAME} ({defs})")
        names = [f["id"] for f in columns]
        placeholders = ", ".join("?" for _ in names)
        self._conn.executemany(
            f"INSERT INTO {TABLE_NAME} VALUES ({placeholders})",
            [[_datastore_value(row.get(n)) for n in names] for row in rows],
        )
        self._finish_load()

    def _finish_load(self) -> None:
        count = self._conn.execute(f"SELECT count(*) FROM {TABLE_NAME}").fetchone()
        self.row_count = int(count[0]) if count else 0
        if self.row_count > self.settings.max_table_rows:
            raise ResourceTooLarge(
                f"Resource has {self.row_count} rows; the limit is {self.settings.max_table_rows}.",
                hint=PARTIAL_DATA_HINT,
            )
        described = self._conn.execute(f"DESCRIBE {TABLE_NAME}").fetchall()
        self.columns = [ColumnInfo(str(r[0]), str(r[1])) for r in described]
        # Lockdown: nothing after this point may touch files, URLs or settings.
        self._conn.execute("SET enable_external_access=false")
        self._conn.execute("SET autoinstall_known_extensions=false")
        self._conn.execute("SET autoload_known_extensions=false")
        self._conn.execute("SET lock_configuration=true")

    # -- querying -----------------------------------------------------------------------

    def query(self, sql: str, limit: int | None = None) -> QueryOutput:
        cap = self._resolve_limit(limit)
        tree = validate_select(sql)
        runnable = _with_row_cap(tree, cap + 1).sql(dialect="duckdb")
        timer = threading.Timer(self.settings.query_timeout_s, self._conn.interrupt)
        timer.start()
        try:
            cursor = self._conn.execute(runnable)
            fetched = cursor.fetchall()
            names = [d[0] for d in cursor.description or []]
        except duckdb.InterruptException as exc:
            raise SqlError(
                f"Query exceeded the {self.settings.query_timeout_s:g}s time limit."
            ) from exc
        except duckdb.Error as exc:
            raise SqlError(f"DuckDB rejected the query: {str(exc)[:300]}") from exc
        finally:
            timer.cancel()
        truncated = len(fetched) > cap
        rows = [[_json_value(v) for v in row] for row in fetched[:cap]]
        return QueryOutput(names, rows, len(rows), truncated, cap, self.row_count)

    def _resolve_limit(self, limit: int | None) -> int:
        if limit is None:
            return self.settings.query_default_limit
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise InvalidInput("`limit` must be a positive integer.")
        return min(limit, self.settings.query_max_limit)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TableSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- value helpers


def _datastore_value(value: Any) -> Any:
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return value


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    return str(value)


def sniff_csv_header(payload: bytes) -> list[str]:
    """Header names only, for error messages; never used for typing."""
    reader = csv.reader(io.StringIO(_decode_text(payload).decode("utf-8")))
    return next(reader, [])
