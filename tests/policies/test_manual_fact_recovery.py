"""
tests/policies/test_manual_fact_recovery.py -- D-032/D-033: exactly TWO
narrowly-scoped, one-time manual recoveries of a single Inline XBRL fact
each (Arelle's own ixTransformValueError). This is explicitly NOT a
general mechanism -- re-verified here to stay scoped to exactly its two
documented accessions and never generalize to any other input.
"""

from __future__ import annotations

from stock_agent.policies.manual_fact_recovery import (
    CRWD_FY2021_LINE_OF_CREDIT,
    MANUAL_FACT_RECOVERY_BY_ACCESSION,
    NVDA_FY2020_LINE_OF_CREDIT,
    recovered_current_debt_zero,
)

CRWD_ACCESSION = "0001535527-21-000007"
NVDA_ACCESSION = "0001045810-20-000010"


def test_exactly_two_entries_in_the_override_table():
    assert len(MANUAL_FACT_RECOVERY_BY_ACCESSION) == 2
    assert set(MANUAL_FACT_RECOVERY_BY_ACCESSION.keys()) == {CRWD_ACCESSION, NVDA_ACCESSION}


def test_crwd_recovery_returns_documented_values():
    result = recovered_current_debt_zero(CRWD_ACCESSION)
    assert result["status"] == "PASS"
    assert result["value"] == 0.0
    assert result["basis"] == "SEC_HTML_MANUAL_PARSE"
    assert result["selection_tier"] == "zero_explicit_undrawn_facility_manual_html_parse"


def test_nvda_recovery_returns_documented_values():
    result = recovered_current_debt_zero(NVDA_ACCESSION)
    assert result["status"] == "PASS"
    assert result["value"] == 0.0
    assert result["basis"] == "SEC_HTML_MANUAL_PARSE"


def test_recovery_is_none_for_every_other_accession():
    for candidate in ("0001535527-22-000006", "0001045810-19-000023", "", "NOT-AN-ACCESSION"):
        assert recovered_current_debt_zero(candidate) is None


def test_recovered_lineage_preserves_the_original_arelle_error():
    """The original Arelle decode failure must always be preserved in
    lineage, never silently dropped once the value is recovered."""
    for fixture in (CRWD_FY2021_LINE_OF_CREDIT, NVDA_FY2020_LINE_OF_CREDIT):
        assert fixture["arelle_value_raw"] == "(ixTransformValueError)"
        assert fixture["arelle_value_numeric"] is None
        assert fixture["final_parsed_value"] == 0.0
