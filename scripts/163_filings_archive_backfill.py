"""
Backfills every already-locked filing under data/sec_filings_locked/
into the compressed DuckDB archive (scripts/161), reading exclusively
from disk -- makes zero network requests and does NOT re-download
anything already fetched by scripts/36b/107. data/sec_filings_locked/
is left completely untouched (not deleted, not modified) -- it remains
the disk-based reference copy until the archive is independently
proven (round-trip tests, warehouse-reproduction proof).

For each locked_filing_manifest.json found under
data/sec_filings_locked/<TICKER>/<ACCESSION>/, applies the same
scripts/161.is_needed_file() filter used by the new downloader to the
files already sitting in that directory, archives the needed ones
(gzip BLOB + SHA-256), and writes one filing_archive_manifest row with
source="BACKFILL_FROM_DISK".
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb

from stock_agent import PROJECT_DIR
from stock_agent.filings import archive

DATA_DIR = PROJECT_DIR / "data"
LOCKED_FILINGS_DIR = DATA_DIR / "sec_filings_locked"


def find_locked_manifests() -> list[Path]:
    return sorted(LOCKED_FILINGS_DIR.glob("*/*/locked_filing_manifest.json"))


def backfill_one_filing(connection: duckdb.DuckDBPyConnection, manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    locked_dir = manifest_path.parent
    primary_document = str(manifest["primary_document"])
    accession_number = str(manifest["accession_number"])

    primary_document_path = locked_dir / primary_document
    if not primary_document_path.exists():
        raise FileNotFoundError(f"Primary document missing on disk: {primary_document_path}")

    candidate_files = sorted(p for p in locked_dir.iterdir() if p.is_file())

    records = []
    total_uncompressed = 0
    total_compressed = 0

    for file_path in candidate_files:
        file_name = file_path.name
        if not archive.is_needed_file(file_name, primary_document):
            continue

        raw_bytes = file_path.read_bytes()
        record = archive.archive_file(
            connection=connection,
            accession_number=accession_number,
            file_name=file_name,
            raw_bytes=raw_bytes,
            declared_size=None,  # already on disk, complete by definition -- nothing to compare against
        )
        records.append(record)
        total_uncompressed += record["byte_size"]
        total_compressed += record["compressed_byte_size"]

    if not records:
        raise RuntimeError(f"No needed files found on disk for {accession_number} at {locked_dir}")

    if primary_document not in {r["file_name"] for r in records}:
        raise RuntimeError(f"Primary document {primary_document} was not archived for {accession_number} -- filter bug.")

    archive.archive_manifest_row(
        connection=connection,
        accession_number=accession_number,
        ticker=str(manifest["ticker"]),
        cik=int(manifest["cik"]),
        company_name=str(manifest.get("company_name", "")),
        form=str(manifest["form"]),
        report_date=str(manifest["report_date"]),
        filing_date=str(manifest["filing_date"]),
        primary_document=primary_document,
        file_count=len(records),
        total_uncompressed_bytes=total_uncompressed,
        total_compressed_bytes=total_compressed,
        source="BACKFILL_FROM_DISK",
    )

    return {
        "accession_number": accession_number,
        "ticker": manifest["ticker"],
        "report_date": manifest["report_date"],
        "form": manifest["form"],
        "file_count": len(records),
        "total_uncompressed_bytes": total_uncompressed,
        "total_compressed_bytes": total_compressed,
        "file_names": sorted(r["file_name"] for r in records),
    }


def main() -> None:
    start = time.perf_counter()

    print()
    print("=" * 110)
    print("BACKFILL: data/sec_filings_locked/ -> compressed DuckDB archive (disk only, no network)")
    print("=" * 110)

    manifest_paths = find_locked_manifests()
    print(f"Locked filing manifests found: {len(manifest_paths)}")
    if not manifest_paths:
        raise RuntimeError(f"No locked_filing_manifest.json files found under {LOCKED_FILINGS_DIR}")

    ARCHIVE_DB_PATH = archive.ARCHIVE_DB_PATH
    ARCHIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=str(ARCHIVE_DB_PATH))
    archive.create_archive_schema(connection)

    results = []
    total_uncompressed_all = 0
    total_compressed_all = 0

    try:
        connection.execute("BEGIN TRANSACTION")
        for i, manifest_path in enumerate(manifest_paths, start=1):
            result = backfill_one_filing(connection, manifest_path)
            results.append(result)
            total_uncompressed_all += result["total_uncompressed_bytes"]
            total_compressed_all += result["total_compressed_bytes"]
            print(
                f"[{i:>3}/{len(manifest_paths)}] {result['ticker']:<6} {result['report_date']} "
                f"{result['form']:<5} {result['accession_number']}  "
                f"{result['file_count']} files, "
                f"{result['total_uncompressed_bytes']:,} -> {result['total_compressed_bytes']:,} bytes"
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise

    accession_count = connection.execute(
        "SELECT COUNT(DISTINCT accession_number) FROM filing_archive_manifest"
    ).fetchone()[0]
    file_count = connection.execute("SELECT COUNT(*) FROM filing_archive_files").fetchone()[0]
    connection.close()

    elapsed = time.perf_counter() - start

    print()
    print("=" * 110)
    print("BACKFILL COMPLETE")
    print("=" * 110)
    print(f"Accessions archived (this run): {len(results)}")
    print(f"Accessions in archive (total, verified read-back): {accession_count}")
    print(f"Files in archive (total, verified read-back): {file_count}")
    print(f"Total uncompressed: {total_uncompressed_all:,} bytes ({total_uncompressed_all / 1_048_576:.1f} MB)")
    print(f"Total compressed:   {total_compressed_all:,} bytes ({total_compressed_all / 1_048_576:.1f} MB)")
    if total_compressed_all:
        print(f"Ratio: {total_uncompressed_all / total_compressed_all:.2f}x")
    print(f"Elapsed: {elapsed:.1f}s")

    result_path = DATA_DIR / "filings_archive_backfill_result.json"
    result_path.write_text(
        json.dumps(
            {
                "accessions_archived": len(results),
                "accessions_in_archive_total": accession_count,
                "files_in_archive_total": file_count,
                "total_uncompressed_bytes": total_uncompressed_all,
                "total_compressed_bytes": total_compressed_all,
                "elapsed_seconds": round(elapsed, 3),
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Result written: {result_path.resolve()}")


if __name__ == "__main__":
    main()
