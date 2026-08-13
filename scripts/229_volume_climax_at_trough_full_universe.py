"""Scale-up of scripts/228's small proof (2026-08-13 session): the
5-episode eyeball proof was mixed (2/5 clearly elevated trough volume,
2/5 not) and confounded -- 3 of the 5 troughs landed in the same week
(the April 2025 tariff-driven market crash), so it was really only ~3
independent observations, not 5. This runs the same trough-volume
statistic across every determinable Group A episode (growth>20% +
pullback>=15%, recovered within 12 months, so the entry-to-recovery
window is fully known) and reports the real distribution, plus how many
distinct calendar months the troughs fall in -- the same "how much
independent information is really here" discipline D-063 established
for the P/E finding's cohort-concentration problem.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\229_volume_climax_at_trough_full_universe.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH

SOURCE_PATH = DATA_DIR / "pullback_recovery_full_universe_result.json"
RESULT_PATH = DATA_DIR / "volume_climax_at_trough_full_universe_result.json"

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
    determinable = [
        e for e in episodes
        if e["recovery"]["12mo"]["recovered"] and e["recovery"]["12mo"]["days_to_recovery"] is not None
        and e["recovery"]["12mo"]["days_to_recovery"] >= 10  # skip near-instant recoveries -- no real decline window
    ]
    print(f"{len(determinable)} determinable Group A episodes (recovered within 12mo, real decline window)")

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    series_cache: dict[str, list[dict]] = {}
    results = []

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
        if len(window) < 10:
            continue

        trough = min(window, key=lambda r: r["close"])
        trough_idx_in_window = window.index(trough)
        global_trough_idx = entry_idx + trough_idx_in_window

        def trailing_avg_volume(global_idx: int) -> float | None:
            lo = max(0, global_idx - TRAILING_AVG_WINDOW)
            trail = series[lo:global_idx]
            return sum(r["volume"] for r in trail) / len(trail) if trail else None

        volume_ratios = []
        for i in range(len(window)):
            gi = entry_idx + i
            trail_avg = trailing_avg_volume(gi)
            volume_ratios.append(window[i]["volume"] / trail_avg if trail_avg else None)

        trough_ratio = volume_ratios[trough_idx_in_window]
        valid_ratios = [r for r in volume_ratios if r is not None]
        rank = (sorted(valid_ratios, reverse=True).index(trough_ratio) + 1) if trough_ratio is not None else None
        rank_fraction = rank / len(valid_ratios) if rank is not None else None  # 0 = highest volume day, 1 = lowest

        results.append({
            "ticker": ticker, "entry_date": ep["entry_date"], "trough_date": trough["date"],
            "trough_month": trough["date"][:7], "n_days_in_window": len(window),
            "trough_volume_ratio": trough_ratio, "trough_rank_fraction": rank_fraction,
        })

    connection.close()

    valid = [r for r in results if r["trough_rank_fraction"] is not None]
    n = len(valid)
    rank_fractions = sorted(r["trough_rank_fraction"] for r in valid)
    mean_rank_fraction = sum(rank_fractions) / n
    median_rank_fraction = rank_fractions[n // 2] if n % 2 else (rank_fractions[n // 2 - 1] + rank_fractions[n // 2]) / 2
    ratios = sorted(r["trough_volume_ratio"] for r in valid if r["trough_volume_ratio"] is not None)
    median_ratio = ratios[len(ratios) // 2]
    n_elevated = sum(1 for r in ratios if r > 1.0)

    distinct_months = {r["trough_month"] for r in valid}
    distinct_tickers = {r["ticker"] for r in valid}

    print(f"\n{'=' * 90}")
    print(f"n={n} episodes | {len(distinct_tickers)} distinct tickers | "
          f"{len(distinct_months)} distinct trough calendar-months")
    print(f"mean rank_fraction={mean_rank_fraction:.3f}  median={median_rank_fraction:.3f}  "
          f"(0.0=highest-volume day in window, 0.5=exactly average, 1.0=lowest-volume day)")
    print(f"median trough volume ratio vs trailing 20d avg: {median_ratio:.2f}x")
    print(f"troughs with ABOVE-average volume (ratio>1.0): {n_elevated}/{len(ratios)} ({n_elevated/len(ratios):.0%})")

    from collections import Counter
    month_counts = Counter(r["trough_month"] for r in valid)
    top_months = month_counts.most_common(5)
    print(f"\ntop 5 trough-clustering months: {top_months}")
    print("(if troughs concentrate heavily in a few shared calendar months, most of the sample is "
          "really a handful of shared macro events, not independent company-level signals)")

    RESULT_PATH.write_text(json.dumps({
        "n_episodes": n, "n_distinct_tickers": len(distinct_tickers), "n_distinct_trough_months": len(distinct_months),
        "mean_rank_fraction": mean_rank_fraction, "median_rank_fraction": median_rank_fraction,
        "median_trough_volume_ratio": median_ratio, "n_elevated": n_elevated, "n_total_valid_ratio": len(ratios),
        "top_trough_months": top_months, "episodes": results,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
