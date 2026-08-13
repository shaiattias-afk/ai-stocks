"""User-directed follow-up (2026-08-13 session) to D-079 (the quarterly
raw YoY revenue growth rate finding, scripts/216-217): before treating
it as usable, apply the same two checks this project already learned it
needs the hard way for the P/E finding --

1. **Regime split** (same method D-074/scripts/211 used for P/E): split
   entries by entry_year (< 2022 vs >= 2022 -- pre- vs post-rate-hike
   regime) and check the correlation holds in BOTH sub-periods
   separately, not just pooled. D-074 found the P/E finding's flagship
   validation was accidentally 100% pre-2022 entries -- this checks
   whether the same blind spot applies here (it should not: this
   factor's forward horizon is only 6-12 months, so entries from both
   regimes are already eligible, unlike P/E's 5-year requirement).
2. **Quintile breakdown** (same method D-068 used for P/E): sort
   entries into 5 equal-sized buckets by growth rate and report mean/
   median excess return and win rate per bucket -- reveals whether the
   effect is smooth/linear across the whole range, or (like P/E)
   concentrated in avoiding one bad tail rather than chasing the best.

Uses the D-079 baseline cell (12-quarter lookback, 12-month horizon,
raw YoY growth rate) -- the cell with the cleanest, most-tested result.

READ-ONLY. Writes one result JSON. No new extraction, no engine run.

    .venv\\Scripts\\python.exe scripts\\218_quarterly_growth_rate_regime_and_quintile_check.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

RESULT_PATH = DATA_DIR / "quarterly_growth_rate_regime_and_quintile_check_result.json"
LOOKBACK_QUARTERS = 12
HORIZON_MONTHS = 12
N_QUINTILES = 5


def _build_dataset(connection: duckdb.DuckDBPyConnection, cutoff_date: str) -> list[dict]:
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
    for ticker, fiscal_year_end, fiscal_quarter, availability_date in quarters:
        availability_date = str(availability_date)
        factor = compute_revenue_growth_acceleration(connection, ticker, fiscal_year_end, fiscal_quarter)
        if factor["status"] != "PASS":
            continue
        stock_fwd = _forward_return(connection, ticker, availability_date, HORIZON_MONTHS)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, availability_date, HORIZON_MONTHS)
        if stock_fwd is None or bench_fwd is None:
            continue
        dataset.append({
            "ticker": ticker, "availability_date": availability_date, "entry_year": int(availability_date[:4]),
            "current_yoy_growth": factor["current_yoy_growth"], "excess_return": stock_fwd["return"] - bench_fwd["return"],
        })
    return dataset


def _bootstrap_report(label: str, rows: list[dict]) -> dict | None:
    n_groups = len({r["ticker"] for r in rows})
    if len(rows) < 5 or n_groups < 2:
        print(f"  {label:<28} n={len(rows):<4} too few rows/groups")
        return None
    bootstrap = block_bootstrap_correlation(
        rows, "current_yoy_growth", "excess_return", group_key="ticker", n_resamples=5000, seed=29
    )
    flag = "CROSSES ZERO" if bootstrap["ci_95_crosses_zero"] else "does NOT cross zero"
    print(f"  {label:<28} n={len(rows):<4} groups={n_groups:<4} corr={bootstrap['observed_correlation']:+.3f}  "
          f"CI=[{bootstrap['ci_95_low']:+.3f}, {bootstrap['ci_95_high']:+.3f}]  {flag}")
    return {"n": len(rows), "n_groups": n_groups, "bootstrap": bootstrap}


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    cutoff_date = (date.today() - timedelta(days=int(LOOKBACK_QUARTERS * 365.25 / 4))).isoformat()
    dataset = _build_dataset(connection, cutoff_date)
    from collections import Counter
    year_spread = dict(sorted(Counter(r["entry_year"] for r in dataset).items()))
    print(f"baseline dataset: n={len(dataset)}, {len({r['ticker'] for r in dataset})} tickers, "
          f"entry-year spread={year_spread}")

    print("\n" + "=" * 100)
    print("1. Regime split -- pre-2022 vs 2022-onward entries")
    print("=" * 100)
    pooled = _bootstrap_report("POOLED (all entries)", dataset)
    pre_2022 = [r for r in dataset if r["entry_year"] < 2022]
    from_2022 = [r for r in dataset if r["entry_year"] >= 2022]
    pre_2022_report = _bootstrap_report("pre-2022 entries only", pre_2022)
    from_2022_report = _bootstrap_report("2022-onward entries only", from_2022)

    print("\n" + "=" * 100)
    print(f"2. Quintile breakdown ({N_QUINTILES} buckets by growth rate)")
    print("=" * 100)
    sorted_rows = sorted(dataset, key=lambda r: r["current_yoy_growth"])
    n = len(sorted_rows)
    quintile_size = n // N_QUINTILES
    quintiles = []
    for i in range(N_QUINTILES):
        start = i * quintile_size
        end = (i + 1) * quintile_size if i < N_QUINTILES - 1 else n
        bucket = sorted_rows[start:end]
        growth_values = [r["current_yoy_growth"] for r in bucket]
        excess_values = [r["excess_return"] for r in bucket]
        mean_excess = sum(excess_values) / len(excess_values)
        win_rate = sum(1 for e in excess_values if e > 0) / len(excess_values)
        quintiles.append({
            "quintile": i + 1, "n": len(bucket),
            "growth_range": [min(growth_values), max(growth_values)],
            "mean_excess_return": mean_excess, "win_rate": win_rate,
        })
        label = f"Q{i + 1}" + (" (lowest growth)" if i == 0 else " (highest growth)" if i == N_QUINTILES - 1 else "")
        print(f"  {label:<22} n={len(bucket):<4} growth=[{min(growth_values):+.1%}, {max(growth_values):+.1%}]  "
              f"mean_excess={mean_excess:+.1%}/yr  win_rate={win_rate:.0%}")

    connection.close()

    payload = {
        "lookback_quarters": LOOKBACK_QUARTERS, "horizon_months": HORIZON_MONTHS, "cutoff_date": cutoff_date,
        "n": len(dataset), "entry_year_spread": year_spread,
        "pooled": pooled, "pre_2022_only": pre_2022_report, "2022_onward_only": from_2022_report,
        "quintiles": quintiles, "dataset": dataset,
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
