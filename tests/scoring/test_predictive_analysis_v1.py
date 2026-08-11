"""
tests/scoring/test_predictive_analysis_v1.py -- Spearman correlation and
bucket-analysis helpers for Scoring Model V1's predictive-power check.
"""

from __future__ import annotations

import pytest

from stock_agent.scoring.predictive_analysis_v1 import (
    decile_analysis,
    factor_correlations,
    spearman_correlation,
)


def test_perfect_positive_correlation():
    pairs = [(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)]
    assert spearman_correlation(pairs) == pytest.approx(1.0)


def test_perfect_negative_correlation():
    pairs = [(1, 50), (2, 40), (3, 30), (4, 20), (5, 10)]
    assert spearman_correlation(pairs) == pytest.approx(-1.0)


def test_no_correlation_with_ties_does_not_crash():
    pairs = [(1, 1), (1, 1), (1, 1)]
    assert spearman_correlation(pairs) is None  # zero variance in x


def test_too_few_pairs_returns_none():
    assert spearman_correlation([(1, 2), (3, 4)]) is None


def test_ties_handled_with_average_rank():
    pairs = [(1, 10), (1, 20), (2, 30)]
    # x has a tie (ranks 1.5, 1.5) and y is monotonic -- correlation should be positive, not crash
    result = spearman_correlation(pairs)
    assert result is not None
    assert result > 0


def _row(ticker, score, excess_return, factor_scores=None):
    return {
        "ticker": ticker, "composite_score": score, "excess_return": excess_return,
        "beats_by_5pct": excess_return >= 0.05,
        "factor_scores": factor_scores or {},
    }


def test_decile_analysis_splits_into_ordered_buckets():
    dataset = [_row(f"T{i}", score=i * 10, excess_return=i * 0.01) for i in range(1, 11)]
    buckets = decile_analysis(dataset, n_buckets=2)
    assert len(buckets) == 2
    assert buckets[0]["score_range"][0] < buckets[1]["score_range"][0]
    assert buckets[1]["mean_excess_return"] > buckets[0]["mean_excess_return"]


def test_factor_correlations_only_uses_available_factors():
    dataset = [
        _row("A", 90, 0.20, {"roic_level": 100}),
        _row("B", 50, 0.05, {"roic_level": 50}),
        _row("C", 10, -0.10, {"roic_level": 0}),
        _row("D", 70, 0.10),  # missing roic_level entirely
    ]
    result = factor_correlations(dataset)
    assert result["roic_level"]["n"] == 3
    assert result["roic_level"]["spearman_correlation"] == pytest.approx(1.0)
    assert result["composite_score"]["n"] == 4
