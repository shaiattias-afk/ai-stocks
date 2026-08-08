"""
policies/prior_fiscal_year_lookup.py — D-027 Policy A item 7:
average_invested_capital may reuse the PREVIOUS fiscal year's own
separately-locked filing and its latest-approved invested_capital
result directly — it does not require the prior period to appear as a
comparative fact inside the current filing (structurally unavailable
for the maturity-basis fallback, which is only ever anchored to the
current balance-sheet date).

Also documents the scripts/98 / scripts/101 / scripts/105
supplementary-accession pattern: for each ticker's own first target
fiscal year, the "prior fiscal year" is a SUPPLEMENTARY accession that
is not itself one of the 45 target company-years (it carries no rows
of its own in the frozen `financial_metric_results` table) — its
invested_capital is recomputed on demand from the warehouse via the
exact same metrics.annual.compute_company_year used for every other
filing, not looked up in production (there is nothing to look up: the
supplementary accession's own metric rows were never part of the
approved/frozen 900-row dataset). See metrics/annual.py's
compute_full_company_year for how the two paths (production lookup for
a normal prior year vs. on-demand recomputation for a supplementary
one) are composed.

Ported byte-exact (the averaging + status-precedence logic) from
scripts/93_average_invested_capital_prior_filing_lookup.py's `main()`
loop body, factored into a standalone function — a structural move
only; the expressions are unchanged.
"""

from __future__ import annotations

SUCCESSFUL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "PASS_NORMALIZED_TAX"}


def combine_current_and_prior_invested_capital(
    current_ic_status: str,
    current_ic_value: float | None,
    prior_ic_status: str,
    prior_ic_value: float | None,
) -> dict[str, object]:
    """
    Mirrors scripts/93_average_invested_capital_prior_filing_lookup.py's
    main() body exactly:

        if current_ic["status"] not in SUCCESSFUL_STATUSES: not this policy's job
        if prior_ic["status"] not in SUCCESSFUL_STATUSES: prior not ready
        avg_value = (current_ic["value"] + prior_ic["value"]) / 2
        status = (
            "PASS_DIRECT_AGGREGATE" if "PASS_DIRECT_AGGREGATE" in (current_ic["status"], prior_ic["status"])
            else "PASS_MATURITY_BASIS" if "PASS_MATURITY_BASIS" in (current_ic["status"], prior_ic["status"])
            else "PASS"
        )
    """

    if current_ic_status not in SUCCESSFUL_STATUSES:
        return {"status": "REVIEW_REQUIRED", "value": None, "error": f"invested_capital itself not resolved (status={current_ic_status})"}
    if prior_ic_status not in SUCCESSFUL_STATUSES:
        return {"status": "REVIEW_REQUIRED", "value": None, "error": f"prior fiscal year's invested_capital not ready (status={prior_ic_status})"}

    avg_value = (current_ic_value + prior_ic_value) / 2
    status = (
        "PASS_DIRECT_AGGREGATE" if "PASS_DIRECT_AGGREGATE" in (current_ic_status, prior_ic_status)
        else "PASS_MATURITY_BASIS" if "PASS_MATURITY_BASIS" in (current_ic_status, prior_ic_status)
        else "PASS"
    )

    return {"status": status, "value": avg_value}
