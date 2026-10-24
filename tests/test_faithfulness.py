"""The numeric-faithfulness check: reading numbers out of text, and matching them to a result."""

from __future__ import annotations

from decimal import Decimal

import pytest

from analyst._vendor.results import Column, ResultSet
from analyst.faithfulness import check, numbers_in


def dec(*values: str) -> list[Decimal]:
    return [Decimal(v) for v in values]


def result(columns: list[str], rows: list[tuple]) -> ResultSet:
    return ResultSet(columns=tuple(Column(c, "X") for c in columns), rows=tuple(rows))


# --- reading numbers --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("revenue was 1,234.56 in total", dec("1234.56")),
        ("1,234,567 orders", dec("1234567")),
        ("a drop of -3 points", dec("-3")),
        ("(-3) and +4", dec("-3", "4")),
        ("margin of 12.5% overall", dec("12.5")),
        ("12.5 percent", dec("12.5")),
        ("it cost $1,200 each", dec("1200")),
        ("down -$1,200", dec("-1200")),
        ("about 0.5 on average", dec("0.5")),
        ("1, 2, 3", dec("1", "2", "3")),
        ("no numbers here", []),
        ("", []),
    ],
)
def test_numbers_in_basic_forms(text: str, expected: list[Decimal]) -> None:
    assert numbers_in(text) == expected


def test_minus_sign_is_not_a_range_or_hyphen() -> None:
    assert numbers_in("between 3-5 items") == dec("3", "5")
    assert numbers_in("the top-5 customers") == dec("5")  # decided: counted, exempt via question
    assert numbers_in("from 2 - 3") == dec("2", "3")
    assert numbers_in("went to " + chr(0x2212) + "2") == dec("-2")  # Unicode minus sign


@pytest.mark.parametrize(
    "text",
    [
        "placed on 2025-03-01 by a customer",
        "at 2025-03-01 10:00:00 sharp",
        "at 2025-03-01T10:00:00Z",
        "in 2025-03",
        "the shop opens 10:30 daily",
    ],
)
def test_dates_and_times_are_skipped(text: str) -> None:
    assert numbers_in(text) == []


def test_numbers_next_to_a_date_are_still_read() -> None:
    assert numbers_in("42 orders on 2025-03-01 and 7 on 2025-03-02") == dec("42", "7")


@pytest.mark.parametrize("text", ["in Q3", "order_12", "version v2", "item sku9 ", "2nd place"])
def test_identifiers_and_ordinals_are_skipped(text: str) -> None:
    assert numbers_in(text) == []


def test_glued_suffix_that_is_not_a_scale_is_skipped() -> None:
    assert numbers_in("a 5x gain over 3d") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2M revenue", dec("1200000")),
        ("3k users", dec("3000")),
        ("2bn", dec("2000000000")),
        ("4 million orders", dec("4000000")),
        ("5mm wide", []),  # not a scale, glued letters
    ],
)
def test_scale_suffixes(text: str, expected: list[Decimal]) -> None:
    assert numbers_in(text) == expected


# --- matching to a result ---------------------------------------------------------------


def test_exact_cell_match() -> None:
    r = result(["segment", "n"], [("consumer", 248), ("smb", 108)])
    rep = check("There are 248 consumer and 108 smb customers.", r)
    assert rep.faithful
    assert rep.supported == dec("248", "108")
    assert rep.unsupported == []


def test_invented_number_is_flagged() -> None:
    r = result(["n"], [(248,)])
    rep = check("There are 250 customers.", r)
    assert not rep.faithful
    assert rep.unsupported == dec("250")


def test_matches_at_the_precision_written() -> None:
    r = result(["x"], [(12.345678,)])
    assert check("about 12.35", r).faithful
    assert check("about 12.3", r).faithful
    assert check("about 12", r).faithful
    assert not check("about 12.36", r).faithful
    assert not check("about 12.345", r).faithful  # 12.346 when rounded to 3 places


def test_integer_written_matches_rounded_cell() -> None:
    assert check("roughly 12", result(["x"], [(12.4,)])).faithful
    assert not check("roughly 13", result(["x"], [(12.4,)])).faithful
    assert not check("roughly 12", result(["x"], [(12.6,)])).faithful


def test_thousands_separator_and_decimal_cell() -> None:
    r = result(["revenue"], [("1234.50",)])  # DECIMAL cells arrive as strings
    assert check("revenue was $1,234.50", r).faithful
    assert check("revenue was about 1,235", r).faithful


def test_percentage_matches_ratio_or_percent_units() -> None:
    assert check("a 12.5% share", result(["share"], [(0.125,)])).faithful
    assert check("a 12.5% share", result(["share"], [(12.5,)])).faithful
    assert not check("a 12.5% share", result(["share"], [(0.2,)])).faithful
    # a plain number is not allowed to match ratio x 100
    assert not check("a share of 12.5", result(["share"], [(0.125,)])).faithful


def test_sign_insensitive() -> None:
    r = result(["delta"], [(-3,)])
    assert check("revenue fell by 3", r).faithful
    assert check("revenue changed by -3", r).faithful


def test_row_count_is_supported() -> None:
    r = result(["name"], [("a",), ("b",), ("c",)])
    assert check("I found 3 customers.", r).faithful
    assert not check("I found 4 customers.", r).faithful


def test_column_aggregates_are_supported() -> None:
    r = result(["amount"], [(10,), (20,), (45,)])
    rep = check("Total 75, average 25, smallest 10, largest 45.", r)
    assert rep.faithful, rep.unsupported
    # a difference of two cells is not an aggregate we accept
    assert not check("The gap is 35.", r).faithful


def test_no_aggregate_for_a_mixed_column() -> None:
    r = result(["v"], [(10,), ("n/a",), (20,)])
    assert not check("total 30", r).faithful


def test_null_cells_are_ignored_in_aggregates() -> None:
    r = result(["v"], [(10,), (None,), (20,)])
    assert check("total 30 across 3 rows", r).faithful


def test_numbers_in_the_question_are_exempt() -> None:
    r = result(["name"], [("a",), ("b",)])
    rep = check("Here are the top 5 customers.", r, question="Who are the top 5 customers?")
    assert rep.faithful
    assert rep.from_question == dec("5")
    assert not check("Here are the top 5 customers.", r).faithful


def test_question_exemption_is_exact_not_fuzzy() -> None:
    r = result(["name"], [("a",)])
    assert not check("top 50", r, question="top 5").faithful


def test_dates_in_cells_support_year_month_day() -> None:
    r = result(["d"], [("2025-03-01",), ("2024-12-31",)])
    rep = check("Orders from 2025 and 2024, in month 3 and 12.", r)
    assert rep.faithful, rep.unsupported
    assert not check("Orders from 2023.", r).faithful


def test_text_cells_contribute_embedded_numbers() -> None:
    r = result(["product"], [("Widget 42",)])
    assert check("The best seller is product 42.", r).faithful


def test_dates_in_the_explanation_do_not_count_as_numbers() -> None:
    r = result(["n"], [(5,)])
    rep = check("On 2025-03-01 there were 5 orders.", r)
    assert rep.numbers == dec("5")
    assert rep.faithful


def test_no_numbers_is_faithful_even_with_no_result() -> None:
    assert check("There is nothing to report.", None).faithful


def test_missing_result_supports_nothing() -> None:
    rep = check("There were 5 orders.", None)
    assert not rep.faithful
    assert check("There were 5 orders.", None, question="the 5 latest orders").faithful


def test_scale_matches_at_written_precision() -> None:
    r = result(["revenue"], [(1_234_567,)])
    assert check("revenue of 1.2M", r).faithful
    assert check("revenue of 1.23M", r).faithful
    assert not check("revenue of 1.3M", r).faithful


def test_list_cells_are_flattened() -> None:
    r = result(["xs"], [([1, 2, 3],)])
    assert check("values 2 and 3", r).faithful


def test_report_as_dict_uses_strings() -> None:
    rep = check("5 and 6", result(["n"], [(5,)]))
    d = rep.as_dict()
    assert d["faithful"] is False
    assert d["unsupported"] == ["6"]
    assert d["supported"] == ["5"]
