"""User-directed (2026-08-12 session, D-076/D-077): re-run the quarterly
composite's predictive check on the FULL ~135-company universe, using the
full 8-factor weight table (QUARTERLY_FACTOR_WEIGHTS_FULL -- roic_level,
roic_trend, balance_sheet_strength_ratio now computable, D-076) instead of
the 5-factor version D-067/D-073 were limited to.

D-067's original composite (5 factors, 9 tickers) showed a positive
result in exactly 1 of 6 (lookback, horizon) cells tested, and excluding
ANY ONE of 6 of the 9 tickers erased it (D-073) -- the small ticker count
was always the stated bottleneck for the bootstrap. This script answers
the two things that changed: does a MUCH larger ticker count (a) still
show fragility on leave-one-out, and (b) does adding the 3 balance-sheet
factors change the result at all, on the SAME lookback/horizon grid
D-073 already used.

Same validation discipline as every prior check this session: company-
grouped block bootstrap (cohort_robustness_v1), never a naive pooled
correlation. Both the original 5-factor weights AND the new 8-factor
weights are run side by side on the identical dataset, so any difference
is attributable to the added factors, not a different sample.

READ-ONLY. Writes one result JSON. No new extraction, no engine run.

    .venv\\Scripts\\python.exe scripts\\215_quarterly_composite_full_universe_check.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.quarterly_composite_v1 import (
    QUARTERLY_FACTOR_WEIGHTS,
    QUARTERLY_FACTOR_WEIGHTS_FULL,
    compute_quarterly_composite_scores,
    compute_quarterly_factors,
)

RESULT_PATH = DATA_DIR / "quarterly_composite_full_universe_check_result.json"

LOOKBACKS = {"12_quarters": 12, "full_history": None}
HORIZONS = (6, 12, 24)
BASELINE_LOOKBACK_QUARTERS = 12
BASELINE_HORIZON_MONTHS = 12


def _candidate_quarters(connection: duckdb.DuckDBPyConnection, cutoff_date: str | None) -> list[tuple]:
    query = """
        SELECT DISTINCT qer.ticker, qmr.fiscal_year_end, qmr.fiscal_quarter, qmr.period_end, qmr.availability_date
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qmr.metric_name = 'revenue' AND qmr.period_end IS NOT NULL
    """
    params: list[str] = []
    if cutoff_date is not None:
        query += " AND qmr.availability_date >= ?"
        params.append(cutoff_date)
    query += " ORDER BY qer.ticker, qmr.availability_date"
    return connection.execute(query, params).fetchall()


def _build_dataset(connection, cutoff_date: str | None, horizon_months: int, factor_weights) -> list[dict]:
    quarters = _candidate_quarters(connection, cutoff_date)
    factor_rows = [
        compute_quarterly_factors(connection, ticker, fye, fq, period_end, availability_date)
        for ticker, fye, fq, period_end, availability_date in quarters
    ]
    scored = compute_quarterly_composite_scores(factor_rows, factor_weights=factor_weights)
    dataset = []
    for row in scored:
        if row["composite_score"] is None:
            continue
        stock_fwd = _forward_return(connection, row["ticker"], row["availability_date"], horizon_months)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, row["availability_date"], horizon_months)
        if stock_fwd is None or bench_fwd is None:
            continue
        dataset.append({**row, "excess_return": stock_fwd["return"] - bench_fwd["return"]})
    return dataset


def _leave_one_out_summary(connection, cutoff_date, horizon_months, factor_weights) -> dict:
    dataset = _build_dataset(connection, cutoff_date, horizon_months, factor_weights)
    tickers = sorted({r["ticker"] for r in dataset})
    full_bootstrap = block_bootstrap_correlation(
        dataset, "composite_score", "excess_return", group_key="ticker", n_resamples=5000, seed=13
    )
    n_crosses_zero_when_excluded = 0
    per_ticker = []
    for excluded in tickers:
        subset = [r for r in dataset if r["ticker"] != excluded]
        if len(subset) < 5 or len({r["ticker"] for r in subset}) < 2:
            continue
        bootstrap = block_bootstrap_correlation(
            subset, "composite_score", "excess_return", group_key="ticker", n_resamples=2000, seed=13
        )
        per_ticker.append({"excluded_ticker": excluded, "n": len(subset), "bootstrap": bootstrap})
        if bootstrap["ci_95_crosses_zero"]:
            n_crosses_zero_when_excluded += 1
    return {
        "n_tickers": len(tickers), "n_dataset": len(dataset), "full_sample_bootstrap": full_bootstrap,
        "n_leave_one_out_tests": len(per_ticker),
        "n_leave_one_out_crosses_zero": n_crosses_zero_when_excluded,
        "leave_one_out_detail": per_ticker,
    }


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    results: dict[str, object] = {"lookback_x_horizon": {"5_factor": [], "8_factor": []}, "leave_one_out": {}}

    print("=" * 100)
    print("Universe size check")
    print("=" * 100)
    all_quarters = _candidate_quarters(connection, None)
    all_tickers = sorted({t for t, *_ in all_quarters})
    print(f"total candidate company-quarters (full history): {len(all_quarters)}, {len(all_tickers)} distinct tickers")

    print("\n" + "=" * 100)
    print("1. Lookback x horizon grid, 5-factor weights (D-067's original set) vs 8-factor (D-076/D-077)")
    print("=" * 100)
    for weight_name, weights in [("5_factor", QUARTERLY_FACTOR_WEIGHTS), ("8_factor", QUARTERLY_FACTOR_WEIGHTS_FULL)]:
        for lookback_name, lookback_quarters in LOOKBACKS.items():
            cutoff_date = (
                (date.today() - timedelta(days=int(lookback_quarters * 365.25 / 4))).isoformat()
                if lookback_quarters is not None else None
            )
            for horizon in HORIZONS:
                dataset = _build_dataset(connection, cutoff_date, horizon, weights)
                n_groups = len({r["ticker"] for r in dataset})
                if len(dataset) < 5 or n_groups < 2:
                    print(f"[{weight_name}] {lookback_name:<14} horizon={horizon:>2}mo  n={len(dataset):<4}  too few rows/groups")
                    results["lookback_x_horizon"][weight_name].append({
                        "lookback": lookback_name, "horizon_months": horizon, "n": len(dataset), "note": "too few rows",
                    })
                    continue
                bootstrap = block_bootstrap_correlation(
                    dataset, "composite_score", "excess_return", group_key="ticker", n_resamples=5000, seed=17
                )
                flag = "CROSSES ZERO" if bootstrap["ci_95_crosses_zero"] else "does NOT cross zero"
                print(f"[{weight_name}] {lookback_name:<14} horizon={horizon:>2}mo  n={len(dataset):<4}  "
                      f"groups={n_groups:<4} corr={bootstrap['observed_correlation']:+.3f}  "
                      f"CI=[{bootstrap['ci_95_low']:+.3f}, {bootstrap['ci_95_high']:+.3f}]  {flag}")
                results["lookback_x_horizon"][weight_name].append({
                    "lookback": lookback_name, "horizon_months": horizon, "n": len(dataset), "n_groups": n_groups,
                    "bootstrap": bootstrap,
                })

    baseline_cutoff = (date.today() - timedelta(days=int(BASELINE_LOOKBACK_QUARTERS * 365.25 / 4))).isoformat()
    print("\n" + "=" * 100)
    print(f"2. Leave-one-ticker-out at the baseline ({BASELINE_LOOKBACK_QUARTERS}q lookback, {BASELINE_HORIZON_MONTHS}mo horizon)")
    print("=" * 100)
    for weight_name, weights in [("5_factor", QUARTERLY_FACTOR_WEIGHTS), ("8_factor", QUARTERLY_FACTOR_WEIGHTS_FULL)]:
        summary = _leave_one_out_summary(connection, baseline_cutoff, BASELINE_HORIZON_MONTHS, weights)
        fb = summary["full_sample_bootstrap"]
        flag = "CROSSES ZERO" if fb["ci_95_crosses_zero"] else "does NOT cross zero"
        print(f"[{weight_name}] FULL SAMPLE  n={summary['n_dataset']}  tickers={summary['n_tickers']}  "
              f"corr={fb['observed_correlation']:+.3f}  CI=[{fb['ci_95_low']:+.3f}, {fb['ci_95_high']:+.3f}]  {flag}")
        print(f"[{weight_name}] leave-one-out: {summary['n_leave_one_out_crosses_zero']} of "
              f"{summary['n_leave_one_out_tests']} single-ticker exclusions flip the CI to crossing zero")
        results["leave_one_out"][weight_name] = summary

    connection.close()
    RESULT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
