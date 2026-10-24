"""Check a candidate query without running it.

Two deterministic checks, in order. The guard asks DuckDB's own parser whether this is exactly
one read statement; a failure here is a refusal, and the writer is never coached around it.
Then ``EXPLAIN`` plans the query on the read-only connection, which binds every table and column,
so a hallucinated name fails here with DuckDB's own message, including its "Candidate bindings"
hint, which is exactly what a writer needs to repair the query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import duckdb

from ._vendor.safety import UnsafeQuery, assert_read_only

_MAX_ERROR_CHARS = 600


@dataclass(frozen=True)
class Validation:
    ok: bool
    kind: Literal["guard", "explain"] | None = None
    error: str | None = None


def validate(con: duckdb.DuckDBPyConnection, sql: str) -> Validation:
    try:
        assert_read_only(sql)
    except UnsafeQuery as exc:
        return Validation(ok=False, kind="guard", error=str(exc))
    try:
        con.execute(f"EXPLAIN {sql.rstrip().rstrip(';')}").fetchall()
    except duckdb.Error as exc:
        return Validation(ok=False, kind="explain", error=str(exc).strip()[:_MAX_ERROR_CHARS])
    return Validation(ok=True)
