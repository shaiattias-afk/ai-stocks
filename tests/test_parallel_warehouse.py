"""
tests/test_parallel_warehouse.py -- the 3 required proofs for the
parallel-ingestion work (docs/CURRENT_STATE.md, "parallel ingestion"):

1. test_parallel_matches_sequential -- a parallel run over N real locked
   filings produces byte-identical warehouse content to a sequential run
   over the same filings.
2. test_validation_failure_does_not_corrupt_warehouse -- one filing in
   a parallel batch that fails validation (a genuine WarehouseLoaderError,
   not a process crash) never gets written, and the other filings in the
   same batch commit normally; the warehouse stays fully queryable.
3. test_worker_timeout_kills_child_and_leaves_no_partial_write -- a
   per-filing timeout small enough to always fire (0.01s, before Python
   + Arelle can even start) proves the child is actually killed (no
   process left writing after subprocess.run raises) and that nothing
   is written for that filing; a normal-timeout run against the SAME
   database afterward proves the database was never left corrupted.

tests/test_rate_limiter.py covers the "never exceeds 10 requests/second"
requirement separately (no Arelle/filing dependency, always runs fast).

These tests read real locked filings under data/sec_filings_locked/ (an
Arelle DTS load, same as tests/test_golden_regression.py's dependency on
the real warehouse/production databases) and write only to temporary
DuckDB files created under pytest's tmp_path -- never to
data/database/xbrl_warehouse_proof.duckdb or ai_stock_agent.duckdb.
Marked @pytest.mark.golden (real Arelle parses, a few seconds each) but
still collected and run by default; only skipped, visibly, when the
fixture filings are not present on disk.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from stock_agent.filings.locked import LOCKED_FILINGS_DIR
from stock_agent.warehouse.loader import WAREHOUSE_TABLES, run_production_warehouse_load
from stock_agent.warehouse.parallel_loader import IngestTarget, run_parallel_warehouse_load

pytestmark = pytest.mark.golden

# 3 small, fast-parsing real 10-Qs already locked on disk for this
# project's approved 9-company universe (CRWD FY2022 quarters).
FIXTURE_TARGETS = [
    IngestTarget("CRWD", "2021-04-30", "10-Q"),
    IngestTarget("CRWD", "2021-07-31", "10-Q"),
    IngestTarget("CRWD", "2021-10-31", "10-Q"),
]

_FIXTURES_PRESENT = all(
    (LOCKED_FILINGS_DIR / t.ticker).exists() for t in FIXTURE_TARGETS
)
_SKIP_REASON = (
    f"locked fixture filings not present under {LOCKED_FILINGS_DIR} -- these tests "
    "parse real, already-locked SEC filings and cannot run without them"
)


def _table_counts(db_path: Path) -> dict[str, int]:
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in WAREHOUSE_TABLES}
    finally:
        connection.close()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason=_SKIP_REASON)
def test_parallel_matches_sequential(tmp_path: Path) -> None:
    sequential_db = tmp_path / "sequential.duckdb"
    parallel_db = tmp_path / "parallel.duckdb"

    for target in FIXTURE_TARGETS:
        result = run_production_warehouse_load(target.ticker, target.report_date, sequential_db, form=target.form)
        assert result["status"] == "PASS"

    parallel_result = run_parallel_warehouse_load(FIXTURE_TARGETS, parallel_db, max_workers=3)
    assert parallel_result["status_counts"] == {"PASS": len(FIXTURE_TARGETS)}

    assert _table_counts(sequential_db) == _table_counts(parallel_db)

    seq_conn = duckdb.connect(str(sequential_db), read_only=True)
    par_conn = duckdb.connect(str(parallel_db), read_only=True)
    try:
        seq_accessions = {row[0] for row in seq_conn.execute("SELECT DISTINCT accession_number FROM xbrl_facts").fetchall()}
        par_accessions = {row[0] for row in par_conn.execute("SELECT DISTINCT accession_number FROM xbrl_facts").fetchall()}
        assert seq_accessions == par_accessions
        assert len(seq_accessions) == len(FIXTURE_TARGETS)

        # deep, order-independent content comparison for every accession and every table
        for accession_number in seq_accessions:
            for table in WAREHOUSE_TABLES:
                if table == "warehouse_runs":
                    continue
                seq_df = seq_conn.execute(f"SELECT * FROM {table} WHERE accession_number = ?", [accession_number]).fetchdf()
                par_df = par_conn.execute(f"SELECT * FROM {table} WHERE accession_number = ?", [accession_number]).fetchdf()
                seq_sorted = seq_df.sort_values(list(seq_df.columns)).reset_index(drop=True)
                par_sorted = par_df.sort_values(list(par_df.columns)).reset_index(drop=True)
                assert seq_sorted.equals(par_sorted), f"{table} content differs for {accession_number}"

        assert {row[0] for row in seq_conn.execute("SELECT status FROM warehouse_runs").fetchall()} == {"PASS"}
        assert {row[0] for row in par_conn.execute("SELECT status FROM warehouse_runs").fetchall()} == {"PASS"}
    finally:
        seq_conn.close()
        par_conn.close()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason=_SKIP_REASON)
def test_validation_failure_does_not_corrupt_warehouse(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed_batch.duckdb"
    good_targets = FIXTURE_TARGETS[:2]
    # a report_date with no locked manifest -- a real, deterministic
    # WarehouseLoaderError (PACKAGE_INCOMPLETE), not a simulated failure.
    bad_target = IngestTarget("CRWD", "1999-01-01", "10-Q")

    result = run_parallel_warehouse_load(good_targets + [bad_target], db_path, max_workers=3)

    assert result["status_counts"].get("PASS") == len(good_targets)
    assert result["status_counts"].get("FAIL") == 1
    bad_result = next(r for r in result["results"] if r["report_date"] == "1999-01-01")
    assert bad_result["status"] == "FAIL"
    assert bad_result["category"] == "PACKAGE_INCOMPLETE"

    # the database must still be a valid, queryable warehouse containing
    # exactly the 2 successful filings -- zero rows for the failed one.
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        accessions = {row[0] for row in connection.execute("SELECT DISTINCT accession_number FROM xbrl_facts").fetchall()}
        assert len(accessions) == len(good_targets)
        run_count = connection.execute("SELECT COUNT(*) FROM warehouse_runs").fetchone()[0]
        assert run_count == len(good_targets)
        assert set(connection.execute("SELECT status FROM warehouse_runs").fetchall()) == {("PASS",)}
    finally:
        connection.close()


@pytest.mark.skipif(not _FIXTURES_PRESENT, reason=_SKIP_REASON)
def test_worker_timeout_kills_child_and_leaves_no_partial_write(tmp_path: Path) -> None:
    db_path = tmp_path / "timeout_then_recover.duckdb"

    # Step 1: a timeout so small (0.01s) it always fires before the
    # child's Python interpreter can even finish starting -- proves the
    # child is force-killed (subprocess.run's own TimeoutExpired path
    # calls .kill()) and that a killed worker writes nothing.
    timeout_result = run_parallel_warehouse_load(
        [FIXTURE_TARGETS[0]], db_path, max_workers=1, per_filing_timeout_seconds=0.01,
    )
    assert timeout_result["status_counts"] == {"TIMEOUT": 1}

    # a valid, openable DuckDB file (proves the file itself is not
    # corrupted) with no schema at all -- write_parsed_filing (the only
    # place schema/tables get created) was never reached, because no
    # filing ever finished parsing.
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        existing_tables = {
            row[0] for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name = ANY(?)", [WAREHOUSE_TABLES]
            ).fetchall()
        }
        for table in existing_tables:
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} has {count} rows after an all-TIMEOUT batch"
    finally:
        connection.close()

    # Step 2: reusing the SAME database file with a normal timeout must
    # succeed cleanly -- proves the timeout above left the database file
    # itself uncorrupted, not merely empty of the one attempted filing.
    recovery_result = run_parallel_warehouse_load(FIXTURE_TARGETS[:2], db_path, max_workers=2)
    assert recovery_result["status_counts"] == {"PASS": 2}

    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        accessions = {row[0] for row in connection.execute("SELECT DISTINCT accession_number FROM xbrl_facts").fetchall()}
        assert len(accessions) == 2
    finally:
        connection.close()
