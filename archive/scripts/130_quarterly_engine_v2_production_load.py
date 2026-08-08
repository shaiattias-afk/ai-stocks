"""
Loads the already-validated scripts/128 (engine v2, annual-anchor-from-
production) results into the active quarterly production tables for
exactly the 12 company-years covering all 54 ANNUAL_ROW_NOT_RESOLVED
cases proven resolvable in data/quarterly_annual_anchor_v2_validation.json.

Three phases:
  1. Read-only pre-write validation (fail-closed; writes nothing on any
     inconsistency).
  2. Full DB backup + Parquet archive of the exact rows being replaced
     (checksum-verified before any write).
  3. One DuckDB transaction per company-year: delete the old (engine-v1)
     run + 24 rows, insert the new (engine-v2) run + 24 rows, validate
     before commit. Any single company-year's failure rolls back that
     company-year AND stops the entire task (per this task's explicit
     instruction — no "continue to the next" here, unlike earlier batch
     scripts).

Does not modify scripts/118/128/129 or any earlier script. Does not touch
the XBRL warehouse, locked filings, or any annual table.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
ANNUAL_V1_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"
BACKUPS_DIR = DATA_DIR / "database" / "backups"
ARCHIVE_DIR = DATA_DIR / "archive"

VALIDATION_JSON_PATH = DATA_DIR / "quarterly_annual_anchor_v2_validation.json"
LOAD_RESULT_PATH = DATA_DIR / "quarterly_engine_v2_production_load_result.json"

ENGINE_VERSION_V1 = "118_quarterly_extraction_engine_v1"
ENGINE_VERSION_V2 = "QUARTERLY_ENGINE_V2_ANNUAL_PRODUCTION_ANCHOR"
SCHEMA_VERSION = "quarterly_v1"

EXPECTED_ANNUAL_V1_CHECKSUM = "e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814"
EXPECTED_PRE_RUNS = 45
EXPECTED_PRE_ROWS = 1080
EXPECTED_PRE_FMR = 900
EXPECTED_PRE_REVIEW_REQUIRED = 111
EXPECTED_TARGET_CASES = 54
EXPECTED_TARGET_COMPANY_YEARS = 12

EXPECTED_COMPANY_YEAR_SET = {
    ("GOOGL", "2021-12-31"), ("GOOGL", "2022-12-31"), ("GOOGL", "2023-12-31"),
    ("MU", "2021-09-02"), ("MU", "2022-09-01"), ("MU", "2023-08-31"), ("MU", "2024-08-29"), ("MU", "2025-08-28"),
    ("NVDA", "2021-01-31"), ("NVDA", "2022-01-30"), ("NVDA", "2023-01-29"), ("NVDA", "2024-01-28"),
}

METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense", "operating_cash_flow", "capex"]
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]

RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_fail_report(reason: str, details: dict) -> None:
    report = f"""# Quarterly engine-v2 production load — RESULT: FAIL (pre-write validation)

## Reason
{reason}

## Details
```json
{json.dumps(details, indent=2, ensure_ascii=False, default=str)}
```

## Result: FAIL

Nothing was written. No backup was created. No archive was created. No
transaction was opened. The active quarterly production tables are
completely unchanged.
"""
    (PROJECT_DIR / "docs" / "LAST_CLAUDE_REPORT.md").write_text(report, encoding="utf-8")
    print("\nFAIL report written to docs/LAST_CLAUDE_REPORT.md")


def values_close(a, b, tol=1.0) -> bool:
    if a is None or b is None:
        return a == b
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


# =====================================================================
# PHASE 1 — PRE-WRITE VALIDATION (read-only)
# =====================================================================

def phase1_pre_write_validation() -> dict:
    print("=" * 100)
    print("PHASE 1 — PRE-WRITE VALIDATION")
    print("=" * 100)

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)

    total_runs = connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0]
    total_rows = connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
    fmr_count = connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]

    print(f"quarterly_extraction_runs = {total_runs} (expected {EXPECTED_PRE_RUNS})")
    print(f"quarterly_metric_results = {total_rows} (expected {EXPECTED_PRE_ROWS})")
    print(f"financial_metric_results = {fmr_count} (expected {EXPECTED_PRE_FMR})")

    if total_runs != EXPECTED_PRE_RUNS or total_rows != EXPECTED_PRE_ROWS or fmr_count != EXPECTED_PRE_FMR:
        connection.close()
        raise RuntimeError(f"Starting counts do not match expected verified state: "
                            f"runs={total_runs}, rows={total_rows}, fmr={fmr_count}")

    one_run_check = connection.execute(
        "SELECT ticker, fiscal_year_end, COUNT(*) c FROM quarterly_extraction_runs "
        "GROUP BY ticker, fiscal_year_end HAVING COUNT(*) != 1"
    ).fetchall()
    rows_per_run = connection.execute(
        "SELECT r.ticker, r.fiscal_year_end, COUNT(*) c FROM quarterly_metric_results r "
        "JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
        "GROUP BY r.ticker, r.fiscal_year_end HAVING COUNT(*) != 24"
    ).fetchall()
    dup = connection.execute(
        "SELECT run_id, metric_name, fiscal_quarter, COUNT(*) c FROM quarterly_metric_results "
        "GROUP BY run_id, metric_name, fiscal_quarter HAVING COUNT(*) > 1"
    ).fetchall()
    review_required_unique = connection.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results "
        "WHERE reconciliation_status = 'REVIEW_REQUIRED')"
    ).fetchone()[0]

    print(f"company-years without exactly 1 run: {one_run_check}")
    print(f"company-years without exactly 24 rows: {rows_per_run}")
    print(f"duplicate natural keys: {dup}")
    print(f"unique REVIEW_REQUIRED cases = {review_required_unique} (expected {EXPECTED_PRE_REVIEW_REQUIRED})")

    if one_run_check or rows_per_run or dup or review_required_unique != EXPECTED_PRE_REVIEW_REQUIRED:
        connection.close()
        raise RuntimeError("Structural pre-write checks failed — see printed detail above.")

    # --- derive target list from the validation JSON ---
    with VALIDATION_JSON_PATH.open(encoding="utf-8") as handle:
        validation = json.load(handle)
    case_results = validation["validation_2"]["case_results"]
    if len(case_results) != EXPECTED_TARGET_CASES:
        connection.close()
        raise RuntimeError(f"Validation JSON has {len(case_results)} cases, expected {EXPECTED_TARGET_CASES}.")

    target_company_years = sorted({(c["ticker"], c["fiscal_year_end"]) for c in case_results})
    if len(target_company_years) != EXPECTED_TARGET_COMPANY_YEARS:
        connection.close()
        raise RuntimeError(f"Derived {len(target_company_years)} unique company-years, expected {EXPECTED_TARGET_COMPANY_YEARS}.")
    if set(target_company_years) != EXPECTED_COMPANY_YEAR_SET:
        connection.close()
        raise RuntimeError(f"Derived company-year set does not match the expected cross-check list.\n"
                            f"Derived: {sorted(target_company_years)}\nExpected: {sorted(EXPECTED_COMPANY_YEAR_SET)}")

    print(f"\nDerived {len(target_company_years)} target company-years (matches expected cross-check list exactly):")
    for cy in target_company_years:
        print(f"  {cy}")

    # cases per company-year, for later per-metric checks
    cases_by_company_year: dict[tuple, list[dict]] = {}
    for c in case_results:
        cases_by_company_year.setdefault((c["ticker"], c["fiscal_year_end"]), []).append(c)

    # --- per-company-year checks ---
    per_cy_detail = {}
    for ticker, fiscal_year_end in target_company_years:
        run_row = connection.execute(
            "SELECT run_id, run_status FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?",
            [ticker, fiscal_year_end],
        ).fetchone()
        if run_row is None:
            connection.close()
            raise RuntimeError(f"No existing production run found for {ticker} {fiscal_year_end}.")
        existing_row_count = connection.execute(
            "SELECT COUNT(*) FROM quarterly_metric_results WHERE run_id = ?", [run_row[0]]
        ).fetchone()[0]
        if existing_row_count != 24:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: existing production has {existing_row_count} rows, expected 24.")

        v2_json_path = DATA_DIR / f"quarterly_engine_v2_{ticker.lower()}_fy{fiscal_year_end[:4]}.json"
        if not v2_json_path.exists():
            connection.close()
            raise RuntimeError(f"Engine-v2 proof JSON not found: {v2_json_path}")
        with v2_json_path.open(encoding="utf-8") as handle:
            v2_output = json.load(handle)
        if len(v2_output["metrics"]) != 6:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: engine-v2 proof has {len(v2_output['metrics'])} metrics, expected 6.")

        # cross-check the target-case statuses recorded in the validation JSON
        # against what the proof JSON itself says (must agree)
        target_metrics_for_cy = {c["metric_name"] for c in cases_by_company_year[(ticker, fiscal_year_end)]}
        for case in cases_by_company_year[(ticker, fiscal_year_end)]:
            proof_status = v2_output["metrics"][case["metric_name"]].get("status")
            if proof_status != case["new_status"]:
                connection.close()
                raise RuntimeError(f"{ticker} {fiscal_year_end} {case['metric_name']}: validation JSON says "
                                    f"{case['new_status']!r} but proof JSON says {proof_status!r}.")

        # non-target metrics must be financially identical between existing
        # production (v1) and the engine-v2 proof
        existing_rows = connection.execute(
            "SELECT metric_name, fiscal_quarter, value, extraction_basis, reconciliation_status "
            "FROM quarterly_metric_results WHERE run_id = ?", [run_row[0]],
        ).fetchall()
        existing_by_key = {(m, q): (v, b, r) for m, q, v, b, r in existing_rows}

        non_target_diffs = []
        for metric_name in METRICS:
            if metric_name in target_metrics_for_cy:
                continue
            v2_metric = v2_output["metrics"].get(metric_name, {})
            for quarter in QUARTERS:
                # a quarter absent from the raw proof JSON's "quarters" dict
                # (v2_q == {}) is the SAME thing as an already-loaded
                # production row with extraction_basis="UNRESOLVED"/value=NULL
                # — both represent "this quarter did not resolve", just
                # serialized differently at the proof-JSON layer vs. the
                # already-built-row layer. Only a genuine value/basis
                # disagreement between two otherwise-resolved sides counts.
                v2_q = v2_metric.get("quarters", {}).get(quarter)
                existing = existing_by_key.get((metric_name, quarter))
                existing_value, existing_basis, existing_recon = existing if existing else (None, None, None)
                existing_unresolved = existing_basis == "UNRESOLVED" and existing_value is None
                v2_unresolved = v2_q is None

                if existing_unresolved and v2_unresolved:
                    continue  # both sides agree: unresolved, no fabricated value on either side

                if existing_unresolved != v2_unresolved:
                    non_target_diffs.append(
                        f"{metric_name}/{quarter}: resolution mismatch — existing_unresolved={existing_unresolved} "
                        f"(basis={existing_basis}, value={existing_value}), v2_unresolved={v2_unresolved} (quarter_entry={v2_q})"
                    )
                    continue

                v2_value = v2_q.get("value")
                v2_basis = v2_q.get("extraction_basis")
                if not values_close(existing_value, v2_value):
                    non_target_diffs.append(f"{metric_name}/{quarter}: value existing={existing_value} v2={v2_value}")
                if existing_basis != v2_basis:
                    non_target_diffs.append(f"{metric_name}/{quarter}: basis existing={existing_basis} v2={v2_basis}")

        if non_target_diffs:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: non-target metrics differ between v1 and v2:\n" + "\n".join(non_target_diffs))

        per_cy_detail[(ticker, fiscal_year_end)] = {
            "old_run_id": run_row[0], "old_run_status": run_row[1],
            "target_metrics": sorted(target_metrics_for_cy), "v2_json_path": str(v2_json_path),
        }
        print(f"  {ticker} {fiscal_year_end}: OK (old_run_id={run_row[0]}, target_metrics={sorted(target_metrics_for_cy)})")

    connection.close()
    print("\nPHASE 1: ALL CHECKS PASSED.")
    return {"target_company_years": target_company_years, "per_cy_detail": per_cy_detail,
            "pre_counts": {"runs": total_runs, "rows": total_rows, "fmr": fmr_count, "review_required": review_required_unique}}


# =====================================================================
# PHASE 2 — BACKUP AND ARCHIVE
# =====================================================================

def phase2_backup_and_archive(audit: dict) -> dict:
    print("\n" + "=" * 100)
    print("PHASE 2 — BACKUP AND ARCHIVE")
    print("=" * 100)

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_quarterly_engine_v2_load_{RUN_TIMESTAMP}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    source_checksum = sha256_of_file(PRODUCTION_DB_PATH)
    backup_checksum = sha256_of_file(backup_path)
    print(f"Backup: {backup_path}")
    print(f"  source checksum: {source_checksum}")
    print(f"  backup checksum: {backup_checksum}")
    if source_checksum != backup_checksum:
        raise RuntimeError("Backup checksum does not match source — aborting before any write.")

    backup_connection = duckdb.connect(database=str(backup_path), read_only=True)
    backup_counts = {
        "quarterly_extraction_runs": backup_connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
        "quarterly_metric_results": backup_connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
        "financial_metric_results": backup_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
    }
    backup_connection.close()
    print(f"  backup counts: {backup_counts}")
    if (backup_counts["quarterly_extraction_runs"] != EXPECTED_PRE_RUNS or
            backup_counts["quarterly_metric_results"] != EXPECTED_PRE_ROWS or
            backup_counts["financial_metric_results"] != EXPECTED_PRE_FMR):
        raise RuntimeError(f"Backup counts do not match expected state: {backup_counts}")

    target_company_years = audit["target_company_years"]

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    run_ids = [audit["per_cy_detail"][cy]["old_run_id"] for cy in target_company_years]
    placeholders = ",".join("?" * len(run_ids))

    # fetch via a parameterized SELECT (proven-safe pattern), then COPY the
    # registered result to Parquet with DuckDB's native writer — avoids any
    # uncertainty about parameter binding inside a COPY(...) statement
    rows_df = prod_connection.execute(
        f"SELECT * FROM quarterly_metric_results WHERE run_id IN ({placeholders})", run_ids
    ).fetchdf()
    prod_connection.register("rows_to_archive", rows_df)
    rows_parquet = ARCHIVE_DIR / f"quarterly_engine_v1_rows_replaced_{RUN_TIMESTAMP}.parquet"
    prod_connection.execute(f"COPY (SELECT * FROM rows_to_archive) TO '{rows_parquet.as_posix()}' (FORMAT PARQUET)")
    prod_connection.unregister("rows_to_archive")

    runs_df = prod_connection.execute(
        f"SELECT * FROM quarterly_extraction_runs WHERE run_id IN ({placeholders})", run_ids
    ).fetchdf()
    prod_connection.register("runs_to_archive", runs_df)
    runs_parquet = ARCHIVE_DIR / f"quarterly_engine_v1_runs_replaced_{RUN_TIMESTAMP}.parquet"
    prod_connection.execute(f"COPY (SELECT * FROM runs_to_archive) TO '{runs_parquet.as_posix()}' (FORMAT PARQUET)")
    prod_connection.unregister("runs_to_archive")

    reread_rows_count = prod_connection.execute(f"SELECT COUNT(*) FROM read_parquet('{rows_parquet.as_posix()}')").fetchone()[0]
    reread_runs_count = prod_connection.execute(f"SELECT COUNT(*) FROM read_parquet('{runs_parquet.as_posix()}')").fetchone()[0]
    prod_connection.close()

    print(f"Archived rows parquet: {rows_parquet} ({reread_rows_count} rows, expected 288)")
    print(f"Archived runs parquet: {runs_parquet} ({reread_runs_count} rows, expected 12)")

    if reread_runs_count != 12 or reread_rows_count != 288:
        raise RuntimeError(f"Archive row counts do not match expected (runs={reread_runs_count}/12, rows={reread_rows_count}/288).")

    manifest_path = ARCHIVE_DIR / f"quarterly_engine_v2_load_manifest_{RUN_TIMESTAMP}.json"
    manifest = {
        "timestamp_utc": RUN_TIMESTAMP, "production_db_path": str(PRODUCTION_DB_PATH.resolve()),
        "backup_db_path": str(backup_path.resolve()), "source_checksum": source_checksum, "backup_checksum": backup_checksum,
        "backup_counts": backup_counts, "target_company_years": [{"ticker": t, "fiscal_year_end": f} for t, f in target_company_years],
        "old_run_ids": run_ids, "rows_archived": reread_rows_count, "runs_archived": reread_runs_count,
        "rows_parquet": str(rows_parquet.resolve()), "runs_parquet": str(runs_parquet.resolve()),
        "engine_version_v1": ENGINE_VERSION_V1, "engine_version_v2": ENGINE_VERSION_V2,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Manifest: {manifest_path}")

    return {"backup_path": str(backup_path), "source_checksum": source_checksum, "backup_checksum": backup_checksum,
            "rows_parquet": str(rows_parquet), "runs_parquet": str(runs_parquet), "manifest_path": str(manifest_path)}


# =====================================================================
# ROW BUILDING (same logic as scripts/123/124/126's REVIEW_REQUIRED-
# tolerant loader — copied, not imported, per this project's established
# pattern for the warehouse/loader scripts)
# =====================================================================

def build_quarter_row(run_id, ticker, fiscal_year_end, quarter, metric_name, filings, metric_result, created_at):
    period_end = filings[quarter]["report_date"] if quarter != "Q4" else filings["FY"]["report_date"]
    availability_date = filings[quarter]["filing_date"] if quarter != "Q4" else filings["FY"]["filing_date"]
    accession_number = filings[quarter]["accession_number"] if quarter != "Q4" else filings["FY"]["accession_number"]

    quarters = metric_result.get("quarters", {})
    if quarter in quarters:
        q = quarters[quarter]
        lineage = q["lineage"]
        reconciliation = metric_result.get("reconciliation")
        return {
            "run_id": run_id, "ticker": ticker, "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter, "metric_name": metric_name,
            "value": q["value"], "unit": "iso4217:USD", "result_status": "PASS", "extraction_basis": q["extraction_basis"],
            "period_start": lineage.get("period_start"), "period_end": period_end, "availability_date": q["availability_date"],
            "accession_number": lineage.get("accession_number", lineage.get("annual_accession_number", accession_number)),
            "concept_qname": lineage.get("concept_qname", lineage.get("annual_concept_qname")),
            "context_id": lineage.get("context_id", lineage.get("nine_month_ytd_context_id")),
            "dimensions_json": "{}", "lineage_json": json.dumps(lineage, ensure_ascii=False, default=str),
            "reconciliation_status": reconciliation["status"] if reconciliation else "REVIEW_REQUIRED",
            "reconciliation_difference": reconciliation["difference"] if reconciliation else None,
            "permitted_difference": reconciliation["precision_calculation"]["permitted_difference"] if reconciliation else None,
            "created_at": created_at,
        }

    error_text = metric_result.get("error", "metric did not resolve for this quarter")
    return {
        "run_id": run_id, "ticker": ticker, "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter, "metric_name": metric_name,
        "value": None, "unit": "iso4217:USD", "result_status": "REVIEW_REQUIRED", "extraction_basis": "UNRESOLVED",
        "period_start": None, "period_end": period_end, "availability_date": availability_date, "accession_number": accession_number,
        "concept_qname": None, "context_id": None, "dimensions_json": "{}",
        "lineage_json": json.dumps({"error": error_text, "source": "engine could not resolve this metric"}, ensure_ascii=False),
        "reconciliation_status": "REVIEW_REQUIRED", "reconciliation_difference": None, "permitted_difference": None,
        "created_at": created_at,
    }


# =====================================================================
# PHASE 3 — TRANSACTIONAL REPLACEMENT, one company-year per transaction
# =====================================================================

def replace_one_company_year(connection, ticker: str, fiscal_year_end: str, old_run_id: str, v2_json_path: str) -> dict:
    cy_start = time.perf_counter()

    with Path(v2_json_path).open(encoding="utf-8") as handle:
        v2_output = json.load(handle)
    filings = v2_output["filings"]

    # reconfirm existing state
    existing_run = connection.execute(
        "SELECT run_id FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?", [ticker, fiscal_year_end]
    ).fetchone()
    if existing_run is None or existing_run[0] != old_run_id:
        return {"status": "ROLLED_BACK", "reason": f"reconfirmation failed: expected run_id {old_run_id}, found {existing_run}"}
    existing_row_count = connection.execute(
        "SELECT COUNT(*) FROM quarterly_metric_results WHERE run_id = ?", [old_run_id]
    ).fetchone()[0]
    if existing_row_count != 24:
        return {"status": "ROLLED_BACK", "reason": f"reconfirmation failed: {existing_row_count} existing rows, expected 24"}

    new_run_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    has_any_review_required = any(len(v2_output["metrics"].get(m, {}).get("quarters", {})) != 4 for m in METRICS)
    run_status = "PASS_WITH_REVIEW_REQUIRED" if has_any_review_required else "PASS"

    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute("DELETE FROM quarterly_metric_results WHERE run_id = ?", [old_run_id])
        connection.execute("DELETE FROM quarterly_extraction_runs WHERE run_id = ?", [old_run_id])

        connection.execute(
            "INSERT INTO quarterly_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [new_run_id, ticker, fiscal_year_end, ENGINE_VERSION_V2, SCHEMA_VERSION,
             filings["Q1"]["accession_number"], filings["Q2"]["accession_number"],
             filings["Q3"]["accession_number"], filings["FY"]["accession_number"],
             str(v2_json_path), "LOADING", created_at, None],
        )

        for metric_name in METRICS:
            metric_result = v2_output["metrics"][metric_name]
            for quarter in QUARTERS:
                row = build_quarter_row(new_run_id, ticker, fiscal_year_end, quarter, metric_name, filings, metric_result, created_at)
                connection.execute(
                    "INSERT INTO quarterly_metric_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [row["run_id"], row["ticker"], row["fiscal_year_end"], row["fiscal_quarter"], row["metric_name"],
                     row["value"], row["unit"], row["result_status"], row["extraction_basis"], row["period_start"],
                     row["period_end"], row["availability_date"], row["accession_number"], row["concept_qname"],
                     row["context_id"], row["dimensions_json"], row["lineage_json"], row["reconciliation_status"],
                     row["reconciliation_difference"], row["permitted_difference"], row["created_at"]],
                )

        # --- validate before commit ---
        errors = []
        committed = connection.execute(
            "SELECT fiscal_quarter, metric_name, value, extraction_basis, reconciliation_status, "
            "lineage_json, availability_date, accession_number FROM quarterly_metric_results WHERE run_id = ?",
            [new_run_id],
        ).fetchdf()

        if len(committed) != 24:
            errors.append(f"row count = {len(committed)}, expected 24")

        dup = committed.groupby(["metric_name", "fiscal_quarter"]).size()
        dup = dup[dup > 1]
        if len(dup) > 0:
            errors.append(f"duplicate natural keys: {dup.to_dict()}")

        if committed["lineage_json"].isna().any():
            errors.append("missing lineage_json on at least one row")

        avail_mismatch = connection.execute(
            "SELECT COUNT(*) FROM quarterly_metric_results r JOIN sec_filings s ON s.accession_number = r.accession_number "
            "WHERE r.run_id = ? AND r.availability_date != CAST(s.filing_date AS VARCHAR)", [new_run_id],
        ).fetchone()[0]
        if avail_mismatch > 0:
            errors.append(f"{avail_mismatch} availability-date mismatch(es)")

        # cross-check values/bases/statuses against the v2 proof JSON exactly
        for _, row in committed.iterrows():
            source_metric = v2_output["metrics"][row["metric_name"]]
            source_quarters = source_metric.get("quarters", {})
            if row["fiscal_quarter"] in source_quarters:
                source_value = source_quarters[row["fiscal_quarter"]]["value"]
                if row["value"] is None or abs(float(row["value"]) - float(source_value)) >= 1:
                    errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: value mismatch vs. proof")
                if row["extraction_basis"] != source_quarters[row["fiscal_quarter"]]["extraction_basis"]:
                    errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: basis mismatch vs. proof")
            source_status = source_metric.get("status")
            row_recon_status = row["reconciliation_status"]
            # metric-level status should match every quarter's reconciliation_status for that metric
            if row["fiscal_quarter"] in source_quarters and row_recon_status != source_status:
                errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: reconciliation_status "
                               f"{row_recon_status!r} != proof metric status {source_status!r}")

        # annual-anchor accession must equal the exact FY accession for every Q4 row
        q4_rows = committed[committed["fiscal_quarter"] == "Q4"]
        for _, row in q4_rows.iterrows():
            if row["accession_number"] != filings["FY"]["accession_number"]:
                errors.append(f"{row['metric_name']}/Q4: accession {row['accession_number']} != FY accession {filings['FY']['accession_number']}")

        if errors:
            connection.execute("ROLLBACK")
            return {"status": "ROLLED_BACK", "reason": "; ".join(errors), "old_run_id": old_run_id, "new_run_id": new_run_id}

        connection.execute(
            "UPDATE quarterly_extraction_runs SET run_status = ?, completed_at = ? WHERE run_id = ?",
            [run_status, datetime.now(timezone.utc).isoformat(), new_run_id],
        )
        connection.execute("COMMIT")

        status_counts = committed["reconciliation_status"].value_counts().to_dict()
        return {"status": "COMMITTED", "old_run_id": old_run_id, "new_run_id": new_run_id,
                "rows_replaced": int(len(committed)), "run_status": run_status,
                "reconciliation_status_counts": status_counts, "elapsed_seconds": round(time.perf_counter() - cy_start, 2)}

    except Exception as exc:  # noqa: BLE001
        connection.execute("ROLLBACK")
        return {"status": "ROLLED_BACK", "reason": str(exc), "old_run_id": old_run_id, "new_run_id": new_run_id}


def atomic_write_json(path: Path, data: dict) -> None:
    temp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
    os.replace(temp_path, path)


def main() -> None:
    batch_start = time.perf_counter()

    try:
        audit = phase1_pre_write_validation()
    except Exception as exc:  # noqa: BLE001
        write_fail_report(str(exc), {})
        print(f"\nPHASE 1 FAILED: {exc}")
        return

    try:
        archive_info = phase2_backup_and_archive(audit)
    except Exception as exc:  # noqa: BLE001
        write_fail_report(f"Phase 2 (backup/archive) failed: {exc}", {})
        print(f"\nPHASE 2 FAILED: {exc}")
        return

    print("\n" + "=" * 100)
    print("PHASE 3 — TRANSACTIONAL REPLACEMENT")
    print("=" * 100)

    company_year_results: dict = {}
    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)

    stopped_early = False
    for ticker, fiscal_year_end in audit["target_company_years"]:
        key = f"{ticker}|{fiscal_year_end}"
        detail = audit["per_cy_detail"][(ticker, fiscal_year_end)]
        print(f"\n>>> {ticker} FY(end={fiscal_year_end}) <<<")

        result = replace_one_company_year(connection, ticker, fiscal_year_end, detail["old_run_id"], detail["v2_json_path"])
        company_year_results[key] = result
        print(f"  {result['status']}" + (f" — {result.get('reason')}" if result["status"] == "ROLLED_BACK" else
              f" (rows_replaced={result.get('rows_replaced')}, run_status={result.get('run_status')})"))

        atomic_write_json(LOAD_RESULT_PATH, {
            "last_updated_utc": datetime.now(timezone.utc).isoformat(),
            "target_company_years": len(audit["target_company_years"]),
            "completed_count": sum(1 for r in company_year_results.values() if r["status"] == "COMMITTED"),
            "company_year_results": company_year_results,
        })

        if result["status"] != "COMMITTED":
            print(f"\n  STOPPING — {ticker} {fiscal_year_end} failed validation and was rolled back. "
                  "Per instructions, the task stops here (no further company-years processed).")
            stopped_early = True
            break

    connection.close()

    write_final_report(audit, archive_info, company_year_results, batch_start, stopped_early)


def write_final_report(audit, archive_info, company_year_results, batch_start, stopped_early) -> None:
    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    total_runs = connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0]
    total_rows = connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
    rows_per_cy = connection.execute(
        "SELECT r.ticker, r.fiscal_year_end, COUNT(*) c FROM quarterly_metric_results r "
        "JOIN quarterly_extraction_runs e ON e.run_id = r.run_id GROUP BY r.ticker, r.fiscal_year_end HAVING COUNT(*) != 24"
    ).fetchall()
    dup = connection.execute(
        "SELECT run_id, metric_name, fiscal_quarter, COUNT(*) c FROM quarterly_metric_results "
        "GROUP BY run_id, metric_name, fiscal_quarter HAVING COUNT(*) > 1"
    ).fetchall()
    missing_lineage = connection.execute("SELECT COUNT(*) FROM quarterly_metric_results WHERE lineage_json IS NULL").fetchone()[0]
    avail_mismatch = connection.execute(
        "SELECT COUNT(*) FROM quarterly_metric_results r JOIN sec_filings s ON s.accession_number = r.accession_number "
        "WHERE r.availability_date != CAST(s.filing_date AS VARCHAR)"
    ).fetchone()[0]
    review_required_unique = connection.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results "
        "WHERE reconciliation_status = 'REVIEW_REQUIRED')"
    ).fetchone()[0]
    annual_row_not_resolved_remaining = connection.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results "
        "WHERE reconciliation_status = 'REVIEW_REQUIRED' AND ticker IN ('GOOGL','MU','NVDA'))"
    ).fetchone()[0]
    fmr_count = connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    non_target_sample = connection.execute(
        "SELECT ticker, fiscal_year_end, COUNT(*) FROM quarterly_extraction_runs "
        "WHERE (ticker, fiscal_year_end) IN (('MSFT','2024-06-30'),('AMZN','2024-12-31'),('ORCL','2024-05-31')) "
        "GROUP BY ticker, fiscal_year_end"
    ).fetchall()
    connection.close()

    actual_v1_checksum = sha256_of_file(ANNUAL_V1_DB_PATH)
    v1_ok = actual_v1_checksum == EXPECTED_ANNUAL_V1_CHECKSUM

    committed_count = sum(1 for r in company_year_results.values() if r["status"] == "COMMITTED")
    all_committed = committed_count == 12 and not stopped_early

    if all_committed and total_runs == 45 and total_rows == 1080 and not rows_per_cy and not dup and missing_lineage == 0 and avail_mismatch == 0 and v1_ok and fmr_count == 900:
        overall = "PASS"
    elif committed_count > 0:
        overall = "PARTIAL PASS"
    else:
        overall = "FAIL"

    lines = [
        f"# Quarterly engine-v2 production load — RESULT: {overall}",
        "",
        "## Backup and archive",
        f"- Backup: `{archive_info['backup_path']}`",
        f"- Source checksum: `{archive_info['source_checksum']}`",
        f"- Backup checksum: `{archive_info['backup_checksum']}` (match: {archive_info['source_checksum'] == archive_info['backup_checksum']})",
        f"- Archived v1 rows: `{archive_info['rows_parquet']}`",
        f"- Archived v1 runs: `{archive_info['runs_parquet']}`",
        f"- Manifest: `{archive_info['manifest_path']}`",
        "",
        "## Company-year replacement results",
    ]
    for key, result in company_year_results.items():
        lines.append(f"- {key}: {result['status']}" +
                      (f" (old_run_id={result.get('old_run_id')}, new_run_id={result.get('new_run_id')}, "
                       f"rows_replaced={result.get('rows_replaced')}, run_status={result.get('run_status')}, "
                       f"recon_counts={result.get('reconciliation_status_counts')})"
                       if result["status"] == "COMMITTED" else f" — {result.get('reason')}"))

    lines += [
        "",
        "## Post-load counts",
        f"- quarterly_extraction_runs = {total_runs} (expected 45)",
        f"- quarterly_metric_results = {total_rows} (expected 1080)",
        f"- company-years without exactly 24 rows = {rows_per_cy}",
        f"- duplicate natural keys = {dup}",
        f"- missing lineage = {missing_lineage}",
        f"- availability-date mismatches = {avail_mismatch}",
        f"- unique REVIEW_REQUIRED cases (all categories) = {review_required_unique} (expected 57 if all 12 committed)",
        f"- REVIEW_REQUIRED cases among GOOGL/MU/NVDA = {annual_row_not_resolved_remaining}",
        f"- financial_metric_results (annual) = {fmr_count} (expected 900)",
        f"- Annual V1 checksum unchanged = {v1_ok}",
        f"- baseline (MSFT/AMZN/ORCL) extraction_runs still present = {non_target_sample}",
        f"- companies committed = {committed_count}/12",
        f"- total elapsed seconds = {round(time.perf_counter() - batch_start, 2)}",
        "",
        f"## Result: {overall}",
    ]
    (PROJECT_DIR / "docs" / "LAST_CLAUDE_REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    print("\n" + "=" * 100)
    print(f"FINAL: {overall}  committed={committed_count}/12  total_runs={total_runs}/45  total_rows={total_rows}/1080  "
          f"review_required={review_required_unique} (57 expected if fully committed)")
    print("=" * 100)


if __name__ == "__main__":
    main()
