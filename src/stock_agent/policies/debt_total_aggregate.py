"""
policies/debt_total_aggregate.py -- D-018's "aggregate-first" total_debt
resolution, extended by D-027 Policy C: a filing's own reported "Total"
row in its debt-maturity schedule is authoritative for total_debt even
when it does not reconcile exactly with the balance-sheet long_term_debt
carrying value (face value vs. carrying value net of unamortized
discount/issuance costs) -- the gap is preserved in lineage, never
silently discarded.

Ported byte-exact from scripts/92_groups_1_3_4_debt_facility_aggregate_
policy.py.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from stock_agent.policies.debt_current_long_term import classify_maturity_buckets

def resolve_total_debt_with_aggregate_policy(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
    current_debt_result: dict[str, object],
    long_term_debt_result: dict[str, object],
) -> dict[str, object]:
    """
    3-tier preference (extends D-022's 2-tier resolver with the NEW
    Policy C direct-aggregate tier in the middle):
      1. GAAP_CARRYING_VALUE — current_debt + long_term_debt, both PASS.
      2. PASS_DIRECT_AGGREGATE (NEW) — the filing's OWN reported "Total"
         row in its debt-maturity schedule, used AS-IS even when it does
         not reconcile with the balance-sheet long_term_debt carrying
         value (e.g. face value vs. carrying value net of unamortized
         discount/issuance costs) — the gap is recorded, never silently
         dropped, and never blocks this or downstream metrics.
      3. PASS_MATURITY_BASIS (D-022, unchanged) — bucket-sum fallback
         when no reported Total row exists either.
    """

    if (
        current_debt_result.get("status") == "PASS"
        and long_term_debt_result.get("status") == "PASS"
    ):
        return {
            "status": "PASS",
            "value": current_debt_result["value"] + long_term_debt_result["value"],
            "basis": "GAAP_CARRYING_VALUE",
        }

    maturity = classify_maturity_buckets(connection, accession_number, presentation, report_date)

    if maturity["reported_total"] is not None:
        return {
            "status": "PASS_DIRECT_AGGREGATE",
            "value": maturity["reported_total"],
            "basis": "DIRECT_AGGREGATE_REPORTED_TOTAL",
            "reconciliation_gap": maturity["reconciliation_gap"],
            "maturity_role_uri": maturity["role_uri"],
            "carrying_value_current_debt": current_debt_result.get("value"),
            "carrying_value_current_debt_status": current_debt_result.get("status"),
            "carrying_value_long_term_debt": long_term_debt_result.get("value"),
            "carrying_value_long_term_debt_status": long_term_debt_result.get("status"),
            "lineage": {
                "reported_total_face_value": maturity["reported_total"],
                "reconciliation_gap_vs_bucket_sum": maturity["reconciliation_gap"],
                "note": (
                    "Policy C: reported total used as-is; gap (if any) vs. "
                    "balance-sheet long_term_debt carrying value is a known, "
                    "expected face-value-vs-carrying-value difference "
                    "(e.g. unamortized debt issuance costs/discount), not a "
                    "data error — never blocks total_debt or downstream "
                    "metrics per approved Policy C."
                ),
            },
        }

    if maturity["total_debt_maturity"] is not None:
        return {
            "status": "PASS_MATURITY_BASIS",
            "value": maturity["total_debt_maturity"],
            "basis": "MATURITY_PRINCIPAL",
            "source_current_debt_maturity": maturity["current_debt_maturity"],
            "source_long_term_debt_maturity": maturity["long_term_debt_maturity"],
            "maturity_role_uri": maturity["role_uri"],
            "carrying_value_current_debt": current_debt_result.get("value"),
            "carrying_value_current_debt_status": current_debt_result.get("status"),
            "carrying_value_long_term_debt": long_term_debt_result.get("value"),
            "carrying_value_long_term_debt_status": long_term_debt_result.get("status"),
        }

    return {
        "status": "REVIEW_REQUIRED",
        "value": None,
        "basis": None,
        "error": (
            f"no reliable carrying-value total (current_debt="
            f"{current_debt_result.get('status')}, long_term_debt="
            f"{long_term_debt_result.get('status')}), no reported "
            "debt-maturity-schedule Total row, and no maturity-bucket-sum "
            "fallback available either"
        ),
    }

