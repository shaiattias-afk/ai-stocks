"""
tests/scoring/test_model_v2_candidate.py -- train-only factor selection
for the Scoring Model V2 candidate (docs/DECISIONS_LOG.md D-061's
recommended next step, done with the out-of-sample discipline D-061
demanded: never select or weight anything using test-period data).
"""

from __future__ import annotations

import pytest

from stock_agent.scoring.model_v2_candidate import (
    CANDIDATE_FACTOR_DIRECTIONS,
    select_and_weight_factors_from_train,
)


def _row(ticker, fiscal_year, excess_return, **factors):
    row = {"ticker": ticker, "report_date": f"{fiscal_year}-12-31", "fiscal_year": fiscal_year,
           "excess_return": excess_return}
    for factor in CANDIDATE_FACTOR_DIRECTIONS:
        row.setdefault(factor, None)
    row.update(factors)
    return row


def test_no_factor_selected_when_nothing_clears_the_bar():
    # random/no relationship
    train = [
        _row("A", 2021, 0.10, roic_level=0.05),
        _row("B", 2021, -0.10, roic_level=0.05),
        _row("C", 2021, 0.05, roic_level=0.05),
    ]
    result = select_and_weight_factors_from_train(train)
    # all identical roic_level -> unrankable (no variance) -> nothing selected
    assert result == {}


def test_selects_a_factor_with_genuine_positive_correlation_in_its_own_direction():
    # roic_level (invert=False, higher is better): higher roic_level should track higher excess_return
    train = [
        _row("A", 2021, 0.30, roic_level=0.30),
        _row("B", 2021, 0.10, roic_level=0.10),
        _row("C", 2021, -0.10, roic_level=-0.10),
        _row("D", 2022, 0.30, roic_level=0.30),
        _row("E", 2022, 0.10, roic_level=0.10),
        _row("F", 2022, -0.10, roic_level=-0.10),
    ]
    result = select_and_weight_factors_from_train(train, min_abs_correlation=0.3)
    assert "roic_level" in result
    weight, invert = result["roic_level"]
    assert invert is False
    assert weight == pytest.approx(1.0)  # only factor selected -> full weight


def test_inverted_relationship_in_own_direction_is_excluded_not_flipped():
    """A factor whose pre-committed direction anti-correlates with
    excess_return on train (D-061's balance_sheet_strength_ratio finding)
    must be dropped, never have its sign silently flipped to fit."""
    # balance_sheet_strength_ratio is invert=True (lower ratio = better);
    # here HIGHER raw ratio tracks HIGHER excess_return -- the opposite
    # of what invert=True would reward -- so its own-direction correlation is negative.
    train = [
        _row("A", 2021, 0.30, balance_sheet_strength_ratio=0.30),
        _row("B", 2021, 0.10, balance_sheet_strength_ratio=0.10),
        _row("C", 2021, -0.10, balance_sheet_strength_ratio=-0.10),
    ]
    result = select_and_weight_factors_from_train(train, min_abs_correlation=0.1)
    assert "balance_sheet_strength_ratio" not in result


def test_weights_are_proportional_to_correlation_strength_and_sum_to_one():
    train = []
    for fy_offset in range(3):
        fy = 2021 + fy_offset
        train.append(_row("A", fy, 0.30, roic_level=0.30, revenue_growth=0.30))
        train.append(_row("B", fy, 0.10, roic_level=0.10, revenue_growth=0.05))
        train.append(_row("C", fy, -0.10, roic_level=-0.10, revenue_growth=-0.20))
    result = select_and_weight_factors_from_train(train, min_abs_correlation=0.1)
    assert set(result) <= {"roic_level", "revenue_growth"}
    total_weight = sum(w for w, _inv in result.values())
    assert total_weight == pytest.approx(1.0)
