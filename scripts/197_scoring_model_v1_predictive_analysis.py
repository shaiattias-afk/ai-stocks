"""Analyzes whether Scoring Model V1's composite score (or any single
factor) predicts beating QQQ (Nasdaq-100) by >= 5% over the following
12 months, across the full ~150-company universe (D-051-D-056 plus
scripts/196's scoring extension).

READ-ONLY. Writes one result JSON (no production table -- an analysis
output, not a per-company-year lineage-tracked fact).

    .venv\\Scripts\\python.exe scripts\\197_scoring_model_v1_predictive_analysis.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.predictive_analysis_v1 import (
    build_predictive_dataset,
    decile_analysis,
    factor_correlations,
)

RESULT_PATH = DATA_DIR / "scoring_model_v1_predictive_analysis_result.json"
HORIZON_MONTHS = 12
BEAT_MARGIN = 0.05


def summarize(dataset: list[dict]) -> dict:
    if not dataset:
        return {"n": 0}
    excess_values = sorted(r["excess_return"] for r in dataset)
    n = len(dataset)
    return {
        "n": n,
        "mean_excess_return": sum(excess_values) / n,
        "median_excess_return": excess_values[n // 2],
        "beat_5pct_rate": sum(1 for r in dataset if r["beats_by_5pct"]) / n,
    }


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    full_dataset = build_predictive_dataset(connection, horizon_months=HORIZON_MONTHS, min_weight_covered=0.0)
    quality_dataset = build_predictive_dataset(connection, horizon_months=HORIZON_MONTHS, min_weight_covered=0.7)
    connection.close()

    print("=" * 92)
    print(f"SCORING MODEL V1 -- predictive analysis, {HORIZON_MONTHS}mo horizon, beat-QQQ-by-{BEAT_MARGIN:.0%} label")
    print("=" * 92)

    print(f"\nFull dataset (any weight_covered): {summarize(full_dataset)}")
    print(f"Quality subset (weight_covered >= 70%): {summarize(quality_dataset)}")

    print("\n--- Decile analysis (composite_score, full dataset) ---")
    full_deciles = decile_analysis(full_dataset, n_buckets=5)
    for b in full_deciles:
        print(f"  bucket {b['bucket']} (score {b['score_range'][0]:.0f}-{b['score_range'][1]:.0f}, n={b['n']}): "
              f"mean_excess={b['mean_excess_return']:.1%} beat_5pct_rate={b['beat_5pct_rate']:.0%}")

    print("\n--- Decile analysis (composite_score, quality subset weight_covered>=70%) ---")
    quality_deciles = decile_analysis(quality_dataset, n_buckets=5)
    for b in quality_deciles:
        print(f"  bucket {b['bucket']} (score {b['score_range'][0]:.0f}-{b['score_range'][1]:.0f}, n={b['n']}): "
              f"mean_excess={b['mean_excess_return']:.1%} beat_5pct_rate={b['beat_5pct_rate']:.0%}")

    print("\n--- Factor correlations with excess_return (full dataset) ---")
    full_correlations = factor_correlations(full_dataset)
    for factor, stats in sorted(full_correlations.items(), key=lambda kv: -(kv[1]["spearman_correlation"] or -999)):
        corr = stats["spearman_correlation"]
        corr_str = f"{corr:+.3f}" if corr is not None else "N/A"
        print(f"  {factor:<32} n={stats['n']:>4}  spearman={corr_str}")

    print("\n--- Factor correlations with excess_return (quality subset) ---")
    quality_correlations = factor_correlations(quality_dataset)
    for factor, stats in sorted(quality_correlations.items(), key=lambda kv: -(kv[1]["spearman_correlation"] or -999)):
        corr = stats["spearman_correlation"]
        corr_str = f"{corr:+.3f}" if corr is not None else "N/A"
        print(f"  {factor:<32} n={stats['n']:>4}  spearman={corr_str}")

    payload = {
        "horizon_months": HORIZON_MONTHS, "beat_margin": BEAT_MARGIN,
        "full_dataset_summary": summarize(full_dataset),
        "quality_dataset_summary": summarize(quality_dataset),
        "full_dataset_deciles": full_deciles,
        "quality_dataset_deciles": quality_deciles,
        "full_dataset_factor_correlations": full_correlations,
        "quality_dataset_factor_correlations": quality_correlations,
        "full_dataset": full_dataset,
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
