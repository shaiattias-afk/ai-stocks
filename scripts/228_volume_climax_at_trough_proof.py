"""User-directed new idea (2026-08-13 session): once a candidate passes
the growth+pullback screen, can the exact BOTTOM of the decline (not
just "down >=15%") be estimated from trading volume behavior -- the
classic "capitulation volume" idea, that the sharpest point of a decline
tends to coincide with unusually high volume (panic selling exhausting
itself), followed by lower volume as the price stabilizes/recovers.

**Method, small proof first (project rule: prove on a few companies
before building anything larger)**: take 5 already-known Group A
episodes (growth>20% + pullback>=15%, recovered within 12 months,
`data/pullback_recovery_full_universe_result.json`) with a clean,
determinable entry-to-recovery window. For each, find the TRUE trough
(the single lowest close price between entry and recovery -- the decline
usually continues past the 15% trigger before turning), then check
whether trading volume around that trough date is distinguishable from
the rest of the decline: (a) where does the trough day's volume rank
among all days in the entry-to-recovery window; (b) is the trough day
(or the few days around it) a local volume spike relative to its own
trailing 20-day average; (c) the "derivative" the user asked about --
day-over-day % change in volume -- checked for a characteristic
spike-then-fade pattern around the trough.

READ-ONLY. Writes one result JSON. Prints enough detail to judge by eye
whether this is worth building into a real rule.

    .venv\\Scripts\\python.exe scripts\\228_volume_climax_at_trough_proof.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH

SOURCE_PATH = DATA_DIR / "pullback_recovery_full_universe_result.json"
RESULT_PATH = DATA_DIR / "volume_climax_at_trough_proof_result.json"

N_PROOF_EPISODES = 5
TRAILING_AVG_WINDOW = 20


def _price_volume_series(connection: duckdb.DuckDBPyConnection, ticker: str) -> list[dict]:
    rows = connection.execute(
        "SELECT price_date, close, volume FROM historical_prices_daily WHERE ticker = ? ORDER BY price_date",
        [ticker],
    ).fetchall()
    return [{"date": str(d), "close": c, "volume": v} for d, c, v in rows]


def main() -> None:
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    episodes = source["group_a_episodes"]
    proof_episodes = [
        e for e in episodes
        if e["recovery"]["12mo"]["recovered"] and e["recovery"]["12mo"]["days_to_recovery"] is not None
        and e["recovery"]["12mo"]["days_to_recovery"] >= 15  # skip trivial near-instant recoveries
    ][:N_PROOF_EPISODES]

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    results = []

    for ep in proof_episodes:
        ticker = ep["ticker"]
        series = _price_volume_series(connection, ticker)
        by_date = {r["date"]: r for r in series}
        dates_sorted = [r["date"] for r in series]

        entry_idx = dates_sorted.index(ep["entry_date"])
        recovery_days = ep["recovery"]["12mo"]["days_to_recovery"]
        recovery_idx = entry_idx + recovery_days
        window = series[entry_idx:recovery_idx + 1]

        # true trough: lowest close in [entry, recovery]
        trough = min(window, key=lambda r: r["close"])
        trough_idx_in_window = window.index(trough)

        # trailing-20-day average volume as of each day (using data BEFORE that day only)
        def trailing_avg_volume(global_idx: int) -> float | None:
            lo = max(0, global_idx - TRAILING_AVG_WINDOW)
            trail = series[lo:global_idx]
            return sum(r["volume"] for r in trail) / len(trail) if trail else None

        volume_ratios = []
        for i, r in enumerate(window):
            global_idx = entry_idx + i
            trail_avg = trailing_avg_volume(global_idx)
            ratio = r["volume"] / trail_avg if trail_avg else None
            volume_ratios.append(ratio)

        trough_ratio = volume_ratios[trough_idx_in_window]
        window_ratios_sorted = sorted((r for r in volume_ratios if r is not None), reverse=True)
        trough_rank = (
            window_ratios_sorted.index(trough_ratio) + 1
            if trough_ratio is not None and trough_ratio in window_ratios_sorted else None
        )
        n_days_in_window = len(window)

        # day-over-day volume % change around trough (+/- 3 trading days)
        lo3, hi3 = max(0, trough_idx_in_window - 3), min(len(window) - 1, trough_idx_in_window + 3)
        around_trough = []
        for i in range(lo3, hi3 + 1):
            prev_vol = window[i - 1]["volume"] if i > 0 else None
            dod_change = (window[i]["volume"] / prev_vol - 1) if prev_vol else None
            around_trough.append({
                "date": window[i]["date"], "close": window[i]["close"], "volume": window[i]["volume"],
                "volume_vs_trailing20_ratio": volume_ratios[i],
                "day_over_day_volume_change": dod_change,
                "is_trough": i == trough_idx_in_window,
            })

        result = {
            "ticker": ticker, "entry_date": ep["entry_date"], "trough_date": trough["date"],
            "trough_close": trough["close"], "days_entry_to_trough": trough_idx_in_window,
            "days_entry_to_recovery": recovery_days, "n_days_in_window": n_days_in_window,
            "trough_volume_vs_trailing20_ratio": trough_ratio,
            "trough_volume_rank_in_window": trough_rank,  # 1 = highest volume day in the whole window
            "around_trough": around_trough,
        }
        results.append(result)

        print(f"\n{'=' * 90}\n{ticker}  entry={ep['entry_date']}  trough={trough['date']} "
              f"({trough_idx_in_window} trading days after entry)  recovered after {recovery_days} days")
        print(f"  trough volume / trailing-20d-avg volume ratio: "
              f"{trough_ratio:.2f}x" if trough_ratio else "  n/a")
        print(f"  trough day's volume rank within the {n_days_in_window}-day decline window: "
              f"{trough_rank} of {n_days_in_window} (1 = highest volume day)" if trough_rank else "  n/a")
        print(f"  {'date':<12}{'close':>10}{'volume':>14}{'vs 20d avg':>12}{'DoD chg':>10}  trough?")
        for r in around_trough:
            dod = f"{r['day_over_day_volume_change']:+.0%}" if r['day_over_day_volume_change'] is not None else "n/a"
            ratio = f"{r['volume_vs_trailing20_ratio']:.2f}x" if r['volume_vs_trailing20_ratio'] else "n/a"
            marker = " <-- TROUGH" if r["is_trough"] else ""
            print(f"  {r['date']:<12}{r['close']:>10.2f}{r['volume']:>14,}{ratio:>12}{dod:>10}{marker}")

    connection.close()

    n_top_quartile = sum(
        1 for r in results if r["trough_volume_rank_in_window"] is not None
        and r["trough_volume_rank_in_window"] <= max(1, r["n_days_in_window"] // 4)
    )
    print(f"\n{'=' * 90}\nSUMMARY: {n_top_quartile}/{len(results)} troughs fall in the top quartile of "
          f"highest-volume days within their own decline window.")

    RESULT_PATH.write_text(json.dumps({"episodes": results}, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
