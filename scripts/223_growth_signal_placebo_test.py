"""User-directed via council process (2026-08-13 session): Advisor D
(backtesting/model-risk) proposed a placebo/permutation test before
trusting D-079's growth-rate finding with real capital decisions --
run the EXACT SAME statistical procedure (company-grouped block
bootstrap correlation, D-079's own methodology, unchanged) on a
variable KNOWN to carry no information, attached to the same real
company-quarters and the same real excess returns, and see how often
this specific procedure manufactures a "CI excludes zero" result out
of pure noise.

**Method**: the `current_yoy_growth` column is randomly shuffled across
all 464 rows (breaking any true growth<->return relationship while
preserving each value's real empirical distribution, the real ticker
groups, and the real excess_return values exactly as observed) --
repeated N_PERMUTATIONS times, recomputing the identical block-
bootstrap correlation each time. If a meaningless, shuffled variable
"passes" (CI excludes zero) close to the nominal 5% of the time, the
procedure is well-calibrated and D-079's real result is not explained
by this specific concern. If it passes noticeably more often than 5%,
that is a direct, quantified problem with trusting single "CI excludes
zero" results from this pipeline without correction.

**Scope, stated plainly**: this tests calibration of the CORRELATION
test itself, not the additional "search for the best quintile
threshold" degree of freedom D-080 introduced after the correlation was
already found significant -- that is a separate, larger concern
(discovering the ~20% cutoff by looking at the data) not fully covered
here. Flagged as a natural follow-up, not attempted in this first pass
for tractability (a full threshold-search permutation test is much more
expensive to run correctly).

READ-ONLY relative to production (reads the already-computed D-079/
D-080 dataset, writes only a local result JSON).

    .venv\\Scripts\\python.exe scripts\\223_growth_signal_placebo_test.py
"""

from __future__ import annotations

import json
import random

from stock_agent import DATA_DIR
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation

SOURCE_PATH = DATA_DIR / "quarterly_growth_rate_regime_and_quintile_check_result.json"
RESULT_PATH = DATA_DIR / "growth_signal_placebo_test_result.json"

N_PERMUTATIONS = 200
N_RESAMPLES_PER_PERMUTATION = 5000  # same as D-079's own bootstrap depth


def main() -> None:
    dataset = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))["dataset"]
    print(f"source dataset: {len(dataset)} rows, {len({r['ticker'] for r in dataset})} tickers")

    # Real (unpermuted) result, reproduced here for a direct side-by-side reference.
    real_result = block_bootstrap_correlation(
        dataset, "current_yoy_growth", "excess_return", group_key="ticker",
        n_resamples=N_RESAMPLES_PER_PERMUTATION, seed=13,
    )
    print(f"\nREAL (unpermuted) result: corr={real_result['observed_correlation']:+.3f}  "
          f"CI=[{real_result['ci_95_low']:+.3f}, {real_result['ci_95_high']:+.3f}]  "
          f"crosses_zero={real_result['ci_95_crosses_zero']}")

    growth_values = [r["current_yoy_growth"] for r in dataset]
    rng = random.Random(42)

    n_false_positive = 0
    permutation_correlations = []
    for i in range(N_PERMUTATIONS):
        shuffled_growth = growth_values.copy()
        rng.shuffle(shuffled_growth)
        permuted_dataset = [
            {"ticker": r["ticker"], "excess_return": r["excess_return"], "current_yoy_growth": shuffled_growth[j]}
            for j, r in enumerate(dataset)
        ]
        result = block_bootstrap_correlation(
            permuted_dataset, "current_yoy_growth", "excess_return", group_key="ticker",
            n_resamples=N_RESAMPLES_PER_PERMUTATION, seed=100 + i,
        )
        permutation_correlations.append(result["observed_correlation"])
        if not result["ci_95_crosses_zero"]:
            n_false_positive += 1
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{N_PERMUTATIONS} permutations done, "
                  f"{n_false_positive} false-positive 'significant' results so far", flush=True)

    false_positive_rate = n_false_positive / N_PERMUTATIONS
    abs_perm_corrs = sorted(abs(c) for c in permutation_correlations)
    real_abs_corr = abs(real_result["observed_correlation"])
    n_permutations_as_extreme_or_more = sum(1 for c in abs_perm_corrs if c >= real_abs_corr)
    permutation_p_value = n_permutations_as_extreme_or_more / N_PERMUTATIONS

    print("\n" + "=" * 100)
    print("PLACEBO TEST RESULT")
    print("=" * 100)
    print(f"Real growth variable: corr={real_result['observed_correlation']:+.3f}, "
          f"CI excludes zero: {not real_result['ci_95_crosses_zero']}")
    print(f"Shuffled (noise) growth variable, {N_PERMUTATIONS} independent permutations:")
    print(f"  false-positive rate (CI excludes zero purely by chance): "
          f"{n_false_positive}/{N_PERMUTATIONS} = {false_positive_rate:.1%}")
    print(f"  (nominal expectation for a well-calibrated 95% CI: ~5%)")
    print(f"  permutation p-value (how many of {N_PERMUTATIONS} random shuffles produced a "
          f"|correlation| >= the real one): {n_permutations_as_extreme_or_more}/{N_PERMUTATIONS} = {permutation_p_value:.1%}")

    if false_positive_rate > 0.10:
        print("\n-> WARNING: false-positive rate is meaningfully above the 5% nominal level. "
              "The bootstrap procedure itself may be under-covering (CIs too narrow) on data shaped like this "
              "(86 groups, this sample size) -- a real calibration concern independent of whether growth is real.")
    else:
        print("\n-> False-positive rate is close to the 5% nominal level -- the procedure appears well-"
              "calibrated on data shaped like this. The real growth result is not explained by miscalibration.")

    RESULT_PATH.write_text(json.dumps({
        "n_permutations": N_PERMUTATIONS, "n_resamples_per_permutation": N_RESAMPLES_PER_PERMUTATION,
        "real_result": real_result, "false_positive_rate": false_positive_rate,
        "n_false_positive": n_false_positive, "permutation_p_value": permutation_p_value,
        "permutation_correlations": permutation_correlations,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
