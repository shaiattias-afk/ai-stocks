"""Parses the newly downloaded pilot filings into the XBRL warehouse,
in parallel, appending to what is already there.

Safety sequence, matching this project's established discipline:
  1. back up the existing warehouse and verify the copy by SHA-256
  2. work out which archived accessions are NOT yet in the warehouse
  3. parse those in parallel and append (never replace)
  4. re-open the warehouse read-only and confirm the pre-existing
     accessions are untouched and the new ones arrived complete

    .venv\\Scripts\\python.exe scripts\\183_warehouse_pilot_filings.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from datetime import datetime, timezone

import duckdb

from stock_agent import DATA_DIR, WAREHOUSE_DB_PATH
from stock_agent.filings import archive
from stock_agent.warehouse import batch, extract as wh_extract

BACKUP_DIR = DATA_DIR / "database" / "backups"
RESULT_PATH = DATA_DIR / "pilot_warehouse_result.json"
WORKERS = 8


def sha256_of_file(path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def table_counts(connection) -> dict[str, int]:
    return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in wh_extract.WAREHOUSE_TABLES}


def main() -> None:
    print("=" * 96)
    print("PARSE PILOT FILINGS INTO THE XBRL WAREHOUSE (parallel, append)")
    print("=" * 96)

    # --- 1. what is already in the warehouse ---------------------------------
    warehouse = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    try:
        existing = {r[0] for r in warehouse.execute(
            "SELECT DISTINCT accession_number FROM xbrl_facts").fetchall()}
        counts_before = table_counts(warehouse)
    finally:
        warehouse.close()

    archived = set(batch.all_archived_accessions())
    todo = sorted(archived - existing)

    print(f"archived accessions      : {len(archived)}")
    print(f"already in warehouse     : {len(existing)}")
    print(f"to parse                 : {len(todo)}")
    if not todo:
        print("nothing to do.")
        return

    # --- 2. backup ------------------------------------------------------------
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"xbrl_warehouse_pre_pilot_load_{stamp}.duckdb"
    print(f"\nbacking up -> {backup_path.name}")
    shutil.copy2(WAREHOUSE_DB_PATH, backup_path)
    source_hash = sha256_of_file(WAREHOUSE_DB_PATH)
    backup_hash = sha256_of_file(backup_path)
    if source_hash != backup_hash:
        raise SystemExit("backup checksum mismatch -- refusing to proceed")
    print(f"backup verified (sha256 {backup_hash[:16]}...)")

    # --- 3. parse in parallel, appending -------------------------------------
    print(f"\nparsing {len(todo)} filings with {WORKERS} workers...\n")
    start = time.perf_counter()
    report = batch.load_many(
        todo, WAREHOUSE_DB_PATH, workers=WORKERS,
        warm_cache=True, progress=True, append=True,
    )
    elapsed = time.perf_counter() - start

    if report["status"] != "PASS":
        print("\nLOAD FAILED -- warehouse left unchanged (merge is refused on any failure)")
        for failure in report.get("failed", [])[:10]:
            print("   ", failure)
        raise SystemExit(1)

    # --- 4. independent read-only verification -------------------------------
    print("\nre-opening the warehouse read-only to verify...")
    warehouse = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    try:
        after = {r[0] for r in warehouse.execute(
            "SELECT DISTINCT accession_number FROM xbrl_facts").fetchall()}
        counts_after = table_counts(warehouse)
        per_ticker = warehouse.execute(
            "SELECT ticker, COUNT(DISTINCT accession_number) FROM warehouse_runs "
            "GROUP BY ticker ORDER BY ticker").df()
    finally:
        warehouse.close()

    preserved = existing <= after
    arrived = set(todo) <= after
    grew = all(counts_after[t] >= counts_before[t] for t in counts_before)

    print()
    print(f"{'table':<38}{'before':>10}{'after':>10}{'delta':>10}")
    for table in wh_extract.WAREHOUSE_TABLES:
        b, a = counts_before[table], counts_after[table]
        print(f"{table:<38}{b:>10}{a:>10}{a - b:>10}")

    print()
    print(f"pre-existing accessions preserved : {preserved} ({len(existing)})")
    print(f"new accessions present            : {arrived} ({len(todo)})")
    print(f"no table shrank                   : {grew}")
    print(f"accessions in warehouse           : {len(existing)} -> {len(after)}")
    print(f"elapsed                           : {elapsed:.1f}s")
    print()
    print("companies in warehouse:")
    print(per_ticker.to_string(index=False))

    ok = preserved and arrived and grew
    print()
    print("=" * 96)
    print("RESULT:", "PASS" if ok else "FAIL -- see above")
    print("=" * 96)

    RESULT_PATH.write_text(json.dumps({
        "status": "PASS" if ok else "FAIL",
        "backup_path": str(backup_path),
        "backup_sha256": backup_hash,
        "accessions_before": len(existing),
        "accessions_after": len(after),
        "parsed_now": len(todo),
        "counts_before": counts_before,
        "counts_after": counts_after,
        "pre_existing_preserved": preserved,
        "new_accessions_present": arrived,
        "elapsed_seconds": round(elapsed, 1),
        "parallel_seconds": report.get("parallel_seconds"),
        "merge_seconds": report.get("merge_seconds"),
    }, indent=2), encoding="utf-8")
    print(f"written: {RESULT_PATH}")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
