"""User-directed follow-up (2026-08-13 session) to scripts/216: the raw
quarterly YoY revenue growth rate (D-065's own "reference check", not the
acceleration factor D-065 was actually built to test) showed a real
result at full-universe scale -- corr +0.235, 95% CI [+0.063, +0.388],
and UNIQUELY among everything tested at quarterly cadence this session,
survived every single leave-one-ticker-out exclusion (0 of 86).

Before treating that as validated, apply the exact discipline that
already caught the quarterly composite's fragility (D-073/D-078): does
this hold across different lookback windows and forward horizons, or is
it specific to the one (12-quarter, 12-month) cell scripts/216 happened
to test? A real, broad effect should show up in multiple cells, not
exactly one -- that was the tell that the composite's apparent signal
was not real.

READ-ONLY. Writes one result JSON. No new extraction, no engine run.

    .venv\\Scripts\\python.exe scripts\\217_quarterly_growth_rate_robustness_check.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

RESULT_PATH = DATA_DIR / "quarterly_growth_rate_robustness_check_result.json"

LOOKBACKS = {"12_quarters": 12, "full_history": None}
HORIZONS = (6, 12, 24)


def _build_dataset(connection: duckdb.DuckDBPyConnection, cutoff_date: str | None, horizon_months: int) -> list[dict]:
    query = """
        SELECT DISTINCT qer.ticker, qmr.fiscal_year_end, qmr.fiscal_quarter, qmr.availability_date
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qmr.metric_name = 'revenue'
    """
    params: list[str] = []
    if cutoff_date is not None:
        query += " AND qmr.availability_date >= ?"
        params.append(cutoff_date)
    query += " ORDER BY qer.ticker, qmr.availability_date"
    quarters = connection.execute(query, params).fetchall()

    dataset = []
    for ticker, fiscal_year_end, fiscal_quarter, availability_date in quarters:
        availability_date = str(availability_date)
        factor = compute_revenue_growth_acceleration(connection, ticker, fiscal_year_end, fiscal_quarter)
        if factor["status"] != "PASS":
            continue
        stock_fwd = _forward_return(connection, ticker, availability_date, horizon_months)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, availability_date, horizon_months)
        if stock_fwd is None or bench_fwd is None:
            continue
        dataset.append({
            "ticker": ticker, "current_yoy_growth": factor["current_yoy_growth"],
            "excess_return": stock_fwd["return"] - bench_fwd["return"],
        })
    return dataset


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    results: dict[str, object] = {"lookback_x_horizon": []}

    print("=" * 100)
    print("Lookback x horizon grid: raw YoY revenue growth rate vs forward excess return")
    print("=" * 100)
    for lookback_name, lookback_quarters in LOOKBACKS.items():
        cutoff_date = (
            (date.today() - timedelta(days=int(lookback_quarters * 365.25 / 4))).isoformat()
            if lookback_quarters is not None else None
        )
        for horizon in HORIZONS:
            dataset = _build_dataset(connection, cutoff_date, horizon)
            n_groups = len({r["ticker"] for r in dataset})
            if len(dataset) < 5 or n_groups < 2:
                print(f"{lookback_name:<14} horizon={horizon:>2}mo  n={len(dataset):<4}  too few rows/groups")
                results["lookback_x_horizon"].append({
                    "lookback": lookback_name, "horizon_months": horizon, "n": len(dataset), "note": "too few rows",
                })
                continue
            bootstrap = block_bootstrap_correlation(
                dataset, "current_yoy_growth", "excess_return", group_key="ticker", n_resamples=5000, seed=23
            )
            flag = "CROSSES ZERO" if bootstrap["ci_95_crosses_zero"] else "does NOT cross zero"
            print(f"{lookback_name:<14} horizon={horizon:>2}mo  n={len(dataset):<4}  groups={n_groups:<4}  "
                  f"corr={bootstrap['observed_correlation']:+.3f}  "
                  f"CI=[{bootstrap['ci_95_low']:+.3f}, {bootstrap['ci_95_high']:+.3f}]  {flag}")
            results["lookback_x_horizon"].append({
                "lookback": lookback_name, "horizon_months": horizon, "n": len(dataset), "n_groups": n_groups,
                "bootstrap": bootstrap,
            })

    connection.close()
    RESULT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
