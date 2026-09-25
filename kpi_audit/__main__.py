"""Command line interface.

    python -m kpi_audit list-scenarios
    python -m kpi_audit list-roles
    python -m kpi_audit list-tables
    python -m kpi_audit run
    python -m kpi_audit run --scenarios window_final_day_lost,control_certified
    python -m kpi_audit report --scenarios reports/investigation.json
    python -m kpi_audit dashboard --out dashboard

Every command builds the warehouse from the fixed seed, so a command run twice
produces the same numbers and a reader can check any figure in the README by
running the command that produced it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .access import grants_report
from .anomalies import ANOMALIES, ANOMALY_IDS
from .catalogue import ROLES, ROLE_NAMES, TABLES, VIEW_NAMES
from .dashboard import build_workbook, export_model
from .investigation import investigate_all
from .report import render_json, render_markdown
from .warehouse import SEED, build, row_counts

DEFAULT_OUT = "reports"


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="kpi_audit",
        description=(
            "Investigate reported KPI figures against the agreed definition, in a "
            "simulated warehouse with enforced roles."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-scenarios", help="Print every scenario.")
    subparsers.add_parser("list-roles", help="Print every role and its grant.")
    subparsers.add_parser("list-tables", help="Print the warehouse catalogue.")
    subparsers.add_parser("list-views", help="Print the certified views.")

    run_parser = subparsers.add_parser("run", help="Investigate and write reports.")
    run_parser.add_argument(
        "--scenarios",
        default=None,
        help=(
            "Comma separated scenario ids. Defaults to every scenario. "
            f"Available: {', '.join(ANOMALY_IDS)}."
        ),
    )
    run_parser.add_argument(
        "--role",
        default="analyst",
        help=f"Role to run under. Available: {', '.join(ROLE_NAMES)}.",
    )
    run_parser.add_argument("--out", default=DEFAULT_OUT, help="Output directory.")

    report_parser = subparsers.add_parser(
        "report", help="Re-render the markdown from a saved investigation."
    )
    report_parser.add_argument("--investigation", required=True, help="Path to investigation.json.")
    report_parser.add_argument("--out", default=None, help="Where to write the markdown.")

    dashboard_parser = subparsers.add_parser(
        "dashboard", help="Build the dashboard workbook and the Power BI model."
    )
    dashboard_parser.add_argument(
        "--scenarios",
        default=None,
        help="Comma separated scenario ids, or every scenario by default.",
    )
    dashboard_parser.add_argument(
        "--out", default="dashboard", help="Output directory."
    )

    return parser


def cmd_list_scenarios() -> int:
    """Print every scenario and what it departs from."""
    print(f"{len(ANOMALIES)} scenarios, {len(ANOMALY_IDS)} ids\n")
    for scenario in ANOMALIES:
        print(f"{scenario.scenario_id} ({scenario.family})")
        print(f"  {scenario.title}")
        print(f"  question: {scenario.question}")
        print(f"  agreed:   {scenario.agreed}")
        print(f"  ran:      {scenario.definition}")
        if scenario.changed_clause:
            print(f"  changed:  {scenario.changed_clause}")
        for note in scenario.notes:
            print(f"  note: {note}")
        print()
    return 0


def cmd_list_roles() -> int:
    """Print every role, with its grant checked against the real warehouse."""
    connection = build()
    try:
        print(f"{len(ROLES)} roles\n")
        for entry in grants_report(connection):
            print(f"{entry['role']}: {entry['description']}")
            print(
                f"  columns readable: {entry['columns_allowed']} of "
                f"{entry['columns_total']}"
            )
            refused = [
                f"{cell['table']}.{cell['column']}"
                for cell in entry["cells"]
                if not cell["allowed"]
            ]
            print(f"  refused: {', '.join(refused) if refused else 'none'}")
            print()
    finally:
        connection.close()
    return 0


def cmd_list_tables() -> int:
    """Print the catalogue: table, grain, columns."""
    print(f"{len(TABLES)} tables\n")
    for table in TABLES.values():
        print(f"{table.name} ({table.kind})")
        print(f"  {table.description}")
        print(f"  grain: {table.grain}")
        for column in table.columns:
            flags = []
            if column.primary_key:
                flags.append("primary key")
            if not column.nullable:
                flags.append("not null")
            if column.references:
                flags.append(f"references {column.references}")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            print(f"  {column.name} {column.type}{suffix}")
            print(f"      {column.description}")
        print()
    return 0


def cmd_list_views() -> int:
    """Print the certified views and the corrected figures they hold."""
    connection = build()
    try:
        print(f"{len(VIEW_NAMES)} certified views, warehouse built from seed {SEED}\n")
        for name in VIEW_NAMES:
            print(f"{name}")
            for row in connection.execute(f"SELECT * FROM {name}"):
                print(f"  {dict(row)}")
            print()
    finally:
        connection.close()
    return 0


def cmd_run(args) -> int:
    """Investigate the selected scenarios and write the markdown and JSON reports."""
    scenario_ids = None
    if args.scenarios:
        scenario_ids = tuple(
            name.strip() for name in args.scenarios.split(",") if name.strip()
        )
        unknown = [name for name in scenario_ids if name not in ANOMALY_IDS]
        if unknown:
            print(f"Unknown scenarios: {', '.join(unknown)}", file=sys.stderr)
            return 2
    if args.role not in ROLE_NAMES:
        print(f"Unknown role: {args.role}", file=sys.stderr)
        return 2

    connection = build()
    try:
        report = investigate_all(scenario_ids, connection, role_name=args.role)
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = out_dir / "investigation.md"
        json_path = out_dir / "investigation.json"
        markdown_path.write_text(
            render_markdown(report, connection), encoding="utf-8"
        )
        json_path.write_text(render_json(report, connection), encoding="utf-8")

        print(f"Scenarios investigated: {len(report.investigations)}")
        print(f"Role: {args.role}")
        print(f"Warehouse: {sum(row_counts(connection).values())} rows")
        print()
        print(
            f"{'scenario':38s} {'family':12s} {'severity':9s} "
            f"{'reported':>14s} {'corrected':>14s} {'relative':>9s}"
        )
        for investigation in report.investigations:
            measurement = investigation.measurement
            severity = (
                "none" if investigation.finding is None else investigation.finding.severity
            )
            print(
                f"{investigation.scenario_id:38s} "
                f"{investigation.scenario.family:12s} {severity:9s} "
                f"{_short(measurement.reported, investigation.scenario.unit):>14s} "
                f"{_short(measurement.agreed, investigation.scenario.unit):>14s} "
                f"{measurement.relative * 100:8.2f}%"
            )
        print()
        counts = report.by_severity
        print(
            f"{len(report.passed)} raised nothing, {counts['high']} high, "
            f"{counts['medium']} medium, {counts['low']} low, "
            f"weight {report.weight}"
        )
        print()
        print(f"Wrote {markdown_path}")
        print(f"Wrote {json_path}")
    finally:
        connection.close()
    return 0


def cmd_report(args) -> int:
    """Re-render the markdown from a saved investigation."""
    path = Path(args.investigation)
    if not path.is_file():
        print(f"Investigation file not found: {path}", file=sys.stderr)
        return 2

    payload = json.loads(path.read_text(encoding="utf-8"))
    rendered = _render_saved(payload)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(rendered)
    return 0


def cmd_dashboard(args) -> int:
    """Build the workbook and the Power BI model export."""
    scenario_ids = None
    if args.scenarios:
        scenario_ids = tuple(
            name.strip() for name in args.scenarios.split(",") if name.strip()
        )
        unknown = [name for name in scenario_ids if name not in ANOMALY_IDS]
        if unknown:
            print(f"Unknown scenarios: {', '.join(unknown)}", file=sys.stderr)
            return 2

    connection = build()
    try:
        report = investigate_all(scenario_ids, connection)
        out_dir = Path(args.out)
        workbook_path = build_workbook(report, connection, out_dir / "kpi_dashboard.xlsx")
        model_files = export_model(connection, out_dir / "model")
    finally:
        connection.close()

    print(f"Wrote {workbook_path}")
    print(f"Wrote {len(model_files)} model files to {out_dir / 'model'}")
    for path in model_files:
        print(f"  {path.name}")
    return 0


def _render_saved(payload: dict) -> str:
    """Render a saved investigation without re-running any scenario."""
    totals = payload.get("totals", {})
    lines = ["# KPI anomaly investigation", ""]
    lines.append("## Summary")
    lines.append("")
    lines.append("| Scenario | Family | Severity | Relative discrepancy |")
    lines.append("| --- | --- | --- | --- |")
    for entry in payload.get("scenarios", []):
        finding = entry.get("finding")
        lines.append(
            f"| {entry['scenario_id']} | {entry['family']} "
            f"| {'none' if finding is None else finding['severity']} "
            f"| {entry['measurement']['relative'] * 100:.2f} percent |"
        )
    lines.append("")
    lines.append(
        f"{totals.get('scenarios', 0)} scenarios investigated, "
        f"{totals.get('passed', 0)} raised nothing, "
        f"{totals.get('weight', 0)} total severity weight."
    )
    lines.append("")
    for entry in payload.get("scenarios", []):
        finding = entry.get("finding")
        if finding is None:
            continue
        lines.append(f"## {finding['scenario_id']} ({finding['severity']})")
        lines.append("")
        lines.append(f"Question: {finding['question']}")
        lines.append("")
        lines.append(f"Reported: {finding['claim']}")
        lines.append("")
        lines.append(f"Root cause: {finding['cause']}")
        lines.append("")
        lines.append(f"Correction: {finding['correction']}")
        lines.append("")
    return "\n".join(lines)


def _short(value: object, unit: str) -> str:
    """Render one measured value compactly for a terminal row."""
    if value is None:
        return "not produced"
    if unit == "currency":
        return f"{float(value):,.2f}"
    return f"{int(value):,}"


def main(argv=None) -> int:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "list-scenarios":
        return cmd_list_scenarios()
    if args.command == "list-roles":
        return cmd_list_roles()
    if args.command == "list-tables":
        return cmd_list_tables()
    if args.command == "list-views":
        return cmd_list_views()
    if args.command == "run":
        return cmd_run(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "dashboard":
        return cmd_dashboard(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
