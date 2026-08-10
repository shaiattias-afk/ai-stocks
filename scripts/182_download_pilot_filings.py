"""Downloads the XBRL document set for every filing found by
scripts/181 into the compressed archive.

Only the ~7 files per filing the extraction pipeline actually reads are
fetched, at SEC's published 10 req/s limit. Filings already in the archive
are skipped, so this is safe to re-run and cheap to resume.

Nothing is deleted and no existing archive row is modified: archiving a
filing is an insert keyed by accession number.

    .venv\\Scripts\\python.exe scripts\\182_download_pilot_filings.py
"""

from __future__ import annotations

import json
import time

import duckdb

from stock_agent import DATA_DIR
from stock_agent.filings import archive
from stock_agent.filings.download import DiscoveredFiling, SecDownloadError, download_filing_into_archive

DISCOVERY_PATH = DATA_DIR / "pilot_discovery_result.json"
RESULT_PATH = DATA_DIR / "pilot_download_result.json"


def main() -> None:
    discovery = json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))

    filings: list[DiscoveredFiling] = []
    for company in discovery["results"]:
        for record in company["filings"]:
            filings.append(DiscoveredFiling(**record))

    print("=" * 96)
    print(f"DOWNLOAD XBRL DOCUMENT SETS -> COMPRESSED ARCHIVE ({len(filings)} filings)")
    print("=" * 96)

    archive.ARCHIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=str(archive.ARCHIVE_DB_PATH))
    archive.create_archive_schema(connection)

    before = connection.execute("SELECT COUNT(*) FROM filing_archive_manifest").fetchone()[0]
    start = time.perf_counter()

    downloaded, skipped, failed = [], [], []
    try:
        for index, filing in enumerate(filings, start=1):
            try:
                result = download_filing_into_archive(connection, filing)
            except SecDownloadError as error:
                failed.append({"accession_number": filing.accession_number,
                               "ticker": filing.ticker, "report_date": filing.report_date,
                               "error": str(error)})
                print(f"[{index:>3}/{len(filings)}] {filing.ticker:<6} {filing.report_date}  FAILED: {error}")
                continue

            if result["status"] == "ALREADY_ARCHIVED":
                skipped.append(result)
            else:
                downloaded.append(result)
                print(f"[{index:>3}/{len(filings)}] {filing.ticker:<6} {filing.report_date}  "
                      f"{result['file_count']} files  "
                      f"{result['total_uncompressed_bytes']:,} -> {result['total_compressed_bytes']:,} bytes",
                      flush=True)
    finally:
        after = connection.execute("SELECT COUNT(*) FROM filing_archive_manifest").fetchone()[0]
        total_files = connection.execute("SELECT COUNT(*) FROM filing_archive_files").fetchone()[0]
        connection.close()

    elapsed = time.perf_counter() - start
    uncompressed = sum(r["total_uncompressed_bytes"] for r in downloaded)
    compressed = sum(r["total_compressed_bytes"] for r in downloaded)

    print()
    print("=" * 96)
    print(f"downloaded          : {len(downloaded)}")
    print(f"already archived    : {len(skipped)}")
    print(f"failed              : {len(failed)}")
    print(f"accessions in archive: {before} -> {after}")
    print(f"files in archive     : {total_files}")
    if compressed:
        print(f"new bytes            : {uncompressed:,} -> {compressed:,} "
              f"({uncompressed / compressed:.2f}x)")
    print(f"elapsed              : {elapsed:.1f}s")
    print("=" * 96)

    for failure in failed:
        print(f"  FAILED {failure['ticker']} {failure['report_date']}: {failure['error']}")

    RESULT_PATH.write_text(json.dumps({
        "filings_considered": len(filings),
        "downloaded": len(downloaded),
        "already_archived": len(skipped),
        "failed": failed,
        "accessions_before": before,
        "accessions_after": after,
        "files_in_archive": total_files,
        "new_uncompressed_bytes": uncompressed,
        "new_compressed_bytes": compressed,
        "elapsed_seconds": round(elapsed, 1),
        "results": downloaded,
    }, indent=2), encoding="utf-8")
    print(f"written: {RESULT_PATH}")


if __name__ == "__main__":
    main()
