"""Tests the user's own idea (2026-08-12 session): does revenue growth
ACCELERATION (quarterly_trend_v1), measured every quarter over the
last 12 quarters (~3 years, deliberately outside the 2020-2021 COVID
window), predict 12-month forward excess return vs QQQ?

Small proof, on the 9 tickers Quarterly Data V1 (D-042) actually covers
-- 10-Q filings were never locked for the wider 143-company expansion,
so this cannot run on the full universe yet. If this shows real,
bootstrap-validated signal, extending 10-Q coverage becomes justified;
if not, that expensive extraction is avoided.

Every correlation reported here is run through the SAME company-
grouped block bootstrap used for the P/E finding (cohort_robustness_
v1) -- quarterly entries for one company are highly autocorrelated
with each other, so a naive row-by-row correlation would overstate
confidence exactly the way the annual P/E finding's naive count did.

READ-ONLY. Writes one result JSON.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

RESULT_PATH = DATA_DIR / "quarterly_trend_predictive_check_result.json"
LOOKBACK_QUARTERS = 12
HORIZON_MONTHS = 12


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    cutoff_date = (date.today() - timedelta(days=int(LOOKBACK_QUARTERS * 365.25 / 4))).isoformat()
    print(f"lookback cutoff (approx {LOOKBACK_QUARTERS} quarters back): {cutoff_date}")

    quarters = connection.execute(
        """
        SELECT DISTINCT qer.ticker, qmr.fiscal_year_end, qmr.fiscal_quarter, qmr.availability_date
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qmr.availability_date >= ?
        ORDER BY qer.ticker, qmr.availability_date
        """,
        [cutoff_date],
    ).fetchall()
    print(f"candidate quarterly entry points: {len(quarters)}")

    dataset = []
    status_counts: dict[str, int] = {}
    for ticker, fiscal_year_end, fiscal_quarter, availability_date in quarters:
        availability_date = str(availability_date)
        factor = compute_revenue_growth_acceleration(connection, ticker, fiscal_year_end, fiscal_quarter)
        status_counts[factor["status"]] = status_counts.get(factor["status"], 0) + 1
        if factor["status"] != "PASS":
            continue

        stock_fwd = _forward_return(connection, ticker, availability_date, HORIZON_MONTHS)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, availability_date, HORIZON_MONTHS)
        if stock_fwd is None or bench_fwd is None:
            continue

        dataset.append({
            "ticker": ticker, "fiscal_year_end": fiscal_year_end, "fiscal_quarter": fiscal_quarter,
            "availability_date": availability_date,
            "growth_acceleration": factor["value"],
            "current_yoy_growth": factor["current_yoy_growth"],
            "stock_return": stock_fwd["return"], "qqq_return": bench_fwd["return"],
            "excess_return": stock_fwd["return"] - bench_fwd["return"],
        })

    print(f"\nfactor resolution status counts: {status_counts}")
    print(f"usable dataset (factor + forward return both resolved): {len(dataset)}")

    if len(dataset) < 5:
        print("\nToo few usable rows for any correlation to be meaningful.")
        RESULT_PATH.write_text(json.dumps({"dataset": dataset, "note": "too few rows"}, indent=2, default=str), encoding="utf-8")
        return

    print("\nPer-entry detail:")
    for r in sorted(dataset, key=lambda r: r["excess_return"]):
        print(f"  {r['ticker']:<6} {r['fiscal_year_end']} {r['fiscal_quarter']}  "
              f"accel={r['growth_acceleration']:+.1%}  yoy_growth={r['current_yoy_growth']:+.1%}  "
              f"12mo_excess_return={r['excess_return']:+.1%}")

    print("\n" + "=" * 92)
    print(f"Block bootstrap (grouped by ticker): growth_acceleration vs {HORIZON_MONTHS}mo excess return")
    print("=" * 92)
    bootstrap_result = block_bootstrap_correlation(
        dataset, "growth_acceleration", "excess_return", group_key="ticker", n_resamples=5000, seed=7
    )
    print(json.dumps(bootstrap_result, indent=2))
    if bootstrap_result["ci_95_crosses_zero"]:
        print("\n-> 95% interval CROSSES ZERO: not (yet) a validated signal on this small proof.")
    else:
        print("\n-> 95% interval does NOT cross zero: real signal, even on this small 9-ticker proof --")
        print("   would justify extending 10-Q coverage to the wider universe.")

    # Also check the growth RATE itself (not just acceleration), as a
    # reference point -- Scoring Inputs V1's annual revenue_growth
    # already captures this at annual frequency; useful to see whether
    # acceleration adds anything beyond the raw quarterly growth rate.
    print("\n" + "=" * 92)
    print("Reference: raw YoY growth rate (not acceleration) vs excess return, same bootstrap")
    print("=" * 92)
    growth_rate_bootstrap = block_bootstrap_correlation(
        dataset, "current_yoy_growth", "excess_return", group_key="ticker", n_resamples=5000, seed=7
    )
    print(json.dumps(growth_rate_bootstrap, indent=2))

    connection.close()

    payload = {
        "lookback_quarters": LOOKBACK_QUARTERS, "horizon_months": HORIZON_MONTHS,
        "cutoff_date": cutoff_date, "status_counts": status_counts,
        "n": len(dataset), "acceleration_bootstrap": bootstrap_result,
        "growth_rate_bootstrap": growth_rate_bootstrap, "dataset": dataset,
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
