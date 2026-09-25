"""Role based access, enforced by the engine rather than by convention.

The CV for this project claims a simulated warehouse with roles and permissions.
A permission that is written in a document and not enforced is not a permission,
so this module installs a real SQLite authorizer on the connection. Every read
the engine plans is offered to the callback, which allows or refuses it against
the role's grant. A refused read raises, so a query that reaches for a restricted
column fails loudly instead of returning a number.

That matters for the audit in one specific way. The restricted column is the list
price, and the load log is restricted from the analyst role. If those grants were
advisory, the reconciliation queries in this package would still run and would
still look right; the point of enforcing them is that a query can be shown to run
under a stated role, and refused where it should be.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .catalogue import ROLES, Role, role

# SQLite reports the requested action as an integer. Only these three matter to a
# read only role: table and column reads, and the transient read of a schema
# table that the engine performs while planning.
READ = sqlite3.SQLITE_READ
SELECT = sqlite3.SQLITE_SELECT

# The temporary schema is not covered by a grant. A role that may not create
# temporary objects cannot shadow a warehouse table either, which closes the one
# way a restricted read could be laundered through an object that has no grant to
# inspect.
TEMP_CREATE_ACTIONS = (
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
)

# The grant is described over the real tables. Reads of the engine's own schema
# tables are how a query is resolved at all, and refusing them would stop every
# query rather than one, so they are allowed and reported separately.
SCHEMA_TABLES = ("sqlite_master", "sqlite_temp_master", "sqlite_schema")


class AccessDenied(sqlite3.DatabaseError):
    """Raised when a role reads something its grant does not cover.

    The engine refuses the read and raises a plain ``DatabaseError``, which is
    the right behaviour but not a useful error: the message names the object and
    says nothing about the role. ``Session`` catches that refusal and re-raises
    it as this type, carrying the role and the target, so a caller can tell a
    permission problem apart from a syntax error without reading the message.
    """

    def __init__(self, role_name: str, table: str, column: str, db: str) -> None:
        target = f"{db}.{table}.{column}" if column else f"{db}.{table}"
        super().__init__(f"role {role_name!r} may not read {target}")
        self.role_name = role_name
        self.table = table
        self.column = column
        self.db = db


@dataclass
class ReadLog:
    """Every read a grouped statement attempted, and how each was decided."""

    role_name: str = ""
    allowed: list[tuple[str, str, str]] = field(default_factory=list)
    denied: list[tuple[str, str, str]] = field(default_factory=list)
    temp_creations: list[str] = field(default_factory=list)

    @property
    def tables_read(self) -> list[str]:
        """Base tables that were read, in first seen order."""
        seen: list[str] = []
        for table, _, _ in self.allowed:
            if table and table not in seen:
                seen.append(table)
        return seen

    @property
    def columns_read(self) -> list[str]:
        """Columns that were read, as table.column, in first seen order."""
        seen: list[str] = []
        for table, column, _ in self.allowed:
            if column and f"{table}.{column}" not in seen:
                seen.append(f"{table}.{column}")
        return seen

    def as_dict(self) -> dict[str, object]:
        """A plain dictionary for JSON output."""
        return {
            "role": self.role_name,
            "tables_read": self.tables_read,
            "columns_read": self.columns_read,
            "denied": [f"{t}.{c}" if c else t for t, c, _ in self.denied],
            "temp_creations": list(self.temp_creations),
        }


def _classify(db: str, table: str) -> str:
    """Where a reported read came from.

    The engine reports a read of a temporary object against the ``temp`` schema
    and a read of the warehouse against ``main``. A column read against ``temp``
    is still a read of a warehouse column whenever the temporary object is what
    resolved the name, so both are reported.
    """
    return "temp" if db == "temp" else "main"


def make_authorizer(
    granted: Role, log: ReadLog
) -> "object":
    """Build the callback that enforces one role's grant.

    The callback records every decision into ``log`` before it returns, so the
    access log and the enforcement cannot drift: a read that was refused appears
    in the log as refused precisely because the same branch refused it.
    """

    def authorizer(action, arg1, arg2, dbname, source):  # noqa: ANN001
        if action in TEMP_CREATE_ACTIONS:
            log.temp_creations.append(f"{arg1} ({dbname or 'main'})")
            return sqlite3.SQLITE_DENY
        if action != READ:
            return sqlite3.SQLITE_OK

        table = arg1 or ""
        column = arg2 or ""

        if table in SCHEMA_TABLES:
            log.allowed.append((table, column, dbname or "main"))
            return sqlite3.SQLITE_OK

        if not granted.allows_table(table) or not granted.allows_column(table, column):
            log.denied.append((table, column, dbname or "main"))
            return sqlite3.SQLITE_DENY

        log.allowed.append((table, column, _classify(dbname or "main", table)))
        return sqlite3.SQLITE_OK

    return authorizer


class Session:
    """A connection wrapped in one role's grant.

    Use it as a context manager so the authorizer is always removed again. A role
    left installed on a shared connection is how a later query ends up running
    under permissions nobody intended, which is the same class of defect as the
    shadowed view.
    """

    def __init__(self, connection: sqlite3.Connection, role_name: str) -> None:
        self.connection = connection
        self.granted = role(role_name)
        self.log = ReadLog(role_name=role_name)
        self._installed = False

    @property
    def role_name(self) -> str:
        """The name of the role this session runs under."""
        return self.granted.name

    def install(self) -> "Session":
        """Install the authorizer. Idempotent, so nesting is harmless."""
        if not self._installed:
            self.connection.set_authorizer(make_authorizer(self.granted, self.log))
            self._installed = True
        return self

    def uninstall(self) -> None:
        """Remove the authorizer."""
        if self._installed:
            self.connection.set_authorizer(None)
            self._installed = False

    def __enter__(self) -> "Session":
        return self.install()

    def __exit__(self, *exc_info) -> bool:
        self.uninstall()
        return False

    def scalar(self, sql: str, parameters: tuple = ()) -> object:
        """Run a single value query under this role's grant."""
        row = self._execute(sql, parameters).fetchone()
        return None if row is None else row[0]

    def rows(self, sql: str, parameters: tuple = ()) -> list[dict[str, object]]:
        """Run a query under this role's grant and return plain dictionaries."""
        return [dict(row) for row in self._execute(sql, parameters)]

    def _execute(self, sql: str, parameters: tuple = ()):
        """Run a statement, translating an engine refusal into ``AccessDenied``.

        The authorizer appends to ``denied`` before it refuses, so a refusal
        that happened while this statement was planned is the last entry in the
        log. Comparing the log against itself before and after the call is what
        distinguishes a permission refusal from a syntax error, and it does not
        depend on reading the engine's message text.
        """
        before = len(self.log.denied)
        try:
            return self.connection.execute(sql, parameters)
        except sqlite3.DatabaseError as exc:
            if len(self.log.denied) > before:
                table, column, db = self.log.denied[-1]
                raise AccessDenied(self.role_name, table, column, db) from exc
            raise

    def reset_log(self) -> None:
        """Clear the read log, so one statement can be attributed at a time.

        The lists are cleared in place rather than the log being replaced,
        because the authorizer holds a reference to this object. Replacing it
        would leave the authorizer writing into a log nobody reads.
        """
        self.log.allowed.clear()
        self.log.denied.clear()
        self.log.temp_creations.clear()


def probe_grant(connection: sqlite3.Connection, role_name: str, table: str, column: str) -> tuple[bool, str]:
    """Ask the engine whether a role can read one column.

    A grant is a claim about behaviour, and a claim about behaviour should be
    checked by attempting the behaviour rather than by reading the policy. This
    runs the smallest possible query against one column under the role and
    reports whether the engine allowed it or refused it.
    """
    statement = f"SELECT COUNT({column}) FROM {table}"
    session = Session(connection, role_name)
    with session:
        try:
            session.scalar(statement)
        except sqlite3.DatabaseError as exc:
            return False, str(exc)
    return True, ""


def grants_report(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Probe every role against every column, for the access page.

    This is the matrix the README publishes, and it is produced by trying each
    cell rather than by restating the policy, so the two cannot drift.
    """
    report: list[dict[str, object]] = []
    for role_name, granted in ROLES.items():
        cells: list[dict[str, object]] = []
        for table, column in _catalogue_columns():
            allowed, _ = probe_grant(connection, role_name, table, column)
            cells.append(
                {
                    "table": table,
                    "column": column,
                    "allowed": allowed,
                }
            )
        report.append(
            {
                "role": role_name,
                "description": granted.description,
                "columns_allowed": sum(1 for cell in cells if cell["allowed"]),
                "columns_total": len(cells),
                "cells": cells,
            }
        )
    return report


def _catalogue_columns() -> list[tuple[str, str]]:
    """Every table and column in the catalogue, as pairs."""
    from .catalogue import TABLES

    pairs: list[tuple[str, str]] = []
    for table in TABLES.values():
        for column in table.columns:
            pairs.append((table.name, column.name))
    return pairs
