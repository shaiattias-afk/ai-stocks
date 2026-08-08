"""
tests/test_fail_closed.py -- general-purpose tests asserting that
ambiguous/incomplete evidence produces REVIEW_REQUIRED, never a guessed
value, across several policy modules (beyond D-017's own dedicated test
module, tests/policies/test_zero_inference.py).
"""

from __future__ import annotations

from stock_agent.extraction.core import TargetRowNotFound
from stock_agent.policies.debt_current_long_term import (
    resolve_current_debt_components_from_warehouse,
    resolve_debt_classification_by_ancestry_from_warehouse,
    resolve_long_term_debt,
)
from stock_agent.policies.short_term_investments import attempt_short_term_investments_zero_proof
from stock_agent.policies.tax_normalization import compute_normalized_tax_nopat

from tests.helpers import (
    BS_ROLE_DEF,
    BS_ROLE_URI,
    insert_calculation_edge,
    insert_fact,
    insert_usd_unit,
    make_warehouse_connection,
    pres_row,
    presentation_df,
)

ACCESSION = "ACC-FAIL-CLOSED"
REPORT_DATE = "2024-06-30"


# --- debt_current_long_term.py: ancestry classifier when the chain doesn't
# --- resolve ------------------------------------------------------------


def test_long_term_debt_ancestry_classifier_review_required_when_chain_unresolved():
    """A debt-vocabulary row whose presentation ancestry never reaches
    ANY identified liabilities section (current or general) must never
    be silently classified either way."""
    conn = make_warehouse_connection()
    presentation = presentation_df(
        [
            pres_row(
                "us-gaap:SeniorNotesPayable", "Senior notes payable",
                role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF,
                parent_qname="us-gaap:SomeOpaqueGroupingWithNoLiabilityWording",
            ),
            pres_row(
                "us-gaap:SomeOpaqueGroupingWithNoLiabilityWording", "Some opaque grouping",
                role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF, is_abstract=True,
            ),
        ]
    )
    result = resolve_long_term_debt(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["value"] is None

    # And directly at the ancestry-resolution layer: zero matches for
    # BOTH "current" and "noncurrent" -- the row is genuinely unresolved,
    # not silently defaulted to either.
    current_matches = resolve_debt_classification_by_ancestry_from_warehouse(presentation, "current", set())
    noncurrent_matches = resolve_debt_classification_by_ancestry_from_warehouse(presentation, "noncurrent", set())
    assert current_matches == []
    assert noncurrent_matches == []


def test_current_debt_components_review_required_when_siblings_have_no_shared_parent():
    presentation = presentation_df(
        [
            pres_row("us-gaap:CommercialPaper", "Commercial paper", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF,
                     parent_qname="us-gaap:ParentA"),
            pres_row("us-gaap:ShortTermBorrowings", "Short-term debt",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF, parent_qname="us-gaap:ParentB"),
        ]
    )
    conn = make_warehouse_connection()
    try:
        resolve_current_debt_components_from_warehouse(conn, ACCESSION, presentation)
        raised = False
    except TargetRowNotFound:
        raised = True
    assert raised


# --- tax_normalization.py: fails closed when a source fact is itself invalid
# (this module takes already-resolved values as input -- "invalid" here means
# the composing caller in metrics/annual.py never invokes normalization
# unless pretax_income/income_tax_expense/operating_income are all
# independently PASS; this test locks in that the normalization FORMULA
# itself, given a value a caller should never pass through in the first
# place -- None -- does not silently coerce it into a number).


def test_tax_normalization_raises_rather_than_silently_coercing_a_missing_value():
    try:
        compute_normalized_tax_nopat(pretax_value=None, tax_expense_value=10.0, operating_income_value=100.0)
        raised = False
    except TypeError:
        raised = True
    assert raised, (
        "compute_normalized_tax_nopat must never silently treat a missing "
        "pretax_income as a number -- callers (metrics/annual.py) are "
        "responsible for only invoking it once all 3 source facts are "
        "independently PASS; this locks in that the formula itself has no "
        "silent None-coercion path"
    )


# --- short_term_investments.py: zero-proof when the calculation-linkbase
# --- completeness check fails --------------------------------------------


def test_short_term_investments_zero_proof_review_required_when_linkbase_incomplete():
    """AssetsCurrent exists and is uniquely identified, but has NO
    calculation-linkbase children at all -- cannot prove completeness,
    so must remain REVIEW_REQUIRED, never assumed zero."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_fact(conn, ACCESSION, "us-gaap:AssetsCurrent", 1000.0, "C_TOTAL", instant_date=REPORT_DATE)
    presentation = presentation_df(
        [pres_row("us-gaap:AssetsCurrent", "Total current assets", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF)]
    )
    result = attempt_short_term_investments_zero_proof(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "REVIEW_REQUIRED"


def test_short_term_investments_zero_proof_review_required_when_a_child_fails_to_resolve():
    """A calculation child exists structurally but its own fact does not
    resolve to a single deterministic value -- must not silently drop it
    from the sum."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_calculation_edge(conn, ACCESSION, BS_ROLE_URI, "us-gaap:AssetsCurrent", "us-gaap:CashAndCashEquivalentsAtCarryingValue")
    insert_calculation_edge(conn, ACCESSION, BS_ROLE_URI, "us-gaap:AssetsCurrent", "us-gaap:AccountsReceivableNetCurrent")
    insert_fact(conn, ACCESSION, "us-gaap:CashAndCashEquivalentsAtCarryingValue", 600.0, "C1", instant_date=REPORT_DATE)
    # AccountsReceivableNetCurrent: two genuinely conflicting values -> never resolves
    insert_fact(conn, ACCESSION, "us-gaap:AccountsReceivableNetCurrent", 400.0, "C2", instant_date=REPORT_DATE)
    insert_fact(conn, ACCESSION, "us-gaap:AccountsReceivableNetCurrent", 999.0, "C3", instant_date=REPORT_DATE)
    insert_fact(conn, ACCESSION, "us-gaap:AssetsCurrent", 1000.0, "C_TOTAL", instant_date=REPORT_DATE)
    presentation = presentation_df(
        [
            pres_row("us-gaap:AssetsCurrent", "Total current assets", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
            pres_row("us-gaap:CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
            pres_row("us-gaap:AccountsReceivableNetCurrent", "Accounts receivable",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
        ]
    )
    result = attempt_short_term_investments_zero_proof(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "REVIEW_REQUIRED"
    assert "did not resolve to a single value" in result["error"]
