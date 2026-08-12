"""User-directed (2026-08-12 session): "one working parameter isn't
enough -- find at least 5 more effective parameters, then we'll weight
them." scripts/209 tested the 9 EXISTING Scoring Inputs V1 factors at
the 5-year horizon and found none clear bootstrap significance -- but
mostly because of small n (extraction-coverage gaps in the wide
universe, D-055's ~79.86% figure, compounded by needing a prior fiscal
year too), not necessarily a genuine absence of effect.

This script tries NEW candidate factors chosen for two properties that
should give them a much better shot at a trustworthy answer than
scripts/209's factors had:
  1. Large usable n -- built from data that's broadly available across
     the wide universe (price history, revenue, net_income), not from
     the sparser derived metrics that already showed coverage gaps.
  2. Standard, pre-committed economic direction -- not searched for.

Candidates:
  - earnings_yield  = net_income / market_cap (the INVERSE of D-068's
    raw P/E, in yield form -- defined even for UNPROFITABLE companies,
    unlike a P/E ratio, so it should recover far more of the wide
    universe than D-068's n=102).
  - fcf_yield       = free_cash_flow / market_cap (same yield-form
    logic, for cash generation instead of accounting earnings).
  - sales_yield     = revenue / market_cap (P/S inverted -- always
    defined, since revenue is never zero for an operating company).
  - book_yield      = stockholders_equity / market_cap (P/B inverted).
  - momentum_12m    = trailing 12-month price return ending at the
    filing date -- classic momentum factor, needs ONLY price history,
    so should have the largest n of anything tried this session.
  - size_log_revenue = log(revenue) -- classic size factor (small-cap
    premium / large-cap safety), also needs only data that's already
    essentially 100% covered.

market_cap is approximated as price * (net_income / diluted_eps) --
the SAME implied-share-count cross-check D-046 already validated (max
diff $0.03 across 45 company-years) before choosing not to store it as
its own metric; reused here as a genuine transient calculation, not a
new stored metric or a new accounting policy.

Direction for every yield factor is fixed a priori: HIGHER yield =
cheaper = conventionally better (matches D-068's P/E direction, just
inverted). Momentum and size directions are left unsigned in the report
(both directions are individually well documented in the academic
factor-investing literature; this project has no basis yet to
pre-commit one over the other) -- report is whatever it is.

READ-ONLY. Writes one result JSON.
"""

from __future__ import annotations

import json
import math

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.metrics import production_lookup
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.entry_price_v1 import _price_at_or_before
from stock_agent.scoring.predictive_analysis_v1 import spearman_correlation

RESULT_PATH = DATA_DIR / "wide_universe_5yr_new_factor_search_result.json"
HORIZON_MONTHS = 60
SUCCESS = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "PASS_NORMALIZED_TAX"}


def _metric(connection, ticker, report_date, metric_name):
    row = production_lookup.latest_metric(connection, ticker, report_date, metric_name)
    if row is None or row["status"] not in SUCCESS:
        return None
    return row["value"]


def _months_before(date_str: str, months: int) -> str:
    from datetime import date
    d = date.fromisoformat(date_str)
    month = d.month - 1 - months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, 28)
    return date(year, month, day).isoformat()


def _build_dataset(connection: duckdb.DuckDBPyConnection) -> list[dict]:
    company_years = production_lookup.all_company_years(connection)
    dataset = []
    for ticker, report_date, _acc in company_years:
        filing_row = connection.execute(
            "SELECT filing_date, fiscal_year FROM sec_filings WHERE ticker = ? AND report_date = ?", [ticker, report_date]
        ).fetchone()
        if filing_row is None:
            continue
        filing_date, fiscal_year = str(filing_row[0]), filing_row[1]

        stock_fwd = _forward_return(connection, ticker, filing_date, HORIZON_MONTHS)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, filing_date, HORIZON_MONTHS)
        if stock_fwd is None or bench_fwd is None:
            continue
        years = HORIZON_MONTHS / 12
        ann_stock = (1 + stock_fwd["return"]) ** (1 / years) - 1
        ann_qqq = (1 + bench_fwd["return"]) ** (1 / years) - 1
        excess_return = ann_stock - ann_qqq

        price, _ = _price_at_or_before(connection, ticker, filing_date)
        net_income = _metric(connection, ticker, report_date, "net_income")
        revenue = _metric(connection, ticker, report_date, "revenue")
        fcf = _metric(connection, ticker, report_date, "free_cash_flow")
        equity = _metric(connection, ticker, report_date, "stockholders_equity")

        diluted_eps_row = connection.execute(
            "SELECT diluted_eps FROM valuation_v1_per_share_inputs WHERE ticker = ? AND fiscal_year = ?",
            [ticker, fiscal_year],
        ).fetchone()
        diluted_eps = float(diluted_eps_row[0]) if diluted_eps_row and diluted_eps_row[0] else None

        record = {"ticker": ticker, "report_date": str(report_date), "filing_date": filing_date,
                   "excess_return": excess_return}

        implied_shares = None
        if diluted_eps and net_income is not None and diluted_eps != 0:
            implied_shares = net_income / diluted_eps
        market_cap = price * implied_shares if (price and implied_shares and implied_shares > 0) else None

        record["earnings_yield"] = (net_income / market_cap) if (market_cap and net_income is not None) else None
        record["fcf_yield"] = (fcf / market_cap) if (market_cap and fcf is not None) else None
        record["sales_yield"] = (revenue / market_cap) if (market_cap and revenue) else None
        record["book_yield"] = (equity / market_cap) if (market_cap and equity is not None and equity > 0) else None
        record["size_log_revenue"] = math.log(revenue) if revenue and revenue > 0 else None
        record["size_log_market_cap"] = math.log(market_cap) if market_cap and market_cap > 0 else None

        prior_date_12 = _months_before(filing_date, 12)
        prior_price_12, _ = _price_at_or_before(connection, ticker, prior_date_12)
        record["momentum_12m"] = (price / prior_price_12 - 1) if (price and prior_price_12) else None

        prior_date_24 = _months_before(filing_date, 24)
        prior_price_24, _ = _price_at_or_before(connection, ticker, prior_date_24)
        record["momentum_24m"] = (price / prior_price_24 - 1) if (price and prior_price_24) else None

        prior_date_36 = _months_before(filing_date, 36)
        prior_price_36, _ = _price_at_or_before(connection, ticker, prior_date_36)
        record["momentum_36m"] = (price / prior_price_36 - 1) if (price and prior_price_36) else None

        vol_rows = connection.execute(
            "SELECT adj_close FROM historical_prices_daily WHERE ticker = ? AND price_date <= ? "
            "ORDER BY price_date DESC LIMIT 253",
            [ticker, filing_date],
        ).fetchall()
        cash = _metric(connection, ticker, report_date, "cash_and_equivalents")
        record["cash_yield"] = (cash / market_cap) if (market_cap and cash is not None) else None

        total_debt = _metric(connection, ticker, report_date, "total_debt")
        record["debt_to_market_cap"] = (total_debt / market_cap) if (market_cap and total_debt is not None) else None

        record["is_profitable"] = (1.0 if net_income > 0 else 0.0) if net_income is not None else None

        trailing_start = _months_before(filing_date, 12)
        dividend_rows = connection.execute(
            "SELECT SUM(dividend) FROM historical_prices_daily WHERE ticker = ? AND price_date > ? AND price_date <= ? AND dividend IS NOT NULL",
            [ticker, trailing_start, filing_date],
        ).fetchone()
        trailing_dividends = float(dividend_rows[0]) if dividend_rows and dividend_rows[0] is not None else 0.0
        record["dividend_yield"] = (trailing_dividends / price) if price else None

        if len(vol_rows) >= 60:
            closes = [float(r[0]) for r in reversed(vol_rows)]
            daily_returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
            if len(daily_returns) >= 59:
                mean_r = sum(daily_returns) / len(daily_returns)
                variance = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
                record["volatility_12m"] = (variance ** 0.5) * (252 ** 0.5)  # annualized
            else:
                record["volatility_12m"] = None
        else:
            record["volatility_12m"] = None

        dataset.append(record)
    return dataset


FACTOR_NAMES = [
    "earnings_yield", "fcf_yield", "sales_yield", "book_yield", "cash_yield", "dividend_yield",
    "debt_to_market_cap", "is_profitable",
    "size_log_revenue", "size_log_market_cap",
    "momentum_12m", "momentum_24m", "momentum_36m", "volatility_12m",
]


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    dataset = _build_dataset(connection)
    connection.close()
    print(f"5-year-eligible company-years (any factor may still be missing per-row): {len(dataset)}")

    results = {}
    print(f"\n{'factor':<20}{'n':>6}{'groups':>8}{'corr':>9}{'ci_low':>9}{'ci_high':>9}  signal?")
    print("-" * 80)
    for factor in FACTOR_NAMES:
        rows = [r for r in dataset if r.get(factor) is not None]
        n_groups = len({r["ticker"] for r in rows})
        if len(rows) < 10:
            print(f"{factor:<20}{len(rows):>6}{n_groups:>8}   too few rows")
            results[factor] = {"n": len(rows), "n_groups": n_groups, "note": "too few rows"}
            continue
        plain = spearman_correlation([(r[factor], r["excess_return"]) for r in rows])
        bootstrap = block_bootstrap_correlation(rows, factor, "excess_return", group_key="ticker", n_resamples=5000, seed=31)
        signal = "REAL (CI excludes 0)" if not bootstrap["ci_95_crosses_zero"] else ""
        print(f"{factor:<20}{len(rows):>6}{n_groups:>8}{bootstrap['observed_correlation']:>+9.3f}"
              f"{bootstrap['ci_95_low']:>+9.3f}{bootstrap['ci_95_high']:>+9.3f}  {signal}")
        results[factor] = {"n": len(rows), "n_groups": n_groups, "plain_spearman": plain, "bootstrap": bootstrap}

    RESULT_PATH.write_text(json.dumps({"horizon_months": HORIZON_MONTHS, "factors": results}, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
