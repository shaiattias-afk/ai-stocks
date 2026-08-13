"""User-directed priority follow-up (2026-08-13 session): D-084 tested
whether the growth-RATE correlation (D-079/D-080) survives excluding
semiconductor/AI names -- it does not (CI flips to crossing zero). D-088
separately corrected the growth+PULLBACK entry-timing signal (D-081) to
excess return vs QQQ, finding a real, bootstrap-confirmed positive mean
excess return at 6/12/24 months. Those two corrections have never been
combined: does the pullback signal's excess-return edge survive the SAME
sector exclusion that broke the plain growth correlation? This is the
single most important open question left from today's session, flagged
explicitly in CLAUDE.md's "Open next step" list (item d) and answered
here.

**Method**: reuses the exact same 136 Group A episodes and SIC lookups
already on file from D-084 (`data/growth_signal_sector_exclusion_check_result.json`)
and D-088's excess-return computation logic (`scripts/227`) -- excludes
SIC 3674 semiconductor names AND the 4 D-080-flagged names (RKLB/MU/STX/APP),
same definition as D-084, then re-runs the identical ticker-grouped
block-bootstrap on mean excess return vs QQQ that D-088 used.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\231_pullback_excess_return_sector_exclusion_check.py
"""

from __future__ import annotations

import json
import random

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return

PULLBACK_SOURCE = DATA_DIR / "pullback_recovery_full_universe_result.json"
SIC_SOURCE = DATA_DIR / "growth_signal_sector_exclusion_check_result.json"
RESULT_PATH = DATA_DIR / "pullback_excess_return_sector_exclusion_check_result.json"

HORIZONS = (6, 12, 24)
SEMICONDUCTOR_SIC = "3674"
NAMED_FLAGGED_TICKERS = {"RKLB", "MU", "STX", "APP"}


def _block_bootstrap_mean(rows: list[dict], value_key: str, group_key: str = "ticker",
                           n_resamples: int = 5000, seed: int = 0) -> dict:
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
    return {"observed_mean": observed_mean, "ci_95_low": lo, "ci_95_high": hi,
            "ci_95_crosses_zero": lo <= 0 <= hi, "n_groups": len(group_keys)}


def main() -> None:
    episodes = json.loads(PULLBACK_SOURCE.read_text(encoding="utf-8"))["group_a_episodes"]
    sic_by_ticker = json.loads(SIC_SOURCE.read_text(encoding="utf-8"))["sic_by_ticker"]

    all_tickers = {e["ticker"] for e in episodes}
    semiconductor_tickers = {t for t in all_tickers if sic_by_ticker.get(t, {}).get("sic") == SEMICONDUCTOR_SIC}
    excluded = semiconductor_tickers | (NAMED_FLAGGED_TICKERS & all_tickers)
    print(f"universe: {len(all_tickers)} tickers")
    print(f"SIC {SEMICONDUCTOR_SIC} semiconductor tickers found: {sorted(semiconductor_tickers)}")
    print(f"named flagged tickers present: {sorted(NAMED_FLAGGED_TICKERS & all_tickers)}")
    print(f"total excluded: {sorted(excluded)} ({len(excluded)} tickers)")

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    enriched = []
    for e in episodes:
        row = {"ticker": e["ticker"], "entry_date": e["entry_date"], "excluded": e["ticker"] in excluded}
        for h in HORIZONS:
            stock_fwd = _forward_return(connection, e["ticker"], e["entry_date"], h)
            bench_fwd = _forward_return(connection, BENCHMARK_TICKER, e["entry_date"], h)
            row[f"excess_return_{h}mo"] = (
                stock_fwd["return"] - bench_fwd["return"] if stock_fwd and bench_fwd else None
            )
        enriched.append(row)
    connection.close()

    print(f"\n{'=' * 100}")
    print("Mean excess return vs QQQ: baseline (all 32 tickers) vs sector-excluded (18 remaining)")
    print("=" * 100)

    results = {}
    for h in HORIZONS:
        key = f"excess_return_{h}mo"
        baseline_rows = [r for r in enriched if r[key] is not None]
        excluded_rows = [r for r in enriched if r[key] is not None and not r["excluded"]]

        baseline = _block_bootstrap_mean(baseline_rows, key, seed=42)
        excl = _block_bootstrap_mean(excluded_rows, key, seed=42) if excluded_rows else None

        b_flag = "CROSSES ZERO" if baseline["ci_95_crosses_zero"] else "does NOT cross zero"
        print(f"\n--- {h}-month horizon ---")
        print(f"  baseline (all):      n={len(baseline_rows):<4} groups={baseline['n_groups']:<4} "
              f"mean={baseline['observed_mean']:+.1%}  CI=[{baseline['ci_95_low']:+.1%}, {baseline['ci_95_high']:+.1%}]  {b_flag}")
        if excl:
            e_flag = "CROSSES ZERO" if excl["ci_95_crosses_zero"] else "does NOT cross zero"
            print(f"  sector-excluded:     n={len(excluded_rows):<4} groups={excl['n_groups']:<4} "
                  f"mean={excl['observed_mean']:+.1%}  CI=[{excl['ci_95_low']:+.1%}, {excl['ci_95_high']:+.1%}]  {e_flag}")
        results[f"{h}mo"] = {"baseline": baseline, "sector_excluded": excl}

    RESULT_PATH.write_text(json.dumps({
        "excluded_tickers": sorted(excluded), "semiconductor_tickers": sorted(semiconductor_tickers),
        "results_by_horizon": results, "episodes": enriched,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
