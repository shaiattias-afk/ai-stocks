"""
policies/debt_undrawn_revolver.py -- D-027 Policy B: an explicit,
resolved, zero-valued "amount outstanding" fact for a revolving credit
facility proves zero debt for that facility at the exact report date.

Ported byte-exact from scripts/92_groups_1_3_4_debt_facility_aggregate_
policy.py (new in scripts/92 -- Policy B did not exist in scripts/79-89).
"""

from __future__ import annotations

import duckdb

REVOLVER_OUTSTANDING_CONCEPT_PATTERN = r"^us-gaap:LineOfCredit$"


REVOLVER_MEMBER_PATTERN = r"revolving"


def find_undrawn_revolver_evidence(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    report_date: str,
) -> dict[str, object] | None:
    """
    Policy B: an explicitly undrawn revolving-credit facility is zero
    debt for that facility. Evidence accepted ONLY as a real, resolved
    (non-nil, numeric) XBRL fact for the facility's own "amount
    outstanding" concept (us-gaap:LineOfCredit — the standard tag filers
    use for revolver draw balances, confirmed generic across CRWD/META/
    NVDA/PANW's own filings, never a ticker-specific tag), dimensioned
    by a CreditFacilityAxis/LongtermDebtTypeAxis member whose name
    contains "Revolving" (case-insensitive — never a company-specific
    member name), at the EXACT report date, equal to 0. Never inferred
    from the credit limit/availability alone (policy requirement — an
    unused commitment is not evidence of an outstanding balance either
    way, so it is never consulted here).
    """

    facts = connection.execute(
        """
        SELECT value_numeric, instant_date, dimensions_json, context_id, is_nil
        FROM xbrl_facts
        WHERE accession_number = ?
          AND regexp_matches(concept_qname, ?, 'i')
          AND regexp_matches(dimensions_json, ?, 'i')
          AND is_nil = FALSE
        """,
        [accession_number, REVOLVER_OUTSTANDING_CONCEPT_PATTERN, REVOLVER_MEMBER_PATTERN],
    ).fetchdf()

    if facts.empty:
        return None

    at_report_date = facts[
        (facts["instant_date"] == report_date) & facts["value_numeric"].notna()
    ]

    if at_report_date.empty:
        return None

    distinct_values = sorted(set(at_report_date["value_numeric"].tolist()))

    if len(distinct_values) != 1:
        return None  # genuinely ambiguous — do not guess, fall through

    if distinct_values[0] != 0:
        return None  # revolver IS drawn — not evidence of zero

    row = at_report_date.iloc[0]
    return {
        "concept_qname": "us-gaap:LineOfCredit",
        "value": 0.0,
        "instant_date": str(row["instant_date"]),
        "context_id": str(row["context_id"]),
        "dimensions_json": str(row["dimensions_json"]),
    }

