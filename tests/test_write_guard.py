"""
tests/test_write_guard.py -- pytest port of the former
scripts/168_versioned_write_guard_tests.py (D-047's required test suite
for stock_agent.storage.write_guard). Every test runs against an
isolated, temporary DuckDB file created fresh by this module -- never
the production database. The scratch file is removed at the end of each
test (success or failure) via a pytest fixture.

Required tests (per D-047):
  1. Attempted DELETE rejected.
  2. Attempted overwrite of an existing row rejected (both a plain
     duplicate-PK INSERT and an explicit ON CONFLICT ... DO UPDATE).
  3. A load declaring 100 rows but writing 101 rejected.
  4. A crash mid-load leaves the database completely unchanged.
  5. A legitimate append succeeds with every prior row byte-identical.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from stock_agent.storage.write_guard import (
    WriteGuardViolation,
    execute_write_statement,
    guarded_versioned_append,
)

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


@pytest.fixture
def db_path():
    path = _fresh_db(seed_rows=3)
    yield path
    path.unlink(missing_ok=True)


def test_delete_rejected(db_path):
    before = _dump(db_path)
    connection = duckdb.connect(str(db_path))
    try:
        with pytest.raises(WriteGuardViolation):
            execute_write_statement(connection, "DELETE FROM test_prices WHERE ticker = 'AAA'")
    finally:
        connection.close()
    assert _dump(db_path) == before


def test_plain_duplicate_pk_insert_rejected(db_path):
    before = _dump(db_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with pytest.raises(Exception):  # DuckDB's own ConstraintException, propagated, not swallowed
        guarded_versioned_append(
            db_path, "test_prices", COLUMNS,
            [("AAA", date(2026, 1, 1), 999.0, ENGINE_TAG, now, True)],  # (AAA, 2026-01-01) already exists
            declared_row_delta=1,
        )
    assert _dump(db_path) == before


def test_explicit_upsert_rejected_by_guard_before_reaching_duckdb(db_path):
    before = _dump(db_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    connection = duckdb.connect(str(db_path))
    try:
        with pytest.raises(WriteGuardViolation):
            execute_write_statement(
                connection,
                "INSERT INTO test_prices (ticker, price_date, close, engine_version, loaded_at, is_active) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT (ticker, price_date) DO UPDATE SET close = EXCLUDED.close",
                ["AAA", date(2026, 1, 1), 999.0, ENGINE_TAG, now, True],
            )
    finally:
        connection.close()
    assert _dump(db_path) == before


def test_declared_delta_mismatch_rejected(db_path):
    before = _dump(db_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    new_rows = [("BBB", date(2026, 2, 1) + timedelta(days=i), 200.0 + i, ENGINE_TAG, now, True) for i in range(101)]
    with pytest.raises(WriteGuardViolation, match="(?i)delta"):
        guarded_versioned_append(db_path, "test_prices", COLUMNS, new_rows, declared_row_delta=100)
    assert _dump(db_path) == before


def test_crash_mid_load_leaves_db_unchanged(db_path):
    before = _dump(db_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    connection = duckdb.connect(str(db_path))
    connection.execute("BEGIN TRANSACTION")
    insert_sql = "INSERT INTO test_prices (ticker, price_date, close, engine_version, loaded_at, is_active) VALUES (?,?,?,?,?,?)"
    execute_write_statement(connection, insert_sql, ["CCC", date(2026, 3, 1), 10.0, ENGINE_TAG, now, True])
    execute_write_statement(connection, insert_sql, ["CCC", date(2026, 3, 2), 11.0, ENGINE_TAG, now, True])
    # Simulate an unrecoverable crash mid-load: the process dies (here, the
    # connection is simply abandoned) before COMMIT is ever reached. No
    # exception is manufactured -- this models an actual crash, not a
    # caught-and-handled error.
    connection.close()  # closing WITHOUT commit == the "crash"

    assert _dump(db_path) == before


def test_legitimate_append_succeeds(db_path):
    before = _dump(db_path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    new_rows = [
        ("DDD", date(2026, 4, 1), 50.0, ENGINE_TAG, now, True),
        ("DDD", date(2026, 4, 2), 51.0, ENGINE_TAG, now, True),
    ]
    result = guarded_versioned_append(db_path, "test_prices", COLUMNS, new_rows, declared_row_delta=2)
    after = _dump(db_path)

    assert result["rows_inserted"] == 2
    assert result["post_count"] == len(before) + 2
    assert result["pre_existing_rows_byte_identical"]
    assert all(r in after for r in before)
    assert len(after) == len(before) + 2
