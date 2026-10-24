"""Audit the scorer against the real golden set with metamorphic variants of each reference.

Execution accuracy is only as honest as ``analyst.score.results_equal``. For every golden question
this script builds variants of the reference result that a correct agent could plausibly produce
(they must compare EQUAL) and variants a wrong agent would produce (they must compare NOT EQUAL),
then checks what the scorer says. A variant is made either by wrapping the reference SQL (so the
engine itself re-types a column) or by rewriting the captured result. Any disagreement between
expectation and verdict is a *surprise*; it is printed and, in the tests, must be listed in
``tests/test_golden_audit.py`` with the reason it is accepted.

Usage::

    uv run python golden/audit_scorer.py [WAREHOUSE] [--matrix] [--questions FILE]

Exit status 1 when there is a surprise.

Matrix symbols: ``E`` expected equal and was; ``N`` expected not equal and was not; ``.`` variant
not applicable to this question; ``!`` surprise (the scorer disagreed with the expectation).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parent.parent
try:
    import analyst  # noqa: F401
except ImportError:  # run from a checkout without the package installed
    sys.path.insert(0, str(ROOT / "src"))

from analyst import executor, golden  # noqa: E402
from analyst._vendor.results import Column, ResultSet  # noqa: E402
from analyst.models import GoldenQuestion  # noqa: E402
from analyst.score import is_numeric_type, results_equal  # noqa: E402

DEFAULT_WAREHOUSE = ROOT / "data" / "warehouse.duckdb"
DEFAULT_QUESTIONS = ROOT / "golden" / "questions.jsonl"
SEED = 20260830

INTEGER_TYPES = frozenset(
    {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER"}
    | {"UBIGINT", "UHUGEINT"}
)


def base_type(type_name: str) -> str:
    return type_name.split("(")[0].strip().upper()


def is_integer(type_name: str) -> bool:
    return base_type(type_name) in INTEGER_TYPES


def is_decimal(type_name: str) -> bool:
    return base_type(type_name) in {"DECIMAL", "NUMERIC"}


def is_numeric(type_name: str) -> bool:
    return is_numeric_type(type_name)


@dataclass(frozen=True)
class Case:
    """One variant of one reference: the result to score and what the scorer must say."""

    result: ResultSet
    #: Overrides the question's ``column_order_matters`` (only the permutation variant uses it).
    column_order_matters: bool | None = None


@dataclass(frozen=True)
class Ctx:
    q: GoldenQuestion
    con: duckdb.DuckDBPyConnection
    ref: ResultSet
    rng: random.Random


@dataclass(frozen=True)
class Variant:
    name: str
    expect_equal: bool
    make: Callable[[Ctx], Case | None]  # None: not applicable to this question
    about: str


@dataclass(frozen=True)
class Outcome:
    qid: str
    variant: str
    expect_equal: bool
    got_equal: bool | None  # None: not applicable

    @property
    def applicable(self) -> bool:
        return self.got_equal is not None

    @property
    def surprise(self) -> bool:
        return self.got_equal is not None and self.got_equal != self.expect_equal


# building blocks --------------------------------------------------------------------------------


def with_rows(ref: ResultSet, rows: Sequence[Sequence[Any]]) -> ResultSet:
    return ResultSet(columns=ref.columns, rows=tuple(tuple(r) for r in rows))


def wrap_sql(ctx: Ctx, convert: Callable[[Column], str | None]) -> Case | None:
    """Run the reference SQL wrapped in a SELECT that casts columns.

    ``convert(column)`` returns the target SQL type or None to leave the column alone. Not
    applicable when no column converts or the engine does not return a different type.
    """
    names = [c.name for c in ctx.ref.columns]
    if len(set(names)) != len(names):
        return None
    parts, changed = [], False
    for col in ctx.ref.columns:
        target = convert(col)
        quoted = '"' + col.name.replace('"', '""') + '"'
        if target is None:
            parts.append(quoted)
        else:
            parts.append(f"CAST({quoted} AS {target}) AS {quoted}")
            changed = True
    if not changed:
        return None
    inner = ctx.q.reference_sql.strip().rstrip(";")
    got = executor.run(ctx.con, f"SELECT {', '.join(parts)} FROM ({inner}) AS t")
    if got.column_types == ctx.ref.column_types:
        return None
    return Case(got)


def numeric_cells(ref: ResultSet, *, integer: bool | None = None) -> list[tuple[int, int, Decimal]]:
    """(row, column, value) of every non-NULL numeric cell, optionally only integer / non-integer
    columns."""
    out = []
    for j, col in enumerate(ref.columns):
        if not is_numeric(col.type):
            continue
        if integer is not None and is_integer(col.type) != integer:
            continue
        for i, row in enumerate(ref.rows):
            v = row[j]
            if v is not None and not isinstance(v, bool):
                out.append((i, j, Decimal(str(v))))
    return out


def set_cell(ref: ResultSet, i: int, j: int, value: Any) -> ResultSet:
    rows = [list(r) for r in ref.rows]
    rows[i][j] = value
    return with_rows(ref, rows)


def scaled(ref: ResultSet, j: int, value: Decimal) -> Any:
    """A numeric value in the representation the column already uses (text for DECIMAL)."""
    t = ref.columns[j].type
    if is_integer(t):
        return int(value)
    if is_decimal(t):
        return str(value)
    return float(value)


# EQUAL variants ---------------------------------------------------------------------------------


def shuffle_rows(ctx: Ctx) -> Case | None:
    if ctx.q.order_matters or len(ctx.ref.rows) < 2:
        return None
    rows = list(ctx.ref.rows)
    ctx.rng.shuffle(rows)
    if rows == list(ctx.ref.rows):
        rows.reverse()
    return Case(with_rows(ctx.ref, rows))


def decimal_to_double(ctx: Ctx) -> Case | None:
    return wrap_sql(ctx, lambda c: "DOUBLE" if is_decimal(c.type) else None)


def int_to_bigint(ctx: Ctx) -> Case | None:
    return wrap_sql(ctx, lambda c: "BIGINT" if base_type(c.type) == "INTEGER" else None)


def int_to_hugeint(ctx: Ctx) -> Case | None:
    return wrap_sql(ctx, lambda c: "HUGEINT" if is_integer(c.type) else None)


def int_to_double(ctx: Ctx) -> Case | None:
    return wrap_sql(ctx, lambda c: "DOUBLE" if is_integer(c.type) else None)


def int_to_decimal(ctx: Ctx) -> Case | None:
    return wrap_sql(ctx, lambda c: "DECIMAL(18,2)" if is_integer(c.type) else None)


def double_to_decimal(ctx: Ctx) -> Case | None:
    return wrap_sql(ctx, lambda c: "DECIMAL(18,4)" if base_type(c.type) == "DOUBLE" else None)


def all_numeric_to_double(ctx: Ctx) -> Case | None:
    return wrap_sql(ctx, lambda c: "DOUBLE" if is_numeric(c.type) else None)


def date_to_timestamp(ctx: Ctx) -> Case | None:
    return wrap_sql(ctx, lambda c: "TIMESTAMP" if base_type(c.type) == "DATE" else None)


def date_to_varchar(ctx: Ctx) -> Case | None:
    return wrap_sql(ctx, lambda c: "VARCHAR" if base_type(c.type) == "DATE" else None)


def perturb_1e9(ctx: Ctx) -> Case | None:
    cells = numeric_cells(ctx.ref)
    if not cells:
        return None
    factor = 1 + Decimal("1e-9")
    columns = list(ctx.ref.columns)
    rows = [list(r) for r in ctx.ref.rows]
    for i, j, v in cells:
        if is_integer(ctx.ref.columns[j].type):
            columns[j] = Column(columns[j].name, "DOUBLE")
            rows[i][j] = float(v * factor)
        elif is_decimal(ctx.ref.columns[j].type):
            rows[i][j] = str(v * factor)
        else:
            rows[i][j] = float(v * factor)
    return Case(ResultSet(columns=tuple(columns), rows=tuple(tuple(r) for r in rows)))


def json_roundtrip(ctx: Ctx) -> Case | None:
    """The result as stored in a run file and read back: NULLs must stay NULL."""
    return Case(ResultSet.from_dict(json.loads(ctx.ref.to_json())))


def columns_permuted(ctx: Ctx) -> Case | None:
    """Columns reversed, scored as if the question left column order free (hypothetical flag)."""
    width = len(ctx.ref.columns)
    if width < 2:
        return None
    order = list(range(width))[::-1]
    columns = tuple(ctx.ref.columns[j] for j in order)
    rows = tuple(tuple(r[j] for j in order) for r in ctx.ref.rows)
    return Case(ResultSet(columns=columns, rows=rows), column_order_matters=False)


# NOT EQUAL variants -----------------------------------------------------------------------------


def drop_row(ctx: Ctx) -> Case | None:
    rows = list(ctx.ref.rows)
    del rows[len(rows) // 2]
    return Case(with_rows(ctx.ref, rows))


def duplicate_row(ctx: Ctx) -> Case | None:
    rows = list(ctx.ref.rows)
    k = len(rows) // 2
    rows.insert(k, rows[k])
    return Case(with_rows(ctx.ref, rows))


def largest(cells: list[tuple[int, int, Decimal]]) -> tuple[int, int, Decimal]:
    return max(cells, key=lambda c: abs(c[2]))


def cell_relative_1e4(ctx: Ctx) -> Case | None:
    """Largest non-integer numeric cell moved by 1e-4 relative (the hardest one to notice)."""
    cells = [c for c in numeric_cells(ctx.ref, integer=False) if c[2] != 0]
    if not cells:
        return None
    i, j, v = largest(cells)
    return Case(set_cell(ctx.ref, i, j, scaled(ctx.ref, j, v * (1 + Decimal("1e-4")))))


def integer_cell_plus_one(ctx: Ctx) -> Case | None:
    """Largest integer cell moved by 1 (hardest to notice: smallest relative change)."""
    cells = numeric_cells(ctx.ref, integer=True)
    if not cells:
        return None
    i, j, v = largest(cells)
    return Case(set_cell(ctx.ref, i, j, int(v) + 1))


def rows_swapped(ctx: Ctx) -> Case | None:
    if not ctx.q.order_matters or len(ctx.ref.rows) < 2:
        return None
    rows = list(ctx.ref.rows)
    k = next((n for n in range(1, len(rows)) if rows[n] != rows[0]), None)
    if k is None:
        return None
    rows[0], rows[k] = rows[k], rows[0]
    return Case(with_rows(ctx.ref, rows))


def same_type_columns_swapped(ctx: Ctx) -> Case | None:
    if not ctx.q.column_order_matters:
        return None
    cols = ctx.ref.columns
    for a in range(len(cols)):
        for b in range(a + 1, len(cols)):
            if cols[a].type != cols[b].type:
                continue
            if all(r[a] == r[b] for r in ctx.ref.rows):
                continue  # swapping identical columns changes nothing
            rows = [list(r) for r in ctx.ref.rows]
            for r in rows:
                r[a], r[b] = r[b], r[a]
            return Case(with_rows(ctx.ref, rows))
    return None


def null_to_zero(ctx: Ctx) -> Case | None:
    for i, row in enumerate(ctx.ref.rows):
        for j, v in enumerate(row):
            if v is None:
                zero = 0 if is_integer(ctx.ref.columns[j].type) else "0"
                if is_numeric(ctx.ref.columns[j].type) and not is_integer(ctx.ref.columns[j].type):
                    zero = scaled(ctx.ref, j, Decimal(0))
                return Case(set_cell(ctx.ref, i, j, zero))
    return None


def extra_column(ctx: Ctx) -> Case | None:
    columns = (*ctx.ref.columns, Column("extra", "INTEGER"))
    return Case(ResultSet(columns=columns, rows=tuple((*r, 1) for r in ctx.ref.rows)))


def zero_to_null(ctx: Ctx) -> Case | None:
    for i, j, v in numeric_cells(ctx.ref):
        if v == 0:
            return Case(set_cell(ctx.ref, i, j, None))
    return None


def cell_to_null(ctx: Ctx) -> Case | None:
    for i, row in enumerate(ctx.ref.rows):
        for j, v in enumerate(row):
            if v is not None:
                return Case(set_cell(ctx.ref, i, j, None))
    return None


def column_dropped(ctx: Ctx) -> Case | None:
    if len(ctx.ref.columns) < 2:
        return None
    return Case(ResultSet(columns=ctx.ref.columns[:-1], rows=tuple(r[:-1] for r in ctx.ref.rows)))


def numbers_as_text(ctx: Ctx) -> Case | None:
    return wrap_sql(ctx, lambda c: "VARCHAR" if is_numeric(c.type) else None)


VARIANTS: tuple[Variant, ...] = (
    Variant("shuffle_rows", True, shuffle_rows, "rows shuffled (only when order is free)"),
    Variant("dec_to_double", True, decimal_to_double, "DECIMAL columns CAST to DOUBLE"),
    Variant("int_to_bigint", True, int_to_bigint, "INTEGER columns CAST to BIGINT"),
    Variant("int_to_hugeint", True, int_to_hugeint, "integer columns CAST to HUGEINT"),
    Variant("int_to_double", True, int_to_double, "integer columns CAST to DOUBLE"),
    Variant("int_to_dec", True, int_to_decimal, "integer columns CAST to DECIMAL(18,2)"),
    Variant("dbl_to_dec", True, double_to_decimal, "DOUBLE columns CAST to DECIMAL(18,4)"),
    Variant("num_to_double", True, all_numeric_to_double, "every numeric column CAST to DOUBLE"),
    Variant("date_to_ts", True, date_to_timestamp, "DATE columns as midnight TIMESTAMP"),
    Variant("date_to_text", True, date_to_varchar, "DATE columns as VARCHAR (ISO text)"),
    Variant("perturb_1e-9", True, perturb_1e9, "every numeric cell moved by 1e-9 relative"),
    Variant("json_roundtrip", True, json_roundtrip, "stored and re-read; NULLs stay NULL"),
    Variant("cols_permuted", True, columns_permuted, "columns reversed, column order free"),
    Variant("drop_row", False, drop_row, "one row dropped"),
    Variant("dup_row", False, duplicate_row, "one row duplicated"),
    Variant("cell_rel_1e-4", False, cell_relative_1e4, "largest non-integer cell +1e-4 relative"),
    Variant("int_cell_+1", False, integer_cell_plus_one, "largest integer cell +1"),
    Variant("rows_swapped", False, rows_swapped, "two rows swapped (only when order matters)"),
    Variant("cols_swapped", False, same_type_columns_swapped, "two same-typed columns swapped"),
    Variant("null_to_0", False, null_to_zero, "one NULL replaced by 0"),
    Variant("extra_col", False, extra_column, "one extra column appended"),
    Variant("zero_to_null", False, zero_to_null, "one numeric 0 replaced by NULL"),
    Variant("cell_to_null", False, cell_to_null, "one non-NULL cell replaced by NULL"),
    Variant("col_dropped", False, column_dropped, "last column dropped"),
    Variant("num_as_text", False, numbers_as_text, "numeric columns CAST to VARCHAR (no coercion)"),
)


# running the audit ------------------------------------------------------------------------------


def audit_question(q: GoldenQuestion, con: duckdb.DuckDBPyConnection) -> list[Outcome]:
    ref = golden.reference_result(con, q)
    out = []
    for variant in VARIANTS:
        ctx = Ctx(q, con, ref, random.Random(f"{SEED}:{q.qid}:{variant.name}"))
        case = variant.make(ctx)
        if case is None:
            out.append(Outcome(q.qid, variant.name, variant.expect_equal, None))
            continue
        flag = q.column_order_matters if case.column_order_matters is None else False
        got = results_equal(ref, case.result, q.order_matters, column_order_matters=flag)
        out.append(Outcome(q.qid, variant.name, variant.expect_equal, got))
    return out


def audit(questions: Sequence[GoldenQuestion], con: duckdb.DuckDBPyConnection) -> list[Outcome]:
    return [o for q in questions for o in audit_question(q, con)]


def summary(outcomes: Sequence[Outcome]) -> str:
    lines = [f"{'variant':<16} {'expect':<9} {'held':>9}  about"]
    about = {v.name: v.about for v in VARIANTS}
    for v in VARIANTS:
        mine = [o for o in outcomes if o.variant == v.name and o.applicable]
        held = sum(not o.surprise for o in mine)
        expect = "EQUAL" if v.expect_equal else "NOT EQUAL"
        lines.append(f"{v.name:<16} {expect:<9} {held:>4}/{len(mine):<4}  {about[v.name]}")
    applicable = [o for o in outcomes if o.applicable]
    for want in (True, False):
        group = [o for o in applicable if o.expect_equal == want]
        held = sum(not o.surprise for o in group)
        lines.append(
            f"total expected {'EQUAL' if want else 'NOT EQUAL':<9}: {held}/{len(group)} held"
        )
    return "\n".join(lines)


def matrix(outcomes: Sequence[Outcome]) -> str:
    names = [v.name for v in VARIANTS]
    qids = list(dict.fromkeys(o.qid for o in outcomes))
    by = {(o.qid, o.variant): o for o in outcomes}
    header = " " * 26 + " ".join(str(i % 100).rjust(2) for i in range(1, len(names) + 1))
    lines = [header]
    for qid in qids:
        cells = []
        for n in names:
            o = by[(qid, n)]
            if not o.applicable:
                cells.append(" .")
            elif o.surprise:
                cells.append(" !")
            else:
                cells.append(" E" if o.expect_equal else " N")
        lines.append(f"{qid:<26}" + " ".join(c.strip().rjust(2) for c in cells))
    lines.append("")
    lines.append("columns: " + ", ".join(f"{i}={n}" for i, n in enumerate(names, start=1)))
    return "\n".join(lines)


def surprises(outcomes: Sequence[Outcome]) -> list[Outcome]:
    return [o for o in outcomes if o.surprise]


def load_questions(path: Path | str) -> list[GoldenQuestion]:
    return golden.load(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Metamorphic audit of the result-set scorer.")
    parser.add_argument(
        "warehouse", nargs="?", default=str(DEFAULT_WAREHOUSE), help="warehouse .duckdb file"
    )
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS), help="golden questions file")
    parser.add_argument(
        "--matrix", action="store_true", help="also print the question x variant grid"
    )
    args = parser.parse_args(argv)

    questions = load_questions(args.questions)
    con = executor.open_readonly(args.warehouse)
    try:
        outcomes = audit(questions, con)
    finally:
        con.close()
    print(summary(outcomes))
    if args.matrix:
        print()
        print(matrix(outcomes))
    bad = surprises(outcomes)
    print()
    if not bad:
        print("no surprises")
        return 0
    print(f"{len(bad)} SURPRISE(S): the scorer disagreed with the expectation")
    for o in bad:
        want = "EQUAL" if o.expect_equal else "NOT EQUAL"
        got = "EQUAL" if o.got_equal else "NOT EQUAL"
        print(f"  {o.qid:<28} {o.variant:<16} expected {want}, got {got}")
    kinds = Counter(o.variant for o in bad)
    print("  by variant: " + ", ".join(f"{k} x{n}" for k, n in kinds.items()))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
