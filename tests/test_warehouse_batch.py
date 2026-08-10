"""Tests for parallel warehouse ingestion (`stock_agent.warehouse.batch`).

The load path shells out to Arelle, so these tests need the compressed
filing archive present; they skip cleanly when it is absent, keeping the
suite portable to a checkout without the (gitignored) databases.

The central guarantee under test: **parallel ingestion produces exactly
the same warehouse as sequential ingestion**. Everything else here
protects the failure modes -- a broken filing must never be silently
dropped, and a failed batch must never leave a half-merged warehouse.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from stock_agent.filings import archive
from stock_agent.warehouse import batch, extract as wh_extract

pytestmark = pytest.mark.golden

# Two structurally different filings: a modern Inline-XBRL 10-K, and the
# pre-Inline-XBRL 10-Q whose facts live in a standalone instance
# document (D-041). Kept small -- this test invokes Arelle for real.
SAMPLE = [
    "0000950170-24-087843",  # MSFT FY2024 10-K, Inline XBRL
    "0001045810-19-000079",  # NVDA 2019 Q1 10-Q, standalone instance
]

FACT_COLUMNS = (
    "accession_number, fact_index, concept_qname, value_raw, value_numeric,"
    " decimals, unit_id, context_id, period_start, period_end, instant_date,"
    " dimensions_json, is_nil"
)


def _require_archive() -> None:
    if not archive.ARCHIVE_DB_PATH.exists():
        pytest.skip(f"filing archive not present at {archive.ARCHIVE_DB_PATH}")
    connection = duckdb.connect(database=str(archive.ARCHIVE_DB_PATH), read_only=True)
    try:
        missing = [
            accession for accession in SAMPLE
            if connection.execute(
                "SELECT COUNT(*) FROM filing_archive_manifest WHERE accession_number = ?",
                [accession],
            ).fetchone()[0] == 0
        ]
    finally:
        connection.close()
    if missing:
        pytest.skip(f"sample accessions not in archive: {missing}")


def test_parallel_output_is_identical_to_sequential(tmp_path: Path) -> None:
    """The whole justification for the parallel path: same input, same
    warehouse. Compared at row-count level for every table AND at full
    row-content level for facts."""
    _require_archive()

    sequential_db = tmp_path / "sequential.duckdb"
    parallel_db = tmp_path / "parallel.duckdb"

    sequential_report = batch.load_many_sequential(SAMPLE, sequential_db)
    assert sequential_report["status"] == "PASS"

    parallel_report = batch.load_many(
        SAMPLE, parallel_db, workers=2, warm_cache=False, progress=False,
    )
    assert parallel_report["status"] == "PASS", parallel_report.get("failed")

    sequential = duckdb.connect(str(sequential_db), read_only=True)
    parallel = duckdb.connect(str(parallel_db), read_only=True)
    try:
        for table in wh_extract.WAREHOUSE_TABLES:
            sequential_count = sequential.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            parallel_count = parallel.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert sequential_count == parallel_count, (
                f"{table}: sequential={sequential_count} parallel={parallel_count}"
            )

        order_by = "ORDER BY accession_number, fact_index"
        sequential_facts = sequential.execute(f"SELECT {FACT_COLUMNS} FROM xbrl_facts {order_by}").fetchall()
        parallel_facts = parallel.execute(f"SELECT {FACT_COLUMNS} FROM xbrl_facts {order_by}").fetchall()
        assert sequential_facts == parallel_facts, "fact row content differs between parallel and sequential"
        assert sequential_facts, "sample produced no facts -- test would be vacuous"
    finally:
        sequential.close()
        parallel.close()


def test_failed_filing_is_reported_and_nothing_is_merged(tmp_path: Path) -> None:
    """A filing that cannot be loaded must surface as a FAIL, and the
    target warehouse must not be created at all -- a partially ingested
    warehouse is never presented as a complete one."""
    _require_archive()

    target = tmp_path / "should_not_exist.duckdb"
    report = batch.load_many(
        [SAMPLE[0], "0000000000-00-000000"],  # second accession is not in the archive
        target, workers=2, warm_cache=False, progress=False,
    )

    assert report["status"] == "FAIL"
    assert report["failed"], "a missing accession must be reported, not silently skipped"
    assert any(f["accession_number"] == "0000000000-00-000000" for f in report["failed"])
    assert not target.exists(), "target warehouse was written despite a failed filing"


def test_merge_leaves_no_staging_file_behind(tmp_path: Path) -> None:
    """The atomic merge builds a `.staging` database and renames it into
    place; no staging artifact may survive a successful run."""
    _require_archive()

    target = tmp_path / "merged.duckdb"
    report = batch.load_many(SAMPLE[:1], target, workers=1, warm_cache=False, progress=False)

    assert report["status"] == "PASS"
    assert target.exists()
    assert not list(tmp_path.glob("*.staging")), "a staging database was left behind"


def test_batch_survives_more_filings_than_one_chunk(tmp_path: Path) -> None:
    """Regression test for a real deadlock.

    The first implementation used `ProcessPoolExecutor(max_tasks_per_child=N)`.
    On Windows, with 8 workers and N=8, every worker reached its retirement
    limit on the same task and the pool hung permanently during simultaneous
    respawn -- progress froze at exactly 64 (= 8 x 8) shards with zero worker
    processes alive and the parent blocked in `as_completed`.

    Chunked pools replaced that mechanism. This test drives more filings than
    fit in one chunk, with a chunk size smaller than the worker count, so the
    chunk-boundary path is genuinely exercised rather than assumed.
    """
    _require_archive()

    accessions = batch.all_archived_accessions()[:6]
    if len(accessions) < 4:
        pytest.skip("not enough archived accessions to span multiple chunks")

    target = tmp_path / "chunked.duckdb"
    report = batch.load_many(
        accessions, target, workers=3, chunk_size=2,
        warm_cache=False, progress=False,
    )

    assert report["status"] == "PASS", report.get("failed")
    assert len(report["results"]) == len(accessions), "a chunk boundary dropped filings"
    assert {r["accession_number"] for r in report["results"]} == set(accessions)

    connection = duckdb.connect(str(target), read_only=True)
    try:
        loaded = connection.execute("SELECT COUNT(DISTINCT accession_number) FROM xbrl_facts").fetchone()[0]
    finally:
        connection.close()
    assert loaded == len(accessions), "not every filing survived the chunked merge"


def test_all_archived_accessions_excludes_filings_without_xbrl() -> None:
    """`all_archived_accessions` offers up only filings that can actually
    be parsed.

    A filing with no XBRL document set is still archived on purpose -- the
    filing is real, and its lack of machine-readable data is a true fact
    about that company's history that a backtest should see. But handing
    one to Arelle can only fail, so it is excluded from the parse list.

    Measured example: Airbnb's FY2020 10-K, their first after a December
    2020 IPO, contains 11 files and zero XBRL (their FY2021 filing has
    105). SEC permits a newly public company to omit XBRL from its first
    annual report, so this recurs for any company that lists mid-window.
    """
    _require_archive()

    connection = duckdb.connect(database=str(archive.ARCHIVE_DB_PATH), read_only=True)
    try:
        total = connection.execute("SELECT COUNT(*) FROM filing_archive_manifest").fetchone()[0]
        without_xbrl = connection.execute(
            "SELECT COUNT(*) FROM filing_archive_manifest WHERE source = ?",
            [archive.NO_XBRL_DOCUMENT_SET],
        ).fetchone()[0]
    finally:
        connection.close()

    parseable = batch.all_archived_accessions()
    everything = batch.all_archived_accessions(parseable_only=False)

    assert len(everything) == total, "parseable_only=False must return every archived filing"
    assert len(parseable) == total - without_xbrl, (
        "the parse list must exclude filings that carry no XBRL document set"
    )
    assert set(parseable) <= set(everything)
    assert len(set(parseable)) == len(parseable), "duplicate accession numbers returned"


def test_has_xbrl_document_set_distinguishes_real_filings() -> None:
    """A primary document alone is not XBRL; a schema, linkbase or
    instance document is what makes a filing machine-readable."""
    assert not archive.has_xbrl_document_set(["airbnb-10k.htm"])
    assert not archive.has_xbrl_document_set([])
    assert not archive.has_xbrl_document_set(["abnb-20201231.htm", "FilingSummary.xml"])

    assert archive.has_xbrl_document_set(["msft-20240630.htm", "msft-20240630.xsd"])
    assert archive.has_xbrl_document_set(["x.htm", "x_cal.xml"])
    assert archive.has_xbrl_document_set(["x.htm", "nvda-20190428.xml"])  # standalone instance
