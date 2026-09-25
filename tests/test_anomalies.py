"""The definitions and the scenarios.

The central property of this project is that a scenario differs from its agreed
definition in exactly one clause, because that is what makes a discrepancy
attributable. These tests assert that property directly, and assert that the
builder refuses to construct a definition that cannot be read.
"""

from __future__ import annotations

import pytest

from kpi_audit.anomalies import (
    ANOMALIES,
    ANOMALY_IDS,
    CERTIFIED,
    DISCREPANCY_BANDS,
    FAMILIES,
    MEASURES,
    STATUS_RULES,
    WINDOW_NAMES,
    Definition,
    build,
    build_rows,
    clarifies,
    load_scenario,
    severity_for_relative,
)


class TestDefinition:
    def test_the_default_definition_is_the_agreed_one(self):
        assert CERTIFIED == Definition()
        assert CERTIFIED.dedupe
        assert CERTIFIED.qualify
        assert CERTIFIED.measure == "net_revenue"
        assert CERTIFIED.status_rule == "agreed"

    def test_a_definition_is_frozen(self):
        with pytest.raises(Exception):
            CERTIFIED.measure = "orders"

    def test_an_unknown_measure_is_refused(self):
        with pytest.raises(ValueError):
            Definition(measure="profit")

    def test_an_unknown_window_is_refused(self):
        with pytest.raises(ValueError):
            Definition(window="H2")

    def test_an_unknown_status_rule_is_refused(self):
        with pytest.raises(ValueError):
            Definition(status_rule="ignore_nulls")

    def test_an_unknown_join_is_refused(self):
        with pytest.raises(ValueError):
            Definition(region_join="cross")

    def test_the_window_exposes_its_start_end_and_operator(self):
        assert CERTIFIED.window_start == "2025-01-01"
        assert CERTIFIED.window_end == "2025-07-01"
        assert CERTIFIED.window_op == "half_open"

    def test_the_closed_window_ends_on_the_last_day_of_the_period(self):
        closed = Definition(window="H1_CLOSED")
        assert closed.window_end == "2025-06-30"
        assert closed.window_op == "inclusive"

    def test_there_are_three_windows(self):
        assert WINDOW_NAMES == ("H1_HALF_OPEN", "FEB_TO_JUN", "H1_CLOSED")

    def test_there_are_three_measures(self):
        assert MEASURES == ("net_revenue", "orders", "order_lines")

    def test_there_are_three_status_rules(self):
        assert STATUS_RULES == ("agreed", "exclude_unknown", "only_cancelled")


class TestClarifies:
    def test_a_definition_clarifies_itself(self):
        assert clarifies(CERTIFIED, CERTIFIED) is False

    def test_one_change_clarifies(self):
        assert clarifies(CERTIFIED, Definition(dedupe=False)) is True

    def test_two_changes_do_not_clarify(self):
        assert clarifies(CERTIFIED, Definition(dedupe=False, qualify=False)) is False

    def test_the_difference_is_symmetric(self):
        changed = Definition(window="FEB_TO_JUN")
        assert clarifies(CERTIFIED, changed) == clarifies(changed, CERTIFIED)


class TestBuilder:
    def test_the_query_reads_the_fact_table(self):
        assert "FROM main.fact_order_lines" in build(CERTIFIED)

    def test_the_query_deduplicates_when_the_definition_says_to(self):
        sql = build(CERTIFIED)
        assert "ROW_NUMBER() OVER" in sql
        assert "f.load_rank = 1" in sql

    def test_a_definition_that_does_not_deduplicate_uses_a_tautology(self):
        sql = build(Definition(dedupe=False))
        assert "ROW_NUMBER() OVER" not in sql
        # The common table expression is kept, so the two queries differ in one
        # clause rather than in their whole shape.
        assert "WITH deduplicated AS (SELECT * FROM main.fact_order_lines)" in sql
        assert "1 = 1" in sql

    def test_the_agreed_window_is_half_open(self):
        sql = build(CERTIFIED)
        assert "f.ordered_at >= '2025-01-01'" in sql
        assert "f.ordered_at < '2025-07-01'" in sql

    def test_the_closed_window_uses_between(self):
        sql = build(Definition(window="H1_CLOSED"))
        assert "BETWEEN '2025-01-01' AND '2025-06-30'" in sql

    def test_the_agreed_status_rule_books_an_unknown_status(self):
        sql = build(CERTIFIED)
        assert "f.status IS NULL OR" in sql

    def test_the_unknown_status_rule_drops_an_unknown_status(self):
        sql = build(Definition(status_rule="exclude_unknown"))
        assert "f.status IS NULL" not in sql
        assert "f.status NOT IN ('CANCELLED', 'RETURNED')" in sql

    def test_the_returns_defect_differs_from_the_agreed_rule_by_one_name(self):
        sql = build(Definition(status_rule="only_cancelled"))
        assert "'RETURNED'" not in sql
        assert "f.status IS NULL OR" in sql

    def test_an_inner_join_is_written_as_one(self):
        sql = build(Definition(channel_join="inner"))
        assert "INNER JOIN dim_channel c" in sql

    def test_a_dropped_join_is_not_written_at_all(self):
        sql = build(Definition(channel_join="none"))
        assert "dim_channel" not in sql

    def test_the_schema_can_be_omitted(self):
        assert "FROM fact_order_lines" in build(CERTIFIED, qualify=False)
        assert "FROM main.fact_order_lines" not in build(CERTIFIED, qualify=False)

    def test_the_query_reports_one_value_named_value(self):
        assert "AS value" in build(CERTIFIED)

    def test_the_row_query_lists_line_identifiers(self):
        sql = build_rows(CERTIFIED)
        assert "SELECT f.line_id" in sql
        assert "AS value" not in sql

    def test_the_row_query_uses_the_same_clauses_as_the_value_query(self):
        """The two queries differ in what they select, not in what they filter.

        The value query aggregates and the row query lists identifiers, so the
        select lists differ by design. Everything that decides which rows are
        read has to be identical, or the rows shown as the evidence would not be
        the rows behind the number.
        """
        for definition in (CERTIFIED, Definition(dedupe=False), Definition(window="H1_CLOSED")):
            value_sql = build(definition)
            rows_sql = build_rows(definition)
            # The common table expression, which is everything before the select.
            assert value_sql.split("SELECT")[0] == rows_sql.split("SELECT")[0]
            # And the whole of the filtering, from the WHERE onwards.
            assert value_sql.split("WHERE")[1] == rows_sql.split("WHERE")[1]


class TestScenario:
    def test_every_scenario_has_a_unique_id(self):
        assert len(ANOMALY_IDS) == len(set(ANOMALY_IDS))

    def test_there_are_ten_scenarios(self):
        assert len(ANOMALIES) == 10

    def test_exactly_one_scenario_is_a_control(self):
        controls = [s for s in ANOMALIES if s.family == "control"]
        assert [s.scenario_id for s in controls] == ["control_certified"]

    def test_the_control_runs_the_definition_it_should_have_run(self):
        control = load_scenario("control_certified")
        assert control.definition == control.agreed
        assert control.changed_clause == ""

    def test_every_other_scenario_changes_exactly_one_clause(self):
        for scenario in ANOMALIES:
            if scenario.family == "control":
                continue
            assert clarifies(scenario.agreed, scenario.definition), scenario.scenario_id
            assert scenario.changed_clause, scenario.scenario_id

    def test_no_scenario_changes_a_clause_it_did_not_declare(self):
        for scenario in ANOMALIES:
            if scenario.family == "control":
                continue
            assert scenario.changed_clause in Definition.__dataclass_fields__

    def test_every_family_is_used_and_named(self):
        used = {scenario.family for scenario in ANOMALIES}
        assert used == set(FAMILIES)

    def test_every_scenario_states_a_question_claim_cause_and_correction(self):
        for scenario in ANOMALIES:
            assert scenario.question.endswith("?"), scenario.scenario_id
            assert scenario.claim, scenario.scenario_id
            assert scenario.cause, scenario.scenario_id
            assert scenario.correction, scenario.scenario_id

    def test_only_the_session_scenario_carries_setup_statements(self):
        for scenario in ANOMALIES:
            if scenario.family == "session":
                assert scenario.setup, scenario.scenario_id
            else:
                assert not scenario.setup, scenario.scenario_id

    def test_a_non_session_scenario_may_not_carry_setup(self):
        from kpi_audit.anomalies import Scenario

        with pytest.raises(ValueError):
            Scenario(
                scenario_id="bad",
                title="bad",
                family="window",
                question="What?",
                claim="Something.",
                agreed=CERTIFIED,
                definition=Definition(window="H1_CLOSED"),
                cause="Because.",
                correction="Fix it.",
                setup=("SELECT 1",),
            )

    def test_a_session_scenario_must_state_its_setup(self):
        from kpi_audit.anomalies import Scenario

        with pytest.raises(ValueError):
            Scenario(
                scenario_id="bad",
                title="bad",
                family="session",
                question="What?",
                claim="Something.",
                agreed=CERTIFIED,
                definition=Definition(qualify=False),
                cause="Because.",
                correction="Fix it.",
            )

    def test_a_control_that_changed_a_clause_is_refused(self):
        from kpi_audit.anomalies import Scenario

        with pytest.raises(ValueError):
            Scenario(
                scenario_id="bad",
                title="bad",
                family="control",
                question="What?",
                claim="Something.",
                agreed=CERTIFIED,
                definition=Definition(dedupe=False),
                cause="Because.",
                correction="Fix it.",
            )

    def test_a_scenario_that_changed_two_clauses_is_refused(self):
        from kpi_audit.anomalies import Scenario

        with pytest.raises(ValueError) as error:
            Scenario(
                scenario_id="bad",
                title="bad",
                family="window",
                question="What?",
                claim="Something.",
                agreed=CERTIFIED,
                definition=Definition(window="H1_CLOSED", dedupe=False),
                cause="Because.",
                correction="Fix it.",
            )
        assert "changes 2" in str(error.value)

    def test_an_unknown_family_is_refused(self):
        from kpi_audit.anomalies import Scenario

        with pytest.raises(ValueError):
            Scenario(
                scenario_id="bad",
                title="bad",
                family="scheduling",
                question="What?",
                claim="Something.",
                agreed=CERTIFIED,
                definition=Definition(dedupe=False),
                cause="Because.",
                correction="Fix it.",
            )

    def test_the_unit_travels_with_the_measure(self):
        assert load_scenario("control_certified").unit == "currency"
        assert load_scenario("grain_rows_counted_as_orders").unit == "count"

    def test_a_count_scenario_states_its_own_correct_answer(self):
        scenario = load_scenario("grain_rows_counted_as_orders")
        assert scenario.definition.measure == "order_lines"
        assert scenario.agreed.measure == "orders"

    def test_the_session_scenario_creates_a_view_named_after_the_fact_table(self):
        scenario = load_scenario("session_shadowed_table")
        assert "CREATE TEMP VIEW fact_order_lines" in " ".join(scenario.setup)
        assert scenario.definition.qualify is False
        assert scenario.agreed.qualify is True

    def test_an_unknown_scenario_id_is_rejected(self):
        with pytest.raises(KeyError):
            load_scenario("nope")


class TestSeverity:
    def test_zero_relative_raises_nothing(self):
        assert severity_for_relative(0.0) is None

    def test_the_bands_are_relative(self):
        assert DISCREPANCY_BANDS == {"high": 0.10, "medium": 0.02}

    def test_a_relative_discrepancy_at_the_high_band_is_high(self):
        assert severity_for_relative(0.10) == "high"

    def test_a_relative_discrepancy_at_the_medium_band_is_medium(self):
        assert severity_for_relative(0.02) == "medium"

    def test_a_relative_discrepancy_below_the_medium_band_is_low(self):
        assert severity_for_relative(0.005) == "low"

    def test_a_discrepancy_above_the_high_band_is_high(self):
        assert severity_for_relative(0.83) == "high"

    def test_severity_is_monotonic(self):
        order = ["low", "medium", "high"]
        previous = -1
        for relative in (0.001, 0.005, 0.019, 0.02, 0.05, 0.099, 0.10, 0.5):
            severity = severity_for_relative(relative)
            assert order.index(severity) >= previous
            previous = order.index(severity)

    def test_an_understated_figure_is_still_a_finding(self):
        """A KPI that understates is as much a defect as one that overstates."""
        assert severity_for_relative(0.20) == "high"
