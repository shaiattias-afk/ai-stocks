"""
tests/scoring/test_cohort_robustness_v1.py -- block bootstrap for an
honest confidence interval on a correlation measured from a clustered
sample (Council Proposal 1, 2026-08-11/12).
"""

from __future__ import annotations

from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation


def _row(ticker, x, y):
    return {"ticker": ticker, "x": x, "y": y}


def test_observed_correlation_matches_plain_spearman():
    rows = [_row("A", 1, 10), _row("B", 2, 20), _row("C", 3, 30)]
    result = block_bootstrap_correlation(rows, "x", "y", n_resamples=100, seed=1)
    assert result["observed_correlation"] == 1.0


def test_each_ticker_moves_as_one_block_not_split_across_resamples():
    """A ticker with 5 rows all pointing the SAME direction as a lone
    ticker's 1 row must not let the 5-row ticker dominate purely by
    row count within a single resample -- each GROUP either fully
    appears or doesn't, exactly once per group slot."""
    rows = [_row("BIG", i, i) for i in range(5)] + [_row("SOLO", 0, -100)]
    result = block_bootstrap_correlation(rows, "x", "y", n_resamples=200, seed=2)
    # 2 groups total -> each resample draws exactly 2 group-slots (with replacement)
    assert result["n_groups"] == 2
    assert result["n_rows"] == 6


def test_wide_ci_when_underlying_relationship_is_pure_noise():
    """A tiny, noisy sample's bootstrap CI should be wide and cross
    zero -- this is the honest, correct behavior, not a bug."""
    rows = [
        _row("A", 1, 5), _row("B", 2, -3), _row("C", 3, 8),
        _row("D", 4, -1), _row("E", 5, 2),
    ]
    result = block_bootstrap_correlation(rows, "x", "y", n_resamples=500, seed=3)
    assert result["ci_95_low"] < 0 < result["ci_95_high"]
    assert result["ci_95_crosses_zero"] is True


def test_strong_consistent_relationship_gives_a_narrow_ci_not_crossing_zero():
    rows = [_row(f"T{i}", i, i * 2 + (1 if i % 2 else -1)) for i in range(1, 21)]
    result = block_bootstrap_correlation(rows, "x", "y", n_resamples=500, seed=4)
    assert result["observed_correlation"] > 0.8
    assert result["ci_95_crosses_zero"] is False


def test_missing_values_are_excluded_not_treated_as_zero():
    rows = [_row("A", 1, 10), _row("B", None, 20), _row("C", 3, None), _row("D", 4, 40)]
    result = block_bootstrap_correlation(rows, "x", "y", n_resamples=50, seed=5)
    assert result["n_rows"] == 2
    assert result["n_groups"] == 2
