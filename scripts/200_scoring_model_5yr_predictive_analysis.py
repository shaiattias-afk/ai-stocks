"""Answers the user's actual question: does the composite score predict
beating Nasdaq-100 by 5%+ PER YEAR ON AVERAGE, held for 5 years (60
months), not the 12-month framing scripts/197 and 199 used?

Uses annualized (CAGR) excess return -- the correct basis for a
"X% per year, on average" question over a multi-year hold -- via
predictive_analysis_v1.build_predictive_dataset's annualized_* fields.

*** Sample-size caveat, checked and reported before any other number:
a 60-month forward return needs 5 full years of FUTURE price data past
the entry date. With price data ending 2026-08-10, only company-years
filed on or before ~2021-08 qualify at all -- a small, early-COVID-era
slice of the dataset, not a broad multi-cycle test. This is stated
here, not discovered after the fact. ***

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\200_scoring_model_5yr_predictive_analysis.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.predictive_analysis_v1 import (
    build_predictive_dataset,
    decile_analysis,
    factor_correlations,
    spearman_correlation,
)

RESULT_PATH = DATA_DIR / "scoring_model_5yr_predictive_analysis_result.json"
HORIZON_MONTHS = 60
BEAT_MARGIN = 0.05


def summarize(dataset: list[dict]) -> dict:
    if not dataset:
        return {"n": 0}
    excess_values = sorted(r["annualized_excess_return"] for r in dataset)
    n = len(excess_values)
    return {
        "n": n,
        "mean_annualized_excess_return": sum(excess_values) / n,
        "median_annualized_excess_return": excess_values[n // 2],
        "beat_5pct_per_year_rate": sum(1 for r in dataset if r["beats_by_5pct_annualized"]) / n,
        "distinct_fiscal_years_of_entry": sorted({r["fiscal_year"] for r in dataset}),
        "distinct_tickers": sorted({r["ticker"] for r in dataset}),
    }


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    dataset = build_predictive_dataset(connection, horizon_months=HORIZON_MONTHS, min_weight_covered=0.0)
    connection.close()

    print("=" * 92)
    print(f"SCORING MODEL V1 -- {HORIZON_MONTHS}-month (5-year) hold, beat-QQQ-by-{BEAT_MARGIN:.0%}/year label")
    print("SAMPLE-SIZE CAVEAT: only company-years filed early enough to have 5 full years of")
    print("forward price data qualify at all -- a small, early-window slice, not a broad test.")
    print("=" * 92)

    summary = summarize(dataset)
    print(f"\n{json.dumps(summary, indent=2, default=str)}")

    if summary["n"] >= 15:
        # Rank-based composite-score correlation, using ANNUALIZED excess
        # return (the ranking metric composite_v1 itself never changes --
        # only which return column is correlated against it here).
        pairs = [(r["composite_score"], r["annualized_excess_return"]) for r in dataset]
        corr = spearman_correlation(pairs)
        print(f"\nSpearman correlation (composite_score vs. annualized excess return): "
              f"{corr:+.3f}" if corr is not None else "\nSpearman correlation: N/A (too few or no-variance)")

        # decile_analysis reads "excess_return" -- reuse it by relabeling
        # the annualized figure into that key for this one call.
        relabeled = [{**r, "excess_return": r["annualized_excess_return"],
                     "beats_by_5pct": r["beats_by_5pct_annualized"]} for r in dataset]
        n_buckets = min(5, summary["n"] // 3) or 1
        deciles = decile_analysis(relabeled, n_buckets=n_buckets)
        print(f"\nBucket analysis ({n_buckets} buckets, composite_score):")
        for b in deciles:
            print(f"  bucket {b['bucket']} (score {b['score_range'][0]:.0f}-{b['score_range'][1]:.0f}, n={b['n']}): "
                  f"mean_annualized_excess={b['mean_excess_return']:.1%} beat_5pct_rate={b['beat_5pct_rate']:.0%}")

        factor_stats = factor_correlations(relabeled)
        print("\nPer-factor correlation with annualized excess return:")
        for factor, stats in sorted(factor_stats.items(), key=lambda kv: -(kv[1]["spearman_correlation"] or -999)):
            c = stats["spearman_correlation"]
            print(f"  {factor:<32} n={stats['n']:>4}  spearman={f'{c:+.3f}' if c is not None else 'N/A'}")
    else:
        corr, deciles, factor_stats = None, [], {}
        print(f"\nToo few company-years (n={summary['n']}) with a complete 5-year forward window "
              "for any correlation/bucket analysis to mean anything -- reporting the raw dataset only.")
        for r in dataset:
            print(f"  {r['ticker']:<6} {r['report_date']}  score={r['composite_score']:.0f}  "
                  f"annualized_excess={r['annualized_excess_return']:+.1%}  "
                  f"beats_5pct/yr={r['beats_by_5pct_annualized']}")

    payload = {
        "horizon_months": HORIZON_MONTHS, "beat_margin": BEAT_MARGIN,
        "summary": summary, "spearman_correlation": corr,
        "deciles": deciles, "factor_correlations": factor_stats,
        "dataset": dataset,
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
