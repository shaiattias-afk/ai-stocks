"""Retries the filings that failed mid-download, and reports any orphan
files left behind.

A failure part-way through a filing leaves files already stored in
`filing_archive_files` with no `filing_archive_manifest` row -- the filing
looks absent to every consumer (all_archived_accessions reads the
manifest) but is occupying space. Re-running self-heals: archive_file
replaces each file by key, and the manifest row is written only after all
files succeed, so a completed retry leaves the filing consistent.

Retries run at half the normal rate, since the failures look like SEC
throttling: 11 of 787, spread across unrelated companies and file types.

    .venv\\Scripts\\python.exe scripts\\192_retry_failed_downloads.py
"""

from __future__ import annotations

import json
import time

import duckdb

from stock_agent import DATA_DIR
from stock_agent.filings import archive, download
from stock_agent.filings.download import DiscoveredFiling, SecDownloadError, download_filing_into_archive

DOWNLOAD_RESULT_PATH = DATA_DIR / "full_universe_download_result.json"
DISCOVERY_PATH = DATA_DIR / "full_universe_discovery.json"
RESULT_PATH = DATA_DIR / "retry_download_result.json"

# the failures look like throttling, so back off
download.REQUEST_DELAY_SECONDS = 0.25
DELAY_BETWEEN_FILINGS_SECONDS = 2.0
MAX_ROUNDS = 3


def main() -> None:
    failed = json.loads(DOWNLOAD_RESULT_PATH.read_text(encoding="utf-8"))["failed"]
    discovery = json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))
    by_accession = {
        record["accession_number"]: DiscoveredFiling(**record)
        for company in discovery["results"] for record in company["filings"]
    }

    connection = duckdb.connect(database=str(archive.ARCHIVE_DB_PATH))
    try:
        orphans = connection.execute("""
            SELECT f.accession_number, COUNT(*) AS files
            FROM filing_archive_files f
            LEFT JOIN filing_archive_manifest m USING (accession_number)
            WHERE m.accession_number IS NULL
            GROUP BY 1 ORDER BY 1
        """).fetchall()
        print("=" * 88)
        print(f"failed filings to retry : {len(failed)}")
        print(f"orphan accessions       : {len(orphans)} "
              f"({sum(n for _, n in orphans)} stray files with no manifest row)")
        for accession_number, count in orphans:
            print(f"   {accession_number}  {count} files")
        print("=" * 88)

        pending = [by_accession[f["accession_number"]] for f in failed
                   if f["accession_number"] in by_accession]
        recovered, still_failing = [], []

        for round_number in range(1, MAX_ROUNDS + 1):
            if not pending:
                break
            print(f"\n--- round {round_number}: {len(pending)} filing(s) ---")
            next_round = []
            for filing in pending:
                try:
                    result = download_filing_into_archive(connection, filing)
                    recovered.append(result)
                    print(f"   {filing.ticker:<6} {filing.report_date}  {result['status']}"
                          f"  files={result.get('file_count', '-')}", flush=True)
                except SecDownloadError as error:
                    next_round.append(filing)
                    print(f"   {filing.ticker:<6} {filing.report_date}  still failing: "
                          f"{str(error)[:80]}", flush=True)
                time.sleep(DELAY_BETWEEN_FILINGS_SECONDS)
            pending = next_round

        still_failing = [{"ticker": f.ticker, "report_date": f.report_date,
                          "accession_number": f.accession_number} for f in pending]

        remaining_orphans = connection.execute("""
            SELECT COUNT(DISTINCT f.accession_number)
            FROM filing_archive_files f
            LEFT JOIN filing_archive_manifest m USING (accession_number)
            WHERE m.accession_number IS NULL
        """).fetchone()[0]
        total = connection.execute("SELECT COUNT(*) FROM filing_archive_manifest").fetchone()[0]
    finally:
        connection.close()

    print()
    print("=" * 88)
    print(f"recovered            : {len(recovered)}")
    print(f"still failing        : {len(still_failing)}")
    print(f"orphan accessions now: {remaining_orphans}")
    print(f"accessions in archive: {total}")
    print("=" * 88)
    for entry in still_failing:
        print(f"   {entry['ticker']} {entry['report_date']} {entry['accession_number']}")

    RESULT_PATH.write_text(json.dumps({
        "attempted": len(failed), "recovered": len(recovered),
        "still_failing": still_failing, "orphan_accessions_remaining": remaining_orphans,
        "accessions_in_archive": total,
    }, indent=2), encoding="utf-8")
    print(f"written: {RESULT_PATH}")


if __name__ == "__main__":
    main()
