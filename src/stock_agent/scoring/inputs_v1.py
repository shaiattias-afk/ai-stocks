"""
scoring/inputs_v1.py -- computes the 9 raw factor values from Scoring
Model V1's Stage 3 table (docs/SCORING_MODEL_V1_BLUEPRINT.md) for one
company-fiscal-year, reading ONLY already-frozen, point-in-time-gated
production data (Annual Data V1, Historical Prices V1). No new
extraction, no external data, nothing invented: a factor that cannot be
resolved from what is already on hand is returned as None/REVIEW_
REQUIRED, never guessed.

Every factor's own "earliest knowable date" (the blueprint's Stage 1
column) is the 10-K's own `filing_date` -- that is the single date used
throughout this module as the point-in-time evaluation anchor, including
for "distance from high" (computed only from prices on or before that
same filing_date, never a later one). This keeps every one of the 9
factors for one company-year anchored to the same, single, defensible
"as of" date.

**revenue_growth / operating_margin, universe-wide (2026-08-11
revision)**: originally read from `derived_metric_results` (Derived
Metrics V1, D-043), which is frozen at exactly the 9 original tickers.
Extending Scoring Model V1 to the wider ~150-company universe (D-051-
D-056) needed these computed directly from `financial_metric_results`
(`revenue`, `operating_income`) instead -- the SAME formula D-043 used
(`revenue_yoy_growth` = current/prior revenue - 1;
`operating_margin` = operating_income/revenue), just not gated to the
frozen 9. Verified byte-identical against the original 9 tickers'
`derived_metric_results` values before this replaced the old lookup
(see tests/scoring/test_inputs_v1.py) -- this is a widening of scope,
not a formula change, and D-043's own frozen table is untouched.
"""

from __future__ import annotations

import duckdb

from stock_agent.metrics import production_lookup

SUCCESSFUL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "PASS_NORMALIZED_TAX"}

CAPEX_DISCIPLINE_TRAILING_YEARS = 3


def _successful(status: str | None) -> bool:
    return status in SUCCESSFUL_STATUSES


def _metric(connection: duckdb.DuckDBPyConnection, ticker: str, report_date: str, metric_name: str) -> tuple[str | None, float | None]:
    looked_up = production_lookup.latest_metric(connection, ticker, report_date, metric_name)
    if looked_up is None:
        return None, None
    return looked_up["status"], looked_up["value"]


def _filing_info(connection: duckdb.DuckDBPyConnection, ticker: str, report_date: str) -> dict | None:
    row = connection.execute(
        "SELECT accession_number, filing_date FROM sec_filings "
        "WHERE ticker = ? AND report_date = ?",
        [ticker, report_date],
    ).fetchone()
    if row is None:
        return None
    return {"accession_number": row[0], "filing_date": str(row[1])}


def _prior_report_date(connection: duckdb.DuckDBPyConnection, ticker: str, report_date: str) -> str | None:
    """Deliberately NOT sec_filings.prior_report_date -- measured (this
    module's own small proof) to be off by one day for every non-first
    fiscal year of MU and NVDA (8 of 45 rows), silently breaking an
    exact-match lookup keyed on it. Uses the same robust mechanism
    metrics.annual.compute_full_company_year relies on: the largest of
    this ticker's own KNOWN annual report_dates that is strictly earlier
    than report_date -- derived from what filings actually exist, not a
    separately-stored (and here, sometimes wrong) column."""
    all_dates = [
        rd for (tkr, rd, _acc) in production_lookup.all_company_years(connection)
        if tkr == ticker
    ]
    return production_lookup.prior_report_date_for(ticker, report_date, all_dates)


def _trailing_high_and_price(
    connection: duckdb.DuckDBPyConnection, ticker: str, as_of_date: str
) -> tuple[float | None, float | None, str | None]:
    """Strictly trailing: only price_date <= as_of_date is ever read.
    Returns (trailing_high, price_on_or_before_as_of_date, price_date_used)."""
    high_row = connection.execute(
        "SELECT MAX(nominal_close) FROM historical_prices_daily "
        "WHERE ticker = ? AND price_date <= ?",
        [ticker, as_of_date],
    ).fetchone()
    trailing_high = float(high_row[0]) if high_row and high_row[0] is not None else None

    price_row = connection.execute(
        "SELECT price_date, nominal_close FROM historical_prices_daily "
        "WHERE ticker = ? AND price_date <= ? ORDER BY price_date DESC LIMIT 1",
        [ticker, as_of_date],
    ).fetchone()
    if price_row is None:
        return trailing_high, None, None
    return trailing_high, float(price_row[1]), str(price_row[0])


def compute_scoring_inputs_v1(
    connection: duckdb.DuckDBPyConnection, ticker: str, report_date: str
) -> dict[str, object]:
    """Returns a dict with the 9 raw factor values (or None where
    unresolvable) plus lineage (filing_date used as the evaluation
    anchor, prior_report_date, accession_number)."""

    filing = _filing_info(connection, ticker, report_date)
    if filing is None:
        raise ValueError(f"no sec_filings row for {ticker} {report_date}")
    filing_date = filing["filing_date"]
    prior_report_date = _prior_report_date(connection, ticker, report_date)

    result: dict[str, object] = {
        "ticker": ticker,
        "report_date": report_date,
        "filing_date": filing_date,
        "accession_number": filing["accession_number"],
        "prior_report_date": prior_report_date,
    }

    # 1. Revenue growth: current_fy_revenue / prior_fy_revenue - 1 (same
    # formula as Derived Metrics V1's revenue_yoy_growth, D-043, computed
    # directly here so it works universe-wide -- see module docstring).
    # Absent for a ticker's first fiscal year in the dataset (no prior
    # year to grow from), by design, not a defect.
    rev_status, rev_value = _metric(connection, ticker, report_date, "revenue")
    if prior_report_date and _successful(rev_status):
        prior_rev_status, prior_rev_value = _metric(connection, ticker, prior_report_date, "revenue")
        if _successful(prior_rev_status) and prior_rev_value:
            result["revenue_growth"] = rev_value / prior_rev_value - 1
            result["revenue_growth_status"] = "PASS"
        else:
            result["revenue_growth"] = None
            result["revenue_growth_status"] = "REVIEW_REQUIRED"
    else:
        result["revenue_growth"] = None
        result["revenue_growth_status"] = "REVIEW_REQUIRED" if prior_report_date else "NO_PRIOR_YEAR"

    # 2. ROIC (level).
    roic_status, roic_value = _metric(connection, ticker, report_date, "roic")
    result["roic_level"] = roic_value if _successful(roic_status) else None
    result["roic_level_status"] = roic_status or "UNAVAILABLE"

    # 3. ROIC trend (new): current - prior.
    if prior_report_date and _successful(roic_status):
        prior_roic_status, prior_roic_value = _metric(connection, ticker, prior_report_date, "roic")
        if _successful(prior_roic_status):
            result["roic_trend"] = roic_value - prior_roic_value
            result["roic_trend_status"] = "PASS"
        else:
            result["roic_trend"] = None
            result["roic_trend_status"] = "REVIEW_REQUIRED"
    else:
        result["roic_trend"] = None
        result["roic_trend_status"] = "REVIEW_REQUIRED" if prior_report_date else "NO_PRIOR_YEAR"

    # 4. Operating margin: operating_income / revenue (same formula as
    # Derived Metrics V1's operating_margin, D-043; see module docstring).
    op_income_status, op_income_value = _metric(connection, ticker, report_date, "operating_income")
    if _successful(op_income_status) and _successful(rev_status) and rev_value:
        result["operating_margin"] = op_income_value / rev_value
        result["operating_margin_status"] = "PASS"
    else:
        result["operating_margin"] = None
        result["operating_margin_status"] = "REVIEW_REQUIRED"

    # 5/6. FCF growth (new) and FCF margin (new).
    fcf_status, fcf_value = _metric(connection, ticker, report_date, "free_cash_flow")

    if _successful(fcf_status) and _successful(rev_status) and rev_value:
        result["fcf_margin"] = fcf_value / rev_value
        result["fcf_margin_status"] = "PASS"
    else:
        result["fcf_margin"] = None
        result["fcf_margin_status"] = "REVIEW_REQUIRED"

    if prior_report_date and _successful(fcf_status):
        prior_fcf_status, prior_fcf_value = _metric(connection, ticker, prior_report_date, "free_cash_flow")
        if _successful(prior_fcf_status) and prior_fcf_value:
            result["fcf_growth"] = fcf_value / prior_fcf_value - 1
            result["fcf_growth_status"] = "PASS"
        else:
            result["fcf_growth"] = None
            result["fcf_growth_status"] = "REVIEW_REQUIRED"
    else:
        result["fcf_growth"] = None
        result["fcf_growth_status"] = "REVIEW_REQUIRED" if prior_report_date else "NO_PRIOR_YEAR"

    # 7. Balance-sheet strength ratio (new): adjusted_net_debt / stockholders_equity.
    # Raw ratio only -- lower is stronger; inverted at the ranking stage.
    debt_status, debt_value = _metric(connection, ticker, report_date, "adjusted_net_debt")
    equity_status, equity_value = _metric(connection, ticker, report_date, "stockholders_equity")
    if _successful(debt_status) and _successful(equity_status) and equity_value:
        result["balance_sheet_strength_ratio"] = debt_value / equity_value
        result["balance_sheet_strength_ratio_status"] = "PASS"
    else:
        result["balance_sheet_strength_ratio"] = None
        result["balance_sheet_strength_ratio_status"] = "REVIEW_REQUIRED"

    # 8. CapEx discipline (new): |current capex/revenue - own trailing
    # N-year (N=3) average capex/revenue|, using ONLY prior fiscal years
    # (never the current one) for the trailing average -- point-in-time
    # safe by construction, and a genuine "deviation from established
    # pattern" rather than a self-referential comparison.
    capex_status, capex_value = _metric(connection, ticker, report_date, "capex")
    if _successful(capex_status) and _successful(rev_status) and rev_value:
        current_ratio = capex_value / rev_value
        trailing_ratios: list[float] = []
        cursor_report_date = prior_report_date
        while cursor_report_date and len(trailing_ratios) < CAPEX_DISCIPLINE_TRAILING_YEARS:
            c_status, c_value = _metric(connection, ticker, cursor_report_date, "capex")
            r_status, r_value = _metric(connection, ticker, cursor_report_date, "revenue")
            if _successful(c_status) and _successful(r_status) and r_value:
                trailing_ratios.append(c_value / r_value)
            cursor_report_date = _prior_report_date(connection, ticker, cursor_report_date)
        if trailing_ratios:
            trailing_avg = sum(trailing_ratios) / len(trailing_ratios)
            result["capex_discipline_deviation"] = abs(current_ratio - trailing_avg)
            result["capex_discipline_deviation_status"] = "PASS"
            result["capex_discipline_trailing_years_used"] = len(trailing_ratios)
        else:
            result["capex_discipline_deviation"] = None
            result["capex_discipline_deviation_status"] = "NO_PRIOR_YEAR"
            result["capex_discipline_trailing_years_used"] = 0
    else:
        result["capex_discipline_deviation"] = None
        result["capex_discipline_deviation_status"] = "REVIEW_REQUIRED"
        result["capex_discipline_trailing_years_used"] = 0

    # 9. Distance from high (new): trailing high computed ONLY from
    # price_date <= filing_date (never a later date) -- the strict
    # point-in-time safety the blueprint's Stage 2 flags as mandatory.
    trailing_high, price_at_filing, price_date_used = _trailing_high_and_price(connection, ticker, filing_date)
    if trailing_high and price_at_filing:
        result["distance_from_high"] = (trailing_high - price_at_filing) / trailing_high
        result["distance_from_high_status"] = "PASS"
        result["distance_from_high_price_date"] = price_date_used
        result["distance_from_high_trailing_high"] = trailing_high
        result["distance_from_high_price"] = price_at_filing
    else:
        result["distance_from_high"] = None
        result["distance_from_high_status"] = "REVIEW_REQUIRED"

    return result
