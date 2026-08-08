"""
tests/policies/test_short_term_investments.py -- D-030: proves
short_term_investments=0 via calculation-linkbase completeness of
AssetsCurrent ("Total current assets") -- if AssetsCurrent's calculation
children are all resolvable and sum to the reported total, and none of
them carries short-term-investment vocabulary, short_term_investments is
proven zero by structural absence.
"""

from __future__ import annotations

from stock_agent.policies.short_term_investments import (
    attempt_short_term_investments_zero_proof,
    find_assets_current_row,
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

ACCESSION = "ACC-STI"
REPORT_DATE = "2024-06-30"


def _fixture(cash_value, receivables_value, total_value, include_short_term_investment_child=False):
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    children = ["us-gaap:CashAndCashEquivalentsAtCarryingValue", "us-gaap:AccountsReceivableNetCurrent"]
    if include_short_term_investment_child:
        children.append("us-gaap:MarketableSecuritiesCurrent")
    for child in children:
        insert_calculation_edge(conn, ACCESSION, BS_ROLE_URI, "us-gaap:AssetsCurrent", child)

    insert_fact(conn, ACCESSION, "us-gaap:CashAndCashEquivalentsAtCarryingValue", cash_value, "C1", instant_date=REPORT_DATE)
    insert_fact(conn, ACCESSION, "us-gaap:AccountsReceivableNetCurrent", receivables_value, "C2", instant_date=REPORT_DATE)
    if include_short_term_investment_child:
        insert_fact(conn, ACCESSION, "us-gaap:MarketableSecuritiesCurrent", 50.0, "C3", instant_date=REPORT_DATE)
    insert_fact(conn, ACCESSION, "us-gaap:AssetsCurrent", total_value, "C_TOTAL", instant_date=REPORT_DATE)

    rows = [
        pres_row("us-gaap:AssetsCurrent", "Total current assets", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
        pres_row("us-gaap:CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents",
                 role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
        pres_row("us-gaap:AccountsReceivableNetCurrent", "Accounts receivable",
                 role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
    ]
    if include_short_term_investment_child:
        rows.append(
            pres_row("us-gaap:MarketableSecuritiesCurrent", "Marketable securities",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF)
        )
    presentation = presentation_df(rows)
    return conn, presentation


def test_finds_unique_assets_current_row():
    _conn, presentation = _fixture(600.0, 400.0, 1000.0)
    result = find_assets_current_row(presentation, REPORT_DATE)
    assert result is not None
    assert result["concept_qname"] == "us-gaap:AssetsCurrent"


def test_proves_zero_when_calculation_children_are_complete_and_reconcile():
    conn, presentation = _fixture(600.0, 400.0, 1000.0)
    result = attempt_short_term_investments_zero_proof(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "PASS"
    assert result["value"] == 0.0
    assert result["basis"] == "ZERO_PROVEN_STRUCTURAL_ABSENCE"


def test_review_required_when_a_child_carries_short_term_investment_vocabulary():
    """A short-term-investment-vocabulary child that the standard row-
    identification tier failed to resolve is a genuine ambiguity, not a
    zero case -- must never be silently treated as absent."""
    conn, presentation = _fixture(600.0, 350.0, 1000.0, include_short_term_investment_child=True)
    result = attempt_short_term_investments_zero_proof(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "REVIEW_REQUIRED"
    assert "short-term-investment" in result["error"]


def test_review_required_when_children_do_not_reconcile_with_reported_total():
    conn, presentation = _fixture(600.0, 400.0, 1500.0)  # total doesn't match 600+400
    result = attempt_short_term_investments_zero_proof(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "REVIEW_REQUIRED"
    assert "does not reconcile" in result["error"]


def test_review_required_when_no_calculation_children_exist():
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_fact(conn, ACCESSION, "us-gaap:AssetsCurrent", 1000.0, "C_TOTAL", instant_date=REPORT_DATE)
    presentation = presentation_df(
        [pres_row("us-gaap:AssetsCurrent", "Total current assets", role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF)]
    )
    result = attempt_short_term_investments_zero_proof(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "REVIEW_REQUIRED"
    assert "no calculation-linkbase children" in result["error"]


def test_review_required_when_no_unique_assets_current_row():
    conn = make_warehouse_connection()
    presentation = presentation_df([])
    result = attempt_short_term_investments_zero_proof(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "REVIEW_REQUIRED"
