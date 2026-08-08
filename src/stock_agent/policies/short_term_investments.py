"""
policies/short_term_investments.py -- D-030: proves
short_term_investments=0 via calculation-linkbase completeness of
AssetsCurrent (the filing's own "Total current assets" row) -- if
AssetsCurrent's calculation-linkbase children are all resolvable and
sum to the reported total, and none of them carries short-term-
investment vocabulary in its label, short_term_investments is proven
zero by structural absence (never inferred from a missing row alone).

Ported byte-exact from scripts/99_short_term_investments_zero_proof.py.
The two calls that script made via its importlib-loaded "s92" module
(reconstruct_calculation_children_by_parent, match_facts_from_warehouse)
are imported directly from extraction.core here instead.
"""

from __future__ import annotations

import re

import duckdb

from stock_agent.extraction.core import (
    match_facts_from_warehouse,
    reconstruct_calculation_children_by_parent,
)

SHORT_TERM_INVESTMENT_VOCABULARY_PATTERN = r"marketable\s+securities|short-?term\s+investments"


ASSETS_CURRENT_CONCEPT_PATTERN = r":AssetsCurrent$"


def find_assets_current_row(presentation, report_date) -> dict | None:
    """Finds the filing's own 'Total current assets' row (standard
    concept us-gaap:AssetsCurrent — universal GAAP, not ticker-specific)
    on the primary balance sheet role."""

    is_statement = presentation["role_definition"].str.match(r"^\d+\s*-\s*Statement\s*-", na=False)
    is_bs_role = presentation["role_definition"].str.contains(
        r"balance\s+sheets?|financial\s+position", case=False, regex=True, na=False
    )
    is_excluded = presentation["role_definition"].str.contains("parenthetical", case=False, regex=True, na=False)
    bs_rows = presentation[is_statement & is_bs_role & ~is_excluded]

    matches = bs_rows[bs_rows["concept_qname"].str.contains(ASSETS_CURRENT_CONCEPT_PATTERN, regex=True, na=False)]
    matches = matches.drop_duplicates(subset=["role_uri", "concept_qname"])

    if len(matches) != 1:
        return None

    row = matches.iloc[0]
    return {"role_uri": str(row["role_uri"]), "concept_qname": str(row["concept_qname"])}



def attempt_short_term_investments_zero_proof(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation,
    report_date: str,
) -> dict:
    assets_current = find_assets_current_row(presentation, report_date)
    if assets_current is None:
        return {"status": "REVIEW_REQUIRED", "error": "no unique 'Total current assets' (AssetsCurrent) row found on the balance sheet"}

    children_by_parent = reconstruct_calculation_children_by_parent(
        connection, accession_number, assets_current["role_uri"]
    )
    children = children_by_parent.get(assets_current["concept_qname"])

    if not children:
        return {"status": "REVIEW_REQUIRED", "error": "no calculation-linkbase children found for AssetsCurrent — cannot prove completeness"}

    label_by_concept = dict(zip(presentation["concept_qname"], presentation["label"]))

    for child in children:
        child_label = label_by_concept.get(child, child)
        if re.search(SHORT_TERM_INVESTMENT_VOCABULARY_PATTERN, child_label, re.IGNORECASE) or re.search(
            SHORT_TERM_INVESTMENT_VOCABULARY_PATTERN, child, re.IGNORECASE
        ):
            return {
                "status": "REVIEW_REQUIRED",
                "error": (
                    f"AssetsCurrent's own calculation includes a short-term-investment-"
                    f"vocabulary child ({child}, label={child_label!r}) that the standard "
                    "row-identification tier failed to resolve — a genuine ambiguity, not "
                    "a zero case"
                ),
            }

    child_values: dict[str, float] = {}
    for child in children:
        decision = match_facts_from_warehouse(connection, accession_number, child, report_date, "instant")
        if decision["status"] != "PASS":
            return {
                "status": "REVIEW_REQUIRED",
                "error": f"AssetsCurrent child {child} did not resolve to a single value ({decision.get('error')})",
            }
        child_values[child] = decision["value"]

    total_decision = match_facts_from_warehouse(
        connection, accession_number, assets_current["concept_qname"], report_date, "instant"
    )
    if total_decision["status"] != "PASS":
        return {"status": "REVIEW_REQUIRED", "error": "AssetsCurrent total itself did not resolve to a single value"}

    children_sum = sum(child_values.values())
    total_value = total_decision["value"]
    gap = total_value - children_sum

    if abs(gap) > 1:
        return {
            "status": "REVIEW_REQUIRED",
            "error": (
                f"AssetsCurrent children sum ({children_sum}) does not reconcile with the "
                f"reported total ({total_value}), gap={gap} — cannot assume the gap is "
                "short-term investments without guessing"
            ),
        }

    return {
        "status": "PASS",
        "value": 0.0,
        "concept_qname": "proven_zero_no_short_term_investments_component",
        "selection_tier": "zero_proven_no_component_found",
        "basis": "ZERO_PROVEN_STRUCTURAL_ABSENCE",
        "lineage": {
            "assets_current_concept": assets_current["concept_qname"],
            "assets_current_role_uri": assets_current["role_uri"],
            "assets_current_total_value": total_value,
            "calculation_children": [
                {"concept_qname": c, "label": label_by_concept.get(c, c), "value": child_values[c]}
                for c in children
            ],
            "children_sum": children_sum,
            "reconciliation_gap": gap,
            "report_date": report_date,
        },
    }
