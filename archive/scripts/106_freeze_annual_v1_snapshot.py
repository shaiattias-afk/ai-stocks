"""
Annual V1 freeze — creates a read-only snapshot copy of the production
database, now that the 45-company-year, 20-primary-metric historical
dataset is fully resolved (D-034: 900 of 900 results, 0
REVIEW_REQUIRED, zero regressions).

This script does NOT compute or change any metric — it is a pure
file-copy + verification step. The production database
(data/database/ai_stock_agent.duckdb) is opened READ-ONLY and is never
modified by this script.

Outputs:
  - data/database/ai_stock_agent_annual_v1.duckdb (byte-for-byte copy)
  - data/database/ai_stock_agent_annual_v1_manifest.json (provenance +
    verification record)
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
SOURCE_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
SNAPSHOT_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"
MANIFEST_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1_manifest.json"

PRIMARY_METRICS = [
    "revenue", "net_income", "operating_income", "operating_cash_flow",
    "capex", "free_cash_flow", "cash_and_equivalents",
    "short_term_investments", "current_debt", "long_term_debt",
    "total_debt", "adjusted_net_debt", "pretax_income",
    "income_tax_expense", "stockholders_equity", "effective_tax_rate",
    "nopat", "invested_capital", "average_invested_capital", "roic",
]

# The 5 supplementary prior-fiscal-year filings, added ONLY to unblock
# average_invested_capital/roic for the first year of each company's
# 45-company-year window (D-029, D-031, D-034) — never counted as part
# of the 45-company-year target universe themselves.
SUPPLEMENTARY_PRIOR_YEAR_ACCESSIONS = [
    "0001018724-21-000004",  # AMZN FY2020
    "0001652044-21-000010",  # GOOGL FY2020
    "0001326801-20-000013",  # META FY2019
    "0001535527-21-000007",  # CRWD FY2021
    "0001045810-19-000023",  # NVDA FY2019
]


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def gather_stats(db_path: Path, read_only: bool) -> dict[str, object]:
    connection = duckdb.connect(database=str(db_path), read_only=read_only)
    placeholders = ",".join("?" * len(PRIMARY_METRICS))
    exclude_placeholders = ",".join("?" * len(SUPPLEMENTARY_PRIOR_YEAR_ACCESSIONS))

    company_year_count = connection.execute(
        f"SELECT COUNT(*) FROM sec_filings WHERE accession_number NOT IN ({exclude_placeholders})",
        SUPPLEMENTARY_PRIOR_YEAR_ACCESSIONS,
    ).fetchone()[0]

    rows = connection.execute(
        f"""
        WITH ranked AS (
            SELECT f.status,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.accession_number, f.metric_name
                       ORDER BY r.loaded_at DESC
                   ) rn
            FROM financial_metric_results f
            JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id
            WHERE f.metric_name IN ({placeholders})
              AND r.accession_number NOT IN ({exclude_placeholders})
        )
        SELECT status, COUNT(*) FROM ranked WHERE rn = 1 GROUP BY status
        """,
        PRIMARY_METRICS + SUPPLEMENTARY_PRIOR_YEAR_ACCESSIONS,
    ).fetchall()
    status_breakdown = {status: count for status, count in rows}
    total_rows = sum(status_breakdown.values())

    connection.close()

    return {
        "company_year_count": company_year_count,
        "total_primary_metric_rows": total_rows,
        "status_breakdown": status_breakdown,
        "review_required_count": status_breakdown.get("REVIEW_REQUIRED", 0),
    }


def main() -> None:
    print(f"Source database: {SOURCE_DB_PATH}")
    if not SOURCE_DB_PATH.exists():
        raise FileNotFoundError(f"Source database not found: {SOURCE_DB_PATH}")

    print("\n--- Step 1: gather stats from SOURCE (read-only) ---")
    source_stats = gather_stats(SOURCE_DB_PATH, read_only=True)
    print(json.dumps(source_stats, indent=2, ensure_ascii=False))

    if source_stats["company_year_count"] != 45:
        raise RuntimeError(f"Expected 45 company-years, found {source_stats['company_year_count']}")
    if source_stats["total_primary_metric_rows"] != 900:
        raise RuntimeError(f"Expected 900 primary-metric rows, found {source_stats['total_primary_metric_rows']}")
    if source_stats["review_required_count"] != 0:
        raise RuntimeError(f"Expected 0 REVIEW_REQUIRED, found {source_stats['review_required_count']}")

    print("\n--- Step 2: copy database file (byte-for-byte) ---")
    if SNAPSHOT_DB_PATH.exists():
        raise FileExistsError(
            f"Snapshot already exists at {SNAPSHOT_DB_PATH} — refusing to overwrite an existing "
            "Annual V1 snapshot. Delete it manually first if a genuine rebuild is intended."
        )
    shutil.copy2(SOURCE_DB_PATH, SNAPSHOT_DB_PATH)
    print(f"Copied to: {SNAPSHOT_DB_PATH}")

    print("\n--- Step 3: compute SHA-256 checksums ---")
    source_checksum = sha256_of_file(SOURCE_DB_PATH)
    snapshot_checksum = sha256_of_file(SNAPSHOT_DB_PATH)
    print(f"source_sha256   = {source_checksum}")
    print(f"snapshot_sha256 = {snapshot_checksum}")
    if source_checksum != snapshot_checksum:
        raise RuntimeError("Checksum mismatch between source and snapshot — copy did not complete cleanly")

    print("\n--- Step 4: verify snapshot opens and returns identical counts ---")
    snapshot_stats = gather_stats(SNAPSHOT_DB_PATH, read_only=True)
    print(json.dumps(snapshot_stats, indent=2, ensure_ascii=False))
    if snapshot_stats != source_stats:
        raise RuntimeError("Snapshot stats do not match source stats")

    print("\n--- Step 5: write manifest ---")
    manifest = {
        "snapshot_name": "Annual V1",
        "snapshot_created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_database_path": str(SOURCE_DB_PATH.resolve()),
        "snapshot_database_path": str(SNAPSHOT_DB_PATH.resolve()),
        "sha256_checksum": snapshot_checksum,
        "target_company_years": source_stats["company_year_count"],
        "primary_metrics_per_company_year": len(PRIMARY_METRICS),
        "total_primary_metric_results": source_stats["total_primary_metric_rows"],
        "status_breakdown": source_stats["status_breakdown"],
        "review_required_count": source_stats["review_required_count"],
        "supplementary_prior_year_filings": SUPPLEMENTARY_PRIOR_YEAR_ACCESSIONS,
        "supplementary_prior_year_filings_note": (
            "These 5 accessions were locked/warehoused/computed ONLY to "
            "supply a prior-fiscal-year invested_capital value for the "
            "first year of each company's 45-company-year window "
            "(D-029, D-031, D-034) — they are NOT part of the 45 "
            "target company-years and are excluded from all counts above."
        ),
        "latest_completed_milestone": "D-034",
        "immutability_note": (
            "This snapshot must not be modified after creation. Any "
            "future correction to the annual dataset must produce a "
            "new, separately-versioned snapshot (e.g. Annual V2), never "
            "an in-place edit of this file."
        ),
    }

    with MANIFEST_PATH.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    print(f"Manifest written to: {MANIFEST_PATH}")

    print("\n" + "=" * 100)
    print("ANNUAL V1 SNAPSHOT — VERIFIED, FROZEN")
    print(f"  company_year_count = {source_stats['company_year_count']} (expected 45)")
    print(f"  total_primary_metric_rows = {source_stats['total_primary_metric_rows']} (expected 900)")
    print(f"  review_required_count = {source_stats['review_required_count']} (expected 0)")
    print(f"  sha256 = {snapshot_checksum}")
    print("=" * 100)


if __name__ == "__main__":
    main()
