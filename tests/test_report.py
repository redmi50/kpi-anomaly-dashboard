"""The reports a person reads, and the CLI that writes them.

The markdown and the JSON are two views of one investigation, so these tests check
that they agree: a finding that appears in the JSON appears in the markdown with
the same numbers, and the certified figures printed in the report are the ones the
warehouse's own views hold. The CLI is exercised end to end into a temporary
directory, so the command in the README is the command under test.
"""

from __future__ import annotations

import json

import pytest

from kpi_audit.__main__ import DEFAULT_OUT, main
from kpi_audit.anomalies import ANOMALY_IDS
from kpi_audit.catalogue import ROLE_NAMES, TABLE_NAMES, VIEW_NAMES
from kpi_audit.report import render_json, render_markdown
from kpi_audit.warehouse import row_counts


@pytest.fixture
def markdown(report, connection):
    """The rendered markdown, produced once per test module."""
    return render_markdown(report, connection)


class TestMarkdownReport:
    def test_the_report_opens_with_a_ledger(self, markdown):
        assert markdown.startswith("# KPI anomaly investigation")
        assert "## KPI ledger" in markdown

    def test_every_scenario_appears_in_the_ledger(self, markdown):
        for scenario_id in ANOMALY_IDS:
            assert scenario_id in markdown

    def test_the_ledger_states_the_measured_discrepancy(self, report, markdown):
        investigation = report.by_id()["window_first_month_dropped"]
        rendered = f"{investigation.measurement.relative * 100:.2f} percent"
        assert rendered in markdown

    def test_the_ledger_reports_the_control_as_agreed(self, markdown):
        assert "| control_certified | control | net_revenue | 881,881.07 | 881,881.07 " in markdown
        assert "1 raised nothing" in markdown

    def test_every_finding_has_its_own_section(self, report, markdown):
        for finding in report.sorted_findings:
            assert f"### {finding.scenario_id} ({finding.severity})" in markdown

    def test_the_findings_summary_is_ordered_most_severe_first(self, report, markdown):
        summary = markdown.split("## Findings")[1].split("###")[0]
        positions = [summary.index(finding.scenario_id) for finding in report.sorted_findings]
        assert positions == sorted(positions)

    def test_a_finding_carries_its_cause_and_correction(self, report, markdown):
        finding = report.by_id()["filter_returns_counted"].finding
        assert f"Root cause: {finding.cause}" in markdown
        assert f"Correction: {finding.correction}" in markdown

    def test_a_finding_shows_the_query_that_produced_the_number(self, report, markdown):
        finding = report.by_id()["window_final_day_lost"].finding
        assert "```sql" in markdown
        assert finding.evidence["reporting_sql"].splitlines()[0] in markdown

    def test_a_finding_states_the_rows_it_wrongly_read(self, report, markdown):
        investigation = report.by_id()["join_unassigned_region_dropped"]
        assert f"| rows wrongly excluded | {len(investigation.measurement.wrongly_excluded)} |" in markdown

    def test_the_certified_figures_are_read_from_the_views(self, connection, markdown):
        for name in VIEW_NAMES:
            assert f"### {name}" in markdown
            rows = connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            assert f"{rows} rows." in markdown

    def test_the_certified_monthly_figure_is_the_view_s_figure(self, connection, markdown):
        """The report prints the view's number rather than a copy of it."""
        total = connection.execute(
            "SELECT ROUND(SUM(net_revenue), 2) FROM v_revenue_by_month"
        ).fetchone()[0]
        assert f"{total:,.2f}" in markdown

    def test_the_access_section_probes_every_role(self, markdown):
        assert "## Access" in markdown
        for role_name in ROLE_NAMES:
            assert role_name in markdown

    def test_the_access_section_names_the_refused_column(self, markdown):
        assert "dim_product.unit_price" in markdown

    def test_the_limitations_section_is_stated(self, markdown):
        assert "## Limitations" in markdown

    def test_the_limitations_include_the_row_counts(self, connection, markdown):
        for name, count in row_counts(connection).items():
            assert f"| {name} | {count} |" in markdown

    def test_the_report_has_no_em_or_en_dashes(self, markdown):
        assert "\u2014" not in markdown
        assert "\u2013" not in markdown

    def test_the_access_section_can_be_left_out(self, report, connection):
        text = render_markdown(report, connection, include_grants=False)
        assert "## Access" not in text
        assert "## KPI ledger" in text


class TestJsonReport:
    def test_the_payload_carries_the_totals(self, report, connection):
        payload = json.loads(render_json(report, connection))
        assert payload["totals"]["scenarios"] == len(ANOMALY_IDS)
        assert payload["totals"]["passed"] == 1
        assert payload["totals"]["by_severity"] == report.by_severity

    def test_every_scenario_appears_once(self, report, connection):
        payload = json.loads(render_json(report, connection))
        ids = [entry["scenario_id"] for entry in payload["scenarios"]]
        assert ids == list(ANOMALY_IDS)

    def test_the_evidence_travels_with_the_finding(self, report, connection):
        payload = json.loads(render_json(report, connection))
        entries = {entry["scenario_id"]: entry for entry in payload["scenarios"]}
        evidence = entries["filter_returns_counted"]["finding"]["evidence"]
        assert evidence["direction"] == "overstated"
        assert evidence["rows_wrongly_included"] > 0
        assert "SELECT" in evidence["reporting_sql"]

    def test_the_certified_figures_travel_alongside(self, connection, report):
        payload = json.loads(render_json(report, connection))
        assert set(payload["certified_figures"]) == set(VIEW_NAMES)
        assert len(payload["certified_figures"]["v_revenue_by_month"]) == 6

    def test_the_grants_matrix_travels_alongside(self, connection, report):
        payload = json.loads(render_json(report, connection))
        assert [entry["role"] for entry in payload["access"]] == list(ROLE_NAMES)

    def test_the_row_counts_travel_alongside(self, connection, report):
        payload = json.loads(render_json(report, connection))
        assert payload["warehouse"] == row_counts(connection)

    def test_the_json_is_serialisable_after_a_round_trip(self, connection, report):
        payload = json.loads(render_json(report, connection))
        assert json.loads(json.dumps(payload)) == payload


class TestCommandLine:
    def test_list_scenarios_names_every_scenario(self, capsys):
        assert main(["list-scenarios"]) == 0
        out = capsys.readouterr().out
        for scenario_id in ANOMALY_IDS:
            assert scenario_id in out

    def test_list_scenarios_states_the_clause_that_changed(self, capsys):
        main(["list-scenarios"])
        out = capsys.readouterr().out
        assert "changed:  window" in out

    def test_list_roles_probes_every_role(self, capsys):
        assert main(["list-roles"]) == 0
        out = capsys.readouterr().out
        for role_name in ROLE_NAMES:
            assert role_name in out
        assert "refused: dim_product.unit_price" in out

    def test_list_tables_prints_the_grain(self, capsys):
        assert main(["list-tables"]) == 0
        out = capsys.readouterr().out
        for name in TABLE_NAMES:
            assert name in out
        assert "grain: one row per order line per load" in out

    def test_list_views_prints_the_corrected_figures(self, capsys):
        assert main(["list-views"]) == 0
        out = capsys.readouterr().out
        for name in VIEW_NAMES:
            assert name in out

    def test_run_writes_both_reports(self, tmp_path):
        assert main(["run", "--out", str(tmp_path)]) == 0
        markdown = (tmp_path / "investigation.md").read_text(encoding="utf-8")
        payload = json.loads((tmp_path / "investigation.json").read_text(encoding="utf-8"))
        assert "## KPI ledger" in markdown
        assert payload["totals"]["scenarios"] == len(ANOMALY_IDS)

    def test_run_prints_a_row_per_scenario(self, tmp_path, capsys):
        main(["run", "--out", str(tmp_path)])
        out = capsys.readouterr().out
        for scenario_id in ANOMALY_IDS:
            assert scenario_id in out
        assert f"Scenarios investigated: {len(ANOMALY_IDS)}" in out
        assert "Role: analyst" in out

    def test_run_accepts_a_subset(self, tmp_path):
        assert main(
            ["run", "--scenarios", "control_certified", "--out", str(tmp_path)]
        ) == 0
        payload = json.loads((tmp_path / "investigation.json").read_text(encoding="utf-8"))
        assert [entry["scenario_id"] for entry in payload["scenarios"]] == [
            "control_certified"
        ]
        assert payload["totals"]["weight"] == 0

    def test_run_accepts_a_role(self, tmp_path):
        assert main(
            ["run", "--role", "auditor", "--out", str(tmp_path)]
        ) == 0
        payload = json.loads((tmp_path / "investigation.json").read_text(encoding="utf-8"))
        assert all(entry["access"]["role"] == "auditor" for entry in payload["scenarios"])

    def test_run_rejects_an_unknown_scenario_without_writing(self, tmp_path, capsys):
        assert main(
            ["run", "--scenarios", "nope", "--out", str(tmp_path)]
        ) == 2
        assert "Unknown scenarios" in capsys.readouterr().err
        assert not (tmp_path / "investigation.md").exists()

    def test_run_rejects_an_unknown_role_without_writing(self, tmp_path, capsys):
        assert main(["run", "--role", "nope", "--out", str(tmp_path)]) == 2
        assert "Unknown role" in capsys.readouterr().err
        assert not (tmp_path / "investigation.md").exists()

    def test_the_default_output_directory_is_documented(self):
        assert DEFAULT_OUT == "reports"

    def test_report_re_renders_markdown_from_saved_json(self, tmp_path, capsys):
        main(["run", "--out", str(tmp_path)])
        capsys.readouterr()
        target = tmp_path / "again.md"
        assert main(
            [
                "report",
                "--investigation",
                str(tmp_path / "investigation.json"),
                "--out",
                str(target),
            ]
        ) == 0
        text = target.read_text(encoding="utf-8")
        assert "## Summary" in text
        assert "filter_returns_counted" in text

    def test_report_prints_to_stdout_without_an_output_path(self, tmp_path, capsys):
        main(["run", "--out", str(tmp_path)])
        capsys.readouterr()
        assert main(
            ["report", "--investigation", str(tmp_path / "investigation.json")]
        ) == 0
        assert "## Summary" in capsys.readouterr().out

    def test_report_rejects_a_missing_file(self, tmp_path, capsys):
        assert main(
            ["report", "--investigation", str(tmp_path / "nope.json")]
        ) == 2
        assert "not found" in capsys.readouterr().err
