"""Direct follow-up (2026-08-13 session): D-080's growth>20% threshold
was chosen by inspecting quintiles AFTER the correlation was already
significant -- flagged repeatedly this session as an untested, uncounted
extra degree of freedom. This checks whether nearby thresholds (15%,
25%, 30%) give a similar result, or whether 20% specifically is a
fragile, overfit cutoff -- restricted to the semiconductor/AI universe
(D-092) where the signal has actually held up, using the same excess-
return bootstrap as D-088/D-091/D-092.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\235_growth_threshold_sensitivity_check.py
"""

from __future__ import annotations

import json
import random

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

SECTOR_SOURCE = DATA_DIR / "pullback_excess_return_sector_exclusion_check_result.json"
RESULT_PATH = DATA_DIR / "growth_threshold_sensitivity_check_result.json"

GROWTH_THRESHOLDS = (0.15, 0.20, 0.25, 0.30)
PULLBACK_THRESHOLD = 0.15
TRAILING_HIGH_WINDOW_DAYS = 252
HORIZON_MONTHS = 12


def _block_bootstrap_mean(rows: list[dict], value_key: str, group_key: str = "ticker",
                           n_resamples: int = 5000, seed: int = 42) -> dict:
    groups: dict[str, list[float]] = {}
    for r in rows:
        groups.setdefault(r[group_key], []).append(r[value_key])
    group_keys = list(groups.keys())
    observed_mean = sum(r[value_key] for r in rows) / len(rows)
    rng = random.Random(seed)
    resampled_means = []
    for _ in range(n_resamples):
        chosen = [rng.choice(group_keys) for _ in group_keys]
        values = [v for key in chosen for v in groups[key]]
        resampled_means.append(sum(values) / len(values))
    resampled_means.sort()
    n = len(resampled_means)
    lo = resampled_means[int(round(0.025 * (n - 1)))]
    hi = resampled_means[int(round(0.975 * (n - 1)))]
    beat = sum(1 for r in rows if r[value_key] > 0)
    return {"observed_mean": observed_mean, "ci_95_low": lo, "ci_95_high": hi,
            "ci_95_crosses_zero": lo <= 0 <= hi, "n_groups": len(group_keys),
            "n": len(rows), "beat_qqq_rate": beat / len(rows)}


def _quarterly_growth_history(connection: duckdb.DuckDBPyConnection, ticker: str) -> list[dict]:
    quarters = connection.execute(
        """
        SELECT DISTINCT qmr.fiscal_year_end, qmr.fiscal_quarter, qmr.availability_date
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qer.ticker = ? AND qmr.metric_name = 'revenue'
        ORDER BY qmr.availability_date
        """,
        [ticker],
    ).fetchall()
    history = []
    for fye, fq, availability_date in quarters:
        factor = compute_revenue_growth_acceleration(connection, ticker, fye, fq)
        if factor["status"] == "PASS":
            history.append({"availability_date": str(availability_date), "growth": factor["current_yoy_growth"]})
    return history


def _growth_as_of(history: list[dict], as_of_date: str) -> dict | None:
    known = [h for h in history if h["availability_date"] <= as_of_date]
    return known[-1] if known else None


def main() -> None:
    semi_tickers = sorted(json.loads(SECTOR_SOURCE.read_text(encoding="utf-8"))["excluded_tickers"])
    print(f"semiconductor/AI universe: {semi_tickers}\n")

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    results = {}
    for threshold in GROWTH_THRESHOLDS:
        episodes = []
        for ticker in semi_tickers:
            growth_history = _quarterly_growth_history(connection, ticker)
            prices = connection.execute(
                "SELECT price_date, close FROM historical_prices_daily WHERE ticker = ? ORDER BY price_date",
                [ticker],
            ).fetchall()
            prices = [(str(d), c) for d, c in prices]

            armed = True
            for i, (price_date, close) in enumerate(prices):
                if i < TRAILING_HIGH_WINDOW_DAYS:
                    continue
                window = prices[max(0, i - TRAILING_HIGH_WINDOW_DAYS):i]
                trailing_high = max(c for _, c in window)
                pullback = (trailing_high - close) / trailing_high
                if pullback < PULLBACK_THRESHOLD / 2:
                    armed = True
                growth_record = _growth_as_of(growth_history, price_date)
                qualifies = (
                    growth_record is not None and growth_record["growth"] > threshold
                    and pullback >= PULLBACK_THRESHOLD
                )
                if qualifies and armed:
                    stock_fwd = _forward_return(connection, ticker, price_date, HORIZON_MONTHS)
                    bench_fwd = _forward_return(connection, BENCHMARK_TICKER, price_date, HORIZON_MONTHS)
                    if stock_fwd and bench_fwd:
                        episodes.append({
                            "ticker": ticker, "entry_date": price_date,
                            "excess_return": stock_fwd["return"] - bench_fwd["return"],
                        })
                    armed = False

        if episodes:
            result = _block_bootstrap_mean(episodes, "excess_return")
            flag = "CROSSES ZERO" if result["ci_95_crosses_zero"] else "does NOT cross zero"
            print(f"threshold=growth>{threshold:.0%}: n={result['n']:<4} groups={result['n_groups']:<3} "
                  f"mean_excess={result['observed_mean']:+.1%}  "
                  f"CI=[{result['ci_95_low']:+.1%}, {result['ci_95_high']:+.1%}]  "
                  f"beat_QQQ={result['beat_qqq_rate']:.0%}  {flag}")
            results[f"growth_gt_{threshold}"] = result
        else:
            print(f"threshold=growth>{threshold:.0%}: no qualifying episodes")

    connection.close()
    RESULT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
