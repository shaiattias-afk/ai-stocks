"""
Quarterly extraction engine v5 — identical to
scripts/136_quarterly_extraction_engine_v4_point_in_time_concept_reuse.py
in every respect EXCEPT one addition: a third, fixed, versioned concept-
resolution fallback tier for Q1/Q2/Q3, tried only after BOTH the primary
presentation-based resolver AND the point-in-time-safe concept-reuse
fallback have failed.

WHY: scripts/137's V4 validation found that 4 of the then-15
CONCEPT_REUSE_CANDIDATE cases could not be resolved by point-in-time
concept reuse because no point-in-time-safe EVIDENCE exists to reuse at
all — CRWD FY2022 pretax_income's only prior 10-K predates the Annual V1
universe; MU FY2021 pretax_income and PANW FY2021 pretax_income/revenue
are each that ticker's EARLIEST fiscal year in the database, so no prior
10-K exists whatsoever. These are not missing data — `revenue` and
`pretax_income` are standard, universally-tagged US-GAAP concepts, and a
correctly-tagged fact for one of a small, fixed set of standard concept
names is expected to exist directly in the blocking quarter's own 10-Q,
with no need to borrow a concept name from anywhere else.

THE ONE ADDITION — TIER 3, STANDARD_GAAP_ALLOW_LIST: when tiers 1 and 2
both fail for Q1, Q2, or Q3, and the metric is one with an approved
standard-concept allow-list (revenue, pretax_income only), try each
allow-listed concept_qname directly against the BLOCKING quarter's own
accession, using the exact same duration classes the downstream pipeline
would independently try for that quarter (Q1: quarter only; Q2/Q3:
quarter then YTD). A concept is accepted only if it yields exactly one
deterministic current-period value (never a prior-year comparative,
never a dimensioned/segment fact, never more than one genuinely
different value across allow-listed concepts). Like tier 2, this tier
ONLY ever supplies a concept_qname candidate — the actual financial VALUE
is always re-selected fresh from the blocking quarter's own exact
accession via the SAME unchanged facts_for_concept + pick_current_period_
fact functions used everywhere else in this engine, which independently
re-enforce every existing safeguard. If zero or more-than-one genuinely
different value remains even after this tier, the quarter (and therefore
the metric) remains REVIEW_REQUIRED, exactly as before — this fallback
can only ADD a resolution path, never remove or weaken an existing check.

RESOLUTION PRIORITY (unchanged order, one tier added at the end):
  1. Primary presentation-based resolver (resolve_concept_for_metric).
  2. Point-in-time-safe concept reuse (attempt_concept_reuse_fallback).
  3. Standard US-GAAP allow-list fallback (attempt_standard_gaap_
     allowlist_fallback) — NEW.
  4. REVIEW_REQUIRED.

UNCHANGED from scripts/136 (verbatim, not re-derived): duration
boundaries, the primary presentation-based resolver itself, the point-
in-time-safe concept-reuse fallback (tier 2), facts_for_concept, pick_
current_period_fact, DIRECT_QUARTER vs. DERIVED_FROM_YTD priority, Q4 =
Annual - Q3_9mYTD, the annual-anchor-from-production logic (D-037),
filing-date availability, the XBRL-decimals precision-tolerance
reconciliation (D-035), and REVIEW_REQUIRED behavior.

Read-only against both databases; writes nothing to either.

--- PR1 refactor note (structural extraction, no logic change) ---
This script's actual logic now lives in
src/stock_agent/extraction/quarterly.py (ported byte-exact, function by
function, with the importlib-loaded "s89" module reference — scripts/
89_panw_zero_long_term_debt_policy.py — replaced by direct imports of
the same functions, now in stock_agent.extraction.core, since that
core engine is what this PR ports). This file is now a thin CLI entry
point that imports and calls that package code, exactly as it did
before except for where the code physically lives. No formula,
threshold, or precedence order changed; see the PR description for the
full verification (scripts/171) that this move produced zero output
changes against the 45-company-year production baseline plus 148's own
72-row MSFT/AMZN/ORCL regression baseline.
"""

from __future__ import annotations

from stock_agent.extraction.quarterly import (
    ACCEPTED_ANNUAL_STATUSES,
    METRICS,
    PRODUCTION_DB_PATH,
    QUARTER_DURATION_CLASSES_TO_TRY,
    QUARTER_DURATION_MAX_DAYS,
    QUARTER_DURATION_MIN_DAYS,
    QUARTER_ORDER,
    STANDARD_GAAP_ALLOW_LIST,
    STANDARD_GAAP_ALLOW_LIST_VERSION,
    WAREHOUSE_DB_PATH,
    YTD_6M_MAX_DAYS,
    YTD_6M_MIN_DAYS,
    YTD_9M_MAX_DAYS,
    YTD_9M_MIN_DAYS,
    YTD_12M_MAX_DAYS,
    YTD_12M_MIN_DAYS,
    attempt_concept_reuse_fallback,
    attempt_standard_gaap_allowlist_fallback,
    classify_duration,
    compute_precision_aware_reconciliation,
    duration_days,
    facts_for_concept,
    load_filing_metadata,
    lookup_annual_fact_decimals,
    parse_arguments,
    parse_decimals,
    pick_current_period_fact,
    resolve_annual_anchor,
    resolve_concept_for_metric,
    run_quarterly_extraction_engine_v5,
    uncertainty_for_decimals,
)

__all__ = [
    "run_quarterly_extraction_engine_v5",
    "parse_arguments",
]

if __name__ == "__main__":
    arguments = parse_arguments()
    run_quarterly_extraction_engine_v5(
        ticker=arguments.ticker, fiscal_year_end=arguments.fiscal_year_end,
        q1_accession=arguments.q1_accession, q2_accession=arguments.q2_accession,
        q3_accession=arguments.q3_accession, fy_accession=arguments.fy_accession,
        json_output_path=arguments.json_output, csv_output_path=arguments.csv_output,
    )
