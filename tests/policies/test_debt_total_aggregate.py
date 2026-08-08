"""
tests/policies/test_debt_total_aggregate.py -- D-018's "aggregate-first"
total_debt resolution, extended by D-027 Policy C (a filing's own
reported "Total" row in its debt-maturity schedule is authoritative for
total_debt even when it does not reconcile exactly with the balance-
sheet long_term_debt carrying value).
"""

from __future__ import annotations

from stock_agent.policies.debt_total_aggregate import (
    D022_APPLIED_SCOPE,
    POLICY_C_APPLIED_SCOPE,
    resolve_total_debt_maturity_basis_d022,
    resolve_total_debt_with_aggregate_policy,
)

from tests.helpers import (
    BS_ROLE_DEF,
    BS_ROLE_URI,
    DEBT_MATURITY_ROLE_DEF,
    DEBT_MATURITY_ROLE_URI,
    insert_fact,
    insert_presentation_edge,
    insert_role,
    insert_usd_unit,
    make_warehouse_connection,
    pres_row,
    presentation_df,
)

ACCESSION = "ACC-TOTAL-DEBT"
REPORT_DATE = "2024-06-30"


def _maturity_schedule_fixture(reported_total_value):
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_role(conn, ACCESSION, DEBT_MATURITY_ROLE_URI, DEBT_MATURITY_ROLE_DEF, "presentation")
    insert_presentation_edge(conn, ACCESSION, DEBT_MATURITY_ROLE_URI,
                              "us-gaap:DebtMaturitiesAbstract", "test:DebtMaturitiesYearOne", 1.0)
    insert_presentation_edge(conn, ACCESSION, DEBT_MATURITY_ROLE_URI,
                              "us-gaap:DebtMaturitiesAbstract", "test:DebtMaturitiesYearTwo", 2.0)
    insert_presentation_edge(conn, ACCESSION, DEBT_MATURITY_ROLE_URI,
                              "us-gaap:DebtMaturitiesAbstract", "test:DebtMaturitiesTotal", 3.0)
    insert_fact(conn, ACCESSION, "test:DebtMaturitiesYearOne", 200.0, "C_Y1", instant_date=REPORT_DATE)
    insert_fact(conn, ACCESSION, "test:DebtMaturitiesYearTwo", 600.0, "C_Y2", instant_date=REPORT_DATE)
    insert_fact(conn, ACCESSION, "test:DebtMaturitiesTotal", reported_total_value, "C_TOT", instant_date=REPORT_DATE)
    presentation = presentation_df(
        [
            pres_row("test:DebtMaturitiesYearOne", "2025", role_uri=DEBT_MATURITY_ROLE_URI,
                     role_definition=DEBT_MATURITY_ROLE_DEF, parent_qname="us-gaap:DebtMaturitiesAbstract"),
            pres_row("test:DebtMaturitiesYearTwo", "2026", role_uri=DEBT_MATURITY_ROLE_URI,
                     role_definition=DEBT_MATURITY_ROLE_DEF, parent_qname="us-gaap:DebtMaturitiesAbstract"),
            pres_row("test:DebtMaturitiesTotal", "Total", role_uri=DEBT_MATURITY_ROLE_URI,
                     role_definition=DEBT_MATURITY_ROLE_DEF, parent_qname="us-gaap:DebtMaturitiesAbstract"),
        ]
    )
    return conn, presentation


# --- tier 1: both carrying values PASS --------------------------------------


def test_gaap_carrying_value_preferred_when_both_current_and_long_term_pass():
    conn, presentation = _maturity_schedule_fixture(reported_total_value=800.0)
    cd = {"status": "PASS", "value": 100.0}
    ltd = {"status": "PASS", "value": 700.0}
    result = resolve_total_debt_with_aggregate_policy(conn, ACCESSION, presentation, REPORT_DATE, cd, ltd)
    assert result["status"] == "PASS"
    assert result["value"] == 800.0
    assert result["basis"] == "GAAP_CARRYING_VALUE"


# --- D-027 Policy C: direct reported total, even with a reconciliation gap -


def test_policy_c_direct_aggregate_used_even_with_reconciliation_gap():
    """A filing's own reported 'Total' row is authoritative for
    total_debt even when it does not reconcile exactly with the balance-
    sheet long_term_debt carrying value (face value vs. carrying value
    net of unamortized discount/issuance costs) -- the gap is preserved
    in lineage, never silently discarded, and never blocks total_debt."""
    conn, presentation = _maturity_schedule_fixture(reported_total_value=810.0)  # != 200+600 bucket sum coincidentally but IS the reported total
    cd = {"status": "REVIEW_REQUIRED", "value": None}
    ltd = {"status": "REVIEW_REQUIRED", "value": None}
    result = resolve_total_debt_with_aggregate_policy(conn, ACCESSION, presentation, REPORT_DATE, cd, ltd)
    assert result["status"] == "PASS_DIRECT_AGGREGATE"
    assert result["value"] == 810.0
    assert result["basis"] == "DIRECT_AGGREGATE_REPORTED_TOTAL"


def test_policy_c_falls_back_to_maturity_bucket_sum_when_no_reported_total():
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_role(conn, ACCESSION, DEBT_MATURITY_ROLE_URI, DEBT_MATURITY_ROLE_DEF, "presentation")
    insert_presentation_edge(conn, ACCESSION, DEBT_MATURITY_ROLE_URI,
                              "us-gaap:DebtMaturitiesAbstract", "test:DebtMaturitiesYearOne", 1.0)
    insert_presentation_edge(conn, ACCESSION, DEBT_MATURITY_ROLE_URI,
                              "us-gaap:DebtMaturitiesAbstract", "test:DebtMaturitiesYearTwo", 2.0)
    insert_fact(conn, ACCESSION, "test:DebtMaturitiesYearOne", 200.0, "C_Y1", instant_date=REPORT_DATE)
    insert_fact(conn, ACCESSION, "test:DebtMaturitiesYearTwo", 600.0, "C_Y2", instant_date=REPORT_DATE)
    presentation = presentation_df(
        [
            pres_row("test:DebtMaturitiesYearOne", "2025", role_uri=DEBT_MATURITY_ROLE_URI,
                     role_definition=DEBT_MATURITY_ROLE_DEF, parent_qname="us-gaap:DebtMaturitiesAbstract"),
            pres_row("test:DebtMaturitiesYearTwo", "2026", role_uri=DEBT_MATURITY_ROLE_URI,
                     role_definition=DEBT_MATURITY_ROLE_DEF, parent_qname="us-gaap:DebtMaturitiesAbstract"),
        ]
    )
    cd = {"status": "REVIEW_REQUIRED", "value": None}
    ltd = {"status": "REVIEW_REQUIRED", "value": None}
    result = resolve_total_debt_with_aggregate_policy(conn, ACCESSION, presentation, REPORT_DATE, cd, ltd)
    assert result["status"] == "PASS_MATURITY_BASIS"
    assert result["value"] == 800.0
    assert result["basis"] == "MATURITY_PRINCIPAL"


def test_review_required_when_nothing_resolves():
    conn = make_warehouse_connection()
    presentation = presentation_df([])
    cd = {"status": "REVIEW_REQUIRED", "value": None}
    ltd = {"status": "REVIEW_REQUIRED", "value": None}
    result = resolve_total_debt_with_aggregate_policy(conn, ACCESSION, presentation, REPORT_DATE, cd, ltd)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["value"] is None


# --- D-022's narrower, original 2-tier precedence (no direct-aggregate tier)


def test_d022_precedence_never_prefers_a_reported_total_row():
    """scripts/79's D-022 resolver predates Policy C -- it must go
    straight from GAAP carrying value to the bucket SUM, never
    preferring the schedule's own reported Total row directly, even when
    one exists."""
    conn, presentation = _maturity_schedule_fixture(reported_total_value=999.0)  # would differ from the bucket sum if used
    cd = {"status": "REVIEW_REQUIRED", "value": None}
    ltd = {"status": "REVIEW_REQUIRED", "value": None}
    result = resolve_total_debt_maturity_basis_d022(conn, ACCESSION, presentation, REPORT_DATE, cd, ltd)
    assert result["status"] == "PASS_MATURITY_BASIS"
    assert result["value"] == 800.0  # bucket sum (200 + 600), never the reported 999 total
    assert result["basis"] == "MATURITY_PRINCIPAL"


def test_scope_sets_are_disjoint_and_match_documented_filing_counts():
    """The two scope sets (which resolver production actually used for
    which accession) must never overlap, and must carry exactly the
    filing counts documented in the module's own docstring."""
    assert POLICY_C_APPLIED_SCOPE.isdisjoint(D022_APPLIED_SCOPE)
    assert len(POLICY_C_APPLIED_SCOPE) == 12
    assert len(D022_APPLIED_SCOPE) == 10
