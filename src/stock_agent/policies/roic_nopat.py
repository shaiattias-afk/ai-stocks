"""
policies/roic_nopat.py — D-015's NOPAT/invested_capital/ROIC formulas,
and the D-027 final ROIC combination pass (scripts/95): once both
nopat and average_invested_capital independently resolve to a
successful status, roic = nopat / average_invested_capital.

D-015 items 1-6 (Total Debt / Invested Capital / Average Invested
Capital definitions) are implemented across extraction/core.py and
policies/debt_current_long_term.py, policies/debt_total_aggregate.py,
and metrics/annual.py's compute_company_year (invested_capital =
total_debt + stockholders_equity - cash - short_term_investments,
carried over unchanged from scripts/92). This module implements only
item 7 (NOPAT = operating_income * (1 - tax_rate)) for the *normalized*
case (see policies/tax_normalization.py) and the final ROIC combination
(scripts/95) — the plain-tax-rate NOPAT/ROIC case is always sourced,
in the live table, from scripts/60/69 (out of scope; see tax_
normalization.py's docstring for the same scoping note).

Ported byte-exact (the status-precedence and division) from
scripts/95_final_roic_combination_pass.py's `main()` loop body,
factored into a standalone function — a structural move only.
"""

from __future__ import annotations

SUCCESSFUL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "PASS_NORMALIZED_TAX"}


def combine_average_invested_capital_and_nopat_into_roic(
    avg_ic_status: str,
    avg_ic_value: float | None,
    nopat_status: str,
    nopat_value: float | None,
) -> dict[str, object]:
    """
    Mirrors scripts/95_final_roic_combination_pass.py's main() body
    exactly:

        if avg_ic["status"] not in SUCCESSFUL_STATUSES: not ready
        if nopat["status"] not in SUCCESSFUL_STATUSES: not ready
        if avg_ic["value"] is None or avg_ic["value"] == 0: not ready
        roic_value = nopat["value"] / avg_ic["value"]
        status = (
            "PASS_DIRECT_AGGREGATE" if "PASS_DIRECT_AGGREGATE" in (avg_ic["status"], nopat["status"])
            else "PASS_NORMALIZED_TAX" if nopat["status"] == "PASS_NORMALIZED_TAX"
            else "PASS_MATURITY_BASIS" if avg_ic["status"] == "PASS_MATURITY_BASIS"
            else "PASS"
        )
    """

    if avg_ic_status not in SUCCESSFUL_STATUSES:
        return {"status": "REVIEW_REQUIRED", "value": None, "error": f"average_invested_capital not ready (status={avg_ic_status})"}
    if nopat_status not in SUCCESSFUL_STATUSES:
        return {"status": "REVIEW_REQUIRED", "value": None, "error": f"nopat not ready (status={nopat_status})"}
    if avg_ic_value is None or avg_ic_value == 0:
        return {"status": "REVIEW_REQUIRED", "value": None, "error": "average_invested_capital value is zero/None"}

    roic_value = nopat_value / avg_ic_value
    status = (
        "PASS_DIRECT_AGGREGATE" if "PASS_DIRECT_AGGREGATE" in (avg_ic_status, nopat_status)
        else "PASS_NORMALIZED_TAX" if nopat_status == "PASS_NORMALIZED_TAX"
        else "PASS_MATURITY_BASIS" if avg_ic_status == "PASS_MATURITY_BASIS"
        else "PASS"
    )

    return {"status": status, "value": roic_value}
