"""Direct follow-up (2026-08-13 session) to D-091: since the growth+
pullback signal's excess-return edge turned out to be entirely carried
by semiconductor/AI names, the user asked the natural next question --
what if we deliberately SCOPE to that universe instead of treating it as
a confound to remove? This checks two things: (1) how strong is the
growth+pullback signal's excess return WITHIN just the 10 semiconductor/
AI names (the mirror image of D-091's exclusion); (2) does the
growth>20% filter add anything within that universe, or does simply
buying any pullback in these names already capture most of the edge
(i.e. is this a stock-picking rule or just a sector-timing bet).

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\232_semiconductor_ai_focused_universe_check.py
"""

from __future__ import annotations

import json
import random

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return

PULLBACK_SOURCE = DATA_DIR / "pullback_recovery_full_universe_result.json"
SECTOR_SOURCE = DATA_DIR / "pullback_excess_return_sector_exclusion_check_result.json"
RESULT_PATH = DATA_DIR / "semiconductor_ai_focused_universe_check_result.json"

HORIZONS = (6, 12, 24)


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
            "n": len(rows), "n_beat_qqq": beat, "beat_qqq_rate": beat / len(rows)}


def main() -> None:
    sector_data = json.loads(SECTOR_SOURCE.read_text(encoding="utf-8"))
    semi_tickers = set(sector_data["excluded_tickers"])
    print(f"semiconductor/AI universe (10 tickers): {sorted(semi_tickers)}\n")

    group_a_semi = [e for e in sector_data["episodes"] if e["excluded"]]

    pullback_source = json.loads(PULLBACK_SOURCE.read_text(encoding="utf-8"))
    group_b_semi_raw = [e for e in pullback_source["group_b_episodes"] if e["ticker"] in semi_tickers]

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    group_b_semi = []
    for e in group_b_semi_raw:
        row = {"ticker": e["ticker"], "entry_date": e["entry_date"]}
        for h in HORIZONS:
            sf = _forward_return(connection, e["ticker"], e["entry_date"], h)
            bf = _forward_return(connection, BENCHMARK_TICKER, e["entry_date"], h)
            row[f"excess_return_{h}mo"] = sf["return"] - bf["return"] if sf and bf else None
        group_b_semi.append(row)
    connection.close()

    print("=" * 100)
    print("Group A (growth>20% + pullback>=15%) vs Group B (pullback alone, no growth filter) -- semi/AI universe only")
    print("=" * 100)
    results = {}
    for h in HORIZONS:
        key = f"excess_return_{h}mo"
        a_rows = [e for e in group_a_semi if e[key] is not None]
        b_rows = [e for e in group_b_semi if e[key] is not None]
        a_result = _block_bootstrap_mean(a_rows, key)
        b_result = _block_bootstrap_mean(b_rows, key)
        print(f"\n--- {h}-month horizon ---")
        print(f"  Group A (growth+pullback): n={a_result['n']:<4} groups={a_result['n_groups']:<3} "
              f"mean={a_result['observed_mean']:+.1%}  CI=[{a_result['ci_95_low']:+.1%}, {a_result['ci_95_high']:+.1%}]  "
              f"beat_QQQ={a_result['n_beat_qqq']}/{a_result['n']} ({a_result['beat_qqq_rate']:.0%})")
        print(f"  Group B (pullback alone):  n={b_result['n']:<4} groups={b_result['n_groups']:<3} "
              f"mean={b_result['observed_mean']:+.1%}  CI=[{b_result['ci_95_low']:+.1%}, {b_result['ci_95_high']:+.1%}]  "
              f"beat_QQQ={b_result['n_beat_qqq']}/{b_result['n']} ({b_result['beat_qqq_rate']:.0%})")
        results[f"{h}mo"] = {"group_a": a_result, "group_b": b_result}

    RESULT_PATH.write_text(json.dumps({
        "semiconductor_ai_tickers": sorted(semi_tickers), "results_by_horizon": results,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
