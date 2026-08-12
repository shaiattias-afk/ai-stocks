"""Direct answer to the user's question (2026-08-12 session): the 5-year
P/E/size/dividend validation (scripts/208, 210) is structurally 100%
composed of 2020-2021 entries (a 5-year forward return needs an entry
>= 5 years before today, 2026-08-12) -- widening the company universe
did NOT fix the "one macro window" limitation D-064 already named, it
only added more companies WITHIN that same window. This script tests
the same 3 validated factors (raw P/E, log revenue, dividend yield) at
SHORTER horizons that open up genuinely different macro regimes: 24 and
36 months lets 2022-2023 entries in (rate-hike bear market), a real
regime change from the 2020-2021 COVID-recovery/AI-boom-onset window.

Reports the pooled bootstrap result AND a split by entry cohort
(pre-2022 vs 2022-onward) so the regime-dependence question is answered
directly, not just asserted.

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
from stock_agent.scoring.model_v2_candidate import _current_year_pe
from stock_agent.scoring.predictive_analysis_v1 import spearman_correlation

RESULT_PATH = DATA_DIR / "multi_regime_factor_check_result.json"
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


def _build_dataset(connection: duckdb.DuckDBPyConnection, horizon_months: int) -> list[dict]:
    company_years = production_lookup.all_company_years(connection)
    dataset = []
    for ticker, report_date, _acc in company_years:
        filing_row = connection.execute(
            "SELECT filing_date, fiscal_year FROM sec_filings WHERE ticker = ? AND report_date = ?", [ticker, report_date]
        ).fetchone()
        if filing_row is None:
            continue
        filing_date, fiscal_year = str(filing_row[0]), filing_row[1]

        stock_fwd = _forward_return(connection, ticker, filing_date, horizon_months)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, filing_date, horizon_months)
        if stock_fwd is None or bench_fwd is None:
            continue
        years = horizon_months / 12
        ann_stock = (1 + stock_fwd["return"]) ** (1 / years) - 1
        ann_qqq = (1 + bench_fwd["return"]) ** (1 / years) - 1
        excess_return = ann_stock - ann_qqq

        raw_pe = _current_year_pe(connection, ticker, fiscal_year)
        revenue = _metric(connection, ticker, report_date, "revenue")
        price, _ = _price_at_or_before(connection, ticker, filing_date)

        trailing_start = _months_before(filing_date, 12)
        dividend_row = connection.execute(
            "SELECT SUM(dividend) FROM historical_prices_daily WHERE ticker = ? AND price_date > ? AND price_date <= ? AND dividend IS NOT NULL",
            [ticker, trailing_start, filing_date],
        ).fetchone()
        trailing_dividends = float(dividend_row[0]) if dividend_row and dividend_row[0] is not None else 0.0
        dividend_yield = (trailing_dividends / price) if price else None

        dataset.append({
            "ticker": ticker, "filing_date": filing_date, "entry_year": int(filing_date[:4]),
            "excess_return": excess_return,
            "raw_pe": raw_pe,
            "size_log_revenue": math.log(revenue) if revenue and revenue > 0 else None,
            "dividend_yield": dividend_yield,
        })
    return dataset


def _bootstrap_report(label: str, rows: list[dict], factor: str) -> dict:
    usable = [r for r in rows if r.get(factor) is not None]
    n_groups = len({r["ticker"] for r in usable})
    if len(usable) < 10:
        print(f"  {label:<45}{factor:<18} n={len(usable):<4} too few rows")
        return {"n": len(usable), "n_groups": n_groups, "note": "too few rows"}
    plain = spearman_correlation([(r[factor], r["excess_return"]) for r in usable])
    bootstrap = block_bootstrap_correlation(usable, factor, "excess_return", group_key="ticker", n_resamples=4000, seed=41)
    signal = "REAL" if not bootstrap["ci_95_crosses_zero"] else "crosses zero"
    print(f"  {label:<45}{factor:<18} n={len(usable):<4} groups={n_groups:<4} "
          f"corr={bootstrap['observed_correlation']:+.3f} CI=[{bootstrap['ci_95_low']:+.3f},{bootstrap['ci_95_high']:+.3f}]  {signal}")
    return {"n": len(usable), "n_groups": n_groups, "plain_spearman": plain, "bootstrap": bootstrap}


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    results = {}

    for horizon_months in (60, 36, 24):
        dataset = _build_dataset(connection, horizon_months)
        from collections import Counter
        year_spread = dict(sorted(Counter(r["entry_year"] for r in dataset).items()))
        print(f"\n{'=' * 100}\nHORIZON = {horizon_months} months, n={len(dataset)}, entry-year spread={year_spread}\n{'=' * 100}")

        pre_2022 = [r for r in dataset if r["entry_year"] < 2022]
        from_2022 = [r for r in dataset if r["entry_year"] >= 2022]
        print(f"  pre-2022 entries: {len(pre_2022)}   2022-onward entries: {len(from_2022)}")

        results[f"{horizon_months}mo"] = {"year_spread": year_spread, "n": len(dataset), "factors": {}}
        for factor in ("raw_pe", "size_log_revenue", "dividend_yield"):
            results[f"{horizon_months}mo"]["factors"][factor] = {
                "pooled": _bootstrap_report(f"[{horizon_months}mo] POOLED (all years)", dataset, factor),
                "pre_2022_only": _bootstrap_report(f"[{horizon_months}mo] PRE-2022 entries only", pre_2022, factor),
                "2022_onward_only": _bootstrap_report(f"[{horizon_months}mo] 2022-ONWARD entries only", from_2022, factor),
            }

    connection.close()
    RESULT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
