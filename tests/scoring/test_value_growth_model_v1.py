"""
tests/scoring/test_value_growth_model_v1.py -- the D-063-motivated
two-bucket valuation/growth model: profitable companies ranked by
cheap P/E, unprofitable companies ranked by fast revenue growth.
"""

from __future__ import annotations

import pytest

from stock_agent.scoring.value_growth_model_v1 import (
    PROFITABLE,
    UNPROFITABLE,
    classify_bucket,
    compute_value_growth_scores,
)


def _row(ticker, fiscal_year, diluted_eps=None, raw_pe=None, revenue_growth=None):
    return {"ticker": ticker, "report_date": f"{fiscal_year}-12-31", "fiscal_year": fiscal_year,
            "diluted_eps": diluted_eps, "raw_pe": raw_pe, "revenue_growth": revenue_growth}


def test_classify_bucket():
    assert classify_bucket(5.0) == PROFITABLE
    assert classify_bucket(0.0) == UNPROFITABLE
    assert classify_bucket(-2.0) == UNPROFITABLE
    assert classify_bucket(None) == UNPROFITABLE


def test_profitable_bucket_ranks_cheaper_pe_higher():
    rows = [
        _row("A", 2023, diluted_eps=5.0, raw_pe=10.0),   # cheap
        _row("B", 2023, diluted_eps=5.0, raw_pe=50.0),   # expensive
        _row("C", 2023, diluted_eps=5.0, raw_pe=30.0),
    ]
    results = {r["ticker"]: r for r in compute_value_growth_scores(rows)}
    assert results["A"]["bucket"] == PROFITABLE
    assert results["A"]["composite_score"] == 100.0  # cheapest -> best score
    assert results["B"]["composite_score"] == 0.0    # most expensive -> worst score
    assert results["A"]["signal_used"] == "raw_pe"


def test_unprofitable_bucket_ranks_faster_growth_higher():
    rows = [
        _row("A", 2023, diluted_eps=-1.0, revenue_growth=0.80),  # fastest growth
        _row("B", 2023, diluted_eps=-1.0, revenue_growth=0.10),
        _row("C", 2023, diluted_eps=-1.0, revenue_growth=0.40),
    ]
    results = {r["ticker"]: r for r in compute_value_growth_scores(rows)}
    assert results["A"]["bucket"] == UNPROFITABLE
    assert results["A"]["composite_score"] == 100.0
    assert results["B"]["composite_score"] == 0.0
    assert results["A"]["signal_used"] == "revenue_growth"


def test_buckets_ranked_independently_within_the_same_fiscal_year():
    """A profitable company's score must never depend on unprofitable
    peers' P/E (they have none) or vice versa."""
    rows = [
        _row("A", 2023, diluted_eps=5.0, raw_pe=10.0),
        _row("B", 2023, diluted_eps=5.0, raw_pe=50.0),
        _row("C", 2023, diluted_eps=-1.0, revenue_growth=0.80),
        _row("D", 2023, diluted_eps=-1.0, revenue_growth=0.10),
    ]
    results = {r["ticker"]: r for r in compute_value_growth_scores(rows)}
    assert results["A"]["composite_score"] == 100.0
    assert results["C"]["composite_score"] == 100.0  # both "best in their own bucket"


def test_missing_signal_gives_no_score_not_fabricated():
    rows = [
        _row("A", 2023, diluted_eps=-1.0, revenue_growth=None),  # unprofitable, no growth data either
        _row("B", 2023, diluted_eps=-1.0, revenue_growth=0.10),
    ]
    results = {r["ticker"]: r for r in compute_value_growth_scores(rows)}
    assert results["A"]["composite_score"] is None
    assert results["A"]["signal_used"] is None


def test_single_company_in_a_bucket_cannot_be_ranked():
    rows = [_row("A", 2023, diluted_eps=5.0, raw_pe=10.0)]
    results = compute_value_growth_scores(rows)
    assert results[0]["composite_score"] is None
