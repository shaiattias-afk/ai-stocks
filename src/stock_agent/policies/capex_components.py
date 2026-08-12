"""
policies/capex_components.py -- D-P2 (docs/CLEANUP_DECISIONS_PENDING.md):
utility capital spending is often split across several cash-flow lines
instead of one "Capital expenditures" row, and the label wording for the
non-routine line drifts year to year at the SAME filer -- measured at
AEP: "Acquisition of Assets" (2020-2022) -> "Acquisitions of Renewable
Energy Facilities" (2023) -> "Acquisitions of Generation Facilities"
(2025), three different labels on what is structurally the same kind of
spend. Chasing label wording for this is a losing game.

The concept itself, not the label, is the reliable signal: AEP reports
"Construction Expenditures" every single year via
us-gaap:PaymentsForConstructionInProcess (routine capex), ALONGSIDE a
second line via us-gaap:PaymentsToAcquireProductiveAssets or us-gaap:
PaymentsToAcquirePropertyPlantAndEquipment (whatever that year's label
says) for larger, lumpier physical-asset purchases. Both concepts are
GAAP-defined for a PHYSICAL asset acquisition, never a business
combination -- "ProductiveAssets" is literally the taxonomy's own term
for physical assets used in operations, deliberately distinct from
PaymentsToAcquireBusinessesNetOfCashAcquired and its siblings. Neither
concept is ever used for nuclear fuel, investment securities, or any
other non-capex spend at AEP (confirmed against every AEP filing in the
warehouse, 2020-2025) -- those use their own, separate concepts.

D-P2's approved decision ("generation facilities -> capital spending,
scoped to physical operating assets only") is implemented here as: sum
whichever of these three concepts are present as distinct rows in the
target cash-flow statement, never guess at a fourth. A single matching
concept resolves exactly like today's identify_canonical_row would (same
number), just reached without depending on that year's label wording.

This is a FALLBACK, tried only when the existing single-row identify_
canonical_row lookup for "capex" has already failed to find exactly one
candidate -- it never overrides an already-working label match, and it
never touches the 45 frozen company-years (their capex is a production
passthrough in metrics/annual.py, never routed through this module).
"""

from __future__ import annotations

import duckdb
import pandas as pd

from stock_agent.extraction.core import (
    SUCCESSFUL_STATUSES,
    _narrow_to_registrant_statements,
    match_facts_from_warehouse,
)

# GAAP concepts (local name only -- namespace-qualified in the warehouse
# as "us-gaap:<name>") that are ALWAYS a physical-asset capex line,
# regardless of that year's custom label. Deliberately excludes
# PaymentsToAcquireInvestments (financial investments, not physical
# assets), PaymentsForNuclearFuel (fuel is an operating/inventory cost,
# not a capital asset), and every PaymentsToAcquireBusinesses* concept
# (a genuine business combination, D-P2's explicit non-goal).
CAPEX_COMPONENT_CONCEPT_LOCAL_NAMES: frozenset[str] = frozenset({
    "PaymentsForConstructionInProcess",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsToAcquirePropertyPlantAndEquipment",
})

_ROLE_INCLUDE_PATTERN = r"cash\s*flows?"


def find_capex_aggregate_components(
    presentation: pd.DataFrame,
) -> list[dict[str, str]] | None:
    """Finds every row, in the registrant's own cash-flow statement,
    whose concept is one of CAPEX_COMPONENT_CONCEPT_LOCAL_NAMES. Returns
    None if none are present (nothing for the caller to aggregate --
    falls through to whatever the caller already does on REVIEW_REQUIRED)
    or if only a role-narrowing failure leaves it ambiguous (fails closed,
    same discipline as every other policy in this project)."""

    is_statement_role = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-", na=False
    )
    is_role_include = presentation["role_definition"].str.contains(
        _ROLE_INCLUDE_PATTERN, case=False, regex=True, na=False
    )
    cash_flow_roles = presentation.loc[
        is_statement_role & is_role_include, ["role_uri", "role_definition"]
    ].drop_duplicates()

    # D-P1: a combined filing repeats the cash-flow statement per
    # subsidiary registrant -- narrow to the registrant's own statement
    # first, exactly as identify_canonical_row does. A no-op for any
    # single-registrant filing.
    cash_flow_roles = _narrow_to_registrant_statements(cash_flow_roles)
    cash_flow_role_uris = set(cash_flow_roles["role_uri"].unique())
    if not cash_flow_role_uris:
        return None

    is_concept_match = presentation["concept_qname"].astype(str).str.extract(
        r":([^:]+)$", expand=False
    ).isin(CAPEX_COMPONENT_CONCEPT_LOCAL_NAMES)

    candidates = presentation[
        presentation["role_uri"].isin(cash_flow_role_uris)
        & is_concept_match
        & ~presentation["is_abstract"].astype(bool)
    ].drop_duplicates(subset=["concept_qname"])

    if candidates.empty:
        return None

    return [
        {
            "concept_qname": str(row["concept_qname"]),
            "label": str(row["label"]),
        }
        for _, row in candidates.iterrows()
    ]


def resolve_capex_by_component_aggregate(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
) -> dict[str, object] | None:
    """Sums every resolved CAPEX_COMPONENT_CONCEPT_LOCAL_NAMES row into a
    single capex figure. Returns None (never REVIEW_REQUIRED) when there
    is nothing to aggregate, so the caller's own existing REVIEW_REQUIRED
    handling is unaffected; returns REVIEW_REQUIRED only once a component
    has actually been identified but fails to resolve to a clean value --
    silently dropping a known component would understate capex exactly
    the way the un-aggregated single-row lookup already did."""

    components = find_capex_aggregate_components(presentation)
    if components is None:
        return None

    resolved = []
    for component in components:
        decision = match_facts_from_warehouse(
            connection, accession_number, component["concept_qname"], report_date, "duration"
        )
        if decision["status"] not in SUCCESSFUL_STATUSES:
            return {
                "status": "REVIEW_REQUIRED",
                "value": None,
                "error": (
                    f"capex component {component['concept_qname']!r} "
                    f"({component['label']!r}) did not resolve to a single value: "
                    f"{decision.get('error')}"
                ),
            }
        resolved.append(decision)

    return {
        "status": "PASS_DIRECT_AGGREGATE" if len(resolved) > 1 else "PASS",
        "value": sum(r["value"] for r in resolved),
        "concept_qname": " + ".join(c["concept_qname"] for c in components),
        "selection_tier": "capex_component_aggregate",
    }
