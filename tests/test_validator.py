from __future__ import annotations

from pathlib import Path

import pytest

from analyst.executor import open_readonly
from analyst.validator import validate


@pytest.fixture
def con(warehouse_path: Path):
    con = open_readonly(warehouse_path)
    yield con
    con.close()


def test_valid_query_passes(con) -> None:
    v = validate(con, "SELECT segment, count(*) FROM customers GROUP BY segment")
    assert v.ok and v.kind is None and v.error is None


def test_trailing_semicolon_is_fine(con) -> None:
    assert validate(con, "SELECT 1;").ok


def test_hallucinated_column_fails_explain_with_candidates(con) -> None:
    v = validate(con, "SELECT customer_nme FROM customers")
    assert not v.ok and v.kind == "explain"
    assert "customer_nme" in v.error
    assert "Candidate bindings" in v.error
    assert len(v.error) <= 600


def test_hallucinated_table_fails_explain(con) -> None:
    v = validate(con, "SELECT * FROM customer")
    assert v.kind == "explain" and "customer" in v.error


def test_unparseable_sql_is_a_guard_failure(con) -> None:
    v = validate(con, "SELEC 1")
    assert not v.ok and v.kind == "guard"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "DROP TABLE orders",
        "SELECT 1; DELETE FROM orders",
        "INSERT INTO customers VALUES (1)",
        "PRAGMA database_list",
    ],
)
def test_writes_and_stacked_statements_are_guard_failures(con, sql: str) -> None:
    v = validate(con, sql)
    assert not v.ok and v.kind == "guard" and v.error


def test_validate_does_not_execute(con) -> None:
    before = con.execute("SELECT count(*) FROM orders").fetchone()
    validate(con, "SELECT * FROM orders")
    assert con.execute("SELECT count(*) FROM orders").fetchone() == before
