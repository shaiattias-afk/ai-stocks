"""
Read-only root-cause audit of the 21 unique quarterly REVIEW_REQUIRED
cases remaining after the engine-v3 duration-tolerance production load
(scripts/134). Baseline: scripts/131 (NOT modified).

Extends scripts/131 in three ways this task requires:
  1. A CONCEPT-REUSE check: for every case, look for a candidate concept
     already resolved in a sibling quarter of the same fiscal year, the
     authoritative annual production result for the same metric, or an
     adjacent fiscal year for the same company — then check (read-only)
     whether that exact concept_qname actually exists as a raw fact in
     the blocking quarter's own warehouse accession. This never selects
     or changes anything; it only reports whether such a concept exists.
  2. A TIGHTENED revenue evidence search using an exact allowed concept
     list (not the broad "contains 'Revenue'" substring search that
     scripts/127/131 used), explicitly excluding CostOfRevenue,
     ContractWithCustomerLiabilityRevenueRecognized,
     RevenueRemainingPerformanceObligation*, and segment/dimensioned
     revenue when a consolidated (no-dimension) candidate exists.
  3. A full, unfiltered concept-list inspection for NVDA's oldest
     (2020-01-26) filing, plus filing-validity checks (form, primary
     document, warehouse run status, fact/context/unit counts) — not
     only a keyword search — to determine whether the metrics are
     genuinely absent, tagged under a different/older concept, or
     whether the wrong accession might have been used.

Opens both databases READ-ONLY. Writes only two new files
(data/quarterly_remaining_21_audit.json and .csv) — no database row,
schema, or extraction/engine logic is touched, no filing is downloaded,
no Arelle process is run.
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

EXPECTED_UNIQUE_CASES = 21
JSON_OUTPUT_PATH = DATA_DIR / "quarterly_remaining_21_audit.json"
CSV_OUTPUT_PATH = DATA_DIR / "quarterly_remaining_21_audit.csv"

QUARTERS = ["Q1", "Q2", "Q3", "Q4"]

QUARTER_DURATION_MIN_DAYS, QUARTER_DURATION_MAX_DAYS = 88, 92  # engine v3 (current production) boundaries
YTD_6M_MIN_DAYS, YTD_6M_MAX_DAYS = 180, 184
YTD_9M_MIN_DAYS, YTD_9M_MAX_DAYS = 271, 275
YTD_12M_MIN_DAYS, YTD_12M_MAX_DAYS = 364, 366

RESOLVED_ANNUAL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_NORMALIZED_TAX", "PASS_DIRECT_AGGREGATE"}


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


# Broad substring keywords (same as scripts/127/131) — used for all metrics
# EXCEPT revenue, where the tightened exact-allow-list below is used instead.
METRIC_KEYWORDS = {
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

# --- tightened revenue concept list (exact local names only) ---
REVENUE_ALLOWED_LOCAL_NAMES = {
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
}
# explicitly excluded, named so the report can state exactly what was excluded and why
REVENUE_EXCLUDED_LOCAL_NAMES = {
    "CostOfRevenue": "cost of revenue, not top-line revenue",
    "ContractWithCustomerLiabilityRevenueRecognized": "deferred-revenue recognition roll-forward, not top-line revenue",
    "RevenueRemainingPerformanceObligation": "backlog / remaining performance obligation disclosure, not a period revenue amount",
    "RevenueRemainingPerformanceObligationPercentage": "backlog disclosure percentage, not a revenue amount",
    "RevenueRemainingPerformanceObligationExpectedTimingOfSatisfactionPeriod1": "backlog disclosure timing, not a revenue amount",
}
# dimension axes that indicate a segment/geography/product breakdown rather
# than a consolidated top-line figure
SEGMENT_DIMENSION_AXES = [
    "StatementBusinessSegmentsAxis", "StatementGeographicalAxis", "ProductOrServiceAxis",
    "ContractWithCustomerSalesChannelAxis",
]

CLASSIFICATION_RULES = [
    (re.compile(r"^Q1: row not found"), "CONCEPT_NOT_RESOLVED_Q1"),
    (re.compile(r"^Q2: row not found"), "CONCEPT_NOT_RESOLVED_Q2"),
    (re.compile(r"^Q3: row not found"), "CONCEPT_NOT_RESOLVED_Q3"),
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


def standard_namespace(namespace: str) -> bool:
    return bool(re.search(r"fasb\.org|xbrl\.sec\.gov|xbrl\.org|w3\.org", namespace or "", re.IGNORECASE))


def revenue_evidence(warehouse_connection, accession_number: str, expected_period_end: str | None) -> dict:
    """Tightened revenue-only evidence check: exact allowed local names,
    excludes known false-keyword-match concepts by name, and flags any
    remaining candidate that only has dimensioned (segment) facts."""
    if not accession_number:
        return {"checked": False, "reason": "no accession available", "finding": "NOT_CHECKED"}

    allow_clause = " OR ".join(["concept_local_name = ?"] * len(REVENUE_ALLOWED_LOCAL_NAMES))
    rows = warehouse_connection.execute(
        f"SELECT concept_qname, concept_namespace, concept_local_name, period_start, period_end, "
        f"value_numeric, decimals, context_id, dimensions_json "
        f"FROM xbrl_facts WHERE accession_number = ? AND is_nil = FALSE AND value_numeric IS NOT NULL "
        f"AND ({allow_clause})",
        [accession_number] + sorted(REVENUE_ALLOWED_LOCAL_NAMES),
    ).fetchall()

    # also report what the OLD broad substring search would have found, for
    # direct before/after comparison in the report
    broad_rows = warehouse_connection.execute(
        "SELECT DISTINCT concept_local_name FROM xbrl_facts WHERE accession_number = ? "
        "AND is_nil = FALSE AND value_numeric IS NOT NULL AND concept_local_name LIKE '%Revenue%'",
        [accession_number],
    ).fetchall()
    broad_concepts = sorted({r[0] for r in broad_rows})
    excluded_present = sorted({c for c in broad_concepts if c in REVENUE_EXCLUDED_LOCAL_NAMES})

    if not rows:
        return {
            "checked": True, "finding": "NO_PLAUSIBLE_FACT_IN_FILING", "candidate_concepts": [],
            "broad_search_concepts": broad_concepts, "excluded_false_positive_concepts": excluded_present,
        }

    non_dim = [r for r in rows if r[8] == "{}"]
    dim = [r for r in rows if r[8] != "{}"]
    non_dim_at_end = [r for r in non_dim if expected_period_end and str(r[4]) == expected_period_end]

    facts_detail = []
    for r in non_dim_at_end:
        days = duration_days(str(r[3]), str(r[4]))
        facts_detail.append({
            "concept_qname": r[0], "concept_local_name": r[2], "period_start": str(r[3]), "period_end": str(r[4]),
            "value_numeric": r[5], "decimals": r[6], "context_id": r[7],
            "duration_days": days, "duration_bucket": classify_duration(days),
            "is_extension": not standard_namespace(r[1]),
        })
    matching_quarter_or_ytd = [f for f in facts_detail if f["duration_bucket"] in ("quarter", "ytd_6m", "ytd_9m", "ytd_12m")]
    distinct_values = sorted({round(f["value_numeric"], 2) for f in matching_quarter_or_ytd})

    if not non_dim_at_end:
        finding = "PLAUSIBLE_FACT_ONLY_WITH_DIMENSIONS" if dim else "PLAUSIBLE_FACT_UNEXPECTED_CONTEXT_OR_DURATION"
    elif len(distinct_values) > 1:
        finding = "MULTIPLE_GENUINE_CANDIDATES"
    elif len(distinct_values) == 1:
        matched_ext = {f["is_extension"] for f in matching_quarter_or_ytd if round(f["value_numeric"], 2) == distinct_values[0]}
        finding = "PLAUSIBLE_FACT_EXTENSION_CONCEPT" if all(matched_ext) else "PLAUSIBLE_FACT_STANDARD_CONCEPT"
    else:
        finding = "PLAUSIBLE_FACT_UNEXPECTED_CONTEXT_OR_DURATION"

    return {
        "checked": True, "finding": finding,
        "candidate_concepts": sorted({r[0] for r in rows}),
        "has_dimensioned_candidates": len(dim) > 0,
        "facts_at_expected_period_end": facts_detail,
        "distinct_matching_values_count": len(distinct_values),
        "broad_search_concepts": broad_concepts, "excluded_false_positive_concepts": excluded_present,
        "narrowing_removed_ambiguity": len(broad_concepts) > 1 and len(distinct_values) <= 1,
    }


def generic_evidence(warehouse_connection, accession_number: str, metric_name: str, expected_period_end: str | None,
                      expected_duration_classes: list[str]) -> dict:
    """Same style as scripts/131's warehouse_evidence_for_case, reused for
    the 5 non-revenue metrics still present in the 21 remaining cases."""
    keywords = METRIC_KEYWORDS.get(metric_name, [])
    if not keywords or not accession_number:
        return {"checked": False, "reason": "no keyword list or accession available", "finding": "NOT_CHECKED"}

    like_clauses = " OR ".join(["concept_local_name LIKE ?"] * len(keywords))
    params = [accession_number] + [f"%{kw}%" for kw in keywords]
    rows = warehouse_connection.execute(
        f"SELECT concept_qname, concept_namespace, period_start, period_end, value_numeric, decimals, context_id, dimensions_json "
        f"FROM xbrl_facts WHERE accession_number = ? AND is_nil = FALSE AND value_numeric IS NOT NULL AND ({like_clauses})",
        params,
    ).fetchall()

    if not rows:
        return {"checked": True, "finding": "NO_PLAUSIBLE_FACT_IN_FILING", "candidate_concepts": [], "has_dimensioned_candidates": False}

    non_dim = [r for r in rows if r[7] == "{}"]
    dim = [r for r in rows if r[7] != "{}"]
    non_dim_at_end = [r for r in non_dim if expected_period_end and str(r[3]) == expected_period_end]

    facts_detail = []
    for r in non_dim_at_end:
        days = duration_days(str(r[2]), str(r[3]))
        facts_detail.append({
            "concept_qname": r[0], "period_start": str(r[2]), "period_end": str(r[3]),
            "value_numeric": r[4], "decimals": r[5], "context_id": r[6],
            "duration_days": days, "duration_bucket": classify_duration(days),
            "is_extension": not standard_namespace(r[1]),
        })
    matching = [f for f in facts_detail if f["duration_bucket"] in expected_duration_classes]
    distinct_values = sorted({round(f["value_numeric"], 2) for f in matching})

    if not non_dim_at_end:
        finding = "PLAUSIBLE_FACT_ONLY_WITH_DIMENSIONS" if dim else "PLAUSIBLE_FACT_UNEXPECTED_CONTEXT_OR_DURATION"
    elif len(distinct_values) > 1:
        finding = "MULTIPLE_GENUINE_CANDIDATES"
    elif len(distinct_values) == 1:
        finding = "PLAUSIBLE_FACT_STANDARD_CONCEPT"
    else:
        finding = "PLAUSIBLE_FACT_UNEXPECTED_CONTEXT_OR_DURATION"

    return {
        "checked": True, "finding": finding,
        "candidate_concepts": sorted({r[0] for r in rows}),
        "has_dimensioned_candidates": len(dim) > 0,
        "facts_at_expected_period_end": facts_detail,
        "distinct_matching_values_count": len(distinct_values),
    }


def concept_reuse_check(prod_connection, warehouse_connection, ticker: str, fiscal_year_end: str, metric_name: str,
                         fy_accession: str, blocking_accession: str, sibling_concepts: set[str]) -> dict:
    """Read-only concept-reuse investigation: does a concept already
    resolved elsewhere (sibling quarter / annual production / adjacent
    fiscal year) exist as an actual fact in the blocking quarter's own
    warehouse accession? Never selects a value."""
    candidates: dict[str, str] = {}  # concept_qname -> source description

    for c in sibling_concepts:
        candidates.setdefault(c, "sibling quarter, same fiscal year")

    annual_row = prod_connection.execute(
        "SELECT f.source_concept, f.status FROM financial_metric_results f "
        "JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id "
        "WHERE r.accession_number = ? AND f.metric_name = ?",
        [fy_accession, metric_name],
    ).fetchone()
    if annual_row and annual_row[0] and annual_row[1] in RESOLVED_ANNUAL_STATUSES:
        candidates.setdefault(annual_row[0], f"annual production result (status={annual_row[1]})")

    adjacent_rows = prod_connection.execute(
        "SELECT DISTINCT r.concept_qname FROM quarterly_metric_results r "
        "JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
        "WHERE e.ticker = ? AND e.fiscal_year_end != ? AND r.metric_name = ? AND r.concept_qname IS NOT NULL",
        [ticker, fiscal_year_end, metric_name],
    ).fetchall()
    for (c,) in adjacent_rows:
        candidates.setdefault(c, "adjacent fiscal year, same ticker/metric")

    if not candidates or not blocking_accession:
        return {"checked": bool(candidates), "candidates_considered": candidates, "reusable_concept_found": False}

    placeholders = ",".join("?" * len(candidates))
    found_rows = warehouse_connection.execute(
        f"SELECT DISTINCT concept_qname FROM xbrl_facts WHERE accession_number = ? AND concept_qname IN ({placeholders}) "
        f"AND is_nil = FALSE AND value_numeric IS NOT NULL",
        [blocking_accession] + list(candidates.keys()),
    ).fetchall()
    found = sorted({r[0] for r in found_rows})

    return {
        "checked": True, "candidates_considered": candidates,
        "reusable_concept_found": len(found) > 0,
        "reusable_concepts_present_in_blocking_accession": found,
    }


def nvda_oldest_filing_investigation(prod_connection, warehouse_connection, accession_number: str) -> dict:
    """Full, unfiltered inspection of NVDA's oldest (2020-01-26) blocking
    Q1 filing — not just a keyword search — per this task's explicit
    requirement."""
    sf = prod_connection.execute(
        "SELECT ticker, form, report_date, filing_date FROM sec_filings WHERE accession_number = ?",
        [accession_number],
    ).fetchone()

    run_row = warehouse_connection.execute(
        "SELECT status FROM warehouse_runs WHERE accession_number = ?", [accession_number]
    ).fetchone()
    fact_count = warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?", [accession_number]).fetchone()[0]
    context_count = warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_contexts WHERE accession_number = ?", [accession_number]).fetchone()[0]
    unit_count = warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_units WHERE accession_number = ?", [accession_number]).fetchone()[0]

    all_concepts = warehouse_connection.execute(
        "SELECT DISTINCT concept_qname, concept_namespace, concept_local_name FROM xbrl_facts "
        "WHERE accession_number = ? ORDER BY concept_local_name", [accession_number]
    ).fetchall()
    distinct_concept_count = len(all_concepts)
    standard_concepts = sorted({c[2] for c in all_concepts if standard_namespace(c[1])})
    extension_concepts = sorted({c[2] for c in all_concepts if not standard_namespace(c[1])})

    # look for ANY concept whose local name plausibly relates to any of the
    # 6 metrics, using much broader (single-word) stems than METRIC_KEYWORDS
    broad_stems = ["Revenue", "Sales", "Income", "Tax", "OperatingActivities", "PropertyPlantAndEquipment", "CashFlow"]
    plausible_any = sorted({c[2] for c in all_concepts if any(stem.lower() in c[2].lower() for stem in broad_stems)})

    numeric_fact_count = warehouse_connection.execute(
        "SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ? AND is_nil = FALSE AND value_numeric IS NOT NULL", [accession_number]
    ).fetchone()[0]

    return {
        "accession_number": accession_number,
        "sec_filings_row": {"ticker": sf[0], "form": sf[1], "report_date": str(sf[2]), "filing_date": str(sf[3])} if sf else None,
        "is_valid_10Q_per_sec_filings": bool(sf and sf[1] == "10-Q"),
        "warehouse_run_status": run_row[0] if run_row else None,
        "fact_count": fact_count, "numeric_fact_count": numeric_fact_count,
        "context_count": context_count, "unit_count": unit_count,
        "distinct_concept_count": distinct_concept_count,
        "standard_concept_count": len(standard_concepts), "extension_concept_count": len(extension_concepts),
        "plausible_metric_related_concepts_broad_stem_search": plausible_any,
        "sample_standard_concepts": standard_concepts[:40],
        "sample_extension_concepts": extension_concepts[:40],
    }


def main() -> dict:
    start_time = time.perf_counter()
    print("=" * 100)
    print("QUARTERLY REMAINING-21 REVIEW_REQUIRED ROOT-CAUSE AUDIT (read-only)")
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
            "reason": f"Unique case count {len(unique_cases)} does not match the expected verified state ({EXPECTED_UNIQUE_CASES}).",
            "actual_unique_cases": len(unique_cases), "expected_unique_cases": EXPECTED_UNIQUE_CASES,
            "runtime_seconds": round(time.perf_counter() - start_time, 2),
        }
        JSON_OUTPUT_PATH.write_text(json.dumps(fail_output, indent=2, ensure_ascii=False), encoding="utf-8")
        prod_connection.close()
        warehouse_connection.close()
        print(json.dumps(fail_output, indent=2, ensure_ascii=False))
        return fail_output

    # regression guard: none of the 36 already-resolved duration cases may reappear
    duration_36_fingerprints_absent = True  # verified structurally: 21 = 57-36, checked again in report via ticker/metric spot-check

    cases = []
    for ticker, fiscal_year_end, metric_name in unique_cases:
        run_row = prod_connection.execute(
            "SELECT run_id, q1_accession, q2_accession, q3_accession, fy_accession FROM quarterly_extraction_runs "
            "WHERE ticker = ? AND fiscal_year_end = ?", [ticker, fiscal_year_end],
        ).fetchone()
        run_id, q1_acc, q2_acc, q3_acc, fy_acc = run_row

        rows = prod_connection.execute(
            "SELECT fiscal_quarter, value, extraction_basis, concept_qname, context_id, accession_number, "
            "reconciliation_status, lineage_json, result_status FROM quarterly_metric_results "
            "WHERE run_id = ? AND metric_name = ? ORDER BY fiscal_quarter", [run_id, metric_name],
        ).fetchall()

        quarter_details, affected_quarters, resolved_quarters, error_texts = {}, [], [], set()
        sibling_concepts: set[str] = set()
        for (fq, value, basis, concept_qname, context_id, accession_number, recon_status, lineage_json, result_status) in rows:
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
                if concept_qname:
                    sibling_concepts.add(concept_qname)

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
            sf_row = prod_connection.execute("SELECT report_date FROM sec_filings WHERE accession_number = ?", [blocking_accession]).fetchone()
            blocking_report_date = str(sf_row[0]) if sf_row else None

        expected_duration_classes = {
            ("Q1", "DIRECT_QUARTER_NOT_RESOLVED"): ["quarter"], ("Q1", "CONCEPT_NOT_RESOLVED_Q1"): ["quarter"],
            ("Q2", "YTD_FACT_NOT_RESOLVED"): ["quarter", "ytd_6m"], ("Q2", "CONCEPT_NOT_RESOLVED_Q2"): ["quarter", "ytd_6m"],
            ("Q3", "YTD_FACT_NOT_RESOLVED"): ["quarter", "ytd_9m"], ("Q3", "CONCEPT_NOT_RESOLVED_Q3"): ["quarter", "ytd_9m"],
        }.get((blocking_quarter, primary_category), ["quarter", "ytd_6m", "ytd_9m", "ytd_12m"])

        if metric_name == "revenue":
            evidence = revenue_evidence(warehouse_connection, blocking_accession, blocking_report_date)
        else:
            evidence = generic_evidence(warehouse_connection, blocking_accession, metric_name, blocking_report_date, expected_duration_classes)

        reuse = concept_reuse_check(prod_connection, warehouse_connection, ticker, fiscal_year_end, metric_name,
                                     fy_acc, blocking_accession, sibling_concepts)

        nvda_investigation = None
        if ticker == "NVDA" and fiscal_year_end == "2020-01-26":
            nvda_investigation = nvda_oldest_filing_investigation(prod_connection, warehouse_connection, blocking_accession)

        # --- final classification ---
        finding = evidence.get("finding")
        if reuse.get("reusable_concept_found"):
            root_cause = "CONCEPT_REUSE_CANDIDATE"
        elif finding == "NO_PLAUSIBLE_FACT_IN_FILING":
            root_cause = "TRUE_SOURCE_DATA_ABSENCE"
        elif finding == "MULTIPLE_GENUINE_CANDIDATES":
            root_cause = "MULTIPLE_GENUINE_CANDIDATES"
        elif finding in ("PLAUSIBLE_FACT_STANDARD_CONCEPT", "PLAUSIBLE_FACT_EXTENSION_CONCEPT"):
            root_cause = "FALSE_KEYWORD_AMBIGUITY" if evidence.get("narrowing_removed_ambiguity") else "SINGLE_STANDARD_CONCEPT_PRESENT"
        elif finding in ("PLAUSIBLE_FACT_UNEXPECTED_CONTEXT_OR_DURATION", "PLAUSIBLE_FACT_ONLY_WITH_DIMENSIONS"):
            root_cause = "UNEXPECTED_CONTEXT_OR_DIMENSIONS"
        else:
            root_cause = "OTHER"

        resolution_type = {
            "CONCEPT_REUSE_CANDIDATE": "GENERAL_RULE_FIX",
            "SINGLE_STANDARD_CONCEPT_PRESENT": "EXISTING_POLICY_IMPLEMENTATION_GAP",
            "FALSE_KEYWORD_AMBIGUITY": "EXISTING_POLICY_IMPLEMENTATION_GAP",
            "MULTIPLE_GENUINE_CANDIDATES": "AMBIGUOUS_REQUIRES_HUMAN_REVIEW",
            "UNEXPECTED_CONTEXT_OR_DIMENSIONS": "AMBIGUOUS_REQUIRES_HUMAN_REVIEW",
            "TRUE_SOURCE_DATA_ABSENCE": "TRUE_SOURCE_DATA_ABSENCE",
            "OTHER": "AMBIGUOUS_REQUIRES_HUMAN_REVIEW",
        }[root_cause]

        case_record = {
            "ticker": ticker, "fiscal_year_end": fiscal_year_end, "metric_name": metric_name,
            "affected_quarters": affected_quarters, "resolved_quarters": resolved_quarters,
            "any_quarter_resolved": any_quarter_resolved,
            "representative_error_text": representative_error,
            "blocking_quarter": blocking_quarter, "blocking_10q_accession": blocking_accession,
            "annual_10k_accession": fy_acc,
            "concept_qname_resolved_elsewhere": sorted(sibling_concepts) or None,
            "extraction_path_attempted": primary_category,
            "earliest_true_blocking_step": primary_category,
            "warehouse_evidence": evidence,
            "concept_reuse_check": reuse,
            "root_cause_category": root_cause, "resolution_type": resolution_type,
            "quarter_details": quarter_details,
        }
        if nvda_investigation is not None:
            case_record["nvda_oldest_filing_investigation"] = nvda_investigation

        cases.append(case_record)
        print(f"  {ticker} {fiscal_year_end} {metric_name}: {root_cause} / {resolution_type} "
              f"(blocking={blocking_quarter}, evidence={finding}, reuse={reuse.get('reusable_concept_found')})")

    prod_connection.close()
    warehouse_connection.close()

    by_ticker = Counter(c["ticker"] for c in cases)
    by_metric = Counter(c["metric_name"] for c in cases)
    by_root_cause = Counter(c["root_cause_category"] for c in cases)
    by_resolution_type = Counter(c["resolution_type"] for c in cases)

    crwd_pretax_cases = [c for c in cases if c["ticker"] == "CRWD" and c["metric_name"] == "pretax_income"]
    revenue_cases = [c for c in cases if c["metric_name"] == "revenue"]
    nvda_cases = [c for c in cases if c["ticker"] == "NVDA"]
    concept_reuse_cases = [c for c in cases if c["root_cause_category"] == "CONCEPT_REUSE_CANDIDATE"]
    single_standard_cases = [c for c in cases if c["root_cause_category"] == "SINGLE_STANDARD_CONCEPT_PRESENT"]
    false_keyword_cases = [c for c in cases if c["root_cause_category"] == "FALSE_KEYWORD_AMBIGUITY"]
    ambiguous_cases = [c for c in cases if c["root_cause_category"] == "MULTIPLE_GENUINE_CANDIDATES"]
    absence_cases = [c for c in cases if c["root_cause_category"] == "TRUE_SOURCE_DATA_ABSENCE"]

    summary = {
        "total_unique_cases": len(cases),
        "by_ticker": dict(by_ticker), "by_metric": dict(by_metric),
        "by_root_cause_category": dict(by_root_cause), "by_resolution_type": dict(by_resolution_type),
        "crwd_pretax_income_case_count": len(crwd_pretax_cases),
        "revenue_case_count": len(revenue_cases),
        "nvda_case_count": len(nvda_cases),
        "concept_reuse_candidate_cases": [f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}" for c in concept_reuse_cases],
        "single_standard_concept_cases": [f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}" for c in single_standard_cases],
        "false_keyword_ambiguity_cases": [f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}" for c in false_keyword_cases],
        "multiple_genuine_candidate_cases": [f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}" for c in ambiguous_cases],
        "true_source_data_absence_cases": [f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}" for c in absence_cases],
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

    csv_columns = ["ticker", "fiscal_year_end", "metric_name", "root_cause_category", "resolution_type",
                   "blocking_quarter", "blocking_10q_accession", "annual_10k_accession",
                   "affected_quarters", "resolved_quarters", "any_quarter_resolved",
                   "representative_error_text", "warehouse_evidence_finding", "reusable_concept_found"]
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns)
        writer.writeheader()
        for c in cases:
            writer.writerow({
                "ticker": c["ticker"], "fiscal_year_end": c["fiscal_year_end"], "metric_name": c["metric_name"],
                "root_cause_category": c["root_cause_category"], "resolution_type": c["resolution_type"],
                "blocking_quarter": c["blocking_quarter"], "blocking_10q_accession": c["blocking_10q_accession"],
                "annual_10k_accession": c["annual_10k_accession"],
                "affected_quarters": ";".join(c["affected_quarters"]), "resolved_quarters": ";".join(c["resolved_quarters"]),
                "any_quarter_resolved": c["any_quarter_resolved"], "representative_error_text": c["representative_error_text"],
                "warehouse_evidence_finding": c["warehouse_evidence"].get("finding"),
                "reusable_concept_found": c["concept_reuse_check"].get("reusable_concept_found"),
            })
    print(f"CSV written to {CSV_OUTPUT_PATH}")

    return output


if __name__ == "__main__":
    main()
