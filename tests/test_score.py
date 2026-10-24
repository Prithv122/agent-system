"""The equality rule is the measuring instrument, so every edge of it has a test."""

from __future__ import annotations

import pytest

from analyst._vendor.results import Column, ResultSet
from analyst.models import FAILURE_KINDS, Trace
from analyst.score import classify, is_numeric_type, normalize, results_equal


def rs(types: list[str], rows: list[tuple]) -> ResultSet:
    cols = tuple(Column(f"c{i}", t) for i, t in enumerate(types))
    return ResultSet(columns=cols, rows=tuple(rows))


def trace(outcome: str, result: ResultSet | None = None) -> Trace:
    return Trace(question="q", outcome=outcome, result=result)  # type: ignore[arg-type]


def test_names_and_types_are_ignored() -> None:
    a = ResultSet((Column("total", "BIGINT"),), ((5,),))
    b = ResultSet((Column("s", "INTEGER"),), ((5,),))
    assert results_equal(a, b)


def test_decimal_string_equals_float_from_a_double_column() -> None:
    assert results_equal(rs(["DECIMAL(10,2)"], [("12.50",)]), rs(["DOUBLE"], [(12.5,)]))


def test_numeric_tolerance_inside_and_outside() -> None:
    base = rs(["DOUBLE"], [(1_000_000.0,)])
    assert results_equal(base, rs(["DOUBLE"], [(1_000_000.5,)]))  # 5e-7 relative
    assert not results_equal(base, rs(["DOUBLE"], [(1_000_002.0,)]))  # 2e-6 relative
    assert results_equal(rs(["DOUBLE"], [(0.0,)]), rs(["DOUBLE"], [(1e-12,)]))  # near zero
    assert not results_equal(rs(["DOUBLE"], [(0.0,)]), rs(["DOUBLE"], [(0.001,)]))


def test_decimal_strings_outside_tolerance_differ() -> None:
    assert not results_equal(rs(["DECIMAL(10,2)"], [("12.50",)]), rs(["DOUBLE"], [(12.51,)]))


def test_varchar_is_not_coerced_to_a_number() -> None:
    assert not results_equal(rs(["VARCHAR"], [("007",)]), rs(["INTEGER"], [(7,)]))
    assert not results_equal(rs(["VARCHAR"], [("7",)]), rs(["VARCHAR"], [("7.0",)]))
    assert results_equal(rs(["VARCHAR"], [("007",)]), rs(["VARCHAR"], [("007",)]))


def test_bool_is_not_a_number() -> None:
    assert not results_equal(rs(["BOOLEAN"], [(True,)]), rs(["INTEGER"], [(1,)]))


def test_nulls() -> None:
    a = rs(["INTEGER", "VARCHAR"], [(None, None)])
    assert results_equal(a, rs(["BIGINT", "VARCHAR"], [(None, None)]))
    assert not results_equal(rs(["INTEGER"], [(None,)]), rs(["INTEGER"], [(0,)]))
    assert not results_equal(rs(["VARCHAR"], [(None,)]), rs(["VARCHAR"], [("",)]))


def test_row_order_ignored_unless_it_matters() -> None:
    a = rs(["INTEGER"], [(1,), (2,), (3,)])
    b = rs(["INTEGER"], [(3,), (1,), (2,)])
    assert results_equal(a, b, order_matters=False)
    assert not results_equal(a, b, order_matters=True)
    assert results_equal(a, a, order_matters=True)


def test_duplicates_are_counted_in_multisets() -> None:
    a = rs(["INTEGER"], [(1,), (1,), (2,)])
    b = rs(["INTEGER"], [(1,), (2,), (2,)])
    assert not results_equal(a, b)
    assert results_equal(a, rs(["INTEGER"], [(2,), (1,), (1,)]))


def test_tolerance_matching_inside_a_multiset() -> None:
    a = rs(["VARCHAR", "DOUBLE"], [("x", 1.0), ("x", 2.0), ("y", 3.0)])
    b = rs(["VARCHAR", "DOUBLE"], [("y", 3.0000001), ("x", 2.0000001), ("x", 1.0)])
    assert results_equal(a, b)
    c = rs(["VARCHAR", "DOUBLE"], [("y", 3.0), ("x", 2.0), ("x", 9.0)])
    assert not results_equal(a, c)


def test_column_permutation_is_accepted() -> None:
    a = rs(["VARCHAR", "BIGINT"], [("a", 1), ("b", 2)])
    b = rs(["BIGINT", "VARCHAR"], [(2, "b"), (1, "a")])
    assert results_equal(a, b)
    ordered = rs(["BIGINT", "VARCHAR"], [(1, "a"), (2, "b")])
    assert results_equal(a, ordered, order_matters=True)
    assert not results_equal(a, b, order_matters=True)


def test_permutation_must_pair_values_row_by_row() -> None:
    # Same per-column values, but the pairing across columns differs: not equal under any mapping.
    a = rs(["INTEGER", "INTEGER"], [(1, 10), (2, 20)])
    b = rs(["INTEGER", "INTEGER"], [(1, 20), (2, 10)])
    assert not results_equal(a, b)


def test_permutation_with_identical_columns() -> None:
    a = rs(["INTEGER", "INTEGER", "VARCHAR"], [(1, 1, "x"), (2, 2, "y")])
    b = rs(["VARCHAR", "INTEGER", "INTEGER"], [("x", 1, 1), ("y", 2, 2)])
    assert results_equal(a, b)


def test_extra_or_missing_column_is_a_mismatch() -> None:
    a = rs(["INTEGER"], [(1,), (2,)])
    two = rs(["INTEGER", "INTEGER"], [(1, 1), (2, 2)])
    assert not results_equal(a, two)
    assert not results_equal(two, a)


def test_empty_results() -> None:
    assert results_equal(rs(["INTEGER"], []), rs(["BIGINT"], []))
    assert not results_equal(rs(["INTEGER"], []), rs(["INTEGER"], [(1,)]))


def test_dates_compare_as_text() -> None:
    assert results_equal(rs(["DATE"], [("2025-01-01",)]), rs(["DATE"], [("2025-01-01",)]))
    assert not results_equal(rs(["DATE"], [("2025-01-01",)]), rs(["DATE"], [("2025-01-02",)]))


def test_list_columns_normalise_elementwise() -> None:
    a = rs(["DECIMAL(5,2)[]"], [(["1.50", "2.00"],)])
    b = rs(["DOUBLE[]"], [([1.5, 2.0],)])
    assert results_equal(a, b)


def test_is_numeric_type() -> None:
    for t in ("DECIMAL(10,2)", "INTEGER", "BIGINT", "HUGEINT", "DOUBLE", "FLOAT", "UBIGINT"):
        assert is_numeric_type(t), t
    for t in ("VARCHAR", "DATE", "TIMESTAMP", "BOOLEAN"):
        assert not is_numeric_type(t), t


def test_normalize_keeps_row_order_and_types() -> None:
    rows = normalize(rs(["VARCHAR", "DECIMAL(4,1)"], [("b", "2.0"), ("a", "1.5")]))
    assert [r[0] for r in rows] == ["b", "a"]
    assert float(rows[1][1]) == 1.5


# classify ----------------------------------------------------------------------------------------

EXPECTED = rs(["VARCHAR", "BIGINT"], [("a", 1), ("b", 2), ("c", 3)])


@pytest.mark.parametrize(
    ("outcome", "kind"),
    [
        ("failed_validation", "syntax"),
        ("failed_execution", "execution"),
        ("refused_unsafe", "unsafe"),
        ("budget_exhausted", "no_sql"),
    ],
)
def test_classify_non_ok_outcomes(outcome: str, kind: str) -> None:
    assert classify(EXPECTED, trace(outcome)) == kind


def test_classify_correct_and_wrong_shapes() -> None:
    assert classify(EXPECTED, trace("ok", EXPECTED)) is None
    wrong_cols = rs(["VARCHAR"], [("a",), ("b",), ("c",)])
    assert classify(EXPECTED, trace("ok", wrong_cols)) == "wrong_columns"
    wrong_grain = rs(["VARCHAR", "BIGINT"], [("a", 1), ("b", 2)])
    assert classify(EXPECTED, trace("ok", wrong_grain)) == "wrong_grain"
    wrong_values = rs(["VARCHAR", "BIGINT"], [("a", 1), ("b", 2), ("c", 4)])
    assert classify(EXPECTED, trace("ok", wrong_values)) == "wrong_values"


def test_classify_wrong_order_only_when_order_matters() -> None:
    shuffled = rs(["VARCHAR", "BIGINT"], [("c", 3), ("a", 1), ("b", 2)])
    assert classify(EXPECTED, trace("ok", shuffled), order_matters=True) == "wrong_order"
    assert classify(EXPECTED, trace("ok", shuffled), order_matters=False) is None
    wrong = rs(["VARCHAR", "BIGINT"], [("c", 9), ("a", 1), ("b", 2)])
    assert classify(EXPECTED, trace("ok", wrong), order_matters=True) == "wrong_values"


def test_classify_ok_without_result_is_no_sql() -> None:
    assert classify(EXPECTED, trace("ok", None)) == "no_sql"


def test_every_failure_kind_is_reachable() -> None:
    seen = {
        classify(EXPECTED, trace(o))
        for o in ("failed_validation", "failed_execution", "refused_unsafe", "budget_exhausted")
    }
    seen |= {
        classify(EXPECTED, trace("ok", rs(["VARCHAR"], [("a",)] * 3))),
        classify(EXPECTED, trace("ok", rs(["VARCHAR", "BIGINT"], []))),
        classify(EXPECTED, trace("ok", rs(["VARCHAR", "BIGINT"], [("a", 0)] * 3))),
        classify(
            EXPECTED,
            trace("ok", rs(["VARCHAR", "BIGINT"], [("c", 3), ("b", 2), ("a", 1)])),
            order_matters=True,
        ),
    }
    assert seen == set(FAILURE_KINDS)


def test_midnight_timestamp_equals_its_date() -> None:
    expected = ResultSet((Column("m", "DATE"),), (("2024-03-01",),))
    truncated = ResultSet((Column("m", "TIMESTAMP"),), (("2024-03-01 00:00:00",),))
    later = ResultSet((Column("m", "TIMESTAMP"),), (("2024-03-01 10:00:00",),))
    assert results_equal(expected, truncated, order_matters=False)
    assert not results_equal(expected, later, order_matters=False)
