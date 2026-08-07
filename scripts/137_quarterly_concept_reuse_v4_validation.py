"""
Read-only validation of scripts/136 (quarterly extraction engine v4,
point-in-time-safe concept reuse) against scripts/132 (engine v3, the
current production duration-tolerance engine).

VALIDATION 1 — the 3 verified baselines (MSFT/AMZN/ORCL FY2024, 72 rows)
must be byte-identical between v3 and v4 in every financial field: fail-
closed, stop before Validation 2 if not. Also explicitly confirms the
concept-reuse fallback never activates for these already-resolved rows.

VALIDATION 2 — the exact 15 CONCEPT_REUSE_CANDIDATE cases, derived (not
hardcoded) from data/quarterly_remaining_21_audit.json (scripts/135's
saved output). Runs v3 and v4 fresh, in-memory, for only the affected
unique company-years (13), and classifies every difference.

Neither database is ever opened for writing; only scripts/132 and
scripts/136's own engine functions are called (never scripts/133/135,
never the full 45-company regression). Writes only
data/quarterly_concept_reuse_v4_validation.json and .csv.
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
SCRATCH_DIR = PROJECT_DIR / "data" / "_scratch_v4_validation"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
AUDIT_JSON_PATH = DATA_DIR / "quarterly_remaining_21_audit.json"

JSON_OUTPUT_PATH = DATA_DIR / "quarterly_concept_reuse_v4_validation.json"
CSV_OUTPUT_PATH = DATA_DIR / "quarterly_concept_reuse_v4_validation.csv"

METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense",
           "operating_cash_flow", "capex"]
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]
RESOLVED_STATUSES = {"PASS", "PASS_ROUNDING_TOLERANCE"}
BASELINE_COMPANY_YEARS = {("MSFT", "2024-06-30"), ("AMZN", "2024-12-31"), ("ORCL", "2024-05-31")}

EXPECTED_TOTAL_TARGET_CASES = 15
EXPECTED_FAMILY_COUNTS = {
    ("CRWD", "pretax_income"): 2, ("META", "revenue"): 3, ("MU", "pretax_income"): 3,
    ("PANW", "pretax_income"): 2, ("PANW", "revenue"): 5,
}


def _load_module(filename: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, PROJECT_DIR / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


s132 = _load_module("132_quarterly_extraction_engine_v3_duration_tolerance.py", "s132_v3_engine")
s136 = _load_module("136_quarterly_extraction_engine_v4_point_in_time_concept_reuse.py", "s136_v4_engine")


def load_company_years(prod_connection, tickers: set[str] | None = None) -> list[dict]:
    query = ("SELECT ticker, fiscal_year_end, q1_accession, q2_accession, q3_accession, fy_accession "
             "FROM quarterly_extraction_runs")
    params = []
    if tickers:
        placeholders = ",".join("?" * len(tickers))
        query += f" WHERE ticker IN ({placeholders})"
        params = list(tickers)
    query += " ORDER BY ticker, fiscal_year_end"
    rows = prod_connection.execute(query, params).fetchall()
    return [
        {"ticker": t, "fiscal_year_end": fy, "q1_accession": q1, "q2_accession": q2,
         "q3_accession": q3, "fy_accession": fya}
        for t, fy, q1, q2, q3, fya in rows
    ]


def load_target_cases() -> list[dict]:
    audit = json.loads(AUDIT_JSON_PATH.read_text(encoding="utf-8"))
    cases = [c for c in audit["cases"] if c["root_cause_category"] == "CONCEPT_REUSE_CANDIDATE"]
    if len(cases) != EXPECTED_TOTAL_TARGET_CASES:
        raise RuntimeError(f"Expected exactly {EXPECTED_TOTAL_TARGET_CASES} CONCEPT_REUSE_CANDIDATE cases in "
                            f"{AUDIT_JSON_PATH}, found {len(cases)}.")
    family_counts: dict[tuple, int] = {}
    for c in cases:
        key = (c["ticker"], c["metric_name"])
        family_counts[key] = family_counts.get(key, 0) + 1
    if family_counts != EXPECTED_FAMILY_COUNTS:
        raise RuntimeError(f"Derived family counts {family_counts} do not match expected {EXPECTED_FAMILY_COUNTS}.")
    return cases


def quarter_snapshot(metric_result: dict, quarter: str) -> dict:
    q = metric_result.get("quarters", {}).get(quarter)
    if q is None:
        return {"resolved": False, "value": None, "extraction_basis": None,
                "availability_date": None, "accession_number": None, "concept_qname": None}
    lineage = q.get("lineage", {})
    return {
        "resolved": True, "value": q.get("value"), "extraction_basis": q.get("extraction_basis"),
        "availability_date": q.get("availability_date"),
        "accession_number": lineage.get("accession_number", lineage.get("annual_accession_number")),
        "concept_qname": lineage.get("concept_qname", lineage.get("annual_concept_qname")),
        "context_id": lineage.get("context_id", lineage.get("nine_month_ytd_context_id")),
        "period_start": lineage.get("period_start"), "period_end": lineage.get("period_end"),
        "duration_days": lineage.get("duration_days"), "concept_source": lineage.get("concept_source"),
    }


def values_equal(a, b) -> bool:
    if a is None or b is None:
        return a == b
    return abs(float(a) - float(b)) < 0.5


def main() -> dict:
    start_time = time.perf_counter()
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 100)
    print("QUARTERLY CONCEPT-REUSE ENGINE V4 VALIDATION (read-only)")
    print("=" * 100)

    target_cases = load_target_cases()
    print(f"Loaded {len(target_cases)} target CONCEPT_REUSE_CANDIDATE cases from {AUDIT_JSON_PATH}")

    target_company_years = sorted({(c["ticker"], c["fiscal_year_end"]) for c in target_cases})
    print(f"Derived {len(target_company_years)} unique target company-years: {target_company_years}")

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    baseline_specs = load_company_years(prod_connection, {"MSFT", "AMZN", "ORCL"})
    baseline_specs = [s for s in baseline_specs if (s["ticker"], s["fiscal_year_end"]) in BASELINE_COMPANY_YEARS]
    target_specs = load_company_years(prod_connection, {t for t, _fy in target_company_years})
    target_specs = [s for s in target_specs if (s["ticker"], s["fiscal_year_end"]) in set(target_company_years)]
    prod_connection.close()

    if len(baseline_specs) != 3:
        raise RuntimeError(f"Expected exactly 3 baseline company-years, found {len(baseline_specs)}.")
    if len(target_specs) != len(target_company_years):
        raise RuntimeError(f"Expected {len(target_company_years)} target company-year specs, found {len(target_specs)}.")

    def run_both_engines(spec: dict) -> tuple[dict, dict]:
        ticker, fiscal_year_end = spec["ticker"], spec["fiscal_year_end"]
        label = f"{ticker}_{fiscal_year_end.replace('-', '')}"
        v3_json = SCRATCH_DIR / f"{label}_v3.json"
        v3_csv = SCRATCH_DIR / f"{label}_v3.csv"
        v4_json = SCRATCH_DIR / f"{label}_v4.json"
        v4_csv = SCRATCH_DIR / f"{label}_v4.csv"
        v3_out = s132.run_quarterly_extraction_engine_v3(
            ticker=ticker, fiscal_year_end=fiscal_year_end,
            q1_accession=spec["q1_accession"], q2_accession=spec["q2_accession"],
            q3_accession=spec["q3_accession"], fy_accession=spec["fy_accession"],
            json_output_path=v3_json, csv_output_path=v3_csv,
        )
        v4_out = s136.run_quarterly_extraction_engine_v4(
            ticker=ticker, fiscal_year_end=fiscal_year_end,
            q1_accession=spec["q1_accession"], q2_accession=spec["q2_accession"],
            q3_accession=spec["q3_accession"], fy_accession=spec["fy_accession"],
            json_output_path=v4_json, csv_output_path=v4_csv,
        )
        return v3_out, v4_out

    # =====================================================================
    # VALIDATION 1 — the 3 baselines
    # =====================================================================
    print("\n" + "=" * 100)
    print("VALIDATION 1 — MSFT/AMZN/ORCL FY2024 baseline")
    print("=" * 100)

    baseline_rows = []
    fallback_activated_on_baseline = []
    for spec in baseline_specs:
        v3_out, v4_out = run_both_engines(spec)
        ticker, fiscal_year_end = spec["ticker"], spec["fiscal_year_end"]
        for metric_name in METRICS:
            v3_m = v3_out["metrics"][metric_name]
            v4_m = v4_out["metrics"][metric_name]
            for label, src in v4_m.get("concept_source_lineage", {}).items():
                if src.get("source") == "POINT_IN_TIME_SAFE_CONCEPT_REUSE":
                    fallback_activated_on_baseline.append(f"{ticker} {fiscal_year_end} {metric_name}/{label}")
            for quarter in QUARTERS:
                v3_q = quarter_snapshot(v3_m, quarter)
                v4_q = quarter_snapshot(v4_m, quarter)
                differs = (
                    v3_q["resolved"] != v4_q["resolved"] or
                    not values_equal(v3_q["value"], v4_q["value"]) or
                    v3_q["extraction_basis"] != v4_q["extraction_basis"] or
                    v3_q["availability_date"] != v4_q["availability_date"] or
                    v3_q["accession_number"] != v4_q["accession_number"]
                )
                baseline_rows.append({
                    "ticker": ticker, "fiscal_year_end": fiscal_year_end, "metric_name": metric_name,
                    "fiscal_quarter": quarter, "v3_value": v3_q["value"], "v4_value": v4_q["value"],
                    "v3_extraction_basis": v3_q["extraction_basis"], "v4_extraction_basis": v4_q["extraction_basis"],
                    "differs": differs,
                })
        print(f"  {ticker} {fiscal_year_end}: done")

    baseline_diffs = [r for r in baseline_rows if r["differs"]]
    validation_1 = {
        "expected_row_count": 72, "actual_row_count": len(baseline_rows),
        "row_count_ok": len(baseline_rows) == 72,
        "zero_differences": len(baseline_diffs) == 0, "differences": baseline_diffs,
        "fallback_activated_on_baseline": fallback_activated_on_baseline,
        "fallback_correctly_inactive": len(fallback_activated_on_baseline) == 0,
        "status": "PASS" if (len(baseline_rows) == 72 and len(baseline_diffs) == 0 and len(fallback_activated_on_baseline) == 0) else "FAIL",
    }
    print(f"\nValidation 1: {validation_1['status']} (rows={len(baseline_rows)}, diffs={len(baseline_diffs)}, "
          f"fallback_activations={len(fallback_activated_on_baseline)})")

    if validation_1["status"] == "FAIL":
        for f in SCRATCH_DIR.glob("*"):
            f.unlink()
        SCRATCH_DIR.rmdir()
        fail_output = {
            "status": "FAIL",
            "reason": "Validation 1 (MSFT/AMZN/ORCL FY2024 baseline) found differences or fallback activation "
                       "between engine v3 and v4. Stopping before Validation 2 per task instruction.",
            "validation_1": validation_1, "runtime_seconds": round(time.perf_counter() - start_time, 2),
        }
        JSON_OUTPUT_PATH.write_text(json.dumps(fail_output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print("VALIDATION 1 FAILED — see", JSON_OUTPUT_PATH)
        return fail_output

    # =====================================================================
    # VALIDATION 2 — the 15 target cases (13 unique company-years)
    # =====================================================================
    print("\n" + "=" * 100)
    print("VALIDATION 2 — the 15 CONCEPT_REUSE_CANDIDATE target cases")
    print("=" * 100)

    target_case_keys = {f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}" for c in target_cases}

    case_results = []
    unexpected_findings = []
    regressions = []
    future_data_violations = []

    for spec in target_specs:
        v3_out, v4_out = run_both_engines(spec)
        ticker, fiscal_year_end = spec["ticker"], spec["fiscal_year_end"]

        for metric_name in METRICS:
            v3_m = v3_out["metrics"][metric_name]
            v4_m = v4_out["metrics"][metric_name]
            v3_status, v4_status = v3_m.get("status"), v4_m.get("status")
            case_key = f"{ticker} {fiscal_year_end} {metric_name}"
            is_target = case_key in target_case_keys

            # --- regression checks (apply to every case, target or not) ---
            for quarter in QUARTERS:
                v3_q = quarter_snapshot(v3_m, quarter)
                v4_q = quarter_snapshot(v4_m, quarter)
                if v3_q["resolved"] and not v4_q["resolved"]:
                    regressions.append({"case": case_key, "quarter": quarter, "type": "PREVIOUSLY_RESOLVED_NOW_UNRESOLVED"})
                elif v3_q["resolved"] and v4_q["resolved"]:
                    if not values_equal(v3_q["value"], v4_q["value"]):
                        regressions.append({"case": case_key, "quarter": quarter, "type": "VALUE_CHANGED",
                                             "v3_value": v3_q["value"], "v4_value": v4_q["value"]})
                    if v3_q["extraction_basis"] != v4_q["extraction_basis"]:
                        regressions.append({"case": case_key, "quarter": quarter, "type": "BASIS_CHANGED",
                                             "v3_basis": v3_q["extraction_basis"], "v4_basis": v4_q["extraction_basis"]})

            if v3_status in RESOLVED_STATUSES and v4_status == "REVIEW_REQUIRED":
                regressions.append({"case": case_key, "type": "CASE_BECAME_REVIEW_REQUIRED",
                                     "v3_status": v3_status, "v4_status": v4_status})

            if not is_target:
                if v3_status != v4_status:
                    unexpected_findings.append({"case": case_key, "v3_status": v3_status, "v4_status": v4_status,
                                                 "note": "non-target case changed status"})
                continue

            # --- per-target-case detail ---
            lineage_by_quarter = {}
            future_violation_here = False
            for quarter in ("Q1", "Q2", "Q3"):
                src = v4_m.get("concept_source_lineage", {}).get(quarter, {})
                lineage_by_quarter[quarter] = src
                if src.get("source") == "POINT_IN_TIME_SAFE_CONCEPT_REUSE":
                    source_filing_date = src.get("source_filing_date")
                    blocking_filing_date = src.get("blocking_filing_date")
                    if source_filing_date and blocking_filing_date and source_filing_date > blocking_filing_date:
                        future_violation_here = True
                        future_data_violations.append({"case": case_key, "quarter": quarter, "lineage": src})

            blocking_quarter = None
            for q_original_case in target_cases:
                if q_original_case["ticker"] == ticker and q_original_case["fiscal_year_end"] == fiscal_year_end and q_original_case["metric_name"] == metric_name:
                    blocking_quarter = q_original_case["blocking_quarter"]
                    break

            fact_selected = quarter_snapshot(v4_m, blocking_quarter) if blocking_quarter else None

            case_results.append({
                "ticker": ticker, "fiscal_year_end": fiscal_year_end, "metric_name": metric_name,
                "blocking_quarter": blocking_quarter,
                "old_status_v3": v3_status, "new_status_v4": v4_status,
                "reconciliation_difference": v4_m.get("reconciliation", {}).get("difference"),
                "permitted_difference": v4_m.get("reconciliation", {}).get("precision_calculation", {}).get("permitted_difference"),
                "concept_source_lineage": lineage_by_quarter,
                "fact_selected_from_blocking_accession": fact_selected,
                "future_data_violation": future_violation_here,
                "complete_v4_metric_result": v4_m,
            })
        print(f"  {ticker} {fiscal_year_end}: done")

    for f in SCRATCH_DIR.glob("*"):
        f.unlink()
    SCRATCH_DIR.rmdir()

    outcome_counts = {"PASS": 0, "PASS_ROUNDING_TOLERANCE": 0, "REVIEW_REQUIRED": 0}
    still_review_required_detail = []
    for cr in case_results:
        outcome_counts[cr["new_status_v4"]] = outcome_counts.get(cr["new_status_v4"], 0) + 1
        if cr["new_status_v4"] == "REVIEW_REQUIRED":
            still_review_required_detail.append({
                "case": f"{cr['ticker']} {cr['fiscal_year_end']} {cr['metric_name']}",
                "reason": cr["complete_v4_metric_result"].get("error"),
                "concept_reuse_attempted": cr["complete_v4_metric_result"].get("concept_reuse_attempted"),
            })

    validation_2 = {
        "target_case_count": len(target_cases), "unique_target_company_years": len(target_company_years),
        "outcome_counts": outcome_counts, "still_review_required_detail": still_review_required_detail,
        "regressions_found": len(regressions), "regression_findings": regressions,
        "unexpected_findings_count": len(unexpected_findings), "unexpected_findings": unexpected_findings,
        "future_data_violations_found": len(future_data_violations), "future_data_violations": future_data_violations,
        "success_requirements_met": len(regressions) == 0 and len(unexpected_findings) == 0 and len(future_data_violations) == 0,
    }

    overall_status = "PASS" if (validation_1["status"] == "PASS" and validation_2["success_requirements_met"]) else "FAIL"

    output = {
        "status": overall_status,
        "point_in_time_policy": {
            "priority_1": "earlier 10-Q, same fiscal year, same metric, already resolved earlier in the run",
            "priority_2_3": "walk back through progressively older 10-Ks for the same company, most recent first, "
                             "using the first whose authoritative annual production result for the metric is resolved",
            "forbidden": ["same fiscal year's own 10-K", "a later quarter in the same fiscal year", "a later fiscal year",
                          "any filing with filing_date after the blocking filing_date"],
        },
        "validation_1": validation_1, "validation_2": validation_2, "case_results": case_results,
        "runtime_seconds": round(time.perf_counter() - start_time, 2),
    }

    print("\n" + "=" * 100)
    print("SUMMARY")
    print(f"Validation 1: {validation_1['status']}")
    print(f"Validation 2: outcome_counts={outcome_counts} regressions={len(regressions)} "
          f"unexpected={len(unexpected_findings)} future_violations={len(future_data_violations)}")
    print(f"OVERALL STATUS: {overall_status}")
    print("=" * 100)

    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")

    csv_columns = ["ticker", "fiscal_year_end", "metric_name", "blocking_quarter", "old_status_v3", "new_status_v4",
                   "reconciliation_difference", "permitted_difference", "future_data_violation"]
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns)
        writer.writeheader()
        for cr in case_results:
            writer.writerow({k: cr[k] for k in csv_columns})
    print(f"CSV written to {CSV_OUTPUT_PATH} ({len(case_results)} rows)")

    return output


if __name__ == "__main__":
    main()
