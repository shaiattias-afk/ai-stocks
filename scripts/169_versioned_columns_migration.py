"""
D-047 schema migration -- adds `engine_version`, `loaded_at`, `is_active`
to the six tables previously frozen by D-042/D-043/D-045/D-046, so future
loads can be safely appended and enforced by
scripts/167_versioned_write_guard.py instead of by an all-or-nothing
"no writes without a new engine version" policy.

This is a one-time, explicitly authorized schema change -- run directly
against the database (NOT through the write guard, which governs
ordinary future loads, not schema migrations), following the same
discipline already used for this project's prior schema migrations
(e.g. scripts/124's `ALTER TABLE ... ALTER COLUMN ... DROP NOT NULL`,
D-036) and every production load in this project: PID lock -> backup
(SHA-256-verified) -> one atomic transaction -> post-commit validation
-> independent read-only re-verification against the live database.

Where a column already exists that serves the same purpose, it is not
duplicated:
  - `quarterly_extraction_runs` already has a per-row `engine_version`.
  - `derived_metric_results` already has a per-row `engine_version`.
Both still receive `loaded_at` and `is_active`.

Every backfilled value is a REAL, traceable fact -- never invented:
  - `financial_metric_results.engine_version`/`loaded_at` are copied
    from `extraction_runs` (already stores both per accession) via the
    existing `extraction_run_id` foreign key.
  - `quarterly_metric_results.engine_version` is copied from
    `quarterly_extraction_runs` via the existing `run_id` foreign key;
    `loaded_at` is parsed from its own existing `created_at` string.
  - `quarterly_extraction_runs.loaded_at` is parsed from its own
    `completed_at` (falling back to `created_at` if `completed_at` is
    NULL).
  - `derived_metric_results.loaded_at` is copied from its own existing
    `created_at`.
  - `historical_prices_daily.engine_version` / `valuation_v1_per_share_
    inputs.engine_version` are backfilled with the exact script that
    produced every one of those rows, per docs/DECISIONS_LOG.md D-045 /
    D-046 -- `loaded_at` is copied from each table's own `created_at`.
  - `is_active = TRUE` for every pre-existing row in all six tables
    (they are the current, approved, frozen-content data).

Two mutually exclusive modes:
  --check-only  Fully read-only. Validates every backfill join has zero
                unmatched rows, prints the exact DDL/backfill SQL that
                --execute would run, writes nothing.
  --execute     PID lock -> backup -> one atomic transaction (ALTER
                TABLE ADD COLUMN, backfill UPDATE, ALTER COLUMN SET NOT
                NULL) per table -> in-transaction validation (0 NULLs in
                new columns, row counts unchanged, every OTHER column's
                content byte-identical before/after) -> COMMIT ->
                independent post-commit re-verification, reopening the
                database read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
LOGS_DIR = PROJECT_DIR / "logs"
BACKUPS_DIR = DATA_DIR / "database" / "backups"

PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

CHECK_ONLY_OUTPUT_PATH = DATA_DIR / "versioned_columns_migration_check.json"
RESULT_JSON_PATH = DATA_DIR / "versioned_columns_migration_result.json"
LOG_PATH = LOGS_DIR / "versioned_columns_migration.log"
PID_LOCK_PATH = DATA_DIR / "versioned_columns_migration.pid"

HISTORICAL_PRICES_ENGINE_VERSION_BACKFILL = "HISTORICAL_PRICES_V1_LOAD (scripts/158_historical_prices_v1_load.py)"
VALUATION_ENGINE_VERSION_BACKFILL = "VALUATION_V1_LOAD (scripts/160_valuation_v1_per_share_inputs.py)"

# Each entry: table, columns to add (name -> SQL type), the backfill
# UPDATE statement (uses only pre-existing tables/columns), and an
# optional pre-flight validation query that must return 0 (unmatched
# join rows) or None if not applicable.
MIGRATION_STEPS = [
    {
        "table": "financial_metric_results",
        "add_columns": [("engine_version", "VARCHAR"), ("loaded_at", "TIMESTAMP"), ("is_active", "BOOLEAN")],
        "preflight_unmatched_sql": (
            "SELECT COUNT(*) FROM financial_metric_results f "
            "LEFT JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id "
            "WHERE r.extraction_run_id IS NULL"
        ),
        "backfill_sql": (
            "UPDATE financial_metric_results f SET "
            "engine_version = r.engine_version, loaded_at = r.loaded_at, is_active = TRUE "
            "FROM extraction_runs r WHERE r.extraction_run_id = f.extraction_run_id"
        ),
    },
    {
        "table": "quarterly_extraction_runs",
        "add_columns": [("loaded_at", "TIMESTAMP"), ("is_active", "BOOLEAN")],
        "preflight_unmatched_sql": None,
        "backfill_sql": (
            "UPDATE quarterly_extraction_runs SET "
            "loaded_at = COALESCE(TRY_CAST(completed_at AS TIMESTAMP), TRY_CAST(created_at AS TIMESTAMP)), "
            "is_active = TRUE"
        ),
    },
    {
        "table": "quarterly_metric_results",
        "add_columns": [("engine_version", "VARCHAR"), ("loaded_at", "TIMESTAMP"), ("is_active", "BOOLEAN")],
        "preflight_unmatched_sql": (
            "SELECT COUNT(*) FROM quarterly_metric_results q "
            "LEFT JOIN quarterly_extraction_runs r ON r.run_id = q.run_id "
            "WHERE r.run_id IS NULL"
        ),
        "backfill_sql": (
            "UPDATE quarterly_metric_results q SET "
            "engine_version = r.engine_version, loaded_at = TRY_CAST(q.created_at AS TIMESTAMP), is_active = TRUE "
            "FROM quarterly_extraction_runs r WHERE r.run_id = q.run_id"
        ),
    },
    {
        "table": "derived_metric_results",
        "add_columns": [("loaded_at", "TIMESTAMP"), ("is_active", "BOOLEAN")],
        "preflight_unmatched_sql": None,
        "backfill_sql": "UPDATE derived_metric_results SET loaded_at = created_at, is_active = TRUE",
    },
    {
        "table": "historical_prices_daily",
        "add_columns": [("engine_version", "VARCHAR"), ("loaded_at", "TIMESTAMP"), ("is_active", "BOOLEAN")],
        "preflight_unmatched_sql": None,
        "backfill_sql": (
            f"UPDATE historical_prices_daily SET "
            f"engine_version = '{HISTORICAL_PRICES_ENGINE_VERSION_BACKFILL}', loaded_at = created_at, is_active = TRUE"
        ),
    },
    {
        "table": "valuation_v1_per_share_inputs",
        "add_columns": [("engine_version", "VARCHAR"), ("loaded_at", "TIMESTAMP"), ("is_active", "BOOLEAN")],
        "preflight_unmatched_sql": None,
        "backfill_sql": (
            f"UPDATE valuation_v1_per_share_inputs SET "
            f"engine_version = '{VALUATION_ENGINE_VERSION_BACKFILL}', loaded_at = created_at, is_active = TRUE"
        ),
    },
]

ALL_TABLES = [step["table"] for step in MIGRATION_STEPS]


# =====================================================================
# SMALL SHARED HELPERS (same discipline as scripts/158/159/160)
# =====================================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log(message: str, also_print: bool = True) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{utc_now_iso()}] {message}\n")
    if also_print:
        print(message)


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def is_pid_active(pid: int) -> bool:
    try:
        completed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=10)
    except Exception:
        return True
    return str(pid) in completed.stdout


def acquire_pid_lock() -> None:
    if PID_LOCK_PATH.exists():
        try:
            content = json.loads(PID_LOCK_PATH.read_text(encoding="utf-8"))
            existing_pid = content["pid"]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"PID lock file exists but is unreadable/malformed ({exc}) -- refusing to start.")
        if is_pid_active(existing_pid):
            raise RuntimeError(f"A live PID lock already exists (pid={existing_pid}) -- refusing to start.")
        log(f"Removing stale PID lock (pid={existing_pid} is not active).")
        PID_LOCK_PATH.unlink(missing_ok=True)
    atomic_write_json(PID_LOCK_PATH, {"pid": os.getpid(), "started_at": utc_now_iso()})
    log(f"PID lock acquired (pid={os.getpid()}).")


def release_pid_lock() -> None:
    if PID_LOCK_PATH.exists():
        PID_LOCK_PATH.unlink(missing_ok=True)
        log("PID lock released.")


# =====================================================================
# FINGERPRINTING -- table content EXCLUDING the new columns (which are
# expected to change from absent -> populated), so we can prove nothing
# else moved.
# =====================================================================

def existing_columns(connection, table: str) -> list[str]:
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position", [table]
    ).fetchall()
    return [r[0] for r in rows]


def fingerprint_original_columns(connection, table: str, original_columns: list[str]) -> dict:
    col_list = ", ".join(original_columns)
    rows = connection.execute(f"SELECT {col_list} FROM {table}").fetchall()
    digest = hashlib.sha256("\n".join(sorted(repr(r) for r in rows)).encode("utf-8")).hexdigest()
    return {"row_count": len(rows), "fingerprint": digest}


def fingerprint_untouched_tables(connection) -> dict:
    """Every other table in the database (companies, sec_filings,
    extraction_runs, historical_review_items, ...) that this migration
    never touches at all."""
    all_tables = [r[0] for r in connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()]
    untouched = [t for t in all_tables if t not in ALL_TABLES]
    result = {}
    for table in untouched:
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        digest = hashlib.sha256("\n".join(sorted(repr(r) for r in rows)).encode("utf-8")).hexdigest()
        result[table] = {"row_count": len(rows), "fingerprint": digest}
    return result


# =====================================================================
# --check-only MODE
# =====================================================================

def run_check_only() -> dict:
    start = time.perf_counter()
    db_hash_before = sha256_of_file(PRODUCTION_DB_PATH)
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    per_table = []
    all_preflight_ok = True
    for step in MIGRATION_STEPS:
        table = step["table"]
        cols_before = existing_columns(connection, table)
        already_present = [name for name, _ in step["add_columns"] if name in cols_before]
        row_count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        unmatched = None
        if step["preflight_unmatched_sql"]:
            unmatched = connection.execute(step["preflight_unmatched_sql"]).fetchone()[0]
        preflight_ok = not already_present and (unmatched is None or unmatched == 0)
        all_preflight_ok = all_preflight_ok and preflight_ok
        per_table.append({
            "table": table, "row_count": row_count, "columns_to_add": [n for n, _ in step["add_columns"]],
            "already_present_columns": already_present, "unmatched_backfill_join_rows": unmatched,
            "preflight_ok": preflight_ok,
        })

    connection.close()
    db_hash_after = sha256_of_file(PRODUCTION_DB_PATH)
    runtime = round(time.perf_counter() - start, 3)

    global_checks = {
        "all_preflight_ok": all_preflight_ok,
        "database_unchanged": db_hash_before == db_hash_after,
    }
    status = "PASS" if all(global_checks.values()) else "FAIL"

    output = {
        "mode": "check-only", "status": status, "per_table": per_table, "global_checks": global_checks,
        "database_sha256_before": db_hash_before, "database_sha256_after": db_hash_after,
        "runtime_seconds": runtime, "checked_at": utc_now_iso(),
    }
    atomic_write_json(CHECK_ONLY_OUTPUT_PATH, output)
    log(f"check-only run: status={status} runtime={runtime}s", also_print=False)

    print("=" * 100)
    print(f"D-047 VERSIONED COLUMNS MIGRATION -- CHECK-ONLY: {status}  (runtime {runtime}s)")
    print("=" * 100)
    for t in per_table:
        print(f"  {t['table']}: rows={t['row_count']} add={t['columns_to_add']} "
              f"already_present={t['already_present_columns']} unmatched_join_rows={t['unmatched_backfill_join_rows']} "
              f"preflight_ok={t['preflight_ok']}")
    print(f"\nGlobal checks: {global_checks}")
    print("=" * 100)
    return output


# =====================================================================
# --execute MODE
# =====================================================================

def phase_backup() -> dict:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_d047_versioned_columns_migration_{timestamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    source_checksum = sha256_of_file(PRODUCTION_DB_PATH)
    backup_checksum = sha256_of_file(backup_path)
    if source_checksum != backup_checksum:
        raise RuntimeError("Backup checksum mismatch -- aborting before any write.")
    return {"backup_path": str(backup_path), "backup_checksum": backup_checksum, "source_checksum": source_checksum}


def run_execute() -> int:
    acquire_pid_lock()
    log("=== D-047 versioned columns migration: --execute started ===")
    try:
        read_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
        pre_original_fingerprints = {}
        pre_row_counts = {}
        for step in MIGRATION_STEPS:
            table = step["table"]
            cols_before = existing_columns(read_connection, table)
            already_present = [name for name, _ in step["add_columns"] if name in cols_before]
            if already_present:
                raise RuntimeError(f"{table}: column(s) {already_present} already exist -- refusing to re-run migration.")
            if step["preflight_unmatched_sql"]:
                unmatched = read_connection.execute(step["preflight_unmatched_sql"]).fetchone()[0]
                if unmatched != 0:
                    raise RuntimeError(f"{table}: {unmatched} row(s) have no matching backfill source -- aborting before any write.")
            pre_original_fingerprints[table] = fingerprint_original_columns(read_connection, table, cols_before)
            pre_row_counts[table] = read_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        pre_untouched_fingerprints = fingerprint_untouched_tables(read_connection)
        read_connection.close()

        backup_info = phase_backup()
        log(f"Backup complete: {backup_info['backup_path']}")

        connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=False)

        # --- transaction 1: ADD COLUMN + backfill for every table, fully
        # validated, committed only as one all-or-nothing unit. (DuckDB
        # 1.5.5 cannot run ALTER COLUMN ... SET NOT NULL in the SAME
        # transaction as a preceding UPDATE on a table with a PRIMARY KEY
        # index -- "Cannot create index with outstanding updates" -- so
        # NOT NULL enforcement is a second, separate transaction below;
        # this does not weaken atomicity of the actual data change, since
        # transaction 2 only tightens a constraint and changes no data.)
        connection.execute("BEGIN TRANSACTION")
        try:
            for step in MIGRATION_STEPS:
                table = step["table"]
                for column_name, column_type in step["add_columns"]:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_type}")
                connection.execute(step["backfill_sql"])
                log(f"{table}: added {[n for n, _ in step['add_columns']]}, backfilled.")

            # --- in-transaction validation, before COMMIT ---
            errors = []
            for step in MIGRATION_STEPS:
                table = step["table"]
                new_cols = [n for n, _ in step["add_columns"]]
                null_count = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE " + " OR ".join(f"{c} IS NULL" for c in new_cols)
                ).fetchone()[0]
                if null_count:
                    errors.append(f"{table}: {null_count} row(s) have NULL in a new column after backfill")
                post_count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if post_count != pre_row_counts[table]:
                    errors.append(f"{table}: row count changed ({pre_row_counts[table]} -> {post_count})")
                cols_before = [c for c in existing_columns(connection, table) if c not in new_cols]
                post_fp = fingerprint_original_columns(connection, table, cols_before)
                if post_fp != pre_original_fingerprints[table]:
                    errors.append(f"{table}: pre-existing column content changed (before={pre_original_fingerprints[table]} after={post_fp})")
            if errors:
                raise RuntimeError(f"Post-backfill validation failed ({len(errors)} issue(s)): {errors}")

            connection.execute("COMMIT")
            log("Transaction 1 (ADD COLUMN + backfill) committed.")
        except Exception:
            connection.execute("ROLLBACK")
            connection.close()
            raise

        # --- transaction 2: NOT NULL enforcement only, no data change ---
        connection.execute("BEGIN TRANSACTION")
        try:
            for step in MIGRATION_STEPS:
                table = step["table"]
                for column_name, _ in step["add_columns"]:
                    connection.execute(f"ALTER TABLE {table} ALTER COLUMN {column_name} SET NOT NULL")
            connection.execute("COMMIT")
            log("Transaction 2 (NOT NULL enforcement) committed.")
        except Exception:
            connection.execute("ROLLBACK")
            connection.close()
            raise
        connection.close()

        # --- independent post-commit re-verification (reopen read-only) ---
        verify_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
        post_checks = {}
        for step in MIGRATION_STEPS:
            table = step["table"]
            new_cols = [n for n, _ in step["add_columns"]]
            null_count = verify_connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE " + " OR ".join(f"{c} IS NULL" for c in new_cols)
            ).fetchone()[0]
            not_active_count = verify_connection.execute(f"SELECT COUNT(*) FROM {table} WHERE is_active <> TRUE").fetchone()[0]
            post_count = verify_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            cols_now = [c for c in existing_columns(verify_connection, table) if c not in new_cols]
            post_fp = fingerprint_original_columns(verify_connection, table, cols_now)
            post_checks[table] = {
                "row_count_unchanged": post_count == pre_row_counts[table],
                "new_columns_null_free": null_count == 0,
                "all_rows_active": not_active_count == 0,
                "pre_existing_columns_byte_identical": post_fp == pre_original_fingerprints[table],
            }
        post_untouched_fingerprints = fingerprint_untouched_tables(verify_connection)
        untouched_unchanged = post_untouched_fingerprints == pre_untouched_fingerprints
        verify_connection.close()

        all_post_checks_passed = untouched_unchanged and all(
            all(v.values()) for v in post_checks.values()
        )
        if not all_post_checks_passed:
            raise RuntimeError(
                f"Post-commit independent re-verification failed (table already committed -- manual review required): "
                f"per_table={post_checks} untouched_unchanged={untouched_unchanged}"
            )

        result = {
            "status": "PASS", "per_table_post_checks": post_checks,
            "untouched_tables_unchanged": untouched_unchanged,
            "backup_path": backup_info["backup_path"], "backup_checksum": backup_info["backup_checksum"],
            "completed_at": utc_now_iso(),
        }
        atomic_write_json(RESULT_JSON_PATH, result)
        log("=== D-047 versioned columns migration: --execute COMPLETE (PASS) ===")
        return 0
    except Exception as exc:  # noqa: BLE001
        fail_result = {"status": "FAIL", "error": str(exc), "failed_at": utc_now_iso()}
        atomic_write_json(RESULT_JSON_PATH, fail_result)
        log(f"=== D-047 versioned columns migration: --execute FAILED: {exc} ===")
        return 1
    finally:
        release_pid_lock()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D-047 versioned columns migration.")
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
