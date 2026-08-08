"""
Shared core module for the compressed SEC filing archive.

Goal (see docs/CURRENT_STATE.md / this milestone's task): stop downloading
SEC files the pipeline never reads, and store the files it DOES need
inside DuckDB as gzip-compressed BLOBs with integrity hashes, instead of
~106 loose files per filing on disk (measured: a locked filing downloads
~106 files, the extraction pipeline reads ~7).

Needed-file rule (general, no ticker/accession-specific logic):
  1. the exact primary document named in SEC's own filing metadata
  2. any file ending in .xsd (the extension schema -- some filers, e.g.
     Microsoft's 10-Ks, embed all four linkbases directly inside the
     .xsd via inline <link:...Link> elements; others, e.g. Amazon, ship
     them as separate files)
  3. any file matching the four separate-linkbase naming convention
     (*_cal.xml, *_def.xml, *_lab.xml, *_pre.xml)
  4. FilingSummary.xml (small, used for human/QA inspection)
  5. any other "plain" .xml file that is not the Inline-XBRL-generated
     "_htm.xml" instance (unused -- Arelle reads the primary .htm
     directly) and is not one of the above -- this is the fallback that
     correctly picks up a genuinely separate, standalone <xbrli:xbrl>
     instance document for the small number of pre-Inline-XBRL filings
     in this project's universe (e.g. NVDA's 2019 Q1 10-Q, see D-041),
     where the primary document is plain HTML and the real facts live
     in a bare instance file with no special suffix.

Explicitly excluded: the full-submission .txt (duplicates everything),
R*.htm rendered-viewer files (HTML tables are banned as an extraction
method, D-004), the "_htm.xml" generated instance (unused, Arelle reads
the .htm directly), MetaLinks.json, exhibit .htm files, and the SEC
index/index-headers .html pages.

Public API:
  is_needed_file(file_name, primary_document) -> bool
  create_archive_schema(connection) -> None
  archive_file(connection, accession_number, file_name, raw_bytes,
               declared_size=None) -> dict
  archive_manifest_row(connection, ...) -> None
  extracted_filing(connection, accession_number) -> contextmanager
      yields (temp_dir: Path, manifest_row: dict)
  TruncatedDownloadError, CorruptedArchiveError
"""

from __future__ import annotations

import gzip
import hashlib
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
ARCHIVE_DB_PATH = DATA_DIR / "database" / "filings_archive.duckdb"

_LINKBASE_SUFFIX_PATTERN = re.compile(r"_(cal|def|lab|pre)\.xml$", re.IGNORECASE)


class TruncatedDownloadError(Exception):
    """Raised when downloaded bytes do not match the size SEC itself
    reported for the file. Never stored in the archive."""


class CorruptedArchiveError(Exception):
    """Raised when a BLOB's recomputed SHA-256 (after gunzip) does not
    match the hash recorded at archive time. Never silently used."""


def is_needed_file(file_name: str, primary_document: str) -> bool:
    if file_name == primary_document:
        return True

    lower_name = file_name.lower()

    if lower_name.endswith(".xsd"):
        return True

    if _LINKBASE_SUFFIX_PATTERN.search(file_name):
        return True

    if lower_name == "filingsummary.xml":
        return True

    if lower_name.endswith(".xml") and not lower_name.endswith("_htm.xml"):
        return True

    return False


def create_archive_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS filing_archive_files (
        accession_number VARCHAR,
        file_name VARCHAR,
        content_gz BLOB,
        sha256 VARCHAR,
        byte_size BIGINT,
        downloaded_at VARCHAR,
        PRIMARY KEY (accession_number, file_name)
    )""")

    connection.execute("""CREATE TABLE IF NOT EXISTS filing_archive_manifest (
        accession_number VARCHAR PRIMARY KEY,
        ticker VARCHAR,
        cik BIGINT,
        company_name VARCHAR,
        form VARCHAR,
        report_date VARCHAR,
        filing_date VARCHAR,
        primary_document VARCHAR,
        file_count INTEGER,
        total_uncompressed_bytes BIGINT,
        total_compressed_bytes BIGINT,
        source VARCHAR,
        archived_at VARCHAR
    )""")


def _sha256_bytes(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def archive_file(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    file_name: str,
    raw_bytes: bytes,
    declared_size: int | None = None,
) -> dict[str, Any]:
    """Gzip-compresses and stores one file's bytes as a BLOB, keyed by
    (accession_number, file_name). Fails loudly (raises, never stores)
    if `declared_size` is given and does not match len(raw_bytes) --
    this is the truncated-download guard: a connection cut short must
    never be silently archived as if it were the complete file."""
    if declared_size is not None and declared_size > 0 and len(raw_bytes) != declared_size:
        raise TruncatedDownloadError(
            f"{accession_number}/{file_name}: downloaded {len(raw_bytes)} bytes, "
            f"SEC reported {declared_size} bytes -- refusing to store a truncated file."
        )

    sha256_hex = _sha256_bytes(raw_bytes)
    content_gz = gzip.compress(raw_bytes, compresslevel=9)
    downloaded_at = datetime.now(timezone.utc).isoformat()

    connection.execute(
        "DELETE FROM filing_archive_files WHERE accession_number = ? AND file_name = ?",
        [accession_number, file_name],
    )
    connection.execute(
        "INSERT INTO filing_archive_files VALUES (?,?,?,?,?,?)",
        [accession_number, file_name, content_gz, sha256_hex, len(raw_bytes), downloaded_at],
    )

    return {
        "file_name": file_name,
        "sha256": sha256_hex,
        "byte_size": len(raw_bytes),
        "compressed_byte_size": len(content_gz),
        "downloaded_at": downloaded_at,
    }


def archive_manifest_row(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    ticker: str,
    cik: int,
    company_name: str,
    form: str,
    report_date: str,
    filing_date: str,
    primary_document: str,
    file_count: int,
    total_uncompressed_bytes: int,
    total_compressed_bytes: int,
    source: str,
) -> None:
    archived_at = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "DELETE FROM filing_archive_manifest WHERE accession_number = ?",
        [accession_number],
    )
    connection.execute(
        "INSERT INTO filing_archive_manifest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            accession_number, ticker, cik, company_name, form, report_date, filing_date,
            primary_document, file_count, total_uncompressed_bytes, total_compressed_bytes,
            source, archived_at,
        ],
    )


def get_manifest_row(connection: duckdb.DuckDBPyConnection, accession_number: str) -> dict[str, Any]:
    columns = [
        "accession_number", "ticker", "cik", "company_name", "form", "report_date",
        "filing_date", "primary_document", "file_count", "total_uncompressed_bytes",
        "total_compressed_bytes", "source", "archived_at",
    ]
    row = connection.execute(
        f"SELECT {', '.join(columns)} FROM filing_archive_manifest WHERE accession_number = ?",
        [accession_number],
    ).fetchone()
    if row is None:
        raise KeyError(f"No filing_archive_manifest row for accession_number={accession_number!r}")
    return dict(zip(columns, row))


def verify_and_extract_file(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    file_name: str,
    destination_dir: Path,
) -> dict[str, Any]:
    """Reads one archived file's BLOB, gunzips it, recomputes its
    SHA-256, and compares it against the hash recorded at archive time.
    Raises CorruptedArchiveError (never writes the file, never hands it
    to a caller) on any mismatch -- a corrupted BLOB is detected and
    rejected, not silently used."""
    row = connection.execute(
        "SELECT content_gz, sha256, byte_size FROM filing_archive_files "
        "WHERE accession_number = ? AND file_name = ?",
        [accession_number, file_name],
    ).fetchone()
    if row is None:
        raise KeyError(f"No archived file {accession_number}/{file_name}")

    content_gz, expected_sha256, expected_size = row

    try:
        raw_bytes = gzip.decompress(content_gz)
    except (OSError, EOFError) as error:
        raise CorruptedArchiveError(
            f"{accession_number}/{file_name}: stored BLOB is not valid gzip data ({error})."
        ) from error

    actual_sha256 = _sha256_bytes(raw_bytes)
    if actual_sha256 != expected_sha256:
        raise CorruptedArchiveError(
            f"{accession_number}/{file_name}: SHA-256 mismatch after decompression. "
            f"expected={expected_sha256} actual={actual_sha256} -- BLOB is corrupted, refusing to use it."
        )
    if len(raw_bytes) != expected_size:
        raise CorruptedArchiveError(
            f"{accession_number}/{file_name}: decompressed size mismatch. "
            f"expected={expected_size} actual={len(raw_bytes)} -- BLOB is corrupted, refusing to use it."
        )

    destination_path = destination_dir / file_name
    destination_path.write_bytes(raw_bytes)
    return {"file_name": file_name, "sha256": actual_sha256, "byte_size": len(raw_bytes),
            "destination_path": str(destination_path)}


@contextmanager
def extracted_filing(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Pulls every archived file for `accession_number`, verifies each
    one's SHA-256, extracts them all into a fresh temp directory, yields
    (temp_dir, manifest_row), and always cleans up the temp directory
    afterward -- even if the caller (e.g. Arelle) raises."""
    manifest_row = get_manifest_row(connection, accession_number)
    file_names = [
        r[0] for r in connection.execute(
            "SELECT file_name FROM filing_archive_files WHERE accession_number = ? ORDER BY file_name",
            [accession_number],
        ).fetchall()
    ]
    if not file_names:
        raise KeyError(f"No archived files for accession_number={accession_number!r}")

    temp_dir = Path(tempfile.mkdtemp(prefix=f"filings_archive_{accession_number.replace('-', '')}_"))
    try:
        for file_name in file_names:
            verify_and_extract_file(connection, accession_number, file_name, temp_dir)
        yield temp_dir, manifest_row
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
