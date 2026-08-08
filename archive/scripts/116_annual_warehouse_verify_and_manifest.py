"""
Annual XBRL Warehouse completion gate — Steps 3+4: final verification of
all 50 expected 10-K filings (no duplicate warehouse_runs rows, no empty
successful load, accession/report-date match) and creation of the
required manifest at
data/database/annual_xbrl_warehouse_v1_manifest.json.

Read-only against every existing database. Writes nothing except the
one new manifest JSON file. Does not touch the production database,
Annual V1 snapshot, or the raw xbrl_warehouse_proof.duckdb warehouse.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
AUDIT_PATH = DATA_DIR / "annual_warehouse_audit.json"
MANIFEST_OUTPUT_PATH = DATA_DIR / "database" / "annual_xbrl_warehouse_v1_manifest.json"


def main() -> None:
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    records = audit["records"]

    if len(records) != 50:
        raise RuntimeError(f"Expected 50 audit records, found {len(records)}. Refusing to build manifest.")

    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    manifest_filings = []
    pass_count = 0
    timeout_count = 0
    fail_count = 0
    missing_count = 0
    duplicate_run_accessions = []
    empty_load_accessions = []

    for record in records:
        accession = record["accession_number"]

        run_rows = warehouse_connection.execute(
            "SELECT status, script_name, completed_at_utc, row_counts_json "
            "FROM warehouse_runs WHERE accession_number = ? ORDER BY completed_at_utc",
            [accession],
        ).fetchall()
        pass_runs = [r for r in run_rows if r[0] == "PASS"]

        if record["classification"] != "COMPLETE":
            missing_count += 1
            status = "MISSING"
        elif len(pass_runs) == 0:
            missing_count += 1
            status = "MISSING"
        else:
            status = "PASS"
            pass_count += 1
            if len(pass_runs) > 1:
                duplicate_run_accessions.append(accession)
            if record["fact_count"] == 0 or record["context_count"] == 0 or record["unit_count"] == 0:
                empty_load_accessions.append(accession)

        latest_pass_run = pass_runs[-1] if pass_runs else None
        loader_script = latest_pass_run[1] if latest_pass_run else None
        load_timestamp = latest_pass_run[2] if latest_pass_run else None
        row_counts_json = json.loads(latest_pass_run[3]) if latest_pass_run else {}

        manifest_filings.append({
            "ticker": record["ticker"],
            "form": record["form"],
            "report_date": record["report_date"],
            "filing_date": record["filing_date"],
            "accession_number": accession,
            "role": record["role"],
            "primary_document": Path(record["primary_document_path"]).name if record["primary_document_path"] else None,
            "arelle_entry_point": Path(record["primary_document_path"]).name if record["primary_document_path"] else None,
            "locked_path": record["primary_document_path"],
            "fact_count": record["fact_count"],
            "context_count": record["context_count"],
            "unit_count": record["unit_count"],
            "presentation_relationship_count": row_counts_json.get("xbrl_presentation_relationships"),
            "calculation_relationship_count": row_counts_json.get("xbrl_calculation_relationships"),
            "warehouse_status": status,
            "warehouse_run_count": len(run_rows),
            "loader_script": loader_script,
            "load_timestamp_utc": load_timestamp,
            "warning": (
                "duplicate_warehouse_run" if accession in duplicate_run_accessions
                else "zero_calculation_relationships" if row_counts_json.get("xbrl_calculation_relationships") == 0
                else None
            ),
            "exception": None,
        })

    warehouse_connection.close()

    manifest = {
        "manifest_name": "Annual XBRL Warehouse V1",
        "creation_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "expected_filing_count": len(records),
        "complete_filing_count": pass_count,
        "missing_filing_count": missing_count,
        "pass_count": pass_count,
        "timeout_count": timeout_count,
        "fail_count": fail_count,
        "duplicate_warehouse_run_accessions": duplicate_run_accessions,
        "empty_successful_load_accessions": empty_load_accessions,
        "filings": manifest_filings,
        "success_criteria_met": (
            len(records) == 50
            and pass_count == 50
            and missing_count == 0
            and timeout_count == 0
            and fail_count == 0
            and len(duplicate_run_accessions) == 0
            and len(empty_load_accessions) == 0
        ),
        "standing_gate": (
            "Quarterly work may begin only when the Annual XBRL Warehouse manifest "
            "shows every expected annual 10-K filing as PASS."
        ),
    }

    MANIFEST_OUTPUT_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=" * 100)
    print("STEP 3/4 — FINAL VERIFICATION + MANIFEST")
    print("=" * 100)
    print(f"expected_filing_count = {len(records)}")
    print(f"pass_count = {pass_count}")
    print(f"missing_count = {missing_count}")
    print(f"timeout_count = {timeout_count}")
    print(f"fail_count = {fail_count}")
    print(f"duplicate_warehouse_run_accessions = {duplicate_run_accessions}")
    print(f"empty_successful_load_accessions = {empty_load_accessions}")
    print(f"success_criteria_met = {manifest['success_criteria_met']}")
    print(f"\nManifest written to {MANIFEST_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
