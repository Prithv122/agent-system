# Vendored from sql-cookbook (src/sqlcookbook/results.py, commit ffd49cf),
# copied unchanged below this header.
# Upstream is MIT (same author). Edit here only with a note in NOTES.md.

"""Result sets, and how they are frozen into fixtures.

Fixtures record column *names and types* alongside values. A recipe that still
returns the right numbers under a renamed or silently re-typed column has still
changed its contract, and the test should say so.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb


@dataclass(frozen=True)
class Column:
    name: str
    type: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "type": self.type}


@dataclass(frozen=True)
class ResultSet:
    columns: tuple[Column, ...]
    rows: tuple[tuple[Any, ...], ...]

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def column_types(self) -> list[str]:
        return [c.type for c in self.columns]

    def as_dict(self) -> dict[str, Any]:
        return {
            "columns": [c.as_dict() for c in self.columns],
            "rows": [list(row) for row in self.rows],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2) + "\n"

    def write(self, path: Path) -> None:
        path.write_text(self.to_json(), encoding="utf-8", newline="\n")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResultSet:
        columns = tuple(Column(c["name"], c["type"]) for c in payload["columns"])
        rows = tuple(tuple(row) for row in payload["rows"])
        return cls(columns=columns, rows=rows)

    @classmethod
    def read(cls, path: Path) -> ResultSet:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _scalar(value: Any) -> Any:
    """Normalise one DuckDB value into something JSON can round-trip losslessly.

    Money is DECIMAL in the schema precisely so it does not arrive as a float;
    it is serialised as a string to keep the trailing zeros a Decimal carries.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, float):
        # Recipes avoid floats for money; anything left is a ratio or an
        # interval in seconds, where six places is far beyond what matters.
        return round(value, 6)
    if isinstance(value, list | tuple):
        return [_scalar(v) for v in value]
    return str(value)


def capture(con: duckdb.DuckDBPyConnection, sql: str) -> ResultSet:
    """Run ``sql`` and normalise the result into a comparable, serialisable form."""
    cur = con.execute(sql)
    description = cur.description or []
    columns = tuple(Column(name=d[0], type=str(d[1])) for d in description)
    rows = tuple(tuple(_scalar(v) for v in row) for row in cur.fetchall())
    return ResultSet(columns=columns, rows=rows)


def diff(expected: ResultSet, actual: ResultSet, limit: int = 5) -> str:
    """Human-readable explanation of the first differences, for assertion messages."""
    problems: list[str] = []
    if expected.column_names != actual.column_names:
        problems.append(
            f"column names: expected {expected.column_names}, got {actual.column_names}"
        )
    if expected.column_types != actual.column_types:
        problems.append(
            f"column types: expected {expected.column_types}, got {actual.column_types}"
        )
    if len(expected.rows) != len(actual.rows):
        problems.append(f"row count: expected {len(expected.rows)}, got {len(actual.rows)}")

    shown = 0
    for i, (exp_row, act_row) in enumerate(zip(expected.rows, actual.rows, strict=False)):
        if exp_row != act_row:
            problems.append(f"row {i}: expected {list(exp_row)}, got {list(act_row)}")
            shown += 1
            if shown >= limit:
                problems.append("... further row differences suppressed")
                break
    return "\n".join(problems) if problems else "no differences"
