"""scripts/207_quarterly_extension_pilot.py -- extends Quarterly Data V1
(frozen, D-042) to 3 new tickers, per the user's explicit direction
(2026-08-12 session) to extend 10-Q coverage beyond the original 9. This
does NOT modify the frozen V5 engine (stock_agent.extraction.quarterly,
unchanged) or touch any existing quarterly_extraction_runs /
quarterly_metric_results row -- purely additive, the same "append,
never touch the frozen set" discipline the annual wider-universe
extension (D-051-D-061) already used for financial_metric_results.

Pilot tickers, chosen for (a) clean existing annual coverage (revenue,
operating_income, operating_cash_flow, capex all PASS for every already-
locked fiscal year -- verified by direct query before picking these) and
(b) genuine sector diversity away from the current 9's mega-cap-tech/
semis skew: COST (consumer staples/retail), CSX (industrial/rail
transport), PYPL (fintech/payments). Their 10-K accessions are already
archived AND warehouse-loaded (verified before writing this script) --
only 10-Q filings are new work.

Pipeline (per ticker):
  1. Discover 10-Q filings across the last ~4 fiscal years
     (discover_annual_filings(..., forms=("10-Q",)) -- fully generic,
     not annual-specific despite the function's name).
  2. Download each into the compressed filing archive
     (filing_archive_manifest/filing_archive_files) -- same mechanism
     scripts/191 used for the wider-universe 10-Ks.
  3. Insert sec_filings rows for each newly-downloaded 10-Q
     (accession-first, D-002; fiscal_year = report_date's own year,
     prior_report_date = NULL, matching the existing 9 tickers' 10-Q
     rows' own shape, verified by direct query before writing this).
  4. Group 10-Qs into fiscal years by matching against this ticker's own
     already-locked 10-K report_dates -- a fiscal year is usable only
     when exactly 3 10-Qs fall strictly between the prior 10-K's report
     date and this one's, chronologically ordered as Q1/Q2/Q3.
  5. Warehouse-load each new 10-Q accession (Arelle, straight from the
     compressed archive -- stock_agent.warehouse.archive_loader, the
     same path the wider-universe annual work uses; NOT the older
     disk-locked loader archive/scripts/144, confirmed superseded).
  6. Run the frozen, UNCHANGED quarterly engine V5
     (stock_agent.extraction.quarterly.run_quarterly_extraction_engine_v5)
     for each usable fiscal year.
  7. Insert results into quarterly_extraction_runs / quarterly_metric_
     results using the LIVE table schema (24 / 15 columns, named-column
     INSERT -- verified directly via `duckdb_tables()` before writing,
     not assumed from an older archived loading script whose column
     count didn't match the live schema).

Explicitly NOT done here: no attempt to fix a metric that comes back
REVIEW_REQUIRED -- that would mean modifying the frozen engine, which
needs its own new version + full regression per D-042. A ticker/fiscal-
year that doesn't cleanly resolve is reported and left REVIEW_REQUIRED,
exactly as the engine's own fail-closed design intends.
"""

from __future__ import annotations

import json
import time

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH, WAREHOUSE_DB_PATH
from stock_agent.filings import archive
from stock_agent.quarterly_extension import (
    already_loaded,
    discover_and_lock_10q,
    group_into_fiscal_years,
    load_engine_output,
    run_engine,
    warehouse_load_new_10q,
)

PILOT_TICKERS = ["COST", "CSX", "PYPL"]
RESULT_PATH = DATA_DIR / "quarterly_extension_pilot_result.json"


def main() -> None:
    print("=" * 100)
    print(f"QUARTERLY EXTENSION PILOT -- tickers: {PILOT_TICKERS}")
    print("=" * 100)

    archive.ARCHIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    archive_connection = duckdb.connect(str(archive.ARCHIVE_DB_PATH))
    archive.create_archive_schema(archive_connection)
    prod_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=False)

    lock_results = {}
    for ticker in PILOT_TICKERS:
        print(f"\n--- discover + lock 10-Q: {ticker} ---")
        result = discover_and_lock_10q(prod_connection, archive_connection, ticker)
        lock_results[ticker] = result
        print(json.dumps({k: v for k, v in result.items() if k != "already_archived"}, indent=2, default=str))

    prod_connection.close()

    print("\n" + "=" * 100)
    print("WAREHOUSE LOAD new 10-Q accessions")
    print("=" * 100)
    prod_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    warehouse_connection = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=False)
    all_new_accessions = []
    for ticker, result in lock_results.items():
        all_new_accessions.extend(result.get("downloaded", []))
        all_new_accessions.extend(result.get("already_archived", []))
    print(f"accessions to warehouse-load: {len(all_new_accessions)}")

    start = time.perf_counter()
    warehouse_result = warehouse_load_new_10q(archive_connection, warehouse_connection, all_new_accessions)
    print(json.dumps(warehouse_result, indent=2, default=str))
    print(f"elapsed: {time.perf_counter() - start:.1f}s")
    warehouse_connection.close()
    archive_connection.close()
    prod_connection.close()

    print("\n" + "=" * 100)
    print("GROUP INTO FISCAL YEARS (read-only pass, connection closed before any engine call)")
    print("=" * 100)
    prod_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    all_targets = []
    for ticker in PILOT_TICKERS:
        targets = group_into_fiscal_years(prod_connection, ticker)
        print(f"{ticker}: {len(targets)} usable fiscal year(s): {[t['fiscal_year_end'] for t in targets]}")
        for target in targets:
            if already_loaded(prod_connection, ticker, target["fiscal_year_end"]):
                print(f"  {target['fiscal_year_end']}: already loaded, skipping")
                continue
            all_targets.append(target)
    prod_connection.close()

    print("\n" + "=" * 100)
    print("RUN QUARTERLY ENGINE V5 (unchanged) + LOAD -- no connection held open across engine calls")
    print("=" * 100)
    engine_results = []
    for target in all_targets:
        ticker, fiscal_year_end = target["ticker"], target["fiscal_year_end"]
        try:
            engine_output = run_engine(target)
            result = load_engine_output(engine_output, target)
            engine_results.append(result)
            print(f"{ticker} {fiscal_year_end}: loaded run_id={result['run_id']} "
                  f"n_rows={result['n_rows']} status_counts={result['status_counts']}")
        except Exception as error:  # noqa: BLE001
            engine_results.append({"ticker": ticker, "fiscal_year_end": fiscal_year_end,
                                    "status": "ENGINE_ERROR", "error": str(error)[:500]})
            print(f"{ticker} {fiscal_year_end}: ENGINE ERROR: {str(error)[:300]}")

    RESULT_PATH.write_text(json.dumps({
        "pilot_tickers": PILOT_TICKERS, "lock_results": lock_results, "engine_results": engine_results,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
