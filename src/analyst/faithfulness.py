"""Does every number in an explanation come from the result it explains?

The explainer is the one place a model writes free text about data, so it is the one place a
model can invent a figure. This module is the deterministic check on that: pull every number out
of the explanation and demand that each traces to the result rows, the question, or a simple
aggregate of the rows. No model is involved, so the check is reproducible and costs nothing.

What counts as a number (`numbers_in`)
--------------------------------------
* Digit numerals with optional thousands separators and decimals: ``1,234.56``, ``42``, ``0.5``.
  Number words ("twelve") are NOT read; that is a known gap, stated rather than hidden.
* A leading minus makes a number negative (``-3``, ``(-3``, ``-$1,200``). A hyphen after a
  letter (``top-5``) or after a digit (``3-5``, a range) is not a minus; those numbers are read
  as positive.
* Currency symbols (``$ £ € ₹``) are ignored: ``$1,200`` is 1200.
* ``%``, ``percent``, ``per cent`` and ``pct`` after a number mark it as a percentage.
* Scale suffixes attached to the number: ``1.2M``, ``3k``, ``2bn``; or the words ``thousand``,
  ``million``, ``billion`` after it. ``1.2M`` is read as 1,200,000 at the precision of "1.2M".
* Skipped entirely: digits inside ISO dates and timestamps (``2025-03-01``, ``2025-03-01
  10:00:00``), year-months (``2025-03``), clock times (``10:30``), identifiers where a letter or
  underscore touches the digits on the left (``Q3``, ``order_12``, ``v2``), ordinals (``2nd``,
  ``3rd``) and any other digit run glued to trailing letters (``5x``, ``3d``).
* ``top-5`` is NOT skipped: the 5 is read as a number. A figure the question itself contains is
  exempt anyway (below), so the stricter rule costs nothing for honest explanations and still
  catches "top-5" when the user asked for something else.

When a number is supported (`check`)
------------------------------------
A mention is supported when ANY of these holds:

1. **In the question.** Numbers written in the question text are exempt ("top 5", "in 2025",
   "over 100"). Compared exactly, ignoring sign.
2. **Matches a candidate from the result at the precision it was written.** The candidate is
   rounded half-up to as many decimals as the mention has, then compared. So "12.35" matches
   12.345678; "12" matches 12.4 (rounds to 12) but not 12.6; "1,235" matches 1234.56.
   Candidates are every numeric cell, the row count, the number of rows the explainer was
   shown (``min(row_count, SHOWN_ROWS)``, so "only the first 50 are shown" is supported), and the
   sum, mean, min and max of each column whose non-null cells are all numeric.
   A mention written as 0 with no decimals ("0 customers", "0 percent") is the exception to
   rounding: it is supported only by a candidate that is exactly zero, never by 0.31 or 0.4.
3. **Percent.** A mention marked as a percentage matches a numeric cell (or a column aggregate)
   either as written (the column already holds percent units) or as a ratio (value x 100):
   "12.5%" matches 0.125 and 12.5. The row count, the shown-row count and dates are not
   percentages and never support one: with 1 row, "100%" is not supported by 1 x 100.
4. **Scale.** "1.2M" matches a candidate of 1,150,000 to 1,249,999 (rounded to one decimal in
   millions); the suffix changes the unit, not the leniency.

Leniencies chosen on purpose, all documented here so none is a surprise:

* **Sign-insensitive.** "fell by 3" is faithful to a cell of -3; direction is carried by words.
* **Numeric strings are numeric.** DECIMAL cells arrive as strings ("1234.50"); a string that is
  wholly a number is parsed as one.
* **Text cells contribute the numbers inside them** ("Widget 42" supports 42), and ISO date or
  timestamp cells contribute their year only ("2025-03-01" supports 2025), so "orders placed in
  2025" is not flagged when the date column says so. Month and day numbers are not contributed:
  a day of 17 must not make "17 customers" look supported.
* **Empty is vacuous.** An explanation with no numbers is faithful. A missing result supports
  nothing, so every number in the explanation is then unsupported unless the question has it.

What this does not catch: a real number attached to the wrong entity (the right figure, the wrong
row), arithmetic the explainer did itself beyond sum/mean/min/max (a difference or a ratio
between two cells reads as unsupported), and number words. An unsupported number is a prompt to
look, not proof of a lie; a supported one is not proof of correctness. It bounds invention, no more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ._vendor.results import ResultSet
from .roles import SHOWN_ROWS

_DATE = r"\d{4}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?"
_TIME = r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
#: A date (or year-month) with an optional time, or a bare clock time. Masked before reading.
_DATETIME_RE = re.compile(
    rf"(?<![\d-])(?:{_DATE}(?:[ T]{_TIME}(?:Z|[+-]\d{{2}}:?\d{{2}})?)?)(?![\d])"
)
_TIME_RE = re.compile(rf"(?<![\d:]){_TIME}(?![\d:])")
_DATE_YEAR_RE = re.compile(
    r"(?<![\d-])(\d{4})-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?(?!\d)"
)

_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|(?<![\w.])\.\d+")
_CURRENCY = "$£€₹"
_MINUS = "-" + chr(0x2212)  # hyphen-minus and the Unicode minus sign
_SCALE_SUFFIX_RE = re.compile(r"(?P<s>bn|k|m|b)(?![A-Za-z0-9_])", re.IGNORECASE)
_SCALE_WORD_RE = re.compile(r"\s+(?P<s>thousand|million|billion)\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"\s?(?:%|percent\b|per\s?cent\b|pct\b)", re.IGNORECASE)
_PLAIN_NUMBER_RE = re.compile(r"\s*[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\s*")

_SCALES = {
    "k": 3,
    "thousand": 3,
    "m": 6,
    "million": 6,
    "b": 9,
    "bn": 9,
    "billion": 9,
}


@dataclass(frozen=True)
class Mention:
    """One number as the explanation wrote it."""

    value: Decimal  # the full value: "1.2M" -> 1200000, "-3" -> -3, "12.5%" -> 12.5
    written: Decimal  # the mantissa as written, without scale: "1.2M" -> 1.2
    decimals: int  # digits after the decimal point as written
    scale_exp: int  # 0, 3, 6 or 9
    percent: bool


@dataclass(frozen=True)
class FaithfulnessReport:
    numbers: list[Decimal]
    supported: list[Decimal]
    unsupported: list[Decimal]
    from_question: list[Decimal] = field(default_factory=list)

    @property
    def faithful(self) -> bool:
        return not self.unsupported

    def as_dict(self) -> dict[str, Any]:
        return {
            "numbers": [str(n) for n in self.numbers],
            "supported": [str(n) for n in self.supported],
            "unsupported": [str(n) for n in self.unsupported],
            "from_question": [str(n) for n in self.from_question],
            "faithful": self.faithful,
        }


def _mask(text: str) -> str:
    """Blank out dates, timestamps and clock times so their digits are not read as numbers."""
    text = _DATETIME_RE.sub(" ", text)
    return _TIME_RE.sub(" ", text)


def _mentions(text: str) -> list[Mention]:
    text = _mask(text)
    found: list[Mention] = []
    for m in _NUM_RE.finditer(text):
        start, end = m.span()
        prev = text[start - 1] if start > 0 else ""
        if prev.isalpha() or prev == "_":  # Q3, order_12, v2
            continue
        raw = m.group().replace(",", "")
        rest = text[end:]

        scale_exp = 0
        if rest[:1].isalpha():
            sm = _SCALE_SUFFIX_RE.match(rest)
            if sm is None:  # ordinals (2nd) and glued suffixes (5x, 3d) are not quantities
                continue
            scale_exp = _SCALES[sm.group("s").lower()]
            tail = rest[sm.end() :]
        elif wm := _SCALE_WORD_RE.match(rest):
            scale_exp = _SCALES[wm.group("s").lower()]
            tail = rest[wm.end() :]
        else:
            tail = rest
        percent = bool(_PERCENT_RE.match(tail))

        negative = _is_negative(text, start)
        written = Decimal(raw)
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        value = written.scaleb(scale_exp)
        found.append(
            Mention(
                value=-value if negative else value,
                written=written,
                decimals=decimals,
                scale_exp=scale_exp,
                percent=percent,
            )
        )
    return found


def _is_negative(text: str, start: int) -> bool:
    i = start - 1
    if i >= 0 and text[i] in _CURRENCY:
        i -= 1
    if i < 0 or text[i] not in _MINUS:
        return False
    before = text[i - 1] if i > 0 else ""
    # "top-5" (letter) and "3-5" (digit) are a hyphen, not a minus; a closing bracket is a range.
    return not (before.isalnum() or before in ")]_")


def numbers_in(text: str) -> list[Decimal]:
    """Every quantity written in ``text``, in order of appearance, as `Decimal`s.

    See the module docstring for what is read and what is skipped.
    """
    return [m.value for m in _mentions(text)]


def _cell_number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value)) if value == value and abs(value) != float("inf") else None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, str) and _PLAIN_NUMBER_RE.fullmatch(value):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    return None


def _flatten(value: Any) -> list[Any]:
    if isinstance(value, list | tuple):
        out: list[Any] = []
        for item in value:
            out.extend(_flatten(item))
        return out
    return [value]


@dataclass(frozen=True)
class _Candidate:
    value: Decimal
    ratio: bool  # a numeric cell or aggregate: may also match a percent as value x 100
    percent_ok: bool  # may match a percent mention as written


def _candidate_pool(result: ResultSet | None) -> list[_Candidate]:
    if result is None:
        return []
    n = len(result.rows)
    # counts and years are quantities, not percentages: they never support a percent mention
    out: list[_Candidate] = [
        _Candidate(Decimal(n), False, False),
        _Candidate(Decimal(min(n, SHOWN_ROWS)), False, False),
    ]
    for ci in range(len(result.columns)):
        numeric: list[Decimal] = []
        all_numeric = True
        for row in result.rows:
            for cell in _flatten(row[ci]):
                if cell is None:
                    continue
                num = _cell_number(cell)
                if num is not None:
                    numeric.append(num)
                    out.append(_Candidate(num, True, True))
                else:
                    all_numeric = False
                    if isinstance(cell, str):
                        out.extend(_Candidate(abs(v), False, True) for v in numbers_in(cell))
                        out.extend(
                            _Candidate(Decimal(y), False, False)
                            for y in _DATE_YEAR_RE.findall(cell)
                        )
        if numeric and all_numeric:
            total = sum(numeric, Decimal(0))
            for value in (total, total / len(numeric), min(numeric), max(numeric)):
                out.append(_Candidate(value, True, True))
    return out


def candidates(result: ResultSet | None) -> list[Decimal]:
    """Every figure the result supports: cells, counts, and per-column sum/mean/min/max."""
    return [c.value for c in _candidate_pool(result)]


def _matches(mention: Mention, candidate: _Candidate) -> bool:
    quantum = Decimal(1).scaleb(-mention.decimals)
    target = abs(mention.written)
    if mention.percent:
        options = []
        if candidate.percent_ok:
            options.append(candidate.value)
        if candidate.ratio:
            options.append(candidate.value * 100)
    else:
        options = [candidate.value]
    for option in options:
        scaled = abs(option).scaleb(-mention.scale_exp)
        if target == 0 and mention.decimals == 0 and scaled != 0:
            continue  # "0" is not a rounding of 0.31
        if scaled.quantize(quantum, rounding=ROUND_HALF_UP) == target.quantize(quantum):
            return True
    return False


def check(explanation: str, result: ResultSet | None, *, question: str = "") -> FaithfulnessReport:
    """Report which numbers in ``explanation`` are supported by ``result`` (rules: module doc)."""
    mentions = _mentions(explanation)
    asked = {abs(v) for v in numbers_in(question)}
    pool = _candidate_pool(result)
    supported: list[Decimal] = []
    unsupported: list[Decimal] = []
    from_question: list[Decimal] = []
    for mention in mentions:
        if abs(mention.value) in asked:
            from_question.append(mention.value)
            supported.append(mention.value)
        elif any(_matches(mention, c) for c in pool):
            supported.append(mention.value)
        else:
            unsupported.append(mention.value)
    return FaithfulnessReport(
        numbers=[m.value for m in mentions],
        supported=supported,
        unsupported=unsupported,
        from_question=from_question,
    )
