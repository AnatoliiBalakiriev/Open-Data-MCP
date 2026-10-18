from __future__ import annotations

from dataclasses import replace

import pytest

from open_data_mcp.config import Settings
from open_data_mcp.errors import (
    InvalidInput,
    ResourceTooLarge,
    SqlError,
    UnsafeSql,
    UnsupportedResource,
)
from open_data_mcp.query_engine import TableSession, validate_select

CSV = b"name,qty,d\nA,1,2024-01-02\nB,2,2024-02-03\nC,3,2024-02-04\n"


@pytest.fixture
def session():
    with TableSession(Settings()) as s:
        s.load_csv(CSV)
        yield s


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE data",
        "DELETE FROM data",
        "UPDATE data SET qty = 0",
        "INSERT INTO data VALUES ('x', 1, DATE '2024-01-01')",
        "CREATE TABLE t AS SELECT 1",
        "COPY data TO '/tmp/out.csv'",
        "ATTACH '/tmp/x.db' AS x",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA database_list",
        "EXPORT DATABASE '/tmp/x'",
        "SET enable_external_access = true",
        "EXPLAIN SELECT 1",
        "SELECT * FROM data; DROP TABLE data",
        "SELECT 1; SELECT 2",
    ],
)
def test_non_select_and_multi_statement_rejected(sql):
    with pytest.raises((UnsafeSql, SqlError)):
        validate_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        "SELECT * FROM read_json('/etc/passwd')",
        "SELECT * FROM read_parquet('x.parquet')",
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM glob('/*')",
        "SELECT * FROM '/etc/passwd'",
        "SELECT * FROM 'https://example.com/a.csv'",
        'SELECT * FROM "/etc/passwd"',
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM main.data",
        "SELECT * FROM other_table",
        "SELECT * FROM data WHERE qty IN (SELECT x FROM read_csv('/etc/passwd'))",
        "WITH t AS (SELECT * FROM read_csv('/etc/passwd')) SELECT * FROM t",
        "SELECT read_text('/etc/passwd')",
        "SELECT getenv('HOME')",
        "SELECT current_setting('home_directory')",
        "SELECT * FROM query('select 1')",
        "SELECT * FROM range(100000000)",
        "SELECT * FROM data WHERE qty = $1",
        "SELECT * FROM data WHERE qty = ?",
    ],
)
def test_file_and_table_function_access_rejected(sql):
    with pytest.raises(UnsafeSql):
        validate_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM data",
        "SELECT name, sum(qty) FROM data GROUP BY name HAVING sum(qty) > 1 ORDER BY 2 DESC",
        "WITH big AS (SELECT * FROM data WHERE qty > 1) SELECT count(*) FROM big",
        "SELECT * FROM data UNION ALL SELECT * FROM data",
        "SELECT year(d), median(qty) FROM data GROUP BY 1",
        "SELECT a.name FROM data a JOIN data b ON a.qty = b.qty",
        "SELECT * EXCLUDE (d) FROM data",
        "SELECT count(*) FROM (SELECT * FROM data) AS t",
        "SELECT strftime(d, '%Y-%m') FROM data",
    ],
)
def test_normal_queries_allowed(sql):
    validate_select(sql)


def test_aggregate_query_returns_correct_values(session):
    out = session.query("SELECT sum(qty) AS total, count(*) AS n FROM data")
    assert out.columns == ["total", "n"]
    assert out.rows == [[6, 3]]
    assert not out.truncated and out.scanned_rows == 3


def test_group_by_and_date_filter(session):
    out = session.query("SELECT month(d) AS m, count(*) AS n FROM data GROUP BY 1 ORDER BY 1")
    assert out.rows == [[1, 1], [2, 2]]
    assert session.query("SELECT count(*) FROM data WHERE d >= DATE '2024-02-01'").rows == [[2]]


def test_row_cap_enforced_on_server_even_if_caller_asks_for_more():
    s = replace(Settings(), query_max_limit=2, query_default_limit=1)
    with TableSession(s) as session:
        session.load_csv(CSV)
        default = session.query("SELECT * FROM data ORDER BY qty")
        assert default.returned_rows == 1 and default.truncated and default.row_limit == 1
        big_ask = session.query("SELECT * FROM data ORDER BY qty", limit=10_000)
        assert big_ask.returned_rows == 2 and big_ask.truncated and big_ask.row_limit == 2


def test_caller_limit_above_default_but_below_max(session):
    out = session.query("SELECT * FROM data ORDER BY qty", limit=3)
    assert out.returned_rows == 3 and not out.truncated


def test_sql_limit_larger_than_cap_is_clamped(session):
    out = session.query("SELECT * FROM data ORDER BY qty LIMIT 1000", limit=2)
    assert out.returned_rows == 2 and out.truncated
    assert session.query("SELECT * FROM data ORDER BY qty LIMIT 1", limit=5).returned_rows == 1


def test_union_is_capped_too(session):
    out = session.query("SELECT * FROM data UNION ALL SELECT * FROM data", limit=4)
    assert out.returned_rows == 4 and out.truncated


def test_order_is_preserved_under_cap(session):
    assert session.query("SELECT name FROM data ORDER BY qty DESC", limit=2).rows == [["C"], ["B"]]


@pytest.mark.parametrize("bad", [0, -1, "5", True, 1.5])
def test_invalid_limits_rejected(session, bad):
    with pytest.raises(InvalidInput):
        session.query("SELECT 1 FROM data", limit=bad)


def test_unknown_column_is_sql_error(session):
    with pytest.raises(SqlError, match="DuckDB rejected"):
        session.query("SELECT population FROM data")


def test_malformed_sql_is_error(session):
    with pytest.raises((SqlError, UnsafeSql)):
        session.query("SELEC * FORM data")


def test_engine_lockdown_blocks_files_even_if_validator_were_bypassed(session):
    import duckdb

    for raw in (
        "SELECT * FROM read_csv('/etc/passwd')",
        "COPY data TO '/tmp/odm-should-not-exist.csv'",
        "SET enable_external_access = true",
        "INSTALL httpfs",
    ):
        with pytest.raises(duckdb.Error):
            session._conn.execute(raw)


def test_query_timeout_interrupts_runaway_query():
    s = replace(Settings(), query_timeout_s=0.3)
    with TableSession(s) as session:
        session.load_csv(CSV)
        endless = (
            "WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM t) SELECT max(x) FROM t"
        )
        with pytest.raises(SqlError, match="time limit"):
            session.query(endless)
        # the session stays usable after an interrupt
        assert session.query("SELECT count(*) FROM data").rows == [[3]]


def test_row_limit_on_resource_refuses_instead_of_truncating():
    s = replace(Settings(), max_table_rows=2)
    with TableSession(s) as session, pytest.raises(ResourceTooLarge) as exc:
        session.load_csv(CSV)
    assert "limit is 2" in str(exc.value) and "Partial data is never queried" in exc.value.hint


def test_cp1251_csv_is_decoded():
    payload = "місто;к\nКиїв;1\nЛьвів;2\n".encode("cp1251")
    with TableSession(Settings()) as session:
        session.load_csv(payload)
        assert session.query('SELECT "місто" FROM data ORDER BY 1').rows == [["Київ"], ["Львів"]]


def test_semicolon_csv_with_bom():
    with TableSession(Settings()) as session:
        session.load_csv(b"\xef\xbb\xbfa;b\n1;2\n3;4\n")
        assert session.query("SELECT sum(b) FROM data").rows == [[6]]


def test_empty_and_garbage_csv_are_unsupported():
    with TableSession(Settings()) as session, pytest.raises(UnsupportedResource):
        session.load_csv(b"   \n")


def test_json_records_and_wrapper_object():
    with TableSession(Settings()) as session:
        session.load_json(b'{"records": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]}')
        assert session.query("SELECT sum(a) FROM data").rows == [[3]]


@pytest.mark.parametrize("payload", [b"not json", b"[]", b"[1, 2]", b'{"a": 1}'])
def test_unsupported_json_shapes(payload):
    with TableSession(Settings()) as session, pytest.raises(UnsupportedResource):
        session.load_json(payload)


def test_values_are_json_serialisable():
    with TableSession(Settings()) as session:
        session.load_csv(b"x,y\n1.5,2024-01-01\n")
        out = session.query("SELECT x, y, x * 2 AS z, CAST(1.25 AS DECIMAL(5,2)) AS dec FROM data")
        import json

        json.dumps(out.rows)
        assert out.rows == [[1.5, "2024-01-01", 3.0, 1.25]]
