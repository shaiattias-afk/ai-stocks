"""
scoring/quarterly_composite_v1.py -- a quarterly-cadence composite score,
combining every factor from composite_v1's 9-factor model (D-057) that
CAN be computed at quarterly frequency from Quarterly Data V1 (D-042).

**2026-08-12 correction (D-076): the "structurally out of scope" claim
below was wrong.** Quarterly Data V1's 6 metrics never included balance-
sheet items, but the 10-Q filings themselves always have -- confirmed by
direct warehouse query. `extraction.quarterly_balance_sheet` (D-076/
D-077) now extracts `roic`, `adjusted_net_debt`, `stockholders_equity`
(and their supporting metrics) at quarterly cadence, reusing the annual
engine's own instant-fact resolvers unchanged. This module now computes
the full 9-factor set, matching `composite_v1`'s annual model exactly
(`QUARTERLY_FACTOR_WEIGHTS_FULL`). The original 5-factor
`QUARTERLY_FACTOR_WEIGHTS` is kept for backward comparability with
D-067/D-073's already-published result, not because the other 4 are
unavailable.

Historical note (left for context, no longer accurate as a scope
description): the original 6 Quarterly Data V1 metrics (revenue,
operating_income, operating_cash_flow, capex, pretax_income,
income_tax_expense) ruled out 4 of the annual composite's 9 factors --
`capex_discipline_deviation` was excluded by the user's own named
5-factor scope for the ORIGINAL build (D-065's request), not by a data
gap; it remains excluded from `QUARTERLY_FACTOR_WEIGHTS_FULL` for the
same reason (a self-referential "deviation from own trailing average"
measure, not a standalone quality/growth signal) -- this is the one
factor still deliberately left out of the 9, by choice, not availability.

The original 5 -- revenue_growth, operating_margin, fcf_margin,
fcf_growth, distance_from_high -- are computed here with formulas
IDENTICAL to inputs_v1.py's annual versions (fcf = operating_cash_flow -
capex, verified byte-identical against the annual production
free_cash_flow metric for AMZN FY2022 before this module was written),
just evaluated at quarterly cadence: revenue_growth/fcf_growth compare
the current quarter to the SAME quarter 4 quarters earlier (YoY, not
sequential -- avoids seasonality, same discipline as quarterly_trend_v1's
acceleration factor), and distance_from_high uses the quarter's own
`availability_date` as the point-in-time anchor instead of a 10-K's
`filing_date`.

**Ranking group -- calendar quarter, not fiscal_quarter label.** composite_
v1 ranks companies cross-sectionally WITHIN THE SAME FISCAL YEAR so a
company is never compared against a different macro period under the
guise of a same-sounding label. Quarterly Data V1's 9 tickers have
different fiscal-year-ends (NVDA/CRWD end late Jan, MU/PANW end Jul-Aug,
MSFT/ORCL end May-Jun, GOOGL/META/AMZN end Dec) -- their "Q1" labels do
NOT refer to the same 3 calendar months, so grouping by fiscal_quarter
would silently compare unrelated periods. Grouping by the CALENDAR
quarter each row's `period_end` actually falls in is the direct
analogue of composite_v1's own "same period" discipline, verified before
being adopted here (see scripts/205's own diagnostic output) to produce
usable cross-sections of 5-9 companies for most of the last 12 quarters,
not the single-company groups a stricter fiscal-label grouping would
produce.
"""

from __future__ import annotations

import duckdb

from stock_agent.scoring.composite_v1 import _percentile_ranks
from stock_agent.scoring.inputs_v1 import _trailing_high_and_price
from stock_agent.scoring.quarterly_trend_v1 import _quarters_before

# The original 5-factor set (D-065's requested scope) -- kept for
# backward comparability with the already-published D-067/D-073 result.
# Same weights composite_v1.FACTOR_WEIGHTS already assigns these exact 5
# factors -- reused unchanged, not reinvented.
QUARTERLY_FACTOR_WEIGHTS: dict[str, tuple[float, bool]] = {
    "revenue_growth": (0.20, False),
    "operating_margin": (0.10, False),
    "fcf_growth": (0.15, False),
    "fcf_margin": (0.10, False),
    "distance_from_high": (0.05, False),
}

# The full 9-factor set (D-076/D-077) -- byte-identical weights to
# composite_v1.FACTOR_WEIGHTS, minus capex_discipline_deviation (still
# deliberately excluded, see module docstring) and renormalized so the
# remaining 8 weights sum to 1.0 (0.05 of capex_discipline_deviation's
# weight redistributed proportionally across the other 8, same mechanism
# compute_quarterly_composite_scores already uses for a row missing any
# factor -- applied here at the WEIGHT-TABLE level since the exclusion is
# permanent, not per-row missing data).
_FULL_MINUS_CAPEX_TOTAL = 1.0 - 0.05  # composite_v1's capex_discipline_deviation weight
QUARTERLY_FACTOR_WEIGHTS_FULL: dict[str, tuple[float, bool]] = {
    "revenue_growth": (0.20 / _FULL_MINUS_CAPEX_TOTAL, False),
    "roic_level": (0.15 / _FULL_MINUS_CAPEX_TOTAL, False),
    "roic_trend": (0.10 / _FULL_MINUS_CAPEX_TOTAL, False),
    "operating_margin": (0.10 / _FULL_MINUS_CAPEX_TOTAL, False),
    "fcf_growth": (0.15 / _FULL_MINUS_CAPEX_TOTAL, False),
    "fcf_margin": (0.10 / _FULL_MINUS_CAPEX_TOTAL, False),
    "balance_sheet_strength_ratio": (0.10 / _FULL_MINUS_CAPEX_TOTAL, True),
    "distance_from_high": (0.05 / _FULL_MINUS_CAPEX_TOTAL, False),
}
assert abs(sum(w for w, _ in QUARTERLY_FACTOR_WEIGHTS_FULL.values()) - 1.0) < 1e-9, "full quarterly factor weights must sum to 100%"


def _metric(
    connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year_end: str, fiscal_quarter: str, metric_name: str
) -> float | None:
    row = connection.execute(
        """
        SELECT qmr.value FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qer.ticker = ? AND qmr.fiscal_year_end = ? AND qmr.fiscal_quarter = ?
          AND qmr.metric_name = ? AND qmr.value IS NOT NULL
        """,
        [ticker, fiscal_year_end, fiscal_quarter, metric_name],
    ).fetchone()
    return float(row[0]) if row is not None else None


def _free_cash_flow(
    connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year_end: str, fiscal_quarter: str
) -> float | None:
    """operating_cash_flow - capex -- the same formula the annual
    production `free_cash_flow` metric uses (verified byte-identical,
    AMZN FY2022: 46,752,000,000 - 63,645,000,000 = -16,893,000,000,
    matching production exactly)."""
    ocf = _metric(connection, ticker, fiscal_year_end, fiscal_quarter, "operating_cash_flow")
    capex = _metric(connection, ticker, fiscal_year_end, fiscal_quarter, "capex")
    if ocf is None or capex is None:
        return None
    return ocf - capex


def calendar_quarter_key(period_end: str) -> str:
    year, month = int(period_end[:4]), int(period_end[5:7])
    calendar_quarter = (month - 1) // 3 + 1
    return f"{year}Q{calendar_quarter}"


def compute_quarterly_factors(
    connection: duckdb.DuckDBPyConnection,
    ticker: str,
    fiscal_year_end: str,
    fiscal_quarter: str,
    period_end: str,
    availability_date: str,
) -> dict[str, object]:
    """Returns all 8 quarterly-computable raw factor values (None where
    unresolvable -- never guessed) plus the row's own calendar_quarter_key
    for later cross-sectional grouping. roic_level/roic_trend/balance_
    sheet_strength_ratio (D-076/D-077) read from quarterly_metric_results
    the same way the original 5 do -- they depend on extraction.
    quarterly_balance_sheet already having been run and loaded for this
    quarter and its YoY-prior; if not, they come back None, same as any
    other unresolvable factor."""
    revenue = _metric(connection, ticker, fiscal_year_end, fiscal_quarter, "revenue")
    operating_income = _metric(connection, ticker, fiscal_year_end, fiscal_quarter, "operating_income")
    fcf = _free_cash_flow(connection, ticker, fiscal_year_end, fiscal_quarter)

    result: dict[str, object] = {
        "ticker": ticker, "fiscal_year_end": fiscal_year_end, "fiscal_quarter": fiscal_quarter,
        "period_end": period_end, "availability_date": availability_date,
        "calendar_quarter_key": calendar_quarter_key(period_end),
    }

    result["operating_margin"] = (operating_income / revenue) if (operating_income is not None and revenue) else None
    result["fcf_margin"] = (fcf / revenue) if (fcf is not None and revenue) else None

    prior_quarters = _quarters_before(connection, ticker, fiscal_year_end, fiscal_quarter, 4)
    if len(prior_quarters) == 4:
        prior_fye, prior_q = prior_quarters[0]
        prior_revenue = _metric(connection, ticker, prior_fye, prior_q, "revenue")
        prior_fcf = _free_cash_flow(connection, ticker, prior_fye, prior_q)
        result["revenue_growth"] = (revenue / prior_revenue - 1) if (revenue is not None and prior_revenue) else None
        result["fcf_growth"] = (fcf / prior_fcf - 1) if (fcf is not None and prior_fcf) else None

        roic = _metric(connection, ticker, fiscal_year_end, fiscal_quarter, "roic")
        prior_roic = _metric(connection, ticker, prior_fye, prior_q, "roic")
        result["roic_trend"] = (roic - prior_roic) if (roic is not None and prior_roic is not None) else None
    else:
        result["revenue_growth"] = None
        result["fcf_growth"] = None
        result["roic_trend"] = None

    result["roic_level"] = _metric(connection, ticker, fiscal_year_end, fiscal_quarter, "roic")

    adjusted_net_debt = _metric(connection, ticker, fiscal_year_end, fiscal_quarter, "adjusted_net_debt")
    stockholders_equity = _metric(connection, ticker, fiscal_year_end, fiscal_quarter, "stockholders_equity")
    result["balance_sheet_strength_ratio"] = (
        (adjusted_net_debt / stockholders_equity) if (adjusted_net_debt is not None and stockholders_equity) else None
    )

    trailing_high, price_at_availability, price_date_used = _trailing_high_and_price(
        connection, ticker, availability_date
    )
    if trailing_high and price_at_availability:
        result["distance_from_high"] = (trailing_high - price_at_availability) / trailing_high
        result["distance_from_high_price_date"] = price_date_used
    else:
        result["distance_from_high"] = None

    return result


def compute_quarterly_composite_scores(
    rows: list[dict[str, object]], factor_weights: dict[str, tuple[float, bool]] | None = None
) -> list[dict[str, object]]:
    """`rows`: one dict per company-quarter, shaped like
    compute_quarterly_factors' return value (must carry `ticker` and
    `calendar_quarter_key` plus the factor values named in `factor_weights`
    -- pass QUARTERLY_FACTOR_WEIGHTS_FULL for all 8, or the module default
    QUARTERLY_FACTOR_WEIGHTS for the original 5). Cross-sectional
    percentile rank WITHIN THE SAME calendar_quarter_key -- see module
    docstring for why calendar quarter, not fiscal_quarter label, is the
    grouping unit. Same renormalize-by-weight-covered mechanism as
    composite_v1.compute_composite_scores_v1; a company-quarter missing a
    factor is excluded from that factor's ranking, never faked."""

    weights = factor_weights if factor_weights is not None else QUARTERLY_FACTOR_WEIGHTS
    total_weight = sum(w for w, _ in weights.values())

    by_period: dict[str, list[dict]] = {}
    for row in rows:
        by_period.setdefault(row["calendar_quarter_key"], []).append(row)

    results: list[dict[str, object]] = []
    for period_key, group in by_period.items():
        factor_ranks: dict[str, dict[str, float]] = {}
        for factor, (_weight, invert) in weights.items():
            raw_values = {row["ticker"]: row[factor] for row in group if row.get(factor) is not None}
            factor_ranks[factor] = _percentile_ranks(raw_values, invert)

        for row in group:
            ticker = row["ticker"]
            factor_scores: dict[str, float] = {}
            weighted_sum = 0.0
            weight_covered = 0.0
            for factor, (weight, _invert) in weights.items():
                score = factor_ranks[factor].get(ticker)
                if score is None:
                    continue
                factor_scores[factor] = score
                weighted_sum += score * weight
                weight_covered += weight

            composite = (weighted_sum / weight_covered) if weight_covered > 0 else None

            results.append({
                "ticker": ticker,
                "fiscal_year_end": row["fiscal_year_end"],
                "fiscal_quarter": row["fiscal_quarter"],
                "period_end": row["period_end"],
                "availability_date": row["availability_date"],
                "calendar_quarter_key": period_key,
                "composite_score": composite,
                "weight_covered": weight_covered / total_weight,
                "factors_used": len(factor_scores),
                "factor_scores": factor_scores,
            })

    return results
