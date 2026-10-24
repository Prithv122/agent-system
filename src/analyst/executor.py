"""Build the warehouse, open it read-only, and run SQL with a row cap and a timeout.

The database is the last line of defence, so the connection is made as unprivileged as DuckDB
allows: the file is opened read-only, SQL cannot touch the filesystem or network
(``enable_external_access = false``), and the configuration is then locked so a query cannot
switch that back on. The guard in `analyst._vendor.safety` runs first on every call, but nothing
here relies on it: a write that slipped past the guard would still be refused by the engine.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import duckdb

from ._vendor.results import ResultSet, capture
from ._vendor.safety import assert_read_only, clamp_limit, wrap_with_limit
from ._vendor.warehouse import SEED
from ._vendor.warehouse import build as _build

_MAX_ERROR_CHARS = 300


class ExecutionError(Exception):
    """The engine raised at run time, or the query hit the timeout. The message is short."""


def build_db(path: Path | str, seed: int = SEED) -> dict[str, int]:
    """Build the seeded warehouse file from scratch and return its row counts."""
    return _build(Path(path), seed)


def open_readonly(path: Path | str) -> duckdb.DuckDBPyConnection:
    """A read-only connection with external access off and the configuration locked."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no warehouse at {path}; run `analyst build-db` first")
    con = duckdb.connect(str(path), read_only=True, config={"enable_external_access": False})
    con.execute("SET lock_configuration = true")
    return con


def run(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    limit: int | None = None,
    timeout_s: float = 10.0,
) -> ResultSet:
    """Run one read statement, capped at ``limit`` rows, interrupted after ``timeout_s`` seconds.

    `UnsafeQuery` from the guard propagates unchanged. Engine errors and timeouts become
    `ExecutionError`, so a caller can tell "the SQL is unsafe" from "the SQL did not work".
    """
    assert_read_only(sql)
    wrapped = wrap_with_limit(_strip_terminator(sql), clamp_limit(limit))
    timed_out = threading.Event()

    def interrupt() -> None:
        timed_out.set()
        con.interrupt()

    timer = threading.Timer(timeout_s, interrupt)
    timer.daemon = True
    timer.start()
    try:
        return capture(con, wrapped)
    except duckdb.Error as exc:
        if timed_out.is_set():
            raise ExecutionError(f"query exceeded {timeout_s:g}s and was interrupted") from exc
        raise ExecutionError(str(exc)[:_MAX_ERROR_CHARS]) from exc
    finally:
        timer.cancel()


# A final `;` followed only by whitespace or comments. The guard has already proved there is
# exactly one statement, so this can only be its terminator.
_TERMINATOR = re.compile(r";(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*$", re.DOTALL)


def _strip_terminator(sql: str) -> str:
    """Drop a trailing `;` (and any comments after it) before the query is wrapped in a subquery.

    The vendored `wrap_with_limit` strips a bare final `;`, but not one followed by a comment:
    ``SELECT 1; -- note`` passed the guard and EXPLAIN, then failed at run time inside the wrapper.
    """
    return _TERMINATOR.sub("", sql.rstrip())
