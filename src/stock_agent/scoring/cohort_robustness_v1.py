"""
scoring/cohort_robustness_v1.py -- Council Proposal 1 (D-063 follow-up,
council session 2026-08-11/12): the 5-year-hold P/E finding (-0.247,
n=102) was measured on a sample where 109 of 133 company-years share
one entry cohort (fiscal year 2020) -- advisors ג' (data engineer) and
ד' (backtesting/model-risk) independently reached the same diagnosis:
the reported n overstates how much independent information is really
there, because most rows are not independent observations of "does
cheap P/E predict returns" -- they are ~100 repeated observations of
"what happened to the market in one specific year."

This module answers the question honestly rather than trusting the
naive correlation: a block bootstrap resampled by TICKER (not by row)
-- if a company happens to appear more than once in the small sample,
all of its rows move together in each resample, so no single company's
idiosyncratic result can be double-counted as if it were several
independent data points. This does NOT fix the deeper problem (the
whole sample still sits inside one macro window) -- it only removes
the narrower, correctable distortion of treating correlated rows as
independent ones. The single-cohort problem itself needs genuinely new,
independent time periods (more price history), not a resampling trick.
"""

from __future__ import annotations

import random

from stock_agent.scoring.predictive_analysis_v1 import spearman_correlation


def block_bootstrap_correlation(
    rows: list[dict[str, object]],
    x_key: str,
    y_key: str,
    group_key: str = "ticker",
    n_resamples: int = 2000,
    seed: int = 0,
) -> dict[str, object]:
    """Resamples whole GROUPS (default: all of one ticker's rows
    together), with replacement, `n_resamples` times, recomputing the
    Spearman correlation each time. Returns the observed (real, un-
    resampled) correlation plus the bootstrap distribution's mean,
    standard deviation, and a 95% percentile interval -- the honest
    uncertainty range, not a single point estimate presented as exact.
    """
    usable = [r for r in rows if r.get(x_key) is not None and r.get(y_key) is not None]
    observed_pairs = [(r[x_key], r[y_key]) for r in usable]
    observed_correlation = spearman_correlation(observed_pairs)

    groups: dict[object, list[dict]] = {}
    for row in usable:
        groups.setdefault(row[group_key], []).append(row)
    group_keys = list(groups.keys())

    rng = random.Random(seed)
    resampled_correlations: list[float] = []
    for _ in range(n_resamples):
        chosen_groups = [rng.choice(group_keys) for _ in group_keys]
        resample_rows = [row for key in chosen_groups for row in groups[key]]
        pairs = [(row[x_key], row[y_key]) for row in resample_rows]
        correlation = spearman_correlation(pairs)
        if correlation is not None:
            resampled_correlations.append(correlation)

    resampled_correlations.sort()
    n = len(resampled_correlations)

    def percentile(p: float) -> float | None:
        if n == 0:
            return None
        index = min(n - 1, max(0, int(round(p * (n - 1)))))
        return resampled_correlations[index]

    mean_resampled = (sum(resampled_correlations) / n) if n else None
    crosses_zero = (
        percentile(0.025) is not None and percentile(0.975) is not None
        and percentile(0.025) <= 0 <= percentile(0.975)
    )

    return {
        "observed_correlation": observed_correlation,
        "n_groups": len(group_keys),
        "n_rows": len(usable),
        "n_resamples_used": n,
        "bootstrap_mean": mean_resampled,
        "ci_95_low": percentile(0.025),
        "ci_95_high": percentile(0.975),
        "ci_95_crosses_zero": crosses_zero,
    }
