"""
TASK_148_V5_FINAL_RELEASE_REGRESSION — the one final release regression
for quarterly engine V5 (scripts/148) across all 45 authoritative
company-years currently in quarterly production.

For each of the 45 company-years, V5 is run EXACTLY ONCE, as a
subprocess of scripts/148's own CLI (so a real, enforceable 45-second
wall-clock timeout can be applied and the child process actually killed
on overrun — an in-process call cannot be forcibly terminated). Engine
V4 (scripts/136) is never invoked anywhere in this script.

Every one of the resulting 24 rows per company-year is compared directly
against the CURRENT ACTIVE production row for that exact
(ticker, fiscal_year_end, metric_name, fiscal_quarter). Only the 4 known
target metric-year cases (CRWD/MU/PANWx2) may differ — and even then
only subject to the required safety checks (value from the exact
blocking 10-Q, concept in STANDARD_GAAP_ALLOW_LIST_V1, no future filing,
no comparative fact, full-year reconciliation, complete lineage). Any
other difference is treated as an immediate, hard FAIL — the regression
stops on the spot, with the exact offending row saved.

The main JSON/CSV outputs are rewritten (atomically) after every single
company-year, so they always reflect a consistent checkpoint of progress
so far even if the process is interrupted or a company-year fails.

Read-only against both databases throughout. Loads nothing into
production. Creates no engine version beyond scripts/148.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
V5_ENGINE_SCRIPT = PROJECT_DIR / "scripts" / "148_quarterly_engine_v5_standard_gaap_fallback.py"
SCRATCH_DIR = DATA_DIR / "_scratch_v5_final_release_regression"

JSON_OUTPUT_PATH = DATA_DIR / "v5_final_release_regression.json"
CSV_OUTPUT_PATH = DATA_DIR / "v5_final_release_regression.csv"

HARD_TIMEOUT_SECONDS = 45
SOFT_WARNING_SECONDS = 30
EXPECTED_COMPANY_YEARS = 45
EXPECTED_ROWS_PER_COMPANY_YEAR = 24
EXPECTED_TOTAL_ROWS = EXPECTED_COMPANY_YEARS * EXPECTED_ROWS_PER_COMPANY_YEAR

# STANDARD_GAAP_ALLOW_LIST_V1 constant + the point-in-time / no-comparative
# safety-check helpers are reused unchanged from scripts/148 and scripts/149
# via importlib -- never copy-pasted, never re-derived.
_spec_v5 = importlib.util.spec_from_file_location("s148", V5_ENGINE_SCRIPT)
s148 = importlib.util.module_from_spec(_spec_v5)
sys.modules["s148"] = s148
_spec_v5.loader.exec_module(s148)

_spec_v149 = importlib.util.spec_from_file_location(
    "s149", PROJECT_DIR / "scripts" / "149_standard_gaap_fallback_validation.py"
)
s149 = importlib.util.module_from_spec(_spec_v149)
sys.modules["s149"] = s149
_spec_v149.loader.exec_module(s149)


def atomic_write_json(path: Path, data: dict) -> None:
    temp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
    os.replace(temp_path, path)


def write_csv(path: Path, company_year_results: list[dict]) -> None:
    temp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with temp_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "fiscal_year_end", "status", "elapsed_seconds", "row_count",
                          "changed_rows", "unexpected_differences", "run_id"])
        for cy in company_year_results:
            writer.writerow([cy["ticker"], cy["fiscal_year_end"], cy["status"], cy["elapsed_seconds"],
                              cy.get("row_count"), len(cy.get("expected_changes", [])),
                              len(cy.get("unexpected_differences", [])), cy["run_id"]])
    os.replace(temp_path, path)


def get_global_counts(prod_connection) -> dict:
    return {
        "quarterly_extraction_runs": prod_connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
        "quarterly_metric_results": prod_connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
        "financial_metric_results": prod_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
        "unique_review_required": prod_connection.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results WHERE reconciliation_status = 'REVIEW_REQUIRED')"
        ).fetchone()[0],
    }


def get_all_company_years(prod_connection) -> list[dict]:
    rows = prod_connection.execute(
        "SELECT run_id, ticker, fiscal_year_end, q1_accession, q2_accession, q3_accession, fy_accession "
        "FROM quarterly_extraction_runs ORDER BY ticker, fiscal_year_end"
    ).fetchall()
    return [{"run_id": r[0], "ticker": r[1], "fiscal_year_end": str(r[2]),
             "q1_accession": r[3], "q2_accession": r[4], "q3_accession": r[5], "fy_accession": r[6]} for r in rows]


def get_target_metric_year_cases(prod_connection) -> set[tuple[str, str, str]]:
    rows = prod_connection.execute(
        "SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results WHERE reconciliation_status = 'REVIEW_REQUIRED'"
    ).fetchall()
    return {(ticker, str(fiscal_year_end), metric_name) for ticker, fiscal_year_end, metric_name in rows}


def get_production_rows(prod_connection, run_id: str) -> dict[tuple[str, str], dict]:
    rows = prod_connection.execute(
        "SELECT metric_name, fiscal_quarter, value, extraction_basis, reconciliation_status, availability_date, "
        "concept_qname, accession_number, (lineage_json IS NOT NULL) AS has_lineage "
        "FROM quarterly_metric_results WHERE run_id = ?", [run_id],
    ).fetchall()
    out = {}
    for metric_name, fiscal_quarter, value, extraction_basis, reconciliation_status, availability_date, concept_qname, accession_number, has_lineage in rows:
        out[(metric_name, fiscal_quarter)] = {
            "value": value, "extraction_basis": extraction_basis, "reconciliation_status": reconciliation_status,
            "availability_date": availability_date, "concept_qname": concept_qname,
            "accession_number": accession_number, "has_lineage": has_lineage,
        }
    return out


def rows_from_v5_output(engine_output: dict) -> list[dict]:
    rows = []
    for metric_name, metric_result in engine_output["metrics"].items():
        for quarter_label in ("Q1", "Q2", "Q3", "Q4"):
            quarter = metric_result.get("quarters", {}).get(quarter_label)
            if quarter is None:
                continue
            lineage = quarter["lineage"]
            rows.append({
                "metric_name": metric_name, "fiscal_quarter": quarter_label,
                "value": quarter["value"], "extraction_basis": quarter["extraction_basis"],
                "reconciliation_status": metric_result.get("status"),
                "availability_date": quarter["availability_date"],
                "concept_qname": lineage.get("concept_qname") or lineage.get("annual_concept_qname"),
                "accession_number": lineage.get("accession_number") or lineage.get("annual_accession_number"),
                "lineage_present": lineage is not None,
            })
    return rows


def values_equal(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1


def compare_company_year(
    v5_rows: list[dict], prod_rows: dict[tuple[str, str], dict], target_metrics: set[str],
) -> tuple[list[dict], list[dict]]:
    """Returns (unexpected_differences, expected_changes)."""
    unexpected = []
    expected_changes = []
    for row in v5_rows:
        key = (row["metric_name"], row["fiscal_quarter"])
        prod_row = prod_rows.get(key)
        if prod_row is None:
            unexpected.append({"key": key, "reason": "no matching production row found for this metric/quarter", "v5_row": row})
            continue

        differs = (
            not values_equal(row["value"], prod_row["value"])
            or row["extraction_basis"] != prod_row["extraction_basis"]
            or row["reconciliation_status"] != prod_row["reconciliation_status"]
            or row["availability_date"] != prod_row["availability_date"]
        )

        if not differs:
            continue

        if row["metric_name"] in target_metrics:
            expected_changes.append({"key": key, "v5_row": row, "production_row": prod_row})
        else:
            unexpected.append({"key": key, "reason": "production row changed outside the 4 approved target metric-year cases",
                                "v5_row": row, "production_row": prod_row})
    return unexpected, expected_changes


def validate_target_family_safety(engine_output: dict, metric_name: str, allow_list: dict) -> dict:
    """Reuses scripts/149's point-in-time / no-comparative-fact / no-extension-concept
    checks (unchanged), scoped to one metric, plus this task's own
    'value from blocking 10-Q' + 'concept in allow-list' checks on whichever
    quarter actually activated the new STANDARD_GAAP_ALLOW_LIST tier."""
    metric_result = engine_output["metrics"][metric_name]
    filings = engine_output["filings"]
    violations = []

    allow_list_activation = None
    for quarter_label, lineage in metric_result.get("concept_source_lineage", {}).items():
        if lineage.get("source") == "STANDARD_GAAP_ALLOW_LIST":
            allow_list_activation = (quarter_label, lineage)
            break

    if allow_list_activation is None:
        violations.append({"reason": f"{metric_name}: no STANDARD_GAAP_ALLOW_LIST activation found, "
                                      f"but production row changed for this metric — cannot confirm the expected cause"})
    else:
        quarter_label, lineage = allow_list_activation
        expected_accession = filings[quarter_label]["accession_number"]
        if lineage.get("blocking_accession") != expected_accession:
            violations.append({"reason": f"{metric_name}/{quarter_label}: blocking_accession != the quarter's own accession"})
        concept_qname = lineage.get("concept_qname")
        if concept_qname not in allow_list.get(metric_name, []):
            violations.append({"reason": f"{metric_name}/{quarter_label}: selected concept {concept_qname!r} is not in STANDARD_GAAP_ALLOW_LIST_V1"})

    # Full V5 output built to reuse s149's whole-output checks; filter to this metric only.
    single_metric_output = {**engine_output, "metrics": {metric_name: metric_result}}
    pit_check = s149.check_point_in_time_safety(single_metric_output)
    no_comp_check = s149.check_no_comparative_and_no_extension_concept(single_metric_output)
    if not pit_check["passed"]:
        violations.extend(pit_check["violations"])
    if not no_comp_check["passed"]:
        violations.extend(no_comp_check["violations"])

    reconciliation = metric_result.get("reconciliation")
    reconciles = reconciliation is not None and reconciliation.get("status") in ("PASS", "PASS_ROUNDING_TOLERANCE")
    if not reconciles:
        violations.append({"reason": f"{metric_name}: reconciliation status {reconciliation.get('status') if reconciliation else None!r} "
                                      f"is not PASS or PASS_ROUNDING_TOLERANCE"})

    missing_lineage = [q for q in ("Q1", "Q2", "Q3", "Q4") if metric_result.get("quarters", {}).get(q, {}).get("lineage") is None]
    if missing_lineage:
        violations.append({"reason": f"{metric_name}: missing lineage for quarters {missing_lineage}"})

    return {"passed": len(violations) == 0, "violations": violations,
            "allow_list_activation_quarter": allow_list_activation[0] if allow_list_activation else None,
            "reconciliation_status": reconciliation.get("status") if reconciliation else None,
            "reconciliation_difference": reconciliation.get("difference") if reconciliation else None}


def check_availability_dates_against_sec_filings(prod_connection, v5_rows: list[dict], accessions_seen: set[str]) -> list[dict]:
    violations = []
    filing_dates = {}
    for accession in accessions_seen:
        row = prod_connection.execute("SELECT filing_date FROM sec_filings WHERE accession_number = ?", [accession]).fetchone()
        filing_dates[accession] = str(row[0]) if row else None
    for row in v5_rows:
        expected = filing_dates.get(row["accession_number"])
        if expected is not None and row["availability_date"] != expected:
            violations.append({"row": row, "expected_availability_date": expected})
    return violations


def main() -> dict:
    start_time = time.perf_counter()
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    global_counts_before = get_global_counts(prod_connection)
    warehouse_facts_before = warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
    warehouse_connection.close()

    all_company_years = get_all_company_years(prod_connection)
    target_metric_year_cases = get_target_metric_year_cases(prod_connection)
    target_cases_by_company_year: dict[tuple[str, str], set[str]] = {}
    for ticker, fiscal_year_end, metric_name in target_metric_year_cases:
        target_cases_by_company_year.setdefault((ticker, fiscal_year_end), set()).add(metric_name)

    print("=" * 100)
    print(f"V5 FINAL RELEASE REGRESSION — {len(all_company_years)} company-years (expected {EXPECTED_COMPANY_YEARS})")
    print(f"Target metric-year cases (expected to change): {sorted(target_metric_year_cases)}")
    print("=" * 100 + "\n")

    if len(all_company_years) != EXPECTED_COMPANY_YEARS:
        raise RuntimeError(f"Expected exactly {EXPECTED_COMPANY_YEARS} company-years in production, found {len(all_company_years)}")

    company_year_results: list[dict] = []
    overall_status = "PASS"
    stop_reason = None
    total_engine_invocations = 0

    output = {
        "status": "IN_PROGRESS", "global_counts_before": global_counts_before,
        "warehouse_facts_before": warehouse_facts_before,
        "target_metric_year_cases": sorted(target_metric_year_cases),
        "expected_company_years": EXPECTED_COMPANY_YEARS, "hard_timeout_seconds": HARD_TIMEOUT_SECONDS,
        "soft_warning_seconds": SOFT_WARNING_SECONDS, "allow_list_version": s148.STANDARD_GAAP_ALLOW_LIST_VERSION,
        "company_year_results": company_year_results, "v4_rerun": False,
    }

    for index, cy in enumerate(all_company_years, start=1):
        cy_start = time.perf_counter()
        json_out = SCRATCH_DIR / f"v5_{cy['ticker']}_{cy['fiscal_year_end']}.json"
        csv_out = SCRATCH_DIR / f"v5_{cy['ticker']}_{cy['fiscal_year_end']}.csv"

        cmd = [
            sys.executable, str(V5_ENGINE_SCRIPT),
            "--ticker", cy["ticker"], "--fiscal-year-end", cy["fiscal_year_end"],
            "--q1-accession", cy["q1_accession"], "--q2-accession", cy["q2_accession"],
            "--q3-accession", cy["q3_accession"], "--fy-accession", cy["fy_accession"],
            "--json-output", str(json_out), "--csv-output", str(csv_out),
        ]

        cy_result = {"ticker": cy["ticker"], "fiscal_year_end": cy["fiscal_year_end"], "run_id": cy["run_id"]}
        total_engine_invocations += 1

        try:
            completed = subprocess.run(cmd, cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=HARD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            elapsed = round(time.perf_counter() - cy_start, 2)
            cy_result.update({"status": "FAIL_PERFORMANCE", "elapsed_seconds": elapsed,
                               "reason": f"exceeded the {HARD_TIMEOUT_SECONDS}s hard per-company-year time limit"})
            company_year_results.append(cy_result)
            overall_status = "FAIL"
            stop_reason = f"{cy['ticker']} {cy['fiscal_year_end']} exceeded {HARD_TIMEOUT_SECONDS}s hard timeout"
            print(f"{index}/{EXPECTED_COMPANY_YEARS} {cy['ticker']} {cy['fiscal_year_end']} "
                  f"FAIL_PERFORMANCE elapsed={elapsed}s (>{HARD_TIMEOUT_SECONDS}s)")
            output["company_year_results"] = company_year_results
            output["status"] = "FAIL"
            output["stop_reason"] = stop_reason
            atomic_write_json(JSON_OUTPUT_PATH, output)
            write_csv(CSV_OUTPUT_PATH, company_year_results)
            break

        elapsed = round(time.perf_counter() - cy_start, 2)

        if completed.returncode != 0:
            cy_result.update({"status": "FAIL_ENGINE_ERROR", "elapsed_seconds": elapsed,
                               "reason": "engine subprocess exited with a non-zero return code",
                               "stderr_tail": completed.stderr[-4000:] if completed.stderr else None})
            company_year_results.append(cy_result)
            overall_status = "FAIL"
            stop_reason = f"{cy['ticker']} {cy['fiscal_year_end']} engine subprocess failed (exit {completed.returncode})"
            print(f"{index}/{EXPECTED_COMPANY_YEARS} {cy['ticker']} {cy['fiscal_year_end']} "
                  f"FAIL_ENGINE_ERROR elapsed={elapsed}s exit={completed.returncode}")
            output["company_year_results"] = company_year_results
            output["status"] = "FAIL"
            output["stop_reason"] = stop_reason
            atomic_write_json(JSON_OUTPUT_PATH, output)
            write_csv(CSV_OUTPUT_PATH, company_year_results)
            break

        engine_output = json.loads(json_out.read_text(encoding="utf-8"))
        v5_rows = rows_from_v5_output(engine_output)

        if len(v5_rows) != EXPECTED_ROWS_PER_COMPANY_YEAR:
            cy_result.update({"status": "FAIL_ROW_COUNT", "elapsed_seconds": elapsed, "row_count": len(v5_rows),
                               "reason": f"expected exactly {EXPECTED_ROWS_PER_COMPANY_YEAR} rows, got {len(v5_rows)}"})
            company_year_results.append(cy_result)
            overall_status = "FAIL"
            stop_reason = f"{cy['ticker']} {cy['fiscal_year_end']} produced {len(v5_rows)} rows, expected {EXPECTED_ROWS_PER_COMPANY_YEAR}"
            print(f"{index}/{EXPECTED_COMPANY_YEARS} {cy['ticker']} {cy['fiscal_year_end']} "
                  f"FAIL_ROW_COUNT elapsed={elapsed}s rows={len(v5_rows)}")
            output["company_year_results"] = company_year_results
            output["status"] = "FAIL"
            output["stop_reason"] = stop_reason
            atomic_write_json(JSON_OUTPUT_PATH, output)
            write_csv(CSV_OUTPUT_PATH, company_year_results)
            break

        keys = [(r["metric_name"], r["fiscal_quarter"]) for r in v5_rows]
        duplicate_keys = len(keys) != len(set(keys))
        missing_lineage_rows = [r for r in v5_rows if not r["lineage_present"]]

        prod_rows = get_production_rows(prod_connection, cy["run_id"])
        target_metrics = target_cases_by_company_year.get((cy["ticker"], cy["fiscal_year_end"]), set())
        unexpected_differences, expected_changes = compare_company_year(v5_rows, prod_rows, target_metrics)

        accessions_seen = {r["accession_number"] for r in v5_rows if r["accession_number"]}
        availability_violations = check_availability_dates_against_sec_filings(prod_connection, v5_rows, accessions_seen)

        family_safety: dict[str, dict] = {}
        changed_metrics = {c["key"][0] for c in expected_changes}
        for metric_name in changed_metrics:
            family_safety[metric_name] = validate_target_family_safety(engine_output, metric_name, s148.STANDARD_GAAP_ALLOW_LIST)
            if not family_safety[metric_name]["passed"]:
                unexpected_differences.append({"reason": f"target metric {metric_name} changed but failed its required safety checks",
                                                "violations": family_safety[metric_name]["violations"]})

        cy_status = "PASS"
        if duplicate_keys:
            unexpected_differences.append({"reason": "duplicate (metric_name, fiscal_quarter) keys found"})
        if missing_lineage_rows:
            unexpected_differences.append({"reason": "rows missing lineage", "rows": [(r["metric_name"], r["fiscal_quarter"]) for r in missing_lineage_rows]})
        if availability_violations:
            unexpected_differences.append({"reason": "availability_date does not match sec_filings.filing_date", "violations": availability_violations})

        if unexpected_differences:
            cy_status = "FAIL"
            overall_status = "FAIL"

        cy_result.update({
            "status": cy_status, "elapsed_seconds": elapsed, "row_count": len(v5_rows),
            "duplicate_keys": duplicate_keys, "missing_lineage_count": len(missing_lineage_rows),
            "availability_violations": availability_violations,
            "expected_changes": expected_changes, "family_safety_checks": family_safety,
            "unexpected_differences": unexpected_differences,
        })
        company_year_results.append(cy_result)

        warn_flag = " [SLOW >30s]" if elapsed > SOFT_WARNING_SECONDS else ""
        print(f"{index}/{EXPECTED_COMPANY_YEARS} {cy['ticker']} {cy['fiscal_year_end']} "
              f"{cy_status} elapsed={elapsed}s changed={len(expected_changes)}{warn_flag}")

        output["company_year_results"] = company_year_results
        output["status"] = "IN_PROGRESS" if cy_status == "PASS" else "FAIL"
        atomic_write_json(JSON_OUTPUT_PATH, output)
        write_csv(CSV_OUTPUT_PATH, company_year_results)

        if cy_status == "FAIL":
            stop_reason = f"{cy['ticker']} {cy['fiscal_year_end']}: unexpected difference(s) found — stopping immediately"
            print(f"\nSTOPPING IMMEDIATELY: {stop_reason}")
            break

    prod_connection.close()

    # -------------------------------------------------------------
    # Global success conditions -- only meaningfully evaluated if all
    # 45 company-years completed without an early stop.
    # -------------------------------------------------------------
    all_completed = len(company_year_results) == EXPECTED_COMPANY_YEARS and overall_status == "PASS"
    total_rows_produced = sum(cy.get("row_count", 0) for cy in company_year_results)
    total_duplicate_keys = sum(1 for cy in company_year_results if cy.get("duplicate_keys"))
    total_missing_lineage = sum(cy.get("missing_lineage_count", 0) for cy in company_year_results)
    total_availability_mismatches = sum(len(cy.get("availability_violations", [])) for cy in company_year_results)
    total_future_data_violations = sum(
        len(v.get("violations", [])) for cy in company_year_results
        for v in cy.get("family_safety_checks", {}).values() if not v["passed"]
    )
    resolved_target_cases = {
        (cy["ticker"], cy["fiscal_year_end"], c["key"][0])
        for cy in company_year_results if cy.get("status") == "PASS"
        for c in cy.get("expected_changes", [])
    }
    all_targets_resolved = target_metric_year_cases <= resolved_target_cases if all_completed else False

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    global_counts_after = get_global_counts(prod_connection)
    warehouse_facts_after = warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
    prod_connection.close()
    warehouse_connection.close()
    databases_unchanged = (global_counts_before == global_counts_after) and (warehouse_facts_before == warehouse_facts_after)

    global_checks = {
        "all_45_company_years_complete": len(company_year_results) == EXPECTED_COMPANY_YEARS,
        "exactly_1080_v5_rows_produced": total_rows_produced == EXPECTED_TOTAL_ROWS if all_completed else False,
        "all_company_years_24_rows": all(cy.get("row_count") == EXPECTED_ROWS_PER_COMPANY_YEAR for cy in company_year_results),
        "duplicate_keys_zero": total_duplicate_keys == 0,
        "missing_lineage_zero": total_missing_lineage == 0,
        "availability_mismatches_zero": total_availability_mismatches == 0,
        "future_data_violations_zero": total_future_data_violations == 0,
        "all_four_target_cases_resolve": all_targets_resolved,
        "no_other_production_row_changes": all(len(cy.get("unexpected_differences", [])) == 0 for cy in company_year_results),
        "expected_review_required_after_future_load_is_zero":
            (global_counts_before["unique_review_required"] - len(target_metric_year_cases)) == 0 if all_completed else None,
        "databases_unchanged": databases_unchanged,
        "engine_invocations_exactly_45": total_engine_invocations == EXPECTED_COMPANY_YEARS,
        "v4_never_rerun": True,
    }

    # PASS requires overall_status PASS (no early stop) and every global check true, including the
    # "after future load" check being exactly True (not None -- None only occurs if not all_completed,
    # in which case overall_status is already FAIL and this whole expression short-circuits to FAIL).
    final_status = "PASS" if (
        overall_status == "PASS"
        and all(bool(v) for k, v in global_checks.items() if k != "expected_review_required_after_future_load_is_zero")
        and global_checks["expected_review_required_after_future_load_is_zero"] is True
    ) else "FAIL"

    runtime_seconds = round(time.perf_counter() - start_time, 2)
    active_execution_seconds = round(sum(cy.get("elapsed_seconds", 0) for cy in company_year_results), 2)
    slowest_five = sorted(company_year_results, key=lambda cy: cy.get("elapsed_seconds", 0), reverse=True)[:5]
    any_over_30s = any(cy.get("elapsed_seconds", 0) > SOFT_WARNING_SECONDS for cy in company_year_results)

    output.update({
        "status": final_status, "stop_reason": stop_reason,
        "global_counts_after": global_counts_after, "warehouse_facts_after": warehouse_facts_after,
        "databases_unchanged": databases_unchanged,
        "total_rows_produced": total_rows_produced, "global_checks": global_checks,
        "performance": {
            "runtime_seconds_wallclock": runtime_seconds, "active_execution_seconds": active_execution_seconds,
            "engine_invocations": total_engine_invocations, "engine_invocations_expected": EXPECTED_COMPANY_YEARS,
            "slowest_five_company_years": [
                {"ticker": cy["ticker"], "fiscal_year_end": cy["fiscal_year_end"], "elapsed_seconds": cy.get("elapsed_seconds")}
                for cy in slowest_five
            ],
            "any_company_year_over_30s": any_over_30s, "hard_timeout_seconds": HARD_TIMEOUT_SECONDS,
            "v4_rerun": False,
        },
        "runtime_seconds": runtime_seconds,
    })

    atomic_write_json(JSON_OUTPUT_PATH, output)
    write_csv(CSV_OUTPUT_PATH, company_year_results)
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")
    print(f"CSV written to {CSV_OUTPUT_PATH}")

    print("\n" + "=" * 100)
    print(f"FINAL: {final_status}")
    print(f"  Company-years completed: {len(company_year_results)}/{EXPECTED_COMPANY_YEARS}")
    print(f"  Total V5 rows produced: {total_rows_produced} (expected {EXPECTED_TOTAL_ROWS})")
    print(f"  Global checks: {global_checks}")
    print(f"  Active execution time: {active_execution_seconds}s, wall-clock: {runtime_seconds}s")
    print(f"  Slowest 5: {[(cy['ticker'], cy['fiscal_year_end'], cy.get('elapsed_seconds')) for cy in slowest_five]}")
    print("=" * 100)

    return output


if __name__ == "__main__":
    main()
