# Vendored from sql-cookbook (src/sqlcookbook/warehouse.py, commit ffd49cf),
# copied unchanged below this header.
# Upstream is MIT (same author). Edit here only with a note in NOTES.md.

"""The synthetic warehouse: schema, seeded generation, and load.

Everything here is deterministic. One ``random.Random(SEED)`` instance drives every
draw, in a fixed order, so the same seed produces identical tables -- which is what
lets the recipes be tested against frozen fixtures instead of eyeballed.

The shapes below are not arbitrary. Each exists to give a recipe something real to
find: heavy-tailed customer spend so top-N has a meaningful head, subscription
periods that overlap *and* lapse so gaps-and-islands has both cases, visit gaps that
straddle the 30-minute sessionization threshold from both sides, and duplicate event
timestamps so the tie-safe ranking recipe demonstrates a real ambiguity rather than a
hypothetical one.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import duckdb

SEED = 20260830

N_CUSTOMERS = 400
N_PRODUCTS = 48
N_ORDERS = 3000
N_EVENT_CUSTOMERS = 400
N_SUBSCRIBED_CUSTOMERS = 150

EPOCH = dt.date(2024, 1, 1)
LAST_DAY = dt.date(2026, 6, 30)

#: Sessionization boundary shared by the recipe and the independent Python check.
SESSION_GAP = dt.timedelta(minutes=30)

#: One customer gets a gap of *exactly* SESSION_GAP, to pin down the boundary rule.
BOUNDARY_CUSTOMER = 7

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

COUNTRIES = ("IN", "US", "GB", "DE", "SG", "AU")
COUNTRY_WEIGHTS = (34, 26, 14, 11, 9, 6)
SEGMENTS = ("consumer", "smb", "enterprise")
SEGMENT_WEIGHTS = (62, 27, 11)
ORDER_STATUSES = ("completed", "returned", "cancelled")
ORDER_STATUS_WEIGHTS = (82, 8, 10)
PLANS = ("basic", "plus", "premium")

PAGES = (
    "/",
    "/search",
    "/category",
    "/product",
    "/cart",
    "/checkout",
    "/account",
    "/support",
)

FIRST_NAMES = (
    "Aditi",
    "Bhavesh",
    "Chen",
    "Divya",
    "Elena",
    "Farhan",
    "Gita",
    "Hugo",
    "Ines",
    "Jonas",
    "Kavya",
    "Lucas",
    "Meera",
    "Noor",
    "Omar",
    "Priya",
    "Quentin",
    "Rohan",
    "Sofia",
    "Tomas",
    "Uma",
    "Viktor",
    "Wei",
    "Yusuf",
)

LAST_NAMES = (
    "Ahuja",
    "Bakker",
    "Costa",
    "Dubois",
    "Eriksen",
    "Fernandes",
    "Gupta",
    "Hansen",
    "Iyer",
    "Jensen",
    "Kowalski",
    "Lindqvist",
    "Mehta",
    "Novak",
    "Okafor",
    "Petrov",
    "Quinn",
    "Rossi",
    "Silva",
    "Tanaka",
    "Ueda",
    "Vargas",
)

PRODUCT_NOUNS = (
    "Alpha",
    "Beacon",
    "Cinder",
    "Delta",
    "Ember",
    "Flux",
    "Grove",
    "Halo",
    "Iris",
    "Juno",
    "Kite",
    "Lumen",
    "Mesa",
    "Nova",
    "Onyx",
    "Prism",
    "Quartz",
    "Ridge",
    "Summit",
    "Tide",
    "Umbra",
    "Vertex",
    "Willow",
    "Zenith",
)

SCHEMA = """
CREATE TABLE customers (
    customer_id   INTEGER       PRIMARY KEY,
    name          VARCHAR       NOT NULL,
    country       VARCHAR       NOT NULL,
    segment       VARCHAR       NOT NULL,
    signup_date   DATE          NOT NULL
);

CREATE TABLE products (
    product_id    INTEGER       PRIMARY KEY,
    name          VARCHAR       NOT NULL,
    category      VARCHAR       NOT NULL,
    list_price    DECIMAL(10,2) NOT NULL
);

CREATE TABLE employees (
    employee_id   INTEGER       PRIMARY KEY,
    name          VARCHAR       NOT NULL,
    title         VARCHAR       NOT NULL,
    manager_id    INTEGER       REFERENCES employees(employee_id),
    hire_date     DATE          NOT NULL
);

CREATE TABLE orders (
    order_id      INTEGER       PRIMARY KEY,
    customer_id   INTEGER       NOT NULL REFERENCES customers(customer_id),
    order_ts      TIMESTAMP     NOT NULL,
    status        VARCHAR       NOT NULL
);

CREATE TABLE order_items (
    order_item_id INTEGER       PRIMARY KEY,
    order_id      INTEGER       NOT NULL REFERENCES orders(order_id),
    product_id    INTEGER       NOT NULL REFERENCES products(product_id),
    quantity      INTEGER       NOT NULL,
    unit_price    DECIMAL(10,2) NOT NULL
);

CREATE TABLE page_events (
    event_id      INTEGER       PRIMARY KEY,
    customer_id   INTEGER       NOT NULL REFERENCES customers(customer_id),
    event_ts      TIMESTAMP     NOT NULL,
    page          VARCHAR       NOT NULL
);

CREATE TABLE subscriptions (
    subscription_id INTEGER     PRIMARY KEY,
    customer_id     INTEGER     NOT NULL REFERENCES customers(customer_id),
    plan            VARCHAR     NOT NULL,
    period_start    DATE        NOT NULL,
    period_end      DATE        NOT NULL
);
"""

TABLES = (
    "customers",
    "products",
    "employees",
    "orders",
    "order_items",
    "page_events",
    "subscriptions",
)


@dataclass
class Warehouse:
    """Generated rows, before they touch a database."""

    customers: list[tuple]
    products: list[tuple]
    employees: list[tuple]
    orders: list[tuple]
    order_items: list[tuple]
    page_events: list[tuple]
    subscriptions: list[tuple]

    def row_counts(self) -> dict[str, int]:
        return {name: len(getattr(self, name)) for name in TABLES}


def _money(value: float) -> Decimal:
    return Decimal(f"{value:.2f}")


def _gen_customers(rng: random.Random) -> list[tuple]:
    rows = []
    for customer_id in range(1, N_CUSTOMERS + 1):
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        country = rng.choices(COUNTRIES, weights=COUNTRY_WEIGHTS, k=1)[0]
        segment = rng.choices(SEGMENTS, weights=SEGMENT_WEIGHTS, k=1)[0]
        signup = EPOCH + dt.timedelta(days=rng.randrange(0, 500))
        rows.append((customer_id, name, country, segment, signup))
    return rows


def _gen_products(rng: random.Random) -> list[tuple]:
    rows = []
    per_category = N_PRODUCTS // len(CATEGORIES)
    product_id = 0
    for category in CATEGORIES:
        for i in range(per_category):
            product_id += 1
            noun = PRODUCT_NOUNS[(product_id * 5 + i) % len(PRODUCT_NOUNS)]
            name = f"{noun} {category[:3].upper()}-{i + 1}"
            rows.append((product_id, name, category, _money(rng.uniform(9, 420))))
    return rows


def _gen_employees(rng: random.Random) -> list[tuple]:
    """A fixed-shape org: 1 CEO -> 4 VPs -> 10 managers -> 20 analysts. Depth 4."""
    rows: list[tuple] = []

    def hire() -> dt.date:
        return EPOCH + dt.timedelta(days=rng.randrange(0, 700))

    def name() -> str:
        return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"

    rows.append((1, name(), "CEO", None, hire()))
    for employee_id in range(2, 6):
        rows.append((employee_id, name(), "VP", 1, hire()))
    for i, employee_id in enumerate(range(6, 16)):
        rows.append((employee_id, name(), "Manager", 2 + (i % 4), hire()))
    for i, employee_id in enumerate(range(16, 36)):
        rows.append((employee_id, name(), "Analyst", 6 + (i % 10), hire()))
    return rows


def _order_customer_weights(rng: random.Random) -> list[float]:
    """Heavy-tailed: a minority of customers place most orders.

    Without this every customer has ~7 orders, top-N is a coin flip between
    near-identical rows, and the ranking recipes demonstrate nothing. The shape
    parameter is 2.5 rather than something more aggressive because a tail heavy
    enough to put 15% of all orders on one customer is just as useless.
    """
    return [rng.paretovariate(2.5) for _ in range(N_CUSTOMERS)]


def _gen_orders_and_items(
    rng: random.Random, customers: list[tuple], products: list[tuple]
) -> tuple[list[tuple], list[tuple]]:
    signup_by_customer = {row[0]: row[4] for row in customers}
    weights = _order_customer_weights(rng)
    customer_ids = [row[0] for row in customers]

    orders: list[tuple] = []
    items: list[tuple] = []
    order_item_id = 0

    for order_id in range(1, N_ORDERS + 1):
        customer_id = rng.choices(customer_ids, weights=weights, k=1)[0]
        earliest = signup_by_customer[customer_id]
        span = max((LAST_DAY - earliest).days, 1)
        order_date = earliest + dt.timedelta(days=rng.randrange(0, span))
        order_ts = dt.datetime.combine(
            order_date,
            dt.time(rng.randrange(6, 23), rng.randrange(0, 60), rng.randrange(0, 60)),
        )
        status = rng.choices(ORDER_STATUSES, weights=ORDER_STATUS_WEIGHTS, k=1)[0]
        orders.append((order_id, customer_id, order_ts, status))

        for _ in range(rng.randint(1, 5)):
            order_item_id += 1
            product = rng.choice(products)
            quantity = rng.randint(1, 4)
            discount = rng.choice((1.0, 1.0, 1.0, 0.9, 0.8))
            unit_price = _money(float(product[3]) * discount)
            items.append((order_item_id, order_id, product[0], quantity, unit_price))

    return orders, items


def _gen_page_events(rng: random.Random) -> list[tuple]:
    """One visit per customer-day, starting between 08:00 and 17:00.

    Two deliberate properties. Visits are always hours apart while events inside a
    visit are always under 20 minutes apart, so the correct sessionization is knowable
    without running any SQL -- which is what lets ``tests/test_semantics.py`` recompute
    it in plain Python and *check* the recipe rather than trust it. And 40% of
    customers browse on consecutive days, so gaps-and-islands has real streaks to find
    instead of a table of one-day islands.
    """
    events: list[tuple] = []
    event_id = 0

    for customer_id in range(1, N_EVENT_CUSTOMERS + 1):
        n_visits = rng.randint(1, 10)
        if rng.random() < 0.40:
            first = EPOCH + dt.timedelta(days=rng.randrange(0, 880))
            days = [first + dt.timedelta(days=i) for i in range(n_visits)]
        else:
            days = [EPOCH + dt.timedelta(days=o) for o in sorted(rng.sample(range(900), n_visits))]

        for visit_index, day in enumerate(days):
            # Latest possible visit: 17:59 + 9 steps x 20 min = under 21:00, so the
            # next day's 08:00 visit is never within the session gap.
            ts = dt.datetime.combine(day, dt.time(rng.randrange(8, 18), rng.randrange(0, 60)))
            for step in range(rng.randint(1, 10)):
                # 5% of steps repeat the previous timestamp exactly. That duplicate is
                # the whole subject of recipe 12.
                if step and rng.random() >= 0.05:
                    ts = ts + dt.timedelta(seconds=rng.randrange(5, 1200))
                event_id += 1
                events.append((event_id, customer_id, ts, rng.choice(PAGES)))

            if customer_id == BOUNDARY_CUSTOMER and visit_index == 0:
                # Exactly at the threshold: same session under "> 30 min", a new one
                # under ">= 30 min". The recipe documents which rule it uses.
                event_id += 1
                events.append((event_id, customer_id, ts + SESSION_GAP, "/product"))

    return events


def _gen_subscriptions(rng: random.Random, customers: list[tuple]) -> list[tuple]:
    """Periods that overlap, abut, and lapse -- the three cases the merge must handle."""
    signup_by_customer = {row[0]: row[4] for row in customers}
    rows: list[tuple] = []
    subscription_id = 0

    for customer_id in range(1, N_SUBSCRIBED_CUSTOMERS + 1):
        cursor = signup_by_customer[customer_id] + dt.timedelta(days=rng.randrange(0, 60))
        plan = rng.choice(PLANS)
        for _ in range(rng.randint(1, 6)):
            period_start = cursor
            period_end = period_start + dt.timedelta(days=rng.choice((28, 30, 31, 90)))
            if period_end > LAST_DAY:
                break
            subscription_id += 1
            rows.append((subscription_id, customer_id, plan, period_start, period_end))

            roll = rng.random()
            if roll < 0.20:  # a real lapse
                cursor = period_end + dt.timedelta(days=rng.randrange(5, 90))
            elif roll < 0.35:  # renewal booked early, so the periods overlap
                cursor = period_end - dt.timedelta(days=rng.randrange(1, 10))
            else:  # renewal abuts the previous period exactly
                cursor = period_end
            if rng.random() < 0.25:
                plan = rng.choice(PLANS)

    return rows


def generate(seed: int = SEED) -> Warehouse:
    """Generate every table from one seeded RNG, in a fixed draw order."""
    rng = random.Random(seed)
    customers = _gen_customers(rng)
    products = _gen_products(rng)
    employees = _gen_employees(rng)
    orders, order_items = _gen_orders_and_items(rng, customers, products)
    page_events = _gen_page_events(rng)
    subscriptions = _gen_subscriptions(rng, customers)
    return Warehouse(
        customers=customers,
        products=products,
        employees=employees,
        orders=orders,
        order_items=order_items,
        page_events=page_events,
        subscriptions=subscriptions,
    )


def literal(value: object) -> str:
    """Render one generated value as a DuckDB SQL literal.

    Why literals and not bound parameters: on this DuckDB build, binding a parameter
    costs about 3 ms regardless of how the inserts are batched, so a parameterised
    load of ~26k rows takes over a minute while the same rows rendered into one
    ``INSERT ... VALUES`` statement per table load in well under a second. Measured
    both ways -- see NOTES.md.

    Every value passed here is machine-generated by this module, never user input.
    The string case still escapes properly, and ``tests/test_warehouse.py`` checks it,
    because an escaper that is only correct for today's name pool is a trap for
    whoever widens the pool later.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, dt.date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    raise TypeError(f"no SQL literal for {type(value).__name__}: {value!r}")


def _insert_statement(table: str, rows: list[tuple]) -> str:
    values = ",".join("(" + ",".join(literal(v) for v in row) + ")" for row in rows)
    return f"INSERT INTO {table} VALUES {values}"


def _parents_first(rows: list[tuple], id_at: int = 0, parent_at: int = 3) -> list[list[tuple]]:
    """Split self-referencing rows into batches, each insertable after the last.

    One multi-row INSERT cannot satisfy a self-referencing foreign key: DuckDB checks
    each new row against the table as it stood *before* the statement, so a manager
    inserted by the same statement as their report does not count yet. Employees load
    one level of the org at a time instead -- four statements for this tree.
    """
    remaining = list(rows)
    present: set[int] = set()
    batches: list[list[tuple]] = []
    while remaining:
        batch = [r for r in remaining if r[parent_at] is None or r[parent_at] in present]
        if not batch:
            raise ValueError("self-referencing rows have a cycle or a missing parent")
        batches.append(batch)
        present.update(r[id_at] for r in batch)
        batched = {r[id_at] for r in batch}
        remaining = [r for r in remaining if r[id_at] not in batched]
    return batches


def load(con: duckdb.DuckDBPyConnection, warehouse: Warehouse | None = None) -> Warehouse:
    """Create the schema on ``con`` and load it. Existing tables are dropped."""
    warehouse = warehouse or generate()
    for table in reversed(TABLES):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(SCHEMA)
    for table in TABLES:
        rows = getattr(warehouse, table)
        if not rows:
            continue
        batches = _parents_first(rows) if table == "employees" else [rows]
        for batch in batches:
            con.execute(_insert_statement(table, batch))
    return warehouse


def connect(database: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """Open a connection. ``None`` means in-memory."""
    return duckdb.connect(str(database) if database is not None else ":memory:")


def build(database: Path, seed: int = SEED) -> dict[str, int]:
    """Build the on-disk warehouse from scratch and return its row counts."""
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    with connect(database) as con:
        return load(con, generate(seed)).row_counts()


def in_memory(seed: int = SEED) -> duckdb.DuckDBPyConnection:
    """A loaded in-memory warehouse -- what the tests use."""
    con = connect(None)
    load(con, generate(seed))
    return con
