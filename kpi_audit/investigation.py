"""The investigation: run a scenario, measure what it did, and say why.

Every number in a finding is computed here from the warehouse, from the two
definitions, or from the session the query ran in. Nothing is asserted.

Four measurements are taken for every scenario.

* The reported value, from the definition that was run.
* The agreed value, from the definition that should have been run.
* The row set behind each of the two values, generated from the same clauses.
  Comparing the two sets yields the rows that were wrongly included and the rows
  that were wrongly excluded, which is what turns "the figure moved" into a cause.
* The session the query ran in, so a defect that lives in session state rather
  than in query text is found by inspecting the session instead of reading the
  query.

The session state is inspected before the scenario runs, not after, because the
scenario's own setup is what puts it there. A leftover view that was already in
the session is the defect; one created by the audit's own instrumentation would
be an artefact of measuring.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import warehouse
from .access import AccessDenied, Session
from .anomalies import (
    ANOMALIES,
    Definition,
    Scenario,
    build,
    build_rows,
    load_scenario,
    severity_for_relative,
)

# What one extra row of each kind is worth, in words. These are the clauses that
# decide whether a discrepancy is a ticket or a note, and they are stated rather
# than left to the reader.
FAMILY_LABELS = {
    "control": "control",
    "window": "time window",
    "filter": "filter",
    "join": "join",
    "grain": "grain",
    "session": "session state",
}


@dataclass
class Measurement:
    """Both values, and the rows that explain the difference between them."""

    reported: float | None
    agreed: float | None
    reported_rows: list[int] = field(default_factory=list)
    agreed_rows: list[int] = field(default_factory=list)

    @property
    def delta(self) -> float:
        """The reported value minus the agreed value."""
        if self.reported is None or self.agreed is None:
            return 0.0
        return self.reported - self.agreed

    @property
    def relative(self) -> float:
        """The size of the discrepancy as a fraction of the agreed value.

        The agreed value is the denominator because it is the number that was
        supposed to be reported. A scenario that answers a count question is
        therefore measured against the count it should have produced, rather than
        against the revenue figure, which is why the agreed definition travels
        with the scenario.
        """
        if self.agreed in (None, 0):
            return 0.0
        return abs(self.delta) / abs(self.agreed)

    @property
    def wrongly_included(self) -> list[int]:
        """Rows the reported definition counted and the agreed one did not."""
        agreed = set(self.agreed_rows)
        return [line for line in self.reported_rows if line not in agreed]

    @property
    def wrongly_excluded(self) -> list[int]:
        """Rows the agreed definition counted and the reported one did not."""
        reported = set(self.reported_rows)
        return [line for line in self.agreed_rows if line not in reported]

    @property
    def direction(self) -> str:
        """Which way the reported figure moved."""
        if self.delta > 0:
            return "overstated"
        if self.delta < 0:
            return "understated"
        return "agrees"


@dataclass(frozen=True)
class Finding:
    """One measured discrepancy, with the evidence that produced it.

    ``evidence`` is the dictionary a reader can retrace the finding from, and a
    test asserts on the same values the report shows. It carries the two row sets
    as counts and as a short list of identifiers, because a list of eight hundred
    identifiers is not evidence a person can read.
    """

    scenario_id: str
    severity: str
    title: str
    family: str
    question: str
    claim: str
    cause: str
    correction: str
    evidence: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        """A plain dictionary for JSON output."""
        return {
            "scenario_id": self.scenario_id,
            "severity": self.severity,
            "title": self.title,
            "family": self.family,
            "question": self.question,
            "claim": self.claim,
            "cause": self.cause,
            "correction": self.correction,
            "evidence": self.evidence,
        }


@dataclass
class Investigation:
    """The outcome of investigating one scenario.

    A control that raises nothing is a successful investigation, so ``finding`` is
    optional and ``passed`` is a first class result rather than the absence of
    one. The access log is attached whether or not a finding was raised, because
    what a query was allowed to read is worth recording even when the query was
    right.
    """

    scenario: Scenario
    measurement: Measurement
    finding: Finding | None = None
    access: dict[str, object] = field(default_factory=dict)
    session_state: list[tuple[str, str]] = field(default_factory=list)
    shadowed: list[str] = field(default_factory=list)

    @property
    def scenario_id(self) -> str:
        """The scenario this investigation ran."""
        return self.scenario.scenario_id

    @property
    def passed(self) -> bool:
        """True when the scenario raised no finding."""
        return self.finding is None

    @property
    def weight(self) -> int:
        """Severity as a number, for ordering and aggregation."""
        from .findings import SEVERITY_WEIGHTS

        if self.finding is None:
            return 0
        return SEVERITY_WEIGHTS[self.finding.severity]

    def as_dict(self) -> dict[str, object]:
        """A plain dictionary for JSON output."""
        return {
            "scenario_id": self.scenario_id,
            "title": self.scenario.title,
            "family": self.scenario.family,
            "question": self.scenario.question,
            "changed_clause": self.scenario.changed_clause,
            "passed": self.passed,
            "weight": self.weight,
            "access": self.access,
            "session_state": [
                {"type": kind, "name": name} for kind, name in self.session_state
            ],
            "shadowed": list(self.shadowed),
            "measurement": {
                "reported": self.measurement.reported,
                "agreed": self.measurement.agreed,
                "delta": self.measurement.delta,
                "relative": self.measurement.relative,
                "direction": self.measurement.direction,
                "reported_rows": len(self.measurement.reported_rows),
                "agreed_rows": len(self.measurement.agreed_rows),
                "wrongly_included": len(self.measurement.wrongly_included),
                "wrongly_excluded": len(self.measurement.wrongly_excluded),
            },
            "finding": None if self.finding is None else self.finding.as_dict(),
        }


@dataclass
class InvestigationReport:
    """Every investigation, plus the totals."""

    investigations: list[Investigation] = field(default_factory=list)

    @property
    def weight(self) -> int:
        """Total severity weight."""
        return sum(investigation.weight for investigation in self.investigations)

    @property
    def by_severity(self) -> dict[str, int]:
        """Counts per severity, including zeroes."""
        from .findings import SEVERITY_WEIGHTS

        counts = {name: 0 for name in SEVERITY_WEIGHTS}
        for investigation in self.investigations:
            if investigation.finding is not None:
                counts[investigation.finding.severity] += 1
        return counts

    @property
    def passed(self) -> list[str]:
        """The scenarios that raised nothing."""
        return [
            investigation.scenario_id
            for investigation in self.investigations
            if investigation.passed
        ]

    @property
    def sorted_findings(self) -> list[Finding]:
        """Findings, most severe first, then by scenario id."""
        from .findings import SEVERITY_WEIGHTS

        findings = [
            investigation.finding
            for investigation in self.investigations
            if investigation.finding is not None
        ]
        return sorted(
            findings,
            key=lambda f: (-SEVERITY_WEIGHTS[f.severity], f.scenario_id),
        )

    def by_id(self) -> dict[str, Investigation]:
        """Investigations keyed by scenario id."""
        return {
            investigation.scenario_id: investigation
            for investigation in self.investigations
        }

    def as_dict(self) -> dict[str, object]:
        """A plain dictionary for JSON output."""
        return {
            "totals": {
                "scenarios": len(self.investigations),
                "passed": len(self.passed),
                "weight": self.weight,
                "by_severity": self.by_severity,
            },
            "scenarios": [investigation.as_dict() for investigation in self.investigations],
        }


def _fact_lines(connection: sqlite3.Connection, definition: Definition, *, qualify: bool) -> list[int]:
    """The fact line identifiers a definition aggregates.

    ``qualify`` is passed separately rather than read off the definition so that
    the row set behind the agreed value is always read from the warehouse table,
    whatever a scenario's own definition resolved against.
    """
    sql = build_rows(definition, qualify=qualify)
    return [row[0] for row in connection.execute(sql)]


def _value(connection: sqlite3.Connection, definition: Definition, *, qualify: bool) -> float | None:
    """The value a definition reports."""
    return warehouse.scalar(connection, build(definition, qualify=qualify))


def measure(
    connection: sqlite3.Connection, scenario: Scenario
) -> Measurement:
    """Compute both values and both row sets for one scenario."""
    qualify = scenario.definition.qualify
    return Measurement(
        reported=_value(connection, scenario.definition, qualify=qualify),
        agreed=_value(connection, scenario.agreed, qualify=True),
        reported_rows=_fact_lines(connection, scenario.definition, qualify=qualify),
        agreed_rows=_fact_lines(connection, scenario.agreed, qualify=True),
    )


def _session_evidence(
    connection: sqlite3.Connection, scenario: Scenario
) -> tuple[list[tuple[str, str]], list[str]]:
    """Run a scenario's setup and report the session state it produced."""
    for statement in scenario.setup:
        connection.execute(statement)
    return warehouse.temp_objects(connection), warehouse.shadowed_names(connection)


def _clear_session(connection: sqlite3.Connection) -> None:
    """Remove every temporary object a scenario may have left behind.

    The audit runs several scenarios against one connection, and a temporary
    object left in place by one scenario would silently change what a later
    scenario reads, which is the very defect being demonstrated. Clearing between
    scenarios is what keeps the control a genuine control.
    """
    for kind, name in warehouse.temp_objects(connection):
        if kind == "view":
            connection.execute(f"DROP VIEW IF EXISTS temp.{name}")
        elif kind == "table":
            connection.execute(f"DROP TABLE IF EXISTS temp.{name}")


def investigate(
    scenario: Scenario,
    connection: sqlite3.Connection | None = None,
    *,
    role_name: str = "analyst",
) -> Investigation:
    """Investigate one scenario and return what was measured.

    The scenario runs under a role, and the role's grant is enforced by the
    engine rather than by convention. A scenario that reads a column its role may
    not read raises rather than returning a number, which is why the run name is
    recorded even when nothing is wrong.
    """
    owned = connection is None
    connection = connection or warehouse.build()

    try:
        _clear_session(connection)
        session_state, shadowed = _session_evidence(connection, scenario)

        session = Session(connection, role_name)
        with session:
            session.reset_log()
            try:
                measurement = measure(connection, scenario)
                denial = ""
            except (AccessDenied, sqlite3.DatabaseError) as exc:
                denial = str(exc)
                measurement = Measurement(reported=None, agreed=None)
            access = session.log.as_dict()
            access["role"] = role_name
            if denial:
                access["denied_reason"] = denial

        if measurement.reported is None or measurement.agreed is None:
            finding = Finding(
                scenario_id=scenario.scenario_id,
                severity="high",
                title=scenario.title,
                family=scenario.family,
                question=scenario.question,
                claim=scenario.claim,
                cause=(
                    "The reporting query could not be run under the role it was "
                    "supposed to run under: " + denial
                ),
                correction=(
                    "Grant the role the columns the definition needs, or run the "
                    "definition under a role that has them. A figure that cannot be "
                    "produced should not be published."
                ),
                evidence={"access": access},
            )
            return Investigation(
                scenario=scenario,
                measurement=measurement,
                finding=finding,
                access=access,
                session_state=session_state,
                shadowed=shadowed,
            )

        severity = severity_for_relative(measurement.relative)
        finding = None
        if severity is not None:
            finding = Finding(
                scenario_id=scenario.scenario_id,
                severity=severity,
                title=scenario.title,
                family=scenario.family,
                question=scenario.question,
                claim=scenario.claim,
                cause=scenario.cause,
                correction=scenario.correction,
                evidence=_evidence(scenario, measurement, session_state, shadowed, access),
            )

        return Investigation(
            scenario=scenario,
            measurement=measurement,
            finding=finding,
            access=access,
            session_state=session_state,
            shadowed=shadowed,
        )
    finally:
        _clear_session(connection)
        if owned:
            connection.close()


def _evidence(
    scenario: Scenario,
    measurement: Measurement,
    session_state: list[tuple[str, str]],
    shadowed: list[str],
    access: dict[str, object],
) -> dict[str, object]:
    """Build the evidence dictionary for one finding."""
    included = measurement.wrongly_included
    excluded = measurement.wrongly_excluded
    evidence: dict[str, object] = {
        "measure": scenario.definition.measure,
        "unit": scenario.unit,
        "changed_clause": scenario.changed_clause,
        "reported": measurement.reported,
        "agreed": measurement.agreed,
        "delta": round(measurement.delta, 4),
        "relative": round(measurement.relative, 6),
        "direction": measurement.direction,
        "rows_reported": len(measurement.reported_rows),
        "rows_agreed": len(measurement.agreed_rows),
        "rows_wrongly_included": len(included),
        "rows_wrongly_excluded": len(excluded),
        "sample_wrongly_included": included[:8],
        "sample_wrongly_excluded": excluded[:8],
        "reporting_sql": build(scenario.definition, qualify=scenario.definition.qualify).strip(),
        "agreed_sql": build(scenario.agreed, qualify=True).strip(),
        "tables_read": access.get("tables_read", []),
    }
    if scenario.family == "session":
        evidence["session_state"] = [
            f"{kind} {name}" for kind, name in session_state
        ]
        evidence["shadowed_names"] = list(shadowed)
        evidence["schema_qualified"] = scenario.definition.qualify
    return evidence


def investigate_all(
    scenario_ids: tuple[str, ...] | None = None,
    connection: sqlite3.Connection | None = None,
    *,
    role_name: str = "analyst",
) -> InvestigationReport:
    """Investigate every scenario, or the named ones, in a stable order."""
    selected = (
        ANOMALIES
        if scenario_ids is None
        else tuple(load_scenario(scenario_id) for scenario_id in scenario_ids)
    )
    owned = connection is None
    connection = connection or warehouse.build()
    try:
        return InvestigationReport(
            investigations=[
                investigate(scenario, connection, role_name=role_name)
                for scenario in selected
            ]
        )
    finally:
        if owned:
            connection.close()
