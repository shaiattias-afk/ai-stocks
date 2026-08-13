"""Direct follow-up (2026-08-13 session) to D-089: that result showed a
REAL but purely retrospective pattern (the trough tends to fall on a
high-volume day) -- it does not by itself prove volume can be used in
real time, without hindsight, to time an entry better than just buying
on the day the 15%-pullback trigger first fires.

This tests a genuine forward-looking rule with NO look-ahead: within
each Group A decline window (growth>20% + pullback>=15%, entry already
declared), watch daily volume from the entry date onward. The trailing-
20-day average used for the ratio is always computed from data strictly
BEFORE the day in question (same as D-089/scripts/229) -- nothing here
needs to see the future. The FIRST day the volume ratio exceeds a fixed,
round threshold (2.0x, chosen for the same reason growth>20%/pullback
>=15% were -- a plain round number decided before looking at outcomes,
not fit to this data) is the "volume trigger" entry day. Compared
against: (a) simply buying on the original pullback-trigger day (the
existing baseline), (b) the TRUE trough price (only knowable in
hindsight -- an upper bound on how good ANY timing rule could possibly
do). A 1.5x threshold is also reported for transparency (sensitivity to
the threshold choice), not because it was picked after seeing which one
"wins."

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\230_volume_trigger_realtime_entry_test.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return

SOURCE_PATH = DATA_DIR / "pullback_recovery_full_universe_result.json"
RESULT_PATH = DATA_DIR / "volume_trigger_realtime_entry_test_result.json"

TRAILING_AVG_WINDOW = 20
THRESHOLDS = (1.5, 2.0)
MAX_WAIT_DAYS = 60  # how long to watch for a volume trigger before giving up


def _price_volume_series(connection: duckdb.DuckDBPyConnection, ticker: str) -> list[dict]:
    rows = connection.execute(
        "SELECT price_date, close, volume FROM historical_prices_daily WHERE ticker = ? ORDER BY price_date",
        [ticker],
    ).fetchall()
    return [{"date": str(d), "close": c, "volume": v} for d, c, v in rows]


def main() -> None:
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    episodes = source["group_a_episodes"]
    determinable = [
        e for e in episodes
        if e["recovery"]["12mo"]["recovered"] and e["recovery"]["12mo"]["days_to_recovery"] is not None
        and e["recovery"]["12mo"]["days_to_recovery"] >= 10
    ]
    print(f"{len(determinable)} determinable Group A episodes\n")

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    series_cache: dict[str, list[dict]] = {}

    results_by_threshold: dict[str, list[dict]] = {str(t): [] for t in THRESHOLDS}

    for ep in determinable:
        ticker = ep["ticker"]
        if ticker not in series_cache:
            series_cache[ticker] = _price_volume_series(connection, ticker)
        series = series_cache[ticker]
        dates_sorted = [r["date"] for r in series]
        if ep["entry_date"] not in dates_sorted:
            continue
        entry_idx = dates_sorted.index(ep["entry_date"])
        recovery_idx = entry_idx + ep["recovery"]["12mo"]["days_to_recovery"]
        window = series[entry_idx:recovery_idx + 1]
        trough = min(window, key=lambda r: r["close"])
        entry_price = window[0]["close"]

        def trailing_avg_volume(global_idx: int) -> float | None:
            lo = max(0, global_idx - TRAILING_AVG_WINDOW)
            trail = series[lo:global_idx]
            return sum(r["volume"] for r in trail) / len(trail) if trail else None

        for threshold in THRESHOLDS:
            trigger_day = None
            scan_limit = min(len(window), MAX_WAIT_DAYS + 1)
            for i in range(scan_limit):
                global_idx = entry_idx + i
                trail_avg = trailing_avg_volume(global_idx)
                if trail_avg and window[i]["volume"] / trail_avg >= threshold:
                    trigger_day = window[i]
                    break

            if trigger_day is None:
                results_by_threshold[str(threshold)].append({
                    "ticker": ticker, "entry_date": ep["entry_date"], "triggered": False,
                })
                continue

            trigger_fwd = _forward_return(connection, ticker, trigger_day["date"], 12)
            entry_fwd = _forward_return(connection, ticker, ep["entry_date"], 12)
            bench_fwd_from_trigger = _forward_return(connection, BENCHMARK_TICKER, trigger_day["date"], 12)

            price_improvement_vs_entry = (entry_price - trigger_day["close"]) / entry_price
            gap_to_true_trough = (trigger_day["close"] - trough["close"]) / trough["close"]

            excess_return_from_trigger = None
            excess_return_from_entry = None
            if trigger_fwd and bench_fwd_from_trigger:
                excess_return_from_trigger = trigger_fwd["return"] - bench_fwd_from_trigger["return"]
            if entry_fwd:
                bench_fwd_from_entry = _forward_return(connection, BENCHMARK_TICKER, ep["entry_date"], 12)
                if bench_fwd_from_entry:
                    excess_return_from_entry = entry_fwd["return"] - bench_fwd_from_entry["return"]

            results_by_threshold[str(threshold)].append({
                "ticker": ticker, "entry_date": ep["entry_date"], "triggered": True,
                "days_to_trigger": window.index(trigger_day),
                "entry_price": entry_price, "trigger_price": trigger_day["close"], "trough_price": trough["close"],
                "price_improvement_vs_entry": price_improvement_vs_entry,
                "gap_to_true_trough": gap_to_true_trough,
                "excess_return_12mo_from_trigger": excess_return_from_trigger,
                "excess_return_12mo_from_entry": excess_return_from_entry,
            })

    connection.close()

    for threshold in THRESHOLDS:
        rows = results_by_threshold[str(threshold)]
        triggered = [r for r in rows if r["triggered"]]
        print(f"\n{'=' * 90}")
        print(f"THRESHOLD = {threshold}x trailing-20d volume")
        print(f"{'=' * 90}")
        print(f"triggered within {MAX_WAIT_DAYS} days: {len(triggered)}/{len(rows)} "
              f"({len(triggered)/len(rows):.0%})")
        if not triggered:
            continue
        improvements = [r["price_improvement_vs_entry"] for r in triggered]
        gaps = [r["gap_to_true_trough"] for r in triggered]
        n = len(improvements)
        avg_improvement = sum(improvements) / n
        avg_gap = sum(gaps) / n
        n_better_price = sum(1 for x in improvements if x > 0)
        print(f"avg price improvement vs plain pullback entry: {avg_improvement:+.1%} "
              f"({n_better_price}/{n} = {n_better_price/n:.0%} got a BETTER (lower) entry price)")
        print(f"avg remaining gap between trigger price and the TRUE trough: {avg_gap:+.1%} "
              f"(0% = caught the exact bottom; positive = still above the eventual low)")

        er_trigger = [r["excess_return_12mo_from_trigger"] for r in triggered if r["excess_return_12mo_from_trigger"] is not None]
        er_entry = [r["excess_return_12mo_from_entry"] for r in triggered if r["excess_return_12mo_from_entry"] is not None]
        if er_trigger and er_entry:
            print(f"mean 12mo excess return vs QQQ: entering on PLAIN pullback trigger = {sum(er_entry)/len(er_entry):+.1%}  "
                  f"vs entering on VOLUME trigger = {sum(er_trigger)/len(er_trigger):+.1%}")

    RESULT_PATH.write_text(json.dumps({"results_by_threshold": results_by_threshold}, indent=2, default=str),
                            encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
