"""
Read-only verification/report script for the GOOGL FY2021 schema-fix
proof. Does NOT back up, migrate, download, run Arelle, or process any
company-year — the backup, schema migration (making
quarterly_metric_results.concept_qname / reconciliation_difference /
permitted_difference nullable), and the GOOGL FY2021 proof itself were
already performed and committed by scripts/124_quarterly_schema_
nullable_fix_and_resume.py in the immediately preceding task.

This script exists because a later instruction in the same session asked
to stop strictly after the one-company-year (GOOGL FY2021) proof and not
resume the remaining batch — but scripts/124 had already auto-resumed
Phase 3 (per that same *prior* task's own explicit instructions) and
committed 20 further company-years before the stop instruction arrived
and the process was terminated. Those commits are real, individually
transactionally validated, and are not undone here (this project's
binding rules forbid deleting or overwriting already-committed data
without explicit authorization) — this script reports the true state
honestly, including that fact, rather than pretending only GOOGL FY2021
was touched.

Every query here is read-only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
ANNUAL_V1_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"
ANNUAL_V1_MANIFEST_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1_manifest.json"

# Recorded directly from scripts/124's own printed output during the run
# that performed the backup and migration (read-only facts, not re-derived).
BACKUP_PATH = DATA_DIR / "database" / "backups" / "ai_stock_agent_pre_quarterly_nullable_fix_20260804T152035Z.duckdb"
BACKUP_SOURCE_CHECKSUM_RECORDED = "ed1a06f7ff78a73b55dc07efe20f10b6561d75a952e89f0c4f59fd3161dc501c"
BACKUP_CHECKSUM_RECORDED = "ed1a06f7ff78a73b55dc07efe20f10b6561d75a952e89f0c4f59fd3161dc501c"
EXPECTED_ANNUAL_V1_CHECKSUM = "e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814"

GOOGL_FY2021 = ("GOOGL", "2021-12-31")


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> dict:
    report: dict = {}

    print("=" * 100)
    print("GOOGL FY2021 SCHEMA-FIX PROOF — VERIFICATION REPORT (read-only)")
    print("=" * 100)

    # --- backup verification (re-verify the backup file itself, now) ---
    backup_exists = BACKUP_PATH.exists()
    backup_checksum_now = sha256_of_file(BACKUP_PATH) if backup_exists else None
    print(f"\nBackup file: {BACKUP_PATH}")
    print(f"  exists: {backup_exists}")
    print(f"  checksum now: {backup_checksum_now}")
    print(f"  checksum recorded at backup time: {BACKUP_CHECKSUM_RECORDED}")
    print(f"  matches recorded source checksum: {backup_checksum_now == BACKUP_SOURCE_CHECKSUM_RECORDED}")

    if backup_exists:
        backup_connection = duckdb.connect(database=str(BACKUP_PATH), read_only=True)
        backup_counts = {
            "financial_metric_results": backup_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
            "quarterly_extraction_runs": backup_connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
            "quarterly_metric_results": backup_connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
        }
        backup_connection.close()
        print(f"  backup pre-migration counts: {backup_counts} (expected 900/18/432)")
    else:
        backup_counts = None

    report["backup"] = {
        "path": str(BACKUP_PATH), "exists": backup_exists, "checksum_now": backup_checksum_now,
        "checksum_recorded": BACKUP_CHECKSUM_RECORDED, "checksums_match": backup_checksum_now == BACKUP_SOURCE_CHECKSUM_RECORDED,
        "pre_migration_counts": backup_counts,
    }

    # --- schema before/after ---
    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    schema_now = connection.execute("DESCRIBE quarterly_metric_results").fetchall()
    nullable_cols = {row[0]: row[3] for row in schema_now if row[0] in ("concept_qname", "reconciliation_difference", "permitted_difference")}
    print(f"\nSchema (current): {nullable_cols} (all should be 'YES' — nullable)")
    report["schema"] = {"before": "concept_qname/reconciliation_difference/permitted_difference were NOT NULL",
                         "after": nullable_cols}

    # --- GOOGL FY2021 exact accessions + 24 rows ---
    run_row = connection.execute(
        "SELECT run_id, run_status, q1_accession, q2_accession, q3_accession, fy_accession, created_at, completed_at "
        "FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?", list(GOOGL_FY2021)
    ).fetchone()
    print(f"\nGOOGL FY2021 extraction run: {run_row}")

    run_id = run_row[0]
    rows = connection.execute(
        "SELECT metric_name, fiscal_quarter, value, unit, result_status, extraction_basis, concept_qname, "
        "reconciliation_status, reconciliation_difference, permitted_difference, lineage_json, availability_date, accession_number "
        "FROM quarterly_metric_results WHERE run_id = ? ORDER BY metric_name, fiscal_quarter", [run_id]
    ).fetchdf()
    print(f"\nGOOGL FY2021 row count: {len(rows)} (expected 24)")

    reconciliation_counts = rows.groupby("reconciliation_status").size().to_dict()
    print(f"Reconciliation-status counts: {reconciliation_counts}")

    review_required_rows = rows[rows["reconciliation_status"] == "REVIEW_REQUIRED"]
    print(f"\nREVIEW_REQUIRED rows ({len(review_required_rows)}):")
    review_required_details = []
    for _, row in review_required_rows.iterrows():
        lineage = json.loads(row["lineage_json"])
        print(f"  {row['metric_name']}/{row['fiscal_quarter']}: value={row['value']}, concept_qname={row['concept_qname']}, "
              f"reason={lineage.get('error')}")
        review_required_details.append({
            "metric_name": row["metric_name"], "fiscal_quarter": row["fiscal_quarter"],
            "value": row["value"], "concept_qname": row["concept_qname"], "reason": lineage.get("error"),
        })

    null_counts_googl = {
        "concept_qname": int(rows["concept_qname"].isna().sum()),
        "reconciliation_difference": int(rows["reconciliation_difference"].isna().sum()),
        "permitted_difference": int(rows["permitted_difference"].isna().sum()),
    }
    print(f"\nGOOGL FY2021 NULL counts: {null_counts_googl}")

    every_null_has_reason = bool(
        (rows[rows["concept_qname"].isna()]["reconciliation_status"] == "REVIEW_REQUIRED").all()
        and (rows[rows["concept_qname"].isna()]["lineage_json"].apply(lambda lj: "error" in json.loads(lj)).all())
    ) if null_counts_googl["concept_qname"] > 0 else True
    print(f"Every NULL concept_qname row is REVIEW_REQUIRED with a documented reason: {every_null_has_reason}")

    dup_googl = rows.groupby(["metric_name", "fiscal_quarter"]).size()
    dup_googl = dup_googl[dup_googl > 1]
    print(f"Duplicate keys within GOOGL FY2021: {len(dup_googl)}")

    missing_lineage_googl = int(rows["lineage_json"].isna().sum())
    print(f"Missing lineage: {missing_lineage_googl}")

    avail_mismatch_googl = connection.execute(
        "SELECT COUNT(*) FROM quarterly_metric_results r JOIN sec_filings s ON s.accession_number = r.accession_number "
        "WHERE r.run_id = ? AND r.availability_date != CAST(s.filing_date AS VARCHAR)", [run_id]
    ).fetchone()[0]
    print(f"Availability-date mismatches: {avail_mismatch_googl}")

    report["googl_fy2021"] = {
        "run": {"run_id": run_row[0], "run_status": run_row[1], "q1_accession": run_row[2], "q2_accession": run_row[3],
                "q3_accession": run_row[4], "fy_accession": run_row[5], "created_at": str(run_row[6]), "completed_at": str(run_row[7])},
        "row_count": len(rows), "reconciliation_status_counts": reconciliation_counts,
        "review_required_details": review_required_details, "null_counts": null_counts_googl,
        "every_null_has_documented_reason": every_null_has_reason, "duplicate_keys": len(dup_googl),
        "missing_lineage": missing_lineage_googl, "availability_date_mismatches": avail_mismatch_googl,
    }

    # --- overall current state (honest full picture, not just GOOGL) ---
    total_runs = connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0]
    total_rows = connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
    dup_all = connection.execute(
        "SELECT run_id, metric_name, fiscal_quarter, COUNT(*) c FROM quarterly_metric_results "
        "GROUP BY run_id, metric_name, fiscal_quarter HAVING COUNT(*) > 1"
    ).fetchall()
    fmr_count = connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    committed_company_years = connection.execute(
        "SELECT ticker, fiscal_year_end, run_status FROM quarterly_extraction_runs ORDER BY ticker, fiscal_year_end"
    ).fetchall()
    connection.close()

    actual_v1_checksum = sha256_of_file(ANNUAL_V1_DB_PATH)

    print(f"\nOverall current state: quarterly_extraction_runs={total_runs}, quarterly_metric_results={total_rows}, "
          f"financial_metric_results={fmr_count}, duplicate_keys={len(dup_all)}, "
          f"Annual V1 checksum unchanged={actual_v1_checksum == EXPECTED_ANNUAL_V1_CHECKSUM}")
    print(f"\nNOTE: {total_runs} company-years are currently committed, not just GOOGL FY2021 — Phase 3 of the "
          f"prior task (scripts/124) had already auto-resumed and committed {total_runs - 18} additional "
          f"company-years, per that task's own explicit instructions, before a later stop-instruction arrived "
          f"and the process was terminated. Full list below.")
    for cy in committed_company_years:
        print(f"  {cy}")

    report["overall_current_state"] = {
        "quarterly_extraction_runs": total_runs, "quarterly_metric_results": total_rows,
        "financial_metric_results": fmr_count, "duplicate_natural_keys": dup_all,
        "annual_v1_checksum_unchanged": actual_v1_checksum == EXPECTED_ANNUAL_V1_CHECKSUM,
        "committed_company_years": [{"ticker": t, "fiscal_year_end": f, "status": s} for t, f, s in committed_company_years],
    }

    output_path = DATA_DIR / "googl_fy2021_proof_verification_report.json"
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nReport written to {output_path}")

    return report


if __name__ == "__main__":
    main()
