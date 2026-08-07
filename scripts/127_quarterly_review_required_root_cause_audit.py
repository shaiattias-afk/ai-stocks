"""
Read-only root-cause audit of every unique (ticker, fiscal_year_end,
metric_name) quarterly metric-year case whose reconciliation_status is
REVIEW_REQUIRED across the completed 45-company-year Quarterly V1 dataset.

Opens the production database and the raw XBRL warehouse READ-ONLY.
Writes only two new files (data/quarterly_review_required_audit.json and
.csv) — no database row, schema, or extraction logic is touched.

Classification approach: every REVIEW_REQUIRED placeholder row's
lineage_json carries exactly one shared `error` string per metric-year
case (scripts/118 sets `metric_result["error"]` once, at the single point
where that metric's resolution first failed; every quarter still missing
after that point inherits the SAME string when scripts/123/124/126's
loader builds its honest NULL placeholder rows) — so classifying the one
distinct error string per case is equivalent to identifying "the earliest
true blocking fact" the task asks for, without extra bookkeeping. A case
where the error string differs from all known s118 templates, or where no
placeholder rows exist at all (i.e. every quarter has a real value but the
metric-year's *reconciliation* itself came back REVIEW_REQUIRED), is
handled as its own distinct category (RECONCILIATION_OUTSIDE_TOLERANCE).
"""

from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
ANNUAL_V1_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"

EXPECTED_UNIQUE_CASES = 111
JSON_OUTPUT_PATH = DATA_DIR / "quarterly_review_required_audit.json"
CSV_OUTPUT_PATH = DATA_DIR / "quarterly_review_required_audit.csv"

QUARTERS = ["Q1", "Q2", "Q3", "Q4"]

# Metric -> plausible alternate/standard concept keyword substrings, used
# ONLY for the read-only warehouse evidence check (never for selection).
METRIC_KEYWORDS = {
    "revenue": ["Revenue", "Sales", "SalesRevenue"],
    "operating_income": ["OperatingIncomeLoss"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
        "IncomeLossFromContinuingOperationsIncludingNoncontrollingInterestBeforeIncomeTaxesExtraordinaryItems",
        "IncomeLossBeforeIncomeTaxes", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForCapitalImprovements",
              "PaymentsToAcquireProductiveAssets", "PaymentsToAcquireOtherPropertyPlantAndEquipment"],
}

# --- known scripts/118 error-string templates -> root-cause category ---
# (regexes matched against the single shared lineage error string)
CLASSIFICATION_RULES = [
    (re.compile(r"^FY: row not found"), "ANNUAL_ROW_NOT_RESOLVED"),
    (re.compile(r"^annual \(FY\) 12-month value did not resolve"), "ANNUAL_ROW_NOT_RESOLVED"),
    (re.compile(r"^Q1: row not found"), "CONCEPT_NOT_RESOLVED"),
    (re.compile(r"^Q2: row not found"), "CONCEPT_NOT_RESOLVED"),
    (re.compile(r"^Q3: row not found"), "CONCEPT_NOT_RESOLVED"),
    (re.compile(r"^Q1 quarter-duration value did not resolve"), "DIRECT_QUARTER_NOT_RESOLVED"),
    (re.compile(r"^Q2: neither a direct quarter value nor a 6-month YTD"), "YTD_FACT_NOT_RESOLVED"),
    (re.compile(r"^Q3: neither a direct quarter value nor a 9-month YTD"), "YTD_FACT_NOT_RESOLVED"),
    (re.compile(r"^Q4 cannot be derived"), "CASCADING_DEPENDENCY_FAILURE"),
]


def classify_error_text(error_text: str) -> str:
    for pattern, category in CLASSIFICATION_RULES:
        if pattern.search(error_text):
            return category
    return "OTHER"


def warehouse_evidence_check(warehouse_connection, accession_number: str, metric_name: str, expected_period_end: str | None) -> dict:
    keywords = METRIC_KEYWORDS.get(metric_name, [])
    if not keywords or not accession_number:
        return {"checked": False, "reason": "no keyword list or accession available"}

    like_clauses = " OR ".join(["concept_local_name LIKE ?"] * len(keywords))
    params = [accession_number] + [f"%{kw}%" for kw in keywords]
    rows = warehouse_connection.execute(
        f"SELECT DISTINCT concept_qname, concept_namespace, period_end, period_type, is_nil "
        f"FROM xbrl_facts WHERE accession_number = ? AND dimensions_json = '{{}}' AND is_nil = FALSE "
        f"AND ({like_clauses})",
        params,
    ).fetchall()

    if not rows:
        return {"checked": True, "finding": "NO_PLAUSIBLE_FACT_IN_FILING", "candidate_concepts": []}

    standard_ns_pattern = re.compile(r"fasb\.org|xbrl\.sec\.gov|xbrl\.org|w3\.org", re.IGNORECASE)
    distinct_concepts = sorted({r[0] for r in rows})
    is_extension = {c: not bool(standard_ns_pattern.search(next(r[1] for r in rows if r[0] == c) or "")) for c in distinct_concepts}
    period_matches = [r for r in rows if expected_period_end and r[2] == expected_period_end]

    if len(distinct_concepts) > 1:
        finding = "MULTIPLE_PLAUSIBLE_FACTS_AMBIGUOUS"
    elif period_matches:
        finding = "PLAUSIBLE_FACT_STANDARD_CONCEPT" if not any(is_extension.values()) else "PLAUSIBLE_FACT_EXTENSION_CONCEPT"
    else:
        finding = "PLAUSIBLE_FACT_UNEXPECTED_CONTEXT_OR_DURATION"

    return {
        "checked": True, "finding": finding,
        "candidate_concepts": [{"concept_qname": c, "is_extension": is_extension[c]} for c in distinct_concepts],
        "period_end_matches": len(period_matches),
    }


def main() -> dict:
    start_time = time.perf_counter()
    print("=" * 100)
    print("QUARTERLY REVIEW_REQUIRED ROOT-CAUSE AUDIT (read-only)")
    print("=" * 100)

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    # --- authoritative unique-case count ---
    unique_cases = prod_connection.execute(
        "SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results "
        "WHERE reconciliation_status = 'REVIEW_REQUIRED' ORDER BY ticker, fiscal_year_end, metric_name"
    ).fetchall()
    print(f"Authoritative unique REVIEW_REQUIRED metric-year cases: {len(unique_cases)} (expected {EXPECTED_UNIQUE_CASES})")
    if len(unique_cases) != EXPECTED_UNIQUE_CASES:
        raise RuntimeError(
            f"Unique case count {len(unique_cases)} does not match the expected verified state "
            f"({EXPECTED_UNIQUE_CASES}). Refusing to continue on an unverified count."
        )

    cases = []
    for ticker, fiscal_year_end, metric_name in unique_cases:
        rows = prod_connection.execute(
            "SELECT r.fiscal_quarter, r.value, r.extraction_basis, r.concept_qname, r.context_id, "
            "r.accession_number, r.reconciliation_status, r.reconciliation_difference, r.permitted_difference, "
            "r.lineage_json, r.result_status "
            "FROM quarterly_metric_results r JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
            "WHERE e.ticker = ? AND e.fiscal_year_end = ? AND r.metric_name = ? ORDER BY r.fiscal_quarter",
            [ticker, fiscal_year_end, metric_name],
        ).fetchall()

        quarter_details = {}
        affected_quarters = []
        resolved_quarters = []
        error_texts = set()
        recon_diff = None
        permitted_diff = None
        fy_accession = None

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
            if fq == "Q4" or fq == "FY":
                fy_accession = accession_number if fq == "Q4" else fy_accession
            if is_unresolved:
                affected_quarters.append(fq)
                if lineage.get("error"):
                    error_texts.add(lineage["error"])
            else:
                resolved_quarters.append(fq)
            if reconciliation_difference is not None:
                recon_diff = reconciliation_difference
            if permitted_difference is not None:
                permitted_diff = permitted_difference

        any_quarter_resolved = len(resolved_quarters) > 0
        # the FY accession for warehouse lookups: prefer the Q4 row's accession_number
        # (which build_quarter_row_allowing_review_required always sets to the FY
        # accession for Q4, resolved or not)
        fy_accession = quarter_details.get("Q4", {}).get("accession_number")

        if error_texts:
            # all placeholder rows in a case share one error string by construction;
            # if more than one distinct string appears, report all but classify by the first
            representative_error = sorted(error_texts)[0]
            root_cause = classify_error_text(representative_error)
            is_cascading = root_cause == "CASCADING_DEPENDENCY_FAILURE"
        elif recon_diff is not None and permitted_diff is not None and abs(recon_diff) > permitted_diff:
            representative_error = f"reconciliation difference {recon_diff} exceeds permitted {permitted_diff}"
            root_cause = "RECONCILIATION_OUTSIDE_TOLERANCE"
            is_cascading = False
        else:
            representative_error = None
            root_cause = "OTHER"
            is_cascading = False

        # warehouse evidence check only for concept/row/context failure categories
        evidence = {"checked": False}
        if root_cause in ("ANNUAL_ROW_NOT_RESOLVED", "CONCEPT_NOT_RESOLVED", "DIRECT_QUARTER_NOT_RESOLVED",
                          "YTD_FACT_NOT_RESOLVED", "CONTEXT_OR_DURATION_NOT_RESOLVED") and fy_accession:
            expected_period_end = fiscal_year_end if root_cause == "ANNUAL_ROW_NOT_RESOLVED" else None
            evidence = warehouse_evidence_check(warehouse_connection, fy_accession, metric_name, expected_period_end)

        case_record = {
            "ticker": ticker, "fiscal_year_end": fiscal_year_end, "metric_name": metric_name,
            "affected_quarters": affected_quarters, "resolved_quarters": resolved_quarters,
            "any_quarter_resolved": any_quarter_resolved,
            "root_cause_category": root_cause, "is_cascading_within_metric": is_cascading,
            "representative_error_text": representative_error,
            "all_distinct_error_texts": sorted(error_texts) if error_texts else [],
            "reconciliation_difference": recon_diff, "permitted_difference": permitted_diff,
            "fy_accession": fy_accession, "quarter_details": quarter_details,
            "warehouse_evidence": evidence,
        }
        cases.append(case_record)
        print(f"  {ticker} {fiscal_year_end} {metric_name}: {root_cause} "
              f"(affected={affected_quarters}, resolved={resolved_quarters}, evidence={evidence.get('finding', 'n/a')})")

    prod_connection.close()
    warehouse_connection.close()

    # --- aggregate summaries ---
    from collections import Counter
    by_ticker = Counter(c["ticker"] for c in cases)
    by_fiscal_year = Counter(c["fiscal_year_end"][:4] for c in cases)
    by_metric = Counter(c["metric_name"] for c in cases)
    by_root_cause = Counter(c["root_cause_category"] for c in cases)
    primary_vs_cascading = Counter("cascading" if c["is_cascading_within_metric"] else "primary" for c in cases)
    plausible_fact_exists = sum(1 for c in cases if c["warehouse_evidence"].get("finding", "").startswith("PLAUSIBLE_FACT") or c["warehouse_evidence"].get("finding") == "MULTIPLE_PLAUSIBLE_FACTS_AMBIGUOUS")
    genuinely_absent = sum(1 for c in cases if c["warehouse_evidence"].get("finding") == "NO_PLAUSIBLE_FACT_IN_FILING")

    summary = {
        "total_unique_cases": len(cases),
        "by_ticker": dict(by_ticker), "by_fiscal_year": dict(by_fiscal_year), "by_metric": dict(by_metric),
        "by_root_cause_category": dict(by_root_cause), "primary_vs_cascading": dict(primary_vs_cascading),
        "cases_with_plausible_warehouse_fact": plausible_fact_exists,
        "cases_with_no_plausible_fact_in_filing": genuinely_absent,
        "cases_not_evidence_checked": len(cases) - plausible_fact_exists - genuinely_absent,
    }

    print("\n" + "=" * 100)
    print("SUMMARY")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("=" * 100)

    output = {"summary": summary, "cases": cases, "runtime_seconds": round(time.perf_counter() - start_time, 2)}
    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")

    csv_columns = ["ticker", "fiscal_year_end", "metric_name", "root_cause_category", "is_cascading_within_metric",
                   "affected_quarters", "resolved_quarters", "any_quarter_resolved", "representative_error_text",
                   "reconciliation_difference", "permitted_difference", "fy_accession", "warehouse_evidence_finding"]
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns)
        writer.writeheader()
        for c in cases:
            writer.writerow({
                "ticker": c["ticker"], "fiscal_year_end": c["fiscal_year_end"], "metric_name": c["metric_name"],
                "root_cause_category": c["root_cause_category"], "is_cascading_within_metric": c["is_cascading_within_metric"],
                "affected_quarters": ";".join(c["affected_quarters"]), "resolved_quarters": ";".join(c["resolved_quarters"]),
                "any_quarter_resolved": c["any_quarter_resolved"], "representative_error_text": c["representative_error_text"],
                "reconciliation_difference": c["reconciliation_difference"], "permitted_difference": c["permitted_difference"],
                "fy_accession": c["fy_accession"], "warehouse_evidence_finding": c["warehouse_evidence"].get("finding"),
            })
    print(f"CSV written to {CSV_OUTPUT_PATH}")

    return output


if __name__ == "__main__":
    main()
