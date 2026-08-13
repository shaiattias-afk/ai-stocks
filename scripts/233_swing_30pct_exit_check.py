"""User-directed new track (2026-08-13 session): alongside the long-term
(3-5 year) entry-timing thesis, the user wants to test a SWING approach
on the stock itself (explicitly NOT options) -- enter on the same
already-validated trigger (growth>20% + pullback>=15%, D-081), but exit
as soon as the stock gains ~30% instead of holding for years, and see
whether that "quick flip" beats holding through.

**Method**: for each Group A episode (same 136-episode pool used
throughout D-081/D-088/D-091/D-092), scan forward day-by-day from entry
and find the first day the close price reaches entry_price * 1.30. If
never reached within a 24-month watch window, the episode is marked
"target not reached" (not fabricated as a loss or a win). For episodes
that DO hit +30%, computes days-held and the ANNUALIZED return implied
by hitting +30% that fast, compared against D-088's already-computed
buy-and-hold mean excess return at the horizon closest to the typical
swing duration. Run on both the full ~92-company universe and the
semiconductor/AI-focused 10-ticker subset (D-092), since that subset
already showed the strongest signal.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\233_swing_30pct_exit_check.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH

PULLBACK_SOURCE = DATA_DIR / "pullback_recovery_full_universe_result.json"
SECTOR_SOURCE = DATA_DIR / "pullback_excess_return_sector_exclusion_check_result.json"
RESULT_PATH = DATA_DIR / "swing_30pct_exit_check_result.json"

TARGET_GAIN = 0.30
MAX_WATCH_DAYS = 504  # ~24 months of trading days


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
    results = []

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

        hit_idx = None
        max_drawdown_before_hit = 0.0  # worst peak-to-trough dip below entry price, before the target was hit
        scan_limit = min(len(series), entry_idx + MAX_WATCH_DAYS + 1)
        for i in range(entry_idx + 1, scan_limit):
            dip = (entry_price - series[i]["close"]) / entry_price
            if dip > max_drawdown_before_hit:
                max_drawdown_before_hit = dip
            if series[i]["close"] >= target_price:
                hit_idx = i
                break

        latest_available_idx = len(series) - 1
        censored = hit_idx is None and (entry_idx + MAX_WATCH_DAYS) > latest_available_idx

        row = {
            "ticker": ticker, "is_semi_ai": ticker in semi_tickers, "entry_date": ep["entry_date"],
            "entry_price": entry_price, "target_price": target_price,
        }
        if hit_idx is not None:
            trading_days_held = hit_idx - entry_idx
            calendar_days_held = (
                __import__("datetime").date.fromisoformat(series[hit_idx]["date"])
                - __import__("datetime").date.fromisoformat(ep["entry_date"])
            ).days
            annualized = (1 + TARGET_GAIN) ** (365.25 / max(calendar_days_held, 1)) - 1
            row.update({
                "outcome": "hit_target", "hit_date": series[hit_idx]["date"],
                "trading_days_held": trading_days_held, "calendar_days_held": calendar_days_held,
                "annualized_return_if_repeated": annualized,
                "max_drawdown_before_hit": max_drawdown_before_hit,
            })
        elif censored:
            row["outcome"] = "censored_too_recent"
        else:
            row["outcome"] = "not_reached_within_24mo"
            row["max_drawdown_seen"] = max_drawdown_before_hit
            final_close = series[min(entry_idx + MAX_WATCH_DAYS, len(series) - 1)]["close"]
            row["return_at_24mo_if_never_hit"] = (final_close - entry_price) / entry_price
        results.append(row)

    connection.close()

    def summarize(label: str, rows: list[dict]) -> dict:
        n = len(rows)
        hit = [r for r in rows if r["outcome"] == "hit_target"]
        not_reached = [r for r in rows if r["outcome"] == "not_reached_within_24mo"]
        censored = [r for r in rows if r["outcome"] == "censored_too_recent"]
        determinable = hit + not_reached
        hit_rate = len(hit) / len(determinable) if determinable else None
        print(f"\n{label}: n={n} (hit={len(hit)}, not_reached={len(not_reached)}, censored={len(censored)})")
        if determinable:
            print(f"  hit-rate among determinable: {len(hit)}/{len(determinable)} ({hit_rate:.0%})")
        if hit:
            days = sorted(r["trading_days_held"] for r in hit)
            n_hit = len(days)
            median_days = days[n_hit // 2]
            p25_days = days[int(n_hit * 0.25)]
            p75_days = days[int(n_hit * 0.75)]
            annualized_sorted = sorted(r["annualized_return_if_repeated"] for r in hit)
            median_annualized = annualized_sorted[len(annualized_sorted) // 2]
            print(f"  trading days to hit +{TARGET_GAIN:.0%}: median={median_days} (~{median_days/21:.1f}mo), "
                  f"25th pct={p25_days}d, 75th pct={p75_days}d")
            print(f"  MEDIAN annualized return if this exact trade could be repeated back-to-back: "
                  f"{median_annualized:+.0%} (illustrative, ignores re-entry gaps/taxes/slippage; "
                  f"mean is not reported -- a handful of 1-2 day hits make it explode to millions of percent "
                  f"and is meaningless)")
            n_fast = sum(1 for d in days if d <= 21)
            print(f"  hit within 1 trading month (<=21 days): {n_fast}/{n_hit} ({n_fast/n_hit:.0%})")
            dd = sorted(r["max_drawdown_before_hit"] for r in hit)
            median_dd = dd[len(dd) // 2]
            n_deep_dd = sum(1 for x in dd if x > 0.15)
            print(f"  max drawdown BELOW entry price before eventually hitting +{TARGET_GAIN:.0%}: "
                  f"median={median_dd:.1%}, worst={dd[-1]:.1%}; "
                  f"{n_deep_dd}/{n_hit} ({n_deep_dd/n_hit:.0%}) dipped >15% further below entry first")
        if not_reached:
            final_rets = sorted(r["return_at_24mo_if_never_hit"] for r in not_reached)
            print(f"  of the {len(not_reached)} that NEVER hit +{TARGET_GAIN:.0%} within 24mo: "
                  f"return at 24mo ranged {final_rets[0]:+.0%} to {final_rets[-1]:+.0%} "
                  f"(median {final_rets[len(final_rets)//2]:+.0%})")
        return {
            "n": n, "n_hit": len(hit), "n_not_reached": len(not_reached), "n_censored": len(censored),
            "hit_rate": hit_rate,
            "median_trading_days_to_hit": (sorted(r["trading_days_held"] for r in hit)[len(hit)//2] if hit else None),
        }

    print("=" * 100)
    print(f"SWING TEST: enter on growth>20%+pullback>=15% trigger, exit at first +{TARGET_GAIN:.0%} close")
    print("=" * 100)
    summary_all = summarize("FULL universe (32 tickers, 136 episodes)", results)
    summary_semi = summarize("Semiconductor/AI subset only (10 tickers)", [r for r in results if r["is_semi_ai"]])

    RESULT_PATH.write_text(json.dumps({
        "target_gain": TARGET_GAIN, "max_watch_days": MAX_WATCH_DAYS,
        "summary_all": summary_all, "summary_semi_ai": summary_semi, "episodes": results,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
