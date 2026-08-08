"""
tests/policies/test_tax_normalization.py -- D-027 Policy D: normalized
21% tax rate for NOPAT when pretax_income<=0, or the reported effective
tax rate falls outside [0, 1], but pretax_income/income_tax_expense/
operating_income are all independently valid.
"""

from __future__ import annotations

from stock_agent.policies.tax_normalization import NORMALIZED_TAX_RATE, compute_normalized_tax_nopat


def test_no_normalization_needed_for_a_normal_positive_rate():
    result = compute_normalized_tax_nopat(pretax_value=1000.0, tax_expense_value=210.0, operating_income_value=1100.0)
    assert result is None


def test_normalizes_when_pretax_income_is_negative():
    result = compute_normalized_tax_nopat(pretax_value=-500.0, tax_expense_value=10.0, operating_income_value=-400.0)
    assert result is not None
    assert result["status"] == "PASS_NORMALIZED_TAX"
    assert result["effective_tax_rate"] == NORMALIZED_TAX_RATE
    assert result["nopat"] == -400.0 * (1 - NORMALIZED_TAX_RATE)
    assert result["basis"] == "FIXED_NORMALIZED_TAX_RATE_21_PERCENT"


def test_negative_operating_income_allowed_to_produce_negative_nopat():
    """Explicitly allowed by D-027 Policy D: negative operating_income
    is never blocked on that basis alone."""
    result = compute_normalized_tax_nopat(pretax_value=-100.0, tax_expense_value=5.0, operating_income_value=-900.0)
    assert result is not None
    assert result["nopat"] < 0


def test_normalizes_when_reported_rate_is_negative():
    result = compute_normalized_tax_nopat(pretax_value=100.0, tax_expense_value=-20.0, operating_income_value=150.0)
    assert result is not None
    assert result["reported_rate"] == -0.2
    assert result["status"] == "PASS_NORMALIZED_TAX"


def test_normalizes_when_reported_rate_exceeds_100_percent():
    result = compute_normalized_tax_nopat(pretax_value=100.0, tax_expense_value=250.0, operating_income_value=150.0)
    assert result is not None
    assert result["reported_rate"] == 2.5
    assert result["status"] == "PASS_NORMALIZED_TAX"


def test_zero_pretax_income_never_divides_by_zero_and_still_normalizes():
    """pretax_value == 0 must both trigger normalization (<=0) and avoid
    a ZeroDivisionError when computing reported_rate."""
    result = compute_normalized_tax_nopat(pretax_value=0.0, tax_expense_value=0.0, operating_income_value=200.0)
    assert result is not None
    assert result["reported_rate"] is None
    assert result["status"] == "PASS_NORMALIZED_TAX"


def test_boundary_rate_exactly_one_does_not_normalize():
    """Reported rate exactly 1.0 (100%) is within the closed interval
    [0, 1] -- must NOT trigger normalization."""
    result = compute_normalized_tax_nopat(pretax_value=100.0, tax_expense_value=100.0, operating_income_value=150.0)
    assert result is None


def test_boundary_rate_exactly_zero_does_not_normalize():
    result = compute_normalized_tax_nopat(pretax_value=100.0, tax_expense_value=0.0, operating_income_value=150.0)
    assert result is None
