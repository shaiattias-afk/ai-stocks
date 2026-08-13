"""User-directed correction (2026-08-13 session): D-081/D-085/D-086/D-087
all measured the pullback/recovery signal by an ABSOLUTE metric -- did the
stock climb back to ITS OWN prior 252-day high -- not by the project's
actual stated goal (CLAUDE.md: "Primary benchmark: Nasdaq-100 (QQQ)").
The user caught this directly: in a falling market, a stock can fail to
reclaim its own old high while still beating a QQQ that fell even further.
D-079/D-080's growth-rate correlation already used excess return vs QQQ
correctly -- this script applies the same standard to the pullback/
recovery episodes, to see whether D-087's "30% vs 90% recovery pre/post-
2023" finding survives once measured the right way.

**Method**: reuses D-081's exact same 136 Group A episodes (growth>20%
AND pullback>=15%, `data/pullback_recovery_full_universe_result.json`)
and each episode's own `entry_date` -- but computes forward EXCESS return
vs QQQ over the same 6/12/24-month windows (`backtest_v1._forward_return`,
the same helper D-079/D-080/the placebo test already used), instead of
re-deriving anything from the recovery-to-high logic.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\227_pullback_signal_excess_return_check.py
"""

from __future__ import annotations

import json
import random

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return

SOURCE_PATH = DATA_DIR / "pullback_recovery_full_universe_result.json"
RESULT_PATH = DATA_DIR / "pullback_signal_excess_return_check_result.json"

HORIZONS = (6, 12, 24, 36, 60)


def _block_bootstrap_mean(rows: list[dict], value_key: str, group_key: str = "ticker",
                           n_resamples: int = 5000, seed: int = 0) -> dict:
    """Same ticker-grouped block-bootstrap discipline as
    cohort_robustness_v1.block_bootstrap_correlation (D-063), applied to
    a single variable's mean instead of a correlation -- whole tickers
    resampled together so no repeated-episode ticker is double-counted
    as independent evidence."""
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
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    episodes = source["group_a_episodes"]
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    enriched = []
    for e in episodes:
        row = dict(e)
        row["excess_return"] = {}
        for h in HORIZONS:
            stock_fwd = _forward_return(connection, e["ticker"], e["entry_date"], h)
            bench_fwd = _forward_return(connection, BENCHMARK_TICKER, e["entry_date"], h)
            if stock_fwd is None or bench_fwd is None:
                row["excess_return"][f"{h}mo"] = None
            else:
                row["excess_return"][f"{h}mo"] = {
                    "stock_return": stock_fwd["return"], "qqq_return": bench_fwd["return"],
                    "excess_return": stock_fwd["return"] - bench_fwd["return"],
                }
        enriched.append(row)

    connection.close()

    print(f"{len(enriched)} episodes enriched with forward excess return vs QQQ\n")
    print("=" * 100)
    print("Excess return vs QQQ, split by entry period -- the corrected version of D-087's year split")
    print("=" * 100)

    def bucket_stats(label: str, entries: list[dict], horizon_key: str) -> dict:
        vals = [e["excess_return"][horizon_key]["excess_return"] for e in entries
                if e["excess_return"][horizon_key] is not None]
        if not vals:
            print(f"  {label:<22} n=0")
            return {"n": 0}
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        median = vals_sorted[n // 2] if n % 2 else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2
        mean = sum(vals_sorted) / n
        n_beat = sum(1 for v in vals_sorted if v > 0)
        print(f"  {label:<22} n={n:<4} median_excess={median:+.1%}  mean_excess={mean:+.1%}  "
              f"beat_QQQ={n_beat}/{n} ({n_beat/n:.0%})")
        return {"n": n, "median_excess_return": median, "mean_excess_return": mean,
                "n_beat_qqq": n_beat, "beat_qqq_rate": n_beat / n}

    results_by_horizon = {}
    for h in HORIZONS:
        hk = f"{h}mo"
        print(f"\n--- {h}-month horizon ---")
        pre2023 = [e for e in enriched if e["entry_date"] < "2023-01-01"]
        post2023 = [e for e in enriched if e["entry_date"] >= "2023-01-01"]
        results_by_horizon[hk] = {
            "pre_2023": bucket_stats("entries pre-2023 (2021-2022)", pre2023, hk),
            "post_2023": bucket_stats("entries 2023 onward", post2023, hk),
            "all": bucket_stats("all entries", enriched, hk),
        }

    print("\n" + "=" * 100)
    print("For direct comparison: the ABSOLUTE 'recovered to own prior high' rate already on record (D-087)")
    print("=" * 100)
    for h in HORIZONS:
        hk = f"{h}mo"
        if hk not in episodes[0]["recovery"]:
            continue  # D-081's original recovery-to-high computation only covers 6/12/24mo
        det = [e for e in episodes if not e["recovery"][hk]["censored"]]
        pre = [e for e in det if e["entry_date"] < "2023-01-01"]
        post = [e for e in det if e["entry_date"] >= "2023-01-01"]
        pre_rate = sum(1 for e in pre if e["recovery"][hk]["recovered"]) / len(pre) if pre else None
        post_rate = sum(1 for e in post if e["recovery"][hk]["recovered"]) / len(post) if post else None
        print(f"  {h}mo: pre-2023 recovered-to-own-high={pre_rate:.0%} (n={len(pre)})  "
              if pre_rate is not None else f"  {h}mo: pre-2023 n=0  ", end="")
        print(f"post-2023 recovered-to-own-high={post_rate:.0%} (n={len(post)})"
              if post_rate is not None else "post-2023 n=0")

    print("\n" + "=" * 100)
    print("Ticker-grouped block bootstrap: is the mean excess return significantly different from zero?")
    print("=" * 100)
    bootstrap_results = {}
    for h in HORIZONS:
        hk = f"{h}mo"
        rows = [{"ticker": e["ticker"], "excess_return": e["excess_return"][hk]["excess_return"]}
                for e in enriched if e["excess_return"][hk] is not None]
        result = _block_bootstrap_mean(rows, "excess_return", seed=42)
        flag = "CROSSES ZERO" if result["ci_95_crosses_zero"] else "does NOT cross zero"
        print(f"  {hk}: n={len(rows)}  groups={result['n_groups']}  mean_excess={result['observed_mean']:+.1%}  "
              f"CI=[{result['ci_95_low']:+.1%}, {result['ci_95_high']:+.1%}]  {flag}")
        bootstrap_results[hk] = result

    RESULT_PATH.write_text(json.dumps({
        "n_episodes": len(enriched), "results_by_horizon": results_by_horizon,
        "mean_excess_return_bootstrap": bootstrap_results, "episodes": enriched,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
