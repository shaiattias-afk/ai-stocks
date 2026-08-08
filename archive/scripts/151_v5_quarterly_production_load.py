"""
TASK_149_V5_PRODUCTION_LOAD_BUILD — the production-load script for
quarterly engine V5 (scripts/148), for exactly the 3 target company-years
identified and regression-proven by TASK_147/TASK_148:
CRWD 2022-01-31, MU 2021-09-02, PANW 2021-07-31.

Two mutually exclusive modes:

  --check-only  Fully read-only. Verifies the script imports/compiles,
                opens both production databases read-only, verifies every
                execution precondition, derives the 3 target company-years
                from the saved TASK_148 regression JSON (cross-checked
                against a fixed fail-closed expectation, never the other
                way around), verifies the expected 72-row / 16-changed /
                56-unchanged replacement scope, and verifies output/
                archive paths can be constructed. No engine invocation, no
                backup, no database write, no archive write. Writes its
                own report to data/v5_production_load_build_validation.json.

  --execute     The real production load. Backs up ai_stock_agent.duckdb,
                archives the 3 old runs + 72 old rows, re-runs engine V5
                exactly once per target company-year (subprocess, real
                45s OS timeout), cross-checks every fresh result against
                both the saved TASK_148 regression result and current
                production, then replaces all 3 company-years (72 rows,
                not just the 16 changed ones) in one atomic transaction,
                with full pre-commit and post-commit validation. Refuses
                to start if a live PID lock exists; removes a stale one
                only after proving the PID is not active. Never runs V4.
                Never processes a non-target company-year.

This script never launches a background process itself and never resumes
a previous failed --execute run silently.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DOCS_DIR = PROJECT_DIR / "docs"
LOGS_DIR = PROJECT_DIR / "logs"
BACKUPS_DIR = DATA_DIR / "database" / "backups"
ARCHIVE_DIR = DATA_DIR / "archive"
SCRATCH_DIR = DATA_DIR / "_scratch_v5_production_load"

PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
ANNUAL_V1_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"
V5_ENGINE_SCRIPT = PROJECT_DIR / "scripts" / "148_quarterly_engine_v5_standard_gaap_fallback.py"

REGRESSION_JSON_PATH = DATA_DIR / "v5_final_release_regression.json"
REGRESSION_CSV_PATH = DATA_DIR / "v5_final_release_regression.csv"
REGRESSION_MD_PATH = DOCS_DIR / "V5_FINAL_RELEASE_REGRESSION.md"

CHECK_ONLY_OUTPUT_PATH = DATA_DIR / "v5_production_load_build_validation.json"
LOG_PATH = LOGS_DIR / "v5_production_load.log"
PID_LOCK_PATH = DATA_DIR / "v5_production_load.pid"
CHECKPOINT_PATH = DATA_DIR / "v5_production_load_checkpoint.json"
RESULT_JSON_PATH = DATA_DIR / "v5_production_load_result.json"
RESULT_CSV_PATH = DATA_DIR / "v5_production_load_result.csv"
MANIFEST_PATH = ARCHIVE_DIR / "v5_production_load_manifest.json"

ENGINE_VERSION_V5 = "QUARTERLY_ENGINE_V5_STANDARD_GAAP_ALLOW_LIST"
SCHEMA_VERSION = "quarterly_v1"
HARD_TIMEOUT_SECONDS = 45

EXPECTED_PRE_RUNS = 45
EXPECTED_PRE_ROWS = 1080
EXPECTED_PRE_FMR = 900
EXPECTED_PRE_REVIEW_REQUIRED = 4
EXPECTED_POST_REVIEW_REQUIRED = 0
EXPECTED_WAREHOUSE_FACTS = 225780
EXPECTED_ROWS_PER_COMPANY_YEAR = 24
EXPECTED_TARGET_COMPANY_YEARS = 3
EXPECTED_TARGET_ROWS = EXPECTED_TARGET_COMPANY_YEARS * EXPECTED_ROWS_PER_COMPANY_YEAR  # 72
EXPECTED_CHANGED_ROWS = 16
EXPECTED_UNCHANGED_ROWS = EXPECTED_TARGET_ROWS - EXPECTED_CHANGED_ROWS  # 56

# Fail-closed cross-check ONLY -- the derivation source of truth is always
# data/v5_final_release_regression.json, never this literal.
EXPECTED_TARGET_METRIC_YEAR_CASES = frozenset({
    ("CRWD", "2022-01-31", "pretax_income"),
    ("MU", "2021-09-02", "pretax_income"),
    ("PANW", "2021-07-31", "pretax_income"),
    ("PANW", "2021-07-31", "revenue"),
})
EXPECTED_TARGET_COMPANY_YEAR_KEYS = frozenset({(t, fy) for t, fy, _ in EXPECTED_TARGET_METRIC_YEAR_CASES})

REQUIRED_IDENTICAL_FIELDS = ("value", "unit", "concept_qname", "extraction_basis",
                             "reconciliation_status", "availability_date", "accession_number")

_spec_v5 = importlib.util.spec_from_file_location("s148", V5_ENGINE_SCRIPT)
s148 = importlib.util.module_from_spec(_spec_v5)
sys.modules["s148"] = s148
_spec_v5.loader.exec_module(s148)

_spec_v150 = importlib.util.spec_from_file_location(
    "s150", PROJECT_DIR / "scripts" / "150_v5_final_release_regression.py"
)
s150 = importlib.util.module_from_spec(_spec_v150)
sys.modules["s150"] = s150
# scripts/150 guards its own regression run behind `if __name__ == "__main__"`,
# so importing it only defines functions/constants -- no side effects.
_spec_v150.loader.exec_module(s150)


# =====================================================================
# SMALL SHARED HELPERS
# =====================================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log(message: str, also_print: bool = True) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{utc_now_iso()}] {message}"
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if also_print:
        print(message)


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
    reread = json.loads(path.read_text(encoding="utf-8"))
    if reread != json.loads(json.dumps(data, default=str)):
        raise RuntimeError(f"Atomic write verification failed for {path}")


def get_global_counts(prod_connection) -> dict:
    return s150.get_global_counts(prod_connection)


# =====================================================================
# PID LOCK
# =====================================================================

def is_pid_active(pid: int) -> bool:
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return True  # fail closed: if we cannot determine, assume active
    return str(pid) in completed.stdout


def check_pid_lock_status(exclude_pid: int | None = None) -> dict:
    """Reads the PID lock file and classifies it into exactly one of:
    no lock (is_free=True), the caller's own lock (is_own_lock=True,
    is_free=True -- NOT another writer), a live foreign lock
    (is_free=False), a stale foreign lock (is_free=True, is_stale=True),
    or a malformed/unreadable lock file (is_free=False -- fail closed,
    never silently treated as absent or stale).

    `exclude_pid` should be the CALLING process's own PID when this is
    used to answer "is some OTHER process writing right now" (e.g. from
    the execution-preconditions gate, called after this same process has
    already acquired its own lock). Leave it None when the question is
    instead "is anyone at all holding the lock" (e.g. from
    acquire_pid_lock, before this process has claimed it)."""
    if not PID_LOCK_PATH.exists():
        return {"is_free": True, "existing_pid": None, "pid_active": None, "is_own_lock": False, "is_stale": False}
    try:
        content = json.loads(PID_LOCK_PATH.read_text(encoding="utf-8"))
        existing_pid = content["pid"]
        if not isinstance(existing_pid, int) or isinstance(existing_pid, bool):
            raise ValueError(f"'pid' field is not an integer: {existing_pid!r}")
    except Exception as exc:  # noqa: BLE001
        return {"is_free": False, "existing_pid": None, "pid_active": None, "is_own_lock": False, "is_stale": False,
                "reason": f"PID lock file unreadable/malformed: {exc}"}

    if exclude_pid is not None and existing_pid == exclude_pid:
        return {"is_free": True, "existing_pid": existing_pid, "pid_active": True, "is_own_lock": True, "is_stale": False}

    active = is_pid_active(existing_pid)
    return {"is_free": not active, "existing_pid": existing_pid, "pid_active": active,
            "is_own_lock": False, "is_stale": not active}


def acquire_pid_lock() -> None:
    # No exclude_pid here: at this point THIS process has not claimed the
    # lock yet, so any PID found in the file -- including, in the
    # vanishingly unlikely case of PID reuse, one equal to our own --
    # belongs to a prior process and must be evaluated as such.
    status = check_pid_lock_status()
    if status["existing_pid"] is None and not status["is_free"]:
        raise RuntimeError(f"PID lock file exists but is malformed/unreadable ({status.get('reason')}) — refusing to start.")
    if status["existing_pid"] is not None and status["pid_active"]:
        raise RuntimeError(f"A live PID lock already exists (pid={status['existing_pid']}) — refusing to start.")
    if status["existing_pid"] is not None and status["is_stale"]:
        log(f"Removing stale PID lock (pid={status['existing_pid']} is not active).")
        PID_LOCK_PATH.unlink(missing_ok=True)
    atomic_write_json(PID_LOCK_PATH, {"pid": os.getpid(), "started_at": utc_now_iso()})
    log(f"PID lock acquired (pid={os.getpid()}).")


def release_pid_lock() -> None:
    if PID_LOCK_PATH.exists():
        PID_LOCK_PATH.unlink(missing_ok=True)
        log("PID lock released.")


# =====================================================================
# REGRESSION ARTIFACT VALIDATION (shared by both modes)
# =====================================================================

def validate_regression_artifacts() -> tuple[bool, dict]:
    detail: dict = {}
    for path in (REGRESSION_JSON_PATH, REGRESSION_CSV_PATH, REGRESSION_MD_PATH):
        if not path.exists():
            return False, {"reason": f"missing regression artifact: {path}"}

    regression = json.loads(REGRESSION_JSON_PATH.read_text(encoding="utf-8"))
    detail["regression_status"] = regression.get("status")
    detail["global_checks"] = regression.get("global_checks")
    ok = regression.get("status") == "PASS" and bool(regression.get("global_checks")) and all(regression["global_checks"].values())
    detail["artifact_hashes"] = {
        "json": sha256_of_file(REGRESSION_JSON_PATH),
        "csv": sha256_of_file(REGRESSION_CSV_PATH),
        "md": sha256_of_file(REGRESSION_MD_PATH),
    }
    return ok, {"regression": regression, **detail}


def derive_and_cross_check_targets(regression: dict) -> tuple[bool, list[dict], set, dict]:
    """Derives target metric-year cases + company-years from the saved
    regression JSON (source of truth), then cross-checks against the
    fixed EXPECTED_* sets as a fail-closed sanity check only."""
    derived_cases = {(t, fy, m) for t, fy, m in regression["target_metric_year_cases"]}
    derived_company_year_keys = {(t, fy) for t, fy, _ in derived_cases}

    cross_check_ok = (
        derived_cases == EXPECTED_TARGET_METRIC_YEAR_CASES
        and derived_company_year_keys == EXPECTED_TARGET_COMPANY_YEAR_KEYS
        and len(derived_company_year_keys) == EXPECTED_TARGET_COMPANY_YEARS
    )

    target_company_years = []
    for cy in regression["company_year_results"]:
        key = (cy["ticker"], cy["fiscal_year_end"])
        if key in derived_company_year_keys:
            target_company_years.append(cy)
    target_company_years.sort(key=lambda cy: (cy["ticker"], cy["fiscal_year_end"]))

    detail = {
        "derived_cases": sorted(derived_cases), "derived_company_year_keys": sorted(derived_company_year_keys),
        "expected_cases": sorted(EXPECTED_TARGET_METRIC_YEAR_CASES),
        "expected_company_year_keys": sorted(EXPECTED_TARGET_COMPANY_YEAR_KEYS),
        "cross_check_passed": cross_check_ok,
    }
    return cross_check_ok, target_company_years, derived_cases, detail


def verify_replacement_scope(target_company_years: list[dict]) -> tuple[bool, dict]:
    if len(target_company_years) != EXPECTED_TARGET_COMPANY_YEARS:
        return False, {"reason": f"expected {EXPECTED_TARGET_COMPANY_YEARS} target company-years, found {len(target_company_years)}"}

    total_rows = sum(cy.get("row_count", 0) for cy in target_company_years)
    total_changed = sum(len(cy.get("expected_changes", [])) for cy in target_company_years)
    total_unchanged = total_rows - total_changed
    per_cy_24 = all(cy.get("row_count") == EXPECTED_ROWS_PER_COMPANY_YEAR for cy in target_company_years)
    no_unexpected = all(len(cy.get("unexpected_differences", [])) == 0 for cy in target_company_years)
    all_pass = all(cy.get("status") == "PASS" for cy in target_company_years)

    ok = (
        total_rows == EXPECTED_TARGET_ROWS and total_changed == EXPECTED_CHANGED_ROWS
        and total_unchanged == EXPECTED_UNCHANGED_ROWS and per_cy_24 and no_unexpected and all_pass
    )
    detail = {
        "total_rows": total_rows, "total_changed": total_changed, "total_unchanged": total_unchanged,
        "expected_total_rows": EXPECTED_TARGET_ROWS, "expected_changed": EXPECTED_CHANGED_ROWS,
        "expected_unchanged": EXPECTED_UNCHANGED_ROWS, "per_company_year_24_rows": per_cy_24,
        "no_unexpected_differences_in_regression": no_unexpected, "all_target_company_years_pass": all_pass,
    }
    return ok, detail


def verify_paths_constructible() -> tuple[bool, dict]:
    paths_to_check = {
        "backups_dir": BACKUPS_DIR, "archive_dir": ARCHIVE_DIR, "scratch_dir": SCRATCH_DIR,
        "logs_dir": LOGS_DIR, "manifest_path": MANIFEST_PATH, "pid_lock_path": PID_LOCK_PATH,
        "checkpoint_path": CHECKPOINT_PATH, "result_json_path": RESULT_JSON_PATH,
        "result_csv_path": RESULT_CSV_PATH, "log_path": LOG_PATH,
    }
    detail = {}
    ok = True
    for name, path in paths_to_check.items():
        parent = path if path.suffix == "" else path.parent
        grandparent_exists = parent.parent.exists()
        detail[name] = {"path": str(path), "parent_exists_or_creatable": grandparent_exists}
        if not grandparent_exists:
            ok = False
    return ok, detail


# =====================================================================
# PRODUCTION PRECONDITIONS (shared by --check-only reporting and the
# real --execute gate)
# =====================================================================

def check_execution_preconditions(
    prod_connection, warehouse_connection, target_company_years: list[dict], target_metric_cases: set,
) -> tuple[bool, dict]:
    checks: dict = {}
    counts = get_global_counts(prod_connection)
    checks["quarterly_extraction_runs_45"] = counts["quarterly_extraction_runs"] == EXPECTED_PRE_RUNS
    checks["quarterly_metric_results_1080"] = counts["quarterly_metric_results"] == EXPECTED_PRE_ROWS
    checks["financial_metric_results_900"] = counts["financial_metric_results"] == EXPECTED_PRE_FMR
    checks["unique_review_required_4"] = counts["unique_review_required"] == EXPECTED_PRE_REVIEW_REQUIRED

    actual_review_required = set(
        prod_connection.execute(
            "SELECT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results WHERE reconciliation_status = 'REVIEW_REQUIRED'"
        ).fetchall()
    )
    checks["exact_four_cases_review_required"] = actual_review_required == target_metric_cases

    one_run_each, twentyfour_rows_each, no_existing_v5_run = True, True, True
    per_company_year_detail = []
    for cy in target_company_years:
        ticker, fiscal_year_end = cy["ticker"], cy["fiscal_year_end"]
        runs = prod_connection.execute(
            "SELECT run_id, engine_version FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?",
            [ticker, fiscal_year_end],
        ).fetchall()
        row_count = prod_connection.execute(
            "SELECT COUNT(*) FROM quarterly_metric_results r JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
            "WHERE e.ticker = ? AND e.fiscal_year_end = ?", [ticker, fiscal_year_end],
        ).fetchone()[0]
        has_one_run = len(runs) == 1
        has_24_rows = row_count == EXPECTED_ROWS_PER_COMPANY_YEAR
        has_no_v5_run = all(engine_version != ENGINE_VERSION_V5 for _, engine_version in runs)
        one_run_each = one_run_each and has_one_run
        twentyfour_rows_each = twentyfour_rows_each and has_24_rows
        no_existing_v5_run = no_existing_v5_run and has_no_v5_run
        per_company_year_detail.append({"ticker": ticker, "fiscal_year_end": fiscal_year_end,
                                         "run_count": len(runs), "row_count": row_count, "has_v5_run": not has_no_v5_run})
    checks["one_active_run_per_target_company_year"] = one_run_each
    checks["24_active_rows_per_target_company_year"] = twentyfour_rows_each
    checks["no_existing_v5_run_for_any_target"] = no_existing_v5_run

    warehouse_facts = warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
    checks["warehouse_facts_unchanged"] = warehouse_facts == EXPECTED_WAREHOUSE_FACTS

    regression_ok, _regression_detail = validate_regression_artifacts()
    checks["regression_artifacts_valid"] = regression_ok

    # exclude_pid=os.getpid(): this process may already hold its own valid
    # lock (acquire_pid_lock runs before this precondition gate in
    # --execute) -- that must never be mistaken for another writer.
    pid_status = check_pid_lock_status(exclude_pid=os.getpid())
    checks["no_other_production_write_process_active"] = pid_status["is_free"]

    detail = {"global_counts": counts, "actual_review_required_cases": sorted(actual_review_required),
              "per_company_year": per_company_year_detail, "warehouse_facts": warehouse_facts,
              "pid_lock_status": pid_status}
    return all(checks.values()), {"checks": checks, "detail": detail}


# =====================================================================
# ROW EXTRACTION / COMPARISON (reuses scripts/150 where possible, adds
# the fuller field-set comparison this task specifically requires)
# =====================================================================

def rows_from_v5_output_full(engine_output: dict) -> list[dict]:
    rows = s150.rows_from_v5_output(engine_output)
    for row in rows:
        row["unit"] = "iso4217:USD"
    return rows


def get_production_rows_full(prod_connection, run_id: str) -> dict[tuple[str, str], dict]:
    rows = prod_connection.execute(
        "SELECT metric_name, fiscal_quarter, value, unit, extraction_basis, reconciliation_status, "
        "availability_date, concept_qname, accession_number, (lineage_json IS NOT NULL) AS has_lineage "
        "FROM quarterly_metric_results WHERE run_id = ?", [run_id],
    ).fetchall()
    out = {}
    for metric_name, fiscal_quarter, value, unit, extraction_basis, reconciliation_status, availability_date, concept_qname, accession_number, has_lineage in rows:
        out[(metric_name, fiscal_quarter)] = {
            "value": value, "unit": unit, "extraction_basis": extraction_basis,
            "reconciliation_status": reconciliation_status, "availability_date": availability_date,
            "concept_qname": concept_qname, "accession_number": accession_number, "has_lineage": has_lineage,
        }
    return out


def rows_differ_full(v5_row: dict, prod_row: dict) -> list[str]:
    diffs = []
    for field in REQUIRED_IDENTICAL_FIELDS:
        if field == "value":
            if not s150.values_equal(v5_row.get("value"), prod_row.get("value")):
                diffs.append(field)
        elif v5_row.get(field) != prod_row.get(field):
            diffs.append(field)
    return diffs


def compare_against_regression_and_production(
    engine_output: dict, regression_cy_entry: dict, prod_rows: dict, target_metrics: set[str],
) -> tuple[bool, dict]:
    fresh_rows = rows_from_v5_output_full(engine_output)
    changed_rows, unchanged_rows, unexpected = [], [], []

    for row in fresh_rows:
        key = (row["metric_name"], row["fiscal_quarter"])
        prod_row = prod_rows.get(key)
        if prod_row is None:
            unexpected.append({"key": key, "reason": "no matching production row"})
            continue
        diffs = rows_differ_full(row, prod_row)
        if not diffs:
            unchanged_rows.append({"key": key})
            continue
        if row["metric_name"] not in target_metrics:
            unexpected.append({"key": key, "reason": "non-target row differs from production", "diffs": diffs,
                                "v5_row": row, "production_row": prod_row})
            continue
        changed_rows.append({"key": key, "diffs": diffs, "v5_row": row, "production_row": prod_row})

    regression_changes_by_key = {tuple(c["key"]): c["v5_row"] for c in regression_cy_entry.get("expected_changes", [])}
    mismatches_vs_regression = []
    for change in changed_rows:
        saved = regression_changes_by_key.get(change["key"])
        if saved is None:
            mismatches_vs_regression.append({"key": change["key"], "reason": "fresh V5 changed a row the saved regression did not expect"})
            continue
        fresh_row = change["v5_row"]
        if not (
            s150.values_equal(fresh_row["value"], saved["value"])
            and fresh_row["extraction_basis"] == saved["extraction_basis"]
            and fresh_row["reconciliation_status"] == saved["reconciliation_status"]
            and fresh_row["availability_date"] == saved["availability_date"]
            and fresh_row["concept_qname"] == saved["concept_qname"]
            and fresh_row["accession_number"] == saved["accession_number"]
        ):
            mismatches_vs_regression.append({"key": change["key"], "fresh": fresh_row, "saved_regression": saved})

    changed_keys = {c["key"] for c in changed_rows}
    missing_from_fresh = [k for k in regression_changes_by_key if k not in changed_keys]
    if missing_from_fresh:
        mismatches_vs_regression.append({"reason": "saved regression expected these keys to change but the fresh run did not", "keys": missing_from_fresh})

    ok = len(unexpected) == 0 and len(mismatches_vs_regression) == 0 and len(fresh_rows) == EXPECTED_ROWS_PER_COMPANY_YEAR
    return ok, {"row_count": len(fresh_rows), "changed_rows": changed_rows, "unchanged_rows": unchanged_rows,
                "unexpected": unexpected, "mismatches_vs_regression": mismatches_vs_regression}


# =====================================================================
# --check-only MODE
# =====================================================================

def run_check_only() -> dict:
    start = time.perf_counter()
    checks: dict = {}
    detail: dict = {}

    try:
        importlib.import_module  # sanity: this module already imported s148/s150 successfully above
        checks["engine_and_regression_scripts_import"] = True
    except Exception as exc:  # noqa: BLE001
        checks["engine_and_regression_scripts_import"] = False
        detail["import_error"] = str(exc)

    regression_ok, regression_detail = validate_regression_artifacts()
    checks["regression_artifacts_valid"] = regression_ok
    detail["regression"] = {k: v for k, v in regression_detail.items() if k != "regression"}

    target_company_years, target_metric_cases = [], set()
    if regression_ok:
        cross_check_ok, target_company_years, target_metric_cases, derivation_detail = derive_and_cross_check_targets(
            regression_detail["regression"]
        )
        checks["target_derivation_matches_expected"] = cross_check_ok
        detail["derivation"] = derivation_detail

        scope_ok, scope_detail = verify_replacement_scope(target_company_years)
        checks["replacement_scope_verified"] = scope_ok
        detail["replacement_scope"] = scope_detail
    else:
        checks["target_derivation_matches_expected"] = False
        checks["replacement_scope_verified"] = False

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    try:
        if target_metric_cases:
            preconditions_ok, preconditions_detail = check_execution_preconditions(
                prod_connection, warehouse_connection, target_company_years, target_metric_cases
            )
        else:
            preconditions_ok, preconditions_detail = False, {"reason": "target cases could not be derived"}
        checks["execution_preconditions_met"] = preconditions_ok
        detail["execution_preconditions"] = preconditions_detail
    finally:
        prod_connection.close()
        warehouse_connection.close()

    paths_ok, paths_detail = verify_paths_constructible()
    checks["paths_constructible"] = paths_ok
    detail["paths"] = paths_detail

    pid_status = check_pid_lock_status(exclude_pid=os.getpid())
    checks["no_live_pid_lock"] = pid_status["is_free"]
    detail["pid_lock_status"] = pid_status

    overall = all(checks.values())
    runtime = round(time.perf_counter() - start, 3)

    output = {
        "mode": "check-only", "status": "PASS" if overall else "FAIL", "checks": checks, "detail": detail,
        "target_company_years": [{"ticker": cy["ticker"], "fiscal_year_end": cy["fiscal_year_end"]} for cy in target_company_years],
        "target_metric_year_cases": sorted(target_metric_cases),
        "engine_invoked": False, "backup_performed": False, "database_written": False, "archive_written": False,
        "runtime_seconds": runtime, "checked_at": utc_now_iso(),
    }
    atomic_write_json(CHECK_ONLY_OUTPUT_PATH, output)
    log(f"check-only run: status={output['status']} runtime={runtime}s -> {CHECK_ONLY_OUTPUT_PATH}", also_print=False)

    print("=" * 100)
    print(f"CHECK-ONLY: {output['status']}  (runtime {runtime}s)")
    for name, value in checks.items():
        print(f"  {name}: {'OK' if value else 'FAIL'}")
    print(f"  Target company-years: {output['target_company_years']}")
    print("=" * 100)
    return output


# =====================================================================
# --execute MODE (built fully; never invoked by this task)
# =====================================================================

def enrich_target_company_years_with_accessions(prod_connection, target_company_years: list[dict]) -> list[dict]:
    """The saved regression artifact's company_year_results entries carry
    ticker/fiscal_year_end/run_id/status/row-comparison fields only --
    NOT q1/q2/q3/fy_accession (scripts/150 never stored them there).
    Accessions must instead be read from the CURRENT production run being
    replaced (quarterly_extraction_runs), which is also the exact row the
    atomic transaction below independently reconfirms against immediately
    before delete -- so this is not a new trust boundary, only the
    correct existing source for lineage already relied on elsewhere.
    Validates all four quarter/annual accession fields consistently (not
    only Q1) and fails closed if any are genuinely missing."""
    enriched = []
    for cy in target_company_years:
        row = prod_connection.execute(
            "SELECT run_id, q1_accession, q2_accession, q3_accession, fy_accession "
            "FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?",
            [cy["ticker"], cy["fiscal_year_end"]],
        ).fetchone()
        if row is None:
            raise RuntimeError(f"{cy['ticker']} {cy['fiscal_year_end']}: no active quarterly_extraction_runs row found "
                                f"-- cannot derive accessions. Refusing to guess.")
        run_id, q1_acc, q2_acc, q3_acc, fy_acc = row
        if run_id != cy["run_id"]:
            raise RuntimeError(f"{cy['ticker']} {cy['fiscal_year_end']}: run_id mismatch -- regression artifact says "
                                f"{cy['run_id']!r}, production currently has {run_id!r}. Refusing to proceed.")
        accessions = {"q1_accession": q1_acc, "q2_accession": q2_acc, "q3_accession": q3_acc, "fy_accession": fy_acc}
        missing = [name for name, value in accessions.items() if not value]
        if missing:
            raise RuntimeError(f"{cy['ticker']} {cy['fiscal_year_end']}: production run is missing required accession "
                                f"field(s) {missing} -- refusing to proceed with incomplete lineage (schema error).")
        enriched.append({**cy, **accessions})
    return enriched


def phase_backup_and_archive(prod_connection, target_company_years: list[dict], regression_hashes: dict) -> dict:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_v5_production_load_{timestamp}.duckdb"
    import shutil
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    source_checksum = sha256_of_file(PRODUCTION_DB_PATH)
    backup_checksum = sha256_of_file(backup_path)
    if source_checksum != backup_checksum:
        raise RuntimeError("Backup checksum mismatch — aborting before any write.")
    backup_connection = duckdb.connect(database=str(backup_path), read_only=True)
    backup_counts = get_global_counts(backup_connection)
    backup_connection.close()
    if backup_counts != get_global_counts(prod_connection):
        raise RuntimeError("Backup counts do not match live production counts.")

    run_ids = [cy["run_id"] for cy in target_company_years]
    placeholders = ",".join("?" * len(run_ids))
    run_df = prod_connection.execute(f"SELECT * FROM quarterly_extraction_runs WHERE run_id IN ({placeholders})", run_ids).fetchdf()
    prod_connection.register("run_tmp", run_df)
    run_archive_path = ARCHIVE_DIR / f"v5_production_load_runs_replaced_{timestamp}.parquet"
    prod_connection.execute(f"COPY (SELECT * FROM run_tmp) TO '{run_archive_path.as_posix()}' (FORMAT PARQUET)")
    prod_connection.unregister("run_tmp")

    rows_df = prod_connection.execute(f"SELECT * FROM quarterly_metric_results WHERE run_id IN ({placeholders})", run_ids).fetchdf()
    prod_connection.register("rows_tmp", rows_df)
    rows_archive_path = ARCHIVE_DIR / f"v5_production_load_rows_replaced_{timestamp}.parquet"
    prod_connection.execute(f"COPY (SELECT * FROM rows_tmp) TO '{rows_archive_path.as_posix()}' (FORMAT PARQUET)")
    prod_connection.unregister("rows_tmp")

    reread_run_count = prod_connection.execute(f"SELECT COUNT(*) FROM read_parquet('{run_archive_path.as_posix()}')").fetchone()[0]
    reread_rows_count = prod_connection.execute(f"SELECT COUNT(*) FROM read_parquet('{rows_archive_path.as_posix()}')").fetchone()[0]
    if reread_run_count != EXPECTED_TARGET_COMPANY_YEARS:
        raise RuntimeError(f"Archived run count {reread_run_count} != expected {EXPECTED_TARGET_COMPANY_YEARS}")
    if reread_rows_count != EXPECTED_TARGET_ROWS:
        raise RuntimeError(f"Archived rows count {reread_rows_count} != expected {EXPECTED_TARGET_ROWS}")

    annual_v1_checksum_before = sha256_of_file(ANNUAL_V1_DB_PATH) if ANNUAL_V1_DB_PATH.exists() else None

    manifest = {
        "task_id": "TASK_149_V5_PRODUCTION_LOAD_BUILD / execute-time load",
        "target_company_years": [{"ticker": cy["ticker"], "fiscal_year_end": cy["fiscal_year_end"], "old_run_id": cy["run_id"]} for cy in target_company_years],
        "regression_artifact_hashes": regression_hashes,
        "backup_path": str(backup_path), "backup_checksum": backup_checksum, "source_checksum": source_checksum,
        "backup_counts": backup_counts,
        "archive_paths_and_counts": {"runs": {"path": str(run_archive_path), "rows": reread_run_count},
                                      "quarterly_rows": {"path": str(rows_archive_path), "rows": reread_rows_count}},
        "annual_v1_checksum_before": annual_v1_checksum_before,
        "pre_load_database_counts": get_global_counts(prod_connection),
        "expected_post_load_counts": {"quarterly_extraction_runs": EXPECTED_PRE_RUNS, "quarterly_metric_results": EXPECTED_PRE_ROWS,
                                       "financial_metric_results": EXPECTED_PRE_FMR, "unique_review_required": EXPECTED_POST_REVIEW_REQUIRED},
        "manifest_created_at_utc": utc_now_iso(),
    }
    atomic_write_json(MANIFEST_PATH, manifest)
    return {"backup_path": str(backup_path), "backup_checksum": backup_checksum, "run_archive_path": str(run_archive_path),
            "rows_archive_path": str(rows_archive_path), "manifest_path": str(MANIFEST_PATH),
            "annual_v1_checksum_before": annual_v1_checksum_before}


def run_execute() -> int:
    acquire_pid_lock()
    checkpoint = {"status": "STARTED", "started_at": utc_now_iso(), "company_years": []}
    atomic_write_json(CHECKPOINT_PATH, checkpoint)
    log("=== v5 production load: --execute started ===")

    try:
        regression_ok, regression_detail = validate_regression_artifacts()
        if not regression_ok:
            raise RuntimeError(f"Regression artifacts invalid: {regression_detail}")
        regression = regression_detail["regression"]
        regression_hashes = regression_detail["artifact_hashes"]

        cross_check_ok, target_company_years, target_metric_cases, derivation_detail = derive_and_cross_check_targets(regression)
        if not cross_check_ok:
            raise RuntimeError(f"Target derivation cross-check failed: {derivation_detail}")

        scope_ok, scope_detail = verify_replacement_scope(target_company_years)
        if not scope_ok:
            raise RuntimeError(f"Replacement scope verification failed: {scope_detail}")

        prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
        warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
        preconditions_ok, preconditions_detail = check_execution_preconditions(
            prod_connection, warehouse_connection, target_company_years, target_metric_cases
        )
        if not preconditions_ok:
            prod_connection.close()
            warehouse_connection.close()
            raise RuntimeError(f"Execution preconditions not met: {preconditions_detail}")

        target_company_years = enrich_target_company_years_with_accessions(prod_connection, target_company_years)

        archive_info = phase_backup_and_archive(prod_connection, target_company_years, regression_hashes)
        log(f"Backup+archive complete: {archive_info['backup_path']}")

        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        regression_by_key = {(cy["ticker"], cy["fiscal_year_end"]): cy for cy in regression["company_year_results"]}
        fresh_results = []

        for index, cy in enumerate(target_company_years, start=1):
            ticker, fiscal_year_end = cy["ticker"], cy["fiscal_year_end"]
            json_out = SCRATCH_DIR / f"v5_{ticker}_{fiscal_year_end}.json"
            csv_out = SCRATCH_DIR / f"v5_{ticker}_{fiscal_year_end}.csv"
            cmd = [
                sys.executable, str(V5_ENGINE_SCRIPT), "--ticker", ticker, "--fiscal-year-end", fiscal_year_end,
                "--q1-accession", cy["q1_accession"], "--q2-accession", cy["q2_accession"],
                "--q3-accession", cy["q3_accession"], "--fy-accession", cy["fy_accession"],
                "--json-output", str(json_out), "--csv-output", str(csv_out),
            ]
            cy_start = time.perf_counter()
            try:
                completed = subprocess.run(cmd, cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=HARD_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"{ticker} {fiscal_year_end}: exceeded {HARD_TIMEOUT_SECONDS}s hard timeout — stopping.")
            elapsed = round(time.perf_counter() - cy_start, 2)
            if completed.returncode != 0:
                raise RuntimeError(f"{ticker} {fiscal_year_end}: engine subprocess failed (exit {completed.returncode}): {completed.stderr[-2000:]}")

            engine_output = json.loads(json_out.read_text(encoding="utf-8"))
            prod_rows = get_production_rows_full(prod_connection, cy["run_id"])
            regression_cy_entry = regression_by_key[(ticker, fiscal_year_end)]
            target_metrics = {m for t, fy, m in target_metric_cases if (t, fy) == (ticker, fiscal_year_end)}
            ok, comparison_detail = compare_against_regression_and_production(
                engine_output, regression_cy_entry, prod_rows, target_metrics
            )
            if not ok:
                raise RuntimeError(f"{ticker} {fiscal_year_end}: fresh V5 result mismatch vs. regression/production: {comparison_detail}")

            fresh_results.append({"ticker": ticker, "fiscal_year_end": fiscal_year_end, "old_run_id": cy["run_id"],
                                   "engine_output": engine_output, "comparison": comparison_detail, "elapsed_seconds": elapsed})
            checkpoint["company_years"].append({"ticker": ticker, "fiscal_year_end": fiscal_year_end, "status": "ENGINE_VERIFIED", "elapsed_seconds": elapsed})
            atomic_write_json(CHECKPOINT_PATH, checkpoint)
            log(f"{index}/{EXPECTED_TARGET_COMPANY_YEARS} {ticker} {fiscal_year_end} engine-verified elapsed={elapsed}s")

        prod_connection.close()
        warehouse_connection.close()

        # -----------------------------------------------------------
        # ONE ATOMIC TRANSACTION over all 3 target company-years
        # -----------------------------------------------------------
        connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
        connection.execute("BEGIN TRANSACTION")
        try:
            new_run_ids = {}
            created_at = utc_now_iso()
            for fresh in fresh_results:
                ticker, fiscal_year_end, old_run_id = fresh["ticker"], fresh["fiscal_year_end"], fresh["old_run_id"]
                reconfirm = connection.execute(
                    "SELECT run_id FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?", [ticker, fiscal_year_end]
                ).fetchone()
                if reconfirm is None or reconfirm[0] != old_run_id:
                    raise RuntimeError(f"Reconfirmation failed for {ticker} {fiscal_year_end}: expected {old_run_id}, found {reconfirm}")
                connection.execute("DELETE FROM quarterly_metric_results WHERE run_id = ?", [old_run_id])
                connection.execute("DELETE FROM quarterly_extraction_runs WHERE run_id = ?", [old_run_id])

                engine_output = fresh["engine_output"]
                rows = rows_from_v5_output_full(engine_output)
                new_run_id = str(uuid.uuid4())
                new_run_ids[(ticker, fiscal_year_end)] = new_run_id
                filings = engine_output["filings"]
                connection.execute(
                    "INSERT INTO quarterly_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [new_run_id, ticker, fiscal_year_end, ENGINE_VERSION_V5, SCHEMA_VERSION,
                     filings["Q1"]["accession_number"], filings["Q2"]["accession_number"],
                     filings["Q3"]["accession_number"], filings["FY"]["accession_number"],
                     str(SCRATCH_DIR / f"v5_{ticker}_{fiscal_year_end}.json"), "PASS", created_at, created_at],
                )
                for metric_name, metric_result in engine_output["metrics"].items():
                    for quarter_label in ("Q1", "Q2", "Q3", "Q4"):
                        quarter = metric_result.get("quarters", {}).get(quarter_label)
                        if quarter is None:
                            continue
                        lineage = quarter["lineage"]
                        reconciliation = metric_result.get("reconciliation", {})
                        connection.execute(
                            "INSERT INTO quarterly_metric_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            [new_run_id, ticker, fiscal_year_end, quarter_label, metric_name,
                             quarter["value"], "iso4217:USD", metric_result.get("status"), quarter["extraction_basis"],
                             lineage.get("period_start"), lineage.get("period_end"), quarter["availability_date"],
                             lineage.get("accession_number") or lineage.get("annual_accession_number"),
                             lineage.get("concept_qname") or lineage.get("annual_concept_qname"),
                             lineage.get("context_id") or lineage.get("nine_month_ytd_context_id"),
                             "{}", json.dumps(lineage, default=str), metric_result.get("status"),
                             reconciliation.get("difference"), reconciliation.get("precision_calculation", {}).get("permitted_difference"),
                             created_at],
                        )

            # --- pre-commit validation ---
            errors = []
            new_run_ids_list = list(new_run_ids.values())
            placeholders = ",".join("?" * len(new_run_ids_list))
            committed_runs = connection.execute(f"SELECT COUNT(*) FROM quarterly_extraction_runs WHERE run_id IN ({placeholders})", new_run_ids_list).fetchone()[0]
            if committed_runs != EXPECTED_TARGET_COMPANY_YEARS:
                errors.append(f"committed target runs = {committed_runs}, expected {EXPECTED_TARGET_COMPANY_YEARS}")
            committed_df = connection.execute(
                f"SELECT * FROM quarterly_metric_results WHERE run_id IN ({placeholders})", new_run_ids_list
            ).fetchdf()
            if len(committed_df) != EXPECTED_TARGET_ROWS:
                errors.append(f"committed target rows = {len(committed_df)}, expected {EXPECTED_TARGET_ROWS}")
            per_run_counts = committed_df.groupby("run_id").size()
            if any(c != EXPECTED_ROWS_PER_COMPANY_YEAR for c in per_run_counts):
                errors.append(f"not every target run has exactly 24 rows: {per_run_counts.to_dict()}")
            dup = committed_df.groupby(["run_id", "metric_name", "fiscal_quarter"]).size()
            if (dup > 1).any():
                errors.append("duplicate natural keys found")
            if committed_df["lineage_json"].isna().any():
                errors.append("missing lineage_json on at least one row")
            if committed_df["value"].isna().any():
                errors.append("null value on at least one committed row")
            avail_mismatch = connection.execute(
                f"SELECT COUNT(*) FROM quarterly_metric_results r JOIN sec_filings s ON s.accession_number = r.accession_number "
                f"WHERE r.run_id IN ({placeholders}) AND r.availability_date != CAST(s.filing_date AS VARCHAR)", new_run_ids_list,
            ).fetchone()[0]
            if avail_mismatch:
                errors.append(f"{avail_mismatch} availability-date mismatch(es)")

            total_changed = sum(len(fresh["comparison"]["changed_rows"]) for fresh in fresh_results)
            total_unchanged = sum(len(fresh["comparison"]["unchanged_rows"]) for fresh in fresh_results)
            if total_changed != EXPECTED_CHANGED_ROWS:
                errors.append(f"expected {EXPECTED_CHANGED_ROWS} changed rows, found {total_changed}")
            if total_unchanged != EXPECTED_UNCHANGED_ROWS:
                errors.append(f"expected {EXPECTED_UNCHANGED_ROWS} unchanged rows, found {total_unchanged}")

            target_metric_statuses = committed_df[committed_df.apply(
                lambda r: (r["ticker"], r["fiscal_year_end"], r["metric_name"]) in target_metric_cases, axis=1
            )]["reconciliation_status"].unique()
            if not all(s in ("PASS", "PASS_ROUNDING_TOLERANCE") for s in target_metric_statuses):
                errors.append(f"not all target metric-year cases are PASS: {target_metric_statuses}")

            if errors:
                connection.execute("ROLLBACK")
                raise RuntimeError(f"Pre-commit validation failed, rolled back: {errors}")

            connection.execute("COMMIT")
            log("Atomic transaction committed.")
        except Exception:
            connection.execute("ROLLBACK")
            connection.close()
            raise
        connection.close()

        # -----------------------------------------------------------
        # POST-COMMIT VALIDATION
        # -----------------------------------------------------------
        prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
        post_counts = get_global_counts(prod_connection)
        post_checks = {
            "quarterly_extraction_runs_45": post_counts["quarterly_extraction_runs"] == EXPECTED_PRE_RUNS,
            "quarterly_metric_results_1080": post_counts["quarterly_metric_results"] == EXPECTED_PRE_ROWS,
            "financial_metric_results_900": post_counts["financial_metric_results"] == EXPECTED_PRE_FMR,
            "unique_review_required_0": post_counts["unique_review_required"] == EXPECTED_POST_REVIEW_REQUIRED,
        }
        rows_per_cy = prod_connection.execute(
            "SELECT COUNT(*) FROM (SELECT r.run_id, COUNT(*) c FROM quarterly_metric_results r GROUP BY r.run_id HAVING COUNT(*) != 24)"
        ).fetchone()[0]
        post_checks["every_company_year_24_rows"] = rows_per_cy == 0
        dup_keys = prod_connection.execute(
            "SELECT COUNT(*) FROM (SELECT run_id, metric_name, fiscal_quarter, COUNT(*) c FROM quarterly_metric_results GROUP BY run_id, metric_name, fiscal_quarter HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        post_checks["duplicate_keys_zero"] = dup_keys == 0
        missing_lineage = prod_connection.execute("SELECT COUNT(*) FROM quarterly_metric_results WHERE lineage_json IS NULL").fetchone()[0]
        post_checks["missing_lineage_zero"] = missing_lineage == 0
        avail_mismatch_all = prod_connection.execute(
            "SELECT COUNT(*) FROM quarterly_metric_results r JOIN sec_filings s ON s.accession_number = r.accession_number "
            "WHERE r.availability_date != CAST(s.filing_date AS VARCHAR)"
        ).fetchone()[0]
        post_checks["availability_mismatches_zero"] = avail_mismatch_all == 0

        annual_v1_checksum_after = sha256_of_file(ANNUAL_V1_DB_PATH) if ANNUAL_V1_DB_PATH.exists() else None
        post_checks["annual_v1_checksum_unchanged"] = annual_v1_checksum_after == archive_info["annual_v1_checksum_before"]

        warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
        warehouse_facts_after = warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
        warehouse_connection.close()
        post_checks["warehouse_facts_unchanged"] = warehouse_facts_after == EXPECTED_WAREHOUSE_FACTS
        prod_connection.close()

        all_post_ok = all(post_checks.values())
        if not all_post_ok:
            raise RuntimeError(f"Post-commit validation failed (data already committed — manual review required): {post_checks}")

        result = {
            "status": "PASS", "target_company_years": [{"ticker": f["ticker"], "fiscal_year_end": f["fiscal_year_end"],
                                                          "old_run_id": f["old_run_id"], "new_run_id": new_run_ids[(f["ticker"], f["fiscal_year_end"])]}
                                                         for f in fresh_results],
            "post_counts": post_counts, "post_checks": post_checks, "backup_path": archive_info["backup_path"],
            "manifest_path": archive_info["manifest_path"], "changed_rows": EXPECTED_CHANGED_ROWS,
            "unchanged_rows": EXPECTED_UNCHANGED_ROWS, "completed_at": utc_now_iso(),
        }
        atomic_write_json(RESULT_JSON_PATH, result)
        with RESULT_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ticker", "fiscal_year_end", "old_run_id", "new_run_id"])
            for tcy in result["target_company_years"]:
                writer.writerow([tcy["ticker"], tcy["fiscal_year_end"], tcy["old_run_id"], tcy["new_run_id"]])

        checkpoint["status"] = "COMPLETE"
        checkpoint["completed_at"] = utc_now_iso()
        atomic_write_json(CHECKPOINT_PATH, checkpoint)
        log("=== v5 production load: --execute COMPLETE (PASS) ===")
        return 0

    except Exception as exc:  # noqa: BLE001
        checkpoint["status"] = "FAILED"
        checkpoint["error"] = str(exc)
        checkpoint["failed_at"] = utc_now_iso()
        atomic_write_json(CHECKPOINT_PATH, checkpoint)
        fail_result = {"status": "FAIL", "error": str(exc), "failed_at": utc_now_iso()}
        atomic_write_json(RESULT_JSON_PATH, fail_result)
        log(f"=== v5 production load: --execute FAILED: {exc} ===")
        return 1
    finally:
        release_pid_lock()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quarterly engine V5 production load.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-only", action="store_true")
    group.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.check_only:
        output = run_check_only()
        return 0 if output["status"] == "PASS" else 1
    return run_execute()


if __name__ == "__main__":
    sys.exit(main())
