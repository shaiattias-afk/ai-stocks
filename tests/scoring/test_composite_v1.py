"""
tests/scoring/test_composite_v1.py -- Scoring Model V1's percentile-rank
composite (docs/SCORING_MODEL_V1_BLUEPRINT.md Stage 3).
"""

from __future__ import annotations

import pytest

from stock_agent.scoring.composite_v1 import FACTOR_WEIGHTS, compute_composite_scores_v1


def _row(ticker, fiscal_year, **factors):
    row = {"ticker": ticker, "report_date": f"{fiscal_year}-12-31", "fiscal_year": fiscal_year}
    row.update(factors)
    return row


def test_weights_sum_to_100_percent():
    assert abs(sum(w for w, _ in FACTOR_WEIGHTS.values()) - 1.0) < 1e-9


def test_three_companies_simple_ranking_no_ties():
    rows = [
        _row("A", 2023, revenue_growth=0.30),
        _row("B", 2023, revenue_growth=0.10),
        _row("C", 2023, revenue_growth=0.20),
    ]
    results = {r["ticker"]: r for r in compute_composite_scores_v1(rows)}
    # revenue_growth is NOT inverted -- higher raw value -> higher percentile.
    assert results["A"]["factor_scores"]["revenue_growth"] == 100.0
    assert results["C"]["factor_scores"]["revenue_growth"] == 50.0
    assert results["B"]["factor_scores"]["revenue_growth"] == 0.0


def test_inverted_factor_lower_raw_value_scores_higher():
    rows = [
        _row("A", 2023, balance_sheet_strength_ratio=0.5),   # weakest (highest ratio)
        _row("B", 2023, balance_sheet_strength_ratio=-0.5),  # strongest (lowest ratio)
        _row("C", 2023, balance_sheet_strength_ratio=0.0),
    ]
    results = {r["ticker"]: r for r in compute_composite_scores_v1(rows)}
    assert results["B"]["factor_scores"]["balance_sheet_strength_ratio"] == 100.0
    assert results["A"]["factor_scores"]["balance_sheet_strength_ratio"] == 0.0


def test_ties_get_averaged_rank():
    rows = [
        _row("A", 2023, roic_level=0.10),
        _row("B", 2023, roic_level=0.10),
        _row("C", 2023, roic_level=0.30),
    ]
    results = {r["ticker"]: r for r in compute_composite_scores_v1(rows)}
    # A and B are tied for the two lowest ranks (0 and 1 of 0..2) -> averaged to 0.5,
    # percentile = 0.5 / (3-1) * 100 = 25.0 for both.
    assert results["A"]["factor_scores"]["roic_level"] == 25.0
    assert results["B"]["factor_scores"]["roic_level"] == 25.0
    assert results["C"]["factor_scores"]["roic_level"] == 100.0


def test_single_company_in_a_year_cannot_be_ranked():
    rows = [_row("A", 2023, revenue_growth=0.30)]
    results = compute_composite_scores_v1(rows)
    assert results[0]["composite_score"] is None
    assert results[0]["weight_covered"] == 0.0
    assert results[0]["factor_scores"] == {}


def test_missing_factor_excluded_and_weight_renormalized():
    """A company missing one factor (e.g. its first fiscal year, no
    revenue_growth) must not be scored 0 for that factor -- the
    composite is the weighted average of only the AVAILABLE factors."""
    rows = [
        _row("A", 2023, revenue_growth=0.30, roic_level=0.20),
        _row("B", 2023, roic_level=0.10),  # missing revenue_growth entirely
    ]
    results = {r["ticker"]: r for r in compute_composite_scores_v1(rows)}
    assert "revenue_growth" not in results["B"]["factor_scores"]
    assert results["B"]["factors_used"] == 1
    expected_weight = FACTOR_WEIGHTS["roic_level"][0]
    assert results["B"]["weight_covered"] == pytest.approx(expected_weight)
    # B is the only one ranked for roic_level among {A, B} -> A has 0.20 (higher), B 0.10 (lower)
    assert results["B"]["factor_scores"]["roic_level"] == 0.0
    assert results["B"]["composite_score"] == pytest.approx(0.0)


def test_years_are_never_mixed_in_one_ranking():
    """A company's percentile must be computed only against the SAME
    fiscal year's peers, never across years."""
    rows = [
        _row("A", 2022, revenue_growth=0.50),  # alone in 2022 -> unrankable
        _row("B", 2023, revenue_growth=0.10),
        _row("C", 2023, revenue_growth=0.05),
    ]
    results = {(r["ticker"], r["fiscal_year"]): r for r in compute_composite_scores_v1(rows)}
    assert results[("A", 2022)]["composite_score"] is None
    assert results[("B", 2023)]["factor_scores"]["revenue_growth"] == 100.0
    assert results[("C", 2023)]["factor_scores"]["revenue_growth"] == 0.0


def test_full_composite_matches_manual_weighted_average():
    rows = [
        _row("A", 2023, revenue_growth=0.30, roic_level=0.10),
        _row("B", 2023, revenue_growth=0.10, roic_level=0.20),
    ]
    results = {r["ticker"]: r for r in compute_composite_scores_v1(rows)}
    # A: revenue_growth rank 100 (higher), roic_level rank 0 (lower)
    w_rev, _ = FACTOR_WEIGHTS["revenue_growth"]
    w_roic, _ = FACTOR_WEIGHTS["roic_level"]
    expected_a = (100 * w_rev + 0 * w_roic) / (w_rev + w_roic)
    assert results["A"]["composite_score"] == pytest.approx(expected_a)
