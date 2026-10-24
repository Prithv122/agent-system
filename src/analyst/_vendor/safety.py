# Vendored from query-service (src/queryservice/safety.py, commit 6e1900c),
# copied unchanged below this header.
# Upstream is MIT (same author). Edit here only with a note in NOTES.md.

"""The guard layer.

Injection is not stopped by one trick, so this module is four independent ones. Any one
of them failing should leave the other three standing:

1. **Values are bound, never formatted.** Every user-supplied *value* reaches DuckDB as a
   prepared-statement parameter.
2. **Identifiers are allowlisted.** A column or table name cannot be bound as a parameter,
   so the only dynamic identifiers permitted are ones the query itself declares, and the
   supplied value must be one of that declared set -- an equality check against a fixed
   list, not a regex on the input.
3. **One statement, and it must read.** ``duckdb.extract_statements`` parses the SQL and
   we reject anything that is not exactly one ``SELECT``/``WITH``. This kills stacked
   queries and comment-terminated payloads at the parser rather than with string matching.
4. **The connection is read-only and the result is capped.** Even a payload that somehow
   reached the engine cannot write, and no query can pull an unbounded result set.

Layer 3 is the one worth explaining in an interview: blocklisting keywords like ``DROP``
is a losing game, but asking the database's own parser "how many statements is this, and
what kind" is not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import duckdb

MAX_ROW_LIMIT = 100_000
DEFAULT_ROW_LIMIT = 1_000

#: Statement types DuckDB reports for a pure read. ``SELECT`` covers ``WITH ... SELECT``.
READ_ONLY_STATEMENTS = {"SELECT"}

#: Keywords a read may start with. DuckDB accepts FROM-first syntax (``FROM t SELECT *``).
READ_ONLY_KEYWORDS = {"SELECT", "WITH", "FROM", "TABLE"}

_LEADING_NOISE = re.compile(r"^(?:\s|--[^\n]*\n|/\*.*?\*/)*", re.DOTALL)


class UnsafeQuery(ValueError):
    """Raised when SQL or a parameter fails a guard. Never leaks the offending SQL text."""


def assert_read_only(sql: str) -> None:
    """Reject anything that is not exactly one read statement.

    Parsing is delegated to DuckDB itself. String matching would have to keep up with
    comment syntax, string escapes, and every statement type the engine grows next.
    """
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:  # a parse failure is a rejection, not a crash
        raise UnsafeQuery(f"Could not parse SQL: {exc}") from exc

    if len(statements) != 1:
        raise UnsafeQuery(f"Expected exactly one statement, got {len(statements)}")

    kind = statements[0].type.name.upper()
    if kind not in READ_ONLY_STATEMENTS:
        raise UnsafeQuery(f"Only read statements are allowed here, got {kind}")

    # DuckDB rewrites some non-SELECT forms into a SELECT plan -- `PRAGMA database_list`
    # is reported as SELECT, and it is not something a query API should serve. The first
    # keyword is a cheap second opinion on top of the parser, not a substitute for it.
    body = _LEADING_NOISE.sub("", sql).lstrip("(")
    keyword = re.split(r"[^A-Za-z]", body, maxsplit=1)[0].upper()
    if keyword not in READ_ONLY_KEYWORDS:
        raise UnsafeQuery(f"Only read statements are allowed here, got {keyword or 'nothing'}")


def validate_identifier(value: str, allowed: Iterable[str], *, label: str = "identifier") -> str:
    """Return ``value`` if it is in ``allowed``, else raise.

    Deliberately an equality check against a closed set. Sanitising an identifier with a
    regex means reasoning about every quoting rule DuckDB has; membership does not.
    """
    allowed = list(allowed)
    if value not in allowed:
        raise UnsafeQuery(f"{label} must be one of {sorted(allowed)}, got {value!r}")
    return value


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_ROW_LIMIT
    limit = int(limit)
    if limit < 1:
        raise UnsafeQuery("limit must be >= 1")
    return min(limit, MAX_ROW_LIMIT)


def wrap_with_limit(sql: str, limit: int) -> str:
    """Cap a result set without trusting the query to have capped itself.

    The subquery wrapper is why this is safe to apply to catalog SQL that already ends in
    its own ``LIMIT``/``ORDER BY``: the inner query keeps its own semantics.
    """
    return f"SELECT * FROM (\n{sql.rstrip().rstrip(';')}\n) AS guarded LIMIT {int(limit)}"
