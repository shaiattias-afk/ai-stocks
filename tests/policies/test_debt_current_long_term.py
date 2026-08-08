"""
tests/policies/test_debt_current_long_term.py -- D-016 (current_debt as
sum of explicit components), D-019 (presentation-ancestry classification
for unlabeled current/non-current rows), and D-022/D-027-Policy-A/D-028
(debt-maturity-schedule classification) helpers.
"""

from __future__ import annotations

import pandas as pd
import pytest

from stock_agent.extraction.core import TargetRowNotFound
from stock_agent.policies.debt_current_long_term import (
    classify_current_or_noncurrent_by_ancestry,
    find_current_debt_calculation_components_from_warehouse,
    find_current_debt_explicit_total,
    find_current_debt_sibling_components,
    resolve_current_debt_components_from_warehouse,
    resolve_debt_classification_by_ancestry_from_warehouse,
    resolve_long_term_debt,
)

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

ACCESSION = "ACC-DEBT-CLT"
REPORT_DATE = "2024-06-30"


# --- Tier 1: explicit "Total current debt" row ------------------------------


def test_explicit_total_current_debt_row_found_when_unique():
    presentation = presentation_df(
        [pres_row("us-gaap:ShortTermBorrowingsTotal", "Total current debt",
                   role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF)]
    )
    result = find_current_debt_explicit_total(presentation)
    assert result is not None
    assert result["concept_qname"] == "us-gaap:ShortTermBorrowingsTotal"
    assert result["selection_tier"] == "explicit_total"


def test_explicit_total_current_debt_row_none_when_absent():
    presentation = presentation_df(
        [pres_row("us-gaap:CommercialPaper", "Commercial paper",
                   role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF)]
    )
    assert find_current_debt_explicit_total(presentation) is None


# --- Tier 2: calculation-linkbase-verified components -----------------------


def test_calculation_verified_components_found():
    conn = make_warehouse_connection()
    insert_calculation_edge(conn, ACCESSION, BS_ROLE_URI, "us-gaap:LiabilitiesCurrent", "us-gaap:CommercialPaper")
    insert_calculation_edge(conn, ACCESSION, BS_ROLE_URI, "us-gaap:LiabilitiesCurrent", "us-gaap:LongTermDebtCurrent")
    presentation = presentation_df(
        [
            pres_row("us-gaap:CommercialPaper", "Commercial paper", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
            pres_row("us-gaap:LongTermDebtCurrent", "Current portion of long-term debt",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
        ]
    )
    components = find_current_debt_calculation_components_from_warehouse(conn, ACCESSION, presentation)
    assert components is not None
    assert {c["concept_qname"] for c in components} == {"us-gaap:CommercialPaper", "us-gaap:LongTermDebtCurrent"}
    assert all(c["selection_tier"] == "calculation_verified" for c in components)


def test_calculation_components_rejected_when_a_child_is_never_allowed():
    """A calculation child whose label carries an operating-liability
    vocabulary word (accounts payable/accrued/lease) must never be
    treated as a debt component, even if the calculation linkbase groups
    it with real debt rows."""
    conn = make_warehouse_connection()
    insert_calculation_edge(conn, ACCESSION, BS_ROLE_URI, "us-gaap:LiabilitiesCurrent", "us-gaap:CommercialPaper")
    insert_calculation_edge(conn, ACCESSION, BS_ROLE_URI, "us-gaap:LiabilitiesCurrent", "us-gaap:AccountsPayableCurrent")
    presentation = presentation_df(
        [
            pres_row("us-gaap:CommercialPaper", "Commercial paper", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
            pres_row("us-gaap:AccountsPayableCurrent", "Accounts payable", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
        ]
    )
    components = find_current_debt_calculation_components_from_warehouse(conn, ACCESSION, presentation)
    assert components is None


# --- Tier 3: presentation siblings sharing one parent ------------------------


def test_sibling_components_require_a_shared_parent():
    presentation = presentation_df(
        [
            pres_row("us-gaap:CommercialPaper", "Commercial paper", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF,
                     parent_qname="us-gaap:DebtCurrentAbstract"),
            pres_row("us-gaap:LongTermDebtCurrent", "Current portion of long-term debt",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF, parent_qname="us-gaap:DebtCurrentAbstract"),
        ]
    )
    components = find_current_debt_sibling_components(presentation)
    assert components is not None and components != "AMBIGUOUS"
    assert len(components) == 2


def test_sibling_components_ambiguous_without_a_shared_parent():
    """Two current-debt-vocabulary candidates with DIFFERENT parents
    cannot structurally prove non-overlap -- must fail closed, never
    silently sum them."""
    presentation = presentation_df(
        [
            pres_row("us-gaap:CommercialPaper", "Commercial paper", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF,
                     parent_qname="us-gaap:ParentA"),
            pres_row("us-gaap:LongTermDebtCurrent", "Current portion of long-term debt",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF, parent_qname="us-gaap:ParentB"),
        ]
    )
    result = find_current_debt_sibling_components(presentation)
    assert result == "AMBIGUOUS"
    with pytest.raises(TargetRowNotFound):
        resolve_current_debt_components_from_warehouse(make_warehouse_connection(), ACCESSION, presentation)


# --- D-019: ancestry classification for unlabeled current/non-current rows -


def test_ancestry_classifies_current_via_liabilities_current_ancestor():
    chain = [{"concept_qname": "us-gaap:LiabilitiesCurrentAbstract", "label": "Current liabilities"}]
    classification, reason = classify_current_or_noncurrent_by_ancestry(chain)
    assert classification == "current"


def test_ancestry_classifies_noncurrent_when_only_general_liabilities_reached():
    """Palo Alto Networks' documented pattern (D-019): a filer that does
    not nest non-current liabilities under any matching 'non-current'
    abstract -- reaching only the general Liabilities section without
    ever passing through a current-liabilities grouping means
    NON-CURRENT."""
    chain = [{"concept_qname": "us-gaap:LiabilitiesAbstract", "label": "Liabilities"}]
    classification, reason = classify_current_or_noncurrent_by_ancestry(chain)
    assert classification == "noncurrent"


def test_ancestry_unresolved_when_chain_never_reaches_a_liabilities_section():
    chain = [{"concept_qname": "us-gaap:SomeUnrelatedGrouping", "label": "Some unrelated grouping"}]
    classification, reason = classify_current_or_noncurrent_by_ancestry(chain)
    assert classification is None


def test_resolve_debt_classification_by_ancestry_finds_unlabeled_convertible_notes():
    """Reproduces the Palo Alto Networks finding (D-019): a debt
    instrument labeled only 'Convertible senior notes, net' (no current/
    non-current wording) is classified purely from its presentation
    ancestry."""
    presentation = presentation_df(
        [
            pres_row("us-gaap:LiabilitiesCurrentAbstract", "Total current liabilities", is_abstract=True,
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
            pres_row("us-gaap:ConvertibleNotesPayableCurrent", "Convertible senior notes, net",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF, parent_qname="us-gaap:LiabilitiesCurrentAbstract"),
        ]
    )
    matches = resolve_debt_classification_by_ancestry_from_warehouse(presentation, "current", set())
    assert len(matches) == 1
    assert matches[0]["concept_qname"] == "us-gaap:ConvertibleNotesPayableCurrent"


# --- resolve_long_term_debt: structural-absence zero proof ------------------


def test_long_term_debt_proven_zero_when_no_noncurrent_debt_candidate_exists():
    conn = make_warehouse_connection()
    presentation = presentation_df([])  # no debt-vocabulary rows anywhere
    result = resolve_long_term_debt(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "PASS"
    assert result["value"] == 0.0
    assert result["basis"] == "ZERO_PROVEN_STRUCTURAL_ABSENCE"


def test_long_term_debt_review_required_when_an_unclaimed_candidate_remains():
    """An unclassifiable debt-vocabulary row (ancestry chain does not
    resolve) must block the zero proof -- never silently ignored."""
    conn = make_warehouse_connection()
    presentation = presentation_df(
        [
            pres_row("us-gaap:SeniorNotes", "Senior notes",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF, parent_qname="us-gaap:SomeUnresolvedGrouping"),
        ]
    )
    result = resolve_long_term_debt(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["value"] is None


def test_long_term_debt_resolves_via_direct_label_when_present():
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_fact(conn, ACCESSION, "us-gaap:LongTermDebtNoncurrent", 750.0, "C1", instant_date=REPORT_DATE)
    presentation = presentation_df(
        [pres_row("us-gaap:LongTermDebtNoncurrent", "Long-term debt", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF)]
    )
    result = resolve_long_term_debt(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "PASS"
    assert result["value"] == 750.0
