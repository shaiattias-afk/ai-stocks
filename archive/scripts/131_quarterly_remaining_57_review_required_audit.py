"""
Read-only root-cause audit of the 57 unique (ticker, fiscal_year_end,
metric_name) quarterly REVIEW_REQUIRED cases that remain after the
engine-v2 annual-anchor production load (scripts/130, D-037), which
resolved all 54 ANNUAL_ROW_NOT_RESOLVED cases.

Baseline: scripts/127_quarterly_review_required_root_cause_audit.py
(NOT modified). This script extends it in one respect the task requires:
script 127 always checked warehouse evidence against the FY (10-K)
accession, even for a Q1/Q2/Q3-caused failure. This script checks the
warehouse evidence against the SPECIFIC blocking quarter's OWN 10-Q
accession (quarterly_metric_results.accession_number is the exact
quarter's own accession for genuinely-unresolved rows — confirmed by
reading scripts/123's build_quarter_row_allowing_review_required, which
sets it to filings[quarter]["accession_number"], never the FY accession,
for any quarter other than Q4).

Opens the production database and the raw XBRL warehouse READ-ONLY.
Writes only two new files (data/quarterly_remaining_57_audit.json and
.csv) — no database row, schema, or extraction/engine logic is touched,
no filing is downloaded, no Arelle process is run.

Classification approach (same shared-error-string logic as scripts/118
and scripts/127): every REVIEW_REQUIRED placeholder row's lineage_json
carries one shared `error` string per metric-year case; the quarter
number embedded at the start of that string (e.g. "Q1:", "Q2 quarter-
duration...") identifies the earliest true blocking fact without extra
bookkeeping. The blocking quarter's own accession is then inspected for
plausible facts matching the metric's known concept-name keywords, both
with and without dimensions, and its duration (in days) is classified
against the same day-count buckets scripts/109-130 use, to distinguish
"no plausible fact anywhere in the filing" from "a plausible fact exists
but falls just outside the expected duration window" from "a plausible
fact exists and matches, but the engine's own presentation-based concept
resolution evidently picked a different concept" (all three are read-only
observations — no concept or value is EVER selected or written).
"""

from __future__ import annotations

import csv
import json
import re
import time
from collections import Counter
from datetime import date
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"

EXPECTED_UNIQUE_CASES = 57
JSON_OUTPUT_PATH = DATA_DIR / "quarterly_remaining_57_audit.json"
CSV_OUTPUT_PATH = DATA_DIR / "quarterly_remaining_57_audit.csv"

QUARTERS = ["Q1", "Q2", "Q3", "Q4"]

# --- day-count duration buckets, copied read-only from scripts/118 ---
QUARTER_DURATION_MIN_DAYS, QUARTER_DURATION_MAX_DAYS = 89, 92
YTD_6M_MIN_DAYS, YTD_6M_MAX_DAYS = 181, 184
YTD_9M_MIN_DAYS, YTD_9M_MAX_DAYS = 271, 275
YTD_12M_MIN_DAYS, YTD_12M_MAX_DAYS = 364, 366


def classify_duration(days: int | None) -> str | None:
    if days is None:
        return None
    if QUARTER_DURATION_MIN_DAYS <= days <= QUARTER_DURATION_MAX_DAYS:
        return "quarter"
    if YTD_6M_MIN_DAYS <= days <= YTD_6M_MAX_DAYS:
        return "ytd_6m"
    if YTD_9M_MIN_DAYS <= days <= YTD_9M_MAX_DAYS:
        return "ytd_9m"
    if YTD_12M_MIN_DAYS <= days <= YTD_12M_MAX_DAYS:
        return "ytd_12m"
    return "other"


def duration_days(period_start: str, period_end: str) -> int | None:
    try:
        return (date.fromisoformat(str(period_end)) - date.fromisoformat(str(period_start))).days
    except (TypeError, ValueError):
        return None


# Metric -> plausible alternate/standard concept keyword substrings, used
# ONLY for the read-only warehouse evidence check (never for selection).
# Same list as scripts/127.
METRIC_KEYWORDS = {
    "revenue": ["Revenue", "Sales", "SalesRevenue"],
    "operating_income": ["OperatingIncomeLoss"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
        "IncomeLossFromContinuingOperationsIncludingNoncontrollingInterestBeforeIncomeTaxesExtraordinaryItems",
        "IncomeLossBeforeIncomeTaxes",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForCapitalImprovements",
        "PaymentsToAcquireProductiveAssets", "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    ],
}

# Expected duration bucket(s) plausible for each (primary_category, blocking_quarter).
EXPECTED_DURATION_BY_QUARTER_CATEGORY = {
    ("Q1", "DIRECT_QUARTER_NOT_RESOLVED"): ["quarter"],
    ("Q1", "CONCEPT_NOT_RESOLVED"): ["quarter"],
    ("Q2", "YTD_FACT_NOT_RESOLVED"): ["quarter", "ytd_6m"],
    ("Q2", "CONCEPT_NOT_RESOLVED"): ["quarter", "ytd_6m"],
    ("Q3", "YTD_FACT_NOT_RESOLVED"): ["quarter", "ytd_9m"],
    ("Q3", "CONCEPT_NOT_RESOLVED"): ["quarter", "ytd_9m"],
}

# --- known scripts/118 error-string templates -> primary category ---
# (7-category taxonomy the task requires; unmapped/annual-side errors that
#  should no longer exist post-D-037 fall through to OTHER, defensively)
CLASSIFICATION_RULES = [
    (re.compile(r"^Q1: row not found"), "CONCEPT_NOT_RESOLVED"),
    (re.compile(r"^Q2: row not found"), "CONCEPT_NOT_RESOLVED"),
    (re.compile(r"^Q3: row not found"), "CONCEPT_NOT_RESOLVED"),
    (re.compile(r"^Q1 quarter-duration value did not resolve"), "DIRECT_QUARTER_NOT_RESOLVED"),
    (re.compile(r"^Q2: neither a direct quarter value nor a 6-month YTD"), "YTD_FACT_NOT_RESOLVED"),
    (re.compile(r"^Q3: neither a direct quarter value nor a 9-month YTD"), "YTD_FACT_NOT_RESOLVED"),
]

BLOCKING_QUARTER_PATTERN = re.compile(r"^(Q[1-4])")


def classify_error_text(error_text: str) -> str:
    for pattern, category in CLASSIFICATION_RULES:
        if pattern.search(error_text):
            return category
    return "OTHER"


def extract_blocking_quarter(error_text: str) -> str | None:
    match = BLOCKING_QUARTER_PATTERN.match(error_text)
    return match.group(1) if match else None


def warehouse_evidence_for_case(
    warehouse_connection, accession_number: str, metric_name: str,
    expected_period_end: str | None, expected_duration_classes: list[str],
) -> dict:
    keywords = METRIC_KEYWORDS.get(metric_name, [])
    if not keywords or not accession_number:
        return {"checked": False, "reason": "no keyword list or accession available", "finding": "NOT_CHECKED"}

    like_clauses = " OR ".join(["concept_local_name LIKE ?"] * len(keywords))
    params = [accession_number] + [f"%{kw}%" for kw in keywords]
    rows = warehouse_connection.execute(
        f"SELECT concept_qname, concept_namespace, period_start, period_end, value_numeric, "
        f"decimals, context_id, dimensions_json "
        f"FROM xbrl_facts WHERE accession_number = ? AND is_nil = FALSE AND value_numeric IS NOT NULL "
        f"AND ({like_clauses})",
        params,
    ).fetchall()

    if not rows:
        return {
            "checked": True, "finding": "NO_PLAUSIBLE_FACT_IN_FILING",
            "candidate_concepts": [], "has_dimensioned_candidates": False,
        }

    standard_ns_pattern = re.compile(r"fasb\.org|xbrl\.sec\.gov|xbrl\.org|w3\.org", re.IGNORECASE)

    non_dim_rows = [r for r in rows if r[7] == "{}"]
    dim_rows = [r for r in rows if r[7] != "{}"]

    def is_extension(namespace: str) -> bool:
        return not bool(standard_ns_pattern.search(namespace or ""))

    distinct_concepts_all = sorted({r[0] for r in rows})
    candidate_concepts = [
        {"concept_qname": c, "is_extension": is_extension(next(r[1] for r in rows if r[0] == c) or "")}
        for c in distinct_concepts_all
    ]

    non_dim_at_expected_end = [r for r in non_dim_rows if expected_period_end and str(r[3]) == expected_period_end]
    dim_at_expected_end = [r for r in dim_rows if expected_period_end and str(r[3]) == expected_period_end]

    duration_findings = []
    matching_duration_rows = []
    for r in non_dim_at_expected_end:
        days = duration_days(str(r[2]), str(r[3]))
        bucket = classify_duration(days)
        duration_findings.append({
            "concept_qname": r[0], "period_start": str(r[2]), "period_end": str(r[3]),
            "value_numeric": r[4], "decimals": r[5], "context_id": r[6],
            "duration_days": days, "duration_bucket": bucket,
        })
        if bucket in expected_duration_classes:
            matching_duration_rows.append(r)

    distinct_matching_values = sorted({round(r[4], 2) for r in matching_duration_rows})
    has_dimensioned_candidates = len(dim_rows) > 0

    if not non_dim_at_expected_end:
        if dim_at_expected_end:
            finding = "PLAUSIBLE_FACT_ONLY_WITH_DIMENSIONS"
        else:
            finding = "PLAUSIBLE_FACT_UNEXPECTED_CONTEXT_OR_DURATION"
    elif len(distinct_matching_values) > 1:
        finding = "MULTIPLE_PLAUSIBLE_FACTS_AMBIGUOUS"
    elif len(distinct_matching_values) == 1:
        matched_concepts = {r[0] for r in matching_duration_rows}
        finding = (
            "PLAUSIBLE_FACT_EXTENSION_CONCEPT"
            if all(is_extension(next(r[1] for r in rows if r[0] == c) or "") for c in matched_concepts)
            else "PLAUSIBLE_FACT_STANDARD_CONCEPT"
        )
    else:
        # facts exist at the expected end date but none classify into the
        # expected duration bucket (e.g. exactly the CRWD 88-day case:
        # Feb-1-to-Apr-30 in a non-leap year is 1 day short of the fixed
        # 89-92 "quarter" bucket)
        finding = "PLAUSIBLE_FACT_UNEXPECTED_CONTEXT_OR_DURATION"

    return {
        "checked": True, "finding": finding,
        "candidate_concepts": candidate_concepts,
        "has_dimensioned_candidates": has_dimensioned_candidates,
        "facts_at_expected_period_end": duration_findings,
        "distinct_matching_values_count": len(distinct_matching_values),
    }


def refine_category(primary_category: str, evidence: dict) -> str:
    finding = evidence.get("finding")
    if finding == "NO_PLAUSIBLE_FACT_IN_FILING":
        return "TRUE_SOURCE_DATA_ABSENCE"
    if finding == "MULTIPLE_PLAUSIBLE_FACTS_AMBIGUOUS":
        return "AMBIGUOUS_MULTIPLE_FACTS"
    if finding in ("PLAUSIBLE_FACT_UNEXPECTED_CONTEXT_OR_DURATION", "PLAUSIBLE_FACT_ONLY_WITH_DIMENSIONS"):
        return "CONTEXT_OR_DURATION_NOT_RESOLVED"
    # PLAUSIBLE_FACT_STANDARD_CONCEPT / PLAUSIBLE_FACT_EXTENSION_CONCEPT / NOT_CHECKED:
    # a fact exists exactly where expected but the engine still didn't resolve
    # it -> most likely evidence of a concept-selection mismatch, not missing
    # data; keep the original error-text-based category, group analysis below
    # calls this an EXISTING_POLICY_IMPLEMENTATION_GAP signal.
    return primary_category


def main() -> dict:
    start_time = time.perf_counter()
    print("=" * 100)
    print("QUARTERLY REMAINING-57 REVIEW_REQUIRED ROOT-CAUSE AUDIT (read-only)")
    print("=" * 100)

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    unique_cases = prod_connection.execute(
        "SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results "
        "WHERE reconciliation_status = 'REVIEW_REQUIRED' ORDER BY ticker, fiscal_year_end, metric_name"
    ).fetchall()
    print(f"Authoritative unique REVIEW_REQUIRED metric-year cases: {len(unique_cases)} (expected {EXPECTED_UNIQUE_CASES})")

    if len(unique_cases) != EXPECTED_UNIQUE_CASES:
        fail_output = {
            "status": "FAIL",
            "reason": f"Unique case count {len(unique_cases)} does not match the expected verified state ({EXPECTED_UNIQUE_CASES}). Refusing to continue on an unverified count.",
            "actual_unique_cases": len(unique_cases),
            "expected_unique_cases": EXPECTED_UNIQUE_CASES,
            "runtime_seconds": round(time.perf_counter() - start_time, 2),
        }
        JSON_OUTPUT_PATH.write_text(json.dumps(fail_output, indent=2, ensure_ascii=False), encoding="utf-8")
        prod_connection.close()
        warehouse_connection.close()
        print(json.dumps(fail_output, indent=2, ensure_ascii=False))
        return fail_output

    cases = []
    for ticker, fiscal_year_end, metric_name in unique_cases:
        run_row = prod_connection.execute(
            "SELECT run_id, q1_accession, q2_accession, q3_accession, fy_accession "
            "FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?",
            [ticker, fiscal_year_end],
        ).fetchone()
        run_id, q1_acc, q2_acc, q3_acc, fy_acc = run_row

        rows = prod_connection.execute(
            "SELECT fiscal_quarter, value, extraction_basis, concept_qname, context_id, "
            "accession_number, reconciliation_status, reconciliation_difference, permitted_difference, "
            "lineage_json, result_status "
            "FROM quarterly_metric_results WHERE run_id = ? AND metric_name = ? ORDER BY fiscal_quarter",
            [run_id, metric_name],
        ).fetchall()

        quarter_details = {}
        affected_quarters = []
        resolved_quarters = []
        error_texts = set()
        resolved_concept_qname = None

        for (fq, value, basis, concept_qname, context_id, accession_number, recon_status,
             reconciliation_difference, permitted_difference, lineage_json, result_status) in rows:
            lineage = json.loads(lineage_json) if lineage_json else {}
            is_unresolved = value is None
            quarter_details[fq] = {
                "value": value, "extraction_basis": basis, "concept_qname": concept_qname,
                "context_id": context_id, "accession_number": accession_number,
                "reconciliation_status": recon_status, "result_status": result_status,
                "lineage_error": lineage.get("error"),
            }
            if is_unresolved:
                affected_quarters.append(fq)
                if lineage.get("error"):
                    error_texts.add(lineage["error"])
            else:
                resolved_quarters.append(fq)
                if concept_qname and resolved_concept_qname is None:
                    resolved_concept_qname = concept_qname

        any_quarter_resolved = len(resolved_quarters) > 0
        representative_error = sorted(error_texts)[0] if error_texts else None
        primary_category = classify_error_text(representative_error) if representative_error else "OTHER"
        blocking_quarter = extract_blocking_quarter(representative_error) if representative_error else None
        if blocking_quarter is None and affected_quarters:
            blocking_quarter = sorted(affected_quarters, key=lambda q: QUARTERS.index(q))[0]

        blocking_accession = quarter_details.get(blocking_quarter, {}).get("accession_number") if blocking_quarter else None
        if blocking_accession is None:
            blocking_accession = {"Q1": q1_acc, "Q2": q2_acc, "Q3": q3_acc, "Q4": fy_acc}.get(blocking_quarter)

        blocking_report_date = None
        if blocking_accession:
            sf_row = prod_connection.execute(
                "SELECT report_date FROM sec_filings WHERE accession_number = ?", [blocking_accession]
            ).fetchone()
            blocking_report_date = str(sf_row[0]) if sf_row else None

        expected_duration_classes = EXPECTED_DURATION_BY_QUARTER_CATEGORY.get(
            (blocking_quarter, primary_category), ["quarter", "ytd_6m", "ytd_9m", "ytd_12m"]
        )

        evidence = warehouse_evidence_for_case(
            warehouse_connection, blocking_accession, metric_name, blocking_report_date, expected_duration_classes
        )
        final_category = refine_category(primary_category, evidence)

        extraction_path_by_category = {
            "CONCEPT_NOT_RESOLVED": "s89 statement-first presentation-based canonical-row identification, run independently against the blocking quarter's OWN 10-Q accession",
            "DIRECT_QUARTER_NOT_RESOLVED": "concept resolved; facts_for_concept + pick_current_period_fact matched against the 'quarter' duration bucket (89-92 days) on the blocking quarter's own accession",
            "YTD_FACT_NOT_RESOLVED": "concept resolved; facts_for_concept + pick_current_period_fact tried 'quarter' then the cumulative YTD duration bucket on the blocking quarter's own accession",
            "OTHER": "unclassified / reconciliation-side failure, not a missing-fact case",
        }

        case_record = {
            "ticker": ticker, "fiscal_year_end": fiscal_year_end, "metric_name": metric_name,
            "affected_quarters": affected_quarters, "resolved_quarters": resolved_quarters,
            "any_quarter_resolved": any_quarter_resolved,
            "concept_qname": resolved_concept_qname,
            "filing_accessions": {"Q1": q1_acc, "Q2": q2_acc, "Q3": q3_acc, "FY": fy_acc},
            "blocking_quarter": blocking_quarter,
            "blocking_quarter_accession": blocking_accession,
            "blocking_quarter_report_date": blocking_report_date,
            "representative_error_text": representative_error,
            "all_distinct_error_texts": sorted(error_texts),
            "extraction_path_attempted": extraction_path_by_category.get(primary_category, "unknown"),
            "primary_category_from_error_text": primary_category,
            "root_cause_category": final_category,
            "warehouse_evidence": evidence,
            "quarter_details": quarter_details,
        }
        cases.append(case_record)
        print(f"  {ticker} {fiscal_year_end} {metric_name}: {final_category} "
              f"(blocking={blocking_quarter}, evidence={evidence.get('finding')})")

    prod_connection.close()
    warehouse_connection.close()

    # --- aggregate summaries ---
    by_ticker = Counter(c["ticker"] for c in cases)
    by_metric = Counter(c["metric_name"] for c in cases)
    by_root_cause = Counter(c["root_cause_category"] for c in cases)
    by_evidence_finding = Counter(c["warehouse_evidence"].get("finding") for c in cases)
    cases_with_standard_concept = [c for c in cases if c["warehouse_evidence"].get("finding") == "PLAUSIBLE_FACT_STANDARD_CONCEPT"]
    cases_with_extension_concept = [c for c in cases if c["warehouse_evidence"].get("finding") == "PLAUSIBLE_FACT_EXTENSION_CONCEPT"]
    cases_duration_context = [c for c in cases if c["root_cause_category"] == "CONTEXT_OR_DURATION_NOT_RESOLVED"]
    cases_true_absence = [c for c in cases if c["root_cause_category"] == "TRUE_SOURCE_DATA_ABSENCE"]
    cases_ambiguous = [c for c in cases if c["root_cause_category"] == "AMBIGUOUS_MULTIPLE_FACTS"]

    # specific, named signature: a fact exists at the expected period_end
    # but its duration is exactly ONE DAY short of the relevant fixed
    # bucket's lower bound -- 88 days vs. QUARTER_DURATION_MIN_DAYS=89 for
    # a Feb-1-to-Apr-30 fiscal quarter (CRWD), and 180 days vs.
    # YTD_6M_MIN_DAYS=181 for a Jan-1-to-Jun-30 fiscal first-half (GOOGL,
    # META). Both are calendar artifacts of the SAME design gap: the fixed
    # day-count buckets in classify_duration() (scripts/109-130) were
    # evidently calibrated against a July-December-style half-year
    # (MSFT's fiscal H1, 184 days) and a leap-year Feb-quarter, and are one
    # day too narrow for a January-June half-year or a non-leap-year
    # February quarter -- both extremely common, non-exotic fiscal
    # calendars, so this is a general, non-ticker-specific classification
    # bug, not a per-filer data issue.
    off_by_one_day_boundary_cases = []
    for c in cases:
        if c["root_cause_category"] != "CONTEXT_OR_DURATION_NOT_RESOLVED":
            continue
        facts = c["warehouse_evidence"].get("facts_at_expected_period_end", [])
        if any(f["duration_bucket"] == "other" and f["duration_days"] in (QUARTER_DURATION_MIN_DAYS - 1, YTD_6M_MIN_DAYS - 1) for f in facts):
            off_by_one_day_boundary_cases.append(f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}")

    # group-level general-fix assessment
    groups = []
    concept_not_resolved_with_plausible_concept = [
        c for c in cases
        if c["primary_category_from_error_text"] == "CONCEPT_NOT_RESOLVED"
        and c["warehouse_evidence"].get("finding") in ("PLAUSIBLE_FACT_STANDARD_CONCEPT", "PLAUSIBLE_FACT_EXTENSION_CONCEPT")
    ]

    groups.append({
        "group": "Fixed day-count duration-classification buckets too narrow for common non-mid-year fiscal calendars (CRWD Q1 'quarter' bucket, GOOGL/META Q2 'ytd_6m' bucket)",
        "case_count": len(off_by_one_day_boundary_cases),
        "cases": off_by_one_day_boundary_cases,
        "assessment": "GENERAL_RULE_FIX",
        "detail": (
            "Every one of these cases has a single, unambiguous, standard- or "
            "extension-namespace fact at exactly the expected period_end, "
            "with a duration exactly one day short of the relevant bucket's "
            "lower bound: 88 days for CRWD's Feb-1-to-Apr-30 direct quarter "
            "(needs QUARTER_DURATION_MIN_DAYS=89) in every non-leap fiscal "
            "year in the dataset, and 180 days for GOOGL's and META's "
            "Jan-1-to-Jun-30 six-month YTD (needs YTD_6M_MIN_DAYS=181) in "
            "every affected year. This is not missing or ambiguous data --  "
            "it is a calendar artifact of the fixed day-count buckets in "
            "classify_duration() (scripts/109-130), which were evidently "
            "calibrated against a mid-year fiscal calendar (e.g. MSFT's "
            "Jul-Dec H1 = 184 days) and are one day too narrow for the "
            "equally common Jan-Jun H1 or Feb-Apr Q1 patterns. Not specific "
            "to CrowdStrike/Alphabet/Meta in principle -- any filer with "
            "these common fiscal-quarter boundaries would hit the same "
            "off-by-one classification."
        ),
    })
    groups.append({
        "group": "CONCEPT_NOT_RESOLVED cases (Q1) with a single plausible standard concept at the exact expected period/duration (MU, PANW pretax_income)",
        "case_count": len(concept_not_resolved_with_plausible_concept),
        "cases": [f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}" for c in concept_not_resolved_with_plausible_concept],
        "assessment": "EXISTING_POLICY_IMPLEMENTATION_GAP",
        "detail": (
            "A single standard-GAAP concept exists at exactly the expected "
            "period_end and duration, yet s89's presentation-based canonical- "
            "row identification, run independently against this specific "
            "quarter's own 10-Q, reported 'row not found'. This mirrors the "
            "exact shape of the bug D-037 already fixed for the annual side "
            "(scripts/128/130): the concept is resolvable, just not via this "
            "quarter's own presentation walk. The same general remedy -- "
            "reuse an already-resolved concept (from a sibling quarter or the "
            "annual anchor) instead of re-deriving it from scratch on a "
            "possibly-thinner interim ('Condensed') presentation linkbase -- "
            "is a plausible general fix, not a per-ticker one."
        ),
    })
    groups.append({
        "group": "TRUE_SOURCE_DATA_ABSENCE -- NVDA fiscal-year-end 2020-01-26, all 6 metrics, Q1 filing",
        "case_count": len(cases_true_absence),
        "cases": [f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}" for c in cases_true_absence],
        "assessment": "TRUE_SOURCE_DATA_ABSENCE",
        "detail": (
            "No candidate fact matching any of the metric's known concept-name "
            "keywords exists anywhere in this Q1 filing (accession "
            "0001045810-19-000079, report_date 2019-04-28) -- NVDA's oldest "
            "quarter in the 45-company-year universe. All 6 metrics fail "
            "identically on the SAME filing, which suggests either a genuinely "
            "different/older tagging convention in this specific filing "
            "(pre-dating concepts the newer filings use) rather than 6 "
            "independent data gaps, or a keyword-coverage gap in this "
            "diagnostic script itself -- not yet distinguished; needs one "
            "direct read of this filing's own concept list before concluding "
            "either way."
        ),
    })
    groups.append({
        "group": "AMBIGUOUS_MULTIPLE_FACTS -- revenue, META/PANW (caveat: partly a diagnostic keyword false-positive, not necessarily genuine filing ambiguity)",
        "case_count": len(cases_ambiguous),
        "cases": [f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}" for c in cases_ambiguous],
        "assessment": "AMBIGUOUS_REQUIRES_HUMAN_REVIEW",
        "detail": (
            "Manual inspection of 2 of these 8 cases (META FY2022 revenue, "
            "PANW FY2021 revenue) found this script's own METRIC_KEYWORDS "
            "substring 'Revenue' also matches concepts that are NOT top-line "
            "revenue -- us-gaap:CostOfRevenue, "
            "us-gaap:ContractWithCustomerLiabilityRevenueRecognized, "
            "us-gaap:RevenueRemainingPerformanceObligation -- inflating the "
            "apparent candidate count. In both sampled cases, exactly ONE "
            "genuine revenue concept "
            "(us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax) "
            "matched the expected period/duration once those false positives "
            "are excluded by eye. This means some or all of these 8 cases may "
            "actually belong in the previous group (a single plausible "
            "concept, implementation gap) rather than being genuinely "
            "ambiguous -- but this was not re-verified programmatically for "
            "all 8, so the case_count above is reported as-is (the diagnostic "
            "script's literal finding), not corrected. A tighter keyword list "
            "(excluding CostOfRevenue/ContractWithCustomerLiability*/"
            "RevenueRemainingPerformanceObligation) in a follow-up read-only "
            "pass would resolve this ambiguity in the audit itself, before "
            "any engine change is considered."
        ),
    })

    summary = {
        "total_unique_cases": len(cases),
        "by_ticker": dict(by_ticker), "by_metric": dict(by_metric),
        "by_root_cause_category": dict(by_root_cause),
        "by_warehouse_evidence_finding": dict(by_evidence_finding),
        "cases_with_plausible_standard_concept": len(cases_with_standard_concept),
        "cases_with_extension_concept": len(cases_with_extension_concept),
        "cases_caused_by_duration_or_context": len(cases_duration_context),
        "cases_true_source_data_absence": len(cases_true_absence),
        "cases_ambiguous_multiple_facts": len(cases_ambiguous),
        "off_by_one_day_duration_boundary_cases": off_by_one_day_boundary_cases,
        "groups": groups,
    }

    print("\n" + "=" * 100)
    print("SUMMARY")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("=" * 100)

    output = {
        "status": "PASS", "summary": summary, "cases": cases,
        "runtime_seconds": round(time.perf_counter() - start_time, 2),
    }
    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")

    csv_columns = [
        "ticker", "fiscal_year_end", "metric_name", "root_cause_category", "primary_category_from_error_text",
        "blocking_quarter", "blocking_quarter_accession", "affected_quarters", "resolved_quarters",
        "any_quarter_resolved", "concept_qname", "representative_error_text", "warehouse_evidence_finding",
        "has_dimensioned_candidates", "candidate_concepts_count",
    ]
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns)
        writer.writeheader()
        for c in cases:
            writer.writerow({
                "ticker": c["ticker"], "fiscal_year_end": c["fiscal_year_end"], "metric_name": c["metric_name"],
                "root_cause_category": c["root_cause_category"],
                "primary_category_from_error_text": c["primary_category_from_error_text"],
                "blocking_quarter": c["blocking_quarter"], "blocking_quarter_accession": c["blocking_quarter_accession"],
                "affected_quarters": ";".join(c["affected_quarters"]), "resolved_quarters": ";".join(c["resolved_quarters"]),
                "any_quarter_resolved": c["any_quarter_resolved"], "concept_qname": c["concept_qname"],
                "representative_error_text": c["representative_error_text"],
                "warehouse_evidence_finding": c["warehouse_evidence"].get("finding"),
                "has_dimensioned_candidates": c["warehouse_evidence"].get("has_dimensioned_candidates"),
                "candidate_concepts_count": len(c["warehouse_evidence"].get("candidate_concepts", [])),
            })
    print(f"CSV written to {CSV_OUTPUT_PATH}")

    return output


if __name__ == "__main__":
    main()
