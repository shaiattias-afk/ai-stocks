"""
tests/policies/test_debt_undrawn_revolver.py -- D-027 Policy B: an
explicit, resolved, zero-valued revolving-credit-facility "amount
outstanding" fact proves zero debt for that facility at the exact
report date.
"""

from __future__ import annotations

from stock_agent.policies.debt_undrawn_revolver import find_undrawn_revolver_evidence

from tests.helpers import insert_fact, make_warehouse_connection

ACCESSION = "ACC-REVOLVER"
REPORT_DATE = "2024-06-30"

REVOLVER_DIMS = '{"us-gaap:CreditFacilityAxis": "us-gaap:RevolvingCreditFacilityMember"}'


def test_undrawn_revolver_at_exact_report_date_is_evidence_of_zero():
    conn = make_warehouse_connection()
    insert_fact(
        conn, ACCESSION, "us-gaap:LineOfCredit", 0.0, "C1",
        instant_date=REPORT_DATE, dims=REVOLVER_DIMS,
    )
    evidence = find_undrawn_revolver_evidence(conn, ACCESSION, REPORT_DATE)
    assert evidence is not None
    assert evidence["value"] == 0.0
    assert evidence["concept_qname"] == "us-gaap:LineOfCredit"


def test_drawn_revolver_is_never_evidence_of_zero():
    conn = make_warehouse_connection()
    insert_fact(
        conn, ACCESSION, "us-gaap:LineOfCredit", 5_000_000.0, "C1",
        instant_date=REPORT_DATE, dims=REVOLVER_DIMS,
    )
    evidence = find_undrawn_revolver_evidence(conn, ACCESSION, REPORT_DATE)
    assert evidence is None


def test_undrawn_revolver_at_a_different_date_is_not_evidence():
    """Policy requires the fact to be dated EXACTLY the report date --
    a zero balance at some other instant date proves nothing about the
    exact balance-sheet date."""
    conn = make_warehouse_connection()
    insert_fact(
        conn, ACCESSION, "us-gaap:LineOfCredit", 0.0, "C1",
        instant_date="2023-06-30", dims=REVOLVER_DIMS,
    )
    evidence = find_undrawn_revolver_evidence(conn, ACCESSION, REPORT_DATE)
    assert evidence is None


def test_no_revolver_dimensioned_fact_at_all_is_none():
    conn = make_warehouse_connection()
    evidence = find_undrawn_revolver_evidence(conn, ACCESSION, REPORT_DATE)
    assert evidence is None


def test_credit_limit_alone_is_never_consulted_as_evidence():
    """Policy requirement: the facility's credit limit/available capacity
    is never treated as evidence of an outstanding balance either way --
    only the facility's own outstanding-balance concept (LineOfCredit)
    counts. A different concept (e.g. LineOfCreditFacilityMaximumBorrowingCapacity)
    must never be picked up, even if it happens to carry the revolver
    dimension."""
    conn = make_warehouse_connection()
    insert_fact(
        conn, ACCESSION, "us-gaap:LineOfCreditFacilityMaximumBorrowingCapacity", 500_000_000.0, "C1",
        instant_date=REPORT_DATE, dims=REVOLVER_DIMS,
    )
    evidence = find_undrawn_revolver_evidence(conn, ACCESSION, REPORT_DATE)
    assert evidence is None


def test_ambiguous_distinct_values_at_report_date_is_not_evidence():
    conn = make_warehouse_connection()
    insert_fact(conn, ACCESSION, "us-gaap:LineOfCredit", 0.0, "C1", instant_date=REPORT_DATE, dims=REVOLVER_DIMS)
    insert_fact(conn, ACCESSION, "us-gaap:LineOfCredit", 100.0, "C2", instant_date=REPORT_DATE, dims=REVOLVER_DIMS)
    evidence = find_undrawn_revolver_evidence(conn, ACCESSION, REPORT_DATE)
    assert evidence is None
