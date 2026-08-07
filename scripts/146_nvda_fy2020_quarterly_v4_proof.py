"""
Read-only quarterly engine V4 proof for exactly NVDA fiscal-year-end
2020-01-26 — determines whether its 6 REVIEW_REQUIRED quarterly
metric-year cases now resolve after TASK_144 populated accession
0001045810-19-000079 in the production warehouse.

Opens both databases read_only=True. Runs scripts/136's engine function
directly (not scripts/137, not the 45-company regression) for this one
company-year only. Writes only the 4 files this task requires:
scripts/146 (this file), data/nvda_fy2020_quarterly_v4_proof.json/.csv,
docs/NVDA_FY2020_QUARTERLY_V4_PROOF.md. Never writes to either production
database. Does not load anything into production.
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
SCRATCH_DIR = DATA_DIR / "_scratch_nvda_fy2020_v4_proof"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"

JSON_OUTPUT_PATH = DATA_DIR / "nvda_fy2020_quarterly_v4_proof.json"
CSV_OUTPUT_PATH = DATA_DIR / "nvda_fy2020_quarterly_v4_proof.csv"

TICKER, FISCAL_YEAR_END = "NVDA", "2020-01-26"
EXPECTED_Q1_ACCESSION = "0001045810-19-000079"
EXPECTED_Q1_WAREHOUSE_FACTS = 654
EXPECTED_NVDA_REVIEW_REQUIRED_COUNT = 6
EXPECTED_TOTAL_REVIEW_REQUIRED = 10
EXPECTED_QUARTERLY_RUNS = 45
EXPECTED_QUARTERLY_ROWS = 1080
EXPECTED_FMR = 900

METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense", "operating_cash_flow", "capex"]
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]

EXPECTED_Q1_CONCEPTS = {
    "revenue": "us-gaap:Revenues",
    "operating_income": "us-gaap:OperatingIncomeLoss",
    "pretax_income": "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "income_tax_expense": "us-gaap:IncomeTaxExpenseBenefit",
    "operating_cash_flow": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    "capex": "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
}
EXPECTED_Q1_PERIOD_START, EXPECTED_Q1_PERIOD_END = "2019-01-28", "2019-04-28"

_spec136 = importlib.util.spec_from_file_location("s136", PROJECT_DIR / "scripts" / "136_quarterly_extraction_engine_v4_point_in_time_concept_reuse.py")
s136 = importlib.util.module_from_spec(_spec136)
sys.modules["s136"] = s136
_spec136.loader.exec_module(s136)


def write_fail_report(reason: str, detail: dict, runtime_seconds: float) -> dict:
    output = {"status": "FAIL", "reason": reason, "detail": detail, "runtime_seconds": runtime_seconds}
    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nFAIL — {reason}")
    print(json.dumps(detail, indent=2, ensure_ascii=False, default=str))
    return output


# =====================================================================
# PHASE 1 — PRECONDITIONS (read-only; engine not run if any fail)
# =====================================================================

def phase1_preconditions() -> dict:
    print("=" * 100)
    print("PHASE 1 — PRECONDITIONS")
    print("=" * 100)

    prod = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)

    quarterly_runs = prod.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0]
    quarterly_rows = prod.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
    fmr = prod.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    total_review_required = prod.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results WHERE reconciliation_status = 'REVIEW_REQUIRED')"
    ).fetchone()[0]
    if (quarterly_runs, quarterly_rows, fmr, total_review_required) != (EXPECTED_QUARTERLY_RUNS, EXPECTED_QUARTERLY_ROWS, EXPECTED_FMR, EXPECTED_TOTAL_REVIEW_REQUIRED):
        prod.close()
        raise RuntimeError(f"Global production counts do not match expected: runs={quarterly_runs}, rows={quarterly_rows}, "
                            f"fmr={fmr}, total_review_required={total_review_required}")

    run_row = prod.execute(
        "SELECT run_id, engine_version, q1_accession, q2_accession, q3_accession, fy_accession, run_status "
        "FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?", [TICKER, FISCAL_YEAR_END],
    ).fetchone()
    if run_row is None:
        prod.close()
        raise RuntimeError(f"No existing quarterly_extraction_runs row found for {TICKER} {FISCAL_YEAR_END}")
    old_run_id, old_engine_version, q1_acc, q2_acc, q3_acc, fy_acc, old_run_status = run_row
    if q1_acc != EXPECTED_Q1_ACCESSION:
        prod.close()
        raise RuntimeError(f"Q1 accession {q1_acc} != expected {EXPECTED_Q1_ACCESSION}")

    q1_filing = prod.execute("SELECT report_date, filing_date, form FROM sec_filings WHERE accession_number = ?", [q1_acc]).fetchone()
    if q1_filing is None:
        prod.close()
        raise RuntimeError(f"Q1 accession {q1_acc} not found in sec_filings")
    q1_report_date, q1_filing_date, q1_form = q1_filing

    old_rows = prod.execute(
        "SELECT fiscal_quarter, metric_name, value, extraction_basis, reconciliation_status, availability_date, "
        "accession_number, concept_qname, context_id, period_start, period_end "
        "FROM quarterly_metric_results WHERE run_id = ? ORDER BY metric_name, fiscal_quarter", [old_run_id],
    ).fetchall()
    if len(old_rows) != 24:
        prod.close()
        raise RuntimeError(f"Existing production has {len(old_rows)} rows for {TICKER} {FISCAL_YEAR_END}, expected 24")

    nvda_review_required_metrics = sorted({r[1] for r in old_rows if r[4] == "REVIEW_REQUIRED"})
    if len(nvda_review_required_metrics) != EXPECTED_NVDA_REVIEW_REQUIRED_COUNT or set(nvda_review_required_metrics) != set(METRICS):
        prod.close()
        raise RuntimeError(f"Expected exactly {EXPECTED_NVDA_REVIEW_REQUIRED_COUNT} REVIEW_REQUIRED metrics "
                            f"(all 6), found {nvda_review_required_metrics}")

    prod.close()

    wh = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    q1_fact_count = wh.execute("SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?", [q1_acc]).fetchone()[0]
    wh.close()
    if q1_fact_count != EXPECTED_Q1_WAREHOUSE_FACTS:
        raise RuntimeError(f"Warehouse facts for Q1 accession = {q1_fact_count}, expected {EXPECTED_Q1_WAREHOUSE_FACTS}")

    print(f"  Global production counts: runs={quarterly_runs} rows={quarterly_rows} fmr={fmr} total_review_required={total_review_required} — all match expected")
    print(f"  Existing run: run_id={old_run_id} engine_version={old_engine_version} run_status={old_run_status}")
    print(f"  Q1 accession = {q1_acc} (report_date={q1_report_date} filing_date={q1_filing_date} form={q1_form})")
    print(f"  Warehouse facts for Q1 accession = {q1_fact_count} (expected {EXPECTED_Q1_WAREHOUSE_FACTS})")
    print(f"  Existing 24 rows: all 6 metrics REVIEW_REQUIRED = {nvda_review_required_metrics}")
    print("\nPHASE 1: ALL PRECONDITIONS MET.")

    return {
        "old_run_id": old_run_id, "old_engine_version": old_engine_version, "old_run_status": old_run_status,
        "q1_accession": q1_acc, "q2_accession": q2_acc, "q3_accession": q3_acc, "fy_accession": fy_acc,
        "q1_report_date": str(q1_report_date), "q1_filing_date": str(q1_filing_date), "q1_form": q1_form,
        "old_rows": [
            {"fiscal_quarter": r[0], "metric_name": r[1], "value": r[2], "extraction_basis": r[3],
             "reconciliation_status": r[4], "availability_date": str(r[5]) if r[5] else None,
             "accession_number": r[6], "concept_qname": r[7], "context_id": r[8],
             "period_start": str(r[9]) if r[9] else None, "period_end": str(r[10]) if r[10] else None}
            for r in old_rows
        ],
        "global_counts_before": {"quarterly_extraction_runs": quarterly_runs, "quarterly_metric_results": quarterly_rows,
                                  "financial_metric_results": fmr, "unique_review_required": total_review_required},
        "q1_warehouse_facts": q1_fact_count,
    }


# =====================================================================
# PHASE 2 — RUN ENGINE V4 (one invocation, this company-year only)
# =====================================================================

def phase2_run_engine(precheck: dict) -> dict:
    print("\n" + "=" * 100)
    print("PHASE 2 — RUN ENGINE V4 (NVDA FY2020-01-26 only)")
    print("=" * 100)
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    v4_json = SCRATCH_DIR / "nvda_fy2020_v4_raw.json"
    v4_csv = SCRATCH_DIR / "nvda_fy2020_v4_raw.csv"
    engine_output = s136.run_quarterly_extraction_engine_v4(
        ticker=TICKER, fiscal_year_end=FISCAL_YEAR_END,
        q1_accession=precheck["q1_accession"], q2_accession=precheck["q2_accession"],
        q3_accession=precheck["q3_accession"], fy_accession=precheck["fy_accession"],
        json_output_path=v4_json, csv_output_path=v4_csv,
    )
    print("\nPHASE 2: ENGINE RUN COMPLETE.")
    return engine_output


# =====================================================================
# ROW BUILDING (read-only proof version of the established
# build_quarter_row pattern from scripts/123/130/134/138 — never writes
# to any database, only builds the 24-row structured proof record)
# =====================================================================

def build_quarter_row(ticker, fiscal_year_end, quarter, metric_name, filings, metric_result):
    period_end = filings[quarter]["report_date"] if quarter != "Q4" else filings["FY"]["report_date"]
    availability_date = filings[quarter]["filing_date"] if quarter != "Q4" else filings["FY"]["filing_date"]
    accession_number = filings[quarter]["accession_number"] if quarter != "Q4" else filings["FY"]["accession_number"]

    quarters = metric_result.get("quarters", {})
    if quarter in quarters:
        q = quarters[quarter]
        lineage = q["lineage"]
        reconciliation = metric_result.get("reconciliation")
        return {
            "ticker": ticker, "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter, "metric_name": metric_name,
            "value": q["value"], "unit": "iso4217:USD", "result_status": "PASS", "extraction_basis": q["extraction_basis"],
            "period_start": lineage.get("period_start"), "period_end": period_end, "availability_date": q["availability_date"],
            "accession_number": lineage.get("accession_number", lineage.get("annual_accession_number", accession_number)),
            "concept_qname": lineage.get("concept_qname", lineage.get("annual_concept_qname")),
            "context_id": lineage.get("context_id", lineage.get("nine_month_ytd_context_id")),
            "duration_days": lineage.get("duration_days"),
            "concept_source": lineage.get("concept_source"),
            "lineage_json": json.dumps(lineage, ensure_ascii=False, default=str),
            "reconciliation_status": reconciliation["status"] if reconciliation else "REVIEW_REQUIRED",
            "reconciliation_difference": reconciliation["difference"] if reconciliation else None,
            "permitted_difference": reconciliation["precision_calculation"]["permitted_difference"] if reconciliation else None,
        }

    error_text = metric_result.get("error", "metric did not resolve for this quarter")
    return {
        "ticker": ticker, "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter, "metric_name": metric_name,
        "value": None, "unit": "iso4217:USD", "result_status": "REVIEW_REQUIRED", "extraction_basis": "UNRESOLVED",
        "period_start": None, "period_end": period_end, "availability_date": availability_date, "accession_number": accession_number,
        "concept_qname": None, "context_id": None, "duration_days": None, "concept_source": None,
        "lineage_json": json.dumps({"error": error_text, "source": "engine could not resolve this metric"}, ensure_ascii=False),
        "reconciliation_status": "REVIEW_REQUIRED", "reconciliation_difference": None, "permitted_difference": None,
    }


# =====================================================================
# PHASE 3 — VALIDATE OUTPUT, BUILD 24 ROWS, COMPARE OLD VS NEW
# =====================================================================

def phase3_validate_and_compare(precheck: dict, engine_output: dict) -> dict:
    print("\n" + "=" * 100)
    print("PHASE 3 — VALIDATE OUTPUT AND COMPARE OLD VS NEW")
    print("=" * 100)

    filings = engine_output["filings"]
    new_rows = []
    for metric_name in METRICS:
        metric_result = engine_output["metrics"][metric_name]
        for quarter in QUARTERS:
            new_rows.append(build_quarter_row(TICKER, FISCAL_YEAR_END, quarter, metric_name, filings, metric_result))

    validation_errors = []
    if len(new_rows) != 24:
        validation_errors.append(f"built {len(new_rows)} rows, expected 24")
    keys = [(r["metric_name"], r["fiscal_quarter"]) for r in new_rows]
    if len(keys) != len(set(keys)):
        validation_errors.append("duplicate metric/quarter keys found")
    for r in new_rows:
        if r["lineage_json"] is None:
            validation_errors.append(f"{r['metric_name']}/{r['fiscal_quarter']}: missing lineage_json")
        expected_avail = filings[r["fiscal_quarter"]]["filing_date"] if r["fiscal_quarter"] != "Q4" else filings["FY"]["filing_date"]
        if r["availability_date"] != expected_avail:
            validation_errors.append(f"{r['metric_name']}/{r['fiscal_quarter']}: availability_date {r['availability_date']} != registered filing_date {expected_avail}")

    # point-in-time checks: no future filing used as concept source, no comparative fact selected
    point_in_time_violations = []
    comparative_fact_violations = []
    for metric_name in METRICS:
        metric_result = engine_output["metrics"][metric_name]
        lineage_by_q = metric_result.get("concept_source_lineage", {})
        for quarter, src in lineage_by_q.items():
            if src.get("source") == "POINT_IN_TIME_SAFE_CONCEPT_REUSE":
                sfd, bfd = src.get("source_filing_date"), src.get("blocking_filing_date")
                if not sfd or not bfd or sfd > bfd:
                    point_in_time_violations.append(f"{metric_name}/{quarter}: source_filing_date={sfd} > blocking_filing_date={bfd}")
        for quarter in ("Q1", "Q2", "Q3"):
            q = metric_result.get("quarters", {}).get(quarter)
            if q and quarter != "Q4":
                expected_period_end = filings[quarter]["report_date"]
                actual_period_end = q["lineage"].get("period_end")
                if actual_period_end and actual_period_end != expected_period_end:
                    comparative_fact_violations.append(f"{metric_name}/{quarter}: period_end {actual_period_end} != this quarter's own report_date {expected_period_end} (possible comparative fact)")

    # Q1-specific checks
    q1_checks = {}
    for metric_name in METRICS:
        q1 = engine_output["metrics"][metric_name].get("quarters", {}).get("Q1")
        if q1 is None:
            q1_checks[metric_name] = {"resolved": False}
            continue
        lineage = q1["lineage"]
        q1_checks[metric_name] = {
            "resolved": True, "accession_number": lineage.get("accession_number"),
            "period_start": lineage.get("period_start"), "period_end": lineage.get("period_end"),
            "concept_qname": lineage.get("concept_qname"), "value": q1["value"],
            "matches_expected_accession": lineage.get("accession_number") == EXPECTED_Q1_ACCESSION,
            "matches_expected_period": lineage.get("period_start") == EXPECTED_Q1_PERIOD_START and lineage.get("period_end") == EXPECTED_Q1_PERIOD_END,
            "matches_expected_concept": lineage.get("concept_qname") == EXPECTED_Q1_CONCEPTS[metric_name],
        }

    # old vs new comparison
    old_by_key = {(r["metric_name"], r["fiscal_quarter"]): r for r in precheck["old_rows"]}
    comparison_rows = []
    regressions = []
    for new_row in new_rows:
        key = (new_row["metric_name"], new_row["fiscal_quarter"])
        old_row = old_by_key[key]
        value_changed = old_row["value"] != new_row["value"]
        basis_changed = old_row["extraction_basis"] != new_row["extraction_basis"]
        status_changed = old_row["reconciliation_status"] != new_row["reconciliation_status"]
        newly_resolved = old_row["value"] is None and new_row["value"] is not None
        regressed = old_row["value"] is not None and new_row["value"] is None
        if regressed:
            regressions.append(key)
        comparison_rows.append({
            "metric_name": new_row["metric_name"], "fiscal_quarter": new_row["fiscal_quarter"],
            "old_status": old_row["reconciliation_status"], "new_status": new_row["reconciliation_status"],
            "old_value": old_row["value"], "new_value": new_row["value"],
            "old_basis": old_row["extraction_basis"], "new_basis": new_row["extraction_basis"],
            "value_changed": value_changed, "basis_changed": basis_changed, "status_changed": status_changed,
            "newly_resolved": newly_resolved, "regressed": regressed,
        })

    metric_outcomes = {}
    for metric_name in METRICS:
        status = engine_output["metrics"][metric_name].get("status")
        metric_outcomes[metric_name] = status

    for name, ok in [("row_count_and_structure", not validation_errors), ("point_in_time_safe", not point_in_time_violations),
                      ("no_comparative_fact_selected", not comparative_fact_violations), ("no_regressions", not regressions)]:
        print(f"  {name}: {'OK' if ok else 'FAIL'}")
    print(f"  metric outcomes: {metric_outcomes}")

    return {
        "new_rows": new_rows, "validation_errors": validation_errors,
        "point_in_time_violations": point_in_time_violations, "comparative_fact_violations": comparative_fact_violations,
        "q1_checks": q1_checks, "comparison_rows": comparison_rows, "regressions": regressions,
        "metric_outcomes": metric_outcomes,
    }


def main() -> dict:
    start_time = time.perf_counter()
    try:
        precheck = phase1_preconditions()
    except Exception as exc:  # noqa: BLE001
        return write_fail_report(f"Phase 1 (preconditions) failed: {exc}", {}, round(time.perf_counter() - start_time, 2))

    engine_output = phase2_run_engine(precheck)
    analysis = phase3_validate_and_compare(precheck, engine_output)

    # cleanup scratch (raw engine dump is not a required deliverable)
    for f in SCRATCH_DIR.glob("*"):
        f.unlink()
    if SCRATCH_DIR.exists():
        SCRATCH_DIR.rmdir()

    # re-verify databases unchanged
    prod = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    counts_after = {
        "quarterly_extraction_runs": prod.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
        "quarterly_metric_results": prod.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
        "financial_metric_results": prod.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
    }
    prod.close()
    wh = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    total_facts_after = wh.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
    wh.close()
    databases_unchanged = counts_after == {k: v for k, v in precheck["global_counts_before"].items() if k in counts_after}

    q1_all_ok = all(v.get("matches_expected_accession", False) and v.get("matches_expected_period", False) for v in analysis["q1_checks"].values() if v.get("resolved"))
    unexplained_diffs = [c for c in analysis["comparison_rows"] if (c["value_changed"] or c["basis_changed"]) and not c["newly_resolved"] and not (c["old_value"] is not None and c["new_value"] is not None and c["old_value"] == c["new_value"])]
    # since ALL old values are None (fully unresolved company-year), any changed row is by definition newly_resolved or still-None; unexplained_diffs should be empty
    unexplained_diffs = [c for c in unexplained_diffs if not (c["old_value"] is None)]

    success = (
        len(analysis["new_rows"]) == 24 and not analysis["validation_errors"]
        and not analysis["point_in_time_violations"] and not analysis["comparative_fact_violations"]
        and not analysis["regressions"] and not unexplained_diffs and databases_unchanged
    )
    overall_status = "PASS" if success else "FAIL"

    resolved_count = sum(1 for s in analysis["metric_outcomes"].values() if s in ("PASS", "PASS_ROUNDING_TOLERANCE"))
    still_review_required = {m: engine_output["metrics"][m].get("error") for m, s in analysis["metric_outcomes"].items() if s == "REVIEW_REQUIRED"}

    output = {
        "status": overall_status, "ticker": TICKER, "fiscal_year_end": FISCAL_YEAR_END,
        "preconditions": {k: v for k, v in precheck.items() if k != "old_rows"},
        "rows": analysis["new_rows"], "old_vs_new_comparison": analysis["comparison_rows"],
        "metric_outcomes": analysis["metric_outcomes"], "resolved_count": resolved_count,
        "still_review_required": still_review_required,
        "q1_checks": analysis["q1_checks"],
        "point_in_time_checks": {"violations": analysis["point_in_time_violations"], "passed": not analysis["point_in_time_violations"]},
        "lineage_checks": {"validation_errors": analysis["validation_errors"], "comparative_fact_violations": analysis["comparative_fact_violations"]},
        "regressions": analysis["regressions"], "unexplained_differences": unexplained_diffs,
        "database_counts_before": precheck["global_counts_before"],
        "database_counts_after": {**counts_after, "total_warehouse_facts_after": total_facts_after},
        "databases_unchanged": databases_unchanged,
        "q1_lineage_all_correct": q1_all_ok,
        "runtime_seconds": round(time.perf_counter() - start_time, 2),
    }

    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")

    csv_columns = ["fiscal_quarter", "metric_name", "old_status", "new_status", "old_value", "new_value",
                   "old_basis", "new_basis", "concept_qname", "context_id", "accession_number",
                   "period_start", "period_end", "duration_days", "reconciliation_difference", "permitted_difference"]
    rows_by_key = {(r["metric_name"], r["fiscal_quarter"]): r for r in analysis["new_rows"]}
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns)
        writer.writeheader()
        for c in analysis["comparison_rows"]:
            full = rows_by_key[(c["metric_name"], c["fiscal_quarter"])]
            writer.writerow({
                "fiscal_quarter": c["fiscal_quarter"], "metric_name": c["metric_name"],
                "old_status": c["old_status"], "new_status": c["new_status"],
                "old_value": c["old_value"], "new_value": c["new_value"],
                "old_basis": c["old_basis"], "new_basis": c["new_basis"],
                "concept_qname": full["concept_qname"], "context_id": full["context_id"],
                "accession_number": full["accession_number"], "period_start": full["period_start"],
                "period_end": full["period_end"], "duration_days": full["duration_days"],
                "reconciliation_difference": full["reconciliation_difference"], "permitted_difference": full["permitted_difference"],
            })
    print(f"CSV written to {CSV_OUTPUT_PATH}")

    print("\n" + "=" * 100)
    print(f"FINAL: {overall_status}  resolved={resolved_count}/6  metric_outcomes={analysis['metric_outcomes']}")
    print("=" * 100)
    return output


if __name__ == "__main__":
    main()
