"""
tests/scoring/test_valuation_wide_v1.py -- diluted EPS resolution
extended to the wide universe (D-046's original rule, reused).
"""

from __future__ import annotations

import duckdb
import pytest

from stock_agent.scoring.valuation_wide_v1 import resolve_diluted_eps

ACCESSION = "ACC-EPS"


@pytest.fixture
def conn():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE xbrl_units (accession_number VARCHAR, unit_id VARCHAR, "
        "numerator_measures VARCHAR, denominator_measures VARCHAR)"
    )
    con.execute(
        "CREATE TABLE xbrl_facts (accession_number VARCHAR, concept_qname VARCHAR, "
        "dimensions_json VARCHAR, unit_id VARCHAR, is_nil BOOLEAN, value_numeric DOUBLE, "
        "context_id VARCHAR, period_start VARCHAR, period_end VARCHAR)"
    )
    con.execute("INSERT INTO xbrl_units VALUES (?, 'usdPerShare', 'iso4217:USD', 'xbrli:shares')", [ACCESSION])
    return con


def _insert_eps_fact(con, value, period_start, period_end, context_id="C1", dims="{}"):
    con.execute(
        "INSERT INTO xbrl_facts VALUES (?, 'us-gaap:EarningsPerShareDiluted', ?, 'usdPerShare', FALSE, ?, ?, ?, ?)",
        [ACCESSION, dims, value, context_id, period_start, period_end],
    )


def test_resolves_single_annual_fact(conn):
    _insert_eps_fact(conn, 5.25, "2023-01-01", "2023-12-31")
    result = resolve_diluted_eps(conn, ACCESSION, "2023-12-31")
    assert result["status"] == "PASS"
    assert result["value"] == 5.25


def test_ignores_a_non_annual_duration_fact():
    """A quarterly EPS fact sharing the same period_end (e.g. Q4-only
    tagged with the fiscal year-end date) must not be picked up."""
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE xbrl_units (accession_number VARCHAR, unit_id VARCHAR, "
        "numerator_measures VARCHAR, denominator_measures VARCHAR)"
    )
    con.execute(
        "CREATE TABLE xbrl_facts (accession_number VARCHAR, concept_qname VARCHAR, "
        "dimensions_json VARCHAR, unit_id VARCHAR, is_nil BOOLEAN, value_numeric DOUBLE, "
        "context_id VARCHAR, period_start VARCHAR, period_end VARCHAR)"
    )
    con.execute("INSERT INTO xbrl_units VALUES (?, 'usdPerShare', 'iso4217:USD', 'xbrli:shares')", [ACCESSION])
    _insert_eps_fact(con, 1.50, "2023-10-01", "2023-12-31")  # ~3 months, not annual
    result = resolve_diluted_eps(con, ACCESSION, "2023-12-31")
    assert result["status"] == "UNAVAILABLE"


def test_dimensional_fact_ignored(conn):
    """Only the consolidated (non-dimensional) fact counts."""
    _insert_eps_fact(conn, 5.25, "2023-01-01", "2023-12-31", context_id="C1", dims="{}")
    _insert_eps_fact(conn, 9.99, "2023-01-01", "2023-12-31", context_id="C2", dims='{"segment": "A"}')
    result = resolve_diluted_eps(conn, ACCESSION, "2023-12-31")
    assert result["status"] == "PASS"
    assert result["value"] == 5.25


def test_multiple_differing_values_fails_closed(conn):
    _insert_eps_fact(conn, 5.25, "2023-01-01", "2023-12-31", context_id="C1")
    _insert_eps_fact(conn, 5.30, "2023-01-01", "2023-12-31", context_id="C2")
    result = resolve_diluted_eps(conn, ACCESSION, "2023-12-31")
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["value"] is None


def test_no_facts_at_all_is_unavailable_not_zero(conn):
    result = resolve_diluted_eps(conn, ACCESSION, "2023-12-31")
    assert result["status"] == "UNAVAILABLE"
    assert result["value"] is None


def test_no_usd_per_share_unit_in_accession_is_unavailable():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE xbrl_units (accession_number VARCHAR, unit_id VARCHAR, "
        "numerator_measures VARCHAR, denominator_measures VARCHAR)"
    )
    con.execute(
        "CREATE TABLE xbrl_facts (accession_number VARCHAR, concept_qname VARCHAR, "
        "dimensions_json VARCHAR, unit_id VARCHAR, is_nil BOOLEAN, value_numeric DOUBLE, "
        "context_id VARCHAR, period_start VARCHAR, period_end VARCHAR)"
    )
    result = resolve_diluted_eps(con, ACCESSION, "2023-12-31")
    assert result["status"] == "UNAVAILABLE"
