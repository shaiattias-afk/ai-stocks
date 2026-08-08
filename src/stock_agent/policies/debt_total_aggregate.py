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

*** SCOPING (read before calling) ***

D-027 Policy C (the PASS_DIRECT_AGGREGATE tier below) was never applied
project-wide: scripts/92's own `main()` only ever invoked
compute_company_year for its own literal, bounded `TARGET_FILINGS` list
(12 filings: PANW 2025-07-31; CRWD 2022/2023/2024/2025/2026-01-31; META
2020/2021/2022/2023/2024-12-31; NVDA 2020-01-26 -- reproduced verbatim
below as `POLICY_C_APPLIED_SCOPE`). scripts/79 (D-022), Policy C's own
direct predecessor, had an EARLIER, narrower 2-tier precedence with NO
"prefer the reported Total row" step at all (GAAP carrying value, then
straight to the maturity-bucket SUM -- never the schedule's own reported
Total row directly) -- confirmed by reading scripts/79's own
`resolve_total_debt_maturity_basis` (archived, byte-for-byte below as
`resolve_total_debt_maturity_basis_d022`), applied to its own separate,
bounded 10-filing scope (`D022_APPLIED_SCOPE` below: AMZN and GOOGL,
2021-2025 each) that scripts/92 never re-covered.

The live `financial_metric_results` table (confirmed directly, not
assumed: `engine_version = 'v1-maturity-basis (scripts/79, D-022)'` on
every one of AMZN's and GOOGL's `total_debt` rows) reflects exactly this
history: AMZN/GOOGL's `total_debt` was produced by scripts/79's narrower
precedence and never reprocessed under Policy C, even though, for these
specific company-years, the reported Total row happens to numerically
equal the bucket sum (so the VALUE is the same either way -- only the
STATUS label differs: `PASS_MATURITY_BASIS` vs. the more specific
`PASS_DIRECT_AGGREGATE` Policy C would report). Reproducing production
exactly therefore requires choosing the resolver BY SCOPE, not always
preferring the more complete one -- metrics/annual.py does this by
checking `accession_number` against the two scope sets below before
calling either resolver, exactly mirroring what each script's own
`TARGET_FILINGS` list actually did.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from stock_agent.policies.debt_current_long_term import classify_maturity_buckets

# Ported verbatim from scripts/92_groups_1_3_4_debt_facility_aggregate_
# policy.py's own TARGET_FILINGS (accession numbers only -- these are the
# exact 12 filings scripts/92's Policy C was ever run against).
POLICY_C_APPLIED_SCOPE: frozenset[str] = frozenset({
    "0001327567-25-000027",  # PANW 2025-07-31
    "0001535527-22-000006",  # CRWD 2022-01-31
    "0001535527-23-000008",  # CRWD 2023-01-31
    "0001535527-24-000007",  # CRWD 2024-01-31
    "0001535527-25-000009",  # CRWD 2025-01-31
    "0001535527-26-000010",  # CRWD 2026-01-31
    "0001326801-21-000014",  # META 2020-12-31
    "0001326801-22-000018",  # META 2021-12-31
    "0001326801-23-000013",  # META 2022-12-31
    "0001326801-24-000012",  # META 2023-12-31
    "0001326801-25-000017",  # META 2024-12-31
    "0001045810-20-000010",  # NVDA 2020-01-26
})

# Ported verbatim from scripts/79_maturity_based_debt_classification.py's
# own TARGET_FILINGS -- the 10 filings D-022's narrower (pre-Policy-C)
# precedence was ever run against, and the only filings production's
# total_debt-family metrics ever sourced from scripts/79 for.
D022_APPLIED_SCOPE: frozenset[str] = frozenset({
    "0001018724-22-000005",  # AMZN 2021-12-31
    "0001018724-23-000004",  # AMZN 2022-12-31
    "0001018724-24-000008",  # AMZN 2023-12-31
    "0001018724-25-000004",  # AMZN 2024-12-31
    "0001018724-26-000004",  # AMZN 2025-12-31
    "0001652044-22-000019",  # GOOGL 2021-12-31
    "0001652044-23-000016",  # GOOGL 2022-12-31
    "0001652044-24-000022",  # GOOGL 2023-12-31
    "0001652044-25-000014",  # GOOGL 2024-12-31
    "0001652044-26-000018",  # GOOGL 2025-12-31
})


def resolve_total_debt_maturity_basis_d022(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
    current_debt_result: dict[str, object],
    long_term_debt_result: dict[str, object],
) -> dict[str, object]:
    """
    Ported byte-exact from scripts/79_maturity_based_debt_classification.
    py's own `resolve_total_debt_maturity_basis` (D-022's ORIGINAL,
    narrower 2-tier precedence -- no direct-aggregate-reported-Total
    preference; this predates Policy C, which added that tier later in
    scripts/92):
      1. GAAP_CARRYING_VALUE -- current_debt + long_term_debt, both PASS.
      2. PASS_MATURITY_BASIS -- total_debt_maturity (the SUM of the
         filing's own current+long-term maturity buckets), never the
         schedule's own reported Total row directly.
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

    if maturity["total_debt_maturity"] is not None:
        return {
            "status": "PASS_MATURITY_BASIS",
            "value": maturity["total_debt_maturity"],
            "basis": "MATURITY_PRINCIPAL",
            "source_current_debt_maturity": maturity["current_debt_maturity"],
            "source_long_term_debt_maturity": maturity["long_term_debt_maturity"],
            "reported_total": maturity["reported_total"],
            "reconciliation_gap": maturity["reconciliation_gap"],
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
            f"{long_term_debt_result.get('status')}) and no maturity-basis "
            "fallback available either (no unique maturity schedule, or a "
            "bucket did not resolve to a single value)"
        ),
    }


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

