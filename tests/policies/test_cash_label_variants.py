"""
tests/policies/test_cash_label_variants.py -- D-023 (Micron: broadened
mention pattern to match bare "cash and equivalents") and D-025 (Palo
Alto Networks: excluded any role whose title contains "reconciliation").
"""

from __future__ import annotations

from stock_agent.extraction.core import BUILT_IN_METRICS, TargetRowNotFound, identify_canonical_row
from stock_agent.policies.cash_label_variants import CASH_AND_EQUIVALENTS_DEFINITION

from tests.helpers import BS_ROLE_DEF, BS_ROLE_URI, pres_row, presentation_df

RECONCILIATION_ROLE_URI = "http://test.invalid/role/CashReconciliation"
# Deliberately tagged as a "Statement" role (not "Disclosure") to
# reproduce the real trap: is_target_role requires role_definition to
# match the Statement-role prefix AND the balance-sheet include pattern
# AND not the exclude pattern -- the historical bug needed all of the
# first two to be true for this role to ever become a rival candidate at
# all, which is exactly why D-025's role_exclude_pattern fix was
# necessary rather than redundant with is_statement_role.
RECONCILIATION_ROLE_DEF = (
    "1500 - Statement - Reconciliation of Cash, Cash Equivalents, and "
    "Restricted Cash to the Consolidated Balance Sheets"
)


def test_re_exported_definition_is_the_same_object_as_built_in_metrics():
    assert CASH_AND_EQUIVALENTS_DEFINITION is BUILT_IN_METRICS["cash_and_equivalents"]


def test_doubled_phrase_cash_and_cash_equivalents_resolves():
    presentation = presentation_df(
        [pres_row("us-gaap:CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents",
                   role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF)]
    )
    row, _ = identify_canonical_row(presentation, BUILT_IN_METRICS["cash_and_equivalents"])
    assert row["concept_qname"] == "us-gaap:CashAndCashEquivalentsAtCarryingValue"


def test_d023_micron_shortened_cash_and_equivalents_label_resolves():
    """D-023: Micron's shortened label 'Cash and equivalents' (missing
    the doubled 'cash') is the SAME standard concept, same balance-sheet
    role, same structural position -- only the label wording differs."""
    presentation = presentation_df(
        [pres_row("us-gaap:CashAndCashEquivalentsAtCarryingValue", "Cash and equivalents",
                   role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF)]
    )
    row, _ = identify_canonical_row(presentation, BUILT_IN_METRICS["cash_and_equivalents"])
    assert row["concept_qname"] == "us-gaap:CashAndCashEquivalentsAtCarryingValue"


def test_restricted_cash_variant_still_correctly_excluded():
    """The broadened pattern must still be fully anchored -- a longer
    label like 'Restricted cash and cash equivalents' must NOT match."""
    presentation = presentation_df(
        [pres_row("us-gaap:RestrictedCashAndCashEquivalentsAtCarryingValue",
                   "Restricted cash and cash equivalents",
                   role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF)]
    )
    try:
        identify_canonical_row(presentation, BUILT_IN_METRICS["cash_and_equivalents"])
        raised = False
    except TargetRowNotFound:
        raised = True
    assert raised, "a 'Restricted cash...' label must never resolve as cash_and_equivalents"


def test_d025_reconciliation_role_excluded_even_though_it_mentions_balance_sheets():
    """D-025: the ASC 230 / ASU 2016-18 'Reconciliation of Cash...to the
    [Consolidated] Balance Sheet(s)' disclosure role's own title mentions
    'balance sheets', which previously caused it to be mistaken for the
    real balance sheet role. Only the TRUE balance-sheet row must be
    picked; the reconciliation-table candidate must never be selected."""
    presentation = presentation_df(
        [
            pres_row("us-gaap:CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
            pres_row("us-gaap:CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents",
                     role_uri=RECONCILIATION_ROLE_URI, role_definition=RECONCILIATION_ROLE_DEF),
        ]
    )
    row, candidates = identify_canonical_row(presentation, BUILT_IN_METRICS["cash_and_equivalents"])
    assert row["role_uri"] == BS_ROLE_URI
    # exactly one candidate survives the role_exclude_pattern filter
    assert (candidates["role_uri"] == RECONCILIATION_ROLE_URI).sum() == 0
