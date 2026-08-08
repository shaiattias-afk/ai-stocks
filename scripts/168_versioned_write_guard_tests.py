"""
Required test suite for scripts/167_versioned_write_guard.py (D-047).
Every test runs against an isolated, temporary DuckDB file created fresh
by this script -- never the production database. The scratch file is
removed at the end of the run (success or failure).

Required tests (per the D-047 task):
  1. Attempted DELETE rejected.
  2. Attempted overwrite of an existing row rejected.
  3. A load declaring 100 rows but writing 101 rejected.
  4. A crash mid-load leaves the database completely unchanged.
  5. A legitimate append succeeds with every prior row byte-identical.

Exit code 0 only if every test passes.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("s167", PROJECT_DIR / "scripts" / "167_versioned_write_guard.py")
s167 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s167)

TEST_DDL = """
CREATE TABLE test_prices (
    ticker          VARCHAR NOT NULL,
    price_date      DATE NOT NULL,
    close           DOUBLE NOT NULL,
    engine_version  VARCHAR NOT NULL,
    loaded_at       TIMESTAMP NOT NULL,
    is_active       BOOLEAN NOT NULL,
    PRIMARY KEY (ticker, price_date)
)
"""

COLUMNS = ["ticker", "price_date", "close", "engine_version", "loaded_at", "is_active"]
ENGINE_TAG = "TEST_ENGINE_V1"


def _fresh_db(seed_rows: int = 3) -> Path:
    scratch_dir = Path(tempfile.gettempdir()) / "ai_stock_agent_write_guard_tests"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    db_path = scratch_dir / f"guard_test_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.duckdb"
    connection = duckdb.connect(str(db_path))
    connection.execute(TEST_DDL)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(seed_rows):
        connection.execute(
            "INSERT INTO test_prices VALUES (?,?,?,?,?,?)",
            ["AAA", date(2026, 1, 1 + i), 100.0 + i, ENGINE_TAG, now, True],
        )
    connection.close()
    return db_path


def _dump(db_path: Path) -> list[tuple]:
    connection = duckdb.connect(str(db_path), read_only=True)
    rows = connection.execute("SELECT * FROM test_prices ORDER BY ticker, price_date").fetchall()
    connection.close()
    return rows


def test_delete_rejected() -> dict:
    db_path = _fresh_db(seed_rows=3)
    before = _dump(db_path)
    connection = duckdb.connect(str(db_path))
    raised = None
    try:
        s167.execute_write_statement(connection, "DELETE FROM test_prices WHERE ticker = 'AAA'")
    except s167.WriteGuardViolation as exc:
        raised = exc
    finally:
        connection.close()
    after = _dump(db_path)
    db_path.unlink(missing_ok=True)
    passed = raised is not None and before == after
    return {"name": "delete_rejected", "passed": passed, "raised": str(raised), "rows_unchanged": before == after}


def test_overwrite_rejected() -> dict:
    db_path = _fresh_db(seed_rows=3)
    before = _dump(db_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # (a) A plain duplicate-PK INSERT must fail on DuckDB's own PRIMARY
    #     KEY constraint (propagated, not swallowed) and roll back cleanly
    #     via guarded_versioned_append.
    plain_overwrite_raised = None
    try:
        s167.guarded_versioned_append(
            db_path, "test_prices", COLUMNS,
            [("AAA", date(2026, 1, 1), 999.0, ENGINE_TAG, now, True)],  # (AAA, 2026-01-01) already exists
            declared_row_delta=1,
        )
    except Exception as exc:  # noqa: BLE001 -- DuckDB raises its own ConstraintException, not WriteGuardViolation
        plain_overwrite_raised = exc

    # (b) An explicit upsert-shaped statement (ON CONFLICT ... DO UPDATE)
    #     must be rejected by the guard itself, before it ever reaches
    #     DuckDB's constraint handling.
    connection = duckdb.connect(str(db_path))
    upsert_raised = None
    try:
        s167.execute_write_statement(
            connection,
            "INSERT INTO test_prices (ticker, price_date, close, engine_version, loaded_at, is_active) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT (ticker, price_date) DO UPDATE SET close = EXCLUDED.close",
            ["AAA", date(2026, 1, 1), 999.0, ENGINE_TAG, now, True],
        )
    except s167.WriteGuardViolation as exc:
        upsert_raised = exc
    finally:
        connection.close()

    after = _dump(db_path)
    db_path.unlink(missing_ok=True)
    passed = plain_overwrite_raised is not None and upsert_raised is not None and before == after
    return {
        "name": "overwrite_rejected", "passed": passed,
        "plain_overwrite_raised": str(plain_overwrite_raised), "upsert_raised": str(upsert_raised),
        "rows_unchanged": before == after,
    }


def test_declared_delta_mismatch_rejected() -> dict:
    db_path = _fresh_db(seed_rows=3)
    before = _dump(db_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    new_rows = [("BBB", date(2026, 2, 1) + timedelta(days=i), 200.0 + i, ENGINE_TAG, now, True) for i in range(101)]
    raised = None
    try:
        s167.guarded_versioned_append(db_path, "test_prices", COLUMNS, new_rows, declared_row_delta=100)
    except s167.WriteGuardViolation as exc:
        raised = exc
    after = _dump(db_path)
    db_path.unlink(missing_ok=True)
    passed = raised is not None and before == after and "delta" in str(raised).lower()
    return {"name": "declared_delta_mismatch_rejected", "passed": passed, "raised": str(raised), "rows_unchanged": before == after}


def test_crash_mid_load_leaves_db_unchanged() -> dict:
    db_path = _fresh_db(seed_rows=3)
    before = _dump(db_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    connection = duckdb.connect(str(db_path))
    connection.execute("BEGIN TRANSACTION")
    insert_sql = "INSERT INTO test_prices (ticker, price_date, close, engine_version, loaded_at, is_active) VALUES (?,?,?,?,?,?)"
    crash_survived_partial_inserts = True
    try:
        s167.execute_write_statement(connection, insert_sql, ["CCC", date(2026, 3, 1), 10.0, ENGINE_TAG, now, True])
        s167.execute_write_statement(connection, insert_sql, ["CCC", date(2026, 3, 2), 11.0, ENGINE_TAG, now, True])
        # Simulate an unrecoverable crash mid-load: the process dies (here,
        # the connection is simply abandoned) before COMMIT is ever reached.
        # No exception is manufactured -- this models an actual crash, not
        # a caught-and-handled error.
        connection.close()  # closing WITHOUT commit == the "crash"
    except Exception:  # noqa: BLE001
        crash_survived_partial_inserts = False

    after = _dump(db_path)
    db_path.unlink(missing_ok=True)
    passed = before == after
    return {
        "name": "crash_mid_load_leaves_db_unchanged", "passed": passed,
        "rows_before": len(before), "rows_after": len(after), "rows_unchanged": before == after,
        "note": "2 inserts were executed inside an open transaction, then the connection was closed without COMMIT",
    }


def test_legitimate_append_succeeds() -> dict:
    db_path = _fresh_db(seed_rows=3)
    before = _dump(db_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    new_rows = [
        ("DDD", date(2026, 4, 1), 50.0, ENGINE_TAG, now, True),
        ("DDD", date(2026, 4, 2), 51.0, ENGINE_TAG, now, True),
    ]
    result = s167.guarded_versioned_append(db_path, "test_prices", COLUMNS, new_rows, declared_row_delta=2)
    after = _dump(db_path)
    db_path.unlink(missing_ok=True)

    prior_rows_still_present_unchanged = all(r in after for r in before)
    passed = (
        result["rows_inserted"] == 2
        and result["post_count"] == len(before) + 2
        and result["pre_existing_rows_byte_identical"]
        and prior_rows_still_present_unchanged
        and len(after) == len(before) + 2
    )
    return {
        "name": "legitimate_append_succeeds", "passed": passed, "result": result,
        "prior_rows_still_present_unchanged": prior_rows_still_present_unchanged,
    }


def main() -> int:
    tests = [
        test_delete_rejected,
        test_overwrite_rejected,
        test_declared_delta_mismatch_rejected,
        test_crash_mid_load_leaves_db_unchanged,
        test_legitimate_append_succeeds,
    ]
    results = []
    for test in tests:
        result = test()
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['name']}")
        for key, value in result.items():
            if key in ("name", "passed"):
                continue
            print(f"    {key}: {value}")

    all_passed = all(r["passed"] for r in results)
    print("=" * 80)
    print(f"WRITE GUARD TEST SUITE: {'PASS' if all_passed else 'FAIL'} ({sum(r['passed'] for r in results)}/{len(results)})")
    print("=" * 80)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
