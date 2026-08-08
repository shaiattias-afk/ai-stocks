"""
scripts/172_parallel_warehouse_ingest_batch.py -- thin entry point for
the parallel warehouse-ingestion engine
(src/stock_agent/warehouse/parallel_loader.py, Part A of the
parallel-ingestion work). Enumerates every locked filing under
data/sec_filings_locked/, skips whatever is already PASS in the target
warehouse database (idempotent, same "verify before load" pattern every
other batch runner in this project already uses, e.g. scripts/121's
is_already_warehoused), and loads the rest in parallel.

Usage:
    .venv\\Scripts\\python.exe scripts\\172_parallel_warehouse_ingest_batch.py [--warehouse-db-path PATH] [--max-workers N] [--limit N] [--dry-run]

Defaults to the production warehouse
(data/database/xbrl_warehouse_proof.duckdb). Pass --warehouse-db-path to
target a scratch database instead (e.g. for a bounded benchmark that
must not touch production).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb

from stock_agent import PROJECT_DIR, WAREHOUSE_DB_PATH
from stock_agent.filings.locked import LOCKED_FILINGS_DIR
from stock_agent.warehouse.parallel_loader import IngestTarget, run_parallel_warehouse_load

RESULT_PATH = PROJECT_DIR / "data" / "parallel_warehouse_ingest_batch_result.json"


def discover_locked_targets() -> list[tuple[IngestTarget, str]]:
    """Returns (target, accession_number) for every locked filing on
    disk, read directly from each package's own manifest -- never
    guessed from the directory name."""
    targets: list[tuple[IngestTarget, str]] = []
    for manifest_path in sorted(LOCKED_FILINGS_DIR.glob("*/*/locked_filing_manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ticker = manifest_path.parent.parent.name
        target = IngestTarget(ticker, manifest["report_date"], manifest["form"])
        targets.append((target, manifest["accession_number"]))
    return targets


def already_warehoused_accessions(warehouse_db_path: Path) -> set[str]:
    if not warehouse_db_path.exists():
        return set()
    connection = duckdb.connect(str(warehouse_db_path), read_only=True)
    try:
        table_exists = connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'warehouse_runs'"
        ).fetchone()
        if not table_exists:
            return set()
        return {
            row[0] for row in connection.execute(
                "SELECT accession_number FROM warehouse_runs WHERE status = 'PASS'"
            ).fetchall()
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--warehouse-db-path", type=Path, default=WAREHOUSE_DB_PATH)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--per-filing-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=None, help="load only the first N missing filings (for a bounded benchmark)")
    parser.add_argument("--dry-run", action="store_true", help="report what would be loaded, load nothing")
    args = parser.parse_args()

    all_targets = discover_locked_targets()
    already_loaded = already_warehoused_accessions(args.warehouse_db_path)
    missing_targets = [target for target, accession in all_targets if accession not in already_loaded]

    print(f"Locked filings on disk: {len(all_targets)}")
    print(f"Already warehoused (PASS) in {args.warehouse_db_path}: {len(already_loaded)}")
    print(f"Missing / to load: {len(missing_targets)}")

    if args.limit is not None:
        missing_targets = missing_targets[: args.limit]
        print(f"--limit applied: loading {len(missing_targets)}")

    if args.dry_run or not missing_targets:
        print("Dry run or nothing to load -- exiting without writing.")
        return

    start = time.perf_counter()
    result = run_parallel_warehouse_load(
        missing_targets, args.warehouse_db_path, max_workers=args.max_workers,
        per_filing_timeout_seconds=args.per_filing_timeout_seconds,
    )
    elapsed = time.perf_counter() - start

    print(f"Batch elapsed: {elapsed:.1f}s for {len(missing_targets)} filings, max_workers={args.max_workers}")
    print(f"Status counts: {result['status_counts']}")

    RESULT_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"Result written to {RESULT_PATH}")


if __name__ == "__main__":
    main()
