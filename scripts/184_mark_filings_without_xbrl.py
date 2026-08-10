"""Re-checks every archived filing for the presence of an XBRL document
set and marks those that have none.

Nothing is deleted. A filing with no XBRL is still a real filing and its
absence of machine-readable data is a real fact about that company's
history -- one an honest backtest should see, because an investor at the
time could not have computed those ratios either. It is only excluded
from PARSING, where it could do nothing but fail.

    .venv\\Scripts\\python.exe scripts\\184_mark_filings_without_xbrl.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR
from stock_agent.filings import archive

RESULT_PATH = DATA_DIR / "filings_without_xbrl.json"


def main() -> None:
    connection = duckdb.connect(database=str(archive.ARCHIVE_DB_PATH))
    try:
        rows = connection.execute(
            "SELECT accession_number, ticker, form, report_date, primary_document, source "
            "FROM filing_archive_manifest ORDER BY ticker, report_date"
        ).fetchall()

        print("=" * 92)
        print(f"CHECKING {len(rows)} ARCHIVED FILINGS FOR AN XBRL DOCUMENT SET")
        print("=" * 92)

        without = []
        for accession_number, ticker, form, report_date, primary_document, source in rows:
            file_names = [
                r[0] for r in connection.execute(
                    "SELECT file_name FROM filing_archive_files WHERE accession_number = ?",
                    [accession_number],
                ).fetchall()
            ]
            if archive.has_xbrl_document_set(file_names):
                continue
            without.append({
                "accession_number": accession_number, "ticker": ticker, "form": form,
                "report_date": report_date, "primary_document": primary_document,
                "archived_files": sorted(file_names), "previous_source": source,
            })

        if not without:
            print("every archived filing carries an XBRL document set. Nothing to mark.")
        else:
            print(f"\n{len(without)} filing(s) carry NO XBRL document set:\n")
            for entry in without:
                print(f"   {entry['ticker']:<6} {entry['report_date']}  {entry['form']:<5} "
                      f"{entry['accession_number']}  files={len(entry['archived_files'])}")
                print(f"      archived: {entry['archived_files']}")

            connection.execute("BEGIN TRANSACTION")
            for entry in without:
                connection.execute(
                    "UPDATE filing_archive_manifest SET source = ? WHERE accession_number = ?",
                    [archive.NO_XBRL_DOCUMENT_SET, entry["accession_number"]],
                )
            connection.execute("COMMIT")
            print(f"\nmarked {len(without)} filing(s) as {archive.NO_XBRL_DOCUMENT_SET}")

        total = connection.execute("SELECT COUNT(*) FROM filing_archive_manifest").fetchone()[0]
        marked = connection.execute(
            "SELECT COUNT(*) FROM filing_archive_manifest WHERE source = ?",
            [archive.NO_XBRL_DOCUMENT_SET],
        ).fetchone()[0]
        print(f"\narchive total: {total}   parseable: {total - marked}   no-XBRL: {marked}")
    finally:
        connection.close()

    RESULT_PATH.write_text(json.dumps({
        "filings_without_xbrl": without,
        "count": len(without),
    }, indent=2), encoding="utf-8")
    print(f"written: {RESULT_PATH}")


if __name__ == "__main__":
    main()
