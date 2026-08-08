"""
tests/test_extraction_core.py -- unit tests for
stock_agent.extraction.core's general-purpose row-identification and
fact-matching primitives (identify_canonical_row, match_facts_from_
warehouse), using hand-built synthetic presentation DataFrames and an
in-memory DuckDB connection. No disk access, no network, milliseconds
per test.
"""

from __future__ import annotations

import pytest

from stock_agent.extraction.core import (
    BUILT_IN_METRICS,
    TargetRowNotFound,
    identify_canonical_row,
    match_facts_from_warehouse,
)

from tests.helpers import (
    BS_ROLE_DEF,
    BS_ROLE_URI,
    IS_ROLE_DEF,
    IS_ROLE_URI,
    insert_fact,
    insert_usd_unit,
    make_warehouse_connection,
    pres_row,
    presentation_df,
)


# --- identify_canonical_row --------------------------------------------------


def test_identify_canonical_row_finds_unique_revenue_row():
    presentation = presentation_df(
        [
            pres_row(
                "us-gaap:Revenues", "Total revenues",
                role_uri=IS_ROLE_URI, role_definition=IS_ROLE_DEF, period_type="duration",
            ),
            pres_row(
                "us-gaap:CostOfRevenue", "Cost of revenue",
                role_uri=IS_ROLE_URI, role_definition=IS_ROLE_DEF, period_type="duration",
            ),
        ]
    )
    row, candidates = identify_canonical_row(presentation, BUILT_IN_METRICS["revenue"])
    assert row["concept_qname"] == "us-gaap:Revenues"
    assert row["selection_tier"] == "plain"
    # "Cost of revenue" also mentions the metric's broad mention_pattern
    # (it is a base candidate) but does not match the anchored plain
    # pattern, so tier-B narrows to exactly the one canonical row.
    assert len(candidates) == 2


def test_identify_canonical_row_raises_target_row_not_found_when_nothing_matches():
    presentation = presentation_df(
        [
            pres_row(
                "us-gaap:CostOfRevenue", "Cost of revenue",
                role_uri=IS_ROLE_URI, role_definition=IS_ROLE_DEF, period_type="duration",
            ),
        ]
    )
    with pytest.raises(TargetRowNotFound):
        identify_canonical_row(presentation, BUILT_IN_METRICS["revenue"])


def test_identify_canonical_row_fails_closed_on_genuine_ambiguity():
    """Two distinct, equally plausible revenue-labeled rows in the same
    statement role, neither an 'attributable to shareholders' variant --
    D-008 fail-closed: never guess which one is canonical."""
    presentation = presentation_df(
        [
            pres_row(
                "us-gaap:Revenues", "Revenues",
                role_uri=IS_ROLE_URI, role_definition=IS_ROLE_DEF, period_type="duration",
            ),
            pres_row(
                "us-gaap:SalesRevenueNet", "Net sales",
                role_uri=IS_ROLE_URI, role_definition=IS_ROLE_DEF, period_type="duration",
            ),
        ]
    )
    with pytest.raises(TargetRowNotFound):
        identify_canonical_row(presentation, BUILT_IN_METRICS["revenue"])


# --- match_facts_from_warehouse: instant ------------------------------------


def test_match_facts_from_warehouse_instant_exact_date_passes():
    conn = make_warehouse_connection()
    insert_usd_unit(conn, "ACC1")
    insert_fact(
        conn, "ACC1", "us-gaap:CashAndCashEquivalentsAtCarryingValue", 1000.0, "C1",
        instant_date="2024-06-30", period_type="instant",
    )
    decision = match_facts_from_warehouse(
        conn, "ACC1", "us-gaap:CashAndCashEquivalentsAtCarryingValue", "2024-06-30", "instant",
    )
    assert decision["status"] == "PASS"
    assert decision["value"] == 1000.0


def test_match_facts_from_warehouse_instant_no_facts_review_required():
    conn = make_warehouse_connection()
    insert_usd_unit(conn, "ACC1")
    decision = match_facts_from_warehouse(conn, "ACC1", "us-gaap:NoSuchConcept", "2024-06-30", "instant")
    assert decision["status"] == "REVIEW_REQUIRED"
    assert decision["value"] is None


def test_match_facts_from_warehouse_instant_ambiguous_multiple_values_review_required():
    """Two facts for the same concept/accession at two different instant
    dates both within tolerance, with genuinely different values -- must
    fail closed, never pick one."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, "ACC1")
    insert_fact(conn, "ACC1", "us-gaap:CashAndCashEquivalentsAtCarryingValue", 1000.0, "C1",
                instant_date="2024-06-30")
    insert_fact(conn, "ACC1", "us-gaap:CashAndCashEquivalentsAtCarryingValue", 2000.0, "C2",
                instant_date="2024-06-30")
    decision = match_facts_from_warehouse(
        conn, "ACC1", "us-gaap:CashAndCashEquivalentsAtCarryingValue", "2024-06-30", "instant",
    )
    assert decision["status"] == "REVIEW_REQUIRED"
    assert decision["value"] is None


# --- match_facts_from_warehouse: duration -----------------------------------
# Regression coverage for a real porting bug found and fixed in this PR:
# see tests/test_historical_bugs.py::test_annual_duration_max_days_constant_
# was_dropped_during_port for the full writeup. These two tests exercise the
# duration branch directly (every production call site in this package only
# ever calls with expected_period_type="instant", so this branch was
# previously untested and unreachable in any real pipeline run).


def test_match_facts_from_warehouse_duration_annual_window_passes():
    conn = make_warehouse_connection()
    insert_usd_unit(conn, "ACC1")
    insert_fact(
        conn, "ACC1", "us-gaap:Revenues", 5000.0, "C1",
        period_start="2023-01-01", period_end="2023-12-31", period_type="duration",
    )
    decision = match_facts_from_warehouse(conn, "ACC1", "us-gaap:Revenues", "2023-12-31", "duration")
    assert decision["status"] == "PASS"
    assert decision["value"] == 5000.0


def test_match_facts_from_warehouse_duration_rejects_non_annual_duration():
    """A quarter-length duration fact sharing the same period_end as a
    would-be annual fact must not be picked up by the annual duration
    window (ANNUAL_DURATION_MIN_DAYS=350 .. ANNUAL_DURATION_MAX_DAYS=380)."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, "ACC1")
    insert_fact(
        conn, "ACC1", "us-gaap:Revenues", 1200.0, "C1",
        period_start="2023-10-01", period_end="2023-12-31", period_type="duration",
    )
    decision = match_facts_from_warehouse(conn, "ACC1", "us-gaap:Revenues", "2023-12-31", "duration")
    assert decision["status"] == "REVIEW_REQUIRED"
