"""User follow-up to D-067 (2026-08-12 session): "I want more information
to understand if we're on the right path." Two things requested together
-- this script is the FIRST (fast, no new extraction): does the D-067
quarterly-composite signal survive under different lookback windows and
different forward-return horizons, using data already on hand? The
SECOND (extending 10-Q coverage to new tickers) is a separate, real
extraction effort -- see scripts/207+.

Three checks, all reusing quarterly_composite_v1 and cohort_robustness_v1
unchanged:

1. **Lookback sensitivity**: D-067 used a 12-quarter (~3yr) cutoff. Does
   the signal look different using the FULL available history per ticker
   (back to 2019 for some) instead? More quarters per ticker does NOT
   add independent bootstrap groups (still 9 tickers) but does change
   which calendar-quarter cross-sections exist to rank within.
2. **Horizon sensitivity**: D-067 used 12 months (chosen for the 5-year-
   back-cap reason documented there). Repeats the exact same bootstrap
   at 6 and 24 months to see if the result is a 12-month-specific
   artifact or holds across horizons.
3. **Leave-one-ticker-out**: D-067 already showed MU alone flips the
   full-sample CI to crossing zero. Repeats that for EVERY ticker (not
   just MU) at the D-067 baseline (12q lookback, 12mo horizon) -- if
   several tickers each individually flip the result, that's a stronger
   signal of overall fragility than an MU-specific story; if only MU
   does it, that's a more precise, narrower caveat.

READ-ONLY. Writes one result JSON. No new filings locked, no engine run.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.predictive_analysis_v1 import spearman_correlation
from stock_agent.scoring.quarterly_composite_v1 import compute_quarterly_composite_scores, compute_quarterly_factors

RESULT_PATH = DATA_DIR / "quarterly_composite_robustness_check_result.json"

LOOKBACKS = {"12_quarters": 12, "full_history": None}
HORIZONS = (6, 12, 24)


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


def _build_dataset(connection, cutoff_date: str | None, horizon_months: int) -> list[dict]:
    quarters = _candidate_quarters(connection, cutoff_date)
    factor_rows = [
        compute_quarterly_factors(connection, ticker, fye, fq, period_end, availability_date)
        for ticker, fye, fq, period_end, availability_date in quarters
    ]
    scored = compute_quarterly_composite_scores(factor_rows)
    dataset = []
    for row in scored:
        if row["composite_score"] is None:
            continue
        stock_fwd = _forward_return(connection, row["ticker"], row["availability_date"], horizon_months)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, row["availability_date"], horizon_months)
        if stock_fwd is None or bench_fwd is None:
            continue
        dataset.append({
            **row,
            "excess_return": stock_fwd["return"] - bench_fwd["return"],
        })
    return dataset


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    results: dict[str, object] = {"lookback_x_horizon": [], "leave_one_ticker_out": [], "per_factor_correlation_baseline": {}}

    print("=" * 100)
    print("1. Lookback x horizon sensitivity grid")
    print("=" * 100)
    for lookback_name, lookback_quarters in LOOKBACKS.items():
        cutoff_date = (
            (date.today() - timedelta(days=int(lookback_quarters * 365.25 / 4))).isoformat()
            if lookback_quarters is not None else None
        )
        for horizon in HORIZONS:
            dataset = _build_dataset(connection, cutoff_date, horizon)
            if len(dataset) < 5:
                print(f"{lookback_name:<14} horizon={horizon:>2}mo  n={len(dataset):<3}  too few rows")
                results["lookback_x_horizon"].append({
                    "lookback": lookback_name, "horizon_months": horizon, "n": len(dataset), "note": "too few rows",
                })
                continue
            bootstrap = block_bootstrap_correlation(
                dataset, "composite_score", "excess_return", group_key="ticker", n_resamples=5000, seed=17
            )
            flag = "CROSSES ZERO" if bootstrap["ci_95_crosses_zero"] else "does NOT cross zero"
            print(f"{lookback_name:<14} horizon={horizon:>2}mo  n={len(dataset):<3}  "
                  f"corr={bootstrap['observed_correlation']:+.3f}  "
                  f"CI=[{bootstrap['ci_95_low']:+.3f}, {bootstrap['ci_95_high']:+.3f}]  {flag}")
            results["lookback_x_horizon"].append({
                "lookback": lookback_name, "horizon_months": horizon, "n": len(dataset),
                "bootstrap": bootstrap,
            })

    print("\n" + "=" * 100)
    print("2. Leave-one-ticker-out at the D-067 baseline (12 quarters, 12 months)")
    print("=" * 100)
    baseline_cutoff = (date.today() - timedelta(days=int(12 * 365.25 / 4))).isoformat()
    baseline_dataset = _build_dataset(connection, baseline_cutoff, 12)
    tickers = sorted({r["ticker"] for r in baseline_dataset})
    full_bootstrap = block_bootstrap_correlation(
        baseline_dataset, "composite_score", "excess_return", group_key="ticker", n_resamples=5000, seed=13
    )
    print(f"FULL SAMPLE  n={len(baseline_dataset)}  corr={full_bootstrap['observed_correlation']:+.3f}  "
          f"CI=[{full_bootstrap['ci_95_low']:+.3f}, {full_bootstrap['ci_95_high']:+.3f}]")
    for excluded in tickers:
        subset = [r for r in baseline_dataset if r["ticker"] != excluded]
        bootstrap = block_bootstrap_correlation(
            subset, "composite_score", "excess_return", group_key="ticker", n_resamples=5000, seed=13
        )
        flag = "CROSSES ZERO" if bootstrap["ci_95_crosses_zero"] else "does NOT cross zero"
        print(f"  excl. {excluded:<6} n={len(subset):<3}  corr={bootstrap['observed_correlation']:+.3f}  "
              f"CI=[{bootstrap['ci_95_low']:+.3f}, {bootstrap['ci_95_high']:+.3f}]  {flag}")
        results["leave_one_ticker_out"].append({
            "excluded_ticker": excluded, "n": len(subset), "bootstrap": bootstrap,
        })
    results["leave_one_ticker_out_full_sample"] = full_bootstrap

    print("\n" + "=" * 100)
    print("3. Per-factor correlation at the D-067 baseline (which factor(s) actually drive it)")
    print("=" * 100)
    factor_names = ["revenue_growth", "operating_margin", "fcf_margin", "fcf_growth", "distance_from_high"]
    quarters = _candidate_quarters(connection, baseline_cutoff)
    factor_rows = [
        compute_quarterly_factors(connection, t, fye, fq, pe, ad) for t, fye, fq, pe, ad in quarters
    ]
    factor_rows_by_key = {(r["ticker"], r["fiscal_year_end"], r["fiscal_quarter"]): r for r in factor_rows}
    for factor in factor_names:
        rows_with_return = []
        for r in baseline_dataset:
            key = (r["ticker"], r["fiscal_year_end"], r["fiscal_quarter"])
            raw_row = factor_rows_by_key.get(key)
            if raw_row is None or raw_row.get(factor) is None:
                continue
            rows_with_return.append({"ticker": r["ticker"], "raw_value": raw_row[factor], "excess_return": r["excess_return"]})
        if len(rows_with_return) < 5:
            print(f"  {factor:<20} too few rows")
            continue
        bootstrap = block_bootstrap_correlation(
            rows_with_return, "raw_value", "excess_return", group_key="ticker", n_resamples=5000, seed=19
        )
        flag = "CROSSES ZERO" if bootstrap["ci_95_crosses_zero"] else "does NOT cross zero"
        print(f"  {factor:<20} n={len(rows_with_return):<3}  corr={bootstrap['observed_correlation']:+.3f}  "
              f"CI=[{bootstrap['ci_95_low']:+.3f}, {bootstrap['ci_95_high']:+.3f}]  {flag}")
        results["per_factor_correlation_baseline"][factor] = {"n": len(rows_with_return), "bootstrap": bootstrap}

    connection.close()
    RESULT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
