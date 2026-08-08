"""
tests/test_historical_bugs.py -- permanent regression tests for 5 named
historical bugs found and fixed earlier in this project's history (see
docs/CURRENT_STATE.md and docs/DECISIONS_LOG.md for the original
incidents). Each test reproduces a minimal synthetic fixture matching
the SPECIFIC documented scenario (the exact label text, the exact
decimals/duplicate-fact shape, the exact accession) and asserts the
CURRENT, ported code in src/stock_agent still handles it correctly.

Sources consulted (cited per test):
  1. XBRL instant-date off-by-one -- docs/CURRENT_STATE.md lines ~688-701
     (the "midnight of the following day" Arelle instantDatetime finding,
     fixed in scripts/46_xbrl_metric_engine.py).
  2. Decimals-precision duplicate reconciliation -- docs/CURRENT_STATE.md
     lines ~880-903 (MSFT CommercialPaper, $6,693,000,000 @ decimals=-6
     vs $6,700,000,000 @ decimals=-8) and docs/DECISIONS_LOG.md D-016's
     "Consequence discovered" paragraph.
  3. ixTransformValueError handling -- docs/DECISIONS_LOG.md D-031/D-032
     (CRWD FY2021 us-gaap:LineOfCredit, accession 0001535527-21-000007).
  4. Meta's "Income (loss) from operations" label variant --
     docs/CURRENT_STATE.md lines ~549-577.
  5. Micron's bare "Current debt" label -- docs/DECISIONS_LOG.md D-019's
     "related, general label-vocabulary gap" paragraph.

All 5 were found with clear, specific documentation in the project's own
history -- none had to be guessed or invented.
"""

from __future__ import annotations

from stock_agent.extraction.core import (
    BUILT_IN_METRICS,
    TargetRowNotFound,
    _reconcile_same_context_precision_duplicates_from_warehouse,
    identify_canonical_row,
    match_facts_from_warehouse,
)
from stock_agent.policies.debt_current_long_term import find_current_debt_sibling_components
from stock_agent.policies.manual_fact_recovery import (
    MANUAL_FACT_RECOVERY_BY_ACCESSION,
    recovered_current_debt_zero,
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


# =============================================================================
# 1. XBRL instant-date off-by-one bug
# =============================================================================
# docs/CURRENT_STATE.md: "Arelle returns a balance sheet dated 2024-06-30
# with instantDatetime = 2024-07-01T00:00:00 -- the exact same 'midnight
# of the following day' convention already handled for duration end
# dates, just not applied to instants. Every real fact was off by one
# day and silently rejected [0 candidates]." Fixed in
# scripts/46_xbrl_metric_engine.py by applying the same -1 day
# adjustment to instant dates that duration end dates already received.
#
# The -1-day adjustment itself is applied once, upstream, when the
# warehouse's own instant_date column is populated from Arelle's raw
# instantDatetime (scripts/121's extraction layer -- out of scope for
# this PR's port, per warehouse/loader.py's own docstring). What IS in
# scope and ported here is match_facts_from_warehouse's own exact-date
# matching contract: given a CORRECTLY adjusted instant_date, it must
# match; given a date that is still off by even one day (i.e. the
# adjustment was never applied, reproducing the original bug), it must
# fail closed to REVIEW_REQUIRED (0 candidates) rather than silently
# accept the wrong day -- this is exactly the "every real fact was off
# by one day and silently rejected" symptom, now correctly reproduced
# as a clean, explicit REVIEW_REQUIRED rather than a wrong value.


def test_instant_date_correctly_adjusted_fact_matches_report_date():
    conn = make_warehouse_connection()
    insert_usd_unit(conn, "ACC1")
    # The warehouse's own instant_date is stored already -1-day-adjusted
    # from Arelle's raw "midnight of the following day" instantDatetime
    # -- i.e. it already reads 2024-06-30, matching the balance sheet's
    # own report_date exactly.
    insert_fact(conn, "ACC1", "us-gaap:Assets", 100.0, "C1", instant_date="2024-06-30")
    decision = match_facts_from_warehouse(conn, "ACC1", "us-gaap:Assets", "2024-06-30", "instant")
    assert decision["status"] == "PASS"
    assert decision["value"] == 100.0


def test_instant_date_off_by_one_day_is_rejected_not_silently_matched():
    """Reproduces the pre-fix symptom directly: a fact recorded one day
    after the true report_date (the raw, un-adjusted Arelle convention)
    must NOT be silently treated as a match -- it must fail closed."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, "ACC1")
    insert_fact(conn, "ACC1", "us-gaap:Assets", 100.0, "C1", instant_date="2024-07-01")
    decision = match_facts_from_warehouse(conn, "ACC1", "us-gaap:Assets", "2024-06-30", "instant")
    assert decision["status"] == "REVIEW_REQUIRED"
    assert decision["value"] is None


# =============================================================================
# 2. Decimals-precision duplicate reconciliation bug (D-016's "Consequence
#    discovered", also referenced by D-035's rounding-tolerance policy area)
# =============================================================================
# docs/CURRENT_STATE.md: Microsoft's CommercialPaper balance was tagged
# TWICE in the same context -- once precisely in the statement table
# (decimals=-6, $6,693,000,000) and once rounded for narrative prose
# (decimals=-8, $6,700,000,000, exactly what $6,693,000,000 rounds to at
# that precision). The old dedup only collapsed IDENTICAL values, so
# this was flagged as a false 2-value conflict. Fixed by reconciling
# same-context facts that are consistent under standard XBRL rounding
# (each fact's own `decimals` attribute) BEFORE deciding ambiguity -- a
# genuine conflict still fails closed exactly as before.


def test_msft_commercial_paper_decimals_duplicate_reconciles_to_most_precise_value():
    conn = make_warehouse_connection()
    insert_usd_unit(conn, "ACC-MSFT")
    # Same context (same fact, tagged twice): precise statement-table
    # value at decimals=-6, and the narrative-prose rounding of the same
    # balance at decimals=-8.
    insert_fact(conn, "ACC-MSFT", "us-gaap:CommercialPaper", 6_693_000_000.0, "C1",
                instant_date="2024-06-30", decimals="-6")
    insert_fact(conn, "ACC-MSFT", "us-gaap:CommercialPaper", 6_700_000_000.0, "C1",
                instant_date="2024-06-30", decimals="-8")
    decision = match_facts_from_warehouse(conn, "ACC-MSFT", "us-gaap:CommercialPaper", "2024-06-30", "instant")
    assert decision["status"] == "PASS"
    # The reported value is never altered/averaged -- the MORE PRECISE of
    # the two consistent facts is selected as-is.
    assert decision["value"] == 6_693_000_000.0


def test_precision_reconciliation_still_fails_closed_on_a_genuine_conflict():
    """Two same-context facts whose values are NOT consistent under
    either one's own rounding precision must remain a genuine ambiguity
    -- the reconciliation fix must never paper over a real conflict."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, "ACC-MSFT")
    insert_fact(conn, "ACC-MSFT", "us-gaap:CommercialPaper", 6_693_000_000.0, "C1",
                instant_date="2024-06-30", decimals="-6")
    insert_fact(conn, "ACC-MSFT", "us-gaap:CommercialPaper", 9_100_000_000.0, "C1",
                instant_date="2024-06-30", decimals="-8")
    decision = match_facts_from_warehouse(conn, "ACC-MSFT", "us-gaap:CommercialPaper", "2024-06-30", "instant")
    assert decision["status"] == "REVIEW_REQUIRED"


def test_reconcile_same_context_precision_duplicates_directly():
    """Direct unit test of the exact function this policy area lives in,
    isolated from date/unit filtering."""
    import pandas as pd

    filtered = pd.DataFrame(
        [
            {"context_id": "C1", "value_numeric": 6_693_000_000.0, "decimals": "-6"},
            {"context_id": "C1", "value_numeric": 6_700_000_000.0, "decimals": "-8"},
        ]
    )
    reconciled, notes = _reconcile_same_context_precision_duplicates_from_warehouse(filtered)
    assert len(reconciled) == 1
    assert reconciled.iloc[0]["value_numeric"] == 6_693_000_000.0
    assert len(notes) == 1
    assert "reconciled" in notes[0]


# =============================================================================
# 3. ixTransformValueError handling (D-031/D-032) -- CRWD FY2021's
#    us-gaap:LineOfCredit fact
# =============================================================================
# docs/DECISIONS_LOG.md D-031: CRWD's FY2021 10-K revolver-outstanding
# fact has value_raw="(ixTransformValueError)" in Arelle's own output --
# Arelle could not decode it. D-032: approved, ONE-TIME, narrowly-scoped
# (exactly this one fact) manual recovery from the same locked SEC HTML
# (displayed text "No", transform ixt-sec:numwordsen -> 0.0). This is
# NOT a general mechanism -- the task explicitly requires re-verifying
# it stays scoped to exactly its two documented accessions (D-032 CRWD,
# D-033 NVDA) and does not generalize.

CRWD_FY2021_ACCESSION = "0001535527-21-000007"
NVDA_FY2020_ACCESSION = "0001045810-20-000010"


def test_crwd_fy2021_ixtransformvalueerror_fact_recovered_as_zero():
    result = recovered_current_debt_zero(CRWD_FY2021_ACCESSION)
    assert result is not None
    assert result["status"] == "PASS"
    assert result["value"] == 0.0
    assert result["basis"] == "SEC_HTML_MANUAL_PARSE"
    assert result["lineage"]["manual_fact_recovery"]["arelle_value_raw"] == "(ixTransformValueError)"
    assert result["lineage"]["manual_fact_recovery"]["concept_qname"] == "us-gaap:LineOfCredit"


def test_manual_recovery_stays_narrowly_scoped_to_exactly_two_accessions():
    """D-032/D-033 are explicit, narrow, one-time exceptions -- not a
    general ixTransformValueError-handling mechanism. Any other
    accession (including a plausible-looking one) must return None, and
    the override table itself must contain exactly these two entries."""
    assert set(MANUAL_FACT_RECOVERY_BY_ACCESSION.keys()) == {CRWD_FY2021_ACCESSION, NVDA_FY2020_ACCESSION}
    assert recovered_current_debt_zero("0001535527-22-000006") is None  # CRWD FY2022, a real neighboring accession
    assert recovered_current_debt_zero("0000000000-00-000000") is None
    assert recovered_current_debt_zero("") is None


# =============================================================================
# 4. Meta's "Income (loss) from operations" label variant
# =============================================================================
# docs/CURRENT_STATE.md lines ~549-577: Meta labels its
# us-gaap:OperatingIncomeLoss line "Income (loss) from operations", not
# "Operating income"/"Operating income (loss)" as Oracle/Microsoft do --
# a third distinct SEC-filer labeling convention for the same concept.
# Originally failed closed with 0 row candidates; fixed by broadening
# operating_income's mention pattern to also match "(income|loss) ...
# from operations".


def test_meta_income_loss_from_operations_label_resolves_as_operating_income():
    presentation = presentation_df(
        [
            pres_row(
                "us-gaap:OperatingIncomeLoss", "Income (loss) from operations",
                role_uri=IS_ROLE_URI, role_definition=IS_ROLE_DEF, period_type="duration",
            ),
            pres_row(
                "us-gaap:CostsAndExpenses", "Total costs and expenses",
                role_uri=IS_ROLE_URI, role_definition=IS_ROLE_DEF, period_type="duration",
            ),
        ]
    )
    row, _candidates = identify_canonical_row(presentation, BUILT_IN_METRICS["operating_income"])
    assert row["concept_qname"] == "us-gaap:OperatingIncomeLoss"
    assert row["selection_tier"] == "plain"


def test_oracle_microsoft_style_operating_income_label_still_resolves():
    """Regression guard: the broadened pattern must not have narrowed
    coverage of the original, more common 'Operating income (loss)'
    label convention (Oracle/Microsoft)."""
    presentation = presentation_df(
        [
            pres_row(
                "us-gaap:OperatingIncomeLoss", "Operating income",
                role_uri=IS_ROLE_URI, role_definition=IS_ROLE_DEF, period_type="duration",
            ),
        ]
    )
    row, _candidates = identify_canonical_row(presentation, BUILT_IN_METRICS["operating_income"])
    assert row["concept_qname"] == "us-gaap:OperatingIncomeLoss"


# =============================================================================
# 5. Micron's bare "Current debt" label
# =============================================================================
# docs/DECISIONS_LOG.md D-019: Micron's balance sheet reports a line
# literally titled bare "Current debt" (us-gaap:DebtCurrent), with no
# further qualifier ("short-term", "commercial paper", "portion of",
# "maturities of") that current_debt's prior label pattern required --
# invisible to every tier, incorrectly falling through toward D-017's
# zero-inference for a company that in fact reports nonzero current
# debt. Fixed by adding `current\s+debt` to current_debt's mention/plain
# patterns, guarded by the pattern's existing `non-?current` exclusion.


def test_micron_bare_current_debt_label_resolves():
    presentation = presentation_df(
        [
            pres_row(
                "us-gaap:DebtCurrent", "Current debt",
                role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF,
                parent_qname="us-gaap:LiabilitiesCurrentAbstract",
            ),
        ]
    )
    components = find_current_debt_sibling_components(presentation)
    assert components is not None and components != "AMBIGUOUS"
    assert [c["concept_qname"] for c in components] == ["us-gaap:DebtCurrent"]


def test_bare_current_debt_label_via_identify_canonical_row_too():
    presentation = presentation_df(
        [
            pres_row(
                "us-gaap:DebtCurrent", "Current debt",
                role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF,
            ),
        ]
    )
    row, _candidates = identify_canonical_row(presentation, BUILT_IN_METRICS["current_debt"])
    assert row["concept_qname"] == "us-gaap:DebtCurrent"


def test_non_current_debt_label_is_still_excluded_by_the_guard():
    """The `current\\s+debt` addition is guarded by the existing
    `non-?current` exclusion -- a row explicitly labeled "Non-current
    debt" must never be mistaken for current_debt."""
    presentation = presentation_df(
        [
            pres_row(
                "us-gaap:LongTermDebtNoncurrent", "Non-current debt",
                role_uri=BS_ROLE_URI, role_definition=BS_ROLE_DEF,
            ),
        ]
    )
    try:
        identify_canonical_row(presentation, BUILT_IN_METRICS["current_debt"])
        raised = False
    except TargetRowNotFound:
        raised = True
    assert raised, "a row labeled 'Non-current debt' must never resolve as current_debt"
