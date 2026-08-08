"""
Required integrity tests for the compressed filing archive
(scripts/161/162/163). Run after the backfill (scripts/163) has
populated data/database/filings_archive.duckdb.

Tests (all four required by this milestone's acceptance criteria):
  1. ROUND_TRIP_BYTE_IDENTICAL   -- every archived file, for a sample of
     accessions spanning different tickers/forms/linkbase layouts
     (separate linkbase files, linkbases embedded in the .xsd, and the
     one pre-Inline-XBRL standalone-instance case), extracts back
     byte-identical to the original file still on disk under
     data/sec_filings_locked/.
  2. SHA256_MATCHES_FRESH_RECOMPUTE -- the SHA-256 recorded at archive
     time equals a hash freshly recomputed from the extracted bytes
     (not merely "no exception was raised").
  3. CORRUPTED_BLOB_REJECTED -- a BLOB whose bytes have been tampered
     with is detected (SHA-256 mismatch) and rejected with
     CorruptedArchiveError, not silently used. Exercised against a
     throwaway scratch copy of the database -- the real archive is
     never touched.
  4. TRUNCATED_DOWNLOAD_REJECTED -- a "download" whose byte count does
     not match the size SEC itself reported is rejected with
     TruncatedDownloadError BEFORE anything is stored, verified by
     re-querying afterward that no row exists.

Fails loudly (non-zero exit, no PASS claim) on the first failing test.
Never modifies data/database/filings_archive.duckdb.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from importlib import util as _importlib_util
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
LOCKED_FILINGS_DIR = DATA_DIR / "sec_filings_locked"

_spec161 = _importlib_util.spec_from_file_location("s161", PROJECT_DIR / "scripts" / "161_filings_archive_core.py")
s161 = _importlib_util.module_from_spec(_spec161)
sys.modules["s161"] = s161
_spec161.loader.exec_module(s161)


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


SAMPLE_ACCESSIONS = [
    ("MSFT", "0000950170-24-087843"),   # 10-K, linkbases embedded inside the .xsd
    ("MSFT", "0000950170-23-014423"),   # 10-Q, separate _cal/_def/_lab/_pre files
    ("AMZN", "0001018724-25-000004"),   # 10-K, separate linkbase files, different filer agent
    ("NVDA", "0001045810-19-000079"),   # pre-Inline-XBRL 10-Q, standalone instance document (D-041)
    ("ORCL", "0000950170-24-075605"),   # 10-K, only 3 needed files (no separate linkbases at all)
    ("PANW", "0001327567-21-000029"),   # 10-K, different filer agent again
]


def test_round_trip_and_sha256(connection: duckdb.DuckDBPyConnection) -> dict:
    checked_files = 0
    checked_accessions = 0

    for ticker, accession_number in SAMPLE_ACCESSIONS:
        manifest_paths = sorted((LOCKED_FILINGS_DIR / ticker).glob(f"{accession_number.replace('-', '')}/locked_filing_manifest.json"))
        if len(manifest_paths) != 1:
            raise AssertionError(f"Expected exactly 1 locked manifest on disk for {ticker}/{accession_number}, found {len(manifest_paths)}")
        locked_dir = manifest_paths[0].parent

        with s161.extracted_filing(connection, accession_number) as (temp_dir, manifest_row):
            extracted_files = sorted(p.name for p in temp_dir.iterdir())
            if not extracted_files:
                raise AssertionError(f"{accession_number}: extracted zero files")

            for file_name in extracted_files:
                original_path = locked_dir / file_name
                extracted_path = temp_dir / file_name
                if not original_path.exists():
                    raise AssertionError(f"{accession_number}/{file_name}: original file no longer on disk to compare against")

                original_bytes = original_path.read_bytes()
                extracted_bytes = extracted_path.read_bytes()
                if original_bytes != extracted_bytes:
                    raise AssertionError(f"{accession_number}/{file_name}: extracted bytes differ from original -- NOT byte-identical")

                original_sha256 = sha256_of_file(original_path)
                extracted_sha256 = sha256_of_file(extracted_path)
                if original_sha256 != extracted_sha256:
                    raise AssertionError(f"{accession_number}/{file_name}: SHA-256 mismatch original vs extracted")

                stored_sha256 = connection.execute(
                    "SELECT sha256 FROM filing_archive_files WHERE accession_number = ? AND file_name = ?",
                    [accession_number, file_name],
                ).fetchone()[0]
                if stored_sha256 != original_sha256:
                    raise AssertionError(f"{accession_number}/{file_name}: stored SHA-256 does not match a fresh recompute")

                checked_files += 1

            # temp_dir must not exist anymore after the context manager exits below --
            # checked separately in test_extracted_dir_cleanup()

        checked_accessions += 1

    return {"accessions_checked": checked_accessions, "files_checked": checked_files}


def test_extracted_dir_cleanup(connection: duckdb.DuckDBPyConnection) -> dict:
    ticker, accession_number = SAMPLE_ACCESSIONS[0]
    with s161.extracted_filing(connection, accession_number) as (temp_dir, _manifest_row):
        captured_temp_dir = temp_dir
        if not captured_temp_dir.exists():
            raise AssertionError("temp_dir should exist inside the context manager")

    if captured_temp_dir.exists():
        raise AssertionError(f"temp_dir was not cleaned up after the context manager exited: {captured_temp_dir}")

    return {"cleaned_up_dir": str(captured_temp_dir)}


def test_corrupted_blob_rejected() -> dict:
    ticker, accession_number = SAMPLE_ACCESSIONS[0]

    scratch_dir = Path(tempfile.mkdtemp(prefix="filings_archive_corruption_test_"))
    scratch_db_path = scratch_dir / "scratch_archive.duckdb"
    try:
        scratch_connection = duckdb.connect(database=str(scratch_db_path))
        s161.create_archive_schema(scratch_connection)

        original_bytes = b"<xbrl>this is a fake filing file for corruption testing</xbrl>" * 100
        record = s161.archive_file(scratch_connection, accession_number, "fake_file.xml", original_bytes)

        # sanity: uncorrupted extraction must succeed first
        extract_dir = scratch_dir / "extract_ok"
        extract_dir.mkdir()
        s161.verify_and_extract_file(scratch_connection, accession_number, "fake_file.xml", extract_dir)

        # now corrupt the stored BLOB directly (flip bytes in the middle of the gzip stream)
        row = scratch_connection.execute(
            "SELECT content_gz FROM filing_archive_files WHERE accession_number = ? AND file_name = ?",
            [accession_number, "fake_file.xml"],
        ).fetchone()
        content_gz = bytearray(row[0])
        midpoint = len(content_gz) // 2
        content_gz[midpoint] ^= 0xFF  # flip every bit of one byte
        scratch_connection.execute(
            "UPDATE filing_archive_files SET content_gz = ? WHERE accession_number = ? AND file_name = ?",
            [bytes(content_gz), accession_number, "fake_file.xml"],
        )

        corrupt_extract_dir = scratch_dir / "extract_corrupt"
        corrupt_extract_dir.mkdir()
        raised = False
        try:
            s161.verify_and_extract_file(scratch_connection, accession_number, "fake_file.xml", corrupt_extract_dir)
        except s161.CorruptedArchiveError:
            raised = True

        if not raised:
            raise AssertionError("verify_and_extract_file did NOT raise CorruptedArchiveError on a tampered BLOB")

        if any(corrupt_extract_dir.iterdir()):
            raise AssertionError("a file was written to disk from a corrupted BLOB -- must never happen")

        scratch_connection.close()
        return {"corruption_detected": True, "original_sha256": record["sha256"]}
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def test_truncated_download_rejected() -> dict:
    scratch_dir = Path(tempfile.mkdtemp(prefix="filings_archive_truncation_test_"))
    scratch_db_path = scratch_dir / "scratch_archive.duckdb"
    try:
        scratch_connection = duckdb.connect(database=str(scratch_db_path))
        s161.create_archive_schema(scratch_connection)

        full_bytes = b"0123456789" * 10_000  # pretend SEC reported this size
        truncated_bytes = full_bytes[: len(full_bytes) // 2]  # connection cut off halfway

        raised = False
        try:
            s161.archive_file(
                scratch_connection, "0000000000-00-000000", "truncated_test.xml",
                truncated_bytes, declared_size=len(full_bytes),
            )
        except s161.TruncatedDownloadError:
            raised = True

        if not raised:
            raise AssertionError("archive_file did NOT raise TruncatedDownloadError on a truncated download")

        stored_row_count = scratch_connection.execute(
            "SELECT COUNT(*) FROM filing_archive_files WHERE accession_number = ?",
            ["0000000000-00-000000"],
        ).fetchone()[0]
        if stored_row_count != 0:
            raise AssertionError(f"a truncated download was stored anyway ({stored_row_count} row(s)) -- must never happen")

        # a correctly-sized download for the same accession must still succeed normally
        s161.archive_file(
            scratch_connection, "0000000000-00-000000", "complete_test.xml",
            full_bytes, declared_size=len(full_bytes),
        )
        stored_row_count = scratch_connection.execute(
            "SELECT COUNT(*) FROM filing_archive_files WHERE accession_number = ?",
            ["0000000000-00-000000"],
        ).fetchone()[0]
        if stored_row_count != 1:
            raise AssertionError("a correctly-sized download was not stored")

        scratch_connection.close()
        return {"truncation_rejected": True, "complete_download_still_works": True}
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def main() -> None:
    print()
    print("=" * 110)
    print("FILINGS ARCHIVE -- REQUIRED INTEGRITY TESTS")
    print("=" * 110)

    if not s161.ARCHIVE_DB_PATH.exists():
        raise RuntimeError(f"Archive database not found: {s161.ARCHIVE_DB_PATH}. Run scripts/163 first.")

    connection = duckdb.connect(database=str(s161.ARCHIVE_DB_PATH), read_only=True)

    results = {}

    print("\n[1/4] ROUND_TRIP_BYTE_IDENTICAL + SHA256_MATCHES_FRESH_RECOMPUTE ...")
    results["round_trip_and_sha256"] = test_round_trip_and_sha256(connection)
    print(f"      PASS -- {results['round_trip_and_sha256']}")

    print("\n[2/4] EXTRACTED_TEMP_DIR_CLEANED_UP ...")
    results["cleanup"] = test_extracted_dir_cleanup(connection)
    print(f"      PASS -- {results['cleanup']}")

    connection.close()

    print("\n[3/4] CORRUPTED_BLOB_REJECTED (scratch copy only, real archive untouched) ...")
    results["corrupted_blob_rejected"] = test_corrupted_blob_rejected()
    print(f"      PASS -- {results['corrupted_blob_rejected']}")

    print("\n[4/4] TRUNCATED_DOWNLOAD_REJECTED (scratch copy only, real archive untouched) ...")
    results["truncated_download_rejected"] = test_truncated_download_rejected()
    print(f"      PASS -- {results['truncated_download_rejected']}")

    print()
    print("=" * 110)
    print("ALL REQUIRED TESTS PASSED")
    print("=" * 110)

    result_path = DATA_DIR / "filings_archive_tests_result.json"
    result_path.write_text(json.dumps({"status": "PASS", "tests": results}, indent=2), encoding="utf-8")
    print(f"Result written: {result_path.resolve()}")


if __name__ == "__main__":
    main()
