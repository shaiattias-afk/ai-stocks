"""
Validation harness for scripts/128_quarterly_extraction_engine_v2.py
(reused unmodified). Two phases, both read-only against production/
warehouse and writing only to the two designated output files — never to
quarterly_extraction_runs, quarterly_metric_results, financial_metric_results,
or the XBRL warehouse.

VALIDATION 1 — baseline regression: MSFT/AMZN/ORCL FY2024 (72 rows), the
already fully-resolved 3-company baseline, compared field-by-field against
the previously verified scripts/118 outputs
(data/quarterly_proof_{ticker}_fy2024.json). Any financial-result
difference (value, extraction_basis, reconciliation status, availability
date) is a hard stop — the 54-case validation never runs.

VALIDATION 2 — all 54 ANNUAL_ROW_NOT_RESOLVED cases, read directly (not
hardcoded) from data/quarterly_review_required_audit.json, run through v2,
with full before/after detail recorded for every metric-year.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

AUDIT_PATH = DATA_DIR / "quarterly_review_required_audit.json"
VALIDATION_JSON_PATH = DATA_DIR / "quarterly_annual_anchor_v2_validation.json"
VALIDATION_CSV_PATH = DATA_DIR / "quarterly_annual_anchor_v2_validation.csv"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_DIR / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


s128 = _load_module("s128", "128_quarterly_extraction_engine_v2.py")

BASELINE_COMPANIES = {
    "MSFT": {"fiscal_year_end": "2024-06-30", "q1": "0000950170-23-054855", "q2": "0000950170-24-008814",
              "q3": "0000950170-24-048288", "fy": "0000950170-24-087843",
              "original_json": DATA_DIR / "quarterly_proof_msft_fy2024.json"},
    "AMZN": {"fiscal_year_end": "2024-12-31", "q1": "0001018724-24-000083", "q2": "0001018724-24-000130",
              "q3": "0001018724-24-000161", "fy": "0001018724-25-000004",
              "original_json": DATA_DIR / "quarterly_proof_amzn_fy2024.json"},
    "ORCL": {"fiscal_year_end": "2024-05-31", "q1": "0000950170-23-047713", "q2": "0000950170-23-069682",
              "q3": "0000950170-24-029904", "fy": "0000950170-24-075605",
              "original_json": DATA_DIR / "quarterly_proof_orcl_fy2024.json"},
}
METRICS = s128.METRICS
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]


def values_close(a, b, tol=1.0) -> bool:
    if a is None or b is None:
        return a == b
    return math.isclose(float(a), float(b), abs_tol=tol)


def compare_v2_vs_original(ticker: str, v2_output: dict, original: dict) -> list[dict]:
    differences = []
    for metric_name in METRICS:
        v2_metric = v2_output["metrics"].get(metric_name, {})
        original_metric = original["metrics"].get(metric_name, {})

        if v2_metric.get("status") != original_metric.get("status"):
            differences.append({"ticker": ticker, "metric": metric_name, "field": "status",
                                 "v2": v2_metric.get("status"), "original": original_metric.get("status")})

        for quarter in QUARTERS:
            v2_q = v2_metric.get("quarters", {}).get(quarter, {})
            original_q = original_metric.get("quarters", {}).get(quarter, {})

            if not values_close(v2_q.get("value"), original_q.get("value")):
                differences.append({"ticker": ticker, "metric": metric_name, "quarter": quarter, "field": "value",
                                     "v2": v2_q.get("value"), "original": original_q.get("value")})
            if v2_q.get("extraction_basis") != original_q.get("extraction_basis"):
                differences.append({"ticker": ticker, "metric": metric_name, "quarter": quarter, "field": "extraction_basis",
                                     "v2": v2_q.get("extraction_basis"), "original": original_q.get("extraction_basis")})
            if v2_q.get("availability_date") != original_q.get("availability_date"):
                differences.append({"ticker": ticker, "metric": metric_name, "quarter": quarter, "field": "availability_date",
                                     "v2": v2_q.get("availability_date"), "original": original_q.get("availability_date")})
    return differences


def run_validation_1() -> dict:
    print("=" * 100)
    print("VALIDATION 1 — BASELINE REGRESSION (MSFT/AMZN/ORCL FY2024, 72 rows)")
    print("=" * 100)

    all_differences = []
    total_rows = 0
    per_company = {}

    for ticker, spec in BASELINE_COMPANIES.items():
        print(f"\n>>> {ticker} FY{spec['fiscal_year_end'][:4]} (v2) <<<")
        v2_json_path = DATA_DIR / f"quarterly_engine_v2_{ticker.lower()}_fy{spec['fiscal_year_end'][:4]}.json"
        v2_csv_path = DATA_DIR / f"quarterly_engine_v2_{ticker.lower()}_fy{spec['fiscal_year_end'][:4]}.csv"
        v2_output = s128.run_quarterly_extraction_engine_v2(
            ticker=ticker, fiscal_year_end=spec["fiscal_year_end"],
            q1_accession=spec["q1"], q2_accession=spec["q2"], q3_accession=spec["q3"], fy_accession=spec["fy"],
            json_output_path=v2_json_path, csv_output_path=v2_csv_path,
        )
        row_count = sum(len(v2_output["metrics"][m].get("quarters", {})) for m in METRICS)
        total_rows += row_count

        with spec["original_json"].open(encoding="utf-8") as handle:
            original = json.load(handle)
        differences = compare_v2_vs_original(ticker, v2_output, original)
        all_differences.extend(differences)

        # separately report annual-anchor-lineage additions (allowed, not a "difference")
        anchor_metadata_present = all(
            "annual_anchor_source" in v2_output["metrics"][m].get("annual_lineage", {})
            for m in METRICS if v2_output["metrics"][m].get("annual_lineage")
        )

        per_company[ticker] = {"row_count": row_count, "differences": differences,
                                "annual_anchor_metadata_present": anchor_metadata_present}
        print(f"{ticker}: {row_count} rows, {len(differences)} differences vs. original, "
              f"annual_anchor_metadata_present={anchor_metadata_present}")

    print(f"\nTOTAL ROWS: {total_rows} (expected 72)")
    print(f"TOTAL DIFFERENCES: {len(all_differences)} (expected 0)")

    return {"total_rows": total_rows, "expected_rows": 72, "rows_match": total_rows == 72,
            "total_differences": len(all_differences), "differences": all_differences, "per_company": per_company}


def load_54_target_cases() -> list[dict]:
    with AUDIT_PATH.open(encoding="utf-8") as handle:
        audit = json.load(handle)
    cases = [c for c in audit["cases"] if c["root_cause_category"] == "ANNUAL_ROW_NOT_RESOLVED"]
    return cases


def get_company_year_accessions(ticker: str, fiscal_year_end: str) -> dict | None:
    """Read-only lookup of the 4 accessions for a company-year from the
    already-committed quarterly_extraction_runs row (authoritative, not
    hardcoded)."""
    import duckdb
    connection = duckdb.connect(database=str(s128.PRODUCTION_DB_PATH), read_only=True)
    row = connection.execute(
        "SELECT q1_accession, q2_accession, q3_accession, fy_accession FROM quarterly_extraction_runs "
        "WHERE ticker = ? AND fiscal_year_end = ?", [ticker, fiscal_year_end],
    ).fetchone()
    connection.close()
    if row is None:
        return None
    return {"q1": row[0], "q2": row[1], "q3": row[2], "fy": row[3]}


def run_validation_2() -> dict:
    print("\n" + "=" * 100)
    print("VALIDATION 2 — ALL 54 ANNUAL_ROW_NOT_RESOLVED TARGET CASES")
    print("=" * 100)

    target_cases = load_54_target_cases()
    print(f"Loaded {len(target_cases)} target cases from {AUDIT_PATH} (expected 54)")

    unique_company_years = sorted({(c["ticker"], c["fiscal_year_end"]) for c in target_cases})
    print(f"Unique company-years among target cases: {len(unique_company_years)}")

    v2_outputs_by_company_year: dict[tuple, dict] = {}
    for ticker, fiscal_year_end in unique_company_years:
        accessions = get_company_year_accessions(ticker, fiscal_year_end)
        if accessions is None:
            print(f"  SKIP {ticker} {fiscal_year_end}: no committed quarterly_extraction_runs row found")
            continue
        print(f"\n>>> {ticker} FY(end={fiscal_year_end}) (v2) <<<")
        v2_json_path = DATA_DIR / f"quarterly_engine_v2_{ticker.lower()}_fy{fiscal_year_end[:4]}.json"
        v2_csv_path = DATA_DIR / f"quarterly_engine_v2_{ticker.lower()}_fy{fiscal_year_end[:4]}.csv"
        v2_output = s128.run_quarterly_extraction_engine_v2(
            ticker=ticker, fiscal_year_end=fiscal_year_end,
            q1_accession=accessions["q1"], q2_accession=accessions["q2"],
            q3_accession=accessions["q3"], fy_accession=accessions["fy"],
            json_output_path=v2_json_path, csv_output_path=v2_csv_path,
        )
        v2_outputs_by_company_year[(ticker, fiscal_year_end)] = v2_output

    case_results = []
    status_counts = {"PASS": 0, "PASS_ROUNDING_TOLERANCE": 0, "REVIEW_REQUIRED": 0}

    for case in target_cases:
        ticker, fiscal_year_end, metric_name = case["ticker"], case["fiscal_year_end"], case["metric_name"]
        v2_output = v2_outputs_by_company_year.get((ticker, fiscal_year_end))
        if v2_output is None:
            case_results.append({"ticker": ticker, "fiscal_year_end": fiscal_year_end, "metric_name": metric_name,
                                  "old_status": "REVIEW_REQUIRED", "new_status": "NOT_RUN",
                                  "reason": "no committed quarterly_extraction_runs row found"})
            continue

        metric_result = v2_output["metrics"].get(metric_name, {})
        new_status = metric_result.get("status", "UNKNOWN")
        status_counts[new_status] = status_counts.get(new_status, 0) + 1

        annual_lineage = metric_result.get("annual_lineage", {})
        quarters = metric_result.get("quarters", {})
        reconciliation = metric_result.get("reconciliation", {})

        case_result = {
            "ticker": ticker, "fiscal_year_end": fiscal_year_end, "metric_name": metric_name,
            "fy_accession": v2_output["filings"]["FY"]["accession_number"],
            "old_status": "REVIEW_REQUIRED", "new_status": new_status,
            "annual_anchor_resolved": bool(annual_lineage),
            "annual_value": metric_result.get("annual_value"),
            "annual_unit": annual_lineage.get("unit_id"),
            "annual_status": annual_lineage.get("annual_anchor_status"),
            "annual_concept_qname": annual_lineage.get("concept_qname"),
            "annual_extraction_run_id": annual_lineage.get("annual_anchor_extraction_run_id"),
            "annual_anchor_accession_matches_fy": annual_lineage.get("annual_anchor_accession") == v2_output["filings"]["FY"]["accession_number"],
            "q1_value": quarters.get("Q1", {}).get("value"), "q1_basis": quarters.get("Q1", {}).get("extraction_basis"),
            "q2_value": quarters.get("Q2", {}).get("value"), "q2_basis": quarters.get("Q2", {}).get("extraction_basis"),
            "q3_value": quarters.get("Q3", {}).get("value"), "q3_basis": quarters.get("Q3", {}).get("extraction_basis"),
            "q4_value": quarters.get("Q4", {}).get("value"), "q4_basis": quarters.get("Q4", {}).get("extraction_basis"),
            "reconciliation_difference": reconciliation.get("difference"),
            "permitted_difference": reconciliation.get("precision_calculation", {}).get("permitted_difference"),
            "remaining_error": metric_result.get("error"),
            "annual_anchor_evidence": metric_result.get("annual_anchor_evidence"),
        }
        case_results.append(case_result)
        print(f"  {ticker} {fiscal_year_end} {metric_name}: REVIEW_REQUIRED -> {new_status}"
              + (f" (remaining: {metric_result.get('error')})" if new_status == "REVIEW_REQUIRED" else ""))

    print(f"\nStatus breakdown among the 54 target cases: {status_counts}")

    return {"target_case_count": len(target_cases), "expected_target_case_count": 54,
            "unique_company_years": len(unique_company_years), "status_counts": status_counts,
            "case_results": case_results}


def main() -> None:
    start = time.perf_counter()
    validation_1 = run_validation_1()

    if validation_1["total_differences"] > 0 or not validation_1["rows_match"]:
        print("\nBASELINE REGRESSION FAILED — stopping before the 54-case validation, per instructions.")
        result = {"overall_status": "FAIL", "validation_1": validation_1, "validation_2": None,
                  "runtime_seconds": round(time.perf_counter() - start, 2)}
        VALIDATION_JSON_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"\nResult written to {VALIDATION_JSON_PATH}")
        return

    print("\nBASELINE REGRESSION PASSED — proceeding to the 54-case validation.")
    validation_2 = run_validation_2()

    result = {
        "overall_status": "PASS", "validation_1": validation_1, "validation_2": validation_2,
        "runtime_seconds": round(time.perf_counter() - start, 2),
    }
    VALIDATION_JSON_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON result written to {VALIDATION_JSON_PATH}")

    import csv
    csv_columns = ["ticker", "fiscal_year_end", "metric_name", "fy_accession", "old_status", "new_status",
                   "annual_anchor_resolved", "annual_value", "annual_status", "annual_concept_qname",
                   "annual_extraction_run_id", "annual_anchor_accession_matches_fy",
                   "q1_value", "q1_basis", "q2_value", "q2_basis", "q3_value", "q3_basis", "q4_value", "q4_basis",
                   "reconciliation_difference", "permitted_difference", "remaining_error"]
    with VALIDATION_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns, extrasaction="ignore")
        writer.writeheader()
        for case in validation_2["case_results"]:
            writer.writerow(case)
    print(f"CSV result written to {VALIDATION_CSV_PATH}")

    print("\n" + "=" * 100)
    print(f"OVERALL: {result['overall_status']}")
    print(f"Baseline: {validation_1['total_rows']}/72 rows, {validation_1['total_differences']} differences")
    print(f"54-case status breakdown: {validation_2['status_counts']}")
    print(f"Runtime: {result['runtime_seconds']}s")
    print("=" * 100)


if __name__ == "__main__":
    main()
