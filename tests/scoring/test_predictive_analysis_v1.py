"""
tests/scoring/test_predictive_analysis_v1.py -- Spearman correlation and
bucket-analysis helpers for Scoring Model V1's predictive-power check.
"""

from __future__ import annotations

import duckdb
import pytest

from stock_agent.scoring.predictive_analysis_v1 import (
    build_predictive_dataset,
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


def _predictive_conn():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE scoring_composite_v1 (ticker VARCHAR, report_date DATE, fiscal_year INTEGER, "
        "composite_score DOUBLE, weight_covered DOUBLE, factors_used INTEGER, factor_scores_json VARCHAR)"
    )
    con.execute("CREATE TABLE sec_filings (ticker VARCHAR, report_date DATE, filing_date DATE)")
    con.execute("CREATE TABLE historical_prices_daily (ticker VARCHAR, price_date DATE, adj_close DOUBLE)")
    return con


def test_annualized_return_equals_raw_return_at_12_months():
    con = _predictive_conn()
    con.execute("INSERT INTO scoring_composite_v1 VALUES ('A','2023-12-31',2023,80.0,1.0,9,'{}')")
    con.execute("INSERT INTO sec_filings VALUES ('A','2023-12-31','2024-02-01')")
    con.execute("INSERT INTO historical_prices_daily VALUES ('A','2024-02-01',100.0)")
    con.execute("INSERT INTO historical_prices_daily VALUES ('A','2025-02-01',120.0)")
    con.execute("INSERT INTO historical_prices_daily VALUES ('QQQ','2024-02-01',100.0)")
    con.execute("INSERT INTO historical_prices_daily VALUES ('QQQ','2025-02-01',110.0)")

    dataset = build_predictive_dataset(con, horizon_months=12)
    row = dataset[0]
    assert row["annualized_stock_return"] == pytest.approx(row["stock_return"])
    assert row["annualized_excess_return"] == pytest.approx(row["excess_return"])


def test_annualized_return_over_5_years_is_cagr_not_total_return():
    con = _predictive_conn()
    con.execute("INSERT INTO scoring_composite_v1 VALUES ('A','2020-12-31',2020,80.0,1.0,9,'{}')")
    con.execute("INSERT INTO sec_filings VALUES ('A','2020-12-31','2021-02-01')")
    # stock doubles over 5 years (total return 100%); QQQ up 50% over the same window
    con.execute("INSERT INTO historical_prices_daily VALUES ('A','2021-02-01',100.0)")
    con.execute("INSERT INTO historical_prices_daily VALUES ('A','2026-02-01',200.0)")
    con.execute("INSERT INTO historical_prices_daily VALUES ('QQQ','2021-02-01',100.0)")
    con.execute("INSERT INTO historical_prices_daily VALUES ('QQQ','2026-02-01',150.0)")

    dataset = build_predictive_dataset(con, horizon_months=60)
    row = dataset[0]
    assert row["stock_return"] == pytest.approx(1.0)  # total return, unchanged meaning
    # CAGR of doubling over 5 years = 2^(1/5) - 1 ~= 14.87%, NOT 100%/5 = 20%
    assert row["annualized_stock_return"] == pytest.approx(2 ** 0.2 - 1)
    assert row["annualized_qqq_return"] == pytest.approx(1.5 ** 0.2 - 1)
    assert row["annualized_excess_return"] == pytest.approx((2 ** 0.2 - 1) - (1.5 ** 0.2 - 1))
