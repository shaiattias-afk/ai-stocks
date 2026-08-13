"""Direct follow-up (2026-08-13 session): D-092 showed Group A
(growth+pullback) beats Group B (pullback alone) within the
semiconductor/AI universe at every horizon, but never ran a formal
significance test on the GAP itself (D-081's own flagged gap, still
open per CLAUDE.md's priority list item (d)) -- only reported each
group's own bootstrap separately. This resamples both groups (each
grouped by its own ticker) together per iteration and reports the
distribution of (mean_A - mean_B), the honest test of whether the
growth filter's added value is itself statistically real or could be
sampling noise between two already-similar numbers.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\236_group_a_vs_b_significance_within_sector.py
"""

from __future__ import annotations

import json
import random

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return

PULLBACK_SOURCE = DATA_DIR / "pullback_recovery_full_universe_result.json"
SECTOR_SOURCE = DATA_DIR / "pullback_excess_return_sector_exclusion_check_result.json"
RESULT_PATH = DATA_DIR / "group_a_vs_b_significance_within_sector_result.json"

HORIZONS = (6, 12, 24)


def main() -> None:
    sector_data = json.loads(SECTOR_SOURCE.read_text(encoding="utf-8"))
    semi_tickers = set(sector_data["excluded_tickers"])
    group_a = [e for e in sector_data["episodes"] if e["excluded"]]

    pullback_source = json.loads(PULLBACK_SOURCE.read_text(encoding="utf-8"))
    group_b_raw = [e for e in pullback_source["group_b_episodes"] if e["ticker"] in semi_tickers]

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    group_b = []
    for e in group_b_raw:
        row = {"ticker": e["ticker"], "entry_date": e["entry_date"]}
        for h in HORIZONS:
            sf = _forward_return(connection, e["ticker"], e["entry_date"], h)
            bf = _forward_return(connection, BENCHMARK_TICKER, e["entry_date"], h)
            row[f"excess_return_{h}mo"] = sf["return"] - bf["return"] if sf and bf else None
        group_b.append(row)
    connection.close()

    print("=" * 100)
    print("Formal significance test: does Group A (growth+pullback) beat Group B (pullback alone) "
          "within the semiconductor/AI universe?")
    print("=" * 100)

    results = {}
    for h in HORIZONS:
        key = f"excess_return_{h}mo"
        a_rows = [e for e in group_a if e[key] is not None]
        b_rows = [e for e in group_b if e[key] is not None]

        a_groups: dict[str, list[float]] = {}
        for r in a_rows:
            a_groups.setdefault(r["ticker"], []).append(r[key])
        b_groups: dict[str, list[float]] = {}
        for r in b_rows:
            b_groups.setdefault(r["ticker"], []).append(r[key])

        observed_diff = (sum(r[key] for r in a_rows) / len(a_rows)) - (sum(r[key] for r in b_rows) / len(b_rows))

        a_keys, b_keys = list(a_groups.keys()), list(b_groups.keys())
        rng = random.Random(42)
        diffs = []
        for _ in range(5000):
            a_chosen = [rng.choice(a_keys) for _ in a_keys]
            b_chosen = [rng.choice(b_keys) for _ in b_keys]
            a_vals = [v for k in a_chosen for v in a_groups[k]]
            b_vals = [v for k in b_chosen for v in b_groups[k]]
            diffs.append(sum(a_vals) / len(a_vals) - sum(b_vals) / len(b_vals))
        diffs.sort()
        n = len(diffs)
        lo = diffs[int(round(0.025 * (n - 1)))]
        hi = diffs[int(round(0.975 * (n - 1)))]
        crosses_zero = lo <= 0 <= hi

        flag = "CROSSES ZERO -- gap not significant" if crosses_zero else "does NOT cross zero -- gap IS significant"
        print(f"\n{h}mo: Group A mean={sum(r[key] for r in a_rows)/len(a_rows):+.1%} (n={len(a_rows)}, {len(a_groups)} tickers)  "
              f"Group B mean={sum(r[key] for r in b_rows)/len(b_rows):+.1%} (n={len(b_rows)}, {len(b_groups)} tickers)")
        print(f"      difference (A - B) = {observed_diff:+.1%}  95% CI=[{lo:+.1%}, {hi:+.1%}]  {flag}")

        results[f"{h}mo"] = {
            "group_a_mean": sum(r[key] for r in a_rows) / len(a_rows), "group_a_n": len(a_rows), "group_a_tickers": len(a_groups),
            "group_b_mean": sum(r[key] for r in b_rows) / len(b_rows), "group_b_n": len(b_rows), "group_b_tickers": len(b_groups),
            "observed_diff": observed_diff, "ci_95_low": lo, "ci_95_high": hi, "ci_95_crosses_zero": crosses_zero,
        }

    RESULT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
