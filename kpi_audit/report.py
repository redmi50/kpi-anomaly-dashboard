"""Turning investigations into a review a person can read.

The markdown report is ordered the way the dashboard is: the KPI ledger first,
because that is what a reader looks at, then the root cause behind each figure
that moved, then the certified figures a report can bind to rather than a number
that has been copied into a slide. The JSON report carries the same content with
the full evidence dictionaries, so a script downstream does not have to parse
prose.

One thing is deliberately absent from both. No severity, cause or correction is
written here. Every one of them arrives from the investigation, which computed
them from the warehouse. A report that restated them would be a second place for
a finding to be wrong.
"""

from __future__ import annotations

import json

from .access import grants_report
from .catalogue import ROLES, VIEW_NAMES
from .investigation import InvestigationReport
from .warehouse import row_counts, rows_as_dicts


def render_markdown(
    report: InvestigationReport,
    connection,
    *,
    include_grants: bool = True,
) -> str:
    """Render the investigation as one markdown document.

    The connection is required because the certified figures and the grants
    matrix are read from the warehouse rather than restated, so the report and
    the warehouse cannot disagree about what the corrected numbers are.
    """
    lines: list[str] = ["# KPI anomaly investigation", ""]
    lines.append(
        "Every figure below was produced by running the warehouse's own certified "
        "views and by measuring each scenario against the definition it should have "
        "run. Severity comes from the size of the measured discrepancy, never from "
        "a label applied by hand."
    )
    lines.append("")

    _kpi_ledger(lines, report)
    _corrected_figures(lines, connection)
    _findings(lines, report)
    if include_grants:
        _access(lines, connection)
    _limitations(lines, connection)
    return "\n".join(lines)


def _kpi_ledger(lines: list[str], report: InvestigationReport) -> None:
    """The dashboard view: every figure, what it was, and what it should be."""
    lines.append("## KPI ledger")
    lines.append("")
    lines.append(
        "Each row is one reported figure. The reported column is what the scenario "
        "produced, the corrected column is the agreed definition's answer to the "
        "same question, and the discrepancy is measured against the corrected "
        "column rather than against the reported one."
    )
    lines.append("")
    lines.append(
        "| Scenario | Family | Measure | Reported | Corrected | Discrepancy | Severity |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for investigation in report.investigations:
        measurement = investigation.measurement
        scenario = investigation.scenario
        severity = "" if investigation.finding is None else investigation.finding.severity
        lines.append(
            f"| {scenario.scenario_id} | {scenario.family} | {scenario.definition.measure} "
            f"| {_number(measurement.reported, scenario.unit)} "
            f"| {_number(measurement.agreed, scenario.unit)} "
            f"| {measurement.relative * 100:.2f} percent | {severity or 'none'} |"
        )
    lines.append("")
    counts = report.by_severity
    lines.append(
        f"{len(report.investigations)} scenarios run, {len(report.passed)} raised nothing, "
        f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low, "
        f"total severity weight {report.weight}."
    )
    lines.append("")


def _corrected_figures(lines: list[str], connection) -> None:
    """The certified figures, read from the views rather than restated."""
    lines.append("## Certified figures")
    lines.append("")
    lines.append(
        "These are the corrected definitions shipped as views in the warehouse. A "
        "report binds to these objects rather than to a number copied out of the "
        "dashboard, so a definition change reaches the report the next time it "
        "refreshes."
    )
    lines.append("")
    for name in VIEW_NAMES:
        rows = rows_as_dicts(connection, f"SELECT * FROM {name}")
        lines.append(f"### {name}")
        lines.append("")
        if not rows:
            lines.append("No rows.")
            lines.append("")
            continue
        headings = list(rows[0])
        lines.append("| " + " | ".join(headings) + " |")
        lines.append("| " + " | ".join("---" for _ in headings) + " |")
        for row in rows:
            lines.append(
                "| " + " | ".join(_cell(row[heading]) for heading in headings) + " |"
            )
        lines.append("")
        lines.append(f"{len(rows)} rows.")
        lines.append("")


def _findings(lines: list[str], report: InvestigationReport) -> None:
    """One section per finding, most severe first."""
    lines.append("## Findings")
    lines.append("")

    if not report.sorted_findings:
        lines.append("No findings. Every reported figure agrees with its agreed definition.")
        lines.append("")
        return

    lines.append("| Severity | Scenario | Changed clause | Cause in one line |")
    lines.append("| --- | --- | --- | --- |")
    for finding in report.sorted_findings:
        lines.append(
            f"| {finding.severity} | {finding.scenario_id} "
            f"| {finding.evidence.get('changed_clause', '')} | {finding.title} |"
        )
    lines.append("")

    by_id = report.by_id()
    for finding in report.sorted_findings:
        investigation = by_id[finding.scenario_id]
        measurement = investigation.measurement
        lines.append(f"### {finding.scenario_id} ({finding.severity})")
        lines.append("")
        lines.append(f"Question: {finding.question}")
        lines.append("")
        lines.append(f"Reported: {finding.claim}")
        lines.append("")
        lines.append(f"Root cause: {finding.cause}")
        lines.append("")
        lines.append(f"Correction: {finding.correction}")
        lines.append("")
        lines.append(
            "| Quantity | Value |"
        )
        lines.append("| --- | --- |")
        lines.append(f"| reported | {_number(measurement.reported, investigation.scenario.unit)} |")
        lines.append(f"| corrected | {_number(measurement.agreed, investigation.scenario.unit)} |")
        lines.append(f"| direction | {measurement.direction} |")
        lines.append(f"| relative discrepancy | {measurement.relative * 100:.2f} percent |")
        lines.append(f"| rows the reported figure counted | {len(measurement.reported_rows)} |")
        lines.append(f"| rows it should have counted | {len(measurement.agreed_rows)} |")
        lines.append(f"| rows wrongly included | {len(measurement.wrongly_included)} |")
        lines.append(f"| rows wrongly excluded | {len(measurement.wrongly_excluded)} |")
        lines.append(
            f"| tables read | {', '.join(finding.evidence.get('tables_read', []))} |"
        )
        lines.append("")

        included = measurement.wrongly_included[:8]
        excluded = measurement.wrongly_excluded[:8]
        if included or excluded:
            lines.append("Sample of the rows behind the discrepancy, by line identifier:")
            lines.append("")
            if included:
                lines.append(f"- wrongly included: {_list(included)}")
            if excluded:
                lines.append(f"- wrongly excluded: {_list(excluded)}")
            lines.append("")

        for note in investigation.scenario.notes:
            lines.append(f"Note: {note}")
            lines.append("")

        lines.append("The query that was run:")
        lines.append("")
        lines.append("```sql")
        lines.append(str(finding.evidence.get("reporting_sql", "")).strip())
        lines.append("```")
        lines.append("")


def _access(lines: list[str], connection) -> None:
    """The grants matrix, produced by probing the engine rather than the policy."""
    lines.append("## Access")
    lines.append("")
    lines.append(
        "Each cell is the answer the engine gave when the role was asked for that "
        "column, rather than a restatement of the policy. A grant that is written in "
        "a table and not enforced is not a grant."
    )
    lines.append("")
    lines.append("| Role | Columns readable | Columns total | Description |")
    lines.append("| --- | --- | --- | --- |")
    for entry in grants_report(connection):
        lines.append(
            f"| {entry['role']} | {entry['columns_allowed']} | {entry['columns_total']} "
            f"| {entry['description']} |"
        )
    lines.append("")
    lines.append("| Role | Refused columns |")
    lines.append("| --- | --- |")
    for entry in grants_report(connection):
        refused = [
            f"{cell['table']}.{cell['column']}"
            for cell in entry["cells"]
            if not cell["allowed"]
        ]
        lines.append(
            f"| {entry['role']} | {_list(refused) if refused else 'none'} |"
        )
    lines.append("")


def _limitations(lines: list[str], connection) -> None:
    """What the report does not cover, stated rather than left to discovery."""
    lines.append("## Limitations")
    lines.append("")
    lines.append(
        "- The warehouse is generated, not captured. It is built to contain the "
        "defect classes this project investigates, and a defect class that is not "
        "in it is not covered here."
    )
    lines.append(
        "- Severity is a function of the size of the discrepancy against the agreed "
        "figure. It says how far the number moved, not what the movement cost, and "
        "a small discrepancy in a figure used to decide something is worth more "
        "attention than this ordering implies."
    )
    lines.append(
        "- The row level reconciliation is limited to the warehouse. A discrepancy "
        "against a downstream system, such as a finance ledger, would need that "
        "system's own figures and is outside this audit."
    )
    lines.append(
        "- Permissions are enforced on one connection at a time through an SQLite "
        "authorizer. That is a real engine check rather than a documented policy, "
        "but it is the database's check in this process, not a substitute for "
        "server side authentication and audit logging."
    )
    lines.append("")
    lines.append("| Table | Rows |")
    lines.append("| --- | --- |")
    for name, count in row_counts(connection).items():
        lines.append(f"| {name} | {count} |")
    lines.append("")
    lines.append(
        f"{len(ROLES)} roles defined: {', '.join(ROLES)}."
    )
    lines.append("")
    lines.append(
        "The warehouse is generated from a fixed seed, so every figure here "
        "reproduces on any machine that runs the same command."
    )
    lines.append("")


def render_json(report: InvestigationReport, connection) -> str:
    """Render the investigation as JSON, with the certified figures alongside."""
    payload = report.as_dict()
    payload["certified_figures"] = {
        name: rows_as_dicts(connection, f"SELECT * FROM {name}") for name in VIEW_NAMES
    }
    payload["access"] = grants_report(connection)
    payload["warehouse"] = row_counts(connection)
    return json.dumps(payload, indent=2, default=str, sort_keys=False)


def _number(value: object, unit: str) -> str:
    """Render a measured value with the unit it is measured in."""
    if value is None:
        return "not produced"
    if unit == "currency":
        return f"{float(value):,.2f}"
    return f"{int(value):,}"


def _cell(value: object) -> str:
    """Render one table cell without hiding its structure."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _list(items: list) -> str:
    """Render a list of identifiers as a comma separated clause."""
    if not items:
        return "none"
    rendered = ", ".join(str(item) for item in items)
    return rendered if len(items) <= 8 else f"{rendered} and {len(items) - 8} more"
