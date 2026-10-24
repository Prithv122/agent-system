"""The schema as text, for the writer's prompt.

Read from ``information_schema`` so it always matches the file being queried, including a
user-loaded one. Tables are sorted by name and columns by ordinal position. Catalog object ids
are not used for ordering because they depend on load order, so the same data always gives the
same text and so the same cache key.
"""

from __future__ import annotations

import duckdb

_QUERY = """
SELECT c.table_name, c.column_name, c.data_type
FROM information_schema.columns AS c
JOIN duckdb_tables() AS t
  ON t.schema_name = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = 'main'
ORDER BY c.table_name, c.ordinal_position
"""


def schema_text(con: duckdb.DuckDBPyConnection) -> str:
    """One line per table: ``orders(order_id INTEGER, customer_id INTEGER, ...)``."""
    tables: dict[str, list[str]] = {}
    for table, column, data_type in con.execute(_QUERY).fetchall():
        tables.setdefault(table, []).append(f"{column} {data_type}")
    return "\n".join(f"{table}({', '.join(cols)})" for table, cols in tables.items())
