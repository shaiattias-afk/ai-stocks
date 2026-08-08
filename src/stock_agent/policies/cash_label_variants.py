"""
policies/cash_label_variants.py -- documents and re-exports the
cash_and_equivalents MetricDefinition used by extraction/core.py's
BUILT_IN_METRICS, carrying the two label/role fixes approved as D-023
(Micron: broadened the mention pattern to match bare "cash and cash
equivalents" label variants) and D-025 (Palo Alto Networks: excluded
any role whose title contains "reconciliation", which had been
matching a non-balance-sheet cash-reconciliation disclosure using the
same concept).

The definition itself lives in extraction/core.py's BUILT_IN_METRICS
literal (kept as one dict, byte-exact from scripts/92, to avoid
splitting a dict literal across files -- see core.py's docstring).
This module re-exports the specific entry under the name used by the
target module map, for discoverability.
"""

from __future__ import annotations

from stock_agent.extraction.core import BUILT_IN_METRICS

CASH_AND_EQUIVALENTS_DEFINITION = BUILT_IN_METRICS["cash_and_equivalents"]
