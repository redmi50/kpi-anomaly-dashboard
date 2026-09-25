"""The investigation: what each scenario measured, and why.

Every claim this project makes is a claim about a measured number, so these tests
assert the numbers rather than the prose. Two groups matter most. The control must
raise nothing, because a set of checks that fire on everything looks the same as a
set that works. And each defective scenario must raise a finding whose severity
comes from the measured relative discrepancy, so that severity cannot be
overridden by editing a label.
"""

from __future__ import annotations

import pytest

from kpi_audit import build, investigate, investigate_all
from kpi_audit.anomalies import (
    ANOMALY_IDS,
    Definition,
    load_scenario,
    severity_for_relative,
)
from kpi_audit.findings import SEVERITY_WEIGHTS
from kpi_audit.investigation import Measurement
from kpi_audit.warehouse import shadowed_names, temp_objects

# The certified headline figure for the half year, asserted as a literal so that a
# change to the generator has to be acknowledged rather than absorbed.
CERTIFIED_REVENUE = 881881.07
CERTIFIED_ORDERS = 1353


class TestMeasurement:
    def test_the_delta_is_reported_minus_agreed(self):
        measurement = Measurement(reported=110.0, agreed=100.0)
        assert measurement.delta == 10.0

    def test_a_missing_value_has_no_delta(self):
        assert Measurement(reported=None, agreed=100.0).delta == 0.0
        assert Measurement(reported=100.0, agreed=None).delta == 0.0

    def test_the_relative_discrepancy_is_measured_against_the_agreed_value(self):
        assert Measurement(reported=110.0, agreed=100.0).relative == pytest.approx(0.10)
        assert Measurement(reported=90.0, agreed=100.0).relative == pytest.approx(0.10)

    def test_a_zero_agreed_value_has_no_relative_discrepancy(self):
        assert Measurement(reported=10.0, agreed=0.0).relative == 0.0

    def test_the_direction_names_which_way_the_figure_moved(self):
        assert Measurement(reported=110.0, agreed=100.0).direction == "overstated"
        assert Measurement(reported=90.0, agreed=100.0).direction == "understated"
        assert Measurement(reported=100.0, agreed=100.0).direction == "agrees"

    def test_wrongly_included_is_the_difference_between_the_two_row_sets(self):
        measurement = Measurement(
            reported=1.0,
            agreed=1.0,
            reported_rows=[1, 2, 3],
            agreed_rows=[2, 3, 4],
        )
        assert measurement.wrongly_included == [1]
        assert measurement.wrongly_excluded == [4]

    def test_the_two_row_sets_cannot_overlap(self):
        measurement = Measurement(
            reported=1.0, agreed=1.0, reported_rows=[1, 2], agreed_rows=[1, 2]
        )
        assert measurement.wrongly_included == []
        assert measurement.wrongly_excluded == []


class TestControl:
    def test_the_control_reported_the_certified_figure(self, control):
        assert control.measurement.reported == pytest.approx(CERTIFIED_REVENUE)
        assert control.measurement.agreed == pytest.approx(CERTIFIED_REVENUE)

    def test_the_control_produced_the_same_rows_twice(self, control):
        assert control.measurement.wrongly_included == []
        assert control.measurement.wrongly_excluded == []

    def test_the_control_raises_no_finding(self, control):
        assert control.passed
        assert control.finding is None
        assert control.weight == 0

    def test_the_control_ran_under_a_role(self, control):
        assert control.access["role"] == "analyst"

    def test_the_control_read_the_fact_table(self, control):
        assert "fact_order_lines" in control.access["tables_read"]

    def test_the_control_is_the_only_scenario_that_passes(self, report):
        assert report.passed == ["control_certified"]


class TestEveryScenario:
    def test_every_scenario_was_investigated(self, report):
        assert [inv.scenario_id for inv in report.investigations] == list(ANOMALY_IDS)

    def test_every_scenario_produced_both_values(self, report):
        for investigation in report.investigations:
            assert investigation.measurement.reported is not None, investigation.scenario_id
            assert investigation.measurement.agreed is not None, investigation.scenario_id

    def test_no_scenario_was_refused_by_its_role(self, report):
        """The role has to be able to run the definitions, or nothing is measured."""
        for investigation in report.investigations:
            assert "denied_reason" not in investigation.access, investigation.scenario_id

    @pytest.mark.parametrize("scenario_id", ANOMALY_IDS)
    def test_severity_comes_from_the_measured_relative_discrepancy(
        self, investigations, scenario_id
    ):
        investigation = investigations[scenario_id]
        expected = severity_for_relative(investigation.measurement.relative)
        if expected is None:
            assert investigation.finding is None
        else:
            assert investigation.finding is not None
            assert investigation.finding.severity == expected

    @pytest.mark.parametrize("scenario_id", ANOMALY_IDS)
    def test_a_finding_carries_the_measurement_it_came_from(
        self, investigations, scenario_id
    ):
        investigation = investigations[scenario_id]
        if investigation.finding is None:
            pytest.skip("the control raises no finding")
        evidence = investigation.finding.evidence
        assert evidence["reported"] == investigation.measurement.reported
        assert evidence["agreed"] == investigation.measurement.agreed
        assert evidence["relative"] == pytest.approx(
            investigation.measurement.relative, abs=1e-6
        )

    @pytest.mark.parametrize("scenario_id", ANOMALY_IDS)
    def test_a_finding_states_the_clause_that_changed(self, investigations, scenario_id):
        investigation = investigations[scenario_id]
        if investigation.finding is None:
            pytest.skip("the control raises no finding")
        assert investigation.finding.evidence["changed_clause"] == (
            investigation.scenario.changed_clause
        )

    @pytest.mark.parametrize("scenario_id", ANOMALY_IDS)
    def test_a_finding_shows_the_query_that_produced_the_number(
        self, investigations, scenario_id
    ):
        investigation = investigations[scenario_id]
        if investigation.finding is None:
            pytest.skip("the control raises no finding")
        assert "SELECT" in investigation.finding.evidence["reporting_sql"]
        assert "SELECT" in investigation.finding.evidence["agreed_sql"]

    @pytest.mark.parametrize("scenario_id", ANOMALY_IDS)
    def test_the_two_queries_in_a_finding_are_different(
        self, investigations, scenario_id
    ):
        investigation = investigations[scenario_id]
        if investigation.finding is None:
            pytest.skip("the control raises no finding")
        assert (
            investigation.finding.evidence["reporting_sql"]
            != investigation.finding.evidence["agreed_sql"]
        )

    @pytest.mark.parametrize("scenario_id", ANOMALY_IDS)
    def test_a_finding_shows_what_caused_it(self, investigations, scenario_id):
        """Every finding is attributable: to the rows read, or to the measure.

        Most scenarios read a different set of rows from the agreed definition,
        so the evidence names the rows wrongly included or excluded. The grain
        scenario is the exception: it reads exactly the same rows and aggregates
        them with a different measure, so the rows cannot explain it and the
        evidence has to be the measure instead. Asserting that a finding always
        has rows behind it would be asserting something untrue of one of them.
        """
        investigation = investigations[scenario_id]
        if investigation.finding is None:
            pytest.skip("the control raises no finding")
        evidence = investigation.finding.evidence
        changed_rows = (
            evidence["rows_wrongly_included"] + evidence["rows_wrongly_excluded"]
        )
        if changed_rows:
            assert evidence["rows_reported"] != evidence["rows_agreed"]
        else:
            assert evidence["changed_clause"] == "measure"
            assert evidence["measure"] != investigation.scenario.agreed.measure


class TestAggregation:
    def test_the_report_counts_every_scenario(self, report):
        assert len(report.investigations) == len(ANOMALY_IDS)

    def test_the_weight_is_the_sum_of_the_finding_weights(self, report):
        assert report.weight == sum(
            investigation.weight for investigation in report.investigations
        )

    def test_the_weight_uses_the_stated_severity_weights(self, report):
        expected = sum(
            SEVERITY_WEIGHTS[finding.severity] for finding in report.sorted_findings
        )
        assert report.weight == expected

    def test_the_severity_counts_add_up_to_the_findings(self, report):
        counts = report.by_severity
        assert sum(counts.values()) == len(report.sorted_findings)

    def test_every_severity_bucket_is_present_in_the_counts(self, report):
        assert set(report.by_severity) == set(SEVERITY_WEIGHTS)

    def test_findings_are_ordered_most_severe_first(self, report):
        weights = [SEVERITY_WEIGHTS[f.severity] for f in report.sorted_findings]
        assert weights == sorted(weights, reverse=True)

    def test_findings_of_equal_severity_are_ordered_by_scenario_id(self, report):
        highs = [
            f.scenario_id
            for f in report.sorted_findings
            if f.severity == "high"
        ]
        assert highs == sorted(highs)

    def test_the_report_can_be_looked_up_by_scenario_id(self, report):
        assert set(report.by_id()) == set(ANOMALY_IDS)

    def test_a_subset_can_be_investigated(self, connection):
        subset = investigate_all(
            ("control_certified", "window_final_day_lost"), connection
        )
        assert [inv.scenario_id for inv in subset.investigations] == [
            "control_certified",
            "window_final_day_lost",
        ]

    def test_a_subset_does_not_change_what_a_scenario_measures(self, report, connection):
        subset = investigate_all(("grain_replayed_batch_counted",), connection)
        assert (
            subset.investigations[0].measurement.reported
            == report.by_id()["grain_replayed_batch_counted"].measurement.reported
        )


class TestMeasuredFigures:
    def test_the_certified_order_count_is_a_distinct_count(self, report):
        investigation = report.by_id()["grain_rows_counted_as_orders"]
        assert investigation.measurement.agreed == CERTIFIED_ORDERS
        assert investigation.measurement.reported > investigation.measurement.agreed

    def test_the_row_count_scenario_reports_lines_not_orders(self, report):
        investigation = report.by_id()["grain_rows_counted_as_orders"]
        assert investigation.measurement.reported == len(
            investigation.measurement.reported_rows
        )

    def test_every_revenue_scenario_agrees_on_the_corrected_figure(self, report):
        """The corrected figure is one number, whatever the scenario did to it."""
        for investigation in report.investigations:
            if investigation.scenario.definition.measure != "net_revenue":
                continue
            assert investigation.measurement.agreed == pytest.approx(
                CERTIFIED_REVENUE
            ), investigation.scenario_id

    def test_the_replay_overstates_revenue(self, report):
        investigation = report.by_id()["grain_replayed_batch_counted"]
        assert investigation.measurement.direction == "overstated"
        assert investigation.measurement.wrongly_excluded == []
        assert investigation.measurement.wrongly_included

    def test_dropping_a_month_understates_revenue(self, report):
        investigation = report.by_id()["window_first_month_dropped"]
        assert investigation.measurement.direction == "understated"
        assert investigation.measurement.wrongly_included == []
        assert investigation.measurement.wrongly_excluded

    def test_the_final_day_defect_removes_only_the_final_day(self, report):
        investigation = report.by_id()["window_final_day_lost"]
        assert investigation.measurement.direction == "understated"
        assert investigation.measurement.wrongly_included == []
        assert investigation.measurement.wrongly_excluded

    def test_the_unknown_status_defect_removes_only_unknown_status_rows(self, report):
        investigation = report.by_id()["filter_unknown_status_dropped"]
        assert investigation.measurement.direction == "understated"
        assert investigation.measurement.wrongly_included == []
        assert investigation.measurement.wrongly_excluded

    def test_counting_returns_adds_only_returned_rows(self, report):
        investigation = report.by_id()["filter_returns_counted"]
        assert investigation.measurement.direction == "overstated"
        assert investigation.measurement.wrongly_excluded == []
        assert investigation.measurement.wrongly_included

    def test_an_inner_channel_join_removes_only_unmatched_channels(self, report):
        investigation = report.by_id()["join_unmapped_channel_dropped"]
        assert investigation.measurement.direction == "understated"
        assert investigation.measurement.wrongly_included == []
        assert investigation.measurement.wrongly_excluded

    def test_an_inner_region_join_removes_only_unassigned_regions(self, report):
        investigation = report.by_id()["join_unassigned_region_dropped"]
        assert investigation.measurement.direction == "understated"
        assert investigation.measurement.wrongly_included == []
        assert investigation.measurement.wrongly_excluded

    def test_the_shadow_reports_one_region_and_undercounts(self, report):
        investigation = report.by_id()["session_shadowed_table"]
        assert investigation.measurement.direction == "understated"
        assert investigation.measurement.relative > 0.5
        assert investigation.measurement.wrongly_included == []


class TestSeverityBands:
    def test_the_two_large_defects_are_high(self, report):
        counts = report.by_severity
        assert counts["high"] >= 4

    def test_a_low_band_finding_exists(self, report):
        assert report.by_severity["low"] >= 1

    def test_a_medium_band_finding_exists(self, report):
        assert report.by_severity["medium"] >= 1

    def test_the_final_day_defect_is_low_rather_than_high(self, report):
        investigation = report.by_id()["window_final_day_lost"]
        assert investigation.finding.severity == "low"

    def test_the_unmapped_channel_defect_is_low_rather_than_high(self, report):
        investigation = report.by_id()["join_unmapped_channel_dropped"]
        assert investigation.finding.severity == "low"

    def test_a_missing_month_is_high(self, report):
        assert report.by_id()["window_first_month_dropped"].finding.severity == "high"

    def test_counting_returns_is_high(self, report):
        assert report.by_id()["filter_returns_counted"].finding.severity == "high"

    def test_the_shadow_is_high(self, report):
        assert report.by_id()["session_shadowed_table"].finding.severity == "high"


class TestSessionState:
    def test_the_session_scenario_left_a_temporary_object(self, investigations):
        investigation = investigations["session_shadowed_table"]
        assert investigation.session_state
        assert ("view", "fact_order_lines") in investigation.session_state

    def test_the_session_scenario_reports_the_shadowed_name(self, investigations):
        assert investigations["session_shadowed_table"].shadowed == ["fact_order_lines"]

    def test_a_non_session_scenario_leaves_no_temporary_object(self, investigations):
        for scenario_id, investigation in investigations.items():
            if scenario_id == "session_shadowed_table":
                continue
            assert investigation.session_state == [], scenario_id

    def test_the_session_finding_carries_the_session_evidence(self, investigations):
        evidence = investigations["session_shadowed_table"].finding.evidence
        assert evidence["shadowed_names"] == ["fact_order_lines"]
        assert evidence["schema_qualified"] is False
        assert evidence["session_state"]

    def test_the_agreed_query_names_the_schema_and_the_reported_one_does_not(
        self, investigations
    ):
        evidence = investigations["session_shadowed_table"].finding.evidence
        assert "FROM main.fact_order_lines" in evidence["agreed_sql"]
        assert "FROM fact_order_lines" in evidence["reporting_sql"]
        assert "FROM main.fact_order_lines" not in evidence["reporting_sql"]

    def test_no_temporary_object_survives_the_audit(self, connection, report):
        """A shadow left behind would change what a later scenario reads."""
        assert temp_objects(connection) == []
        assert shadowed_names(connection) == []

    def test_a_session_scenario_does_not_poison_a_later_one(self, connection):
        """Running the shadow scenario first must not change the control.

        This is the defect the audit exists to find, so the audit must not
        inflict it on itself: the session is cleared between scenarios.
        """
        first = investigate(load_scenario("session_shadowed_table"), connection)
        second = investigate(load_scenario("control_certified"), connection)
        assert first.finding is not None
        assert second.passed
        assert second.measurement.reported == pytest.approx(CERTIFIED_REVENUE)


class TestDenial:
    def test_a_scenario_run_under_a_role_that_cannot_run_it_reports_why(self, connection):
        """A figure that cannot be produced should say so rather than be published.

        The contractor role may not read the fact table, so the definition cannot
        run at all. The investigation raises a finding whose cause is the refusal
        rather than reporting a number.
        """
        investigation = investigate(
            load_scenario("control_certified"), connection, role_name="contractor"
        )
        assert investigation.finding is not None
        assert investigation.finding.severity == "high"
        assert investigation.measurement.reported is None
        assert "could not be run under the role" in investigation.finding.cause

    def test_the_denial_is_recorded_in_the_access_log(self, connection):
        investigation = investigate(
            load_scenario("control_certified"), connection, role_name="contractor"
        )
        assert investigation.access["denied"]
        assert investigation.access["role"] == "contractor"

    def test_the_auditor_can_run_the_certified_definition(self, connection):
        investigation = investigate(
            load_scenario("control_certified"), connection, role_name="auditor"
        )
        assert investigation.passed
        assert investigation.measurement.reported == pytest.approx(CERTIFIED_REVENUE)


class TestAsDict:
    def test_an_investigation_serialises_its_measurement(self, investigations):
        payload = investigations["window_first_month_dropped"].as_dict()
        assert payload["measurement"]["reported"]
        assert payload["measurement"]["agreed"]
        assert payload["measurement"]["direction"] == "understated"
        assert payload["changed_clause"] == "window"

    def test_a_control_serialises_as_passed_with_no_finding(self, control):
        payload = control.as_dict()
        assert payload["passed"] is True
        assert payload["finding"] is None

    def test_the_report_serialises_its_totals(self, report):
        payload = report.as_dict()
        assert payload["totals"]["scenarios"] == len(ANOMALY_IDS)
        assert payload["totals"]["weight"] == report.weight
        assert payload["totals"]["by_severity"] == report.by_severity

    def test_every_scenario_appears_once_in_the_serialised_report(self, report):
        payload = report.as_dict()
        ids = [entry["scenario_id"] for entry in payload["scenarios"]]
        assert ids == list(ANOMALY_IDS)
