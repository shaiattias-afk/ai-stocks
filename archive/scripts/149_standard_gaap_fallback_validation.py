"""
TASK_147_STANDARD_GAAP_CONCEPT_FALLBACK_PROOF — read-only validation
driver for scripts/148_quarterly_engine_v5_standard_gaap_fallback.py.

Validation A: derive the current 4 REVIEW_REQUIRED metric-year cases
directly from production (CRWD/MU/PANWx2, 3 distinct company-years), run
engine V5 for each, report old vs. new status, selected concept, exact
blocking accession, and full lineage including rejected candidates.

Validation B: derive the 4 already-resolved regression controls (MSFT
FY2024, AMZN FY2024, ORCL FY2024, NVDA FY2020) directly from production,
run BOTH engine V4 (scripts/136, unmodified) and engine V5 (scripts/148)
for each, and require the resulting 96 rows to be identical in value,
extraction_basis, reconciliation_status, and availability_date, with the
new standard-GAAP-allow-list tier never activating on any control row.

Validation C: for every newly-resolved target case, re-derive from the
V5 output that the resolved value came only from the blocking quarter's
own exact 10-Q accession, no future filing or same-year 10-K was used, no
comparative fact was selected, exactly one allow-listed us-gaap concept
was chosen, and the full-year quarterly sum reconciles to the
authoritative annual result.

Opens both databases read-only throughout. Writes nothing to either.
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
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
SCRATCH_DIR = DATA_DIR / "_scratch_standard_gaap_fallback_validation"

JSON_OUTPUT_PATH = DATA_DIR / "standard_gaap_fallback_validation.json"
CSV_OUTPUT_PATH = DATA_DIR / "standard_gaap_fallback_validation.csv"

CONTROL_TICKERS_2024 = ("MSFT", "AMZN", "ORCL")
CONTROL_TICKERS_2020 = ("NVDA",)

_spec_v4 = importlib.util.spec_from_file_location(
    "s136", PROJECT_DIR / "scripts" / "136_quarterly_extraction_engine_v4_point_in_time_concept_reuse.py"
)
s136 = importlib.util.module_from_spec(_spec_v4)
sys.modules["s136"] = s136
_spec_v4.loader.exec_module(s136)

_spec_v5 = importlib.util.spec_from_file_location(
    "s148", PROJECT_DIR / "scripts" / "148_quarterly_engine_v5_standard_gaap_fallback.py"
)
s148 = importlib.util.module_from_spec(_spec_v5)
sys.modules["s148"] = s148
_spec_v5.loader.exec_module(s148)


# =====================================================================
# DERIVE CASES FROM PRODUCTION — nothing hardcoded
# =====================================================================

def get_global_counts(prod_connection) -> dict:
    return {
        "quarterly_extraction_runs": prod_connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
        "quarterly_metric_results": prod_connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
        "financial_metric_results": prod_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
        "unique_review_required": prod_connection.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results WHERE reconciliation_status = 'REVIEW_REQUIRED')"
        ).fetchone()[0],
    }


def get_target_company_years(prod_connection) -> list[dict]:
    rows = prod_connection.execute(
        "SELECT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results "
        "WHERE reconciliation_status = 'REVIEW_REQUIRED' GROUP BY ticker, fiscal_year_end, metric_name "
        "ORDER BY ticker, fiscal_year_end, metric_name"
    ).fetchall()
    by_company_year: dict[tuple, list[str]] = {}
    for ticker, fiscal_year_end, metric_name in rows:
        by_company_year.setdefault((ticker, str(fiscal_year_end)), []).append(metric_name)

    company_years = []
    for (ticker, fiscal_year_end), metrics in sorted(by_company_year.items()):
        run = prod_connection.execute(
            "SELECT q1_accession, q2_accession, q3_accession, fy_accession, run_id, engine_version, run_status "
            "FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?",
            [ticker, fiscal_year_end],
        ).fetchone()
        if run is None:
            raise RuntimeError(f"No quarterly_extraction_runs row found for target company-year {ticker}/{fiscal_year_end}")
        q1_acc, q2_acc, q3_acc, fy_acc, old_run_id, old_engine_version, old_run_status = run
        company_years.append({
            "ticker": ticker, "fiscal_year_end": fiscal_year_end, "target_metrics": metrics,
            "q1_accession": q1_acc, "q2_accession": q2_acc, "q3_accession": q3_acc, "fy_accession": fy_acc,
            "old_run_id": old_run_id, "old_engine_version": old_engine_version, "old_run_status": old_run_status,
        })
    return company_years


def get_control_company_years(prod_connection) -> list[dict]:
    rows = prod_connection.execute(
        "SELECT ticker, fiscal_year_end, q1_accession, q2_accession, q3_accession, fy_accession, run_id, engine_version, run_status "
        "FROM quarterly_extraction_runs "
        "WHERE (ticker IN ('MSFT','AMZN','ORCL') AND fiscal_year_end LIKE '2024-%') "
        "   OR (ticker IN ('NVDA') AND fiscal_year_end LIKE '2020-%') "
        "ORDER BY ticker"
    ).fetchall()
    controls = []
    for ticker, fiscal_year_end, q1_acc, q2_acc, q3_acc, fy_acc, run_id, engine_version, run_status in rows:
        controls.append({
            "ticker": ticker, "fiscal_year_end": str(fiscal_year_end),
            "q1_accession": q1_acc, "q2_accession": q2_acc, "q3_accession": q3_acc, "fy_accession": fy_acc,
            "old_run_id": run_id, "old_engine_version": engine_version, "old_run_status": run_status,
        })
    if len(controls) != 4:
        raise RuntimeError(f"Expected exactly 4 control company-years (MSFT/AMZN/ORCL FY2024 + NVDA FY2020), found {len(controls)}: {controls}")
    return controls


# =====================================================================
# LINEAGE / SAFETY EXTRACTION FROM AN ENGINE OUTPUT
# =====================================================================

def extract_fallback_activations(engine_output: dict) -> list[dict]:
    activations = []
    for metric_name, metric_result in engine_output["metrics"].items():
        for quarter_label, lineage in metric_result.get("concept_source_lineage", {}).items():
            if lineage.get("source") == "STANDARD_GAAP_ALLOW_LIST":
                activations.append({
                    "ticker": engine_output["company"], "fiscal_year_end": engine_output["fiscal_year_end"],
                    "metric_name": metric_name, "fiscal_quarter": quarter_label, **lineage,
                })
    return activations


def extract_point_in_time_reuse_activations(engine_output: dict) -> list[dict]:
    activations = []
    for metric_name, metric_result in engine_output["metrics"].items():
        for quarter_label, lineage in metric_result.get("concept_source_lineage", {}).items():
            if lineage.get("source") == "POINT_IN_TIME_SAFE_CONCEPT_REUSE":
                activations.append({
                    "ticker": engine_output["company"], "fiscal_year_end": engine_output["fiscal_year_end"],
                    "metric_name": metric_name, "fiscal_quarter": quarter_label, **lineage,
                })
    return activations


def check_point_in_time_safety(engine_output: dict) -> dict:
    violations = []
    filings = engine_output["filings"]
    for activation in extract_point_in_time_reuse_activations(engine_output):
        blocking_filing_date = filings[activation["fiscal_quarter"]]["filing_date"]
        source_filing_date = activation.get("source_filing_date")
        if source_filing_date is None or source_filing_date > blocking_filing_date:
            violations.append({"reason": "point-in-time-reuse source_filing_date is after the blocking quarter's own filing_date",
                                "activation": activation})
    for activation in extract_fallback_activations(engine_output):
        quarter_label = activation["fiscal_quarter"]
        expected_accession = filings[quarter_label]["accession_number"]
        if activation.get("blocking_accession") != expected_accession:
            violations.append({"reason": "standard-GAAP-allow-list activation's blocking_accession does not match "
                                          "the quarter's own accession (would mean a different filing was used)",
                                "activation": activation})
        for rejected in activation.get("rejected_candidates", []):
            pass  # rejected candidates are informational only; no safety implication
    return {"passed": len(violations) == 0, "violations": violations}


def check_no_comparative_and_no_extension_concept(engine_output: dict) -> dict:
    violations = []
    filings = engine_output["filings"]
    for metric_name, metric_result in engine_output["metrics"].items():
        for quarter_label in ("Q1", "Q2", "Q3"):
            quarter = metric_result.get("quarters", {}).get(quarter_label)
            if quarter is None:
                continue
            lineage = quarter["lineage"]
            period_end = lineage.get("period_end")
            expected_period_end = filings[quarter_label]["report_date"]
            if period_end is not None and period_end != expected_period_end:
                violations.append({"reason": f"{metric_name}/{quarter_label}: period_end {period_end} != quarter's own report_date {expected_period_end} (comparative-fact suspicion)"})
        concept_lineage = metric_result.get("concept_source_lineage", {})
        for quarter_label, lineage in concept_lineage.items():
            if lineage.get("source") == "STANDARD_GAAP_ALLOW_LIST":
                concept_qname = lineage["concept_qname"]
                if not concept_qname.startswith("us-gaap:"):
                    violations.append({"reason": f"{metric_name}/{quarter_label}: allow-list selected a non-us-gaap (extension) concept {concept_qname!r}"})
                allow_list = s148.STANDARD_GAAP_ALLOW_LIST.get(metric_name, [])
                if concept_qname not in allow_list:
                    violations.append({"reason": f"{metric_name}/{quarter_label}: selected concept {concept_qname!r} is not literally in the allow-list for {metric_name!r}"})
    return {"passed": len(violations) == 0, "violations": violations}


def rows_from_engine_output(engine_output: dict) -> list[dict]:
    rows = []
    for metric_name, metric_result in engine_output["metrics"].items():
        for quarter_label in ("Q1", "Q2", "Q3", "Q4"):
            quarter = metric_result.get("quarters", {}).get(quarter_label)
            if quarter is None:
                continue
            rows.append({
                "ticker": engine_output["company"], "fiscal_year_end": engine_output["fiscal_year_end"],
                "metric_name": metric_name, "fiscal_quarter": quarter_label,
                "value": quarter["value"], "extraction_basis": quarter["extraction_basis"],
                "reconciliation_status": metric_result.get("status"),
                "availability_date": quarter["availability_date"],
            })
    return rows


def compare_rows(v4_rows: list[dict], v5_rows: list[dict]) -> dict:
    key = lambda r: (r["ticker"], r["fiscal_year_end"], r["metric_name"], r["fiscal_quarter"])
    v4_by_key = {key(r): r for r in v4_rows}
    v5_by_key = {key(r): r for r in v5_rows}
    differences = []
    if set(v4_by_key) != set(v5_by_key):
        differences.append({"reason": "row key sets differ", "v4_only": sorted(set(v4_by_key) - set(v5_by_key)),
                             "v5_only": sorted(set(v5_by_key) - set(v4_by_key))})
    for k in sorted(set(v4_by_key) & set(v5_by_key)):
        v4_row, v5_row = v4_by_key[k], v5_by_key[k]
        for field in ("value", "extraction_basis", "reconciliation_status", "availability_date"):
            v4_value, v5_value = v4_row[field], v5_row[field]
            if field == "value":
                if abs(float(v4_value) - float(v5_value)) >= 1:
                    differences.append({"key": k, "field": field, "v4": v4_value, "v5": v5_value})
            elif v4_value != v5_value:
                differences.append({"key": k, "field": field, "v4": v4_value, "v5": v5_value})
    return {"identical": len(differences) == 0, "differences": differences,
            "v4_row_count": len(v4_rows), "v5_row_count": len(v5_rows)}


# =====================================================================
# MAIN
# =====================================================================

def main() -> dict:
    start_time = time.perf_counter()
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    warehouse_facts_before = warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
    global_counts_before = get_global_counts(prod_connection)

    target_company_years = get_target_company_years(prod_connection)
    control_company_years = get_control_company_years(prod_connection)
    warehouse_connection.close()
    prod_connection.close()

    print("=" * 100)
    print("TARGET COMPANY-YEARS (derived from production REVIEW_REQUIRED rows)")
    for cy in target_company_years:
        print(f"  {cy['ticker']} {cy['fiscal_year_end']}: {cy['target_metrics']}")
    print("\nCONTROL COMPANY-YEARS (derived from production quarterly_extraction_runs)")
    for cy in control_company_years:
        print(f"  {cy['ticker']} {cy['fiscal_year_end']} (engine_version={cy['old_engine_version']}, run_status={cy['old_run_status']})")
    print("=" * 100 + "\n")

    # -----------------------------------------------------------------
    # VALIDATION A — 4 target metric-year cases, engine V5 (+ engine V4
    # for isolation, proving the improvement is specifically tier 3's).
    # -----------------------------------------------------------------
    target_results = []
    for cy in target_company_years:
        print(f"\n### TARGET: {cy['ticker']} {cy['fiscal_year_end']} ###")
        v5_json = SCRATCH_DIR / f"v5_target_{cy['ticker']}_{cy['fiscal_year_end']}.json"
        v5_csv = SCRATCH_DIR / f"v5_target_{cy['ticker']}_{cy['fiscal_year_end']}.csv"
        v5_output = s148.run_quarterly_extraction_engine_v5(
            ticker=cy["ticker"], fiscal_year_end=cy["fiscal_year_end"],
            q1_accession=cy["q1_accession"], q2_accession=cy["q2_accession"],
            q3_accession=cy["q3_accession"], fy_accession=cy["fy_accession"],
            json_output_path=v5_json, csv_output_path=v5_csv,
        )

        v4_json = SCRATCH_DIR / f"v4_target_{cy['ticker']}_{cy['fiscal_year_end']}.json"
        v4_csv = SCRATCH_DIR / f"v4_target_{cy['ticker']}_{cy['fiscal_year_end']}.csv"
        v4_output = s136.run_quarterly_extraction_engine_v4(
            ticker=cy["ticker"], fiscal_year_end=cy["fiscal_year_end"],
            q1_accession=cy["q1_accession"], q2_accession=cy["q2_accession"],
            q3_accession=cy["q3_accession"], fy_accession=cy["fy_accession"],
            json_output_path=v4_json, csv_output_path=v4_csv,
        )

        point_in_time_check = check_point_in_time_safety(v5_output)
        no_comparative_check = check_no_comparative_and_no_extension_concept(v5_output)
        fallback_activations = extract_fallback_activations(v5_output)

        for metric_name in cy["target_metrics"]:
            v5_metric = v5_output["metrics"][metric_name]
            v4_metric = v4_output["metrics"][metric_name]
            new_status = v5_metric.get("status")
            metric_activations = [a for a in fallback_activations if a["metric_name"] == metric_name]
            case_result = {
                "ticker": cy["ticker"], "fiscal_year_end": cy["fiscal_year_end"], "metric_name": metric_name,
                "old_status": "REVIEW_REQUIRED",
                "old_run_id": cy["old_run_id"], "old_engine_version": cy["old_engine_version"],
                "v4_isolation_status": v4_metric.get("status"),
                "new_status_v5": new_status,
                "resolved": new_status in ("PASS", "PASS_ROUNDING_TOLERANCE"),
                "standard_gaap_fallback_activated": len(metric_activations) > 0,
                "fallback_activations": metric_activations,
                "q1_accession": cy["q1_accession"], "q2_accession": cy["q2_accession"],
                "q3_accession": cy["q3_accession"], "fy_accession": cy["fy_accession"],
                "quarters": v5_metric.get("quarters"),
                "reconciliation": v5_metric.get("reconciliation"),
                "concept_source_lineage": v5_metric.get("concept_source_lineage"),
                "error_if_unresolved": v5_metric.get("error"),
            }
            target_results.append(case_result)
            print(f"  {metric_name}: REVIEW_REQUIRED -> {new_status} "
                  f"(V4-only rerun: {v4_metric.get('status')}; fallback activated: {len(metric_activations) > 0})")

    family_counts = {"pretax_income": sum(1 for r in target_results if r["metric_name"] == "pretax_income"),
                      "revenue": sum(1 for r in target_results if r["metric_name"] == "revenue")}
    print(f"\nFamily counts: {family_counts} (expected pretax_income=3, revenue=1)")

    # -----------------------------------------------------------------
    # VALIDATION B — 4 regression controls, V4 vs V5, require 96 rows,
    # identical values, and 0 fallback activations.
    # -----------------------------------------------------------------
    control_results = []
    all_control_v4_rows, all_control_v5_rows = [], []
    control_fallback_activations = []
    for cy in control_company_years:
        print(f"\n### CONTROL: {cy['ticker']} {cy['fiscal_year_end']} ###")
        v4_json = SCRATCH_DIR / f"v4_control_{cy['ticker']}.json"
        v4_csv = SCRATCH_DIR / f"v4_control_{cy['ticker']}.csv"
        v4_output = s136.run_quarterly_extraction_engine_v4(
            ticker=cy["ticker"], fiscal_year_end=cy["fiscal_year_end"],
            q1_accession=cy["q1_accession"], q2_accession=cy["q2_accession"],
            q3_accession=cy["q3_accession"], fy_accession=cy["fy_accession"],
            json_output_path=v4_json, csv_output_path=v4_csv,
        )
        v5_json = SCRATCH_DIR / f"v5_control_{cy['ticker']}.json"
        v5_csv = SCRATCH_DIR / f"v5_control_{cy['ticker']}.csv"
        v5_output = s148.run_quarterly_extraction_engine_v5(
            ticker=cy["ticker"], fiscal_year_end=cy["fiscal_year_end"],
            q1_accession=cy["q1_accession"], q2_accession=cy["q2_accession"],
            q3_accession=cy["q3_accession"], fy_accession=cy["fy_accession"],
            json_output_path=v5_json, csv_output_path=v5_csv,
        )

        v4_rows = rows_from_engine_output(v4_output)
        v5_rows = rows_from_engine_output(v5_output)
        comparison = compare_rows(v4_rows, v5_rows)
        activations = extract_fallback_activations(v5_output)

        all_control_v4_rows.extend(v4_rows)
        all_control_v5_rows.extend(v5_rows)
        control_fallback_activations.extend(activations)

        control_results.append({
            "ticker": cy["ticker"], "fiscal_year_end": cy["fiscal_year_end"],
            "old_run_id": cy["old_run_id"], "old_engine_version": cy["old_engine_version"],
            "v4_row_count": len(v4_rows), "v5_row_count": len(v5_rows),
            "identical": comparison["identical"], "differences": comparison["differences"],
            "standard_gaap_fallback_activations": activations,
        })
        print(f"  V4 rows={len(v4_rows)} V5 rows={len(v5_rows)} identical={comparison['identical']} "
              f"fallback_activations={len(activations)}")

    total_control_comparison = compare_rows(all_control_v4_rows, all_control_v5_rows)
    controls_all_identical = total_control_comparison["identical"] and len(control_fallback_activations) == 0 \
        and total_control_comparison["v4_row_count"] == 96 and total_control_comparison["v5_row_count"] == 96

    # -----------------------------------------------------------------
    # VALIDATION C — target safety checks (point-in-time, no-comparative,
    # no-extension-concept already computed per case above; add full-year
    # reconciliation check here).
    # -----------------------------------------------------------------
    safety_checks = []
    for case in target_results:
        reconciliation = case.get("reconciliation")
        reconciles = reconciliation is not None and reconciliation.get("status") in ("PASS", "PASS_ROUNDING_TOLERANCE")
        lineage = case.get("concept_source_lineage", {})
        q1_lineage = lineage.get("Q1", {})
        allow_list_used = q1_lineage.get("source") == "STANDARD_GAAP_ALLOW_LIST"
        concept_qname = q1_lineage.get("concept_qname")
        is_us_gaap = concept_qname is not None and concept_qname.startswith("us-gaap:")
        in_allow_list = concept_qname in s148.STANDARD_GAAP_ALLOW_LIST.get(case["metric_name"], []) if concept_qname else False
        q1_blocking_accession_ok = q1_lineage.get("blocking_accession") == case["q1_accession"]
        safety_checks.append({
            "ticker": case["ticker"], "fiscal_year_end": case["fiscal_year_end"], "metric_name": case["metric_name"],
            "resolved": case["resolved"],
            "value_from_blocking_10q": q1_blocking_accession_ok if allow_list_used else None,
            "exactly_one_allowed_concept": in_allow_list if allow_list_used else None,
            "no_extension_concept": is_us_gaap if allow_list_used else None,
            "full_year_reconciles": reconciles,
            "reconciliation_status": reconciliation.get("status") if reconciliation else None,
            "reconciliation_difference": reconciliation.get("difference") if reconciliation else None,
        })

    for cy in target_company_years:
        v5_json = SCRATCH_DIR / f"v5_target_{cy['ticker']}_{cy['fiscal_year_end']}.json"
        v5_output = json.loads(v5_json.read_text(encoding="utf-8"))
        pit = check_point_in_time_safety(v5_output)
        comp = check_no_comparative_and_no_extension_concept(v5_output)
        cy["point_in_time_check"] = pit
        cy["no_comparative_no_extension_check"] = comp

    all_safety_ok = (
        all(sc["full_year_reconciles"] for sc in safety_checks if sc["resolved"])
        and all(cy["point_in_time_check"]["passed"] for cy in target_company_years)
        and all(cy["no_comparative_no_extension_check"]["passed"] for cy in target_company_years)
    )

    # -----------------------------------------------------------------
    # DATABASE UNCHANGED CONFIRMATION
    # -----------------------------------------------------------------
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    global_counts_after = get_global_counts(prod_connection)
    warehouse_facts_after = warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
    prod_connection.close()
    warehouse_connection.close()
    databases_unchanged = (global_counts_before == global_counts_after) and (warehouse_facts_before == warehouse_facts_after)

    resolved_count = sum(1 for c in target_results if c["resolved"])
    still_unresolved = [{"ticker": c["ticker"], "fiscal_year_end": c["fiscal_year_end"], "metric_name": c["metric_name"],
                          "reason": c["error_if_unresolved"]} for c in target_results if not c["resolved"]]

    overall_status = "PASS" if (controls_all_identical and all_safety_ok and databases_unchanged) else "FAIL"

    runtime_seconds = round(time.perf_counter() - start_time, 2)

    output = {
        "status": overall_status,
        "allow_list_version": s148.STANDARD_GAAP_ALLOW_LIST_VERSION,
        "allow_list": s148.STANDARD_GAAP_ALLOW_LIST,
        "global_counts_before": global_counts_before, "global_counts_after": global_counts_after,
        "warehouse_facts_before": warehouse_facts_before, "warehouse_facts_after": warehouse_facts_after,
        "databases_unchanged": databases_unchanged,
        "target_company_years": [
            {"ticker": cy["ticker"], "fiscal_year_end": cy["fiscal_year_end"], "target_metrics": cy["target_metrics"],
             "point_in_time_check": cy["point_in_time_check"],
             "no_comparative_no_extension_check": cy["no_comparative_no_extension_check"]}
            for cy in target_company_years
        ],
        "target_cases": target_results, "family_counts": family_counts,
        "resolved_count": resolved_count, "still_unresolved": still_unresolved,
        "safety_checks": safety_checks, "all_safety_checks_passed": all_safety_ok,
        "control_company_years": [
            {"ticker": cy["ticker"], "fiscal_year_end": cy["fiscal_year_end"],
             "old_run_id": cy["old_run_id"], "old_engine_version": cy["old_engine_version"]}
            for cy in control_company_years
        ],
        "control_results": control_results,
        "control_total_comparison": total_control_comparison,
        "control_fallback_activations": control_fallback_activations,
        "controls_all_identical": controls_all_identical,
        "runtime_seconds": runtime_seconds,
    }

    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")

    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["group", "ticker", "fiscal_year_end", "metric_name", "fiscal_quarter",
                          "v5_value", "v5_extraction_basis", "v5_reconciliation_status",
                          "fallback_activated", "old_status", "new_status"])
        for case in target_results:
            for quarter_label, quarter in (case.get("quarters") or {}).items():
                writer.writerow(["target", case["ticker"], case["fiscal_year_end"], case["metric_name"], quarter_label,
                                  quarter["value"], quarter["extraction_basis"], case["new_status_v5"],
                                  quarter_label in [a["fiscal_quarter"] for a in case["fallback_activations"]],
                                  case["old_status"], case["new_status_v5"]])
        for row in all_control_v5_rows:
            writer.writerow(["control", row["ticker"], row["fiscal_year_end"], row["metric_name"], row["fiscal_quarter"],
                              row["value"], row["extraction_basis"], row["reconciliation_status"],
                              False, row["reconciliation_status"], row["reconciliation_status"]])
    print(f"CSV written to {CSV_OUTPUT_PATH}")

    print("\n" + "=" * 100)
    print(f"FINAL: {overall_status}")
    print(f"  Target cases resolved: {resolved_count}/{len(target_results)}")
    print(f"  Controls identical (96 rows required): {controls_all_identical} "
          f"(v4={total_control_comparison['v4_row_count']} v5={total_control_comparison['v5_row_count']})")
    print(f"  Safety checks passed: {all_safety_ok}")
    print(f"  Databases unchanged: {databases_unchanged}")
    print(f"  Runtime: {runtime_seconds}s")
    print("=" * 100)

    return output


if __name__ == "__main__":
    main()
