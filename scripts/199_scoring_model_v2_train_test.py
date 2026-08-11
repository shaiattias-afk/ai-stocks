"""Builds Scoring Model V2's candidate weights on a TRAIN period only
(filings before 2023-01-01) and validates them purely OUT OF SAMPLE on
a TEST period (filings on/after 2023-01-01) -- see stock_agent.scoring.
model_v2_candidate's own module docstring for why this split, and why
factor directions are fixed rather than fit.

READ-ONLY. Writes one result JSON (no production table -- this is
model research, not a per-company-year lineage-tracked fact).

    .venv\\Scripts\\python.exe scripts\\199_scoring_model_v2_train_test.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.composite_v1 import FACTOR_WEIGHTS as V1_FACTOR_WEIGHTS
from stock_agent.scoring.composite_v1 import compute_composite_scores_v1
from stock_agent.scoring.model_v2_candidate import (
    build_dataset_with_valuation,
    select_and_weight_factors_from_train,
)
from stock_agent.scoring.predictive_analysis_v1 import decile_analysis, spearman_correlation

RESULT_PATH = DATA_DIR / "scoring_model_v2_train_test_result.json"
TRAIN_TEST_CUTOFF = "2023-01-01"
HORIZON_MONTHS = 12
BEAT_MARGIN = 0.05
MIN_ABS_CORRELATION = 0.03


def summarize(rows: list[dict]) -> dict:
    resolvable = [r for r in rows if r["composite_score"] is not None and r["excess_return"] is not None]
    if not resolvable:
        return {"n": 0}
    pairs = [(r["composite_score"], r["excess_return"]) for r in resolvable]
    excess_sorted = sorted(r["excess_return"] for r in resolvable)
    n = len(resolvable)
    return {
        "n": n,
        "spearman_correlation": spearman_correlation(pairs),
        "mean_excess_return": sum(excess_sorted) / n,
        "median_excess_return": excess_sorted[n // 2],
        "beat_5pct_rate": sum(1 for r in resolvable if r["excess_return"] >= BEAT_MARGIN) / n,
    }


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    full_dataset = build_dataset_with_valuation(connection, horizon_months=HORIZON_MONTHS)
    connection.close()

    train = [r for r in full_dataset if r["filing_date"] < TRAIN_TEST_CUTOFF and r["excess_return"] is not None]
    test = [r for r in full_dataset if r["filing_date"] >= TRAIN_TEST_CUTOFF and r["excess_return"] is not None]
    print(f"train (filed < {TRAIN_TEST_CUTOFF}): {len(train)} company-years")
    print(f"test  (filed >= {TRAIN_TEST_CUTOFF}): {len(test)} company-years")

    v2_weights = select_and_weight_factors_from_train(train, min_abs_correlation=MIN_ABS_CORRELATION)
    print(f"\nV2 candidate weights (selected on TRAIN only, min_abs_correlation={MIN_ABS_CORRELATION}):")
    if not v2_weights:
        print("  (nothing cleared the bar -- no factor showed even a weak positive train-period relationship)")
    for factor, (weight, invert) in sorted(v2_weights.items(), key=lambda kv: -kv[1][0]):
        print(f"  {factor:<32} weight={weight:.1%}  invert={invert}")

    def score(dataset: list[dict], weights) -> list[dict]:
        inputs = [{k: r[k] for k in ("ticker", "report_date", "fiscal_year", *weights)} for r in dataset]
        composites = compute_composite_scores_v1(inputs, factor_weights=weights)
        out = []
        for c, r in zip(composites, dataset):
            row = {**c, "excess_return": r["excess_return"]}
            row["beats_by_5pct"] = row["excess_return"] is not None and row["excess_return"] >= BEAT_MARGIN
            out.append(row)
        return out

    print("\n" + "=" * 92)
    print("V1 (original 9-factor, fixed weights) -- TEST period only")
    print("=" * 92)
    v1_test_scored = score(test, V1_FACTOR_WEIGHTS)
    v1_test_summary = summarize(v1_test_scored)
    print(json.dumps(v1_test_summary, indent=2))

    if v2_weights:
        print("\n" + "=" * 92)
        print("V2 candidate (train-selected factors + valuation) -- TEST period only (true out-of-sample)")
        print("=" * 92)
        v2_test_scored = score(test, v2_weights)
        v2_test_summary = summarize(v2_test_scored)
        print(json.dumps(v2_test_summary, indent=2))

        # Fair, same-population comparison: V1 and V2 don't necessarily
        # resolve for the exact same company-years (V2 uses fewer, more
        # specific factors) -- restricting to the intersection isolates
        # the MODEL's difference from a difference in which rows each
        # one happened to score at all.
        v1_by_key = {(r["ticker"], r["report_date"]): r for r in v1_test_scored if r["composite_score"] is not None}
        v2_by_key = {(r["ticker"], r["report_date"]): r for r in v2_test_scored if r["composite_score"] is not None}
        common_keys = set(v1_by_key) & set(v2_by_key)
        v1_common_summary = summarize([v1_by_key[k] for k in common_keys])
        v2_common_summary = summarize([v2_by_key[k] for k in common_keys])
        print(f"\nSame-population comparison (n={len(common_keys)}, company-years both models scored):")
        print(f"  V1: {json.dumps(v1_common_summary)}")
        print(f"  V2: {json.dumps(v2_common_summary)}")

        print("\nV2 decile analysis on TEST:")
        v2_deciles = decile_analysis(
            [r for r in v2_test_scored if r["composite_score"] is not None and r["excess_return"] is not None],
            n_buckets=5,
        )
        for b in v2_deciles:
            print(f"  bucket {b['bucket']} (score {b['score_range'][0]:.0f}-{b['score_range'][1]:.0f}, n={b['n']}): "
                  f"mean_excess={b['mean_excess_return']:.1%} beat_5pct_rate={b['beat_5pct_rate']:.0%}")
    else:
        v2_test_summary, v2_deciles = {"n": 0, "note": "no V2 model built -- no factor cleared the train-period bar"}, []
        v1_common_summary = v2_common_summary = {}

    # Also report V2's OWN train-period fit, for transparency about the
    # gap between in-sample (train) and out-of-sample (test) performance
    # -- a large gap is itself an important, honestly-reported finding.
    v2_train_summary = {}
    if v2_weights:
        v2_train_scored = score(train, v2_weights)
        v2_train_summary = summarize(v2_train_scored)

    payload = {
        "train_test_cutoff": TRAIN_TEST_CUTOFF, "horizon_months": HORIZON_MONTHS, "beat_margin": BEAT_MARGIN,
        "min_abs_correlation": MIN_ABS_CORRELATION,
        "train_n": len(train), "test_n": len(test),
        "v2_weights": {k: {"weight": w, "invert": inv} for k, (w, inv) in v2_weights.items()},
        "v1_test_summary": v1_test_summary,
        "v2_test_summary": v2_test_summary,
        "v1_common_population_summary": v1_common_summary,
        "v2_common_population_summary": v2_common_summary,
        "v2_train_summary": v2_train_summary,
        "v2_test_deciles": v2_deciles,
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
