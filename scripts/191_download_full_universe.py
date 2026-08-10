"""Downloads the XBRL document set for every filing found by scripts/190.

Same mechanics as scripts/182 (XBRL-only, SEC's 10 req/s limit, skips
what is already archived, safe to re-run) applied to the full
survivorship-free universe instead of the 25-company pilot.

    .venv\\Scripts\\python.exe scripts\\191_download_full_universe.py
"""

from __future__ import annotations

import json
import time

import duckdb

from stock_agent import DATA_DIR
from stock_agent.filings import archive
from stock_agent.filings.download import DiscoveredFiling, SecDownloadError, download_filing_into_archive

DISCOVERY_PATH = DATA_DIR / "full_universe_discovery.json"
RESULT_PATH = DATA_DIR / "full_universe_download_result.json"


def main() -> None:
    discovery = json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))
    filings = [DiscoveredFiling(**record)
               for company in discovery["results"] for record in company["filings"]]

    print("=" * 96)
    print(f"DOWNLOAD XBRL DOCUMENT SETS -> ARCHIVE ({len(filings)} filings)")
    print("=" * 96)

    archive.ARCHIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=str(archive.ARCHIVE_DB_PATH))
    archive.create_archive_schema(connection)
    before = connection.execute("SELECT COUNT(*) FROM filing_archive_manifest").fetchone()[0]
    start = time.perf_counter()

    downloaded, skipped, no_xbrl, failed = [], [], [], []
    try:
        for index, filing in enumerate(filings, start=1):
            try:
                result = download_filing_into_archive(connection, filing)
            except SecDownloadError as error:
                failed.append({"accession_number": filing.accession_number,
                               "ticker": filing.ticker, "report_date": filing.report_date,
                               "error": str(error)[:300]})
                print(f"[{index:>4}/{len(filings)}] {filing.ticker:<6} {filing.report_date} FAILED: {str(error)[:90]}",
                      flush=True)
                continue

            status = result["status"]
            if status == "ALREADY_ARCHIVED":
                skipped.append(result)
            elif status == archive.NO_XBRL_DOCUMENT_SET:
                no_xbrl.append(result)
                print(f"[{index:>4}/{len(filings)}] {filing.ticker:<6} {filing.report_date} NO XBRL", flush=True)
            else:
                downloaded.append(result)
                if index % 25 == 0 or index == len(filings):
                    elapsed = time.perf_counter() - start
                    print(f"[{index:>4}/{len(filings)}] {filing.ticker:<6} {filing.report_date}  "
                          f"({len(downloaded)} new, {elapsed:.0f}s)", flush=True)
    finally:
        after = connection.execute("SELECT COUNT(*) FROM filing_archive_manifest").fetchone()[0]
        files_total = connection.execute("SELECT COUNT(*) FROM filing_archive_files").fetchone()[0]
        connection.close()

    elapsed = time.perf_counter() - start
    uncompressed = sum(r.get("total_uncompressed_bytes", 0) for r in downloaded)
    compressed = sum(r.get("total_compressed_bytes", 0) for r in downloaded)

    print()
    print("=" * 96)
    print(f"downloaded           : {len(downloaded)}")
    print(f"already archived     : {len(skipped)}")
    print(f"no XBRL document set : {len(no_xbrl)}")
    print(f"failed               : {len(failed)}")
    print(f"accessions in archive: {before} -> {after}")
    print(f"files in archive     : {files_total}")
    if compressed:
        print(f"new bytes            : {uncompressed:,} -> {compressed:,} ({uncompressed / compressed:.2f}x)")
    print(f"elapsed              : {elapsed / 60:.1f} min")
    print("=" * 96)
    for f in failed[:20]:
        print(f"  FAILED {f['ticker']} {f['report_date']}: {f['error'][:110]}")

    RESULT_PATH.write_text(json.dumps({
        "filings_considered": len(filings), "downloaded": len(downloaded),
        "already_archived": len(skipped), "no_xbrl": no_xbrl, "failed": failed,
        "accessions_before": before, "accessions_after": after,
        "files_in_archive": files_total,
        "new_uncompressed_bytes": uncompressed, "new_compressed_bytes": compressed,
        "elapsed_seconds": round(elapsed, 1),
    }, indent=2), encoding="utf-8")
    print(f"written: {RESULT_PATH}")


if __name__ == "__main__":
    main()
