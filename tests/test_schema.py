from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from analyst.executor import open_readonly
from analyst.schema import schema_text


def test_schema_text_lists_tables_in_sorted_by_table_name(warehouse_path: Path) -> None:
    con = open_readonly(warehouse_path)
    try:
        text = schema_text(con)
    finally:
        con.close()
    lines = text.splitlines()
    assert [ln.split("(")[0] for ln in lines] == [
        "customers",
        "employees",
        "order_items",
        "orders",
        "page_events",
        "products",
        "subscriptions",
    ]
    assert (
        "orders(order_id INTEGER, customer_id INTEGER, order_ts TIMESTAMP, status VARCHAR)" in lines
    )
    assert "DECIMAL(10,2)" in text


def test_schema_text_is_deterministic(warehouse_path: Path) -> None:
    con = open_readonly(warehouse_path)
    try:
        assert schema_text(con) == schema_text(con)
    finally:
        con.close()


def test_schema_text_follows_the_file_it_is_given(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / "x.duckdb"))
    con.execute("CREATE TABLE zebra (b INTEGER, a VARCHAR)")
    con.execute("CREATE TABLE apple (c DATE)")
    assert schema_text(con) == "apple(c DATE)\nzebra(b INTEGER, a VARCHAR)"
    con.close()


# --- the default text is pinned: a change here would change every cached prompt ------------

PINNED_DEFAULT = (
    "customers(customer_id INTEGER, name VARCHAR, country VARCHAR, segment VARCHAR, "
    "signup_date DATE)\n"
    "employees(employee_id INTEGER, name VARCHAR, title VARCHAR, manager_id INTEGER, "
    "hire_date DATE)\n"
    "order_items(order_item_id INTEGER, order_id INTEGER, product_id INTEGER, "
    "quantity INTEGER, unit_price DECIMAL(10,2))\n"
    "orders(order_id INTEGER, customer_id INTEGER, order_ts TIMESTAMP, status VARCHAR)\n"
    "page_events(event_id INTEGER, customer_id INTEGER, event_ts TIMESTAMP, page VARCHAR)\n"
    "products(product_id INTEGER, name VARCHAR, category VARCHAR, list_price DECIMAL(10,2))\n"
    "subscriptions(subscription_id INTEGER, customer_id INTEGER, plan VARCHAR, "
    "period_start DATE, period_end DATE)"
)


def test_default_schema_text_is_byte_identical_to_the_pinned_text(ro_con) -> None:
    assert schema_text(ro_con) == PINNED_DEFAULT
    assert schema_text(ro_con, value_hints=False) == PINNED_DEFAULT


def test_value_hints_list_low_cardinality_text_columns(ro_con) -> None:
    lines = schema_text(ro_con, value_hints=True).splitlines()
    assert (
        "orders(order_id INTEGER, customer_id INTEGER, order_ts TIMESTAMP, "
        "status VARCHAR {cancelled, completed, returned})" in lines
    )
    assert any("segment VARCHAR {consumer, enterprise, smb}" in ln for ln in lines)
    # high-cardinality text (customer names) and non-text columns get no hint
    customers = next(ln for ln in lines if ln.startswith("customers("))
    assert "name VARCHAR," in customers
    assert "customer_id INTEGER," in customers and "signup_date DATE)" in customers
    assert schema_text(ro_con, value_hints=True) == schema_text(ro_con, value_hints=True)


def test_value_hint_threshold_and_nulls(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / "h.duckdb"))
    con.execute("CREATE TABLE t (few VARCHAR, many VARCHAR, empty VARCHAR)")
    con.execute(
        "INSERT INTO t SELECT CASE WHEN i % 5 = 0 THEN NULL ELSE 'v' || (i % 12) END, "
        "'m' || i, NULL FROM range(100) AS r(i)"
    )
    text = schema_text(con, value_hints=True)
    con.close()
    # exactly 12 distinct non-null values: hinted, NULL not listed, sorted as text
    assert "few VARCHAR {v0, v1, v10, v11, v2, v3, v4, v5, v6, v7, v8, v9}" in text
    assert "many VARCHAR," in text  # 100 distinct values: no hint
    assert "empty VARCHAR)" in text  # no non-null values: no hint


def test_value_hints_stop_at_more_than_twelve_values(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / "h.duckdb"))
    con.execute("CREATE TABLE t (c VARCHAR)")
    con.execute("INSERT INTO t SELECT 'v' || i FROM range(13) AS r(i)")
    assert schema_text(con, value_hints=True) == "t(c VARCHAR)"
    con.close()


def test_ro_con_fixture_is_locked_like_production(ro_con) -> None:
    """`SET threads` works on an unlocked connection and fails on a locked one."""
    with pytest.raises(duckdb.Error):
        ro_con.execute("SET threads = 1")
