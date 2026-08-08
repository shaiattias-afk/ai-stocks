"""
Acceptance proof for this milestone: rebuild the entire XBRL warehouse
FROM THE COMPRESSED ARCHIVE ONLY (scripts/161/163), for all 185
archived accessions, and verify it reproduces the existing production
warehouse (data/database/xbrl_warehouse_proof.duckdb, 225,780 facts
across 185 accessions) EXACTLY -- same fact/context/unit/concept/label/
presentation/calculation/definition/role counts, per accession, table
by table. data/sec_filings_locked/ is never read by this script -- only
the archive DB and Arelle's own cache.

Each accession runs scripts/165 in its own subprocess (isolates Arelle
session state between filings, bounded by a real per-filing timeout --
consistent with this project's established bounded-child-process
pattern, e.g. scripts/121/37c). A fresh target warehouse DB is created
before the run (this is a proof artifact created and owned by this
script, not project data -- deleted/recreated at the START of each
full run, never mid-run, never touching xbrl_warehouse_proof.duckdb).

Writes a full JSON report and only claims REPRODUCTION_VERIFIED after
independently re-querying both databases read-only, post-run, and
finding zero differences.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
ARCHIVE_DB_PATH = DATA_DIR / "database" / "filings_archive.duckdb"
PRODUCTION_WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
REBUILD_WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_from_archive_proof.duckdb"
LOADER_SCRIPT_PATH = PROJECT_DIR / "scripts" / "165_filings_archive_arelle_loader.py"
RESULT_PATH = DATA_DIR / "warehouse_rebuild_from_archive_result.json"

PER_FILING_TIMEOUT_SECONDS = 180

WAREHOUSE_TABLES = [
    "xbrl_facts", "xbrl_contexts", "xbrl_units", "xbrl_concepts", "xbrl_labels",
    "xbrl_presentation_relationships", "xbrl_calculation_relationships",
    "xbrl_definition_relationships", "xbrl_roles",
]


def list_archived_accessions() -> list[dict]:
    connection = duckdb.connect(database=str(ARCHIVE_DB_PATH), read_only=True)
    try:
        rows = connection.execute(
            "SELECT accession_number, ticker, form, report_date FROM filing_archive_manifest ORDER BY ticker, report_date"
        ).fetchall()
    finally:
        connection.close()
    return [{"accession_number": r[0], "ticker": r[1], "form": r[2], "report_date": r[3]} for r in rows]


def load_one_accession_in_subprocess(accession_number: str) -> dict:
    start = time.perf_counter()
    try:
        result = subprocess.run(
            [
                sys.executable, str(LOADER_SCRIPT_PATH),
                "--accession-number", accession_number,
                "--warehouse-db-path", str(REBUILD_WAREHOUSE_DB_PATH),
                "--internet-connectivity", "online",
            ],
            timeout=PER_FILING_TIMEOUT_SECONDS, capture_output=True, text=True, encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return {"accession_number": accession_number, "status": "TIMEOUT", "elapsed_seconds": round(time.perf_counter() - start, 3)}

    elapsed = round(time.perf_counter() - start, 3)
    if result.returncode != 0:
        return {"accession_number": accession_number, "status": "FAIL", "elapsed_seconds": elapsed,
                "error": f"child exited {result.returncode}", "stderr": result.stderr[-3000:]}

    worker_line = next((line for line in result.stdout.splitlines() if line.startswith("WORKER_RESULT_JSON=")), None)
    if worker_line is None:
        return {"accession_number": accession_number, "status": "FAIL", "elapsed_seconds": elapsed,
                "error": "no WORKER_RESULT_JSON line in child stdout", "stdout_tail": result.stdout[-2000:]}

    summary = json.loads(worker_line[len("WORKER_RESULT_JSON="):])
    summary["elapsed_seconds"] = elapsed
    return summary


def compare_warehouses() -> dict:
    """Independent, read-only re-verification: opens both databases
    fresh (not reusing any in-memory state from the load loop above)
    and compares every table's per-accession row count exactly."""
    prod = duckdb.connect(database=str(PRODUCTION_WAREHOUSE_DB_PATH), read_only=True)
    rebuilt = duckdb.connect(database=str(REBUILD_WAREHOUSE_DB_PATH), read_only=True)
    try:
        prod_accessions = {r[0] for r in prod.execute("SELECT DISTINCT accession_number FROM xbrl_facts").fetchall()}
        rebuilt_accessions = {r[0] for r in rebuilt.execute("SELECT DISTINCT accession_number FROM xbrl_facts").fetchall()}

        differences = []
        if prod_accessions != rebuilt_accessions:
            differences.append({
                "type": "ACCESSION_SET_MISMATCH",
                "in_production_only": sorted(prod_accessions - rebuilt_accessions),
                "in_rebuilt_only": sorted(rebuilt_accessions - prod_accessions),
            })

        per_table_totals = {}
        for table in WAREHOUSE_TABLES:
            prod_total = prod.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            rebuilt_total = rebuilt.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            per_table_totals[table] = {"production": prod_total, "rebuilt": rebuilt_total, "match": prod_total == rebuilt_total}
            if prod_total != rebuilt_total:
                differences.append({"type": "TABLE_TOTAL_MISMATCH", "table": table, "production": prod_total, "rebuilt": rebuilt_total})

        per_accession_mismatches = []
        for accession_number in sorted(prod_accessions & rebuilt_accessions):
            for table in WAREHOUSE_TABLES:
                prod_count = prod.execute(f"SELECT COUNT(*) FROM {table} WHERE accession_number = ?", [accession_number]).fetchone()[0]
                rebuilt_count = rebuilt.execute(f"SELECT COUNT(*) FROM {table} WHERE accession_number = ?", [accession_number]).fetchone()[0]
                if prod_count != rebuilt_count:
                    per_accession_mismatches.append({
                        "accession_number": accession_number, "table": table,
                        "production": prod_count, "rebuilt": rebuilt_count,
                    })

        return {
            "accession_count_production": len(prod_accessions),
            "accession_count_rebuilt": len(rebuilt_accessions),
            "per_table_totals": per_table_totals,
            "differences": differences,
            "per_accession_mismatches": per_accession_mismatches,
            "reproduction_verified": (not differences) and (not per_accession_mismatches),
        }
    finally:
        prod.close()
        rebuilt.close()


def main() -> None:
    start = time.perf_counter()

    print()
    print("=" * 110)
    print("REBUILD XBRL WAREHOUSE FROM THE COMPRESSED ARCHIVE (all accessions, no disk-file reads)")
    print("=" * 110)

    if REBUILD_WAREHOUSE_DB_PATH.exists():
        REBUILD_WAREHOUSE_DB_PATH.unlink()
        print(f"Removed stale proof warehouse: {REBUILD_WAREHOUSE_DB_PATH}")
    for suffix in ("_arelle.log",):
        log_path = REBUILD_WAREHOUSE_DB_PATH.parent / f"{REBUILD_WAREHOUSE_DB_PATH.stem}{suffix}"
        if log_path.exists():
            log_path.unlink()

    accessions = list_archived_accessions()
    print(f"Accessions to rebuild: {len(accessions)}")

    load_results = []
    for i, entry in enumerate(accessions, start=1):
        result = load_one_accession_in_subprocess(entry["accession_number"])
        load_results.append(result)
        status = result.get("status", "UNKNOWN")
        elapsed = result.get("elapsed_seconds", "?")
        facts = result.get("computed_counts", {}).get("xbrl_facts", "?")
        print(f"[{i:>3}/{len(accessions)}] {entry['ticker']:<6} {entry['report_date']} {entry['form']:<5} "
              f"{entry['accession_number']}  {status:<8} facts={facts} ({elapsed}s)")
        if status != "PASS":
            print(f"          -> {result.get('error', '')}")
            if result.get("stderr"):
                print(f"          stderr tail: {result['stderr'][-500:]}")

    failed = [r for r in load_results if r.get("status") != "PASS"]

    print()
    print(f"Load phase complete: {len(load_results) - len(failed)}/{len(load_results)} PASS, {len(failed)} not PASS")

    print()
    print("Independently re-verifying, read-only, against the production warehouse...")
    comparison = compare_warehouses()

    elapsed_total = time.perf_counter() - start

    print()
    print("=" * 110)
    if comparison["reproduction_verified"] and not failed:
        print("RESULT: REPRODUCTION_VERIFIED -- every table, every accession, matches production exactly")
    else:
        print("RESULT: NOT VERIFIED -- see differences below")
    print("=" * 110)
    print(f"Production accessions: {comparison['accession_count_production']}")
    print(f"Rebuilt accessions:    {comparison['accession_count_rebuilt']}")
    for table, counts in comparison["per_table_totals"].items():
        marker = "OK" if counts["match"] else "MISMATCH"
        print(f"  {table:<38} production={counts['production']:<8} rebuilt={counts['rebuilt']:<8} {marker}")
    if comparison["differences"]:
        print(f"Differences: {json.dumps(comparison['differences'], indent=2)}")
    if comparison["per_accession_mismatches"]:
        print(f"Per-accession mismatches ({len(comparison['per_accession_mismatches'])}):")
        for m in comparison["per_accession_mismatches"][:20]:
            print(f"  {m}")
    print(f"Elapsed: {elapsed_total:.1f}s")

    RESULT_PATH.write_text(
        json.dumps(
            {
                "status": "REPRODUCTION_VERIFIED" if (comparison["reproduction_verified"] and not failed) else "FAIL",
                "accessions_attempted": len(accessions),
                "accessions_passed_load": len(load_results) - len(failed),
                "failed_loads": failed,
                "comparison": comparison,
                "elapsed_seconds": round(elapsed_total, 3),
                "load_results": load_results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Full result written: {RESULT_PATH.resolve()}")

    if failed or not comparison["reproduction_verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
