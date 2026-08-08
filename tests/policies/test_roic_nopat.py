"""
tests/policies/test_roic_nopat.py -- D-015's ROIC combination (scripts/95):
once both nopat and average_invested_capital independently resolve to a
successful status, roic = nopat / average_invested_capital, with a
documented status-precedence order.
"""

from __future__ import annotations

from stock_agent.policies.roic_nopat import combine_average_invested_capital_and_nopat_into_roic


def test_roic_computed_when_both_inputs_pass():
    result = combine_average_invested_capital_and_nopat_into_roic(
        avg_ic_status="PASS", avg_ic_value=1000.0, nopat_status="PASS", nopat_value=150.0,
    )
    assert result["status"] == "PASS"
    assert result["value"] == 0.15


def test_review_required_when_average_invested_capital_not_ready():
    result = combine_average_invested_capital_and_nopat_into_roic(
        avg_ic_status="REVIEW_REQUIRED", avg_ic_value=None, nopat_status="PASS", nopat_value=150.0,
    )
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["value"] is None


def test_review_required_when_nopat_not_ready():
    result = combine_average_invested_capital_and_nopat_into_roic(
        avg_ic_status="PASS", avg_ic_value=1000.0, nopat_status="REVIEW_REQUIRED", nopat_value=None,
    )
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["value"] is None


def test_review_required_when_average_invested_capital_is_zero():
    """Zero invested capital would divide by zero -- must fail closed,
    never raise or silently produce inf/nan."""
    result = combine_average_invested_capital_and_nopat_into_roic(
        avg_ic_status="PASS", avg_ic_value=0.0, nopat_status="PASS", nopat_value=150.0,
    )
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["value"] is None


def test_status_precedence_direct_aggregate_wins():
    result = combine_average_invested_capital_and_nopat_into_roic(
        avg_ic_status="PASS_DIRECT_AGGREGATE", avg_ic_value=1000.0, nopat_status="PASS", nopat_value=100.0,
    )
    assert result["status"] == "PASS_DIRECT_AGGREGATE"


def test_status_precedence_normalized_tax_wins_over_plain_pass():
    result = combine_average_invested_capital_and_nopat_into_roic(
        avg_ic_status="PASS", avg_ic_value=1000.0, nopat_status="PASS_NORMALIZED_TAX", nopat_value=100.0,
    )
    assert result["status"] == "PASS_NORMALIZED_TAX"


def test_status_precedence_maturity_basis_wins_over_plain_pass():
    result = combine_average_invested_capital_and_nopat_into_roic(
        avg_ic_status="PASS_MATURITY_BASIS", avg_ic_value=1000.0, nopat_status="PASS", nopat_value=100.0,
    )
    assert result["status"] == "PASS_MATURITY_BASIS"


def test_negative_nopat_produces_negative_roic():
    result = combine_average_invested_capital_and_nopat_into_roic(
        avg_ic_status="PASS", avg_ic_value=1000.0, nopat_status="PASS_NORMALIZED_TAX", nopat_value=-100.0,
    )
    assert result["status"] == "PASS_NORMALIZED_TAX"
    assert result["value"] == -0.1
