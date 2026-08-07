"""
Loads the already-validated scripts/132 (engine v3, duration-tolerance:
88-day quarter / 180-day six-month-YTD minimums) results into the active
quarterly production tables for exactly the company-years whose results
changed under v3, per data/quarterly_duration_v3_validation.json (the
output of scripts/133, NOT re-run here).

Three phases, same discipline as scripts/130 (D-037 production load):
  1. Read-only pre-write validation against the SAVED v3 validation JSON
     (fail-closed; writes nothing on any inconsistency). The saved
     validation JSON and CSV do not carry full per-row lineage (concept,
     context, decimals) needed for a production write, so this phase
     also invokes scripts/132's engine function directly (NOT
     scripts/133, NOT the 45-company regression) for exactly the derived
     target company-years, and cross-checks every one of the 6 metrics'
     resulting status against the value already recorded for that exact
     case in the saved validation JSON's case_rows — a fresh, independent
     reproduction check, not a re-validation.
  2. Full DB backup + Parquet archive of the exact rows being replaced
     (checksum-verified before any write).
  3. One DuckDB transaction per company-year: delete the old run + 24
     rows, insert the new (engine-v3) run + 24 rows, validate before
     commit. Any single company-year's failure rolls back that
     company-year AND stops the entire task (no "continue to the next").

Does not modify scripts/128/130/132/133 or any earlier script. Does not
touch the XBRL warehouse, locked filings, or any annual table.
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

VALIDATION_JSON_PATH = DATA_DIR / "quarterly_duration_v3_validation.json"
LOAD_RESULT_PATH = DATA_DIR / "quarterly_engine_v3_production_load_result.json"

ENGINE_VERSION_V3 = "QUARTERLY_ENGINE_V3_DURATION_TOLERANCE"
SCHEMA_VERSION = "quarterly_v1"

EXPECTED_ANNUAL_V1_CHECKSUM = "e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814"
EXPECTED_PRE_RUNS = 45
EXPECTED_PRE_ROWS = 1080
EXPECTED_PRE_FMR = 900
EXPECTED_PRE_REVIEW_REQUIRED = 57

EXPECTED_TOTAL_CASES = 270
EXPECTED_RESOLVED_DURATION_CASES = 36
EXPECTED_REMAINING_AUDITED = 2
EXPECTED_PANW_BASIS_ONLY = 10
EXPECTED_OTHER_FINDINGS = 18
EXPECTED_TARGET_COMPANY_YEARS = 15
EXPECTED_POST_REVIEW_REQUIRED = 21

METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense", "operating_cash_flow", "capex"]
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]

RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_module(filename: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, PROJECT_DIR / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


s132 = _load_module("132_quarterly_extraction_engine_v3_duration_tolerance.py", "s132_v3_engine")


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_fail_report(reason: str, details: dict) -> None:
    report = f"""# Quarterly engine V3 duration validation — production load — RESULT: FAIL (pre-write validation)

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
# PHASE 1 — PRE-WRITE VALIDATION (mostly read-only; also regenerates the
# full-lineage v3 proof JSON for exactly the derived target company-years
# by calling scripts/132's engine function directly — NOT scripts/133,
# NOT the 45-company regression)
# =====================================================================

def phase1_pre_write_validation() -> dict:
    print("=" * 100)
    print("PHASE 1 — PRE-WRITE VALIDATION (against the SAVED scripts/133 validation output)")
    print("=" * 100)

    with VALIDATION_JSON_PATH.open(encoding="utf-8") as handle:
        validation = json.load(handle)

    v2 = validation["validation_2"]
    if v2["total_quarterly_rows"] != 1080 or not v2["row_count_ok"]:
        raise RuntimeError(f"Saved validation total_quarterly_rows={v2['total_quarterly_rows']}, row_count_ok={v2['row_count_ok']} — expected 1080/True.")

    case_rows = validation["case_rows"]
    if len(case_rows) != EXPECTED_TOTAL_CASES:
        raise RuntimeError(f"Saved validation has {len(case_rows)} case_rows, expected {EXPECTED_TOTAL_CASES}.")

    resolved_cases = [c for c in case_rows if c["classification"] == "EXPECTED_RESOLUTION_OF_AUDITED_CASE"]
    if len(resolved_cases) != EXPECTED_RESOLVED_DURATION_CASES:
        raise RuntimeError(f"Found {len(resolved_cases)} EXPECTED_RESOLUTION_OF_AUDITED_CASE cases, expected {EXPECTED_RESOLVED_DURATION_CASES}.")

    still_review = v2["audited_38_cases_still_review_required"]
    if len(still_review) != EXPECTED_REMAINING_AUDITED:
        raise RuntimeError(f"Found {len(still_review)} still-REVIEW_REQUIRED audited cases, expected {EXPECTED_REMAINING_AUDITED}.")
    if not all("CRWD" in c["case"] and "pretax_income" in c["case"] for c in still_review):
        raise RuntimeError(f"Remaining audited REVIEW_REQUIRED cases are not the expected CRWD pretax_income pair: {still_review}")

    other_findings = v2["other_unexpected_findings"]
    if len(other_findings) != EXPECTED_OTHER_FINDINGS:
        raise RuntimeError(f"Found {len(other_findings)} other_unexpected_findings, expected {EXPECTED_OTHER_FINDINGS}.")

    panw_basis_only = [f for f in other_findings if f["type"] == "UNEXPECTED_CHANGE_TO_ALREADY_RESOLVED_QUARTER"]
    crwd_cascade = [f for f in other_findings if f["type"] == "180_DAY_CASE_NOT_GOOGL_OR_META"]
    if len(panw_basis_only) != EXPECTED_PANW_BASIS_ONLY:
        raise RuntimeError(f"Found {len(panw_basis_only)} PANW basis-only findings, expected {EXPECTED_PANW_BASIS_ONLY}.")
    if len(panw_basis_only) + len(crwd_cascade) != len(other_findings):
        raise RuntimeError("Every one of the 18 other_unexpected_findings must be classified as either the "
                            "approved PANW basis-only pattern or the CRWD same-case Q2 cascade pattern — found an unexplained finding.")
    if not all(f["row"]["ticker"] == "PANW" for f in panw_basis_only):
        raise RuntimeError("Not all PANW basis-only findings have ticker=PANW.")
    if not all(f["row"]["value_changed"] is False for f in panw_basis_only):
        raise RuntimeError("At least one PANW basis-only finding does not have an identical (unchanged) value — refusing to treat it as approved.")
    if not all(f["row"]["is_audited_38_case"] and f["row"]["newly_resolved"] for f in crwd_cascade):
        raise RuntimeError("At least one CRWD cascade finding is not a newly-resolved row within an already-audited case.")

    if v2["regressions_found"] != 0 or len(v2["regression_findings"]) != 0:
        raise RuntimeError(f"Saved validation reports {v2['regressions_found']} regressions — refusing to load on top of a regression.")

    print(f"Saved validation confirmed: 1080 rows / 270 cases compared; "
          f"{len(resolved_cases)} resolved duration cases; {len(still_review)} remain REVIEW_REQUIRED; "
          f"{len(panw_basis_only)} approved PANW basis-only changes; {len(crwd_cascade)} explained same-case cascades; "
          f"0 regressions.")

    # --- derive target company-years (not hardcoded) ---
    target_from_resolved = {(c["ticker"], c["fiscal_year_end"]) for c in resolved_cases}
    target_from_panw = {(f["row"]["ticker"], f["row"]["fiscal_year_end"]) for f in panw_basis_only}
    target_company_years = sorted(target_from_resolved | target_from_panw)

    if len(target_company_years) != EXPECTED_TARGET_COMPANY_YEARS:
        raise RuntimeError(f"Derived {len(target_company_years)} unique target company-years, expected {EXPECTED_TARGET_COMPANY_YEARS}.\n"
                            f"Derived: {target_company_years}")

    by_ticker: dict[str, int] = {}
    for t, _fy in target_company_years:
        by_ticker[t] = by_ticker.get(t, 0) + 1
    print(f"\nDerived {len(target_company_years)} target company-years, by ticker: {by_ticker}")
    for cy in target_company_years:
        print(f"  {cy}")

    # cases (of the 270) belonging to each target company-year, for later status cross-checks
    cases_by_company_year: dict[tuple, dict[str, dict]] = {}
    for c in case_rows:
        cases_by_company_year.setdefault((c["ticker"], c["fiscal_year_end"]), {})[c["metric_name"]] = c

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

        existing_rows = connection.execute(
            "SELECT metric_name, fiscal_quarter, value, extraction_basis FROM quarterly_metric_results WHERE run_id = ?",
            [old_run_id],
        ).fetchall()
        existing_by_key = {(m, q): (v, b) for m, q, v, b in existing_rows}

        # --- regenerate the full-lineage v3 output for this company-year via
        # scripts/132's engine directly (NOT scripts/133) ---
        v3_json_path = DATA_DIR / f"quarterly_engine_v3_{ticker.lower()}_fy{fiscal_year_end[:4]}.json"
        v3_csv_path = DATA_DIR / f"quarterly_engine_v3_{ticker.lower()}_fy{fiscal_year_end[:4]}.csv"
        v3_output = s132.run_quarterly_extraction_engine_v3(
            ticker=ticker, fiscal_year_end=fiscal_year_end,
            q1_accession=q1_acc, q2_accession=q2_acc, q3_accession=q3_acc, fy_accession=fy_acc,
            json_output_path=v3_json_path, csv_output_path=v3_csv_path,
        )
        if len(v3_output["metrics"]) != 6:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: freshly-generated v3 output has {len(v3_output['metrics'])} metrics, expected 6.")

        # cross-check every one of the 6 metrics' status against the SAVED
        # validation JSON's case_rows for this exact company-year (an
        # independent reproduction check, not a re-validation)
        saved_cases = cases_by_company_year.get((ticker, fiscal_year_end), {})
        if len(saved_cases) != 6:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: saved validation has {len(saved_cases)} cases, expected 6.")
        status_mismatches = []
        for metric_name in METRICS:
            fresh_status = v3_output["metrics"][metric_name].get("status")
            saved_status = saved_cases[metric_name]["v3_status"]
            if fresh_status != saved_status:
                status_mismatches.append(f"{metric_name}: freshly-generated status={fresh_status!r} != saved validation v3_status={saved_status!r}")
        if status_mismatches:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: freshly-generated v3 engine output does not reproduce the saved validation "
                                f"(environment drift or non-determinism — refusing to load):\n" + "\n".join(status_mismatches))

        # PANW-specific extra safety: every approved basis-only quarter must
        # have a value identical to what is CURRENTLY in production right now
        panw_value_mismatches = []
        if ticker == "PANW":
            for metric_name in METRICS:
                q3_new = v3_output["metrics"][metric_name].get("quarters", {}).get("Q3")
                existing = existing_by_key.get((metric_name, "Q3"))
                if q3_new is not None and existing is not None and existing[0] is not None:
                    if not values_close(existing[0], q3_new["value"]):
                        panw_value_mismatches.append(f"{metric_name}/Q3: existing production value={existing[0]} != fresh v3 value={q3_new['value']}")
        if panw_value_mismatches:
            connection.close()
            raise RuntimeError(f"{ticker} {fiscal_year_end}: PANW basis-only change does not have an identical value:\n" + "\n".join(panw_value_mismatches))

        per_cy_detail[(ticker, fiscal_year_end)] = {
            "old_run_id": old_run_id, "old_engine_version": old_engine_version,
            "v3_json_path": str(v3_json_path),
        }
        print(f"  {ticker} {fiscal_year_end}: OK (old_run_id={old_run_id}, old_engine_version={old_engine_version}, "
              f"statuses reproduced exactly)")

    connection.close()
    print("\nPHASE 1: ALL CHECKS PASSED.")
    return {
        "target_company_years": target_company_years, "per_cy_detail": per_cy_detail,
        "pre_counts": {"runs": total_runs, "rows": total_rows, "fmr": fmr_count, "review_required": review_required_unique},
        "resolved_duration_cases": len(resolved_cases), "remaining_audited": len(still_review),
        "panw_basis_only_count": len(panw_basis_only), "crwd_cascade_count": len(crwd_cascade),
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

    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_quarterly_engine_v3_load_{RUN_TIMESTAMP}.duckdb"
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
    rows_parquet = ARCHIVE_DIR / f"quarterly_engine_v3_rows_replaced_{RUN_TIMESTAMP}.parquet"
    prod_connection.execute(f"COPY (SELECT * FROM rows_to_archive) TO '{rows_parquet.as_posix()}' (FORMAT PARQUET)")
    prod_connection.unregister("rows_to_archive")

    runs_df = prod_connection.execute(
        f"SELECT * FROM quarterly_extraction_runs WHERE run_id IN ({placeholders})", run_ids
    ).fetchdf()
    prod_connection.register("runs_to_archive", runs_df)
    runs_parquet = ARCHIVE_DIR / f"quarterly_engine_v3_runs_replaced_{RUN_TIMESTAMP}.parquet"
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

    manifest_path = ARCHIVE_DIR / f"quarterly_engine_v3_load_manifest_{RUN_TIMESTAMP}.json"
    manifest = {
        "timestamp_utc": RUN_TIMESTAMP, "production_db_path": str(PRODUCTION_DB_PATH.resolve()),
        "backup_db_path": str(backup_path.resolve()), "source_checksum": source_checksum, "backup_checksum": backup_checksum,
        "backup_counts": backup_counts, "target_company_years": [{"ticker": t, "fiscal_year_end": f} for t, f in target_company_years],
        "old_run_ids": run_ids, "rows_archived": reread_rows_count, "runs_archived": reread_runs_count,
        "rows_parquet": str(rows_parquet.resolve()), "runs_parquet": str(runs_parquet.resolve()),
        "engine_version_v3": ENGINE_VERSION_V3, "resolved_duration_cases": audit["resolved_duration_cases"],
        "panw_basis_only_count": audit["panw_basis_only_count"], "remaining_audited": audit["remaining_audited"],
        "validation_source": str(VALIDATION_JSON_PATH.resolve()),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Manifest: {manifest_path}")

    return {"backup_path": str(backup_path), "source_checksum": source_checksum, "backup_checksum": backup_checksum,
            "rows_parquet": str(rows_parquet), "runs_parquet": str(runs_parquet), "manifest_path": str(manifest_path)}


# =====================================================================
# ROW BUILDING — same logic as scripts/123/124/126/130's REVIEW_REQUIRED-
# tolerant loader (copied, not imported, per this project's established pattern)
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

def replace_one_company_year(connection, ticker: str, fiscal_year_end: str, old_run_id: str, v3_json_path: str) -> dict:
    cy_start = time.perf_counter()

    with Path(v3_json_path).open(encoding="utf-8") as handle:
        v3_output = json.load(handle)
    filings = v3_output["filings"]

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
    has_any_review_required = any(len(v3_output["metrics"].get(m, {}).get("quarters", {})) != 4 for m in METRICS)
    run_status = "PASS_WITH_REVIEW_REQUIRED" if has_any_review_required else "PASS"

    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute("DELETE FROM quarterly_metric_results WHERE run_id = ?", [old_run_id])
        connection.execute("DELETE FROM quarterly_extraction_runs WHERE run_id = ?", [old_run_id])

        connection.execute(
            "INSERT INTO quarterly_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [new_run_id, ticker, fiscal_year_end, ENGINE_VERSION_V3, SCHEMA_VERSION,
             filings["Q1"]["accession_number"], filings["Q2"]["accession_number"],
             filings["Q3"]["accession_number"], filings["FY"]["accession_number"],
             str(v3_json_path), "LOADING", created_at, None],
        )

        for metric_name in METRICS:
            metric_result = v3_output["metrics"][metric_name]
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
            source_metric = v3_output["metrics"][row["metric_name"]]
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
            source_metric = v3_output["metrics"][row["metric_name"]]
            if row["fiscal_quarter"] in source_metric.get("quarters", {}):
                expected_end = filings[row["fiscal_quarter"]]["report_date"]
                lineage = json.loads(row["lineage_json"])
                actual_end = lineage.get("period_end")
                if actual_end and actual_end != expected_end:
                    errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: period_end {actual_end} != filing report_date {expected_end} "
                                   f"(possible comparative prior-year fact)")

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

        result = replace_one_company_year(connection, ticker, fiscal_year_end, detail["old_run_id"], detail["v3_json_path"])
        result["resolved_case_count"] = audit["resolved_duration_cases"]
        result["basis_only_change_count"] = audit["panw_basis_only_count"]
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
    crwd_pretax_still_review = connection.execute(
        "SELECT ticker, fiscal_year_end FROM quarterly_metric_results "
        "WHERE ticker='CRWD' AND metric_name='pretax_income' AND reconciliation_status='REVIEW_REQUIRED' "
        "GROUP BY ticker, fiscal_year_end"
    ).fetchall()
    fmr_count = connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    non_target_engine_versions = connection.execute(
        "SELECT engine_version, COUNT(*) FROM quarterly_extraction_runs GROUP BY engine_version"
    ).fetchall()
    connection.close()

    actual_v1_checksum = sha256_of_file(ANNUAL_V1_DB_PATH)
    v1_ok = actual_v1_checksum == EXPECTED_ANNUAL_V1_CHECKSUM

    committed_count = sum(1 for r in company_year_results.values() if r["status"] == "COMMITTED")
    all_committed = committed_count == EXPECTED_TARGET_COMPANY_YEARS and not stopped_early

    if all_committed and total_runs == 45 and total_rows == 1080 and not rows_per_cy and not dup and missing_lineage == 0 and avail_mismatch == 0 and v1_ok and fmr_count == 900:
        overall = "PASS"
    elif committed_count > 0:
        overall = "PARTIAL PASS"
    else:
        overall = "FAIL"

    lines = [
        f"# Quarterly engine V3 duration validation — production load — RESULT: {overall}",
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
                       f"recon_counts={result.get('reconciliation_status_counts')})"
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
        f"- unique REVIEW_REQUIRED cases (all categories) = {review_required_unique} (task expects {EXPECTED_POST_REVIEW_REQUIRED} if all {EXPECTED_TARGET_COMPANY_YEARS} committed)",
        f"- CRWD pretax_income still REVIEW_REQUIRED = {crwd_pretax_still_review} (expected [('CRWD','2022-01-31'),('CRWD','2023-01-31')])",
        f"- financial_metric_results (annual) = {fmr_count} (expected 900)",
        f"- Annual V1 checksum unchanged = {v1_ok}",
        f"- engine_version breakdown = {non_target_engine_versions}",
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
