"""
policies/tax_normalization.py — D-027 Policy D: normalized 21% tax
rate for NOPAT when pretax_income<=0, or the reported effective tax
rate falls outside [0, 1], but pretax_income/income_tax_expense/
operating_income are all independently PASS (valid, unambiguous
source facts).

Scope note (D-020, out of scope for this port): pretax_income's own
ROW IDENTIFICATION — including D-020's structural fallback for filings
whose pretax-income row uses non-standard label wording (CRWD/PANW
"Loss before...", GOOGL's bare "Total" row, MU's middle-clause
variant) — lives in scripts/69_xbrl_metric_engine.py, which together
with scripts/60 is explicitly out of scope for this PR (per the task
brief: "standalone live-Arelle engines... not part of your scope —
leave them as historical/archived"). pretax_income, income_tax_expense,
and operating_income are always sourced, in the live
`financial_metric_results` table, from scripts/60 (engine v14) or
scripts/69 (engine v15) — confirmed by direct query (n_engines=2 for
all three metric names across all 45 company-years, zero exceptions).
This module therefore takes pretax_income/income_tax_expense/
operating_income as already-resolved PASS inputs (read via
metrics.production_lookup.latest_metric, exactly as scripts/94 itself
did) rather than re-deriving their row identification.

Ported byte-exact (the normalization condition and NOPAT formula) from
scripts/94_normalized_tax_policy.py's `main()` loop body, factored into
a standalone function so it is callable without a database write loop
— a structural move only; the expressions themselves are unchanged.
"""

from __future__ import annotations

NORMALIZED_TAX_RATE = 0.21


def compute_normalized_tax_nopat(
    pretax_value: float,
    tax_expense_value: float,
    operating_income_value: float,
) -> dict[str, object] | None:
    """
    Mirrors scripts/94_normalized_tax_policy.py's main() body exactly:

        reported_rate = (
            tax_expense_value / pretax_value if pretax_value not in (0, None) else None
        )
        needs_normalization = (
            pretax_value <= 0
            or (reported_rate is not None and (reported_rate < 0 or reported_rate > 1))
        )
        if not needs_normalization:
            continue  # genuinely a different, independent blocker — not this policy's job
        normalized_nopat = operating_income_value * (1 - NORMALIZED_TAX_RATE)

    Returns None when normalization is not needed (the original script's
    `continue`) — callers should fall through to whatever the reported
    effective_tax_rate/nopat already are in that case.
    """

    reported_rate = (
        tax_expense_value / pretax_value if pretax_value not in (0, None) else None
    )

    needs_normalization = (
        pretax_value <= 0
        or (reported_rate is not None and (reported_rate < 0 or reported_rate > 1))
    )

    if not needs_normalization:
        return None

    normalized_nopat = operating_income_value * (1 - NORMALIZED_TAX_RATE)

    return {
        "status": "PASS_NORMALIZED_TAX",
        "effective_tax_rate": NORMALIZED_TAX_RATE,
        "nopat": normalized_nopat,
        "reported_rate": reported_rate,
        "basis": "FIXED_NORMALIZED_TAX_RATE_21_PERCENT",
    }
