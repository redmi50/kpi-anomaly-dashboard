"""The seeded warehouse.

These tests check the two properties the audit rests on: the warehouse is
deterministic, and it contains the defects that were planted rather than defects
that happened. A defect that is present by accident would make a scenario pass for
the wrong reason, so each defect is asserted to be present, and the two small ones
are asserted to be in the band their scenario needs.
"""

from __future__ import annotations

from kpi_audit import build
from kpi_audit.catalogue import TABLE_NAMES, VIEW_NAMES
from kpi_audit.warehouse import (
    CALL_CENTRE_KEY,
    CUTOVER_BATCH,
    CUTOVER_ORDERS,
    SEED,
    generate,
    main_objects,
    row_counts,
    scalar,
    shadowed_names,
    temp_objects,
)

# The agreed row set, written here as the test's own independent query rather
# than by importing the certified definition. A test that reuses the code under
# test can only confirm that the code agrees with itself.
DEDUPLICATED = """(
    SELECT f.* FROM main.fact_order_lines f
    JOIN (
        SELECT order_id, order_line_no, MAX(loaded_at) AS newest
        FROM main.fact_order_lines GROUP BY order_id, order_line_no
    ) k
      ON k.order_id = f.order_id
     AND k.order_line_no = f.order_line_no
     AND k.newest = f.loaded_at
) f"""

BOOKED = "(f.status IS NULL OR f.status NOT IN ('CANCELLED', 'RETURNED'))"


class TestDeterminism:
    def test_the_seed_is_fixed(self):
        assert SEED == 20250630

    def test_generating_twice_produces_the_same_rows(self):
        assert generate() == generate()

    def test_building_twice_produces_the_same_row_counts(self):
        first = build()
        second = build()
        try:
            assert row_counts(first) == row_counts(second)
            assert scalar(
                first, "SELECT SUM(net_amount) FROM fact_order_lines"
            ) == scalar(second, "SELECT SUM(net_amount) FROM fact_order_lines")
        finally:
            first.close()
            second.close()

    def test_the_business_key_survives_a_rebuild(self):
        first = build()
        second = build()
        try:
            assert scalar(
                first, "SELECT COUNT(DISTINCT order_id) FROM fact_order_lines"
            ) == scalar(
                second, "SELECT COUNT(DISTINCT order_id) FROM fact_order_lines"
            )
        finally:
            first.close()
            second.close()


class TestSchema:
    def test_every_catalogue_table_exists(self):
        connection = build()
        try:
            created = {name for _, name in main_objects(connection)}
            for name in TABLE_NAMES:
                assert name in created, name
        finally:
            connection.close()

    def test_every_certified_view_exists(self):
        connection = build()
        try:
            views = {name for kind, name in main_objects(connection) if kind == "view"}
            assert views == set(VIEW_NAMES)
        finally:
            connection.close()

    def test_the_warehouse_starts_with_no_temporary_objects(self):
        connection = build()
        try:
            assert temp_objects(connection) == []
            assert shadowed_names(connection) == []
        finally:
            connection.close()

    def test_the_calendar_covers_the_half_year(self):
        connection = build()
        try:
            assert scalar(connection, "SELECT COUNT(*) FROM dim_date") == 181
            assert scalar(connection, "SELECT MIN(date_key) FROM dim_date") == 20250101
            assert scalar(connection, "SELECT MAX(date_key) FROM dim_date") == 20250630
        finally:
            connection.close()

    def test_every_fact_row_resolves_to_a_calendar_row(self):
        connection = build()
        try:
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM fact_order_lines f "
                "LEFT JOIN dim_date d ON d.date_key = f.date_key "
                "WHERE d.date_key IS NULL",
            ) == 0
        finally:
            connection.close()


class TestLoadLog:
    def test_no_batch_id_appears_twice_in_the_load_log(self):
        connection = build()
        try:
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM (SELECT batch_id FROM load_audit "
                "GROUP BY batch_id HAVING COUNT(*) > 1)",
            ) == 0
        finally:
            connection.close()

    def test_the_cutover_batch_does_not_share_a_name_with_a_daily_batch(self):
        connection = build()
        try:
            assert not CUTOVER_BATCH.startswith("B-2025-")
            assert scalar(
                connection,
                f"SELECT COUNT(*) FROM load_audit WHERE batch_id = '{CUTOVER_BATCH}'",
            ) == 1
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM load_audit WHERE batch_id = 'B-2025-05-10'",
            ) == 1
        finally:
            connection.close()

    def test_exactly_one_batch_disagrees_with_the_rows_it_wrote(self):
        connection = build()
        try:
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM v_load_reconciliation WHERE variance <> 0",
            ) == 1
        finally:
            connection.close()

    def test_the_disagreeing_batch_is_the_cutover_batch(self):
        connection = build()
        try:
            assert scalar(
                connection,
                "SELECT batch_id FROM v_load_reconciliation WHERE variance <> 0",
            ) == CUTOVER_BATCH
        finally:
            connection.close()

    def test_the_replayed_batch_has_more_rows_than_it_registered(self):
        connection = build()
        try:
            registered = scalar(
                connection,
                f"SELECT row_count FROM load_audit WHERE batch_id = '{CUTOVER_BATCH}'",
            )
            found = scalar(
                connection,
                f"SELECT COUNT(*) FROM fact_order_lines WHERE batch_id = '{CUTOVER_BATCH}'",
            )
            assert found == 2 * registered
            # The variance is counted in lines while the cutover was planned in
            # orders, and a batch writes one to three lines per order, so the
            # variance is larger than the number of orders replayed. Asserting
            # the two are equal would be asserting that the grain does not
            # exist.
            assert scalar(
                connection,
                f"SELECT variance FROM v_load_reconciliation "
                f"WHERE batch_id = '{CUTOVER_BATCH}'",
            ) == found - registered
            assert found - registered > CUTOVER_ORDERS
        finally:
            connection.close()

    def test_every_other_batch_reconciles(self):
        connection = build()
        try:
            # One of the 182 registered loads is the cutover batch, which is the
            # one load that disagrees with the rows it wrote.
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM v_load_reconciliation WHERE variance = 0",
            ) == scalar(connection, "SELECT COUNT(*) FROM load_audit") - 1
        finally:
            connection.close()


class TestPlantedDefects:
    def test_the_replay_duplicates_the_business_key_deliberately(self):
        connection = build()
        try:
            duplicated = scalar(
                connection,
                "SELECT COUNT(*) FROM (SELECT order_id, order_line_no "
                "FROM fact_order_lines GROUP BY order_id, order_line_no "
                "HAVING COUNT(*) > 1)",
            )
            assert duplicated > 0
            assert duplicated < scalar(
                connection, "SELECT COUNT(*) FROM fact_order_lines"
            )
        finally:
            connection.close()

    def test_a_duplicate_is_indistinguishable_from_an_original_by_key_alone(self):
        """The duplicated rows share every key, which is why the load log is silent.

        The replay appends rows with the same order id and line number and the same
        batch id, so nothing in the key separates them. Only the load timestamp
        does, which is what the agreed definition orders by.
        """
        connection = build()
        try:
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM ("
                "  SELECT order_id, order_line_no, COUNT(DISTINCT batch_id) AS batches "
                "  FROM fact_order_lines GROUP BY order_id, order_line_no "
                "  HAVING COUNT(*) > 1 AND batches = 1"
                ")",
            ) > 0
        finally:
            connection.close()

    def test_the_replayed_rows_carry_a_later_load_timestamp(self):
        connection = build()
        try:
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM ("
                "  SELECT order_id, order_line_no, COUNT(DISTINCT loaded_at) AS stamps "
                "  FROM fact_order_lines GROUP BY order_id, order_line_no "
                "  HAVING COUNT(*) > 1 AND stamps = 1"
                ")",
            ) == 0
        finally:
            connection.close()

    def test_the_replayed_rows_carry_corrected_amounts(self):
        """The retry was the good load, so the duplicate is not simply a copy.

        A duplicate that carried the same amount as its original would make the
        two definitions differ by a fact about the loader. It carries a smaller
        amount because the first attempt used an out of date price list, which is
        why keeping the newest row is the correction rather than an approximation.
        """
        connection = build()
        try:
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM ("
                "  SELECT order_id, order_line_no, MIN(net_amount) AS low, "
                "         MAX(net_amount) AS high "
                "  FROM fact_order_lines GROUP BY order_id, order_line_no "
                "  HAVING COUNT(*) > 1 AND low < high"
                ")",
            ) > 0
        finally:
            connection.close()

    def test_a_channel_has_fact_rows_and_no_dimension_row(self):
        connection = build()
        try:
            assert scalar(
                connection,
                f"SELECT COUNT(*) FROM dim_channel WHERE channel_key = {CALL_CENTRE_KEY}",
            ) == 0
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM fact_order_lines WHERE channel_key = "
                f"{CALL_CENTRE_KEY}",
            ) > 0
        finally:
            connection.close()

    def test_the_unmapped_channel_started_mid_period(self):
        connection = build()
        try:
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM fact_order_lines f "
                "JOIN dim_date d ON d.date_key = f.date_key "
                f"WHERE f.channel_key = {CALL_CENTRE_KEY} AND d.month < 6",
            ) == 0
        finally:
            connection.close()

    def test_some_fact_rows_carry_no_region(self):
        connection = build()
        try:
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM fact_order_lines WHERE region_key IS NULL",
            ) > 0
        finally:
            connection.close()

    def test_some_fact_rows_carry_no_status(self):
        connection = build()
        try:
            assert scalar(
                connection,
                "SELECT COUNT(*) FROM fact_order_lines WHERE status IS NULL",
            ) > 0
        finally:
            connection.close()

    def test_the_unknown_status_rows_are_at_the_end_of_the_period(self):
        connection = build()
        try:
            outside = scalar(
                connection,
                "SELECT COUNT(*) FROM fact_order_lines f "
                "JOIN dim_date d ON d.date_key = f.date_key "
                "WHERE f.status IS NULL AND d.month < 6",
            )
            inside = scalar(
                connection,
                "SELECT COUNT(*) FROM fact_order_lines f "
                "JOIN dim_date d ON d.date_key = f.date_key "
                "WHERE f.status IS NULL AND d.month = 6",
            )
            assert outside == 0
            assert inside > 0
        finally:
            connection.close()

    def test_both_cancelled_and_returned_orders_exist(self):
        connection = build()
        try:
            cancelled = scalar(
                connection,
                "SELECT COUNT(*) FROM fact_order_lines WHERE status = 'CANCELLED'",
            )
            returned = scalar(
                connection,
                "SELECT COUNT(*) FROM fact_order_lines WHERE status = 'RETURNED'",
            )
            assert 0 < returned < cancelled
        finally:
            connection.close()

    def test_orders_carry_more_than_one_line(self):
        """The line against order grain difference has to be real for the scenario."""
        connection = build()
        try:
            lines = scalar(connection, "SELECT COUNT(*) FROM fact_order_lines")
            orders = scalar(
                connection, "SELECT COUNT(DISTINCT order_id) FROM fact_order_lines"
            )
            assert lines > 1.5 * orders
        finally:
            connection.close()


class TestDefectSizes:
    def test_the_final_day_defect_is_small_enough_to_be_a_judgement(self):
        """The last day of the period must be a small share of the half year.

        This is the defect that has to land in the low band, so its size is
        asserted rather than left to the seed. If a change to the generator made
        the final day material, the audit would report a low finding as high and
        the reason would not be obvious from the finding text.
        """
        connection = build()
        try:
            total = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} WHERE {BOOKED}",
            )
            final_day = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} "
                "WHERE f.ordered_at >= '2025-06-30 00:00:00' "
                f"  AND f.ordered_at < '2025-07-01' AND {BOOKED}",
            )
            share = final_day / total
            assert 0.0 < share < 0.02, share
        finally:
            connection.close()

    def test_the_unknown_status_defect_is_small_but_not_negligible(self):
        connection = build()
        try:
            unknown = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} WHERE f.status IS NULL",
            )
            total = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} WHERE {BOOKED}",
            )
            assert 0.005 < unknown / total < 0.10
        finally:
            connection.close()

    def test_the_replayed_batch_is_large_enough_to_move_the_headline(self):
        """The replayed batch carries the migration backlog, not one day of trade.

        What is asserted is the batch's own share of the corrected headline. The
        inflation it causes is a fraction of that share, so a batch that is
        itself small could not move the headline however often it was replayed.
        A day of trade is well under one percent of the half year, so a share
        above five percent is a batch that carried a backlog.
        """
        connection = build()
        try:
            replay = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} "
                f"WHERE f.batch_id = '{CUTOVER_BATCH}'",
            )
            total = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} WHERE {BOOKED}",
            )
            assert replay / total > 0.05
        finally:
            connection.close()

    def test_the_unmapped_channel_defect_is_small(self):
        connection = build()
        try:
            unmapped = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} "
                f"WHERE f.channel_key = {CALL_CENTRE_KEY} AND {BOOKED}",
            )
            total = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} WHERE {BOOKED}",
            )
            assert 0.0 < unmapped / total < 0.02
        finally:
            connection.close()

    def test_the_unassigned_region_defect_is_material(self):
        connection = build()
        try:
            unassigned = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} "
                f"WHERE f.region_key IS NULL AND {BOOKED}",
            )
            total = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} WHERE {BOOKED}",
            )
            assert 0.02 < unassigned / total < 0.10
        finally:
            connection.close()

    def test_a_missing_month_is_unmistakable(self):
        connection = build()
        try:
            january = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} "
                "JOIN dim_date d ON d.date_key = f.date_key "
                f"WHERE d.month = 1 AND {BOOKED}",
            )
            total = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} WHERE {BOOKED}",
            )
            assert january / total > 0.10
        finally:
            connection.close()

    def test_counted_returns_are_material(self):
        connection = build()
        try:
            returned = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} "
                "WHERE f.status = 'RETURNED'",
            )
            total = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} WHERE {BOOKED}",
            )
            assert returned / total > 0.10
        finally:
            connection.close()

    def test_the_shadowed_region_is_a_small_share_of_the_headline(self):
        """The shadow defect is severe because it removes almost everything.

        The view a scenario leaves behind filters to one region, so the reported
        figure is that region's revenue. For the defect to be unmistakable the
        region has to be a small part of the whole, which is asserted here.
        """
        connection = build()
        try:
            region = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} "
                f"WHERE f.region_key = 1 AND {BOOKED}",
            )
            total = scalar(
                connection,
                f"SELECT SUM(f.net_amount) FROM {DEDUPLICATED} WHERE {BOOKED}",
            )
            assert region / total < 0.50
        finally:
            connection.close()
