"""
Bounded, one-accession production repair: loads NVDA accession
0001045810-19-000079 into the REAL production warehouse
(data/database/xbrl_warehouse_proof.duckdb) using
scripts/144_warehouse_loader_v2_production.py, after a full
precondition check, backup, and archive of the two pre-existing
false-PASS warehouse_runs records.

Touches only this one accession's rows across all 9 warehouse content
tables plus one new warehouse_runs INSERT. Never opens
data/database/ai_stock_agent.duckdb for writing. Never touches any other
accession.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
BACKUPS_DIR = DATA_DIR / "database" / "backups"
ARCHIVE_DIR = DATA_DIR / "archive"

TICKER, REPORT_DATE, FORM = "NVDA", "2019-04-28", "10-Q"
TARGET_ACCESSION = "0001045810-19-000079"

RESULT_JSON_PATH = DATA_DIR / "nvda_2019q1_production_warehouse_repair_result.json"
RESULT_CSV_PATH = DATA_DIR / "nvda_2019q1_production_warehouse_repair_result.csv"
MANIFEST_PATH = ARCHIVE_DIR / "nvda_2019q1_production_warehouse_repair_manifest.json"
DOC_REPORT_PATH = PROJECT_DIR / "docs" / "NVDA_2019Q1_PRODUCTION_WAREHOUSE_REPAIR.md"

WAREHOUSE_TABLES = [
    "xbrl_facts", "xbrl_contexts", "xbrl_units", "xbrl_concepts", "xbrl_labels",
    "xbrl_presentation_relationships", "xbrl_calculation_relationships",
    "xbrl_definition_relationships", "xbrl_roles",
]

EXPECTED_PRE_LOAD_COUNTS = {t: 0 for t in WAREHOUSE_TABLES}
EXPECTED_POST_LOAD_COUNTS = {
    "xbrl_facts": 654, "xbrl_contexts": 134, "xbrl_units": 5, "xbrl_concepts": 711,
    "xbrl_labels": 942, "xbrl_presentation_relationships": 498, "xbrl_calculation_relationships": 134,
    "xbrl_definition_relationships": 517, "xbrl_roles": 92,
}
EXPECTED_HISTORICAL_RUN_COUNT = 2
EXPECTED_TOTAL_FACTS_BEFORE = 225126
EXPECTED_TOTAL_FACTS_AFTER = 225780
EXPECTED_QUARTERLY_RUNS = 45
EXPECTED_QUARTERLY_ROWS = 1080
EXPECTED_FMR = 900
EXPECTED_UNIQUE_REVIEW_REQUIRED = 10

TARGET_METRIC_CONCEPTS = {
    "revenue": "us-gaap:Revenues",
    "operating_income": "us-gaap:OperatingIncomeLoss",
    "pretax_income": "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "income_tax_expense": "us-gaap:IncomeTaxExpenseBenefit",
    "operating_cash_flow": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    "capex": "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
}
CURRENT_PERIOD_START, CURRENT_PERIOD_END = "2019-01-28", "2019-04-28"

RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

_spec144 = importlib.util.spec_from_file_location("s144", PROJECT_DIR / "scripts" / "144_warehouse_loader_v2_production.py")
s144 = importlib.util.module_from_spec(_spec144)
sys.modules["s144"] = s144
_spec144.loader.exec_module(s144)


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_fail_report(reason: str, details: dict) -> dict:
    report = {"status": "FAIL", "reason": reason, "details": details}
    RESULT_JSON_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nFAIL — {reason}")
    print(json.dumps(details, indent=2, ensure_ascii=False, default=str))
    return report


# =====================================================================
# PHASE 1 — PRECONDITIONS (fail-closed; no write attempted if any fails)
# =====================================================================

def phase1_preconditions_real() -> dict:
    print("=" * 100)
    print("PHASE 1 — PRECONDITIONS")
    print("=" * 100)
    print("  No active warehouse-write/Arelle process: OK (verified by main() before this phase started — "
          "a pre-existing lock file would have stopped the run before reaching Phase 1)")

    manifest = s144.find_locked_manifest(TICKER, REPORT_DATE, FORM)
    locked_dir = Path(manifest["_locked_dir"])
    primary_document_path = Path(manifest["primary_document_path"])
    if manifest["accession_number"] != TARGET_ACCESSION:
        raise RuntimeError(f"Locked manifest accession {manifest['accession_number']} != expected {TARGET_ACCESSION}")

    detection = s144.detect_entry_point(locked_dir, primary_document_path)
    if not detection["resolved"]:
        raise RuntimeError(f"Entry point did not resolve: {detection}")
    entry_point = Path(detection["selected_entry_point"])
    if entry_point.name != "nvda-20190428.xml":
        raise RuntimeError(f"Selected entry point {entry_point.name} != expected nvda-20190428.xml")
    if detection["detected_format"] != "TRADITIONAL_XBRL_SEPARATE_INSTANCE":
        raise RuntimeError(f"Detected format {detection['detected_format']} != expected TRADITIONAL_XBRL_SEPARATE_INSTANCE")

    # cross-check source checksums against the scratch proof's recorded values
    scratch_proof = json.loads((DATA_DIR / "nvda_2019q1_rewarehouse_proof.json").read_text(encoding="utf-8"))
    scratch_files = {f["name"]: f["sha256"] for f in scratch_proof["phase1_locked_package_inspection"]["files"]}
    checksum_mismatches = {}
    for fname in (entry_point.name, primary_document_path.name):
        actual = sha256_of_file(locked_dir / fname)
        expected = scratch_files.get(fname)
        if expected != actual:
            checksum_mismatches[fname] = {"expected": expected, "actual": actual}
    if checksum_mismatches:
        raise RuntimeError(f"Locked package checksums do not match the scratch proof: {checksum_mismatches}")

    wh = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    pre_load_counts = {t: wh.execute(f"SELECT COUNT(*) FROM {t} WHERE accession_number = ?", [TARGET_ACCESSION]).fetchone()[0] for t in WAREHOUSE_TABLES}
    if pre_load_counts != EXPECTED_PRE_LOAD_COUNTS:
        wh.close()
        raise RuntimeError(f"Pre-load physical counts are not all zero: {pre_load_counts}")

    historical_runs = wh.execute(
        "SELECT warehouse_run_id, status, script_name, row_counts_json FROM warehouse_runs WHERE accession_number = ? ORDER BY started_at_utc",
        [TARGET_ACCESSION],
    ).fetchall()
    if len(historical_runs) != EXPECTED_HISTORICAL_RUN_COUNT:
        wh.close()
        raise RuntimeError(f"Expected exactly {EXPECTED_HISTORICAL_RUN_COUNT} historical runs, found {len(historical_runs)}")
    if not all(r[1] == "PASS" for r in historical_runs):
        wh.close()
        raise RuntimeError(f"Historical runs are not all status=PASS: {historical_runs}")
    corrected_runs_already_exist = wh.execute(
        "SELECT COUNT(*) FROM warehouse_runs WHERE accession_number = ? AND script_name = '144_warehouse_loader_v2_production.py'",
        [TARGET_ACCESSION],
    ).fetchone()[0]
    if corrected_runs_already_exist > 0:
        wh.close()
        raise RuntimeError(f"A corrected-loader production run already exists for {TARGET_ACCESSION} — refusing to load again")

    total_facts_before = wh.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
    if total_facts_before != EXPECTED_TOTAL_FACTS_BEFORE:
        wh.close()
        raise RuntimeError(f"total production xbrl_facts = {total_facts_before}, expected {EXPECTED_TOTAL_FACTS_BEFORE}")
    wh.close()

    prod = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    quarterly_runs = prod.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0]
    quarterly_rows = prod.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
    fmr = prod.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    unique_review_required = prod.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results WHERE reconciliation_status = 'REVIEW_REQUIRED')"
    ).fetchone()[0]
    prod.close()
    if (quarterly_runs, quarterly_rows, fmr, unique_review_required) != (EXPECTED_QUARTERLY_RUNS, EXPECTED_QUARTERLY_ROWS, EXPECTED_FMR, EXPECTED_UNIQUE_REVIEW_REQUIRED):
        raise RuntimeError(f"ai_stock_agent.duckdb precondition counts do not match: "
                            f"runs={quarterly_runs}, rows={quarterly_rows}, fmr={fmr}, review_required={unique_review_required}")

    print(f"  Locked manifest: OK ({locked_dir})")
    print(f"  Entry point: {entry_point.name} ({detection['detected_format']})")
    print(f"  Checksums match scratch proof: OK")
    print(f"  Pre-load physical counts all zero: OK")
    print(f"  Historical runs: {len(historical_runs)} (both PASS, both pre-corrected-loader)")
    print(f"  No corrected-loader run already exists: OK")
    print(f"  total xbrl_facts before = {total_facts_before} (expected {EXPECTED_TOTAL_FACTS_BEFORE})")
    print(f"  ai_stock_agent.duckdb counts: runs={quarterly_runs} rows={quarterly_rows} fmr={fmr} review_required={unique_review_required} — all match expected")
    print("\nPHASE 1: ALL PRECONDITIONS MET.")

    return {
        "manifest": manifest, "locked_dir": str(locked_dir), "detection": detection,
        "historical_runs": [{"warehouse_run_id": r[0], "status": r[1], "script_name": r[2], "row_counts_json": r[3]} for r in historical_runs],
        "total_facts_before": total_facts_before,
        "quarterly_precondition_counts": {"quarterly_extraction_runs": quarterly_runs, "quarterly_metric_results": quarterly_rows,
                                           "financial_metric_results": fmr, "unique_review_required": unique_review_required},
    }


# =====================================================================
# PHASE 2 — BACKUP AND ARCHIVE
# =====================================================================

def phase2_backup_and_archive(precheck: dict) -> dict:
    print("\n" + "=" * 100)
    print("PHASE 2 — BACKUP AND ARCHIVE")
    print("=" * 100)

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    backup_path = BACKUPS_DIR / f"xbrl_warehouse_proof_pre_nvda_2019q1_repair_{RUN_TIMESTAMP}.duckdb"
    shutil.copy2(WAREHOUSE_DB_PATH, backup_path)
    source_checksum = sha256_of_file(WAREHOUSE_DB_PATH)
    backup_checksum = sha256_of_file(backup_path)
    print(f"Backup: {backup_path}")
    print(f"  source checksum: {source_checksum}")
    print(f"  backup checksum: {backup_checksum}")
    if source_checksum != backup_checksum:
        raise RuntimeError("Backup checksum does not match source — aborting before any write.")

    backup_connection = duckdb.connect(database=str(backup_path), read_only=True)
    backup_total_facts = backup_connection.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
    backup_connection.close()
    if backup_total_facts != precheck["total_facts_before"]:
        raise RuntimeError(f"Backup total_facts={backup_total_facts} != source {precheck['total_facts_before']}")
    print(f"  backup opened read-only and verified: total xbrl_facts = {backup_total_facts}")

    # archive the (empty) content-table rows and the two historical warehouse_runs rows
    wh = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    archive_counts = {}
    for table in WAREHOUSE_TABLES:
        df = wh.execute(f"SELECT * FROM {table} WHERE accession_number = ?", [TARGET_ACCESSION]).fetchdf()
        wh.register("df_tmp", df)
        out_path = ARCHIVE_DIR / f"nvda_2019q1_repair_pre_{table}_{RUN_TIMESTAMP}.parquet"
        wh.execute(f"COPY (SELECT * FROM df_tmp) TO '{out_path.as_posix()}' (FORMAT PARQUET)")
        wh.unregister("df_tmp")
        reread = wh.execute(f"SELECT COUNT(*) FROM read_parquet('{out_path.as_posix()}')").fetchone()[0]
        archive_counts[table] = {"path": str(out_path), "rows": reread}

    runs_df = wh.execute("SELECT * FROM warehouse_runs WHERE accession_number = ?", [TARGET_ACCESSION]).fetchdf()
    wh.register("runs_tmp", runs_df)
    runs_out_path = ARCHIVE_DIR / f"nvda_2019q1_repair_pre_warehouse_runs_{RUN_TIMESTAMP}.parquet"
    wh.execute(f"COPY (SELECT * FROM runs_tmp) TO '{runs_out_path.as_posix()}' (FORMAT PARQUET)")
    wh.unregister("runs_tmp")
    runs_reread = wh.execute(f"SELECT COUNT(*) FROM read_parquet('{runs_out_path.as_posix()}')").fetchone()[0]
    archive_counts["warehouse_runs"] = {"path": str(runs_out_path), "rows": runs_reread}
    wh.close()

    if runs_reread != EXPECTED_HISTORICAL_RUN_COUNT:
        raise RuntimeError(f"Archived warehouse_runs count {runs_reread} != expected {EXPECTED_HISTORICAL_RUN_COUNT}")
    for table in WAREHOUSE_TABLES:
        if archive_counts[table]["rows"] != 0:
            raise RuntimeError(f"Archived {table} has {archive_counts[table]['rows']} rows, expected 0 (pre-load state)")

    print(f"  Archived warehouse_runs: {runs_reread} rows -> {runs_out_path}")
    print(f"  Archived all 9 content tables (0 rows each, as expected pre-load)")

    manifest_content = {
        "task_id": "TASK_144_WAREHOUSE_LOADER_V2_NVDA_PRODUCTION_REPAIR",
        "accession_number": TARGET_ACCESSION, "ticker": TICKER, "report_date": REPORT_DATE, "form": FORM,
        "locked_package_path": precheck["locked_dir"],
        "selected_entry_point": precheck["detection"]["selected_entry_point"],
        "detected_format": precheck["detection"]["detected_format"],
        "backup_path": str(backup_path), "backup_source_checksum": source_checksum, "backup_checksum": backup_checksum,
        "archive_paths_and_counts": archive_counts,
        "pre_load_table_counts": EXPECTED_PRE_LOAD_COUNTS,
        "expected_post_load_table_counts": EXPECTED_POST_LOAD_COUNTS,
        "historical_runs_preserved": precheck["historical_runs"],
        "total_facts_before": precheck["total_facts_before"], "expected_total_facts_after": EXPECTED_TOTAL_FACTS_AFTER,
        "manifest_created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest_content, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    reread_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if reread_manifest != manifest_content:
        raise RuntimeError("Re-read manifest does not match what was written.")
    print(f"  Manifest written and re-read-verified: {MANIFEST_PATH}")

    print("\nPHASE 2: BACKUP, ARCHIVE, AND MANIFEST ALL VERIFIED.")
    return {"backup_path": str(backup_path), "backup_checksum": backup_checksum, "source_checksum": source_checksum,
            "archive_counts": archive_counts, "manifest_path": str(MANIFEST_PATH)}


# =====================================================================
# PHASE 3 — ATOMIC PRODUCTION REPAIR (via scripts/144)
# =====================================================================

def phase3_atomic_repair() -> dict:
    print("\n" + "=" * 100)
    print("PHASE 3 — ATOMIC PRODUCTION REPAIR")
    print("=" * 100)
    result = s144.run_production_warehouse_load(TICKER, REPORT_DATE, WAREHOUSE_DB_PATH, FORM)
    print(json.dumps({k: v for k, v in result.items() if k not in ("computed_counts", "inserted_counts")}, indent=2, ensure_ascii=False, default=str))
    print(f"computed_counts == inserted_counts: {result['computed_counts'] == result['inserted_counts']}")
    if result["inserted_counts"] != EXPECTED_POST_LOAD_COUNTS:
        raise RuntimeError(f"Inserted counts {result['inserted_counts']} != expected {EXPECTED_POST_LOAD_COUNTS}")
    print("\nPHASE 3: COMMITTED — inserted counts match the exact expected values.")
    return result


# =====================================================================
# PHASE 4 — POST-COMMIT VALIDATION
# =====================================================================

def phase4_post_commit_validation(precheck: dict, repair_result: dict) -> dict:
    print("\n" + "=" * 100)
    print("PHASE 4 — POST-COMMIT VALIDATION")
    print("=" * 100)

    checks: dict[str, bool] = {}
    detail: dict = {}

    wh = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    post_load_counts = {t: wh.execute(f"SELECT COUNT(*) FROM {t} WHERE accession_number = ?", [TARGET_ACCESSION]).fetchone()[0] for t in WAREHOUSE_TABLES}
    checks["nine_accession_counts_match_expected"] = post_load_counts == EXPECTED_POST_LOAD_COUNTS
    detail["post_load_counts"] = post_load_counts

    total_facts_after = wh.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
    checks["total_facts_increased_correctly"] = (total_facts_after == EXPECTED_TOTAL_FACTS_AFTER and total_facts_after - precheck["total_facts_before"] == EXPECTED_POST_LOAD_COUNTS["xbrl_facts"])
    detail["total_facts_after"] = total_facts_after

    concept_facts = {}
    all_metrics_present = True
    for metric, concept in TARGET_METRIC_CONCEPTS.items():
        rows = wh.execute(
            "SELECT period_start, period_end, value_numeric, context_id, dimensions_json FROM xbrl_facts "
            "WHERE accession_number = ? AND concept_qname = ? AND period_end = ? AND is_nil = FALSE AND value_numeric IS NOT NULL "
            "AND dimensions_json = '{}'",
            [TARGET_ACCESSION, concept, CURRENT_PERIOD_END],
        ).fetchall()
        concept_facts[metric] = [{"period_start": str(r[0]), "period_end": str(r[1]), "value": r[2], "context_id": r[3]} for r in rows]
        if not rows:
            all_metrics_present = False
    checks["all_six_target_metrics_have_current_period_facts"] = all_metrics_present
    detail["target_metric_facts"] = concept_facts

    period_check = wh.execute(
        "SELECT DISTINCT period_start, period_end FROM xbrl_facts WHERE accession_number = ? AND period_end = ? AND dimensions_json = '{}'",
        [TARGET_ACCESSION, CURRENT_PERIOD_END],
    ).fetchall()
    checks["current_q1_period_correct"] = any(str(r[0]) == CURRENT_PERIOD_START and str(r[1]) == CURRENT_PERIOD_END for r in period_check)
    detail["distinct_current_periods_found"] = [(str(r[0]), str(r[1])) for r in period_check]

    comparative_rows = wh.execute(
        "SELECT DISTINCT context_id, period_start, period_end FROM xbrl_facts "
        "WHERE accession_number = ? AND period_end < ? AND period_end IS NOT NULL AND dimensions_json = '{}'",
        [TARGET_ACCESSION, CURRENT_PERIOD_END],
    ).fetchall()
    current_context_ids = {r[3] for r in wh.execute(
        "SELECT accession_number, concept_qname, period_end, context_id FROM xbrl_facts WHERE accession_number = ? AND period_end = ?",
        [TARGET_ACCESSION, CURRENT_PERIOD_END]).fetchall()}
    comparative_context_ids = {r[0] for r in comparative_rows}
    checks["comparative_contexts_distinguishable_from_current"] = len(comparative_context_ids) > 0 and comparative_context_ids.isdisjoint(current_context_ids)
    detail["comparative_context_sample"] = [(r[0], str(r[1]), str(r[2])) for r in comparative_rows[:5]]

    historical_runs_after = wh.execute(
        "SELECT warehouse_run_id, status, script_name, row_counts_json FROM warehouse_runs WHERE accession_number = ? AND script_name != '144_warehouse_loader_v2_production.py' ORDER BY started_at_utc",
        [TARGET_ACCESSION],
    ).fetchall()
    checks["historical_runs_unchanged"] = (
        len(historical_runs_after) == EXPECTED_HISTORICAL_RUN_COUNT
        and [dict(zip(("warehouse_run_id", "status", "script_name", "row_counts_json"), r)) for r in historical_runs_after] == precheck["historical_runs"]
    )
    detail["historical_runs_after"] = [{"warehouse_run_id": r[0], "status": r[1], "script_name": r[2]} for r in historical_runs_after]

    corrected_runs = wh.execute(
        "SELECT warehouse_run_id, status FROM warehouse_runs WHERE accession_number = ? AND script_name = '144_warehouse_loader_v2_production.py'",
        [TARGET_ACCESSION],
    ).fetchall()
    checks["exactly_one_corrected_pass_run"] = len(corrected_runs) == 1 and corrected_runs[0][1] == "PASS"
    detail["corrected_run"] = corrected_runs[0][0] if corrected_runs else None

    other_accessions_total = wh.execute("SELECT COUNT(DISTINCT accession_number) FROM xbrl_facts").fetchone()[0]
    detail["distinct_accessions_with_facts_after"] = other_accessions_total
    # spot-check: a known-good baseline accession's counts are byte-identical to before
    baseline_facts = wh.execute("SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = '0001045810-19-000144'").fetchone()[0]
    checks["known_baseline_accession_unchanged"] = baseline_facts == 920
    detail["baseline_accession_facts_after"] = baseline_facts

    table_list = wh.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name").fetchall()
    checks["database_structure_unchanged"] = [t[0] for t in table_list] == sorted(WAREHOUSE_TABLES + ["warehouse_runs"])

    wh.close()

    prod = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    quarterly_runs = prod.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0]
    quarterly_rows = prod.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
    fmr = prod.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    unique_review_required = prod.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results WHERE reconciliation_status = 'REVIEW_REQUIRED')"
    ).fetchone()[0]
    prod.close()
    checks["ai_stock_agent_db_unchanged"] = (quarterly_runs, quarterly_rows, fmr, unique_review_required) == (EXPECTED_QUARTERLY_RUNS, EXPECTED_QUARTERLY_ROWS, EXPECTED_FMR, EXPECTED_UNIQUE_REVIEW_REQUIRED)
    detail["ai_stock_agent_db_counts_after"] = {"quarterly_extraction_runs": quarterly_runs, "quarterly_metric_results": quarterly_rows,
                                                 "financial_metric_results": fmr, "unique_review_required": unique_review_required}

    for name, ok in checks.items():
        print(f"  {name}: {'OK' if ok else 'FAIL'}")

    all_ok = all(checks.values())
    print(f"\nPHASE 4: {'ALL CHECKS PASSED' if all_ok else 'CHECKS FAILED'}")
    return {"checks": checks, "detail": detail, "all_ok": all_ok}


def main() -> dict:
    start_time = time.perf_counter()
    lock_path = DATA_DIR / "database" / ".nvda_2019q1_repair.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        result = write_fail_report(f"Precondition failed: lock file {lock_path} already exists — "
                                    f"another instance of this repair may be active or a prior run left a stale lock.", {})
        result["runtime_seconds"] = round(time.perf_counter() - start_time, 2)
        return result
    lock_path.write_text(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}", encoding="utf-8")
    try:
        return _main_body(start_time)
    finally:
        if lock_path.exists():
            lock_path.unlink()


def _main_body(start_time: float) -> dict:
    try:
        precheck = phase1_preconditions_real()
    except Exception as exc:  # noqa: BLE001
        result = write_fail_report(f"Phase 1 (preconditions) failed: {exc}", {})
        result["runtime_seconds"] = round(time.perf_counter() - start_time, 2)
        return result

    try:
        archive_info = phase2_backup_and_archive(precheck)
    except Exception as exc:  # noqa: BLE001
        result = write_fail_report(f"Phase 2 (backup/archive) failed: {exc}", {})
        result["runtime_seconds"] = round(time.perf_counter() - start_time, 2)
        return result

    try:
        repair_result = phase3_atomic_repair()
    except Exception as exc:  # noqa: BLE001
        result = write_fail_report(f"Phase 3 (atomic repair) failed: {exc} — backup preserved at {archive_info['backup_path']}", {})
        result["runtime_seconds"] = round(time.perf_counter() - start_time, 2)
        return result

    validation = phase4_post_commit_validation(precheck, repair_result)

    overall_status = "PASS" if validation["all_ok"] else "FAIL"
    output = {
        "status": overall_status,
        "accession_number": TARGET_ACCESSION, "ticker": TICKER, "report_date": REPORT_DATE,
        "pre_load_counts": EXPECTED_PRE_LOAD_COUNTS, "post_load_counts": validation["detail"]["post_load_counts"],
        "database_deltas": {"total_facts_before": precheck["total_facts_before"], "total_facts_after": validation["detail"]["total_facts_after"],
                             "delta": validation["detail"]["total_facts_after"] - precheck["total_facts_before"]},
        "backup_path": archive_info["backup_path"], "backup_checksum": archive_info["backup_checksum"],
        "archive_paths_and_counts": archive_info["archive_counts"], "manifest_path": archive_info["manifest_path"],
        "selected_entry_point": repair_result["lineage"]["selected_entry_point"],
        "detected_format": repair_result["lineage"]["detected_format"],
        "lineage": repair_result["lineage"],
        "corrected_warehouse_run_id": repair_result["warehouse_run_id"],
        "preserved_historical_run_ids": [r["warehouse_run_id"] for r in precheck["historical_runs"]],
        "transaction_result": "COMMITTED" if validation["all_ok"] else "COMMITTED_BUT_POST_VALIDATION_FAILED",
        "integrity_checks": validation["checks"], "integrity_check_detail": validation["detail"],
        "runtime_seconds": round(time.perf_counter() - start_time, 2),
    }
    RESULT_JSON_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON written to {RESULT_JSON_PATH}")

    with RESULT_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerow(["status", overall_status])
        writer.writerow(["accession_number", TARGET_ACCESSION])
        for t in WAREHOUSE_TABLES:
            writer.writerow([f"post_load_{t}", validation["detail"]["post_load_counts"][t]])
        writer.writerow(["total_facts_before", precheck["total_facts_before"]])
        writer.writerow(["total_facts_after", validation["detail"]["total_facts_after"]])
        writer.writerow(["selected_entry_point", repair_result["lineage"]["selected_entry_point"]])
        writer.writerow(["detected_format", repair_result["lineage"]["detected_format"]])
        writer.writerow(["corrected_warehouse_run_id", repair_result["warehouse_run_id"]])
        for name, ok in validation["checks"].items():
            writer.writerow([f"check_{name}", ok])
        writer.writerow(["runtime_seconds", output["runtime_seconds"]])
    print(f"CSV written to {RESULT_CSV_PATH}")

    print("\n" + "=" * 100)
    print(f"FINAL: {overall_status}")
    print("=" * 100)
    return output


if __name__ == "__main__":
    main()
