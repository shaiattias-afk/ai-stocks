"""Direct follow-up (2026-08-13 session) to D-093: that test only checked
the profit-target side of a swing rule (exit at +30%) -- no stop-loss was
defined, so it wasn't a complete, risk-managed strategy. This adds a real
stop-loss and reports the full three-way outcome: hit +30% first, hit the
stop-loss first, or timed out (12-month max hold, matching D-093's typical
holding period) without hitting either.

Two stop-loss levels are tested (-15% and -20%), both round numbers
decided before looking at outcomes here, not fit to this data -- same
discipline as the 20%/15% thresholds already used throughout this
project.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\234_swing_stoploss_full_risk_profile.py
"""

from __future__ import annotations

import json
from datetime import date

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH

PULLBACK_SOURCE = DATA_DIR / "pullback_recovery_full_universe_result.json"
SECTOR_SOURCE = DATA_DIR / "pullback_excess_return_sector_exclusion_check_result.json"
RESULT_PATH = DATA_DIR / "swing_stoploss_full_risk_profile_result.json"

TARGET_GAIN = 0.30
STOP_LOSS_LEVELS = (0.15, 0.20)
MAX_HOLD_DAYS = 252  # ~12 months, matches D-093's typical holding period


def _price_series(connection: duckdb.DuckDBPyConnection, ticker: str) -> list[dict]:
    rows = connection.execute(
        "SELECT price_date, close FROM historical_prices_daily WHERE ticker = ? ORDER BY price_date", [ticker],
    ).fetchall()
    return [{"date": str(d), "close": c} for d, c in rows]


def main() -> None:
    episodes = json.loads(PULLBACK_SOURCE.read_text(encoding="utf-8"))["group_a_episodes"]
    semi_tickers = set(json.loads(SECTOR_SOURCE.read_text(encoding="utf-8"))["excluded_tickers"])

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    series_cache: dict[str, list[dict]] = {}
    all_results: dict[str, dict] = {}

    for stop_loss in STOP_LOSS_LEVELS:
        print(f"\n{'=' * 100}")
        print(f"STOP-LOSS = -{stop_loss:.0%}  |  TARGET = +{TARGET_GAIN:.0%}  |  MAX HOLD = {MAX_HOLD_DAYS} trading days (~12mo)")
        print("=" * 100)

        for scope_label, scope_filter in [("FULL universe", lambda t: True), ("Semiconductor/AI only", lambda t: t in semi_tickers)]:
            results = []
            for ep in episodes:
                ticker = ep["ticker"]
                if not scope_filter(ticker):
                    continue
                if ticker not in series_cache:
                    series_cache[ticker] = _price_series(connection, ticker)
                series = series_cache[ticker]
                dates_sorted = [r["date"] for r in series]
                if ep["entry_date"] not in dates_sorted:
                    continue
                entry_idx = dates_sorted.index(ep["entry_date"])
                entry_price = series[entry_idx]["close"]
                target_price = entry_price * (1 + TARGET_GAIN)
                stop_price = entry_price * (1 - stop_loss)

                outcome = None
                exit_idx = None
                scan_limit = min(len(series), entry_idx + MAX_HOLD_DAYS + 1)
                for i in range(entry_idx + 1, scan_limit):
                    close = series[i]["close"]
                    if close <= stop_price:
                        outcome, exit_idx = "stopped_out", i
                        break
                    if close >= target_price:
                        outcome, exit_idx = "hit_target", i
                        break

                if outcome is None:
                    censored = (entry_idx + MAX_HOLD_DAYS) > (len(series) - 1)
                    if censored:
                        outcome = "censored_too_recent"
                    else:
                        outcome = "timed_out"
                        exit_idx = scan_limit - 1

                row = {"ticker": ticker, "entry_date": ep["entry_date"], "outcome": outcome}
                if exit_idx is not None and outcome != "censored_too_recent":
                    exit_price = series[exit_idx]["close"]
                    row["realized_return"] = (exit_price - entry_price) / entry_price
                    row["trading_days_held"] = exit_idx - entry_idx
                results.append(row)

            determinable = [r for r in results if r["outcome"] != "censored_too_recent"]
            hit = [r for r in determinable if r["outcome"] == "hit_target"]
            stopped = [r for r in determinable if r["outcome"] == "stopped_out"]
            timed_out = [r for r in determinable if r["outcome"] == "timed_out"]
            n = len(determinable)
            if n == 0:
                continue
            mean_return = sum(r["realized_return"] for r in determinable) / n
            print(f"\n  {scope_label}: n={n} determinable (+{len(results)-n} censored/too recent)")
            print(f"    hit +{TARGET_GAIN:.0%} target first:  {len(hit)}/{n} ({len(hit)/n:.0%})")
            print(f"    hit -{stop_loss:.0%} stop-loss first: {len(stopped)}/{n} ({len(stopped)/n:.0%})")
            print(f"    timed out (12mo, neither hit):    {len(timed_out)}/{n} ({len(timed_out)/n:.0%})")
            if timed_out:
                to_rets = sorted(r["realized_return"] for r in timed_out)
                print(f"      timed-out returns: median={to_rets[len(to_rets)//2]:+.1%}, "
                      f"range=[{to_rets[0]:+.1%}, {to_rets[-1]:+.1%}]")
            print(f"    mean realized return per trade (all outcomes blended): {mean_return:+.1%}")
            expected_ev = len(hit)/n*TARGET_GAIN - len(stopped)/n*stop_loss
            print(f"    simple expected value check (win_rate*target - loss_rate*stop): {expected_ev:+.1%} "
                  f"(ignores timed-out trades' actual P&L, illustrative only)")

            key = f"stop{stop_loss}_{scope_label.replace(' ', '_').replace('/', '_')}"
            all_results[key] = {
                "stop_loss": stop_loss, "scope": scope_label, "n": n,
                "n_hit_target": len(hit), "n_stopped_out": len(stopped), "n_timed_out": len(timed_out),
                "hit_rate": len(hit) / n, "stop_rate": len(stopped) / n, "timeout_rate": len(timed_out) / n,
                "mean_realized_return": mean_return, "expected_value_simple": expected_ev,
                "episodes": results,
            }

    connection.close()
    RESULT_PATH.write_text(json.dumps(all_results, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
