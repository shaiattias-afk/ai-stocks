"""
Read-only validation of scripts/132 (quarterly extraction engine v3,
duration-tolerance fix) against scripts/128 (engine v2, the current
production engine for the annual-anchor mechanism).

Runs BOTH engines, in-memory, for all 45 authoritative company-years
(never touching quarterly_extraction_runs / quarterly_metric_results /
financial_metric_results / the warehouse's own tables — both DB
connections used by the engines are opened read_only=True, and this
script itself performs zero INSERT/UPDATE/DELETE/ALTER of any kind).

VALIDATION 1 — the 3 verified baselines (MSFT/AMZN/ORCL FY2024, 72 rows)
must be byte-identical between v2 and v3: fail-closed, stop before
Validation 2 if not.

VALIDATION 2 — full 45-company-year / 1,080-row regression: every
difference between v2 and v3 is classified as either the expected
resolution of one of the 38 audited CONTEXT_OR_DURATION_NOT_RESOLVED
cases (data/quarterly_remaining_57_audit.json ->
summary.off_by_one_day_duration_boundary_cases) or an unexpected change,
which fails the run.

Writes only data/quarterly_duration_v3_validation.json and .csv.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
SCRATCH_DIR = PROJECT_DIR / "data" / "_scratch_v3_validation"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
AUDIT_JSON_PATH = DATA_DIR / "quarterly_remaining_57_audit.json"

JSON_OUTPUT_PATH = DATA_DIR / "quarterly_duration_v3_validation.json"
CSV_OUTPUT_PATH = DATA_DIR / "quarterly_duration_v3_validation.csv"

METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense",
           "operating_cash_flow", "capex"]
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]
RESOLVED_STATUSES = {"PASS", "PASS_ROUNDING_TOLERANCE"}
BASELINE_COMPANY_YEARS = {("MSFT", "2024-06-30"), ("AMZN", "2024-12-31"), ("ORCL", "2024-05-31")}


def _load_module(filename: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, PROJECT_DIR / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


s128 = _load_module("128_quarterly_extraction_engine_v2.py", "s128_v2_engine")
s132 = _load_module("132_quarterly_extraction_engine_v3_duration_tolerance.py", "s132_v3_engine")


def load_company_years(prod_connection) -> list[dict]:
    rows = prod_connection.execute(
        "SELECT ticker, fiscal_year_end, q1_accession, q2_accession, q3_accession, fy_accession "
        "FROM quarterly_extraction_runs ORDER BY ticker, fiscal_year_end"
    ).fetchall()
    return [
        {"ticker": t, "fiscal_year_end": fy, "q1_accession": q1, "q2_accession": q2,
         "q3_accession": q3, "fy_accession": fya}
        for t, fy, q1, q2, q3, fya in rows
    ]


def load_audited_38_cases() -> set[str]:
    audit = json.loads(AUDIT_JSON_PATH.read_text(encoding="utf-8"))
    cases = audit["summary"]["off_by_one_day_duration_boundary_cases"]
    if len(cases) != 38:
        raise RuntimeError(f"Expected exactly 38 audited duration cases in {AUDIT_JSON_PATH}, found {len(cases)}. Refusing to proceed.")
    return set(cases)


def quarter_snapshot(metric_result: dict, quarter: str) -> dict:
    q = metric_result.get("quarters", {}).get(quarter)
    if q is None:
        return {"resolved": False, "value": None, "extraction_basis": None,
                "availability_date": None, "accession_number": None, "concept_qname": None,
                "duration_days": None}
    lineage = q.get("lineage", {})
    return {
        "resolved": True, "value": q.get("value"), "extraction_basis": q.get("extraction_basis"),
        "availability_date": q.get("availability_date"),
        "accession_number": lineage.get("accession_number", lineage.get("annual_accession_number")),
        "concept_qname": lineage.get("concept_qname", lineage.get("annual_concept_qname")),
        "duration_days": lineage.get("duration_days"),
    }


def values_equal(a, b) -> bool:
    if a is None or b is None:
        return a == b
    return abs(float(a) - float(b)) < 0.5


def main() -> dict:
    start_time = time.perf_counter()
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 100)
    print("QUARTERLY DURATION-TOLERANCE ENGINE V3 VALIDATION (read-only)")
    print("=" * 100)

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    company_years = load_company_years(prod_connection)
    prod_connection.close()

    if len(company_years) != 45:
        fail_output = {
            "status": "FAIL",
            "reason": f"Expected exactly 45 company-years in quarterly_extraction_runs, found {len(company_years)}.",
            "runtime_seconds": round(time.perf_counter() - start_time, 2),
        }
        JSON_OUTPUT_PATH.write_text(json.dumps(fail_output, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(fail_output, indent=2))
        return fail_output

    audited_38 = load_audited_38_cases()
    print(f"Loaded {len(company_years)} company-years; {len(audited_38)} audited duration-boundary cases.")

    quarter_rows: list[dict] = []
    case_rows: list[dict] = []
    unexpected_findings: list[dict] = []

    for spec in company_years:
        ticker, fiscal_year_end = spec["ticker"], spec["fiscal_year_end"]
        label = f"{ticker}_{fiscal_year_end.replace('-', '')}"
        v2_json = SCRATCH_DIR / f"{label}_v2.json"
        v2_csv = SCRATCH_DIR / f"{label}_v2.csv"
        v3_json = SCRATCH_DIR / f"{label}_v3.json"
        v3_csv = SCRATCH_DIR / f"{label}_v3.csv"

        v2_out = s128.run_quarterly_extraction_engine_v2(
            ticker=ticker, fiscal_year_end=fiscal_year_end,
            q1_accession=spec["q1_accession"], q2_accession=spec["q2_accession"],
            q3_accession=spec["q3_accession"], fy_accession=spec["fy_accession"],
            json_output_path=v2_json, csv_output_path=v2_csv,
        )
        v3_out = s132.run_quarterly_extraction_engine_v3(
            ticker=ticker, fiscal_year_end=fiscal_year_end,
            q1_accession=spec["q1_accession"], q2_accession=spec["q2_accession"],
            q3_accession=spec["q3_accession"], fy_accession=spec["fy_accession"],
            json_output_path=v3_json, csv_output_path=v3_csv,
        )

        is_baseline = (ticker, fiscal_year_end) in BASELINE_COMPANY_YEARS

        for metric_name in METRICS:
            v2_m = v2_out["metrics"][metric_name]
            v3_m = v3_out["metrics"][metric_name]
            v2_status = v2_m.get("status")
            v3_status = v3_m.get("status")
            case_key = f"{ticker} {fiscal_year_end} {metric_name}"
            is_audited = case_key in audited_38

            for quarter in QUARTERS:
                v2_q = quarter_snapshot(v2_m, quarter)
                v3_q = quarter_snapshot(v3_m, quarter)

                newly_resolved = (not v2_q["resolved"]) and v3_q["resolved"]
                newly_unresolved = v2_q["resolved"] and (not v3_q["resolved"])
                value_changed = v2_q["resolved"] and v3_q["resolved"] and not values_equal(v2_q["value"], v3_q["value"])
                basis_changed = v2_q["resolved"] and v3_q["resolved"] and v2_q["extraction_basis"] != v3_q["extraction_basis"]
                availability_changed = v2_q["resolved"] and v3_q["resolved"] and v2_q["availability_date"] != v3_q["availability_date"]
                accession_changed = v2_q["resolved"] and v3_q["resolved"] and v2_q["accession_number"] != v3_q["accession_number"]

                row = {
                    "ticker": ticker, "fiscal_year_end": fiscal_year_end, "metric_name": metric_name,
                    "fiscal_quarter": quarter, "is_audited_38_case": is_audited, "is_baseline": is_baseline,
                    "v2_resolved": v2_q["resolved"], "v2_value": v2_q["value"], "v2_extraction_basis": v2_q["extraction_basis"],
                    "v2_availability_date": v2_q["availability_date"], "v2_accession_number": v2_q["accession_number"],
                    "v3_resolved": v3_q["resolved"], "v3_value": v3_q["value"], "v3_extraction_basis": v3_q["extraction_basis"],
                    "v3_availability_date": v3_q["availability_date"], "v3_accession_number": v3_q["accession_number"],
                    "v3_duration_days": v3_q["duration_days"],
                    "newly_resolved": newly_resolved, "newly_unresolved": newly_unresolved,
                    "value_changed": value_changed, "basis_changed": basis_changed,
                    "availability_changed": availability_changed, "accession_changed": accession_changed,
                }
                quarter_rows.append(row)

                if newly_unresolved:
                    unexpected_findings.append({"level": "quarter", "type": "REGRESSION_PREVIOUSLY_RESOLVED_NOW_UNRESOLVED", "row": row})
                elif v2_q["resolved"] and v3_q["resolved"] and (value_changed or basis_changed or availability_changed or accession_changed):
                    if not is_audited:
                        unexpected_findings.append({"level": "quarter", "type": "UNEXPECTED_CHANGE_TO_ALREADY_RESOLVED_QUARTER", "row": row})
                elif newly_resolved and not is_audited:
                    unexpected_findings.append({"level": "quarter", "type": "UNEXPECTED_NEW_RESOLUTION_OUTSIDE_AUDITED_38", "row": row})
                elif newly_resolved and is_audited:
                    # extra safety checks the task requires explicitly
                    if quarter == "Q1" and v3_q["duration_days"] != 88:
                        unexpected_findings.append({"level": "quarter", "type": "UNEXPECTED_DURATION_FOR_RESOLVED_Q1_CASE", "row": row})
                    if quarter == "Q1" and ticker != "CRWD":
                        unexpected_findings.append({"level": "quarter", "type": "88_DAY_CASE_NOT_CRWD", "row": row})
                    if quarter == "Q2" and v3_q["extraction_basis"] == "DERIVED_FROM_YTD" and ticker not in ("GOOGL", "META"):
                        unexpected_findings.append({"level": "quarter", "type": "180_DAY_CASE_NOT_GOOGL_OR_META", "row": row})

            case_classification = "UNCHANGED"
            if v2_status == v3_status:
                case_classification = "UNCHANGED"
            elif is_audited and v2_status == "REVIEW_REQUIRED" and v3_status in RESOLVED_STATUSES:
                case_classification = "EXPECTED_RESOLUTION_OF_AUDITED_CASE"
            elif v2_status in RESOLVED_STATUSES and v3_status == "REVIEW_REQUIRED":
                case_classification = "REGRESSION_RESOLVED_TO_REVIEW_REQUIRED"
                unexpected_findings.append({"level": "case", "type": "REGRESSION_RESOLVED_TO_REVIEW_REQUIRED",
                                             "case": case_key, "v2_status": v2_status, "v3_status": v3_status})
            elif v2_status in RESOLVED_STATUSES and v3_status in RESOLVED_STATUSES:
                case_classification = "UNEXPECTED_STATUS_CHANGE_WITHIN_RESOLVED"
                unexpected_findings.append({"level": "case", "type": "UNEXPECTED_STATUS_CHANGE_WITHIN_RESOLVED",
                                             "case": case_key, "v2_status": v2_status, "v3_status": v3_status})
            elif not is_audited and v2_status == "REVIEW_REQUIRED" and v3_status in RESOLVED_STATUSES:
                case_classification = "UNEXPECTED_RESOLUTION_OUTSIDE_AUDITED_38"
                unexpected_findings.append({"level": "case", "type": "UNEXPECTED_RESOLUTION_OUTSIDE_AUDITED_38",
                                             "case": case_key, "v2_status": v2_status, "v3_status": v3_status})
            elif v2_status == "REVIEW_REQUIRED" and v3_status == "REVIEW_REQUIRED":
                case_classification = "STILL_REVIEW_REQUIRED"
            else:
                case_classification = "OTHER"
                unexpected_findings.append({"level": "case", "type": "OTHER_UNCLASSIFIED_STATUS_CHANGE",
                                             "case": case_key, "v2_status": v2_status, "v3_status": v3_status})

            v3_error = v3_m.get("error") if v3_status == "REVIEW_REQUIRED" else None
            case_rows.append({
                "ticker": ticker, "fiscal_year_end": fiscal_year_end, "metric_name": metric_name,
                "is_audited_38_case": is_audited, "is_baseline": is_baseline,
                "v2_status": v2_status, "v3_status": v3_status,
                "classification": case_classification, "v3_remaining_error": v3_error,
            })

        print(f"  {ticker} {fiscal_year_end}: done")

    # cleanup scratch (per-company engine JSON/CSV outputs are not required deliverables)
    for f in SCRATCH_DIR.glob("*"):
        f.unlink()
    SCRATCH_DIR.rmdir()

    # --- VALIDATION 1: the 3 verified baselines, byte-identical ---
    baseline_rows = [r for r in quarter_rows if r["is_baseline"]]
    baseline_diffs = [
        r for r in baseline_rows
        if r["v2_resolved"] != r["v3_resolved"] or r["value_changed"] or r["basis_changed"]
        or r["availability_changed"] or r["accession_changed"]
    ]
    validation_1 = {
        "expected_row_count": 72, "actual_row_count": len(baseline_rows),
        "row_count_ok": len(baseline_rows) == 72,
        "zero_differences": len(baseline_diffs) == 0,
        "differences": baseline_diffs,
        "status": "PASS" if (len(baseline_rows) == 72 and len(baseline_diffs) == 0) else "FAIL",
    }

    if validation_1["status"] == "FAIL":
        fail_output = {
            "status": "FAIL", "reason": "Validation 1 (MSFT/AMZN/ORCL FY2024 baseline) found differences between engine v2 and v3. Stopping before Validation 2 conclusions per task instruction.",
            "validation_1": validation_1,
            "runtime_seconds": round(time.perf_counter() - start_time, 2),
        }
        JSON_OUTPUT_PATH.write_text(json.dumps(fail_output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print("VALIDATION 1 FAILED — see", JSON_OUTPUT_PATH)
        return fail_output

    # --- VALIDATION 2: full 45-company-year / 1080-row regression ---
    audited_case_outcomes = {}
    for row in case_rows:
        key = f"{row['ticker']} {row['fiscal_year_end']} {row['metric_name']}"
        if key in audited_38:
            audited_case_outcomes[key] = row["v3_status"]

    outcome_counts = {"PASS": 0, "PASS_ROUNDING_TOLERANCE": 0, "REVIEW_REQUIRED": 0}
    still_review_required_detail = []
    for key, status in audited_case_outcomes.items():
        outcome_counts[status] = outcome_counts.get(status, 0) + 1
        if status == "REVIEW_REQUIRED":
            matching_case_row = next(r for r in case_rows if f"{r['ticker']} {r['fiscal_year_end']} {r['metric_name']}" == key)
            still_review_required_detail.append({"case": key, "reason": matching_case_row["v3_remaining_error"]})

    regressions = [f for f in unexpected_findings if "REGRESSION" in f["type"]]
    other_unexpected = [f for f in unexpected_findings if "REGRESSION" not in f["type"]]

    validation_2 = {
        "total_quarterly_rows": len(quarter_rows), "expected_quarterly_rows": 1080,
        "row_count_ok": len(quarter_rows) == 1080,
        "audited_38_case_outcomes": outcome_counts,
        "audited_38_cases_still_review_required": still_review_required_detail,
        "regressions_found": len(regressions), "regression_findings": regressions,
        "other_unexpected_findings_count": len(other_unexpected), "other_unexpected_findings": other_unexpected,
        "success_requirements_met": len(regressions) == 0 and len(other_unexpected) == 0,
    }

    overall_status = "PASS" if (validation_1["status"] == "PASS" and validation_2["success_requirements_met"]) else "FAIL"

    output = {
        "status": overall_status,
        "old_duration_boundaries": {"QUARTER_DURATION_MIN_DAYS": 89, "YTD_6M_MIN_DAYS": 181},
        "new_duration_boundaries": {"QUARTER_DURATION_MIN_DAYS": 88, "YTD_6M_MIN_DAYS": 180},
        "validation_1": validation_1, "validation_2": validation_2,
        "case_rows": case_rows,
        "runtime_seconds": round(time.perf_counter() - start_time, 2),
    }

    print("\n" + "=" * 100)
    print("SUMMARY")
    print(f"Validation 1 (baseline 72 rows): {validation_1['status']}")
    print(f"Validation 2 (1080 rows): row_count_ok={validation_2['row_count_ok']} "
          f"success_requirements_met={validation_2['success_requirements_met']}")
    print(f"38 audited cases -> {outcome_counts}")
    print(f"Regressions found: {len(regressions)}")
    print(f"Other unexpected findings: {len(other_unexpected)}")
    print(f"OVERALL STATUS: {overall_status}")
    print("=" * 100)

    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")

    csv_columns = [
        "ticker", "fiscal_year_end", "metric_name", "fiscal_quarter", "is_audited_38_case", "is_baseline",
        "v2_resolved", "v2_value", "v2_extraction_basis", "v3_resolved", "v3_value", "v3_extraction_basis",
        "v3_duration_days", "newly_resolved", "newly_unresolved", "value_changed", "basis_changed",
        "availability_changed", "accession_changed",
    ]
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns)
        writer.writeheader()
        for row in quarter_rows:
            writer.writerow({k: row[k] for k in csv_columns})
    print(f"CSV written to {CSV_OUTPUT_PATH} ({len(quarter_rows)} rows)")

    return output


if __name__ == "__main__":
    main()
