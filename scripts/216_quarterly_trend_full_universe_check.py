"""User-directed (2026-08-13 session): re-run D-065's quarterly revenue-
growth-acceleration factor on the FULL universe now that Quarterly Data
covers ~99-135 tickers (D-072), not just the original 9 -- the same
"was it just too few tickers" question D-078 already answered for the
quarterly composite, applied to the one other quarterly-cadence factor
this project has tested (D-065).

`quarterly_trend_v1.compute_revenue_growth_acceleration` and script 203's
predictive-check logic are entirely ticker-agnostic already (no 9-ticker
assumption baked into either) -- this script is that same logic, run on
whatever quarterly_metric_results now actually contains, plus the
leave-one-ticker-out robustness check D-073/D-078 already established as
this project's standard once a universe is large enough to run it on.

Tests BOTH growth acceleration (D-065's primary factor) and the raw YoY
growth rate (D-065's own reference check) side by side, same as before.

READ-ONLY. Writes one result JSON. No new extraction, no engine run.

    .venv\\Scripts\\python.exe scripts\\216_quarterly_trend_full_universe_check.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

RESULT_PATH = DATA_DIR / "quarterly_trend_full_universe_check_result.json"
LOOKBACK_QUARTERS = 12
HORIZON_MONTHS = 12


def _build_dataset(connection: duckdb.DuckDBPyConnection, cutoff_date: str) -> tuple[list[dict], dict]:
    quarters = connection.execute(
        """
        SELECT DISTINCT qer.ticker, qmr.fiscal_year_end, qmr.fiscal_quarter, qmr.availability_date
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qmr.availability_date >= ? AND qmr.metric_name = 'revenue'
        ORDER BY qer.ticker, qmr.availability_date
        """,
        [cutoff_date],
    ).fetchall()

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
            "growth_acceleration": factor["value"], "current_yoy_growth": factor["current_yoy_growth"],
            "excess_return": stock_fwd["return"] - bench_fwd["return"],
        })
    return dataset, status_counts


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    cutoff_date = (date.today() - timedelta(days=int(LOOKBACK_QUARTERS * 365.25 / 4))).isoformat()
    print(f"lookback cutoff (approx {LOOKBACK_QUARTERS} quarters back): {cutoff_date}")

    dataset, status_counts = _build_dataset(connection, cutoff_date)
    tickers = sorted({r["ticker"] for r in dataset})
    print(f"factor resolution status counts: {status_counts}")
    print(f"usable dataset (factor + forward return both resolved): {len(dataset)}, {len(tickers)} distinct tickers "
          f"(D-065's original proof: 57 rows, 9 tickers)")

    if len(dataset) < 5 or len(tickers) < 2:
        print("\nToo few usable rows/tickers for any correlation to be meaningful.")
        RESULT_PATH.write_text(json.dumps({"dataset": dataset, "note": "too few rows"}, indent=2, default=str), encoding="utf-8")
        return

    print("\n" + "=" * 92)
    print(f"1. Block bootstrap (grouped by ticker): growth_acceleration vs {HORIZON_MONTHS}mo excess return")
    print("=" * 92)
    accel_bootstrap = block_bootstrap_correlation(
        dataset, "growth_acceleration", "excess_return", group_key="ticker", n_resamples=5000, seed=7
    )
    flag = "CROSSES ZERO" if accel_bootstrap["ci_95_crosses_zero"] else "does NOT cross zero"
    print(f"n={len(dataset)} groups={len(tickers)} corr={accel_bootstrap['observed_correlation']:+.3f} "
          f"CI=[{accel_bootstrap['ci_95_low']:+.3f}, {accel_bootstrap['ci_95_high']:+.3f}]  {flag}")

    print("\n" + "=" * 92)
    print("2. Reference: raw YoY growth rate (not acceleration) vs excess return, same bootstrap")
    print("=" * 92)
    growth_rate_bootstrap = block_bootstrap_correlation(
        dataset, "current_yoy_growth", "excess_return", group_key="ticker", n_resamples=5000, seed=7
    )
    flag = "CROSSES ZERO" if growth_rate_bootstrap["ci_95_crosses_zero"] else "does NOT cross zero"
    print(f"n={len(dataset)} groups={len(tickers)} corr={growth_rate_bootstrap['observed_correlation']:+.3f} "
          f"CI=[{growth_rate_bootstrap['ci_95_low']:+.3f}, {growth_rate_bootstrap['ci_95_high']:+.3f}]  {flag}")

    print("\n" + "=" * 92)
    print("3. Leave-one-ticker-out (both factors) -- same discipline D-073/D-078 already applied to the composite")
    print("=" * 92)
    leave_one_out: dict[str, list[dict]] = {"growth_acceleration": [], "current_yoy_growth": []}
    for factor_name in ("growth_acceleration", "current_yoy_growth"):
        n_flip = 0
        for excluded in tickers:
            subset = [r for r in dataset if r["ticker"] != excluded]
            if len(subset) < 5 or len({r["ticker"] for r in subset}) < 2:
                continue
            bootstrap = block_bootstrap_correlation(
                subset, factor_name, "excess_return", group_key="ticker", n_resamples=2000, seed=7
            )
            leave_one_out[factor_name].append({"excluded_ticker": excluded, "n": len(subset), "bootstrap": bootstrap})
            if bootstrap["ci_95_crosses_zero"]:
                n_flip += 1
        print(f"  {factor_name:<20} {n_flip} of {len(leave_one_out[factor_name])} single-ticker exclusions "
              f"flip the CI to crossing zero")

    connection.close()

    payload = {
        "lookback_quarters": LOOKBACK_QUARTERS, "horizon_months": HORIZON_MONTHS, "cutoff_date": cutoff_date,
        "status_counts": status_counts, "n": len(dataset), "n_tickers": len(tickers),
        "acceleration_bootstrap": accel_bootstrap, "growth_rate_bootstrap": growth_rate_bootstrap,
        "leave_one_out": leave_one_out, "dataset": dataset,
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
