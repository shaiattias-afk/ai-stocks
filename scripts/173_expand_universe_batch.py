"""
scripts/173_expand_universe_batch.py -- thin entry point for expanding
the company universe (point-in-time work, docs/DECISIONS_LOG.md D-052).
Downloads + disk-locks (src/stock_agent/ingestion/download_and_lock.py)
and then warehouses (src/stock_agent/warehouse/parallel_loader.py) a
list of (ticker, form, report_date_or_None, company_name_hint) targets
-- the SAME two steps, in the SAME order, for a still-listed or a
delisted ticker; no special-casing anywhere in this script or the
library code it calls.

Usage:
    .venv\\Scripts\\python.exe scripts\\173_expand_universe_batch.py [--warehouse-db-path PATH] [--max-workers N]

Defaults to TARGETS below (currently: the 2 delisted companies proved
end-to-end for D-052 -- CTXS, MXIM). Extend TARGETS with more former
Nasdaq-100 constituents (see
src/stock_agent/universe/point_in_time.py's FORMER_NASDAQ100_CONSTITUENTS)
or additional still-listed Nasdaq-100 names to continue the "~25, then
100 companies" scale-out -- each addition is idempotent (skips a ticker
already locked+warehoused) and requires no code change beyond adding a
row to TARGETS.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from stock_agent import PROJECT_DIR, WAREHOUSE_DB_PATH
from stock_agent.ingestion.download_and_lock import LOCKED_FILINGS_DIR, download_and_lock_filing
from stock_agent.warehouse.parallel_loader import IngestTarget, run_parallel_warehouse_load

RESULT_PATH = PROJECT_DIR / "data" / "universe_expansion_batch_result.json"

# (ticker, form, report_date_or_None, company_name_hint)
# report_date=None means "the most recent filing of this form" -- used
# for delisted names where we want whatever the last one was, not a
# specific already-known date.
TARGETS: list[tuple[str, str, str | None, str]] = [
    ("CTXS", "10-K", None, "Citrix Systems Inc"),
    ("MXIM", "10-K", None, "Maxim Integrated Products Inc"),
]


def _already_locked(ticker: str) -> bool:
    return (LOCKED_FILINGS_DIR / ticker.upper()).exists() and any(
        (LOCKED_FILINGS_DIR / ticker.upper()).glob("*/locked_filing_manifest.json")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--warehouse-db-path", type=Path, default=WAREHOUSE_DB_PATH)
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args()

    lock_results = []
    warehouse_targets: list[IngestTarget] = []

    for ticker, form, report_date, name_hint in TARGETS:
        if _already_locked(ticker):
            print(f"{ticker}: already locked, skipping download.")
        else:
            print(f"{ticker}: downloading + locking (form={form}, report_date={report_date or 'most recent'})...")
            manifest = download_and_lock_filing(ticker, form=form, report_date=report_date, company_name_hint=name_hint)
            lock_results.append(manifest)
            print(f"  locked accession {manifest['accession_number']} via {manifest['cik_resolution_method']}")

        # discover the actual report_date/accession that ended up locked, for the warehouse step
        manifests_dir = LOCKED_FILINGS_DIR / ticker.upper()
        for manifest_path in sorted(manifests_dir.glob("*/locked_filing_manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("form") == form:
                warehouse_targets.append(IngestTarget(ticker.upper(), manifest["report_date"], form))

    start = time.perf_counter()
    warehouse_result = run_parallel_warehouse_load(warehouse_targets, args.warehouse_db_path, max_workers=args.max_workers)
    elapsed = time.perf_counter() - start

    print(f"\nWarehouse batch elapsed: {elapsed:.1f}s for {len(warehouse_targets)} filings")
    print(f"Status counts: {warehouse_result['status_counts']}")

    RESULT_PATH.write_text(
        json.dumps({"lock_results": lock_results, "warehouse_result": warehouse_result}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Result written to {RESULT_PATH}")


if __name__ == "__main__":
    main()
