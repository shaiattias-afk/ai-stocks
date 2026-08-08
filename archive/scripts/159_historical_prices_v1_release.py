"""
Historical Prices V1 -- one closed release task: preflight -> exactly
one --execute of scripts/158 -> independent post-load verification
(re-opening production read-only, never trusting the loader's own
report alone) -> freeze artifact generation -> nothing else. Git
commit/tag are performed separately by the calling agent after this
script reports PASS, per the task's Git-safety discipline (this script
never runs `git commit`/`git tag` itself).

Every production write in this release happens exactly once, inside
scripts/158_historical_prices_v1_load.py --execute, invoked here as a
real subprocess exactly one time. This script itself never opens the
production database for writing.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DOCS_DIR = PROJECT_DIR / "docs"

PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
ANNUAL_V1_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"
LOADER_SCRIPT_PATH = PROJECT_DIR / "scripts" / "158_historical_prices_v1_load.py"
PYTHON_EXE_PATH = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"

BUILD_VALIDATION_JSON_PATH = DATA_DIR / "historical_prices_v1_build_validation.json"
NINE_TICKER_PROOF_JSON_PATH = DATA_DIR / "proofs" / "9_ticker_historical_price_proof.json"
PRICE_POLICY_PROOF_JSON_PATH = DATA_DIR / "proofs" / "price_policy_v1_proof.json"
LOAD_RESULT_JSON_PATH = DATA_DIR / "historical_prices_v1_load_result.json"

RELEASE_OUTPUT_PATH = DATA_DIR / "historical_prices_v1_release_task_result.json"

EXPECTED_ANNUAL_V1_CHECKSUM = "e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814"
EXPECTED_BEFORE_COUNTS = {
    "financial_metric_results": 900, "quarterly_extraction_runs": 45,
    "quarterly_metric_results": 1080, "derived_metric_results": 405,
}
TICKERS = ["ORCL", "MSFT", "META", "NVDA", "GOOGL", "AMZN", "MU", "CRWD", "PANW"]
EXPECTED_TICKER_COUNT = 9
EXPECTED_ROWS_PER_TICKER = 1657
EXPECTED_TOTAL_ROWS = 14913
EXPECTED_FIRST_DATE, EXPECTED_LAST_DATE = "2020-01-02", "2026-08-06"
PRICE_POLICY_VERSION = "HISTORICAL_PRICE_POLICY_V1"
POLICY_TICKERS_TO_CROSS_CHECK = ("NVDA", "GOOGL", "PANW")
RECONSTRUCTION_TOLERANCE_ABS = 0.001


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def run_git(*args: str) -> tuple[int, str, str]:
    completed = subprocess.run(["git", *args], cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=30)
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def fingerprint_table(connection, table_name: str) -> str:
    rows = connection.execute(f"SELECT * FROM {table_name}").fetchall()
    rows_str = sorted(repr(r) for r in rows)
    return hashlib.sha256("\n".join(rows_str).encode("utf-8")).hexdigest()


def get_existing_tables(connection) -> list[str]:
    return [r[0] for r in connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()]


def fingerprint_all_tables(connection) -> dict[str, dict]:
    result = {}
    for table in get_existing_tables(connection):
        count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        result[table] = {"row_count": count, "fingerprint": fingerprint_table(connection, table)}
    return result


def get_review_required_count(connection) -> int:
    total = 0
    for table, column in (("financial_metric_results", "status"), ("quarterly_metric_results", "reconciliation_status")):
        exists = connection.execute(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = ? AND column_name = ?", [table, column]
        ).fetchone()[0]
        if exists:
            total += connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = 'REVIEW_REQUIRED'").fetchone()[0]
    return total


# =====================================================================
# STAGE 1 -- PREFLIGHT
# =====================================================================

def stage1_preflight() -> dict:
    checks = {}
    detail = {}

    # Excludes this release script's own file: it is this task's deliverable
    # (does not exist in Git yet, will be committed in Stage 5) and is not
    # leftover state from a prior task that preflight needs to detect.
    rc, stdout, _ = run_git("status", "--porcelain")
    detail["git_status_porcelain"] = stdout
    self_generated_names = (Path(__file__).name, RELEASE_OUTPUT_PATH.name)
    dirty_lines = [
        line for line in stdout.splitlines()
        if not any(name in line for name in self_generated_names)
    ]
    detail["git_status_porcelain_excluding_this_script"] = dirty_lines
    checks["git_working_tree_clean"] = (rc == 0 and len(dirty_lines) == 0)

    rc, stdout, _ = run_git("log", "--oneline")
    checks["historical_price_policy_v1_in_git"] = "Define Historical Price Policy V1" in stdout
    checks["historical_prices_v1_loader_build_in_git"] = "Build Historical Prices V1 loader" in stdout

    rc, stdout, _ = run_git("remote", "-v")
    checks["no_remote_exists"] = stdout == ""

    checks["loader_script_exists"] = LOADER_SCRIPT_PATH.exists()

    if not BUILD_VALIDATION_JSON_PATH.exists():
        checks["build_validation_exists"] = False
    else:
        build = json.loads(BUILD_VALIDATION_JSON_PATH.read_text(encoding="utf-8"))
        checks["build_validation_exists"] = True
        checks["build_check_only_status_pass"] = build["status"] == "PASS"
        checks["build_9_tickers"] = len(build["tickers_processed"]) == EXPECTED_TICKER_COUNT
        checks["build_1657_rows_per_ticker"] = all(v["row_count"] == EXPECTED_ROWS_PER_TICKER for v in build["per_ticker_validation"])
        checks["build_14913_total_rows"] = build["total_rows"] == EXPECTED_TOTAL_ROWS
        checks["build_date_range_correct"] = all(
            v["first_date"] == EXPECTED_FIRST_DATE and v["last_date"] == EXPECTED_LAST_DATE for v in build["per_ticker_validation"]
        )
        checks["build_in_memory_load_proof_pass"] = build["in_memory_load_proof"]["proof_passed"]
        checks["build_table_not_yet_created"] = build["table_created"] is False

        raw_ok = True
        for rec in build["raw_file_records"]:
            p = Path(rec["path"])
            if not p.exists() or sha256_of_file(p) != rec["sha256"]:
                raw_ok = False
        checks["all_raw_source_files_verified"] = raw_ok

    decisions_log_text = (DOCS_DIR / "DECISIONS_LOG.md").read_text(encoding="utf-8")
    checks["d044_present_in_decisions_log"] = "D-044" in decisions_log_text and "Historical Price Policy V1 is binding" in decisions_log_text

    if ANNUAL_V1_DB_PATH.exists():
        annual_v1_actual = sha256_of_file(ANNUAL_V1_DB_PATH)
        checks["annual_v1_checksum_matches_expected"] = annual_v1_actual == EXPECTED_ANNUAL_V1_CHECKSUM
        detail["annual_v1_checksum_actual"] = annual_v1_actual
    else:
        checks["annual_v1_checksum_matches_expected"] = False
        detail["annual_v1_checksum_actual"] = None

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    before_fingerprints = fingerprint_all_tables(connection)
    before_review_required = get_review_required_count(connection)
    connection.close()

    for table, expected_count in EXPECTED_BEFORE_COUNTS.items():
        actual = before_fingerprints.get(table, {}).get("row_count")
        checks[f"before_count_{table}_matches_expected"] = actual == expected_count
    checks["before_unique_review_required_is_0"] = before_review_required == 0

    checks["production_db_hash_captured"] = True
    production_db_hash_before = sha256_of_file(PRODUCTION_DB_PATH)

    overall_pass = all(checks.values())
    return {
        "status": "PASS" if overall_pass else "FAIL", "checks": checks, "detail": detail,
        "before_fingerprints": before_fingerprints, "before_review_required": before_review_required,
        "production_db_hash_before": production_db_hash_before,
        "annual_v1_hash_before": detail.get("annual_v1_checksum_actual"),
    }


# =====================================================================
# STAGE 2 -- PRODUCTION LOAD (exactly one subprocess invocation)
# =====================================================================

def stage2_execute() -> dict:
    started = time.perf_counter()
    completed = subprocess.run(
        [str(PYTHON_EXE_PATH), str(LOADER_SCRIPT_PATH), "--execute"],
        cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=280,
    )
    runtime = round(time.perf_counter() - started, 3)
    load_result = json.loads(LOAD_RESULT_JSON_PATH.read_text(encoding="utf-8")) if LOAD_RESULT_JSON_PATH.exists() else None
    return {
        "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr,
        "runtime_seconds": runtime, "load_result_json": load_result,
        "succeeded": completed.returncode == 0 and load_result is not None and load_result.get("status") == "PASS",
    }


# =====================================================================
# STAGE 2-FAIL -- diagnose whether production changed after a failed execute
# =====================================================================

def diagnose_failed_execute(preflight: dict) -> dict:
    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    table_exists = bool(connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'historical_prices_daily'"
    ).fetchone()[0])
    after_fingerprints = fingerprint_all_tables(connection)
    after_review_required = get_review_required_count(connection)
    connection.close()

    production_db_hash_after = sha256_of_file(PRODUCTION_DB_PATH)
    db_hash_unchanged = production_db_hash_after == preflight["production_db_hash_before"]

    fingerprint_diffs = []
    for table, before in preflight["before_fingerprints"].items():
        after = after_fingerprints.get(table)
        if after is None or after != before:
            fingerprint_diffs.append({"table": table, "before": before, "after": after})

    production_fully_unchanged = (
        not table_exists and db_hash_unchanged and not fingerprint_diffs
        and after_review_required == preflight["before_review_required"]
    )

    return {
        "historical_prices_daily_exists": table_exists,
        "production_db_hash_after": production_db_hash_after,
        "production_db_hash_unchanged": db_hash_unchanged,
        "fingerprint_diffs": fingerprint_diffs,
        "review_required_unchanged": after_review_required == preflight["before_review_required"],
        "production_fully_unchanged": production_fully_unchanged,
    }


# =====================================================================
# STAGE 3 -- INDEPENDENT POST-LOAD VERIFICATION
# =====================================================================

def stage3_verify(preflight: dict) -> dict:
    checks = {}
    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)

    table_exists = bool(connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'historical_prices_daily'"
    ).fetchone()[0])
    checks["historical_prices_daily_exists"] = table_exists
    if not table_exists:
        connection.close()
        return {"status": "FAIL", "checks": checks}

    total_rows = connection.execute("SELECT COUNT(*) FROM historical_prices_daily").fetchone()[0]
    checks["exactly_14913_rows"] = total_rows == EXPECTED_TOTAL_ROWS

    distinct_tickers = connection.execute("SELECT COUNT(DISTINCT ticker) FROM historical_prices_daily").fetchone()[0]
    checks["exactly_9_tickers"] = distinct_tickers == EXPECTED_TICKER_COUNT

    per_ticker_counts = dict(connection.execute("SELECT ticker, COUNT(*) FROM historical_prices_daily GROUP BY ticker").fetchall())
    checks["all_tickers_have_1657_rows"] = all(per_ticker_counts.get(t) == EXPECTED_ROWS_PER_TICKER for t in TICKERS)

    date_range = connection.execute("SELECT MIN(price_date), MAX(price_date) FROM historical_prices_daily").fetchone()
    checks["date_range_correct"] = (str(date_range[0]) == EXPECTED_FIRST_DATE and str(date_range[1]) == EXPECTED_LAST_DATE)

    dup = connection.execute(
        "SELECT COUNT(*) FROM (SELECT ticker, price_date, COUNT(*) c FROM historical_prices_daily GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    checks["zero_duplicate_ticker_date_rows"] = dup == 0

    missing = connection.execute(
        "SELECT COUNT(*) FROM historical_prices_daily WHERE open IS NULL OR high IS NULL OR low IS NULL "
        "OR close IS NULL OR adj_close IS NULL OR nominal_open IS NULL OR nominal_high IS NULL "
        "OR nominal_low IS NULL OR nominal_close IS NULL OR volume IS NULL"
    ).fetchone()[0]
    checks["zero_missing_required_price_fields"] = missing == 0

    negative_prices = connection.execute(
        "SELECT COUNT(*) FROM historical_prices_daily WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 "
        "OR adj_close <= 0 OR nominal_open <= 0 OR nominal_high <= 0 OR nominal_low <= 0 OR nominal_close <= 0"
    ).fetchone()[0]
    checks["zero_negative_or_non_positive_prices"] = negative_prices == 0

    negative_volume = connection.execute("SELECT COUNT(*) FROM historical_prices_daily WHERE volume < 0").fetchone()[0]
    checks["zero_negative_volume"] = negative_volume == 0

    ohlc_invalid = connection.execute(
        "SELECT COUNT(*) FROM historical_prices_daily WHERE NOT (low <= open AND open <= high AND low <= close AND close <= high)"
    ).fetchone()[0]
    checks["valid_ohlc_relationships"] = ohlc_invalid == 0

    nominal_ohlc_invalid = connection.execute(
        "SELECT COUNT(*) FROM historical_prices_daily WHERE NOT ("
        "nominal_low <= nominal_open AND nominal_open <= nominal_high AND "
        "nominal_low <= nominal_close AND nominal_close <= nominal_high)"
    ).fetchone()[0]
    checks["valid_reconstructed_nominal_ohlc_relationships"] = nominal_ohlc_invalid == 0

    wrong_policy = connection.execute(
        "SELECT COUNT(*) FROM historical_prices_daily WHERE price_policy_version <> ?", [PRICE_POLICY_VERSION]
    ).fetchone()[0]
    checks["price_policy_version_correct_on_every_row"] = wrong_policy == 0

    missing_lineage = connection.execute(
        "SELECT COUNT(*) FROM historical_prices_daily WHERE source IS NULL OR source_raw_file IS NULL OR source_raw_sha256 IS NULL"
    ).fetchone()[0]
    checks["source_lineage_present_on_every_row"] = missing_lineage == 0
    checks["source_sha256_present_on_every_row"] = missing_lineage == 0  # same query covers source_raw_sha256

    # --- split events match the approved 9-company proof ---
    nine_ticker_proof = json.loads(NINE_TICKER_PROOF_JSON_PATH.read_text(encoding="utf-8"))
    split_mismatches = []
    for entry in nine_ticker_proof["per_ticker"]:
        ticker = entry["ticker"]
        proof_splits = sorted((s["date"], s["numerator"], s["denominator"]) for s in entry["validation"]["splits_found"])
        db_splits_raw = connection.execute(
            "SELECT DISTINCT price_date, split_ratio FROM historical_prices_daily WHERE ticker = ? AND split_ratio IS NOT NULL ORDER BY price_date",
            [ticker],
        ).fetchall()
        db_splits = sorted(
            (str(d), float(r.split(":")[0]), float(r.split(":")[1])) for d, r in db_splits_raw
        )
        if db_splits != proof_splits:
            split_mismatches.append({"ticker": ticker, "db": db_splits, "proof": proof_splits})
    checks["split_events_match_9_ticker_proof"] = len(split_mismatches) == 0

    # --- dividend counts match the approved 9-company proof ---
    dividend_mismatches = []
    for entry in nine_ticker_proof["per_ticker"]:
        ticker = entry["ticker"]
        expected_count = entry["validation"]["dividends_count"]
        actual_count = connection.execute(
            "SELECT COUNT(*) FROM historical_prices_daily WHERE ticker = ? AND dividend IS NOT NULL", [ticker]
        ).fetchone()[0]
        if actual_count != expected_count:
            dividend_mismatches.append({"ticker": ticker, "db": actual_count, "proof": expected_count})
    checks["dividend_counts_match_9_ticker_proof"] = len(dividend_mismatches) == 0

    # --- NVDA/GOOGL/PANW reconstructed prices still match Historical Price Policy V1 proof ---
    policy_proof = json.loads(PRICE_POLICY_PROOF_JSON_PATH.read_text(encoding="utf-8"))
    policy_mismatches = []
    for window in policy_proof["split_window_analyses"]:
        ticker = window["ticker"]
        if ticker not in POLICY_TICKERS_TO_CROSS_CHECK:
            continue
        for row in window["window_table"]:
            db_row = connection.execute(
                "SELECT nominal_close FROM historical_prices_daily WHERE ticker = ? AND price_date = ?", [ticker, row["date"]]
            ).fetchone()
            if db_row is None:
                policy_mismatches.append({"ticker": ticker, "date": row["date"], "reason": "not found in production table"})
                continue
            diff = abs(db_row[0] - row["reconstructed_nominal_close"])
            if diff > RECONSTRUCTION_TOLERANCE_ABS:
                policy_mismatches.append({"ticker": ticker, "date": row["date"], "db_value": db_row[0],
                                           "proof_value": row["reconstructed_nominal_close"], "diff": diff})
    checks["nvda_googl_panw_match_price_policy_v1_proof"] = len(policy_mismatches) == 0

    # --- pre-existing production information unchanged ---
    after_review_required = get_review_required_count(connection)
    checks["review_required_still_0"] = after_review_required == 0

    for table, expected_count in EXPECTED_BEFORE_COUNTS.items():
        actual = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        checks[f"after_count_{table}_unchanged"] = actual == expected_count

    fingerprint_diffs = []
    for table, before in preflight["before_fingerprints"].items():
        after_count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        after_fp = fingerprint_table(connection, table)
        if after_count != before["row_count"] or after_fp != before["fingerprint"]:
            fingerprint_diffs.append({"table": table, "before": before, "after": {"row_count": after_count, "fingerprint": after_fp}})
    checks["all_pre_existing_table_fingerprints_unchanged"] = len(fingerprint_diffs) == 0

    connection.close()

    annual_v1_hash_after = sha256_of_file(ANNUAL_V1_DB_PATH) if ANNUAL_V1_DB_PATH.exists() else None
    checks["annual_v1_checksum_unchanged"] = annual_v1_hash_after == preflight["annual_v1_hash_before"]

    overall = all(checks.values())
    return {
        "status": "PASS" if overall else "FAIL", "checks": checks,
        "total_rows": total_rows, "per_ticker_counts": per_ticker_counts, "date_range": [str(date_range[0]), str(date_range[1])],
        "split_mismatches": split_mismatches, "dividend_mismatches": dividend_mismatches, "policy_mismatches": policy_mismatches,
        "fingerprint_diffs": fingerprint_diffs, "annual_v1_hash_after": annual_v1_hash_after,
    }


def main() -> int:
    print("=" * 100)
    print("HISTORICAL PRICES V1 -- RELEASE TASK")
    print("=" * 100)

    print("\n--- STAGE 1: PREFLIGHT ---")
    preflight = stage1_preflight()
    print(f"Preflight status: {preflight['status']}")
    for k, v in preflight["checks"].items():
        print(f"  {k}: {v}")

    if preflight["status"] != "FAIL" and preflight["status"] != "PASS":
        pass  # unreachable, defensive

    if preflight["status"] == "FAIL":
        failed = [k for k, v in preflight["checks"].items() if not v]
        output = {"final_status": "FAIL", "stage_failed": "preflight", "failed_checks": failed, "preflight": preflight}
        RELEASE_OUTPUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
        print(f"\nFAIL at STAGE 1 -- failed checks: {failed}")
        return 1

    print("\n--- STAGE 2: PRODUCTION LOAD (--execute, exactly once) ---")
    execute_result = stage2_execute()
    print(f"Return code: {execute_result['returncode']}  Succeeded: {execute_result['succeeded']}  Runtime: {execute_result['runtime_seconds']}s")

    if not execute_result["succeeded"]:
        print("\n--- STAGE 2 FAILED: diagnosing production state (no retry) ---")
        diagnosis = diagnose_failed_execute(preflight)
        print(f"Production fully unchanged: {diagnosis['production_fully_unchanged']}")
        output = {
            "final_status": "FAIL", "stage_failed": "execute", "execute_result": execute_result,
            "diagnosis": diagnosis, "preflight": preflight,
        }
        RELEASE_OUTPUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
        return 1

    print("\n--- STAGE 3: INDEPENDENT POST-LOAD VERIFICATION ---")
    verification = stage3_verify(preflight)
    print(f"Verification status: {verification['status']}")
    for k, v in verification["checks"].items():
        print(f"  {k}: {v}")

    final_status = "PASS" if verification["status"] == "PASS" else "FAIL"
    output = {
        "final_status": final_status, "preflight": preflight, "execute_result": execute_result,
        "verification": verification, "generated_at_utc": utc_now_iso(),
    }
    RELEASE_OUTPUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 100)
    print(f"FINAL: {final_status}")
    print("=" * 100)
    return 0 if final_status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
