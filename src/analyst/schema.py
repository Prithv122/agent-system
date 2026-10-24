"""The schema as text, for the writer's prompt.

Read from ``information_schema`` so it always matches the file being queried, including a
user-loaded one. Tables are sorted by name and columns by ordinal position. Catalog object ids
are not used for ordering because they depend on load order, so the same data always gives the
same text and so the same cache key.

With ``value_hints`` on, each VARCHAR column that holds at most `MAX_HINT_VALUES` distinct
non-null values lists them, sorted, in braces after its type. It is off by default and the
default text is unchanged, so cached prompts stay valid.
"""

from __future__ import annotations

import duckdb

#: A VARCHAR column with more distinct non-null values than this gets no hint.
MAX_HINT_VALUES = 12

_QUERY = """
SELECT c.table_name, c.column_name, c.data_type
FROM information_schema.columns AS c
JOIN duckdb_tables() AS t
  ON t.schema_name = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = 'main'
ORDER BY c.table_name, c.ordinal_position
"""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _value_hint(con: duckdb.DuckDBPyConnection, table: str, column: str) -> str:
    """``" {a, b, c}"`` when the column has 1..MAX_HINT_VALUES distinct non-null values, else ""."""
    col = _quote(column)
    rows = con.execute(
        f"SELECT DISTINCT {col} FROM main.{_quote(table)} "
        f"WHERE {col} IS NOT NULL ORDER BY {col} LIMIT {MAX_HINT_VALUES + 1}"
    ).fetchall()
    if not rows or len(rows) > MAX_HINT_VALUES:
        return ""
    return " {" + ", ".join(str(r[0]) for r in rows) + "}"


def schema_text(con: duckdb.DuckDBPyConnection, *, value_hints: bool = False) -> str:
    """One line per table: ``orders(order_id INTEGER, customer_id INTEGER, ...)``."""
    tables: dict[str, list[str]] = {}
    for table, column, data_type in con.execute(_QUERY).fetchall():
        entry = f"{column} {data_type}"
        if value_hints and data_type == "VARCHAR":
            entry += _value_hint(con, table, column)
        tables.setdefault(table, []).append(entry)
    return "\n".join(f"{table}({', '.join(cols)})" for table, cols in tables.items())
