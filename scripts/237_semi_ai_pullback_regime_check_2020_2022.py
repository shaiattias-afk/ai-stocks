"""Direct follow-up (2026-08-13 session): every quarterly-cadence result
this session is 2023-2025 data because the growth-rate lookback cannot
reach further back (D-079/D-080). But `historical_prices_daily` itself
goes back to 2020-01-02 -- so the PULLBACK half of the rule (no growth
filter needed) CAN be tested in a genuinely different regime: 2020's
COVID crash/recovery and 2022's rate-hike bear market, for the exact
same 10 semiconductor/AI tickers (D-092).

This is explicitly a PARTIAL regime check -- it cannot test the growth
filter itself (quarterly fundamentals aren't available that far back),
only whether "buy a >=15% pullback in these specific names" shows a
similar pattern outside 2023-2025. Still a real, immediately-available
piece of evidence, not a full regime test.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\237_semi_ai_pullback_regime_check_2020_2022.py
"""

from __future__ import annotations

import json
import random

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return

SECTOR_SOURCE = DATA_DIR / "pullback_excess_return_sector_exclusion_check_result.json"
RESULT_PATH = DATA_DIR / "semi_ai_pullback_regime_check_2020_2022_result.json"

PULLBACK_THRESHOLD = 0.15
TRAILING_HIGH_WINDOW_DAYS = 252
HORIZON_MONTHS = 12
PERIODS = {
    "2020-2022 (COVID crash/recovery + 2022 rate-hike bear market)": ("2020-01-01", "2023-01-01"),
    "2023-2025 (this session's usual window, for direct comparison)": ("2023-01-01", "2026-01-01"),
}


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


def main() -> None:
    semi_tickers = sorted(json.loads(SECTOR_SOURCE.read_text(encoding="utf-8"))["excluded_tickers"])
    print(f"semiconductor/AI universe: {semi_tickers}\n")

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    results = {}

    for period_label, (start_date, end_date) in PERIODS.items():
        episodes = []
        for ticker in semi_tickers:
            prices = connection.execute(
                "SELECT price_date, close FROM historical_prices_daily WHERE ticker = ? ORDER BY price_date",
                [ticker],
            ).fetchall()
            prices = [(str(d), c) for d, c in prices]

            armed = True
            for i, (price_date, close) in enumerate(prices):
                if i < TRAILING_HIGH_WINDOW_DAYS or price_date < start_date or price_date >= end_date:
                    continue
                window = prices[max(0, i - TRAILING_HIGH_WINDOW_DAYS):i]
                trailing_high = max(c for _, c in window)
                pullback = (trailing_high - close) / trailing_high
                if pullback < PULLBACK_THRESHOLD / 2:
                    armed = True
                if pullback >= PULLBACK_THRESHOLD and armed:
                    stock_fwd = _forward_return(connection, ticker, price_date, HORIZON_MONTHS)
                    bench_fwd = _forward_return(connection, BENCHMARK_TICKER, price_date, HORIZON_MONTHS)
                    if stock_fwd and bench_fwd:
                        episodes.append({
                            "ticker": ticker, "entry_date": price_date,
                            "excess_return": stock_fwd["return"] - bench_fwd["return"],
                        })
                    armed = False

        print(f"\n{'=' * 100}\n{period_label}\n{'=' * 100}")
        if episodes:
            result = _block_bootstrap_mean(episodes, "excess_return")
            flag = "CROSSES ZERO" if result["ci_95_crosses_zero"] else "does NOT cross zero"
            print(f"pullback>=15% (no growth filter, price-only): n={result['n']:<4} groups={result['n_groups']:<3} "
                  f"mean_excess={result['observed_mean']:+.1%}  "
                  f"CI=[{result['ci_95_low']:+.1%}, {result['ci_95_high']:+.1%}]  "
                  f"beat_QQQ={result['beat_qqq_rate']:.0%}  {flag}")
            results[period_label] = result
        else:
            print("no qualifying episodes")
            results[period_label] = None

    connection.close()
    RESULT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
