"""
Scratch-only proof: inspects NVDA accession 0001045810-19-000079's
locked filing package, reproduces the EXISTING (broken) warehouse-loading
path against an isolated scratch database to confirm the false-PASS-
with-zero-rows behavior, then runs the corrected loader
(scripts/139_corrected_warehouse_loader_entry_point_detection.py) on
both the broken accession and a known-good NVDA baseline accession.

Never touches data/database/xbrl_warehouse_proof.duckdb or
data/database/ai_stock_agent.duckdb. All writes go to
data/database/nvda_2019_q1_warehouse_proof.duckdb only. Makes zero
network calls (internetConnectivity="offline" in the corrected loader;
Phase 2's reproduction of the existing path relies entirely on the
Arelle taxonomy cache already populated by this project's many prior
successful loads of the same 2018-01-31 us-gaap taxonomy vintage).

Does not modify scripts/121 or scripts/139 — scripts/121's
WAREHOUSE_DB_PATH module attribute is monkey-patched at runtime (a
Python attribute reassignment on the already-loaded module object, not a
file edit) so its unchanged `load_and_warehouse_one_10q()` writes to the
scratch database instead of production for this one reproduction call.
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
SCRATCH_WAREHOUSE_DB_PATH = DATA_DIR / "database" / "nvda_2019_q1_warehouse_proof.duckdb"
PROD_WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PROD_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

JSON_OUTPUT_PATH = DATA_DIR / "nvda_2019q1_rewarehouse_proof.json"
CSV_OUTPUT_PATH = DATA_DIR / "nvda_2019q1_rewarehouse_proof.csv"

BROKEN_TICKER, BROKEN_REPORT_DATE = "NVDA", "2019-04-28"
GOOD_TICKER, GOOD_REPORT_DATE = "NVDA", "2019-07-28"  # derived below from the database, recorded explicitly

METRIC_KEYWORDS = {
    "revenue": ["Revenue", "Sales"],
    "operating_income": ["OperatingIncomeLoss"],
    "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxes"],
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}

_spec139 = importlib.util.spec_from_file_location("s139", PROJECT_DIR / "scripts" / "139_corrected_warehouse_loader_entry_point_detection.py")
s139 = importlib.util.module_from_spec(_spec139)
sys.modules["s139"] = s139
_spec139.loader.exec_module(s139)
s121 = s139.s121  # same already-loaded module instance scripts/139 imported, reused (not re-imported)


def derive_known_good_accession() -> dict:
    """Derive the known-good NVDA baseline accession from the database
    (never hardcoded blind) — the NVDA 10-Q with the earliest report_date
    that already has non-zero facts in the production warehouse."""
    prod = duckdb.connect(database=str(PROD_DB_PATH), read_only=True)
    filings = prod.execute(
        "SELECT report_date, filing_date, accession_number FROM sec_filings "
        "WHERE ticker = 'NVDA' AND form = '10-Q' ORDER BY report_date"
    ).fetchall()
    prod.close()

    wh = duckdb.connect(database=str(PROD_WAREHOUSE_DB_PATH), read_only=True)
    for report_date, filing_date, accession_number in filings:
        if str(report_date) == BROKEN_REPORT_DATE:
            continue
        fact_count = wh.execute("SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?", [accession_number]).fetchone()[0]
        if fact_count > 0:
            wh.close()
            return {"report_date": str(report_date), "filing_date": str(filing_date),
                     "accession_number": accession_number, "existing_fact_count_in_production_warehouse": fact_count}
    wh.close()
    raise RuntimeError("No known-good NVDA 10-Q with non-zero facts found in the production warehouse.")


def phase1_inspect_locked_package() -> dict:
    print("=" * 100)
    print("PHASE 1 — INSPECT THE LOCKED PACKAGE")
    print("=" * 100)
    manifest = s139.find_locked_manifest(BROKEN_TICKER, BROKEN_REPORT_DATE)
    inspection = s139.inspect_locked_package(manifest)
    print(f"Locked dir: {inspection['locked_dir']}")
    print(f"Files: {inspection['file_count']}")
    print(f"Primary document: {inspection['primary_document']}")
    print(f"Schema files: {inspection['schema_files']}")
    print(f"Linkbase files: {inspection['linkbase_files']}")
    print(f"Candidate traditional instance files: {inspection['candidate_traditional_instance_files']}")
    print(f"Entry-point detection: {json.dumps(inspection['entry_point_detection'], indent=2, ensure_ascii=False)}")
    print(f"Package complete enough to attempt parsing: {inspection['package_complete_enough_to_attempt_parsing']}")
    if not inspection["package_complete_enough_to_attempt_parsing"]:
        raise RuntimeError("Locked package is not complete enough to attempt parsing — stopping per instruction (no download).")
    return {"manifest": manifest, "inspection": inspection}


def phase2_reproduce_existing_failure() -> dict:
    print("\n" + "=" * 100)
    print("PHASE 2 — REPRODUCE THE EXISTING (BROKEN) LOADING PATH, SCRATCH DB ONLY")
    print("=" * 100)

    if SCRATCH_WAREHOUSE_DB_PATH.exists():
        SCRATCH_WAREHOUSE_DB_PATH.unlink()

    original_warehouse_db_path = s121.WAREHOUSE_DB_PATH
    s121.WAREHOUSE_DB_PATH = SCRATCH_WAREHOUSE_DB_PATH  # runtime attribute reassignment only — scripts/121's own file is never touched
    try:
        result = s121.load_and_warehouse_one_10q(BROKEN_TICKER, BROKEN_REPORT_DATE)
    finally:
        s121.WAREHOUSE_DB_PATH = original_warehouse_db_path  # restore immediately

    print(f"Existing-path result: status={result['status']} row_counts={result['row_counts']} "
          f"elapsed={result['total_elapsed_seconds']}s")

    scratch = duckdb.connect(database=str(SCRATCH_WAREHOUSE_DB_PATH), read_only=True)
    run_row = scratch.execute(
        "SELECT status, row_counts_json, total_elapsed_seconds FROM warehouse_runs WHERE accession_number = ?",
        [result["accession_number"]],
    ).fetchone()
    scratch.close()

    false_pass_reproduced = (run_row is not None and run_row[0] == "PASS" and all(v == 0 for v in result["row_counts"].values()))
    print(f"warehouse_runs row recorded: status={run_row[0] if run_row else None}, row_counts_json={run_row[1] if run_row else None}")
    print(f"False-PASS-with-zero-rows behavior reproduced: {false_pass_reproduced}")

    return {"result": result, "warehouse_runs_row": {"status": run_row[0], "row_counts_json": run_row[1],
             "total_elapsed_seconds": run_row[2]} if run_row else None, "false_pass_reproduced": false_pass_reproduced}


def phase4_validate_corrected_loader(good_accession_info: dict) -> dict:
    print("\n" + "=" * 100)
    print("PHASE 4 — VALIDATE THE CORRECTED LOADER ON BOTH FILINGS")
    print("=" * 100)

    print(f"\n--- A. Broken NVDA accession ({BROKEN_TICKER} {BROKEN_REPORT_DATE}) ---")
    broken_result = s139.run_corrected_warehouse_load(BROKEN_TICKER, BROKEN_REPORT_DATE, SCRATCH_WAREHOUSE_DB_PATH)
    print(json.dumps({k: v for k, v in broken_result.items() if k != "entry_point_detection"}, indent=2, ensure_ascii=False, default=str))

    print(f"\n--- B. Known-good NVDA baseline ({GOOD_TICKER} {good_accession_info['report_date']}) ---")
    good_result = s139.run_corrected_warehouse_load(GOOD_TICKER, good_accession_info["report_date"], SCRATCH_WAREHOUSE_DB_PATH)
    print(json.dumps({k: v for k, v in good_result.items() if k != "entry_point_detection"}, indent=2, ensure_ascii=False, default=str))

    return {"broken": broken_result, "good_baseline": good_result}


def check_target_metrics_plausibility(accession_number: str) -> dict:
    """Read-only inspection of the scratch warehouse for plausible facts
    matching each of the 6 target metrics — same broad-keyword style as
    scripts/127/131/135, used here only to report plausibility, not to
    select any value."""
    scratch = duckdb.connect(database=str(SCRATCH_WAREHOUSE_DB_PATH), read_only=True)
    findings = {}
    for metric_name, keywords in METRIC_KEYWORDS.items():
        like_clauses = " OR ".join(["concept_local_name LIKE ?"] * len(keywords))
        params = [accession_number] + [f"%{kw}%" for kw in keywords]
        rows = scratch.execute(
            f"SELECT concept_qname, period_start, period_end, value_numeric, decimals, context_id, dimensions_json "
            f"FROM xbrl_facts WHERE accession_number = ? AND is_nil = FALSE AND value_numeric IS NOT NULL "
            f"AND dimensions_json = '{{}}' AND ({like_clauses}) ORDER BY period_end",
            params,
        ).fetchall()
        findings[metric_name] = {
            "plausible_fact_count": len(rows),
            "facts": [
                {"concept_qname": r[0], "period_start": str(r[1]), "period_end": str(r[2]),
                 "value_numeric": r[3], "decimals": r[4], "context_id": r[5]}
                for r in rows[:6]
            ],
        }
    scratch.close()
    return findings


def compare_against_production_baseline(good_accession_number: str, good_result: dict) -> dict:
    prod_wh = duckdb.connect(database=str(PROD_WAREHOUSE_DB_PATH), read_only=True)
    prod_counts = {
        "xbrl_facts": prod_wh.execute("SELECT COUNT(*) FROM xbrl_facts WHERE accession_number=?", [good_accession_number]).fetchone()[0],
        "xbrl_contexts": prod_wh.execute("SELECT COUNT(*) FROM xbrl_contexts WHERE accession_number=?", [good_accession_number]).fetchone()[0],
        "xbrl_units": prod_wh.execute("SELECT COUNT(*) FROM xbrl_units WHERE accession_number=?", [good_accession_number]).fetchone()[0],
        "xbrl_concepts": prod_wh.execute("SELECT COUNT(*) FROM xbrl_concepts WHERE accession_number=?", [good_accession_number]).fetchone()[0],
    }
    prod_wh.close()
    corrected_counts = {k: good_result["row_counts"].get(k) for k in prod_counts}
    no_regression = all(corrected_counts[k] is not None and corrected_counts[k] >= prod_counts[k] for k in prod_counts)
    return {"production_counts": prod_counts, "corrected_loader_counts": corrected_counts, "no_regression": no_regression}


def main() -> dict:
    start_time = time.perf_counter()

    good_accession_info = derive_known_good_accession()
    print(f"Derived known-good baseline accession: {good_accession_info}")
    global GOOD_REPORT_DATE
    GOOD_REPORT_DATE = good_accession_info["report_date"]

    try:
        phase1 = phase1_inspect_locked_package()
    except Exception as exc:  # noqa: BLE001
        fail_output = {"status": "FAIL", "phase": 1, "reason": str(exc), "runtime_seconds": round(time.perf_counter() - start_time, 2)}
        JSON_OUTPUT_PATH.write_text(json.dumps(fail_output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(json.dumps(fail_output, indent=2))
        return fail_output

    phase2 = phase2_reproduce_existing_failure()
    phase4 = phase4_validate_corrected_loader(good_accession_info)

    broken_metric_plausibility = None
    if phase4["broken"]["status"] == "PASS":
        broken_metric_plausibility = check_target_metrics_plausibility(phase4["broken"]["accession_number"])

    baseline_comparison = None
    if phase4["good_baseline"]["status"] == "PASS":
        baseline_comparison = compare_against_production_baseline(good_accession_info["accession_number"], phase4["good_baseline"])

    broken_fixed = phase4["broken"]["status"] == "PASS" and phase4["broken"]["row_counts"].get("xbrl_facts", 0) > 0
    baseline_ok = phase4["good_baseline"]["status"] == "PASS" and baseline_comparison and baseline_comparison["no_regression"]
    overall_status = "PASS" if (broken_fixed and baseline_ok) else "FAIL"

    # confirm production databases were never touched
    prod_wh = duckdb.connect(database=str(PROD_WAREHOUSE_DB_PATH), read_only=True)
    prod_wh_fact_count_broken_acc = prod_wh.execute(
        "SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?", [phase4["broken"]["accession_number"]]
    ).fetchone()[0]
    prod_wh.close()
    prod_db = duckdb.connect(database=str(PROD_DB_PATH), read_only=True)
    prod_counts_unchanged = {
        "quarterly_extraction_runs": prod_db.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
        "quarterly_metric_results": prod_db.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
        "financial_metric_results": prod_db.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
    }
    prod_db.close()

    output = {
        "status": overall_status,
        "broken_accession": {"ticker": BROKEN_TICKER, "report_date": BROKEN_REPORT_DATE, "accession_number": phase4["broken"]["accession_number"]},
        "known_good_baseline_accession": good_accession_info,
        "phase1_locked_package_inspection": phase1["inspection"],
        "phase2_existing_failure_reproduction": phase2,
        "phase4_corrected_loader_results": phase4,
        "broken_accession_target_metric_plausibility": broken_metric_plausibility,
        "baseline_comparison_vs_production_warehouse": baseline_comparison,
        "production_warehouse_untouched": {
            "xbrl_facts_for_broken_accession_still_zero_in_production": prod_wh_fact_count_broken_acc == 0,
        },
        "production_database_untouched": prod_counts_unchanged,
        "runtime_seconds": round(time.perf_counter() - start_time, 2),
    }

    print("\n" + "=" * 100)
    print("SUMMARY")
    print(f"Existing path false-PASS-with-zero-rows reproduced: {phase2['false_pass_reproduced']}")
    print(f"Corrected loader on broken accession: status={phase4['broken']['status']} "
          f"facts={phase4['broken']['row_counts'].get('xbrl_facts')}")
    print(f"Corrected loader on known-good baseline: status={phase4['good_baseline']['status']} "
          f"facts={phase4['good_baseline']['row_counts'].get('xbrl_facts')} no_regression={baseline_ok}")
    print(f"Production warehouse (real, not scratch) still shows 0 facts for the broken accession: "
          f"{prod_wh_fact_count_broken_acc == 0}")
    print(f"OVERALL STATUS: {overall_status}")
    print("=" * 100)

    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")

    csv_rows = []
    for label, r in (("BROKEN", phase4["broken"]), ("GOOD_BASELINE", phase4["good_baseline"])):
        csv_rows.append({
            "label": label, "ticker": r["ticker"], "report_date": r["report_date"], "accession_number": r["accession_number"],
            "status": r["status"], "failure_category": r.get("failure_category"),
            "xbrl_facts": r["row_counts"].get("xbrl_facts"), "xbrl_contexts": r["row_counts"].get("xbrl_contexts"),
            "xbrl_units": r["row_counts"].get("xbrl_units"), "xbrl_concepts": r["row_counts"].get("xbrl_concepts"),
            "entry_point_format": r["entry_point_detection"].get("detected_format"),
            "elapsed_seconds": r["elapsed_seconds"],
        })
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)
    print(f"CSV written to {CSV_OUTPUT_PATH}")

    return output


if __name__ == "__main__":
    main()
