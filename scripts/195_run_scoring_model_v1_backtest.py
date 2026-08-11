"""Runs Scoring Model V1's point-in-time backtest
(stock_agent.scoring.backtest_v1) against the 9-company frozen universe
and QQQ (Nasdaq-100 benchmark), and writes the full result to
data/scoring_model_v1_backtest_result.json.

READ-ONLY -- writes no production table. This is a research/reporting
script, not a data loader: the backtest result is an analysis output,
not a per-company-year lineage-tracked production fact, so it does not
get a new production table (the project's existing pattern for
one-off/exploratory result JSON, e.g.
data/full_universe_remeasure_result.json).

*** See stock_agent.scoring.backtest_v1's own module docstring before
trusting any number this script prints -- severe survivorship bias by
construction (a hand-picked 9-company watchlist of already-successful
companies), small-sample statistics (5-6 independent annual entry
points), no compounded equity curve, no drawdown/volatility figure. ***

    .venv\\Scripts\\python.exe scripts\\195_run_scoring_model_v1_backtest.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import run_top_bottom_backtest_v1

RESULT_PATH = DATA_DIR / "scoring_model_v1_backtest_result.json"


def fmt(v):
    return f"{v:.1%}" if v is not None else "N/A"


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    result = run_top_bottom_backtest_v1(connection, top_n=3)
    connection.close()

    print("=" * 92)
    print("SCORING MODEL V1 BACKTEST -- top-3 vs bottom-3 vs QQQ (Nasdaq-100)")
    print("SEVERE SURVIVORSHIP BIAS: 9-company hand-picked watchlist, not a point-in-time universe.")
    print("=" * 92)
    print("\nHorizon summary (across independent annual entry points, NOT a compounded curve):")
    for horizon, summary in result["horizon_summary"].items():
        if summary["observations"] == 0:
            print(f"  {horizon:>2}mo: no complete observations")
            continue
        print(f"  {horizon:>2}mo: n={summary['observations']} "
              f"hit_rate_vs_QQQ={summary['hit_rate_vs_benchmark']:.0%} "
              f"mean_excess={fmt(summary['mean_excess_return'])} "
              f"median_excess={fmt(summary['median_excess_return'])}")

    print("\nPer-year detail:")
    for year in result["per_year"]:
        print(f"  FY{year['fiscal_year']} (universe={year['universe_size']}):")
        for horizon, h in year["horizons"].items():
            print(f"    {horizon:>2}mo: top={fmt(h['top_avg_return'])} bottom={fmt(h['bottom_avg_return'])} "
                  f"QQQ={fmt(h['benchmark_avg_return'])} top_excess={fmt(h['top_excess_vs_benchmark'])} "
                  f"top_minus_bottom={fmt(h['top_minus_bottom_spread'])}")

    RESULT_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
