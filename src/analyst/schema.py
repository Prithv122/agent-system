"""The schema as text, for the writer's prompt.

Read from ``information_schema`` so it always matches the file being queried, including a
user-loaded one. Tables are sorted by name and columns by ordinal position. Catalog object ids
are not used for ordering because they depend on load order, so the same data always gives the
same text and so the same cache key.

With ``value_hints`` on, each VARCHAR column that holds at most `MAX_HINT_VALUES` distinct
non-null values lists them, sorted, in braces after its type. It is off by default and the
default text is unchanged, so cached prompts stay valid. `value_hint_set` returns the same hints
as data (and `value_hint_sha256` fingerprints them) for the run sidecar.
"""

from __future__ import annotations

import hashlib
import json

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


def _hint_values(con: duckdb.DuckDBPyConnection, table: str, column: str) -> list[str] | None:
    """The distinct non-null values of a column, sorted, when there are 1..MAX_HINT_VALUES of
    them, else None. This is the one rule for what a value hint is: the schema text and the
    recorded hint set both come from here, so they cannot disagree."""
    col = _quote(column)
    rows = con.execute(
        f"SELECT DISTINCT {col} FROM main.{_quote(table)} "
        f"WHERE {col} IS NOT NULL ORDER BY {col} LIMIT {MAX_HINT_VALUES + 1}"
    ).fetchall()
    if not rows or len(rows) > MAX_HINT_VALUES:
        return None
    return [str(r[0]) for r in rows]


def _value_hint(con: duckdb.DuckDBPyConnection, table: str, column: str) -> str:
    """``" {a, b, c}"`` when the column has 1..MAX_HINT_VALUES distinct non-null values, else ""."""
    values = _hint_values(con, table, column)
    return "" if values is None else " {" + ", ".join(values) + "}"


def value_hint_set(con: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
    """Every value hint ``schema_text(con, value_hints=True)`` shows, as ``"table.column"`` ->
    the listed values in the order shown (sorted). A run records this so the exact hints a model
    saw are on file, not just the fact that hints were on."""
    hints: dict[str, list[str]] = {}
    for table, column, data_type in con.execute(_QUERY).fetchall():
        if data_type == "VARCHAR":
            values = _hint_values(con, table, column)
            if values is not None:
                hints[f"{table}.{column}"] = values
    return hints


def value_hint_sha256(hints: dict[str, list[str]]) -> str:
    """sha256 of the hint set as canonical JSON (sorted keys, no spaces, UTF-8)."""
    blob = json.dumps(hints, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def schema_text(con: duckdb.DuckDBPyConnection, *, value_hints: bool = False) -> str:
    """One line per table: ``orders(order_id INTEGER, customer_id INTEGER, ...)``."""
    tables: dict[str, list[str]] = {}
    for table, column, data_type in con.execute(_QUERY).fetchall():
        entry = f"{column} {data_type}"
        if value_hints and data_type == "VARCHAR":
            entry += _value_hint(con, table, column)
        tables.setdefault(table, []).append(entry)
    return "\n".join(f"{table}({', '.join(cols)})" for table, cols in tables.items())
