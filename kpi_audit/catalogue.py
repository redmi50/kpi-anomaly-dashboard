"""The warehouse catalogue.

Everything the rest of the package needs to know about the simulated warehouse
lives here: the tables and their grain, the roles and what each one may read, the
certified views, and the DDL generated from the same definitions the checks
inspect. The catalogue is the single source of truth on purpose. If a table is
described in one place and created in another, then a check can verify a schema
that was never loaded, which is how a reconciliation report ends up reassuring
and wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The warehouse is a star schema over order lines. The fact carries a surrogate
# primary key so that a row can be identified, and a separate business key so
# that a duplicate load can be detected. Those two are not the same key, and the
# difference between them is the whole reason the replay anomaly is findable.
BUSINESS_KEY = ("order_id", "order_line_no")


@dataclass(frozen=True)
class Column:
    """One column: its name, storage type, and what it holds."""

    name: str
    type: str
    description: str
    nullable: bool = True
    primary_key: bool = False
    references: str | None = None


@dataclass(frozen=True)
class Table:
    """One relation, with the grain it is meant to hold.

    ``grain`` is a sentence rather than a key because the point of the sentence
    is to be readable next to a query that violates it.
    """

    name: str
    kind: str
    grain: str
    description: str
    columns: tuple[Column, ...] = field(default_factory=tuple)

    @property
    def column_names(self) -> tuple[str, ...]:
        """Every column name, in declaration order."""
        return tuple(column.name for column in self.columns)

    def column(self, name: str) -> Column:
        """Look up one column by name."""
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(f"Table {self.name!r} has no column {name!r}.")


def _column(name: str, type_: str, description: str, **kwargs) -> Column:
    return Column(name=name, type=type_, description=description, **kwargs)


DIM_DATE = Table(
    name="dim_date",
    kind="dimension",
    grain="one row per calendar date in the reporting period",
    description="Calendar attributes, so reporting windows can be stated on a key.",
    columns=(
        _column("date_key", "INTEGER", "Surrogate key, the date as YYYYMMDD.", nullable=False, primary_key=True),
        _column("calendar_date", "TEXT", "The date as YYYY-MM-DD."),
        _column("year", "INTEGER", "Calendar year."),
        _column("month", "INTEGER", "Calendar month number."),
        _column("month_name", "TEXT", "Calendar month name."),
        _column("iso_week", "INTEGER", "ISO week number."),
        _column("is_weekend", "INTEGER", "1 when the date falls on a Saturday or Sunday."),
    ),
)

DIM_REGION = Table(
    name="dim_region",
    kind="dimension",
    grain="one row per sales region",
    description="The regions orders are booked against.",
    columns=(
        _column("region_key", "INTEGER", "Surrogate key.", nullable=False, primary_key=True),
        _column("region_code", "TEXT", "Short code, for example NE."),
        _column("region_name", "TEXT", "Region name."),
        _column("country", "TEXT", "Country the region sits in."),
    ),
)

DIM_CHANNEL = Table(
    name="dim_channel",
    kind="dimension",
    grain="one row per sales channel",
    description=(
        "The channels orders arrive through. This dimension is loaded by a "
        "separate process from the fact, so a fact row can arrive before the "
        "channel it names exists here."
    ),
    columns=(
        _column("channel_key", "INTEGER", "Surrogate key.", nullable=False, primary_key=True),
        _column("channel_name", "TEXT", "Channel name."),
        _column("is_digital", "INTEGER", "1 for a self service digital channel."),
    ),
)

DIM_PRODUCT = Table(
    name="dim_product",
    kind="dimension",
    grain="one row per sellable product",
    description="Products, with the list price orders are priced from.",
    columns=(
        _column("product_key", "INTEGER", "Surrogate key.", nullable=False, primary_key=True),
        _column("sku", "TEXT", "Stock keeping unit."),
        _column("product_name", "TEXT", "Product name."),
        _column("category", "TEXT", "Product category."),
        _column("unit_price", "REAL", "List price per unit. Commercially restricted."),
    ),
)

FACT_ORDER_LINES = Table(
    name="fact_order_lines",
    kind="fact",
    grain="one row per order line per load, which is not the same as one row per order line",
    description=(
        "Order lines, appended by the loader. The loader is an append only "
        "process that retries failed batches, so a business key can appear more "
        "than once with a later load timestamp and corrected figures."
    ),
    columns=(
        _column("line_id", "INTEGER", "Surrogate key, unique per loaded row.", nullable=False, primary_key=True),
        _column("order_id", "TEXT", "Order number, shared by the lines of one order."),
        _column("order_line_no", "INTEGER", "Line number within the order."),
        _column("batch_id", "TEXT", "The load batch that wrote this row.", references="load_audit.batch_id"),
        _column("date_key", "INTEGER", "Order date as YYYYMMDD.", references="dim_date.date_key"),
        _column("ordered_at", "TEXT", "Order timestamp, to the second."),
        _column("region_key", "INTEGER", "Region the order is booked against.", references="dim_region.region_key"),
        _column("channel_key", "INTEGER", "Channel the order arrived through.", references="dim_channel.channel_key"),
        _column("product_key", "INTEGER", "Product ordered.", references="dim_product.product_key"),
        _column("status", "TEXT", "Order status. Nullable: the status feed can lag the order feed."),
        _column("quantity", "INTEGER", "Units ordered."),
        _column("gross_amount", "REAL", "Line value before discount."),
        _column("discount_amount", "REAL", "Discount applied to the line."),
        _column("net_amount", "REAL", "Line value after discount."),
        _column("loaded_at", "TEXT", "When the loader wrote this row. An engineering field."),
    ),
)

LOAD_AUDIT = Table(
    name="load_audit",
    kind="audit",
    grain="one row per registered load batch",
    description=(
        "One row per batch the loader registered, with the row count it wrote. A "
        "retry that succeeds on the second attempt does not register a new row, "
        "which is why a batch can appear here once and in the fact twice."
    ),
    columns=(
        _column("load_id", "INTEGER", "Surrogate key.", nullable=False, primary_key=True),
        _column("batch_id", "TEXT", "Batch identifier, the key the fact rows carry."),
        _column("source_file", "TEXT", "File the batch was read from."),
        _column("loaded_at", "TEXT", "When the batch was first attempted."),
        _column("row_count", "INTEGER", "Rows the batch registered as written."),
        _column("status", "TEXT", "The status the loader recorded for the batch."),
    ),
)

TABLES = {
    table.name: table
    for table in (
        DIM_DATE,
        DIM_REGION,
        DIM_CHANNEL,
        DIM_PRODUCT,
        LOAD_AUDIT,
        FACT_ORDER_LINES,
    )
}

TABLE_NAMES = tuple(TABLES)


def ddl(table: Table, *, include_foreign_keys: bool = True) -> str:
    """Build the CREATE TABLE statement for one table.

    The DDL is generated from the same ``Column`` objects the checks read, so a
    column cannot be described to a reviewer and stored differently to disk.
    SQLite requires a foreign key to name an existing table at creation time, so
    ``include_foreign_keys`` lets the loader drop the clause while creating
    tables that reference each other.
    """
    lines: list[str] = []
    for column in table.columns:
        piece = f"    {column.name} {column.type}"
        if column.primary_key:
            piece += " PRIMARY KEY"
        if not column.nullable and not column.primary_key:
            piece += " NOT NULL"
        if include_foreign_keys and column.references is not None:
            table_name, _, column_name = column.references.partition(".")
            piece += f" REFERENCES {table_name}({column_name})"
        lines.append(piece)
    body = ",\n".join(lines)
    return f"CREATE TABLE {table.name} (\n{body}\n)"


def ddl_for_all(*, include_foreign_keys: bool = True) -> str:
    """Build the DDL for every table, in catalogue order.

    Dimensions are declared before the fact so that a foreign key clause can
    resolve, and the audit table is declared before the fact because the fact
    references its batch identifier.
    """
    return ";\n\n".join(ddl(table, include_foreign_keys=include_foreign_keys) for table in TABLES.values()) + ";"


# Roles. A role lists the tables it may read and the individual columns it may
# not, and anything not named in ``tables`` is refused. Stating the grant as an
# allow list rather than a deny list matters: a new table added to the warehouse
# is then invisible to every role until someone grants it, rather than visible to
# all of them by default.
@dataclass(frozen=True)
class Role:
    """What one role is allowed to read."""

    name: str
    description: str
    tables: tuple[str, ...]
    denied_columns: tuple[tuple[str, str], ...] = ()

    def allows_table(self, table: str) -> bool:
        """True when the role may read the table at all."""
        return table in self.tables

    def allows_column(self, table: str, column: str) -> bool:
        """True when the role may read one column of one table."""
        if not self.allows_table(table):
            return False
        return (table, column) not in self.denied_columns


ROLES = {
    "engineer": Role(
        name="engineer",
        description="Owns the pipeline. Reads every table, including the load log.",
        tables=TABLE_NAMES,
    ),
    "analyst": Role(
        name="analyst",
        description=(
            "Business reporting. Reads the dimensions and the fact, but not the "
            "load log, which is an engineering artefact, and not list prices, "
            "which are commercially restricted."
        ),
        tables=("dim_date", "dim_region", "dim_channel", "dim_product", "fact_order_lines"),
        denied_columns=(("dim_product", "unit_price"),),
    ),
    "auditor": Role(
        name="auditor",
        description=(
            "Reads everything, including the load log, to reconcile a batch against "
            "the rows it wrote."
        ),
        tables=TABLE_NAMES,
    ),
    "contractor": Role(
        name="contractor",
        description=(
            "Reference data only. No fact rows and no list prices, so a query that "
            "needs either is refused rather than answered with a smaller number."
        ),
        tables=("dim_date", "dim_region", "dim_channel", "dim_product"),
        denied_columns=(("dim_product", "unit_price"),),
    ),
}

ROLE_NAMES = tuple(ROLES)


def role(name: str) -> Role:
    """Look up a role by name."""
    if name not in ROLES:
        raise KeyError(f"Unknown role {name!r}. Expected one of {list(ROLE_NAMES)}.")
    return ROLES[name]


# The certified layer. These views are the corrected definitions of the headline
# figures, shipped as objects in the warehouse rather than as prose in a
# document, so a dashboard can bind to a number that is already right and a
# reviewer can read the definition that produced it.
#
# Every one of them deduplicates on the business key first. That is the one
# correction that has to be in place before any other, because a replayed batch
# inflates every subsequent aggregate.
#
# The base table is named with its schema throughout. That is not decoration
# either. A temporary object whose name matches a warehouse table takes
# precedence over it, so an unqualified reference can resolve to something other
# than the table a reviewer is looking at. Naming the schema makes the agreed
# definition immune to that class of defect, and keeps the defect visible in the
# one query that has it rather than spreading it into the correction.
DEDUPLICATED = """
WITH deduplicated AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY order_id, order_line_no
            ORDER BY loaded_at DESC, line_id DESC
        ) AS load_rank
    FROM main.fact_order_lines
)
"""

def booked(alias: str) -> str:
    """The agreed rule for revenue bearing rows, written against one alias.

    Revenue is booked unless the order is known to be cancelled or returned. An
    order whose status feed has not arrived is therefore booked, which is why the
    rule is written with an explicit null branch rather than as ``NOT IN``. The
    two are not the same predicate, and the difference is a reporting defect that
    this warehouse exists to demonstrate.
    """
    return f"({alias}.status IS NULL OR {alias}.status NOT IN ('CANCELLED', 'RETURNED'))"


VIEWS = {
    "v_revenue_by_month": (
        DEDUPLICATED
        + f"""
SELECT
    d.year,
    d.month,
    COUNT(DISTINCT f.order_id) AS orders,
    ROUND(SUM(f.net_amount), 2) AS net_revenue
FROM deduplicated f
JOIN dim_date d ON d.date_key = f.date_key
WHERE f.load_rank = 1
  AND {booked("f")}
GROUP BY d.year, d.month
"""
    ),
    "v_revenue_by_channel": (
        DEDUPLICATED
        + f"""
SELECT
    COALESCE(c.channel_name, 'UNMAPPED') AS channel_name,
    ROUND(SUM(f.net_amount), 2) AS net_revenue
FROM deduplicated f
LEFT JOIN dim_channel c ON c.channel_key = f.channel_key
WHERE f.load_rank = 1
  AND {booked("f")}
GROUP BY 1
"""
    ),
    "v_load_reconciliation": """
SELECT
    l.batch_id,
    l.source_file,
    l.status AS load_status,
    l.row_count AS rows_registered,
    COUNT(f.line_id) AS rows_found,
    COUNT(f.line_id) - l.row_count AS variance
FROM load_audit l
LEFT JOIN main.fact_order_lines f ON f.batch_id = l.batch_id
GROUP BY l.batch_id, l.source_file, l.status, l.row_count
""",
}

VIEW_NAMES = tuple(VIEWS)


def create_view_sql(name: str) -> str:
    """Build the CREATE VIEW statement for one certified view."""
    return f"CREATE VIEW {name} AS{VIEWS[name]}"
