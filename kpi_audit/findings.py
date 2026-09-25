"""How a finding's severity is defined.

This module exists so that severity has one definition and not several. A
discrepancy is measured as a fraction of the figure that should have been
reported, and mapped onto a band. The bands are in ``anomalies.py`` next to the
definitions they apply to, and the weights live here.
"""

from __future__ import annotations

SEVERITY_WEIGHTS = {"high": 3, "medium": 2, "low": 1}

SEVERITY_ORDER = ("high", "medium", "low")
