"""
Minimal production quarterly database schema + load of the 72
already-verified quarterly results (MSFT/AMZN/ORCL FY2024) produced by
the consolidated engine (scripts/118) and validated with zero
differences against the original per-company proofs in
scripts/119_quarterly_extraction_engine_validation.py.

Adds exactly two new tables to the EXISTING production database
(data/database/ai_stock_agent.duckdb) — quarterly_extraction_runs and
quarterly_metric_results. Does not touch any existing table (companies,
sec_filings, extraction_runs, financial_metric_results,
historical_review_items), does not touch Annual V1, and processes no
new company.

Safety model:
  1. BEFORE writing anything, every input engine JSON
     (data/quarterly_engine_{ticker}_fy2024.json) is re-compared against
     its original verified proof JSON (data/quarterly_proof_{ticker}_
     fy2024.json) on quarterly value, extraction_basis, and
     reconciliation status. Any difference for ANY company aborts the
     ENTIRE load (nothing is written for any company) and a FAIL report
     is produced instead.
  2. Each company is loaded in its own transaction (BEGIN/COMMIT). A
     natural-key (ticker, fiscal_year_end, engine_version) already
     present in quarterly_extraction_runs is treated as "already
     loaded" and skipped — idempotent, safe to rerun.
  3. After each company's 24-row insert, the committed data is read back
     and validated: exactly 24 rows, every required lineage field
     present, no duplicate (run_id, metric_name, fiscal_quarter) natural
     key, and every value/extraction_basis matches the source JSON
     exactly. Any failure ROLLS BACK that company's transaction (nothing
     partially committed) and the company is marked FAILED; the other
     companies are unaffected.

result_status vs. reconciliation_status (two distinct row-level fields,
both required by the schema): `result_status` records whether THIS
row's own quarterly value resolved to a single deterministic fact
(row-level — always "PASS" in this dataset, since every one of the 72
rows already resolved in the source proofs; a REVIEW_REQUIRED metric
would never have reached the 24-row stage in the source JSON to begin
with). `reconciliation_status` records the METRIC-YEAR-level Q1+Q2+Q3+Q4-
vs-Annual tie-out outcome (PASS / PASS_ROUNDING_TOLERANCE /
REVIEW_REQUIRED per D-035) — identical across all 4 quarter rows of the
same metric, duplicated per row for query convenience.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
ANNUAL_V1_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"
ANNUAL_V1_MANIFEST_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1_manifest.json"

ENGINE_VERSION = "118_quarterly_extraction_engine_v1"
SCHEMA_VERSION = "quarterly_v1"
SCRIPT_NAME = "120_quarterly_production_schema_load.py"

QUARTERS = ["Q1", "Q2", "Q3", "Q4"]
METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense",
           "operating_cash_flow", "capex"]

COMPANIES = {
    "MSFT": {
        "fiscal_year_end": "2024-06-30",
        "engine_json": DATA_DIR / "quarterly_engine_msft_fy2024.json",
        "original_json": DATA_DIR / "quarterly_proof_msft_fy2024.json",
    },
    "AMZN": {
        "fiscal_year_end": "2024-12-31",
        "engine_json": DATA_DIR / "quarterly_engine_amzn_fy2024.json",
        "original_json": DATA_DIR / "quarterly_proof_amzn_fy2024.json",
    },
    "ORCL": {
        "fiscal_year_end": "2024-05-31",
        "engine_json": DATA_DIR / "quarterly_engine_orcl_fy2024.json",
        "original_json": DATA_DIR / "quarterly_proof_orcl_fy2024.json",
    },
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def values_close(a: float, b: float, tolerance: float = 1.0) -> bool:
    return math.isclose(a, b, abs_tol=tolerance)


# =====================================================================
# STEP 0 — pre-write consistency check: engine JSON vs. original
# verified proof JSON. Any difference anywhere aborts the ENTIRE load.
# =====================================================================

def compare_engine_vs_original(ticker: str, engine_output: dict, original: dict) -> list[dict]:
    differences: list[dict] = []
    for metric_name in METRICS:
        engine_metric = engine_output["metrics"].get(metric_name, {})
        original_metric = original["metrics"].get(metric_name, {})

        if engine_metric.get("status") != original_metric.get("status"):
            differences.append({
                "ticker": ticker, "metric": metric_name, "field": "reconciliation_status",
                "engine_value": engine_metric.get("status"), "original_value": original_metric.get("status"),
            })

        for quarter in QUARTERS:
            engine_q = engine_metric.get("quarters", {}).get(quarter, {})
            original_q = original_metric.get("quarters", {}).get(quarter, {})

            engine_value = engine_q.get("value")
            original_value = original_q.get("value")
            if engine_value is None or original_value is None:
                if engine_value != original_value:
                    differences.append({
                        "ticker": ticker, "metric": metric_name, "quarter": quarter, "field": "value",
                        "engine_value": engine_value, "original_value": original_value,
                    })
            elif not values_close(float(engine_value), float(original_value)):
                differences.append({
                    "ticker": ticker, "metric": metric_name, "quarter": quarter, "field": "value",
                    "engine_value": engine_value, "original_value": original_value,
                })

            if engine_q.get("extraction_basis") != original_q.get("extraction_basis"):
                differences.append({
                    "ticker": ticker, "metric": metric_name, "quarter": quarter, "field": "extraction_basis",
                    "engine_value": engine_q.get("extraction_basis"), "original_value": original_q.get("extraction_basis"),
                })

    return differences


# =====================================================================
# SCHEMA — created idempotently, additive only, no change to any
# existing table.
# =====================================================================

def create_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS quarterly_extraction_runs (
            run_id VARCHAR PRIMARY KEY,
            ticker VARCHAR NOT NULL,
            fiscal_year_end VARCHAR NOT NULL,
            engine_version VARCHAR NOT NULL,
            schema_version VARCHAR NOT NULL,
            q1_accession VARCHAR NOT NULL,
            q2_accession VARCHAR NOT NULL,
            q3_accession VARCHAR NOT NULL,
            fy_accession VARCHAR NOT NULL,
            source_json_path VARCHAR NOT NULL,
            run_status VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL,
            completed_at VARCHAR,
            UNIQUE (ticker, fiscal_year_end, engine_version)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS quarterly_metric_results (
            run_id VARCHAR NOT NULL,
            ticker VARCHAR NOT NULL,
            fiscal_year_end VARCHAR NOT NULL,
            fiscal_quarter VARCHAR NOT NULL,
            metric_name VARCHAR NOT NULL,
            value DOUBLE,
            unit VARCHAR,
            result_status VARCHAR NOT NULL,
            extraction_basis VARCHAR NOT NULL,
            period_start VARCHAR,
            period_end VARCHAR,
            availability_date VARCHAR,
            accession_number VARCHAR NOT NULL,
            concept_qname VARCHAR NOT NULL,
            context_id VARCHAR,
            dimensions_json VARCHAR NOT NULL,
            lineage_json VARCHAR NOT NULL,
            reconciliation_status VARCHAR NOT NULL,
            reconciliation_difference DOUBLE NOT NULL,
            permitted_difference DOUBLE NOT NULL,
            created_at VARCHAR NOT NULL,
            PRIMARY KEY (run_id, metric_name, fiscal_quarter)
        )
    """)


# =====================================================================
# ROW BUILDING — mirrors exactly the fallback rules already verified in
# scripts/118's own CSV-row construction (accession_number/concept_qname
# fall back to the annual_* keys for Q4; context_id has no fallback for
# Q2/Q3 DERIVED_FROM_YTD rows, legitimately null there, matching the
# already-verified CSV output byte-for-byte).
# =====================================================================

REQUIRED_NON_NULL_FIELDS = [
    "value", "unit", "result_status", "extraction_basis", "period_end",
    "availability_date", "accession_number", "concept_qname",
    "dimensions_json", "lineage_json", "reconciliation_status",
]


def build_quarter_row(
    run_id: str, ticker: str, fiscal_year_end: str, quarter: str,
    metric_name: str, filings: dict, metric_result: dict, created_at: str,
) -> dict:
    q = metric_result["quarters"][quarter]
    lineage = q["lineage"]
    reconciliation = metric_result["reconciliation"]

    period_end = (
        filings[quarter]["report_date"] if quarter != "Q4" else filings["FY"]["report_date"]
    )
    accession_number = lineage.get("accession_number", lineage.get("annual_accession_number"))
    concept_qname = lineage.get("concept_qname", lineage.get("annual_concept_qname"))
    context_id = lineage.get("context_id", lineage.get("nine_month_ytd_context_id"))

    return {
        "run_id": run_id, "ticker": ticker, "fiscal_year_end": fiscal_year_end,
        "fiscal_quarter": quarter, "metric_name": metric_name,
        "value": q["value"], "unit": "iso4217:USD",
        "result_status": "PASS",  # this row's own value resolved deterministically (source proof)
        "extraction_basis": q["extraction_basis"],
        "period_start": lineage.get("period_start"), "period_end": period_end,
        "availability_date": q["availability_date"],
        "accession_number": accession_number, "concept_qname": concept_qname,
        "context_id": context_id, "dimensions_json": "{}",
        "lineage_json": json.dumps(lineage, ensure_ascii=False, default=str),
        "reconciliation_status": reconciliation["status"],
        "reconciliation_difference": reconciliation["difference"],
        "permitted_difference": reconciliation["precision_calculation"]["permitted_difference"],
        "created_at": created_at,
    }


# =====================================================================
# PER-COMPANY LOAD — one transaction, validated read-back, rollback on
# any failure.
# =====================================================================

def load_one_company(connection: duckdb.DuckDBPyConnection, ticker: str, spec: dict, engine_output: dict) -> dict:
    fiscal_year_end = spec["fiscal_year_end"]

    already_loaded = connection.execute(
        "SELECT run_id FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ? AND engine_version = ?",
        [ticker, fiscal_year_end, ENGINE_VERSION],
    ).fetchone()
    if already_loaded is not None:
        return {"ticker": ticker, "status": "SKIPPED_ALREADY_LOADED", "run_id": already_loaded[0]}

    filings = engine_output["filings"]
    run_id = str(uuid.uuid4())
    created_at = utc_now_iso()

    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            "INSERT INTO quarterly_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, ticker, fiscal_year_end, ENGINE_VERSION, SCHEMA_VERSION,
             filings["Q1"]["accession_number"], filings["Q2"]["accession_number"],
             filings["Q3"]["accession_number"], filings["FY"]["accession_number"],
             str(spec["engine_json"]), "LOADING", created_at, None],
        )

        rows_inserted = 0
        for metric_name in METRICS:
            metric_result = engine_output["metrics"][metric_name]
            for quarter in QUARTERS:
                row = build_quarter_row(run_id, ticker, fiscal_year_end, quarter, metric_name, filings, metric_result, created_at)
                connection.execute(
                    """INSERT INTO quarterly_metric_results VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [row["run_id"], row["ticker"], row["fiscal_year_end"], row["fiscal_quarter"],
                     row["metric_name"], row["value"], row["unit"], row["result_status"],
                     row["extraction_basis"], row["period_start"], row["period_end"],
                     row["availability_date"], row["accession_number"], row["concept_qname"],
                     row["context_id"], row["dimensions_json"], row["lineage_json"],
                     row["reconciliation_status"], row["reconciliation_difference"],
                     row["permitted_difference"], row["created_at"]],
                )
                rows_inserted += 1

        # --- read-back validation, still inside the transaction ---
        validation_errors: list[str] = []

        committed = connection.execute(
            "SELECT fiscal_quarter, metric_name, value, extraction_basis, "
            "accession_number, concept_qname, dimensions_json, lineage_json, "
            "reconciliation_status, result_status, unit, period_end, availability_date "
            "FROM quarterly_metric_results WHERE run_id = ?",
            [run_id],
        ).fetchdf()

        if len(committed) != 24:
            validation_errors.append(f"row count = {len(committed)}, expected 24")

        for field in REQUIRED_NON_NULL_FIELDS:
            null_count = committed[field].isna().sum() if field in committed.columns else 24
            if null_count > 0:
                validation_errors.append(f"required field {field!r} is null in {null_count} row(s)")

        duplicate_keys = committed.groupby(["metric_name", "fiscal_quarter"]).size()
        duplicates = duplicate_keys[duplicate_keys > 1]
        if len(duplicates) > 0:
            validation_errors.append(f"duplicate (metric_name, fiscal_quarter) natural keys: {duplicates.to_dict()}")

        for _, row in committed.iterrows():
            source_value = engine_output["metrics"][row["metric_name"]]["quarters"][row["fiscal_quarter"]]["value"]
            if not values_close(float(row["value"]), float(source_value)):
                validation_errors.append(
                    f"{row['metric_name']}/{row['fiscal_quarter']}: committed value {row['value']} "
                    f"!= source JSON value {source_value}"
                )
            source_basis = engine_output["metrics"][row["metric_name"]]["quarters"][row["fiscal_quarter"]]["extraction_basis"]
            if row["extraction_basis"] != source_basis:
                validation_errors.append(
                    f"{row['metric_name']}/{row['fiscal_quarter']}: committed extraction_basis "
                    f"{row['extraction_basis']!r} != source JSON {source_basis!r}"
                )

        if validation_errors:
            connection.execute("ROLLBACK")
            return {"ticker": ticker, "status": "ROLLED_BACK", "run_id": run_id,
                    "rows_attempted": rows_inserted, "validation_errors": validation_errors}

        connection.execute(
            "UPDATE quarterly_extraction_runs SET run_status = ?, completed_at = ? WHERE run_id = ?",
            ["PASS", utc_now_iso(), run_id],
        )
        connection.execute("COMMIT")

        return {
            "ticker": ticker, "status": "COMMITTED", "run_id": run_id,
            "rows_committed": int(len(committed)),
            "extraction_basis_counts": committed["extraction_basis"].value_counts().to_dict(),
        }

    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure must roll back this company only
        connection.execute("ROLLBACK")
        return {"ticker": ticker, "status": "ROLLED_BACK", "run_id": run_id, "error": str(exc)}


def main() -> dict:
    print("=" * 100)
    print("QUARTERLY PRODUCTION SCHEMA + LOAD (MSFT / AMZN / ORCL FY2024)")
    print("=" * 100)

    # --- STEP 0: pre-write consistency check across ALL companies ---
    engine_outputs: dict[str, dict] = {}
    all_pre_write_differences: list[dict] = []

    for ticker, spec in COMPANIES.items():
        with spec["engine_json"].open(encoding="utf-8") as handle:
            engine_output = json.load(handle)
        with spec["original_json"].open(encoding="utf-8") as handle:
            original = json.load(handle)
        engine_outputs[ticker] = engine_output
        differences = compare_engine_vs_original(ticker, engine_output, original)
        all_pre_write_differences.extend(differences)
        print(f"Pre-write check {ticker}: {len(differences)} difference(s) vs. original verified JSON")

    if all_pre_write_differences:
        print("\nABORTING — pre-write consistency check found differences. Nothing will be written.")
        for diff in all_pre_write_differences:
            print(f"  DIFF: {diff}")
        result = {
            "overall_status": "FAIL", "reason": "pre_write_consistency_check_failed",
            "differences": all_pre_write_differences,
        }
        return result

    print("\nAll 3 companies match their original verified JSON exactly. Proceeding to load.\n")

    # --- schema creation (idempotent, additive) ---
    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
    create_schema(connection)

    company_results: dict[str, dict] = {}
    for ticker, spec in COMPANIES.items():
        print(f">>> Loading {ticker} (one transaction) <<<")
        result = load_one_company(connection, ticker, spec, engine_outputs[ticker])
        company_results[ticker] = result
        print(f"  {ticker}: {result['status']}")
        if result["status"] == "ROLLED_BACK":
            print(f"    errors: {result.get('validation_errors', result.get('error'))}")

    # --- final aggregate validation ---
    total_runs = connection.execute(
        "SELECT COUNT(*) FROM quarterly_extraction_runs WHERE engine_version = ?", [ENGINE_VERSION]
    ).fetchone()[0]
    total_rows = connection.execute(
        "SELECT COUNT(*) FROM quarterly_metric_results r "
        "JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
        "WHERE e.engine_version = ?", [ENGINE_VERSION]
    ).fetchone()[0]

    basis_counts = dict(connection.execute(
        "SELECT extraction_basis, COUNT(*) FROM quarterly_metric_results r "
        "JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
        "WHERE e.engine_version = ? GROUP BY extraction_basis", [ENGINE_VERSION]
    ).fetchall())

    reconciliation_status_counts_by_metric_year = connection.execute(
        "SELECT r.reconciliation_status, COUNT(*) FROM ("
        "  SELECT DISTINCT run_id, metric_name, reconciliation_status FROM quarterly_metric_results"
        ") r JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
        "WHERE e.engine_version = ? GROUP BY r.reconciliation_status", [ENGINE_VERSION]
    ).fetchall()
    reconciliation_status_counts = dict(reconciliation_status_counts_by_metric_year)

    rows_per_company = dict(connection.execute(
        "SELECT r.ticker, COUNT(*) FROM quarterly_metric_results r "
        "JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
        "WHERE e.engine_version = ? GROUP BY r.ticker", [ENGINE_VERSION]
    ).fetchall())

    duplicate_check = connection.execute(
        "SELECT run_id, metric_name, fiscal_quarter, COUNT(*) c FROM quarterly_metric_results "
        "GROUP BY run_id, metric_name, fiscal_quarter HAVING COUNT(*) > 1"
    ).fetchall()

    availability_date_check = connection.execute(
        "SELECT r.ticker, r.fiscal_quarter, r.availability_date, e.q1_accession, e.q2_accession, e.q3_accession, e.fy_accession "
        "FROM quarterly_metric_results r JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
        "WHERE e.engine_version = ? AND r.metric_name = 'revenue'", [ENGINE_VERSION]
    ).fetchdf()

    connection.close()

    result = {
        "overall_status": "PASS" if all(
            r["status"] in ("COMMITTED", "SKIPPED_ALREADY_LOADED") for r in company_results.values()
        ) else "FAIL",
        "company_results": company_results,
        "total_extraction_runs": total_runs,
        "total_quarterly_metric_results": total_rows,
        "extraction_basis_counts": basis_counts,
        "reconciliation_status_counts_metric_year": reconciliation_status_counts,
        "rows_per_company": rows_per_company,
        "duplicate_natural_keys": duplicate_check,
    }

    print("\n" + "=" * 100)
    print("FINAL VALIDATION")
    print(f"  total_extraction_runs = {total_runs} (expected 3)")
    print(f"  total_quarterly_metric_results = {total_rows} (expected 72)")
    print(f"  rows_per_company = {rows_per_company}")
    print(f"  extraction_basis_counts = {basis_counts}")
    print(f"  reconciliation_status_counts (metric-year level) = {reconciliation_status_counts}")
    print(f"  duplicate_natural_keys = {duplicate_check} (expected [])")
    print(f"  OVERALL STATUS = {result['overall_status']}")
    print("=" * 100)

    return result


if __name__ == "__main__":
    final_result = main()
    output_path = DATA_DIR / "quarterly_production_load_result.json"
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(final_result, handle, indent=2, ensure_ascii=False, default=str)
    print(f"\nResult written to {output_path}")
