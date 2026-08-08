"""
policies/manual_fact_recovery.py — D-032 and D-033: exactly TWO
narrowly-scoped, explicitly-documented manual recoveries of a single
Inline XBRL fact each, where Arelle itself could not decode the value
(``value_raw="(ixTransformValueError)"``, ``value_numeric=NULL``).

*** THIS IS NOT A GENERAL MECHANISM. ***

D-032 (docs/DECISIONS_LOG.md) is explicit: "this decision applies ONLY
to the one specific fact identified in D-031 ... it is NOT a general
policy for handling every future ixTransformValueError case; each such
case must be brought to the user individually, per the same
'never guess' fail-closed principle that governs every other tier in
this project." D-033 applies the same one-time rule to a second,
independently identified fact.

Do not add a third entry to MANUAL_FACT_RECOVERY_BY_ACCESSION without a
new, explicit decision-log entry the same way D-032/D-033 were each
separately recorded. Do not generalize this into a pattern-matching or
heuristic recovery mechanism.

Ported byte-exact (the MANUAL_FACT_RECOVERY dict literal and the exact
current_debt override it feeds) from scripts/102_crwd_fy2021_manual_
html_parse.py (D-032) and scripts/103_nvda_fy2020_manual_html_parse.py
(D-033).
"""

from __future__ import annotations

# D-032 — CRWD FY2021 (accession 0001535527-21-000007), us-gaap:LineOfCredit,
# context i36456875e4304ffba24e889b8aca8952_I20210131. Recovered from the
# original locked SEC 10-K HTML: displayed text "No", transform
# ixt-sec:numwordsen, surrounding sentence confirms 0.
CRWD_FY2021_LINE_OF_CREDIT = {
    "concept_qname": "us-gaap:LineOfCredit",
    "context_id": "i36456875e4304ffba24e889b8aca8952_I20210131",
    "dimensions": {"us-gaap:CreditFacilityAxis": "us-gaap:RevolvingCreditFacilityMember"},
    "instant_date": "2021-01-31",
    "html_element": (
        '<ix:nonFraction unitRef="usd" '
        'contextRef="i36456875e4304ffba24e889b8aca8952_I20210131" '
        'decimals="INF" format="ixt-sec:numwordsen" '
        'name="us-gaap:LineOfCredit" scale="0" '
        'id="id3VybDovL2RvY3MudjEvZG9jOjJiYTdmNGRjMDUzMDQwMGU5MzI3OGJiMTYwYTQwMjU1L3NlYzoyYmE3ZjRkYzA1MzA0MDBlOTMyNzhiYjE2MGE0MDI1NV8xMjcvZnJhZzpmM2NhMWFmYjIwMjk0ZDI2YWIxZTZlYzBjM2VjMTgzYS90ZXh0cmVnaW9uOmYzY2ExYWZiMjAyOTRkMjZhYjFlNmVjMGMzZWMxODNhXzI4MjE_'
        'd947932e-c00d-4be7-bfee-8d9f8ff30060">No</ix:nonFraction>'
    ),
    "displayed_text": "No",
    "surrounding_sentence": (
        "No amounts were outstanding under the A&R Credit Agreement "
        "as of January 31, 2021."
    ),
    "sign": "positive (no minus sign or negative indicator present)",
    "scale": "0",
    "format_transform": "ixt-sec:numwordsen",
    "final_parsed_value": 0.0,
    "arelle_value_raw": "(ixTransformValueError)",
    "arelle_value_numeric": None,
    "recovery_method": (
        "Manual parse of the original locked SEC 10-K HTML (not an "
        "external source, not an estimate) — approved by the user "
        "explicitly for this one identified Arelle ixTransformValueError."
    ),
}

# D-033 — NVDA FY2020 (accession 0001045810-20-000010), us-gaap:LineOfCredit,
# context FI2020Q4_us-gaap_CreditFacilityAxis_us-gaap_RevolvingCreditFacilityMember.
NVDA_FY2020_LINE_OF_CREDIT = {
    "concept_qname": "us-gaap:LineOfCredit",
    "context_id": "FI2020Q4_us-gaap_CreditFacilityAxis_us-gaap_RevolvingCreditFacilityMember",
    "dimensions": {"us-gaap:CreditFacilityAxis": "us-gaap:RevolvingCreditFacilityMember"},
    "instant_date": "2020-01-26",
    "html_element": (
        '<ix:nonFraction id="d29785171e1120-wk-Fact-12183F73F0C670A50F86EBA117F1A41E" '
        'name="us-gaap:LineOfCredit" '
        'contextRef="FI2020Q4_us-gaap_CreditFacilityAxis_us-gaap_RevolvingCreditFacilityMember" '
        'unitRef="usd" decimals="INF" scale="0" '
        'format="ixt-sec:numwordsen">no</ix:nonFraction>'
    ),
    "displayed_text": "no",
    "surrounding_sentence": (
        "...as of January 26, 2020, we had not borrowed any amounts "
        "under this agreement."
    ),
    "sign": "positive (no minus sign or negative indicator present)",
    "scale": "0",
    "format_transform": "ixt-sec:numwordsen",
    "final_parsed_value": 0.0,
    "arelle_value_raw": "(ixTransformValueError)",
    "arelle_value_numeric": None,
    "recovery_method": (
        "Manual parse of the original locked SEC 10-K HTML (not an "
        "external source, not an estimate) — approved per the D-032 "
        "rule, applied here to the identical class of Arelle transform "
        "failure found in NVDA's own FY2020 filing."
    ),
}

# Keyed by accession_number — the exact, narrow scope of D-032/D-033.
MANUAL_FACT_RECOVERY_BY_ACCESSION: dict[str, dict[str, object]] = {
    "0001535527-21-000007": CRWD_FY2021_LINE_OF_CREDIT,
    "0001045810-20-000010": NVDA_FY2020_LINE_OF_CREDIT,
}


def recovered_current_debt_zero(accession_number: str) -> dict[str, object] | None:
    """
    Returns the same current_debt result shape used by scripts/102 and
    scripts/103 (status=PASS, value=0.0, basis=SEC_HTML_MANUAL_PARSE)
    for exactly the two accessions above, or None otherwise. Callers
    must independently confirm the usual preconditions still hold
    (mode == "zero_inference_needed", zero unclaimed current-classified
    ancestry candidates) before using this override — mirroring the
    explicit `assert` each original script made before applying it.
    """

    recovery = MANUAL_FACT_RECOVERY_BY_ACCESSION.get(accession_number)
    if recovery is None:
        return None

    return {
        "status": "PASS",
        "value": recovery["final_parsed_value"],
        "concept_qname": recovery["concept_qname"],
        "selection_tier": "zero_explicit_undrawn_facility_manual_html_parse",
        "basis": "SEC_HTML_MANUAL_PARSE",
        "lineage": {
            "manual_fact_recovery": recovery,
            "unclaimed_current_classified_candidates": 0,
            "note": (
                "Arelle recorded value_raw='(ixTransformValueError)' / "
                "value_numeric=NULL for this exact fact (same concept, "
                "context, and dimensions) in the warehouse — preserved "
                "here as evidence. The value was recovered by manually "
                "reading the same locked SEC HTML source, not estimated "
                "or sourced externally, per explicit user approval "
                "(D-032/D-033)."
            ),
        },
    }
