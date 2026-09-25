"""KPI anomaly investigation dashboard.

A simulated cloud data warehouse, the queries that investigate it, and the
corrected figures the dashboard binds to.
"""

from __future__ import annotations

from .anomalies import ANOMALIES, ANOMALY_IDS
from .catalogue import ROLE_NAMES, TABLE_NAMES, TABLES, VIEW_NAMES
from .investigation import Investigation, InvestigationReport, investigate, investigate_all
from .warehouse import SEED, build

__all__ = [
    "ANOMALIES",
    "ANOMALY_IDS",
    "ROLE_NAMES",
    "SEED",
    "TABLE_NAMES",
    "TABLES",
    "VIEW_NAMES",
    "Investigation",
    "InvestigationReport",
    "build",
    "investigate",
    "investigate_all",
]
