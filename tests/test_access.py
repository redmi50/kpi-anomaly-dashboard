"""Permissions, enforced by the engine.

The CV claims a warehouse with roles and permissions. A grant that is documented
and not enforced is not a grant, so these tests check that the engine refuses a
read the grant does not cover, that the refusal is an exception rather than a
smaller number, and that the temporary schema is closed off so a restricted read
cannot be laundered through an object that has no grant to inspect.
"""

from __future__ import annotations

import sqlite3

import pytest

from kpi_audit import build
from kpi_audit.access import (
    AccessDenied,
    Session,
    grants_report,
    probe_grant,
)
from kpi_audit.catalogue import ROLE_NAMES


@pytest.fixture
def connection():
    """A fresh warehouse, so an authorizer left installed cannot leak between tests."""
    con = build()
    yield con
    con.close()


class TestProbe:
    def test_the_engineer_may_read_every_column(self, connection):
        for table, column in (
            ("fact_order_lines", "net_amount"),
            ("dim_product", "unit_price"),
            ("load_audit", "row_count"),
        ):
            allowed, reason = probe_grant(connection, "engineer", table, column)
            assert allowed, (table, column, reason)

    def test_the_analyst_is_refused_the_list_price(self, connection):
        allowed, reason = probe_grant(connection, "analyst", "dim_product", "unit_price")
        assert not allowed
        assert "unit_price" in reason

    def test_the_analyst_is_refused_the_load_log(self, connection):
        allowed, _ = probe_grant(connection, "analyst", "load_audit", "row_count")
        assert not allowed

    def test_the_analyst_may_read_what_reporting_needs(self, connection):
        for table, column in (
            ("fact_order_lines", "net_amount"),
            ("fact_order_lines", "loaded_at"),
            ("fact_order_lines", "status"),
            ("dim_channel", "channel_name"),
        ):
            allowed, reason = probe_grant(connection, "analyst", table, column)
            assert allowed, (table, column, reason)

    def test_the_contractor_is_refused_the_fact_table(self, connection):
        allowed, _ = probe_grant(connection, "contractor", "fact_order_lines", "net_amount")
        assert not allowed

    def test_the_contractor_may_read_the_dimensions(self, connection):
        allowed, reason = probe_grant(connection, "contractor", "dim_region", "region_name")
        assert allowed, reason

    def test_the_auditor_may_read_the_load_log(self, connection):
        allowed, reason = probe_grant(connection, "auditor", "load_audit", "row_count")
        assert allowed, reason


class TestSession:
    def test_a_read_the_grant_covers_returns_a_value(self, connection):
        with Session(connection, "analyst") as session:
            assert session.scalar("SELECT COUNT(*) FROM fact_order_lines") > 0

    def test_a_read_the_grant_refuses_raises(self, connection):
        with Session(connection, "analyst") as session:
            with pytest.raises(AccessDenied):
                session.scalar("SELECT SUM(unit_price) FROM dim_product")

    def test_a_refusal_names_the_role_and_the_target(self, connection):
        with Session(connection, "analyst") as session:
            with pytest.raises(AccessDenied) as error:
                session.scalar("SELECT SUM(unit_price) FROM dim_product")
        assert error.value.role_name == "analyst"
        assert error.value.table == "dim_product"
        assert error.value.column == "unit_price"
        assert error.value.db == "main"

    def test_a_refusal_is_a_database_error_so_a_caller_can_catch_it_broadly(self, connection):
        assert issubclass(AccessDenied, sqlite3.DatabaseError)

    def test_the_log_records_what_was_allowed_and_what_was_denied(self, connection):
        with Session(connection, "analyst") as session:
            session.scalar("SELECT COUNT(*) FROM fact_order_lines")
            with pytest.raises(AccessDenied):
                session.scalar("SELECT SUM(unit_price) FROM dim_product")
            log = session.log.as_dict()
        assert "fact_order_lines" in log["tables_read"]
        assert "dim_product.unit_price" in log["denied"]

    def test_the_log_records_the_columns_read(self, connection):
        with Session(connection, "analyst") as session:
            session.reset_log()
            session.scalar("SELECT SUM(net_amount) FROM fact_order_lines")
            log = session.log.as_dict()
        assert "fact_order_lines.net_amount" in log["columns_read"]

    def test_the_authorizer_is_removed_when_the_session_ends(self, connection):
        with Session(connection, "analyst"):
            pass
        # If the authorizer were still installed this read would raise.
        assert connection.execute("SELECT SUM(unit_price) FROM dim_product").fetchone()[0] > 0

    def test_a_refused_read_does_not_return_a_smaller_number(self, connection):
        """The point of enforcement is that the query fails rather than under-reports."""
        with Session(connection, "contractor") as session:
            with pytest.raises(AccessDenied):
                session.scalar("SELECT COUNT(*) FROM fact_order_lines")

    def test_every_role_name_produces_a_session(self, connection):
        for role_name in ROLE_NAMES:
            with Session(connection, role_name) as session:
                assert session.role_name == role_name


class TestTemporaryObjects:
    def test_a_role_may_not_create_a_temporary_table(self, connection):
        with Session(connection, "analyst") as session:
            with pytest.raises(sqlite3.DatabaseError):
                session.connection.execute(
                    "CREATE TEMP TABLE scratch AS SELECT 1 AS one"
                )

    def test_a_role_may_not_create_a_temporary_view(self, connection):
        with Session(connection, "analyst") as session:
            with pytest.raises(sqlite3.DatabaseError):
                session.connection.execute(
                    "CREATE TEMP VIEW scratch AS SELECT 1 AS one"
                )

    def test_the_attempted_creation_is_recorded(self, connection):
        with Session(connection, "analyst") as session:
            with pytest.raises(sqlite3.DatabaseError):
                session.connection.execute(
                    "CREATE TEMP VIEW scratch AS SELECT 1 AS one"
                )
            log = session.log.as_dict()
        assert log["temp_creations"]

    def test_a_temporary_view_shadowing_a_table_is_detected(self, connection):
        """The shadow is detectable structurally, not from the query text.

        This test creates the shadow outside a session, because the point being
        demonstrated is that the audit can see it, and then asserts that the
        warehouse helpers report it.
        """
        from kpi_audit.warehouse import shadowed_names, temp_objects

        connection.execute(
            "CREATE TEMP VIEW fact_order_lines AS "
            "SELECT * FROM main.fact_order_lines WHERE region_key = 1"
        )
        try:
            assert ("view", "fact_order_lines") in temp_objects(connection)
            assert shadowed_names(connection) == ["fact_order_lines"]
        finally:
            connection.execute("DROP VIEW IF EXISTS temp.fact_order_lines")

    def test_an_unqualified_query_reads_the_shadow_and_a_qualified_one_does_not(
        self, connection
    ):
        """The reason the certified views name the schema.

        An unqualified reference resolves to the temporary object, so the same
        query text returns a different number. A reference that names the schema
        is immune, which is what keeps the correction out of the defect.
        """
        total = connection.execute("SELECT SUM(net_amount) FROM main.fact_order_lines").fetchone()[0]
        connection.execute(
            "CREATE TEMP VIEW fact_order_lines AS "
            "SELECT * FROM main.fact_order_lines WHERE region_key = 1"
        )
        try:
            unqualified = connection.execute(
                "SELECT SUM(net_amount) FROM fact_order_lines"
            ).fetchone()[0]
            qualified = connection.execute(
                "SELECT SUM(net_amount) FROM main.fact_order_lines"
            ).fetchone()[0]
            assert unqualified < qualified
            assert qualified == total
        finally:
            connection.execute("DROP VIEW IF EXISTS temp.fact_order_lines")

    def test_the_certified_views_are_immune_to_a_shadow(self, connection):
        before = connection.execute(
            "SELECT SUM(net_revenue) FROM v_revenue_by_month"
        ).fetchone()[0]
        connection.execute(
            "CREATE TEMP VIEW fact_order_lines AS "
            "SELECT * FROM main.fact_order_lines WHERE region_key = 1"
        )
        try:
            after = connection.execute(
                "SELECT SUM(net_revenue) FROM v_revenue_by_month"
            ).fetchone()[0]
            assert after == before
        finally:
            connection.execute("DROP VIEW IF EXISTS temp.fact_order_lines")


class TestGrantsReport:
    def test_every_role_appears_in_the_report(self, connection):
        names = [entry["role"] for entry in grants_report(connection)]
        assert names == list(ROLE_NAMES)

    def test_the_report_covers_every_column_of_every_table(self, connection):
        from kpi_audit.catalogue import TABLES

        expected = sum(len(table.columns) for table in TABLES.values())
        for entry in grants_report(connection):
            assert entry["columns_total"] == expected

    def test_the_engineer_is_allowed_every_column(self, connection):
        entry = next(item for item in grants_report(connection) if item["role"] == "engineer")
        assert entry["columns_allowed"] == entry["columns_total"]

    def test_the_analyst_is_allowed_fewer_columns_than_the_engineer(self, connection):
        report = {entry["role"]: entry for entry in grants_report(connection)}
        assert (
            report["analyst"]["columns_allowed"]
            < report["engineer"]["columns_allowed"]
        )

    def test_the_contractor_is_allowed_the_fewest_columns(self, connection):
        report = {entry["role"]: entry for entry in grants_report(connection)}
        assert report["contractor"]["columns_allowed"] == min(
            entry["columns_allowed"] for entry in report.values()
        )

    def test_each_cell_records_which_column_it_probed(self, connection):
        entry = grants_report(connection)[0]
        assert all(cell["table"] and cell["column"] for cell in entry["cells"])
