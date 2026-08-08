"""
tests/policies/test_prior_fiscal_year_lookup.py -- D-027 Policy A item 7:
average_invested_capital may reuse the PREVIOUS fiscal year's own
separately-locked filing's invested_capital directly.
"""

from __future__ import annotations

from stock_agent.policies.prior_fiscal_year_lookup import combine_current_and_prior_invested_capital


def test_averages_when_both_years_pass():
    result = combine_current_and_prior_invested_capital(
        current_ic_status="PASS", current_ic_value=1000.0,
        prior_ic_status="PASS", prior_ic_value=800.0,
    )
    assert result["status"] == "PASS"
    assert result["value"] == 900.0


def test_review_required_when_current_year_invested_capital_not_resolved():
    result = combine_current_and_prior_invested_capital(
        current_ic_status="REVIEW_REQUIRED", current_ic_value=None,
        prior_ic_status="PASS", prior_ic_value=800.0,
    )
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["value"] is None


def test_review_required_when_prior_year_not_ready():
    result = combine_current_and_prior_invested_capital(
        current_ic_status="PASS", current_ic_value=1000.0,
        prior_ic_status="REVIEW_REQUIRED", prior_ic_value=None,
    )
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["value"] is None


def test_status_precedence_direct_aggregate_from_either_year_wins():
    result = combine_current_and_prior_invested_capital(
        current_ic_status="PASS", current_ic_value=1000.0,
        prior_ic_status="PASS_DIRECT_AGGREGATE", prior_ic_value=800.0,
    )
    assert result["status"] == "PASS_DIRECT_AGGREGATE"


def test_status_precedence_maturity_basis_from_either_year_wins_over_plain_pass():
    result = combine_current_and_prior_invested_capital(
        current_ic_status="PASS_MATURITY_BASIS", current_ic_value=1000.0,
        prior_ic_status="PASS", prior_ic_value=800.0,
    )
    assert result["status"] == "PASS_MATURITY_BASIS"
