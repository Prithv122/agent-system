"""Result-set equality and the failure taxonomy.

The equality rule is the measurement instrument: execution accuracy (EX) is only as honest as the
comparison behind it, and the rule may not change after a real run. It is values-only:

- column names and column types are ignored (``SUM(x) AS total`` equals ``SUM(x) AS s``);
- rows are compared as a multiset unless the question says ``order_matters``;
- numbers match within 1e-6 relative (plus a tiny absolute floor so values near zero can match);
- NULL equals NULL, and nothing else.

Two decisions live here because the one-line rule leaves them open. Both are stated, not hidden.

**Numeric detection is type-aware, never content-based.** A column is numeric because its
declared DuckDB type is (INTEGER, BIGINT, HUGEINT, DOUBLE, FLOAT, DECIMAL, ...), not because its
text looks like a number. Results arrive from ``capture`` with DECIMAL as a string ("12.50") and
DOUBLE as a rounded float (12.5); the type is what lets those two agree, and the same type check is
what keeps a VARCHAR "007" from being coerced to 7. Dates and timestamps arrive as ISO text and are
compared as text, so a DATE and a VARCHAR holding the same ISO string are equal (values only).
One temporal rule on top: a TIMESTAMP at exactly midnight compares as its date. DuckDB's
``date_trunc`` returns a TIMESTAMP even for a DATE input, so a correct monthly answer arrives as
``2024-03-01 00:00:00`` against a reference ``2024-03-01``; the value is the same day, and only the
type differs. Booleans are tagged so True never equals 1.

**Column order is not part of the answer.** "Values only" is read as: the same set of columns,
in any order. If the actual result has the same number of columns, we look for a mapping of actual
columns onto expected columns (candidates come from matching per-column value multisets, and every
candidate mapping is then verified row by row). ``SELECT a, b`` and ``SELECT b, a`` are therefore
equal. An extra or a missing column is always a mismatch. This is an interpretation of the brief,
chosen because question wording rarely fixes column order; it is applied identically to every arm.

Digests (`golden.digest`) are built from `normalize`, with numbers rendered at 9 significant digits
so a frozen digest and an equality check see the same canonical values.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from ._vendor.results import ResultSet
from .models import Trace

REL_TOL = 1e-6
ABS_TOL = 1e-9
_REL = Decimal(str(REL_TOL))
_ABS = Decimal(str(ABS_TOL))

#: Base type names (parameters and array suffixes stripped) that are compared as numbers.
NUMERIC_TYPES = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "INT",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
        "INT1",
        "INT2",
        "INT4",
        "INT8",
        "SIGNED",
        "LONG",
        "SHORT",
        "DOUBLE",
        "FLOAT",
        "FLOAT4",
        "FLOAT8",
        "REAL",
        "DECIMAL",
        "NUMERIC",
    }
)

#: Cap on how many column permutations are verified before giving up (8 columns = 40k worst case,
#: and only identical-valued columns ever make the candidate list long).
MAX_MAPPINGS = 5_000

_PARAMS = re.compile(r"\(.*\)")

Value = Any  # None | Decimal | str | tuple


def is_numeric_type(type_name: str) -> bool:
    """True when a DuckDB type name (``DECIMAL(10,2)``, ``BIGINT``) is compared as a number."""
    base = _PARAMS.sub("", type_name).strip().upper()
    return base in NUMERIC_TYPES


def _number(value: Any) -> Value:
    """A number as an exact Decimal; non-finite floats and unparseable text stay as tagged text."""
    if isinstance(value, bool):
        return ("bool", value)
    try:
        number = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    if not number.is_finite():
        return "<" + str(number).lower() + ">"
    return number


def _normalize_value(value: Any, type_name: str) -> Value:
    if value is None:
        return None
    base = type_name.strip()
    if base.endswith("[]") and isinstance(value, list | tuple):
        return tuple(_normalize_value(v, base[:-2]) for v in value)
    if is_numeric_type(base):
        return _number(value)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, list | tuple):
        return tuple(_normalize_value(v, "") for v in value)
    text = value if isinstance(value, str) else str(value)
    if base.upper().startswith("TIMESTAMP") and text.endswith(" 00:00:00"):
        return text[: -len(" 00:00:00")]
    return text


def normalize(result: ResultSet) -> list[tuple[Value, ...]]:
    """Rows with every cell reduced to a comparable value, in the result's own row order."""
    types = result.column_types
    return [
        tuple(_normalize_value(v, t) for v, t in zip(row, types, strict=True))
        for row in result.rows
    ]


def _close(a: Value, b: Value) -> bool:
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        if a == b:
            return True
        return abs(a - b) <= max(_REL * max(abs(a), abs(b)), _ABS)
    if isinstance(a, tuple) and isinstance(b, tuple):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b) and type(a) is type(b)


def _rows_close(a: tuple[Value, ...], b: tuple[Value, ...]) -> bool:
    return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b, strict=True))


def _sort_key(value: Value) -> tuple:
    if value is None:
        return (0, 0)
    if isinstance(value, Decimal):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    return (3, repr(value))


def _row_key(row: tuple[Value, ...]) -> tuple:
    return tuple(_sort_key(v) for v in row)


def sorted_rows(rows: Sequence[tuple[Value, ...]]) -> list[tuple[Value, ...]]:
    """Deterministic row order for multiset handling (used by digests too)."""
    return sorted(rows, key=_row_key)


def _split(row: tuple[Value, ...]) -> tuple[tuple, tuple[Decimal, ...]]:
    """Separate the exactly-compared part of a row from its tolerance-compared numbers."""
    shape = tuple("<num>" if isinstance(v, Decimal) else v for v in row)
    return shape, tuple(v for v in row if isinstance(v, Decimal))


def _multiset_equal(a: Sequence[tuple[Value, ...]], b: Sequence[tuple[Value, ...]]) -> bool:
    if len(a) != len(b):
        return False
    # Exact matches cancel first; only the leftovers need the slower tolerance matching.
    left, right = Counter(a), Counter(b)
    common = left & right
    left_over = list((left - common).elements())
    right_over = list((right - common).elements())
    if len(left_over) != len(right_over):
        return False
    if not left_over:
        return True
    buckets: dict[tuple, list[tuple[Decimal, ...]]] = defaultdict(list)
    for row in left_over:
        shape, nums = _split(row)
        buckets[shape].append(nums)
    for row in right_over:
        shape, nums = _split(row)
        pool = buckets.get(shape)
        if not pool:
            return False
        for i, candidate in enumerate(pool):
            if all(_close(x, y) for x, y in zip(candidate, nums, strict=True)):
                del pool[i]
                break
        else:
            return False
    return True


def _column_values(rows: Sequence[tuple[Value, ...]], j: int) -> list[tuple[Value, ...]]:
    return [(row[j],) for row in rows]


def _rows_equal(
    expected: Sequence[tuple[Value, ...]],
    actual: Sequence[tuple[Value, ...]],
    order_matters: bool,
) -> bool:
    if order_matters:
        return len(expected) == len(actual) and all(
            _rows_close(e, a) for e, a in zip(expected, actual, strict=True)
        )
    return _multiset_equal(expected, actual)


def _permute(rows: Sequence[tuple[Value, ...]], mapping: Sequence[int]) -> list[tuple[Value, ...]]:
    """Reorder each row so output column ``j`` is the actual column ``mapping[j]``."""
    return [tuple(row[i] for i in mapping) for row in rows]


def _candidate_mappings(
    expected: Sequence[tuple[Value, ...]], actual: Sequence[tuple[Value, ...]], width: int
):
    """Yield column mappings whose per-column value multisets agree, identity first."""
    compatible: list[list[int]] = []
    for j in range(width):
        exp_col = _column_values(expected, j)
        compatible.append(
            [i for i in range(width) if _multiset_equal(exp_col, _column_values(actual, i))]
        )
    if any(not options for options in compatible):
        return
    identity = tuple(range(width))
    if all(j in compatible[j] for j in range(width)):
        yield identity
    count = 0

    def extend(j: int, chosen: tuple[int, ...]):
        nonlocal count
        if j == width:
            if chosen != identity:
                count += 1
                yield chosen
            return
        for i in compatible[j]:
            if i not in chosen and count < MAX_MAPPINGS:
                yield from extend(j + 1, (*chosen, i))

    yield from extend(0, ())


def results_equal(expected: ResultSet, actual: ResultSet, order_matters: bool = False) -> bool:
    """Do two results hold the same values? See the module docstring for the exact rule."""
    width = len(expected.columns)
    if width != len(actual.columns) or len(expected.rows) != len(actual.rows):
        return False
    exp_rows, act_rows = normalize(expected), normalize(actual)
    if not exp_rows:
        return True
    identity = tuple(range(width))
    if _rows_equal(exp_rows, act_rows, order_matters):
        return True
    if width < 2:
        return False
    for mapping in _candidate_mappings(exp_rows, act_rows, width):
        if mapping == identity:
            continue  # already tried above
        if _rows_equal(exp_rows, _permute(act_rows, mapping), order_matters):
            return True
    return False


_OUTCOME_KIND = {
    "failed_validation": "syntax",
    "failed_execution": "execution",
    "refused_unsafe": "unsafe",
    "budget_exhausted": "no_sql",
}


def classify(expected: ResultSet, trace: Trace, order_matters: bool = False) -> str | None:
    """Failure kind for a trace against the reference result, or None when it is correct."""
    if trace.outcome in _OUTCOME_KIND:
        return _OUTCOME_KIND[trace.outcome]
    actual = trace.result
    if actual is None:  # outcome "ok" with nothing to compare: the writer never produced a result
        return "no_sql"
    if results_equal(expected, actual, order_matters):
        return None
    if len(actual.columns) != len(expected.columns):
        return "wrong_columns"
    if len(actual.rows) != len(expected.rows):
        return "wrong_grain"
    if order_matters and results_equal(expected, actual, order_matters=False):
        return "wrong_order"
    return "wrong_values"


def number_text(value: Decimal) -> str:
    """Canonical text for a number in a digest: 9 significant digits, no sign on zero."""
    x = float(value)
    return "0" if x == 0 else format(x, ".9g")


__all__ = [
    "ABS_TOL",
    "REL_TOL",
    "classify",
    "is_numeric_type",
    "normalize",
    "number_text",
    "results_equal",
    "sorted_rows",
]
