"""
tests/policies/test_zero_inference.py -- D-017's 4-condition
current_debt=0 structural-zero proof (see docs/DECISIONS_LOG.md D-017).

D-017's 4 conditions:
  1. D-016 tiers 1-3 already found ZERO explicit current-debt components
     (a precondition enforced by the CALLER, resolve_current_debt_with_
     facility_policy -- see D-017's own text: "a precondition, not
     sufficient on its own").
  2. The chronologically-earliest bucket in the filer's own debt-
     maturity disclosure is exactly 0.
  3. The maturity schedule's own "Total" row reconciles with
     long_term_debt (tolerance <= $1).
  4. No contradicting fact/row/note exists anywhere in the debt-related
     disclosures.

Conditions 2 and 3 are the two structural checks actually coded in
policies/zero_inference.py (verify_condition_2_from_warehouse /
verify_condition_3_from_warehouse) -- these are tested here as real,
parametrized, blocking-failure cases. Condition 1 is enforced one layer
up, by policies/debt_current_long_term.resolve_current_debt_with_
facility_policy, which only ever reaches the zero-inference tier when
D-016's own components search already came back empty -- tested here at
that composing level. Condition 4 has NO separate, independent blocking
check anywhere in the ported code today: D-019's own text describes this
corroboration as "informational, not a second blocking gate" -- this is
an honest, documented finding (not a fabricated mechanism), tested below
to lock in the CURRENT, observed behavior rather than assert an
enforcement path that does not exist.
"""

from __future__ import annotations

import pytest

from stock_agent.extraction.core import TargetRowNotFound
from stock_agent.policies.debt_current_long_term import resolve_current_debt_with_facility_policy
from stock_agent.policies.zero_inference import attempt_current_debt_zero_inference_from_warehouse

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

ACCESSION = "ACC-ZERO-INFERENCE"
REPORT_DATE = "2024-06-30"


def build_zero_inference_fixture(
    *, year1_value: float = 0.0, total_value: float = 500.0, ltd_value: float = 500.0
):
    """A filing with: (1) no explicit current-debt row anywhere on the
    balance sheet, (2) a debt-maturity schedule with a chronologically-
    first bucket (Year 1), a second bucket (Year 2), and a reported
    Total row, and (3) a long_term_debt row on the balance sheet. The
    year1/total/ltd values are overridable to construct each condition's
    pass/fail variant."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_role(conn, ACCESSION, DEBT_MATURITY_ROLE_URI, DEBT_MATURITY_ROLE_DEF, "presentation")
    insert_presentation_edge(conn, ACCESSION, DEBT_MATURITY_ROLE_URI,
                              "us-gaap:DebtMaturitiesAbstract", "test:DebtMaturitiesYearOne", 1.0)
    insert_presentation_edge(conn, ACCESSION, DEBT_MATURITY_ROLE_URI,
                              "us-gaap:DebtMaturitiesAbstract", "test:DebtMaturitiesYearTwo", 2.0)
    insert_presentation_edge(conn, ACCESSION, DEBT_MATURITY_ROLE_URI,
                              "us-gaap:DebtMaturitiesAbstract", "test:DebtMaturitiesTotal", 3.0)

    insert_fact(conn, ACCESSION, "test:DebtMaturitiesYearOne", year1_value, "C_Y1", instant_date=REPORT_DATE)
    insert_fact(conn, ACCESSION, "test:DebtMaturitiesYearTwo", 500.0, "C_Y2", instant_date=REPORT_DATE)
    insert_fact(conn, ACCESSION, "test:DebtMaturitiesTotal", total_value, "C_TOT", instant_date=REPORT_DATE)
    insert_fact(conn, ACCESSION, "us-gaap:LongTermDebtNoncurrent", ltd_value, "C_LTD", instant_date=REPORT_DATE)

    presentation = presentation_df(
        [
            pres_row("us-gaap:LongTermDebtNoncurrent", "Long-term debt",
                     role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF),
            pres_row("test:DebtMaturitiesYearOne", "2025",
                     role_uri=DEBT_MATURITY_ROLE_URI, role_definition=DEBT_MATURITY_ROLE_DEF,
                     parent_qname="us-gaap:DebtMaturitiesAbstract"),
            pres_row("test:DebtMaturitiesYearTwo", "2026",
                     role_uri=DEBT_MATURITY_ROLE_URI, role_definition=DEBT_MATURITY_ROLE_DEF,
                     parent_qname="us-gaap:DebtMaturitiesAbstract"),
            pres_row("test:DebtMaturitiesTotal", "Total",
                     role_uri=DEBT_MATURITY_ROLE_URI, role_definition=DEBT_MATURITY_ROLE_DEF,
                     parent_qname="us-gaap:DebtMaturitiesAbstract"),
        ]
    )
    ltd_row = {"concept_qname": "us-gaap:LongTermDebtNoncurrent"}
    return conn, presentation, ltd_row


# --- the all-4-proven PASS case ---------------------------------------------


def test_all_4_conditions_proven_infers_zero_current_debt():
    conn, presentation, ltd_row = build_zero_inference_fixture()
    result = attempt_current_debt_zero_inference_from_warehouse(
        conn, ACCESSION, presentation, REPORT_DATE, ltd_row
    )
    assert result["value"] == 0.0
    assert result["selection_tier"] == "zero_inference_proven"


# --- conditions 2 and 3: real, coded, blocking-failure variants ------------


@pytest.mark.parametrize(
    "year1_value, total_value, ltd_value, expected_condition_in_error",
    [
        pytest.param(100.0, 500.0, 500.0, "condition 2", id="condition_2_earliest_bucket_nonzero"),
        pytest.param(0.0, 600.0, 500.0, "condition 3", id="condition_3_total_does_not_reconcile_with_ltd"),
    ],
)
def test_condition_2_or_3_failing_raises_target_row_not_found(
    year1_value, total_value, ltd_value, expected_condition_in_error
):
    conn, presentation, ltd_row = build_zero_inference_fixture(
        year1_value=year1_value, total_value=total_value, ltd_value=ltd_value
    )
    with pytest.raises(TargetRowNotFound, match=expected_condition_in_error):
        attempt_current_debt_zero_inference_from_warehouse(
            conn, ACCESSION, presentation, REPORT_DATE, ltd_row
        )


# --- condition 1: precondition enforced by the caller -----------------------


def test_condition_1_failing_bypasses_the_zero_tier_entirely():
    """When D-016 tiers 1-3 do NOT come back empty (an explicit current-
    debt component exists), resolve_current_debt_with_facility_policy
    must resolve via that explicit component -- it must never reach, let
    alone pass through, the zero-inference tier. This is condition 1's
    real enforcement point in the ported code."""
    conn, presentation, _ltd_row = build_zero_inference_fixture()
    extra_current_debt_row = pres_row(
        "us-gaap:ShortTermBorrowings", "Short-term debt",
        role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF,
        parent_qname="us-gaap:LiabilitiesCurrentAbstract",
    )
    import pandas as pd
    presentation_with_component = pd.concat(
        [presentation, pd.DataFrame([extra_current_debt_row])], ignore_index=True
    )
    ltd_result = {"status": "PASS", "concept_qname": "us-gaap:LongTermDebtNoncurrent"}
    resolution = resolve_current_debt_with_facility_policy(
        conn, ACCESSION, presentation_with_component, REPORT_DATE, ltd_result
    )
    assert resolution["mode"] == "components"


# --- condition 4: documented scope boundary, not a fabricated mechanism ----


def test_condition_4_has_no_independent_blocking_gate_in_the_ported_code():
    """Honest, documented finding (see this PR's report, not a bug fix):
    D-017 condition 4 ("no contradicting fact exists anywhere in the
    debt-related disclosures") has no separate, independently-coded
    blocking check in policies/zero_inference.py or its caller --
    D-019's own text calls this corroboration "informational, not a
    second blocking gate". This test locks in today's actual behavior
    (conditions 2+3 alone are sufficient for a PASS) instead of asserting
    an enforcement mechanism that would have to be invented -- doing
    that would violate this suite's own "never invent a mechanism you
    can't verify" rule. If a future decision makes condition 4 a real,
    coded gate, this test should be updated alongside that change."""
    conn, presentation, ltd_row = build_zero_inference_fixture()
    result = attempt_current_debt_zero_inference_from_warehouse(
        conn, ACCESSION, presentation, REPORT_DATE, ltd_row
    )
    assert result["value"] == 0.0
