"""Pytest configuration.

Placing this file at the repository root puts the repository root on ``sys.path``
so that ``kpi_audit`` imports without an install step, and it gives the tests one
shared warehouse and one shared investigation, both built once per session.

The warehouse is built once rather than per test because it is deterministic: the
seeded generator produces the same rows on every build, so a second copy would
only make the suite slower. The connection is shared for the same reason, and the
investigation clears any temporary object a scenario leaves behind before the next
one runs, which is what keeps the control a genuine control.
"""

from __future__ import annotations

import pytest

from kpi_audit import build, investigate_all
from kpi_audit.anomalies import ANOMALY_IDS, load_scenario
from kpi_audit.catalogue import ROLES


@pytest.fixture(scope="session")
def connection():
    """One warehouse, built from the fixed seed."""
    con = build()
    yield con
    con.close()


@pytest.fixture(scope="session")
def report(connection):
    """Every scenario investigated under the analyst role, run once per session."""
    return investigate_all(connection=connection)


@pytest.fixture(scope="session")
def investigations(report):
    """The investigations keyed by scenario id."""
    return report.by_id()


@pytest.fixture(scope="session")
def findings(report):
    """The findings keyed by scenario id."""
    return {finding.scenario_id: finding for finding in report.sorted_findings}


@pytest.fixture(scope="session")
def control(investigations):
    """The control investigation, which must raise nothing."""
    return investigations["control_certified"]


@pytest.fixture(scope="session")
def scenario_ids():
    """Every scenario id."""
    return ANOMALY_IDS


@pytest.fixture(scope="session")
def role_names():
    """Every role name."""
    return tuple(ROLES)


@pytest.fixture
def scenario():
    """Look up a scenario by id inside a test."""

    def _load(scenario_id: str):
        return load_scenario(scenario_id)

    return _load
