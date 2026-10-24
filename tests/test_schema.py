from __future__ import annotations

from pathlib import Path

import duckdb

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
