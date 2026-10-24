"""Independent recomputation of every golden reference answer, in plain Python.

The golden set's ground truth is the output of each question's reference SQL. This script asks
"would someone who only read the question text, and the raw tables, get the same answer?". It loads
the raw tables with ``SELECT *`` and recomputes each answer with loops, dicts and sets, never with
the reference SQL logic (window functions, QUALIFY, PIVOT, recursion). The recomputed rows are then
compared with the reference result by the project's own scorer, ``analyst.score.results_equal``,
using each question's ``order_matters`` and ``column_order_matters``.

Usage::

    uv run python golden/crosscheck.py [WAREHOUSE] [--questions FILE]

One line per question (``OK`` or ``MISMATCH``/``MISSING``/``ERROR``, rows, columns). Exit status 1
when any question does not match or has no recomputation.

Each recomputation is written from the question text and the business definitions in
``golden/AUTHORING.md``; a new golden question needs a new function registered with
``@recompute("<qid>")`` or the crosscheck fails with ``MISSING``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from decimal import Decimal
from fractions import Fraction
from math import floor
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parent.parent
try:
    import analyst  # noqa: F401
except ImportError:  # run from a checkout without the package installed
    sys.path.insert(0, str(ROOT / "src"))

from analyst import executor, golden  # noqa: E402
from analyst._vendor.results import Column, ResultSet, diff  # noqa: E402
from analyst._vendor.results import _scalar as _frozen_scalar  # noqa: E402
from analyst.models import GoldenQuestion  # noqa: E402
from analyst.score import results_equal  # noqa: E402

DEFAULT_WAREHOUSE = ROOT / "data" / "warehouse.duckdb"
DEFAULT_QUESTIONS = ROOT / "golden" / "questions.jsonl"
TABLES = (
    "customers",
    "employees",
    "order_items",
    "orders",
    "page_events",
    "products",
    "subscriptions",
)
Row = tuple[Any, ...]


class Tables:
    """The raw tables, each a list of ``{column: value}`` dicts loaded with ``SELECT *``."""

    customers: list[dict[str, Any]]
    employees: list[dict[str, Any]]
    order_items: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    page_events: list[dict[str, Any]]
    products: list[dict[str, Any]]
    subscriptions: list[dict[str, Any]]

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        for name in TABLES:
            cur = con.execute(f"SELECT * FROM {name}")
            names = [d[0] for d in cur.description]
            setattr(self, name, [dict(zip(names, row, strict=True)) for row in cur.fetchall()])
        self.customer = {c["customer_id"]: c for c in self.customers}
        self.product = {p["product_id"]: p for p in self.products}
        self.order = {o["order_id"]: o for o in self.orders}
        self.employee = {e["employee_id"]: e for e in self.employees}

    def lines(self, status: str = "completed") -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """(order, order line) pairs for orders with this status."""
        for item in self.order_items:
            order = self.order[item["order_id"]]
            if order["status"] == status:
                yield order, item

    def order_values(self, status: str = "completed") -> dict[int, Decimal]:
        """Order id -> quantity * unit_price summed over its lines, for orders of this status."""
        out: dict[int, Decimal] = defaultdict(Decimal)
        for order, item in self.lines(status):
            out[order["order_id"]] += item["quantity"] * item["unit_price"]
        return dict(out)

    def completed_by_customer(self) -> dict[int, list[dict[str, Any]]]:
        """Customer id -> completed orders in placement order (timestamp, then order id)."""
        out: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for order in self.orders:
            if order["status"] == "completed":
                out[order["customer_id"]].append(order)
        for orders in out.values():
            orders.sort(key=lambda o: (o["order_ts"], o["order_id"]))
        return dict(out)

    def events_by_customer(self) -> dict[int, list[dict[str, Any]]]:
        """Customer id -> page views in order (timestamp, then event id)."""
        out: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for event in self.page_events:
            out[event["customer_id"]].append(event)
        for events in out.values():
            events.sort(key=lambda e: (e["event_ts"], e["event_id"]))
        return dict(out)


Recompute = Callable[[Tables], list[Row]]
RECOMPUTE: dict[str, Recompute] = {}


def recompute(qid: str) -> Callable[[Recompute], Recompute]:
    def register(fn: Recompute) -> Recompute:
        RECOMPUTE[qid] = fn
        return fn

    return register


# helpers ----------------------------------------------------------------------------------------


def round_half_up(x: Fraction | Decimal | int, places: int) -> Decimal:
    """Round an exact value to ``places`` decimals, ties away from zero (half-up for positives)."""
    scaled = Fraction(x) * 10**places
    n = floor(abs(scaled) + Fraction(1, 2))
    return Decimal(n if scaled >= 0 else -n).scaleb(-places)


def competition_ranks(values: dict[Any, int | Decimal]) -> dict[Any, int]:
    """1 + the number of keys with a strictly larger value (ties share a rank: 1, 2, 2, 4)."""
    return {k: 1 + sum(1 for w in values.values() if w > v) for k, v in values.items()}


def dense_ranks(values: dict[Any, int | Decimal]) -> dict[Any, int]:
    """1 + the number of distinct larger values (1, 2, 2, 3)."""
    return {k: 1 + len({w for w in values.values() if w > v}) for k, v in values.items()}


def month_text(ts: dt.datetime) -> str:
    return f"{ts.year:04d}-{ts.month:02d}"


def runs_of_consecutive(values: Iterable[int]) -> list[tuple[int, int]]:
    """Maximal runs (first, last) of consecutive integers in a set of integers."""
    ordered = sorted(set(values))
    runs: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for v in ordered[1:]:
        if v == prev + 1:
            prev = v
        else:
            runs.append((start, prev))
            start = prev = v
    runs.append((start, prev))
    return runs


def sessions_per_customer(t: Tables, max_gap_minutes: int) -> dict[int, int]:
    """A new session at a customer's first page view and after a gap of MORE than the limit."""
    limit = dt.timedelta(minutes=max_gap_minutes)
    out: dict[int, int] = {}
    for customer, events in t.events_by_customer().items():
        count = 0
        previous: dt.datetime | None = None
        for event in events:
            if previous is None or event["event_ts"] - previous > limit:
                count += 1
            previous = event["event_ts"]
        out[customer] = count
    return out


# running_total ----------------------------------------------------------------------------------


@recompute("running_total-1")
def _(t: Tables) -> list[Row]:
    month: dict[str, Decimal] = defaultdict(Decimal)
    for order, item in t.lines():
        if order["order_ts"].year == 2025:
            month[month_text(order["order_ts"])] += item["quantity"] * item["unit_price"]
    rows, cumulative = [], Decimal(0)
    for key in sorted(month):
        cumulative += month[key]
        rows.append((key, month[key], cumulative))
    return rows


@recompute("running_total-2")
def _(t: Tables) -> list[Row]:
    revenue: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for order, item in t.lines():
        ts = order["order_ts"]
        if ts.year == 2025:
            country = t.customer[order["customer_id"]]["country"]
            revenue[(country, f"2025-Q{(ts.month - 1) // 3 + 1}")] += (
                item["quantity"] * item["unit_price"]
            )
    rows = []
    for country in sorted({c for c, _ in revenue}):
        running = Decimal(0)
        for quarter in sorted(q for c, q in revenue if c == country):
            running += revenue[(country, quarter)]
            rows.append((country, quarter, revenue[(country, quarter)], running))
    return rows


@recompute("running_total-3")
def _(t: Tables) -> list[Row]:
    value = t.order_values()
    rows = []
    for customer, orders in t.completed_by_customer().items():
        cumulative = Decimal(0)
        for order in orders:
            cumulative += value[order["order_id"]]
            if cumulative >= 50000:
                rows.append((customer, order["order_id"], cumulative))
                break
    return rows


# lag_lead ---------------------------------------------------------------------------------------


@recompute("lag_lead-1")
def _(t: Tables) -> list[Row]:
    rows, previous = [], None
    for order in t.completed_by_customer()[13]:
        day = order["order_ts"].date()
        rows.append((order["order_id"], day, None if previous is None else (day - previous).days))
        previous = day
    return rows


@recompute("lag_lead-2")
def _(t: Tables) -> list[Row]:
    gaps: dict[str, list[int]] = defaultdict(list)
    for customer, orders in t.completed_by_customer().items():
        for before, after in itertools.pairwise(orders):
            gap = (after["order_ts"].date() - before["order_ts"].date()).days
            gaps[t.customer[customer]["segment"]].append(gap)
    return [(seg, round_half_up(Fraction(sum(g), len(g)), 2)) for seg, g in gaps.items()]


@recompute("lag_lead-3")
def _(t: Tables) -> list[Row]:
    followed: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    for customer, orders in t.completed_by_customer().items():
        country = t.customer[customer]["country"]
        for i, order in enumerate(orders):
            total[country] += 1
            if i + 1 < len(orders):
                days = (orders[i + 1]["order_ts"].date() - order["order_ts"].date()).days
                if days <= 30:
                    followed[country] += 1
    return [(c, round_half_up(Fraction(100 * followed[c], n), 1)) for c, n in total.items()]


# ranking ----------------------------------------------------------------------------------------


@recompute("ranking-1")
def _(t: Tables) -> list[Row]:
    units: dict[int, int] = defaultdict(int)
    for _order, item in t.lines():
        units[item["product_id"]] += item["quantity"]
    rank = competition_ranks(units)
    return [(p, units[p], r) for p, r in rank.items() if r <= 10]


@recompute("ranking-2")
def _(t: Tables) -> list[Row]:
    revenue: dict[int, Decimal] = defaultdict(Decimal)
    for order, item in t.lines():
        revenue[order["customer_id"]] += item["quantity"] * item["unit_price"]
    rows = []
    for country in sorted({t.customer[c]["country"] for c in revenue}):
        group = {c: v for c, v in revenue.items() if t.customer[c]["country"] == country}
        for customer, rank in dense_ranks(group).items():
            if rank <= 3:
                rows.append((country, customer, group[customer], rank))
    return rows


@recompute("ranking-3")
def _(t: Tables) -> list[Row]:
    units: dict[int, dict[int, int]] = {2024: defaultdict(int), 2025: defaultdict(int)}
    for order, item in t.lines():
        year = order["order_ts"].year
        if year in units:
            units[year][item["product_id"]] += item["quantity"]
    rank24, rank25 = competition_ranks(units[2024]), competition_ranks(units[2025])
    return [
        (p, rank24[p], rank25[p]) for p in rank24 if p in rank25 and rank24[p] - rank25[p] >= 10
    ]


# top_n_per_group --------------------------------------------------------------------------------


@recompute("top_n_per_group-1")
def _(t: Tables) -> list[Row]:
    revenue: dict[int, Decimal] = defaultdict(Decimal)
    for _order, item in t.lines():
        revenue[item["product_id"]] += item["quantity"] * item["unit_price"]
    rows = []
    for category in sorted({t.product[p]["category"] for p in revenue}):
        best = min((-v, p) for p, v in revenue.items() if t.product[p]["category"] == category)
        rows.append((category, best[1], -best[0]))
    return rows


@recompute("top_n_per_group-2")
def _(t: Tables) -> list[Row]:
    spend: dict[tuple[str, int], Decimal] = defaultdict(Decimal)
    for order, item in t.lines():
        category = t.product[item["product_id"]]["category"]
        spend[(category, order["customer_id"])] += item["quantity"] * item["unit_price"]
    rows = []
    for category in sorted({c for c, _ in spend}):
        ranked = sorted((-v, cust) for (cat, cust), v in spend.items() if cat == category)
        rows += [(category, cust, -neg) for neg, cust in ranked[:2]]
    return rows


@recompute("top_n_per_group-3")
def _(t: Tables) -> list[Row]:
    value = t.order_values()
    total: dict[int, Decimal] = defaultdict(Decimal)
    for order_id, v in value.items():
        total[t.order[order_id]["customer_id"]] += v
    top5 = [cust for _neg, cust in sorted((-v, cust) for cust, v in total.items())[:5]]
    rows = []
    for cust in top5:
        mine = sorted((-v, oid) for oid, v in value.items() if t.order[oid]["customer_id"] == cust)
        rows += [(cust, oid, -neg) for neg, oid in mine[:3]]
    return rows


# recursive_hierarchy ----------------------------------------------------------------------------


def reports_of(t: Tables, boss: int) -> list[int]:
    """Everyone below ``boss`` in the reporting tree, found by walking down level by level."""
    found: list[int] = []
    frontier = [boss]
    while frontier:
        frontier = [e["employee_id"] for e in t.employees if e["manager_id"] in frontier]
        found += frontier
    return found


@recompute("recursive_hierarchy-1")
def _(t: Tables) -> list[Row]:
    return [(e,) for e in reports_of(t, 2)]


@recompute("recursive_hierarchy-2")
def _(t: Tables) -> list[Row]:
    ceo = next(e["employee_id"] for e in t.employees if e["manager_id"] is None)
    rows = []
    for e in t.employees:
        if e["title"] != "Analyst":
            continue
        node = e
        while node["manager_id"] != ceo:  # climb until the next step up is the CEO
            node = t.employee[node["manager_id"]]
        rows.append((e["employee_id"], node["employee_id"]))
    return rows


@recompute("recursive_hierarchy-3")
def _(t: Tables) -> list[Row]:
    rows = []
    for vp in (e for e in t.employees if e["title"] == "VP"):
        people = [t.employee[i] for i in reports_of(t, vp["employee_id"])]
        first = min(people, key=lambda e: (e["hire_date"], e["employee_id"]))
        rows.append((vp["employee_id"], first["employee_id"], first["hire_date"]))
    return rows


# date_spine -------------------------------------------------------------------------------------


def completed_orders_per_day(t: Tables) -> Counter[dt.date]:
    return Counter(o["order_ts"].date() for o in t.orders if o["status"] == "completed")


def days_between(first: dt.date, last: dt.date) -> list[dt.date]:
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


@recompute("date_spine-1")
def _(t: Tables) -> list[Row]:
    per_day = completed_orders_per_day(t)
    return [(d, per_day.get(d, 0)) for d in days_between(dt.date(2024, 3, 1), dt.date(2024, 3, 14))]


@recompute("date_spine-2")
def _(t: Tables) -> list[Row]:
    per_day = completed_orders_per_day(t)
    return [
        (d,) for d in days_between(dt.date(2024, 2, 1), dt.date(2024, 2, 29)) if d not in per_day
    ]


@recompute("date_spine-3")
def _(t: Tables) -> list[Row]:
    revenue: dict[dt.date, Decimal] = defaultdict(Decimal)
    for order, item in t.lines():
        revenue[order["order_ts"].date()] += item["quantity"] * item["unit_price"]
    rows = []
    for day in days_between(dt.date(2026, 5, 1), dt.date(2026, 5, 31)):
        window = [revenue.get(day - dt.timedelta(days=k), Decimal(0)) for k in range(7)]
        rows.append((day, round_half_up(Fraction(sum(window)) / 7, 2)))
    return rows


# gaps_and_islands -------------------------------------------------------------------------------


@recompute("gaps_and_islands-1")
def _(t: Tables) -> list[Row]:
    days = {e["event_ts"].date().toordinal() for e in t.page_events if e["customer_id"] == 281}
    return [
        (dt.date.fromordinal(a), dt.date.fromordinal(b), b - a + 1)
        for a, b in runs_of_consecutive(days)
        if b > a
    ]


@recompute("gaps_and_islands-2")
def _(t: Tables) -> list[Row]:
    days: dict[int, set[int]] = defaultdict(set)
    for e in t.page_events:
        days[e["customer_id"]].add(e["event_ts"].date().toordinal())
    rows = []
    for customer, ds in days.items():
        longest = max(b - a + 1 for a, b in runs_of_consecutive(ds))
        if longest >= 7:
            rows.append((customer, longest))
    return rows


@recompute("gaps_and_islands-3")
def _(t: Tables) -> list[Row]:
    months: dict[int, set[int]] = defaultdict(set)
    for o in t.orders:
        if o["status"] == "completed":
            ts = o["order_ts"]
            months[o["customer_id"]].add(ts.year * 12 + ts.month - 1)

    def text(index: int) -> str:
        return f"{index // 12:04d}-{index % 12 + 1:02d}"

    rows = []
    for customer, ms in months.items():
        best = None
        for first, last in runs_of_consecutive(ms):  # runs come in calendar order
            if best is None or last - first > best[1] - best[0]:  # strictly longer wins
                best = (first, last)
        assert best is not None
        length = best[1] - best[0] + 1
        if length >= 4:
            rows.append((customer, text(best[0]), text(best[1]), length))
    return rows


# subscription_lapses ----------------------------------------------------------------------------


def periods_by_customer(t: Tables) -> dict[int, list[tuple[dt.date, dt.date]]]:
    out: dict[int, list[tuple[dt.date, dt.date]]] = defaultdict(list)
    for s in t.subscriptions:
        out[s["customer_id"]].append((s["period_start"], s["period_end"]))
    for periods in out.values():
        periods.sort()
    return dict(out)


def lapses_of(periods: list[tuple[dt.date, dt.date]]) -> list[tuple[dt.date, dt.date, int]]:
    """(coverage ended, resumed on, days) for each period starting after all earlier ends."""
    out = []
    latest_end = periods[0][1]
    for start, end in periods[1:]:
        if start > latest_end:
            out.append((latest_end, start, (start - latest_end).days))
        latest_end = max(latest_end, end)
    return out


@recompute("subscription_lapses-1")
def _(t: Tables) -> list[Row]:
    return sorted(lapses_of(periods_by_customer(t)[46]))


@recompute("subscription_lapses-2")
def _(t: Tables) -> list[Row]:
    every = [
        (cust, ended, resumed, days)
        for cust, periods in periods_by_customer(t).items()
        for ended, resumed, days in lapses_of(periods)
    ]
    every.sort(key=lambda r: (-r[3], r[0], r[1]))
    return every[:10]


@recompute("subscription_lapses-3")
def _(t: Tables) -> list[Row]:
    totals = {}
    for cust, periods in periods_by_customer(t).items():
        total = 0
        stretch_start, stretch_end = periods[0]
        for start, end in periods[1:]:
            if start <= stretch_end:
                stretch_end = max(stretch_end, end)
            else:
                total += (stretch_end - stretch_start).days
                stretch_start, stretch_end = start, end
        total += (stretch_end - stretch_start).days
        totals[cust] = total
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:5]


# sessionization ---------------------------------------------------------------------------------


@recompute("sessionization-1")
def _(t: Tables) -> list[Row]:
    sessions = sessions_per_customer(t, 10)
    views = {c: len(ev) for c, ev in t.events_by_customer().items()}
    return [(c, sessions[c], views[c]) for c in (3, 26, 127)]


@recompute("sessionization-2")
def _(t: Tables) -> list[Row]:
    total_sessions = sum(sessions_per_customer(t, 10).values())
    total_views = len(t.page_events)
    return [(total_sessions, round_half_up(Fraction(total_views, total_sessions), 2))]


@recompute("sessionization-3")
def _(t: Tables) -> list[Row]:
    sessions = sessions_per_customer(t, 15)
    return sorted(sessions.items(), key=lambda kv: (-kv[1], kv[0]))[:10]


# pivot ------------------------------------------------------------------------------------------


@recompute("pivot-1")
def _(t: Tables) -> list[Row]:
    segments = ("consumer", "smb", "enterprise")
    count: dict[tuple[str, str], int] = defaultdict(int)
    for o in t.orders:
        if o["status"] == "completed":
            c = t.customer[o["customer_id"]]
            count[(c["country"], c["segment"])] += 1
    countries = sorted({c["country"] for c in t.customers})
    return [(country, *(count[(country, s)] for s in segments)) for country in countries]


CATEGORIES = (
    "Audio",
    "Cameras",
    "Computing",
    "Gaming",
    "Home",
    "Mobile",
    "Networking",
    "Wearables",
)


@recompute("pivot-2")
def _(t: Tables) -> list[Row]:
    cell: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for order, item in t.lines():
        if order["order_ts"].year == 2025:
            category = t.product[item["product_id"]]["category"]
            cell[(month_text(order["order_ts"]), category)] += item["quantity"] * item["unit_price"]
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    return [(m, *(cell.get((m, c), Decimal(0)) for c in CATEGORIES)) for m in months]


@recompute("pivot-3")
def _(t: Tables) -> list[Row]:
    cell: dict[tuple[str, int], Decimal] = defaultdict(Decimal)
    for order, item in t.lines():
        category = t.product[item["product_id"]]["category"]
        cell[(category, order["order_ts"].year)] += item["quantity"] * item["unit_price"]
    rows = []
    for category in sorted({p["category"] for p in t.products}):
        r24, r25, r26 = (cell.get((category, y), Decimal(0)) for y in (2024, 2025, 2026))
        growth = round_half_up(Fraction(100) * Fraction(r25 - r24) / Fraction(r24), 1)
        rows.append((category, r24, r25, r26, growth))
    return rows


# conditional_aggregation ------------------------------------------------------------------------


@recompute("conditional_aggregation-1")
def _(t: Tables) -> list[Row]:
    placed: dict[str, int] = defaultdict(int)
    cancelled: dict[str, int] = defaultdict(int)
    for o in t.orders:
        segment = t.customer[o["customer_id"]]["segment"]
        placed[segment] += 1
        if o["status"] == "cancelled":
            cancelled[segment] += 1
    return [
        (s, placed[s], cancelled[s], round_half_up(Fraction(100 * cancelled[s], placed[s]), 1))
        for s in placed
    ]


@recompute("conditional_aggregation-2")
def _(t: Tables) -> list[Row]:
    completed: dict[str, Decimal] = defaultdict(Decimal)
    returned: dict[str, Decimal] = defaultdict(Decimal)
    for _order, item in t.lines("completed"):
        completed[t.product[item["product_id"]]["category"]] += (
            item["quantity"] * item["unit_price"]
        )
    for _order, item in t.lines("returned"):
        returned[t.product[item["product_id"]]["category"]] += item["quantity"] * item["unit_price"]
    rows = []
    for category in sorted({p["category"] for p in t.products}):
        c, r = completed[category], returned[category]
        rows.append(
            (category, c, r, round_half_up(Fraction(100) * Fraction(r) / Fraction(c + r), 2))
        )
    return rows


@recompute("conditional_aggregation-3")
def _(t: Tables) -> list[Row]:
    per_customer = Counter(o["customer_id"] for o in t.orders if o["status"] == "completed")
    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for customer, n in per_customer.items():
        slot = 0 if n == 1 else (1 if n <= 4 else 2)
        buckets[t.customer[customer]["country"]][slot] += 1
    return [(country, *counts) for country, counts in buckets.items()]


# tie_safe_ranking -------------------------------------------------------------------------------


@recompute("tie_safe_ranking-1")
def _(t: Tables) -> list[Row]:
    events = t.events_by_customer()
    return [(c, events[c][0]["page"]) for c in range(1, 16)]


@recompute("tie_safe_ranking-2")
def _(t: Tables) -> list[Row]:
    last: dict[tuple[int, dt.date], dict[str, Any]] = {}
    for e in t.page_events:
        key = (e["customer_id"], e["event_ts"].date())
        if key not in last or (e["event_ts"], e["event_id"]) > (
            last[key]["event_ts"],
            last[key]["event_id"],
        ):
            last[key] = e
    return list(Counter(e["page"] for e in last.values()).items())


@recompute("tie_safe_ranking-3")
def _(t: Tables) -> list[Row]:
    pages = Counter(
        events[2]["page"] for events in t.events_by_customer().values() if len(events) >= 3
    )
    return list(pages.items())


# comparison -------------------------------------------------------------------------------------


def to_result(reference: ResultSet, rows: Sequence[Row]) -> ResultSet:
    """Recomputed rows as a result set shaped like the reference (its column names and types).

    Values go through the same serialiser the frozen results use, so a Decimal arrives as text
    and a date as ISO text, exactly as ``capture`` would hand them to the scorer. The types come
    from the reference because this check compares values; it does not test the column types.
    """
    columns = tuple(Column(c.name, c.type) for c in reference.columns)
    return ResultSet(
        columns=columns, rows=tuple(tuple(_frozen_scalar(v) for v in row) for row in rows)
    )


def load_questions(path: Path | str) -> list[GoldenQuestion]:
    """Read the golden file. A seam for tests, which swap in an altered question list."""
    return golden.load(path)


def check_one(
    q: GoldenQuestion, con: duckdb.DuckDBPyConnection, tables: Tables
) -> tuple[str, int, int, str]:
    """``(status, rows, cols, detail)`` for one question; status is OK, MISMATCH, MISSING, ERROR."""
    function = RECOMPUTE.get(q.qid)
    if function is None:
        return "MISSING", 0, 0, "no recomputation registered for this qid"
    try:
        reference = golden.reference_result(con, q)
    except Exception as exc:
        return "ERROR", 0, 0, f"reference SQL did not run: {exc}"
    try:
        mine = to_result(reference, function(tables))
    except Exception as exc:
        return "ERROR", len(reference.rows), len(reference.columns), f"recompute raised: {exc!r}"
    rows, cols = len(reference.rows), len(reference.columns)
    if results_equal(reference, mine, q.order_matters, column_order_matters=q.column_order_matters):
        return "OK", rows, cols, ""
    detail = (
        diff(reference, mine) + f"\n(recomputed {len(mine.rows)} rows x {len(mine.columns)} cols)"
    )
    return "MISMATCH", rows, cols, detail


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recompute every golden answer in plain Python.")
    parser.add_argument(
        "warehouse", nargs="?", default=str(DEFAULT_WAREHOUSE), help="warehouse .duckdb file"
    )
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS), help="golden questions file")
    args = parser.parse_args(argv)

    questions = load_questions(args.questions)
    con = executor.open_readonly(args.warehouse)
    try:
        tables = Tables(con)
        failures = 0
        for q in questions:
            status, rows, cols, detail = check_one(q, con, tables)
            print(f"{status:<8} {q.qid:<28} rows={rows:<3} cols={cols}")
            if status != "OK":
                failures += 1
                for line in detail.splitlines():
                    print(f"           {line}")
    finally:
        con.close()
    unused = sorted(set(RECOMPUTE) - {q.qid for q in questions})
    if unused:
        print(f"note: recomputations with no question: {', '.join(unused)}")
    print(f"{len(questions) - failures}/{len(questions)} match")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
