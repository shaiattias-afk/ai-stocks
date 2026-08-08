"""Integrity tests for the compressed SEC filing archive
(`stock_agent.filings.archive`).

Ported from `scripts/164_filings_archive_tests.py`, which ran as a
standalone script writing a JSON report. The assertions are unchanged;
they now run as part of the pytest suite so a regression is caught on
every run rather than only when someone remembers to invoke the script.

Two groups:
  * pure tests (corruption rejection, truncation rejection) build their
    own scratch archive in a temp directory -- no project data required,
    always run.
  * round-trip tests compare archived bytes against the original files
    still on disk under data/sec_filings_locked/; they skip cleanly if
    either the archive or the on-disk reference copy is absent, so the
    suite stays green on a machine that has the code but not the 4.4 GB
    of locked filings.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb
import pytest

from stock_agent import PROJECT_DIR
from stock_agent.filings import archive

LOCKED_FILINGS_DIR = PROJECT_DIR / "data" / "sec_filings_locked"

# Deliberately spans every structural variation present in this project's
# universe -- linkbases embedded in the .xsd vs. shipped separately, a
# pre-Inline-XBRL filing with a standalone instance document (D-041), a
# filing with only three needed files, and four different filer agents.
SAMPLE_ACCESSIONS = [
    ("MSFT", "0000950170-24-087843"),   # 10-K, linkbases embedded inside the .xsd
    ("MSFT", "0000950170-23-014423"),   # 10-Q, separate _cal/_def/_lab/_pre files
    ("AMZN", "0001018724-25-000004"),   # 10-K, separate linkbase files, different filer agent
    ("NVDA", "0001045810-19-000079"),   # pre-Inline-XBRL 10-Q, standalone instance document (D-041)
    ("ORCL", "0000950170-24-075605"),   # 10-K, only 3 needed files (no separate linkbases at all)
    ("PANW", "0001327567-21-000029"),   # 10-K, different filer agent again
]


def _sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@pytest.fixture(scope="module")
def archive_connection():
    if not archive.ARCHIVE_DB_PATH.exists():
        pytest.skip(f"filing archive not present at {archive.ARCHIVE_DB_PATH}")
    connection = duckdb.connect(database=str(archive.ARCHIVE_DB_PATH), read_only=True)
    try:
        yield connection
    finally:
        connection.close()


def _locked_dir_for(ticker: str, accession_number: str) -> Path | None:
    compact = accession_number.replace("-", "")
    manifest_paths = sorted((LOCKED_FILINGS_DIR / ticker).glob(f"{compact}/locked_filing_manifest.json"))
    if len(manifest_paths) != 1:
        return None
    return manifest_paths[0].parent


@pytest.mark.parametrize(("ticker", "accession_number"), SAMPLE_ACCESSIONS)
def test_round_trip_is_byte_identical(archive_connection, ticker: str, accession_number: str) -> None:
    """Every archived file, extracted back out, must be byte-identical to
    the original SEC file, and its stored SHA-256 must match a fresh
    recompute of the original."""
    locked_dir = _locked_dir_for(ticker, accession_number)
    if locked_dir is None:
        pytest.skip(f"on-disk reference copy not available for {ticker}/{accession_number}")

    with archive.extracted_filing(archive_connection, accession_number) as (temp_dir, _manifest_row):
        extracted_files = sorted(p.name for p in temp_dir.iterdir())
        assert extracted_files, f"{accession_number}: extracted zero files"

        for file_name in extracted_files:
            original_path = locked_dir / file_name
            extracted_path = temp_dir / file_name
            assert original_path.exists(), (
                f"{accession_number}/{file_name}: original file no longer on disk to compare against"
            )

            assert original_path.read_bytes() == extracted_path.read_bytes(), (
                f"{accession_number}/{file_name}: extracted bytes differ from original -- NOT byte-identical"
            )

            original_sha256 = _sha256_of_file(original_path)
            assert _sha256_of_file(extracted_path) == original_sha256, (
                f"{accession_number}/{file_name}: SHA-256 mismatch original vs extracted"
            )

            stored_sha256 = archive_connection.execute(
                "SELECT sha256 FROM filing_archive_files WHERE accession_number = ? AND file_name = ?",
                [accession_number, file_name],
            ).fetchone()[0]
            assert stored_sha256 == original_sha256, (
                f"{accession_number}/{file_name}: stored SHA-256 does not match a fresh recompute"
            )


def test_extracted_dir_is_cleaned_up(archive_connection) -> None:
    """The temp directory must exist inside the context manager and be
    gone afterward -- extraction must never leak filing bytes to disk."""
    _ticker, accession_number = SAMPLE_ACCESSIONS[0]
    with archive.extracted_filing(archive_connection, accession_number) as (temp_dir, _manifest_row):
        captured_temp_dir = temp_dir
        assert captured_temp_dir.exists(), "temp_dir should exist inside the context manager"

    assert not captured_temp_dir.exists(), (
        f"temp_dir was not cleaned up after the context manager exited: {captured_temp_dir}"
    )


def test_corrupted_blob_is_rejected(tmp_path: Path) -> None:
    """A tampered BLOB must raise CorruptedArchiveError and must never
    result in a file being written to disk."""
    scratch_db_path = tmp_path / "scratch_archive.duckdb"
    accession_number = "0000000000-00-000001"

    connection = duckdb.connect(database=str(scratch_db_path))
    try:
        archive.create_archive_schema(connection)

        original_bytes = b"<xbrl>this is a fake filing file for corruption testing</xbrl>" * 100
        record = archive.archive_file(connection, accession_number, "fake_file.xml", original_bytes)

        # sanity: uncorrupted extraction must succeed first
        extract_dir = tmp_path / "extract_ok"
        extract_dir.mkdir()
        archive.verify_and_extract_file(connection, accession_number, "fake_file.xml", extract_dir)
        assert (extract_dir / "fake_file.xml").read_bytes() == original_bytes

        # now corrupt the stored BLOB: flip every bit of one byte mid-stream
        row = connection.execute(
            "SELECT content_gz FROM filing_archive_files WHERE accession_number = ? AND file_name = ?",
            [accession_number, "fake_file.xml"],
        ).fetchone()
        content_gz = bytearray(row[0])
        content_gz[len(content_gz) // 2] ^= 0xFF
        connection.execute(
            "UPDATE filing_archive_files SET content_gz = ? WHERE accession_number = ? AND file_name = ?",
            [bytes(content_gz), accession_number, "fake_file.xml"],
        )

        corrupt_extract_dir = tmp_path / "extract_corrupt"
        corrupt_extract_dir.mkdir()
        with pytest.raises(archive.CorruptedArchiveError):
            archive.verify_and_extract_file(connection, accession_number, "fake_file.xml", corrupt_extract_dir)

        assert not any(corrupt_extract_dir.iterdir()), (
            "a file was written to disk from a corrupted BLOB -- must never happen"
        )
        assert record["sha256"]
    finally:
        connection.close()


def test_truncated_download_is_rejected(tmp_path: Path) -> None:
    """A download shorter than the size SEC declared must raise and must
    never be stored; a correctly-sized one must still store normally."""
    scratch_db_path = tmp_path / "scratch_archive.duckdb"
    accession_number = "0000000000-00-000000"

    connection = duckdb.connect(database=str(scratch_db_path))
    try:
        archive.create_archive_schema(connection)

        full_bytes = b"0123456789" * 10_000          # pretend SEC reported this size
        truncated_bytes = full_bytes[: len(full_bytes) // 2]   # connection cut off halfway

        with pytest.raises(archive.TruncatedDownloadError):
            archive.archive_file(
                connection, accession_number, "truncated_test.xml",
                truncated_bytes, declared_size=len(full_bytes),
            )

        stored = connection.execute(
            "SELECT COUNT(*) FROM filing_archive_files WHERE accession_number = ?",
            [accession_number],
        ).fetchone()[0]
        assert stored == 0, f"a truncated download was stored anyway ({stored} row(s)) -- must never happen"

        # a correctly-sized download for the same accession must still succeed
        archive.archive_file(
            connection, accession_number, "complete_test.xml",
            full_bytes, declared_size=len(full_bytes),
        )
        stored = connection.execute(
            "SELECT COUNT(*) FROM filing_archive_files WHERE accession_number = ?",
            [accession_number],
        ).fetchone()[0]
        assert stored == 1, "a correctly-sized download was not stored"
    finally:
        connection.close()


def test_is_needed_file_selects_only_the_xbrl_document_set() -> None:
    """The needed-file rule is what cuts ~106 files per filing down to ~7.
    Guard both directions: everything the pipeline reads is kept, and the
    large duplicates it never reads are excluded."""
    primary = "msft-20230331.htm"

    for kept in (
        primary,
        "msft-20230331.xsd",
        "msft-20230331_cal.xml",
        "msft-20230331_def.xml",
        "msft-20230331_lab.xml",
        "msft-20230331_pre.xml",
        "FilingSummary.xml",
        "nvda-20190428.xml",          # pre-Inline-XBRL standalone instance (D-041)
    ):
        assert archive.is_needed_file(kept, primary), f"{kept} should be archived"

    for excluded in (
        "0000950170-23-014423.txt",   # full submission, duplicates everything (31 MB)
        "R11.htm",                    # SEC rendered viewer page; HTML tables banned (D-004)
        "R28.htm",
        "MetaLinks.json",
        "msft-20230331_htm.xml",      # generated instance; Arelle reads the .htm
        "0000950170-23-014423-index.html",
        "logo.jpg",
    ):
        assert not archive.is_needed_file(excluded, primary), f"{excluded} should NOT be archived"
