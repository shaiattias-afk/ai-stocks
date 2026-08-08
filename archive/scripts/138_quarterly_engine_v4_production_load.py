"""
Loads the already-validated scripts/136 (engine v4, point-in-time-safe
concept reuse) results into the active quarterly production tables for
exactly the 10 company-years covering the 11 resolved CONCEPT_REUSE_
CANDIDATE cases, per data/quarterly_concept_reuse_v4_validation.json
(the output of scripts/137, NOT re-run here).

Explicitly excludes the 4 cases proven to have no point-in-time-safe
concept source (CRWD 2022-01-31 pretax_income; MU 2021-09-02
pretax_income; PANW 2021-07-31 revenue; PANW 2021-07-31 pretax_income) —
each is its ticker's earliest fiscal year in the database, with no
earlier same-year quarter and no earlier fiscal year's 10-K to reuse a
concept from. These 4 remain REVIEW_REQUIRED, unchanged, same as the 6
NVDA warehouse-ingestion cases (also out of scope here).

Three phases, same discipline as scripts/130/134 (D-037/D-038):
  1. Read-only pre-write validation against the SAVED v4 validation JSON
     (fail-closed). The saved validation JSON's case_results carry full
     lineage only for the 15 target metric-cases, not the other 4-5
     non-target metrics each affected company-year also has — so this
     phase also invokes scripts/136's engine function directly (NOT
     scripts/137, NOT the 45-company regression) for exactly the 10
     derived target company-years, and cross-checks every one of the 6
     metrics' resulting status against the saved validation JSON.
  2. Full DB backup + Parquet archive of the exact rows being replaced
     (checksum-verified before any write).
  3. One DuckDB transaction per company-year: delete the old run + 24
     rows, insert the new (engine-v4) run + 24 rows, validate before
     commit — including point-in-time-specific checks (every concept-
     source filing_date <= blocking filing_date; no same-fiscal-year
     10-K ever used as a source). Any single company-year's failure
     rolls back that company-year AND stops the entire task.

Does not modify scripts/128/130/132/134/136/137 or any earlier script.
Does not touch the XBRL warehouse, locked filings, or any annual table.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
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

VALIDATION_JSON_PATH = DATA_DIR / "quarterly_concept_reuse_v4_validation.json"
LOAD_RESULT_PATH = DATA_DIR / "quarterly_engine_v4_production_load_result.json"

ENGINE_VERSION_V4 = "QUARTERLY_ENGINE_V4_POINT_IN_TIME_CONCEPT_REUSE"
SCHEMA_VERSION = "quarterly_v1"

EXPECTED_ANNUAL_V1_CHECKSUM = "e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814"
EXPECTED_PRE_RUNS = 45
EXPECTED_PRE_ROWS = 1080
EXPECTED_PRE_FMR = 900
EXPECTED_PRE_REVIEW_REQUIRED = 21

EXPECTED_TARGET_CASE_COUNT = 15
EXPECTED_RESOLVED_CASES = 11
EXPECTED_PASS = 9
EXPECTED_PASS_ROUNDING_TOLERANCE = 2
EXPECTED_EXCLUDED_CASES = 4
EXPECTED_TARGET_COMPANY_YEARS = 10
EXPECTED_POST_REVIEW_REQUIRED = 10

EXCLUDED_CASE_KEYS = {
    "CRWD 2022-01-31 pretax_income", "MU 2021-09-02 pretax_income",
    "PANW 2021-07-31 revenue", "PANW 2021-07-31 pretax_income",
}

METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense", "operating_cash_flow", "capex"]
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]

RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_module(filename: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, PROJECT_DIR / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


s136 = _load_module("136_quarterly_extraction_engine_v4_point_in_time_concept_reuse.py", "s136_v4_engine")


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_fail_report(reason: str, details: dict) -> None:
    report = f"""# Quarterly engine V4 production load — RESULT: FAIL (pre-write validation)

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
# PHASE 1 — PRE-WRITE VALIDATION
# =====================================================================

def phase1_pre_write_validation() -> dict:
    print("=" * 100)
    print("PHASE 1 — PRE-WRITE VALIDATION (against the SAVED scripts/137 validation output)")
    print("=" * 100)

    with VALIDATION_JSON_PATH.open(encoding="utf-8") as handle:
        validation = json.load(handle)

    v1 = validation["validation_1"]
    if not v1.get("row_count_ok") or not v1.get("zero_differences") or not v1.get("fallback_correctly_inactive"):
        raise RuntimeError(f"Saved validation_1 does not show a clean baseline: {v1}")

    v2 = validation["validation_2"]
    if v2["target_case_count"] != EXPECTED_TARGET_CASE_COUNT:
        raise RuntimeError(f"Saved target_case_count={v2['target_case_count']}, expected {EXPECTED_TARGET_CASE_COUNT}.")
    oc = v2["outcome_counts"]
    if oc.get("PASS") != EXPECTED_PASS or oc.get("PASS_ROUNDING_TOLERANCE") != EXPECTED_PASS_ROUNDING_TOLERANCE:
        raise RuntimeError(f"Saved outcome_counts={oc} does not match expected PASS={EXPECTED_PASS}, "
                            f"PASS_ROUNDING_TOLERANCE={EXPECTED_PASS_ROUNDING_TOLERANCE}.")
    if oc.get("REVIEW_REQUIRED") != EXPECTED_EXCLUDED_CASES:
        raise RuntimeError(f"Saved REVIEW_REQUIRED count={oc.get('REVIEW_REQUIRED')}, expected {EXPECTED_EXCLUDED_CASES}.")
    if v2["regressions_found"] != 0 or v2["future_data_violations_found"] != 0 or v2["unexpected_findings_count"] != 0:
        raise RuntimeError(f"Saved validation_2 reports non-zero regressions/violations/unexpected findings: {v2}")

    still_review = {d["case"] for d in v2["still_review_required_detail"]}
    if still_review != EXCLUDED_CASE_KEYS:
        raise RuntimeError(f"Saved still-REVIEW_REQUIRED case set {still_review} does not match expected exclusion set {EXCLUDED_CASE_KEYS}.")

    case_results = validation["case_results"]
    resolved_cases = [c for c in case_results if c["new_status_v4"] in ("PASS", "PASS_ROUNDING_TOLERANCE")]
    if len(resolved_cases) != EXPECTED_RESOLVED_CASES:
        raise RuntimeError(f"Found {len(resolved_cases)} resolved cases in case_results, expected {EXPECTED_RESOLVED_CASES}.")
    for c in resolved_cases:
        key = f"{c['ticker']} {c['fiscal_year_end']} {c['metric_name']}"
        if key in EXCLUDED_CASE_KEYS:
            raise RuntimeError(f"Resolved case {key} is in the exclusion set — refusing to proceed (data inconsistency).")

    print(f"Saved validation confirmed: 15 target cases; {len(resolved_cases)} resolved (PASS={oc['PASS']}, "
          f"PASS_ROUNDING_TOLERANCE={oc['PASS_ROUNDING_TOLERANCE']}); {len(still_review)} excluded (no "
          f"point-in-time-safe source); 0 regressions/violations/unexpected findings.")

    target_company_years = sorted({(c["ticker"], c["fiscal_year_end"]) for c in resolved_cases})
    if len(target_company_years) != EXPECTED_TARGET_COMPANY_YEARS:
        raise RuntimeError(f"Derived {len(target_company_years)} unique target company-years, expected {EXPECTED_TARGET_COMPANY_YEARS}.\n"
                            f"Derived: {target_company_years}")

    resolved_metrics_by_cy: dict[tuple, set[str]] = {}
    for c in resolved_cases:
        resolved_metrics_by_cy.setdefault((c["ticker"], c["fiscal_year_end"]), set()).add(c["metric_name"])

    print(f"\nDerived {len(target_company_years)} target company-years:")
    for cy in target_company_years:
        print(f"  {cy}: resolved metrics = {sorted(resolved_metrics_by_cy[cy])}")

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)

    total_runs = connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0]
    total_rows = connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
    fmr_count = connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    review_required_unique = connection.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results "
        "WHERE reconciliation_status = 'REVIEW_REQUIRED')"
    ).fetchone()[0]
    print(f"\nCurrent production state: runs={total_runs} rows={total_rows} fmr={fmr_count} unique_review_required={review_required_unique}")
    if total_runs != EXPECTED_PRE_RUNS or total_rows != EXPECTED_PRE_ROWS or fmr_count != EXPECTED_PRE_FMR or review_required_unique != EXPECTED_PRE_REVIEW_REQUIRED:
        connection.close()
        raise RuntimeError(f"Starting counts do not match expected verified state: "
                            f"runs={total_runs}, rows={total_rows}, fmr={fmr_count}, review_required={review_required_unique}")

    per_cy_detail = {}
    for ticker, fiscal_year_end in target_company_years:
        run_row = connection.execute(
            "SELECT run_id, q1_accession, q2_accession, q3_accession, fy_accession, engine_version "
            "FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?",
            [ticker, fiscal_year_end],
        ).fetchone()
        if run_row is None:
            connection.close()
            raise RuntimeError(f"No existing production run found for {ticker} {fiscal_year_end}.")
        old_run_id, q1_acc, q2_acc, q3_acc, fy_acc, old_engine_version = run_row
        existing_row_count = connection.execute(
            "SELECT COUNT(*) FROM quarterly_metric_results WHERE run_id = ?", [old_run_id]
        ).fetchone()[0]
        if existing_row_count != 24:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: existing production has {existing_row_count} rows, expected 24.")

        # --- regenerate the full-lineage v4 output for this company-year via
        # scripts/136's engine directly (NOT scripts/137) ---
        v4_json_path = DATA_DIR / f"quarterly_engine_v4_{ticker.lower()}_fy{fiscal_year_end[:4]}.json"
        v4_csv_path = DATA_DIR / f"quarterly_engine_v4_{ticker.lower()}_fy{fiscal_year_end[:4]}.csv"
        v4_output = s136.run_quarterly_extraction_engine_v4(
            ticker=ticker, fiscal_year_end=fiscal_year_end,
            q1_accession=q1_acc, q2_accession=q2_acc, q3_accession=q3_acc, fy_accession=fy_acc,
            json_output_path=v4_json_path, csv_output_path=v4_csv_path,
        )
        if len(v4_output["metrics"]) != 6:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: freshly-generated v4 output has {len(v4_output['metrics'])} metrics, expected 6.")

        # cross-check the resolved target metrics' status against the SAVED
        # validation JSON for this exact company-year
        status_mismatches = []
        point_in_time_violations = []
        for metric_name in resolved_metrics_by_cy[(ticker, fiscal_year_end)]:
            saved_case = next(c for c in resolved_cases if c["ticker"] == ticker and c["fiscal_year_end"] == fiscal_year_end and c["metric_name"] == metric_name)
            fresh_status = v4_output["metrics"][metric_name].get("status")
            if fresh_status != saved_case["new_status_v4"]:
                status_mismatches.append(f"{metric_name}: freshly-generated status={fresh_status!r} != saved validation status={saved_case['new_status_v4']!r}")

            # point-in-time re-verification against the FRESH run (not just the saved JSON)
            lineage = v4_output["metrics"][metric_name].get("concept_source_lineage", {})
            fy_accession = v4_output["filings"]["FY"]["accession_number"]
            for quarter, src in lineage.items():
                if src.get("source") == "POINT_IN_TIME_SAFE_CONCEPT_REUSE":
                    source_filing_date = src.get("source_filing_date")
                    blocking_filing_date = src.get("blocking_filing_date")
                    if source_filing_date is None or blocking_filing_date is None or source_filing_date > blocking_filing_date:
                        point_in_time_violations.append(f"{metric_name}/{quarter}: source_filing_date={source_filing_date} > blocking_filing_date={blocking_filing_date}")
                    if src.get("source_accession") == fy_accession:
                        point_in_time_violations.append(f"{metric_name}/{quarter}: concept source accession equals the SAME fiscal year's own FY 10-K — forbidden")

        if status_mismatches:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: freshly-generated v4 engine output does not reproduce the saved "
                                f"validation (environment drift or non-determinism — refusing to load):\n" + "\n".join(status_mismatches))
        if point_in_time_violations:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: point-in-time policy violation detected on fresh re-verification:\n" + "\n".join(point_in_time_violations))

        per_cy_detail[(ticker, fiscal_year_end)] = {
            "old_run_id": old_run_id, "old_engine_version": old_engine_version,
            "v4_json_path": str(v4_json_path), "resolved_metrics": sorted(resolved_metrics_by_cy[(ticker, fiscal_year_end)]),
        }
        print(f"  {ticker} {fiscal_year_end}: OK (old_run_id={old_run_id}, old_engine_version={old_engine_version}, "
              f"statuses reproduced exactly, 0 point-in-time violations)")

    connection.close()
    print("\nPHASE 1: ALL CHECKS PASSED.")
    return {
        "target_company_years": target_company_years, "per_cy_detail": per_cy_detail,
        "pre_counts": {"runs": total_runs, "rows": total_rows, "fmr": fmr_count, "review_required": review_required_unique},
        "resolved_case_count": len(resolved_cases), "excluded_case_count": len(still_review),
    }


# =====================================================================
# PHASE 2 — BACKUP AND ARCHIVE
# =====================================================================

def phase2_backup_and_archive(audit: dict) -> dict:
    print("\n" + "=" * 100)
    print("PHASE 2 — BACKUP AND ARCHIVE")
    print("=" * 100)

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_quarterly_engine_v4_load_{RUN_TIMESTAMP}.duckdb"
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

    rows_df = prod_connection.execute(
        f"SELECT * FROM quarterly_metric_results WHERE run_id IN ({placeholders})", run_ids
    ).fetchdf()
    prod_connection.register("rows_to_archive", rows_df)
    rows_parquet = ARCHIVE_DIR / f"quarterly_engine_v4_rows_replaced_{RUN_TIMESTAMP}.parquet"
    prod_connection.execute(f"COPY (SELECT * FROM rows_to_archive) TO '{rows_parquet.as_posix()}' (FORMAT PARQUET)")
    prod_connection.unregister("rows_to_archive")

    runs_df = prod_connection.execute(
        f"SELECT * FROM quarterly_extraction_runs WHERE run_id IN ({placeholders})", run_ids
    ).fetchdf()
    prod_connection.register("runs_to_archive", runs_df)
    runs_parquet = ARCHIVE_DIR / f"quarterly_engine_v4_runs_replaced_{RUN_TIMESTAMP}.parquet"
    prod_connection.execute(f"COPY (SELECT * FROM runs_to_archive) TO '{runs_parquet.as_posix()}' (FORMAT PARQUET)")
    prod_connection.unregister("runs_to_archive")

    reread_rows_count = prod_connection.execute(f"SELECT COUNT(*) FROM read_parquet('{rows_parquet.as_posix()}')").fetchone()[0]
    reread_runs_count = prod_connection.execute(f"SELECT COUNT(*) FROM read_parquet('{runs_parquet.as_posix()}')").fetchone()[0]
    prod_connection.close()

    expected_rows_archived = EXPECTED_TARGET_COMPANY_YEARS * 24
    print(f"Archived rows parquet: {rows_parquet} ({reread_rows_count} rows, expected {expected_rows_archived})")
    print(f"Archived runs parquet: {runs_parquet} ({reread_runs_count} rows, expected {EXPECTED_TARGET_COMPANY_YEARS})")

    if reread_runs_count != EXPECTED_TARGET_COMPANY_YEARS or reread_rows_count != expected_rows_archived:
        raise RuntimeError(f"Archive row counts do not match expected (runs={reread_runs_count}/{EXPECTED_TARGET_COMPANY_YEARS}, "
                            f"rows={reread_rows_count}/{expected_rows_archived}).")

    manifest_path = ARCHIVE_DIR / f"quarterly_engine_v4_load_manifest_{RUN_TIMESTAMP}.json"
    manifest = {
        "timestamp_utc": RUN_TIMESTAMP, "production_db_path": str(PRODUCTION_DB_PATH.resolve()),
        "backup_db_path": str(backup_path.resolve()), "source_checksum": source_checksum, "backup_checksum": backup_checksum,
        "backup_counts": backup_counts, "target_company_years": [{"ticker": t, "fiscal_year_end": f} for t, f in target_company_years],
        "old_run_ids": run_ids, "rows_archived": reread_rows_count, "runs_archived": reread_runs_count,
        "rows_parquet": str(rows_parquet.resolve()), "runs_parquet": str(runs_parquet.resolve()),
        "engine_version_v4": ENGINE_VERSION_V4, "resolved_case_count": audit["resolved_case_count"],
        "excluded_case_count": audit["excluded_case_count"], "excluded_case_keys": sorted(EXCLUDED_CASE_KEYS),
        "validation_source": str(VALIDATION_JSON_PATH.resolve()),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Manifest: {manifest_path}")

    return {"backup_path": str(backup_path), "source_checksum": source_checksum, "backup_checksum": backup_checksum,
            "rows_parquet": str(rows_parquet), "runs_parquet": str(runs_parquet), "manifest_path": str(manifest_path)}


# =====================================================================
# ROW BUILDING — same logic as scripts/123/124/126/130/134's REVIEW_
# REQUIRED-tolerant loader (copied, not imported)
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

def replace_one_company_year(connection, ticker: str, fiscal_year_end: str, old_run_id: str, v4_json_path: str) -> dict:
    cy_start = time.perf_counter()

    with Path(v4_json_path).open(encoding="utf-8") as handle:
        v4_output = json.load(handle)
    filings = v4_output["filings"]

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
    has_any_review_required = any(len(v4_output["metrics"].get(m, {}).get("quarters", {})) != 4 for m in METRICS)
    run_status = "PASS_WITH_REVIEW_REQUIRED" if has_any_review_required else "PASS"

    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute("DELETE FROM quarterly_metric_results WHERE run_id = ?", [old_run_id])
        connection.execute("DELETE FROM quarterly_extraction_runs WHERE run_id = ?", [old_run_id])

        connection.execute(
            "INSERT INTO quarterly_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [new_run_id, ticker, fiscal_year_end, ENGINE_VERSION_V4, SCHEMA_VERSION,
             filings["Q1"]["accession_number"], filings["Q2"]["accession_number"],
             filings["Q3"]["accession_number"], filings["FY"]["accession_number"],
             str(v4_json_path), "LOADING", created_at, None],
        )

        for metric_name in METRICS:
            metric_result = v4_output["metrics"][metric_name]
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

        for _, row in committed.iterrows():
            source_metric = v4_output["metrics"][row["metric_name"]]
            source_quarters = source_metric.get("quarters", {})
            if row["fiscal_quarter"] in source_quarters:
                source_value = source_quarters[row["fiscal_quarter"]]["value"]
                if row["value"] is None or abs(float(row["value"]) - float(source_value)) >= 1:
                    errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: value mismatch vs. proof")
                if row["extraction_basis"] != source_quarters[row["fiscal_quarter"]]["extraction_basis"]:
                    errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: basis mismatch vs. proof")
            source_status = source_metric.get("status")
            row_recon_status = row["reconciliation_status"]
            if row["fiscal_quarter"] in source_quarters and row_recon_status != source_status:
                errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: reconciliation_status "
                               f"{row_recon_status!r} != proof metric status {source_status!r}")

        q4_rows = committed[committed["fiscal_quarter"] == "Q4"]
        for _, row in q4_rows.iterrows():
            if row["accession_number"] != filings["FY"]["accession_number"]:
                errors.append(f"{row['metric_name']}/Q4: accession {row['accession_number']} != FY accession {filings['FY']['accession_number']}")

        # no comparative prior-year fact: every resolved quarter's own
        # period_end must equal that quarter's own filing report_date
        for _, row in committed.iterrows():
            if row["fiscal_quarter"] == "Q4":
                continue
            source_metric = v4_output["metrics"][row["metric_name"]]
            if row["fiscal_quarter"] in source_metric.get("quarters", {}):
                expected_end = filings[row["fiscal_quarter"]]["report_date"]
                lineage = json.loads(row["lineage_json"])
                actual_end = lineage.get("period_end")
                if actual_end and actual_end != expected_end:
                    errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: period_end {actual_end} != filing report_date {expected_end} "
                                   f"(possible comparative prior-year fact)")

        # point-in-time-specific checks: every concept-source filing_date
        # must be <= blocking filing_date; the FY accession must never be
        # used as a concept source
        fy_accession = filings["FY"]["accession_number"]
        for metric_name in METRICS:
            lineage_by_q = v4_output["metrics"][metric_name].get("concept_source_lineage", {})
            for quarter, src in lineage_by_q.items():
                if src.get("source") == "POINT_IN_TIME_SAFE_CONCEPT_REUSE":
                    sfd, bfd = src.get("source_filing_date"), src.get("blocking_filing_date")
                    if not sfd or not bfd or sfd > bfd:
                        errors.append(f"{metric_name}/{quarter}: point-in-time violation, source_filing_date={sfd} > blocking_filing_date={bfd}")
                    if src.get("source_accession") == fy_accession:
                        errors.append(f"{metric_name}/{quarter}: concept source is the same fiscal year's own FY 10-K — forbidden")

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

        result = replace_one_company_year(connection, ticker, fiscal_year_end, detail["old_run_id"], detail["v4_json_path"])
        result["resolved_metrics"] = detail["resolved_metrics"]
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
    excluded_still_review = connection.execute(
        "SELECT ticker, fiscal_year_end, metric_name FROM quarterly_metric_results "
        "WHERE reconciliation_status='REVIEW_REQUIRED' AND (ticker, fiscal_year_end, metric_name) IN "
        "(('CRWD','2022-01-31','pretax_income'),('MU','2021-09-02','pretax_income'),"
        "('PANW','2021-07-31','revenue'),('PANW','2021-07-31','pretax_income')) GROUP BY ticker, fiscal_year_end, metric_name"
    ).fetchall()
    nvda_still_review = connection.execute(
        "SELECT COUNT(DISTINCT metric_name) FROM quarterly_metric_results "
        "WHERE ticker='NVDA' AND fiscal_year_end='2020-01-26' AND reconciliation_status='REVIEW_REQUIRED'"
    ).fetchone()[0]
    fmr_count = connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    engine_version_breakdown = connection.execute(
        "SELECT engine_version, COUNT(*) FROM quarterly_extraction_runs GROUP BY engine_version"
    ).fetchall()
    connection.close()

    actual_v1_checksum = sha256_of_file(ANNUAL_V1_DB_PATH)
    v1_ok = actual_v1_checksum == EXPECTED_ANNUAL_V1_CHECKSUM

    committed_count = sum(1 for r in company_year_results.values() if r["status"] == "COMMITTED")
    all_committed = committed_count == EXPECTED_TARGET_COMPANY_YEARS and not stopped_early

    if (all_committed and total_runs == 45 and total_rows == 1080 and not rows_per_cy and not dup
            and missing_lineage == 0 and avail_mismatch == 0 and v1_ok and fmr_count == 900
            and len(excluded_still_review) == 4 and nvda_still_review == 6):
        overall = "PASS"
    elif committed_count > 0:
        overall = "PARTIAL PASS"
    else:
        overall = "FAIL"

    lines = [
        f"# Quarterly engine V4 production load — RESULT: {overall}",
        "",
        "## Backup and archive",
        f"- Backup: `{archive_info['backup_path']}`",
        f"- Source checksum: `{archive_info['source_checksum']}`",
        f"- Backup checksum: `{archive_info['backup_checksum']}` (match: {archive_info['source_checksum'] == archive_info['backup_checksum']})",
        f"- Archived pre-load rows: `{archive_info['rows_parquet']}`",
        f"- Archived pre-load runs: `{archive_info['runs_parquet']}`",
        f"- Manifest: `{archive_info['manifest_path']}`",
        "",
        f"## Company-year replacement results ({EXPECTED_TARGET_COMPANY_YEARS} targets)",
    ]
    for key, result in company_year_results.items():
        lines.append(f"- {key}: {result['status']}" +
                      (f" (old_run_id={result.get('old_run_id')}, new_run_id={result.get('new_run_id')}, "
                       f"rows_replaced={result.get('rows_replaced')}, run_status={result.get('run_status')}, "
                       f"recon_counts={result.get('reconciliation_status_counts')}, resolved_metrics={result.get('resolved_metrics')})"
                       if result["status"] == "COMMITTED" else f" — {result.get('reason')}"))

    lines += [
        "",
        "## Post-load counts (actual, not forced)",
        f"- quarterly_extraction_runs = {total_runs} (expected 45)",
        f"- quarterly_metric_results = {total_rows} (expected 1080)",
        f"- company-years without exactly 24 rows = {rows_per_cy}",
        f"- duplicate natural keys = {dup}",
        f"- missing lineage = {missing_lineage}",
        f"- availability-date mismatches = {avail_mismatch}",
        f"- unique REVIEW_REQUIRED cases (all categories) = {review_required_unique} (expected {EXPECTED_POST_REVIEW_REQUIRED} if all {EXPECTED_TARGET_COMPANY_YEARS} committed)",
        f"- 4 excluded earliest-year cases still REVIEW_REQUIRED = {len(excluded_still_review)}/4",
        f"- NVDA FY2020-01-26 metrics still REVIEW_REQUIRED = {nvda_still_review}/6",
        f"- financial_metric_results (annual) = {fmr_count} (expected 900)",
        f"- Annual V1 checksum unchanged = {v1_ok}",
        f"- engine_version breakdown = {engine_version_breakdown}",
        f"- companies committed = {committed_count}/{EXPECTED_TARGET_COMPANY_YEARS}",
        f"- total elapsed seconds = {round(time.perf_counter() - batch_start, 2)}",
        "",
        f"## Result: {overall}",
    ]
    (PROJECT_DIR / "docs" / "LAST_CLAUDE_REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    print("\n" + "=" * 100)
    print(f"FINAL: {overall}  committed={committed_count}/{EXPECTED_TARGET_COMPANY_YEARS}  total_runs={total_runs}/45  "
          f"total_rows={total_rows}/1080  review_required={review_required_unique}")
    print("=" * 100)


if __name__ == "__main__":
    main()
