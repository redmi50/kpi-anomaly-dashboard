"""The warehouse catalogue.

The catalogue is the single source of truth, so these tests check that the
definitions it holds are coherent and that the DDL it generates matches them.
The grain sentences are checked because they are the specification a scenario
violates: a table whose grain is not stated cannot have its grain queried wrongly.
"""

from __future__ import annotations

import pytest

from kpi_audit.catalogue import (
    BUSINESS_KEY,
    DEDUPLICATED,
    ROLE_NAMES,
    TABLES,
    TABLE_NAMES,
    VIEW_NAMES,
    ddl,
    ddl_for_all,
    role,
)


class TestTables:
    def test_the_fact_is_declared_after_the_dimensions_it_references(self):
        order = list(TABLE_NAMES)
        assert order.index("fact_order_lines") > order.index("dim_region")
        assert order.index("fact_order_lines") > order.index("dim_channel")
        assert order.index("fact_order_lines") > order.index("load_audit")

    def test_every_table_states_its_grain(self):
        for name, table in TABLES.items():
            assert table.grain, name
            assert table.grain == table.grain.strip()

    def test_the_fact_grain_distinguishes_a_line_from_an_order(self):
        grain = TABLES["fact_order_lines"].grain
        assert "one row per order line per load" in grain
        assert "not the same as one row per order line" in grain

    def test_every_table_has_a_surrogate_primary_key(self):
        for name, table in TABLES.items():
            keys = [column.name for column in table.columns if column.primary_key]
            assert len(keys) == 1, (name, keys)

    def test_the_business_key_is_the_pair_the_dedupe_partitions_on(self):
        assert BUSINESS_KEY == ("order_id", "order_line_no")
        for column in BUSINESS_KEY:
            assert column in TABLES["fact_order_lines"].column_names
        for column in BUSINESS_KEY:
            assert f"PARTITION BY {', '.join(BUSINESS_KEY)}" in DEDUPLICATED

    def test_the_fact_carries_both_a_surrogate_and_a_business_key(self):
        names = TABLES["fact_order_lines"].column_names
        assert "line_id" in names
        assert set(BUSINESS_KEY).issubset(names)
        assert "line_id" not in BUSINESS_KEY

    def test_status_is_nullable_because_the_feed_lags(self):
        column = TABLES["fact_order_lines"].column("status")
        assert column.nullable

    def test_the_list_price_is_described_as_restricted(self):
        column = TABLES["dim_product"].column("unit_price")
        assert "restricted" in column.description.lower()

    def test_column_lookup_rejects_an_unknown_name(self):
        with pytest.raises(KeyError):
            TABLES["dim_region"].column("nope")


class TestDdl:
    def test_the_ddl_states_every_column_in_declaration_order(self):
        statement = ddl(TABLES["dim_region"])
        positions = [
            statement.index(column.name)
            for column in TABLES["dim_region"].columns
        ]
        assert positions == sorted(positions)

    def test_a_primary_key_is_declared_as_one(self):
        assert "load_id INTEGER PRIMARY KEY" in ddl(TABLES["load_audit"])

    def test_a_reference_names_the_table_and_the_column_not_the_pair(self):
        statement = ddl(TABLES["fact_order_lines"])
        assert "REFERENCES load_audit(batch_id)" in statement
        assert "REFERENCES load_audit.batch_id" not in statement

    def test_foreign_keys_can_be_omitted_for_a_loader_that_needs_them_off(self):
        statement = ddl(TABLES["fact_order_lines"], include_foreign_keys=False)
        assert "REFERENCES" not in statement

    def test_every_table_appears_in_the_combined_ddl(self):
        script = ddl_for_all()
        for name in TABLE_NAMES:
            assert f"CREATE TABLE {name} (" in script

    def test_the_combined_ddl_ends_each_statement(self):
        assert ddl_for_all().rstrip().endswith(";")


class TestViews:
    def test_there_are_three_certified_views(self):
        assert VIEW_NAMES == (
            "v_revenue_by_month",
            "v_revenue_by_channel",
            "v_load_reconciliation",
        )

    def test_both_revenue_views_deduplicate(self):
        from kpi_audit.catalogue import VIEWS

        for name in ("v_revenue_by_month", "v_revenue_by_channel"):
            assert "ROW_NUMBER() OVER" in VIEWS[name], name
            assert "load_rank = 1" in VIEWS[name], name

    def test_both_revenue_views_book_an_unknown_status(self):
        from kpi_audit.catalogue import VIEWS, booked

        for name in ("v_revenue_by_month", "v_revenue_by_channel"):
            assert booked("f") in VIEWS[name], name

    def test_the_revenue_views_name_the_schema_of_the_fact_table(self):
        from kpi_audit.catalogue import VIEWS

        for name in ("v_revenue_by_month", "v_revenue_by_channel"):
            assert "main.fact_order_lines" in VIEWS[name], name

    def test_the_reconciliation_view_counts_lines_per_batch(self):
        from kpi_audit.catalogue import VIEWS

        view = VIEWS["v_load_reconciliation"]
        assert "COUNT(f.line_id) AS rows_found" in view
        assert "COUNT(f.line_id) - l.row_count AS variance" in view

    def test_a_view_can_be_created_from_its_definition(self):
        from kpi_audit.catalogue import create_view_sql

        assert create_view_sql("v_revenue_by_month").startswith(
            "CREATE VIEW v_revenue_by_month AS"
        )


class TestRoles:
    def test_there_are_four_roles(self):
        assert ROLE_NAMES == ("engineer", "analyst", "auditor", "contractor")

    def test_every_role_is_described(self):
        for name in ROLE_NAMES:
            assert role(name).description

    def test_an_unknown_role_is_rejected(self):
        with pytest.raises(KeyError):
            role("nope")

    def test_the_engineer_reads_every_table(self):
        engineer = role("engineer")
        for name in TABLE_NAMES:
            assert engineer.allows_table(name), name

    def test_the_analyst_cannot_read_the_load_log(self):
        assert not role("analyst").allows_table("load_audit")

    def test_the_analyst_cannot_read_list_prices(self):
        analyst = role("analyst")
        assert analyst.allows_table("dim_product")
        assert not analyst.allows_column("dim_product", "unit_price")

    def test_the_analyst_can_read_the_columns_the_dedupe_needs(self):
        analyst = role("analyst")
        for column in ("order_id", "order_line_no", "loaded_at", "line_id"):
            assert analyst.allows_column("fact_order_lines", column), column

    def test_the_contractor_cannot_read_the_fact(self):
        assert not role("contractor").allows_table("fact_order_lines")

    def test_a_table_not_granted_refuses_every_column(self):
        contractor = role("contractor")
        assert not contractor.allows_column("fact_order_lines", "net_amount")

    def test_the_auditor_can_reconcile_a_batch(self):
        auditor = role("auditor")
        assert auditor.allows_table("load_audit")
        assert auditor.allows_column("load_audit", "row_count")
