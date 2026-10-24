from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from analyst._vendor.safety import UnsafeQuery
from analyst.executor import ExecutionError, build_db, open_readonly, run


@pytest.fixture
def con(warehouse_path: Path):
    con = open_readonly(warehouse_path)
    yield con
    con.close()


def _counts(con) -> dict[str, int]:
    tables = [r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()]
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def test_build_db_returns_row_counts(tmp_path: Path) -> None:
    counts = build_db(tmp_path / "w.duckdb")
    assert counts["customers"] == 400
    assert set(counts) >= {"orders", "order_items", "subscriptions"}


def test_open_readonly_missing_file_says_how_to_fix(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="analyst build-db"):
        open_readonly(tmp_path / "nope.duckdb")


def test_run_returns_result_set(con) -> None:
    result = run(con, "SELECT segment, count(*) AS n FROM customers GROUP BY segment ORDER BY 1")
    assert result.column_names == ["segment", "n"]
    assert sum(row[1] for row in result.rows) == 400


def test_run_caps_rows(con) -> None:
    assert len(run(con, "SELECT * FROM orders", limit=5).rows) == 5
    assert len(run(con, "SELECT * FROM orders").rows) == 1000  # default cap


def test_run_accepts_trailing_semicolon(con) -> None:
    assert run(con, "SELECT 1 AS x;").rows == ((1,),)


def test_run_rejects_writes_before_the_engine(con) -> None:
    before = _counts(con)
    for sql in ("DELETE FROM orders", "DROP TABLE orders", "SELECT 1; DELETE FROM orders"):
        with pytest.raises(UnsafeQuery):
            run(con, sql)
    assert _counts(con) == before


def test_connection_itself_is_read_only_when_guard_is_bypassed(con) -> None:
    before = _counts(con)
    with pytest.raises(duckdb.Error):
        con.execute("DELETE FROM orders")
    with pytest.raises(duckdb.Error):
        con.execute("DROP TABLE orders")
    with pytest.raises(duckdb.Error):
        con.execute("CREATE TABLE t AS SELECT 1")
    assert _counts(con) == before


def test_external_access_is_off(con, tmp_path: Path) -> None:
    csv = tmp_path / "data.csv"
    csv.write_text("a\n1\n", encoding="utf-8")
    sql = f"SELECT * FROM read_csv('{csv.as_posix()}')"
    with pytest.raises(ExecutionError, match="file system operations are disabled"):
        run(con, sql)
    with pytest.raises(duckdb.Error):
        con.execute(sql)


def test_sql_cannot_re_enable_external_access(con, tmp_path: Path) -> None:
    csv = tmp_path / "data.csv"
    csv.write_text("a\n1\n", encoding="utf-8")
    for stmt in (
        "SET enable_external_access = true",
        "SET lock_configuration = false",
        "RESET enable_external_access",
    ):
        with pytest.raises(duckdb.Error):
            con.execute(stmt)
    assert con.execute("SELECT current_setting('enable_external_access')").fetchone() == (False,)
    with pytest.raises(duckdb.Error):
        con.execute(f"SELECT * FROM read_csv('{csv.as_posix()}')")


def test_timeout_interrupts_slow_query(con) -> None:
    slow = "SELECT count(*) FROM range(100000) a, range(100000) b, range(100000) c"
    with pytest.raises(ExecutionError, match=r"exceeded 0\.5s"):
        run(con, slow, timeout_s=0.5)
    # the connection is still usable afterwards
    assert run(con, "SELECT 1 AS x").rows == ((1,),)


def test_engine_error_becomes_short_execution_error(con) -> None:
    with pytest.raises(ExecutionError) as info:
        run(con, "SELECT CAST('abc' AS INTEGER)")
    assert len(str(info.value)) <= 300


def test_trailing_semicolon_with_comment_runs(warehouse_path) -> None:
    con = open_readonly(warehouse_path)
    try:
        for sql in (
            "SELECT COUNT(*) FROM orders; -- note",
            "SELECT COUNT(*) FROM orders /* x */ ;",
        ):
            assert run(con, sql).rows == ((3000,),)
        assert run(con, "SELECT ';' AS s").rows == ((";",),)
    finally:
        con.close()
