"""Council-directed follow-up (2026-08-13 session, council on the SWING
project): the backtesting advisor's key warning was that "no stop-loss"
was previously tested with a 24-month watch window (D-093/scripts/233),
where extra time let bad trades recover -- that is NOT the same as "no
stop-loss" with a hard 6-month cap, which the user actually wants. This
runs the honest version: simulate forward EXACTLY 126 trading days
(~6 months) from each entry, no extension, no stop-loss. Every entry is
classified into exactly one of three buckets at day 126, and the worst
drawdown along the way is recorded even though nothing acts on it (the
substitute for a stop-loss the user rejected: report the risk, don't
manage it) -- avoiding the exact silent bug D-093 originally had (a
generous watch window making outcomes look better than a real fixed
horizon would).

Universe: the 10 semiconductor/AI tickers where the growth+pullback
entry rule has shown a real, tested edge (D-092), since that is the only
group with demonstrated evidence -- not the full universe.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\238_swing_6month_hard_cap_no_stoploss.py
"""

from __future__ import annotations

import json
import random
from datetime import date

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return

PULLBACK_SOURCE = DATA_DIR / "pullback_recovery_full_universe_result.json"
SECTOR_SOURCE = DATA_DIR / "pullback_excess_return_sector_exclusion_check_result.json"
RESULT_PATH = DATA_DIR / "swing_6month_hard_cap_no_stoploss_result.json"

TARGET_GAIN = 0.30
HARD_CAP_TRADING_DAYS = 126  # ~6 months, NO extension past this under any circumstance
HORIZON_MONTHS_FOR_EXCESS_RETURN = 6  # for the QQQ-comparison bootstrap, calendar-based like D-088/091/092/094


def _price_series(connection: duckdb.DuckDBPyConnection, ticker: str) -> list[dict]:
    rows = connection.execute(
        "SELECT price_date, close FROM historical_prices_daily WHERE ticker = ? ORDER BY price_date", [ticker],
    ).fetchall()
    return [{"date": str(d), "close": c} for d, c in rows]


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
    episodes = [e for e in json.loads(PULLBACK_SOURCE.read_text(encoding="utf-8"))["group_a_episodes"]
                if e["ticker"] in semi_tickers]
    print(f"semiconductor/AI universe: {semi_tickers}")
    print(f"growth>20% + pullback>=15% entries in this universe: {len(episodes)}\n")

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    series_cache: dict[str, list[dict]] = {}

    hard_cap_results = []
    excess_return_rows = []

    for ep in episodes:
        ticker = ep["ticker"]
        if ticker not in series_cache:
            series_cache[ticker] = _price_series(connection, ticker)
        series = series_cache[ticker]
        dates_sorted = [r["date"] for r in series]
        if ep["entry_date"] not in dates_sorted:
            continue
        entry_idx = dates_sorted.index(ep["entry_date"])
        entry_price = series[entry_idx]["close"]
        target_price = entry_price * (1 + TARGET_GAIN)

        last_available_idx = len(series) - 1
        cap_idx = entry_idx + HARD_CAP_TRADING_DAYS
        if cap_idx > last_available_idx:
            hard_cap_results.append({
                "ticker": ticker, "entry_date": ep["entry_date"], "outcome": "censored_too_recent",
            })
            continue

        hit_idx = None
        max_drawdown = 0.0
        for i in range(entry_idx + 1, cap_idx + 1):
            close = series[i]["close"]
            dip = (entry_price - close) / entry_price
            if dip > max_drawdown:
                max_drawdown = dip
            if close >= target_price and hit_idx is None:
                hit_idx = i
                break  # hard cap rule: once target is hit within the window, that IS the outcome -- no further tracking needed

        if hit_idx is not None:
            trading_days_to_hit = hit_idx - entry_idx
            outcome = "hit_target_by_126d"
            final_return = (series[hit_idx]["close"] - entry_price) / entry_price
        else:
            trading_days_to_hit = None
            final_close = series[cap_idx]["close"]
            final_return = (final_close - entry_price) / entry_price
            outcome = "open_gain_at_126d" if final_return > 0 else "open_loss_at_126d"
            # max_drawdown must reflect the FULL window even if target was never hit
            for i in range(entry_idx + 1, cap_idx + 1):
                dip = (entry_price - series[i]["close"]) / entry_price
                if dip > max_drawdown:
                    max_drawdown = dip

        hard_cap_results.append({
            "ticker": ticker, "entry_date": ep["entry_date"], "outcome": outcome,
            "trading_days_to_hit": trading_days_to_hit, "return_at_126d_or_hit": final_return,
            "max_drawdown_within_window": max_drawdown,
        })

        stock_fwd = _forward_return(connection, ticker, ep["entry_date"], HORIZON_MONTHS_FOR_EXCESS_RETURN)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, ep["entry_date"], HORIZON_MONTHS_FOR_EXCESS_RETURN)
        if stock_fwd and bench_fwd:
            excess_return_rows.append({
                "ticker": ticker, "entry_date": ep["entry_date"],
                "excess_return": stock_fwd["return"] - bench_fwd["return"],
            })

    connection.close()

    determinable = [r for r in hard_cap_results if r["outcome"] != "censored_too_recent"]
    censored = [r for r in hard_cap_results if r["outcome"] == "censored_too_recent"]
    hit = [r for r in determinable if r["outcome"] == "hit_target_by_126d"]
    open_gain = [r for r in determinable if r["outcome"] == "open_gain_at_126d"]
    open_loss = [r for r in determinable if r["outcome"] == "open_loss_at_126d"]
    n = len(determinable)

    print("=" * 100)
    print(f"HARD 126-TRADING-DAY (~6mo) CAP, NO STOP-LOSS -- honest outcome at exactly day 126, no extension")
    print("=" * 100)
    print(f"n={n} determinable entries (+{len(censored)} too recent to have 126 days of forward data yet)\n")
    print(f"hit +{TARGET_GAIN:.0%} within 126 days:      {len(hit)}/{n} ({len(hit)/n:.0%})")
    print(f"still open, POSITIVE at day 126:  {len(open_gain)}/{n} ({len(open_gain)/n:.0%})")
    print(f"still open, NEGATIVE at day 126:  {len(open_loss)}/{n} ({len(open_loss)/n:.0%})")

    if hit:
        days = sorted(r["trading_days_to_hit"] for r in hit)
        print(f"\n  of the hits: median {days[len(days)//2]} trading days (~{days[len(days)//2]/21:.1f} months)")
    if open_gain:
        rets = sorted(r["return_at_126d_or_hit"] for r in open_gain)
        print(f"  of the open-gains at day 126: median return {rets[len(rets)//2]:+.1%}, "
              f"range [{rets[0]:+.1%}, {rets[-1]:+.1%}]")
    if open_loss:
        rets = sorted(r["return_at_126d_or_hit"] for r in open_loss)
        print(f"  of the open-losses at day 126: median return {rets[len(rets)//2]:+.1%}, "
              f"range [{rets[0]:+.1%}, {rets[-1]:+.1%}]")

    all_returns = sorted(r["return_at_126d_or_hit"] for r in determinable)
    mean_all = sum(all_returns) / n
    median_all = all_returns[n // 2] if n % 2 else (all_returns[n // 2 - 1] + all_returns[n // 2]) / 2
    print(f"\nTRUE expected value per trade -- ALL {n} positions blended, including open losses "
          f"(this is the honest number, not conditional on winning):")
    print(f"  mean return:   {mean_all:+.1%}")
    print(f"  median return: {median_all:+.1%}")

    dd_all = sorted(r["max_drawdown_within_window"] for r in determinable)
    n_deep = sum(1 for d in dd_all if d > 0.15)
    print(f"\nmax drawdown seen within the 126-day window (informational only -- nothing acts on this):")
    print(f"  median: {dd_all[len(dd_all)//2]:.1%}, worst: {dd_all[-1]:.1%}, "
          f"{n_deep}/{n} ({n_deep/n:.0%}) saw a drawdown deeper than 15% at some point")

    print(f"\n{'=' * 100}")
    print(f"Excess return vs QQQ at the 6-calendar-month mark (same bootstrap method as D-088/091/092/094)")
    print("=" * 100)
    er_result = _block_bootstrap_mean(excess_return_rows, "excess_return")
    flag = "CROSSES ZERO" if er_result["ci_95_crosses_zero"] else "does NOT cross zero"
    print(f"n={er_result['n']} groups={er_result['n_groups']} mean_excess={er_result['observed_mean']:+.1%}  "
          f"CI=[{er_result['ci_95_low']:+.1%}, {er_result['ci_95_high']:+.1%}]  "
          f"beat_QQQ={er_result['beat_qqq_rate']:.0%}  {flag}")

    RESULT_PATH.write_text(json.dumps({
        "hard_cap_trading_days": HARD_CAP_TRADING_DAYS, "target_gain": TARGET_GAIN,
        "n_determinable": n, "n_censored": len(censored),
        "n_hit": len(hit), "n_open_gain": len(open_gain), "n_open_loss": len(open_loss),
        "mean_return_all_blended": mean_all, "median_return_all_blended": median_all,
        "excess_return_vs_qqq_6mo": er_result,
        "hard_cap_episodes": hard_cap_results,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
