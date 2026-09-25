"""The seeded warehouse.

The data is generated from a fixed seed, so every figure the audit reports is
reproducible on any machine, and each anomaly is placed deliberately rather than
discovered by luck. That is the point of a simulated warehouse: the audit can be
judged by whether it finds the defects that were planted, and by whether it stays
silent on the control.

Five decisions shape what is seeded here.

* One defect per scenario. Every scenario query is the certified definition with
  exactly one clause changed, so the discrepancy it produces can only be caused
  by that change. A scenario that mutated two things would measure the sum and
  explain neither. A test asserts the one clause property rather than trusting
  it.
* The defects are sized differently on purpose. A month missing from a reporting
  window and a returned order counted as revenue are seeded large enough to be
  unmistakable; an unknown status dropped by a null comparison and a final day
  lost to a boundary are seeded small enough that they would pass a glance at a
  dashboard.
* One defect is seeded below the threshold that matters. The final day lost to an
  inclusive boundary moves the headline figure by a fraction of a percent, which
  is exactly the case a reviewer has to make a judgement about, so the severity
  bands have to be able to say low rather than fire on everything.
* The replayed batch is large on purpose. It is the cutover batch that carried the
  backlog at a platform migration, so its retry duplicates a material share of the
  warehouse. A retry of an ordinary day would move the headline figure by a few
  basis points and the duplicate would survive any review, which is how a
  replayed load actually gets through.
* The control is a genuine pass. Nothing in the certified layer is defective, so a
  scenario that reports the certified definition as its own query raises nothing.
  Without it, a set of scenarios that fired on everything would look the same as
  a set that worked.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta

from . import catalogue

# The seed, and the two dates that size the reporting period. Fixed so that every
# figure in the README and in the test suite is reproducible.
SEED = 20250630
PERIOD_START = date(2025, 1, 1)
PERIOD_END = date(2025, 6, 30)

# The status feed lags the order feed at the end of the period. Orders placed in
# this window have an unknown status, and the agreed rule books them as revenue
# because they are not known to be cancelled. A query written as ``NOT IN`` drops
# them instead, silently and in the direction that understates revenue.
STATUS_LAG_START = date(2025, 6, 21)
STATUS_LAG_RATE = 0.30

# A channel launched before its dimension row was loaded. Fact rows for the new
# channel exist; the channel dimension does not know about it yet. The region
# feed has a similar gap, where the region is captured after the order arrives.
CALL_CENTRE_KEY = 5
CALL_CENTRE_START = date(2025, 6, 10)
CALL_CENTRE_RATE = 0.05
REGION_NULL_RATE = 0.06

# The cutover batch, and the region its leftover debug view filtered to. The
# cutover batch carried the backlog of orders accumulated before the migration,
# then failed and was retried. The retry succeeded after a pricing correction,
# and it appended its rows rather than replacing the ones already written.
#
# The batch id is not the date. A migration batch is named by the pipeline that
# ran it rather than by the day it landed, and giving it the daily batch's name
# would make the load reconciliation report two rows for one batch id, which
# reads as a second defect and is only a naming collision.
CUTOVER_BATCH = "B-MIG-BACKLOG-2025-05-10"
CUTOVER_DAY = date(2025, 5, 10)
CUTOVER_ORDERS = 140
CUTOVER_PRICE_CORRECTION = 0.08
SHADOW_REGION_KEY = 1

REGIONS = (
    (1, "NE", "North East", "Nigeria"),
    (2, "SE", "South East", "Nigeria"),
    (3, "MW", "Mid West", "Nigeria"),
    (4, "WE", "West End", "Ghana"),
)

# Channel key 5 is deliberately absent. That absence is the orphan.
CHANNELS = (
    (1, "web", 1),
    (2, "mobile_app", 1),
    (3, "partner_api", 1),
    (4, "retail", 0),
)

PRODUCTS = (
    (1, "SKU-1001", "Starter Plan", "subscription", 29.00),
    (2, "SKU-1002", "Standard Plan", "subscription", 79.00),
    (3, "SKU-1003", "Premium Plan", "subscription", 149.00),
    (4, "SKU-1004", "Analytics Addon", "subscription", 45.00),
    (5, "SKU-1005", "Support Addon", "subscription", 60.00),
    (6, "SKU-2001", "Onboarding Service", "services", 500.00),
    (7, "SKU-2002", "Training Day", "services", 1200.00),
    (8, "SKU-3001", "Hardware Kit", "hardware", 340.00),
)

# Order status, with the weights the ordinary case is drawn from. Every one of
# these is revenue bearing except CANCELLED and RETURNED, and the difference
# between the two exclusion rules is one of the seeded defects.
STATUS_WEIGHTS = (
    ("COMPLETED", 60),
    ("SHIPPED", 13),
    ("CANCELLED", 17),
    ("RETURNED", 10),
)

DISCOUNT_CHOICES = (0.0, 0.0, 0.0, 0.05, 0.10, 0.15, 0.20)


def _dates() -> list[date]:
    """Every date in the reporting period."""
    span = (PERIOD_END - PERIOD_START).days
    return [PERIOD_START + timedelta(days=offset) for offset in range(span + 1)]


def _weighted_status(rng: random.Random) -> str:
    """Pick an ordinary order status."""
    names = [name for name, _ in STATUS_WEIGHTS]
    weights = [weight for _, weight in STATUS_WEIGHTS]
    return rng.choices(names, weights=weights, k=1)[0]


class _Loader:
    """Builds order lines and remembers what each batch wrote.

    The loader is a small object rather than a set of local variables because the
    count a batch registers in the load log has to be exactly the number of rows
    that batch wrote, or the reconciliation between the two is measuring the
    generator instead of the warehouse.
    """

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.lines: list[tuple] = []
        self.line_id = 0
        self.order_number = 0
        self.written: dict[str, int] = {}

    def order(
        self,
        *,
        batch_id: str,
        day: date,
        ordered_at: str,
        loaded_at: str,
        status: str | None,
        region_key: int | None,
        channel_key: int | None,
    ) -> None:
        """Write every line of one order."""
        self.order_number += 1
        order_id = f"ORD-{self.order_number:06d}"
        for order_line_no in range(1, self.rng.randint(1, 3) + 1):
            product_key, _, _, category, unit_price = self.rng.choice(PRODUCTS)
            quantity = 1 if category != "hardware" else self.rng.randint(1, 3)
            discount_pct = self.rng.choice(DISCOUNT_CHOICES)
            gross = round(unit_price * quantity, 2)
            discount = round(gross * discount_pct, 2)
            self.line_id += 1
            self.lines.append(
                (
                    self.line_id,
                    order_id,
                    order_line_no,
                    batch_id,
                    day.year * 10000 + day.month * 100 + day.day,
                    ordered_at,
                    region_key,
                    channel_key,
                    product_key,
                    status,
                    quantity,
                    gross,
                    discount,
                    round(gross - discount, 2),
                    loaded_at,
                )
            )
            self.written[batch_id] = self.written.get(batch_id, 0) + 1


def generate() -> dict[str, list[tuple]]:
    """Generate every row of the warehouse.

    Returns the rows keyed by table name, in each table's declaration order and
    ready to be inserted. Generating rather than inserting keeps the row
    generation testable without a database.
    """
    rng = random.Random(SEED)

    dates = _dates()
    dim_date = [
        (
            day.year * 10000 + day.month * 100 + day.day,
            day.isoformat(),
            day.year,
            day.month,
            day.strftime("%B"),
            day.isocalendar()[1],
            int(day.weekday() >= 5),
        )
        for day in dates
    ]

    loader = _Loader(rng)
    load_audit: list[tuple] = []

    for day in dates:
        batch_id = f"B-{day.isoformat()}"
        orders_today = rng.randint(9, 12)
        if day.weekday() >= 5:
            orders_today = max(5, orders_today - 4)

        for _ in range(orders_today):
            region_key: int | None = rng.choice([1, 2, 2, 3, 3, 4])
            if rng.random() < REGION_NULL_RATE:
                region_key = None

            if day >= CALL_CENTRE_START and rng.random() < CALL_CENTRE_RATE:
                channel_key: int | None = CALL_CENTRE_KEY
            else:
                channel_key = rng.choice([1, 1, 2, 2, 3, 4])

            if day >= STATUS_LAG_START and rng.random() < STATUS_LAG_RATE:
                status: str | None = None
            else:
                status = _weighted_status(rng)

            loader.order(
                batch_id=batch_id,
                day=day,
                ordered_at=(
                    f"{day.isoformat()} "
                    f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
                ),
                loaded_at=f"{(day + timedelta(days=1)).isoformat()} 03:{rng.randint(0, 59):02d}:00",
                status=status,
                region_key=region_key,
                channel_key=channel_key,
            )

        load_audit.append(
            (
                len(load_audit) + 1,
                batch_id,
                f"s3://landing/orders/{day.isoformat()}.csv",
                f"{day.isoformat()} 03:00:00",
                loader.written[batch_id],
                "OK",
            )
        )

    _cutover(loader)

    load_audit.append(
        (
            len(load_audit) + 1,
            CUTOVER_BATCH,
            "s3://landing/migration/orders_backlog.csv",
            f"{CUTOVER_DAY.isoformat()} 02:00:00",
            loader.written[CUTOVER_BATCH],
            # The loader recorded the batch as complete after its retry. It did
            # not record that the retry appended to the rows the first attempt had
            # already written, which is the whole reason the duplicate is
            # invisible to the load log.
            "OK",
        )
    )

    loader.lines.extend(_replay(rng, loader.lines))

    return {
        "dim_date": dim_date,
        "dim_region": list(REGIONS),
        "dim_channel": list(CHANNELS),
        "dim_product": list(PRODUCTS),
        "load_audit": load_audit,
        "fact_order_lines": loader.lines,
    }


def _cutover(loader: _Loader) -> None:
    """Write the backlog batch, dated across the period before the migration.

    The batch carried orders that accumulated before the platform cutover, so its
    rows are dated across the period rather than on the day it was loaded. That
    is also why the batch is large: a cutover carries a backlog, and a backlog is
    most of a quarter rather than most of a day.
    """
    span = (CUTOVER_DAY - PERIOD_START).days
    for _ in range(CUTOVER_ORDERS):
        offset = loader.rng.randint(0, span)
        day = PERIOD_START + timedelta(days=offset)
        ordered_at = (
            f"{day.isoformat()} "
            f"{loader.rng.randint(0, 23):02d}:{loader.rng.randint(0, 59):02d}:{loader.rng.randint(0, 59):02d}"
        )
        loader.order(
            batch_id=CUTOVER_BATCH,
            day=day,
            ordered_at=ordered_at,
            loaded_at=f"{CUTOVER_DAY.isoformat()} 02:{loader.rng.randint(0, 59):02d}:00",
            status=_weighted_status(loader.rng),
            region_key=loader.rng.choice([1, 2, 2, 3, 3, 4]),
            channel_key=loader.rng.choice([1, 1, 2, 2, 3, 4]),
        )


def _replay(rng: random.Random, lines: list[tuple]) -> list[tuple]:
    """The rows the retried batch wrote a second time.

    The retry succeeded on its second attempt, after correcting the amounts on
    the batch that had been loaded from an out of date price list. The rows are
    appended rather than replacing the originals, which is what an append only
    loader does, and the load log still holds one row for the batch. The
    duplicate is therefore invisible to anything that counts loads and visible to
    anything that counts lines.

    The repeated rows carry a later load timestamp, so the agreed definition,
    which keeps the most recently loaded row per business key, keeps the
    correction. A definition that does not deduplicate keeps both.
    """
    duplicates: list[tuple] = []
    line_id = max(row[0] for row in lines)
    for row in lines:
        if row[3] != CUTOVER_BATCH:
            continue
        line_id += 1
        gross = round(row[11] * (1 - CUTOVER_PRICE_CORRECTION), 2)
        discount = round(row[12] * (1 - CUTOVER_PRICE_CORRECTION), 2)
        duplicates.append(
            (
                line_id,
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
                row[8],
                row[9],
                row[10],
                gross,
                discount,
                round(gross - discount, 2),
                f"{PERIOD_END.isoformat()} 03:{rng.randint(0, 59):02d}:00",
            )
        )
    return duplicates


def connect(location: str) -> sqlite3.Connection:
    """Open a connection with the settings the warehouse runs under.

    Foreign keys are left off. That is not an oversight: the warehouse
    deliberately contains a fact row whose channel has not been loaded yet, and
    the point of the audit is to detect that gap rather than to have the loader
    refuse to write it.
    """
    connection = sqlite3.connect(location)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA temp_store = MEMORY")
    return connection


def build(location: str = ":memory:") -> sqlite3.Connection:
    """Create the warehouse and load it.

    The schema comes from the catalogue, so the tables a check inspects are the
    tables that were created. The certified views are created last, because they
    read the base tables.
    """
    connection = connect(location)
    connection.executescript(catalogue.ddl_for_all())
    rows = generate()

    for table in catalogue.TABLES.values():
        payload = rows[table.name]
        if not payload:
            continue
        placeholders = ", ".join("?" for _ in table.columns)
        connection.executemany(
            f"INSERT INTO {table.name} VALUES ({placeholders})", payload
        )

    for name in catalogue.VIEW_NAMES:
        connection.execute(catalogue.create_view_sql(name))
    connection.commit()
    return connection


def row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Row count per catalogue table."""
    return {
        name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in catalogue.TABLE_NAMES
    }


def main_objects(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every object in the main schema, as (type, name)."""
    return [
        (row["type"], row["name"])
        for row in connection.execute(
            "SELECT type, name FROM sqlite_master ORDER BY name"
        )
    ]


def temp_objects(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every object in the connection's temporary schema, as (type, name).

    Temporary objects live in their own schema and are not listed alongside the
    warehouse objects, which is why a leftover view can hide in a session
    without appearing anywhere in the schema a reviewer reads.
    """
    return [
        (row["type"], row["name"])
        for row in connection.execute(
            "SELECT type, name FROM sqlite_temp_master ORDER BY name"
        )
    ]


def shadowed_names(connection: sqlite3.Connection) -> list[str]:
    """Temporary objects whose name is also a name in the main schema.

    A name that appears in both schemas is resolved to the temporary object, so a
    query that names it unqualified is silently reading something other than the
    table the reviewer is looking at. This is the signature of that situation,
    and it is checked structurally rather than by reading the query text, because
    the query text looks entirely ordinary.
    """
    main_names = {name for _, name in main_objects(connection)}
    return sorted(name for _, name in temp_objects(connection) if name in main_names)


def scalar(connection: sqlite3.Connection, sql: str) -> float | None:
    """Run a single value query and return the value, or None for no rows."""
    row = connection.execute(sql).fetchone()
    return None if row is None else row[0]


def rows_as_dicts(connection: sqlite3.Connection, sql: str) -> list[dict[str, object]]:
    """Run a query and return plain dictionaries."""
    return [dict(row) for row in connection.execute(sql)]
