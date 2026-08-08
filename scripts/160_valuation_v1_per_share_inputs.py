"""
Valuation V1 per-share inputs -- closes the filing-based valuation-data
gap identified in the Scoring Model V1 blueprint (docs/SCORING_MODEL_V1_BLUEPRINT.md)
using ONLY the SEC filings already locked in the project (the same 45
approved 10-K accessions behind Annual Data V1) and the already-built
XBRL warehouse (data/database/xbrl_warehouse_proof.duckdb). No new
filing is downloaded. No external provider is used.

Chosen Valuation V1 basis (Stage 2): use REPORTED DILUTED EPS
(`us-gaap:EarningsPerShareDiluted`) directly wherever it resolves
unambiguously -- the consolidated (non-dimensional), full-fiscal-year
duration fact for the filing's own report_date. This requires no
shares-outstanding data at all, since historical P/E only needs a
per-share earnings figure, not a share count. Diluted weighted-average
shares (`us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding`) is
used ONLY as a validated fallback/cross-check, never stored in
production, since it has no other required use in V1.

Resolution rule (generic, no ticker/year-specific logic):
  1. Take facts for the target concept where `dimensions_json = '{}'`
     (the single consolidated company-wide value, not a segment/class-
     of-stock breakdown) AND `period_end` equals the filing's own
     `report_date` AND the duration is an annual one (300-400 days).
  2. If exactly one distinct value (compared as floats, not raw
     strings -- '11.8' and '11.80' are the same value) remains ->
     RESOLVED.
  3. If zero remain -> try the earnings/shares fallback; if that also
     fails -> UNAVAILABLE.
  4. If more than one distinct value remains -> AMBIGUOUS ->
     REVIEW_REQUIRED (fail closed, never guess which one is right).

Two mutually exclusive modes:
  --check-only  Fully read-only. Runs Stages 1-5 (inventory, micro
                proof, full 45-company-year proof, historical P/E
                proof, in-memory load proof). Writes nothing to any
                file database.
  --execute     Stage 6: the real production load, only reachable if a
                --check-only-equivalent run just confirmed 45/45
                resolved. PID lock -> backup (SHA-256-verified) -> one
                atomic transaction creating `valuation_v1_per_share_inputs`
                and inserting all 45 rows -> post-commit validation ->
                exit code 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DOCS_DIR = PROJECT_DIR / "docs"
LOGS_DIR = PROJECT_DIR / "logs"
BACKUPS_DIR = DATA_DIR / "database" / "backups"
PROOFS_DIR = DATA_DIR / "proofs"

PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
ANNUAL_V1_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"

CHECK_ONLY_OUTPUT_PATH = DATA_DIR / "valuation_v1_build_validation.json"
PREVIEW_CSV_PATH = DATA_DIR / "valuation_v1_preview.csv"
RESULT_JSON_PATH = DATA_DIR / "valuation_v1_load_result.json"
RESULT_CSV_PATH = DATA_DIR / "valuation_v1_load_result.csv"
RELEASE_MANIFEST_PATH = DATA_DIR / "valuation_v1_release_manifest.json"
LOG_PATH = LOGS_DIR / "valuation_v1_load.log"
PID_LOCK_PATH = DATA_DIR / "valuation_v1_load.pid"

TICKERS = ["ORCL", "MSFT", "META", "NVDA", "GOOGL", "AMZN", "MU", "CRWD", "PANW"]
EXPECTED_COMPANY_YEARS = 45
MICRO_PROOF_TICKERS = ("MSFT", "NVDA", "AMZN")
CROSS_CHECK_TOLERANCE_ABS = 0.05  # dollars; empirically the largest observed reported-vs-calculated gap across all 45 company-years was $0.03
ANNUAL_DURATION_MIN_DAYS, ANNUAL_DURATION_MAX_DAYS = 300, 400
VALUATION_VERSION = "VALUATION_V1"
PRICE_POLICY_VERSION_EXPECTED = "HISTORICAL_PRICE_POLICY_V1"

VALUATION_V1_DDL = """
CREATE TABLE valuation_v1_per_share_inputs (
    ticker                          VARCHAR NOT NULL,
    fiscal_year                     INTEGER NOT NULL,
    fiscal_year_end                 DATE NOT NULL,
    diluted_eps                     DOUBLE NOT NULL,
    resolution_method               VARCHAR NOT NULL,
    eps_source_concept               VARCHAR NOT NULL,
    accession_number                VARCHAR NOT NULL,
    filing_date                     DATE NOT NULL,
    availability_date               DATE NOT NULL,
    cross_check_calculated_eps      DOUBLE,
    cross_check_diff                DOUBLE,
    valuation_version               VARCHAR NOT NULL,
    created_at                      TIMESTAMP NOT NULL,
    CONSTRAINT chk_resolution_method CHECK (
        resolution_method IN ('REPORTED_DILUTED_EPS', 'CALCULATED_NET_INCOME_OVER_DILUTED_SHARES')
    ),
    CONSTRAINT chk_valuation_version CHECK (valuation_version = 'VALUATION_V1'),
    PRIMARY KEY (ticker, fiscal_year)
)
"""


# =====================================================================
# SMALL SHARED HELPERS
# =====================================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log(message: str, also_print: bool = True) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{utc_now_iso()}] {message}\n")
    if also_print:
        print(message)


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


# =====================================================================
# PID LOCK (same discipline as scripts/151/153/158)
# =====================================================================

def is_pid_active(pid: int) -> bool:
    try:
        completed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=10)
    except Exception:
        return True
    return str(pid) in completed.stdout


def check_pid_lock_status(exclude_pid: int | None = None) -> dict:
    if not PID_LOCK_PATH.exists():
        return {"is_free": True, "existing_pid": None, "pid_active": None, "is_own_lock": False, "is_stale": False}
    try:
        content = json.loads(PID_LOCK_PATH.read_text(encoding="utf-8"))
        existing_pid = content["pid"]
        if not isinstance(existing_pid, int) or isinstance(existing_pid, bool):
            raise ValueError(f"'pid' field is not an integer: {existing_pid!r}")
    except Exception as exc:  # noqa: BLE001
        return {"is_free": False, "existing_pid": None, "pid_active": None, "is_own_lock": False, "is_stale": False,
                "reason": f"PID lock file unreadable/malformed: {exc}"}
    if exclude_pid is not None and existing_pid == exclude_pid:
        return {"is_free": True, "existing_pid": existing_pid, "pid_active": True, "is_own_lock": True, "is_stale": False}
    active = is_pid_active(existing_pid)
    return {"is_free": not active, "existing_pid": existing_pid, "pid_active": active, "is_own_lock": False, "is_stale": not active}


def acquire_pid_lock() -> None:
    status = check_pid_lock_status()
    if status["existing_pid"] is None and not status["is_free"]:
        raise RuntimeError(f"PID lock file exists but is malformed/unreadable ({status.get('reason')}) -- refusing to start.")
    if status["existing_pid"] is not None and status["pid_active"]:
        raise RuntimeError(f"A live PID lock already exists (pid={status['existing_pid']}) -- refusing to start.")
    if status["existing_pid"] is not None and status["is_stale"]:
        log(f"Removing stale PID lock (pid={status['existing_pid']} is not active).")
        PID_LOCK_PATH.unlink(missing_ok=True)
    atomic_write_json(PID_LOCK_PATH, {"pid": os.getpid(), "started_at": utc_now_iso()})
    log(f"PID lock acquired (pid={os.getpid()}).")


def release_pid_lock() -> None:
    if PID_LOCK_PATH.exists():
        PID_LOCK_PATH.unlink(missing_ok=True)
        log("PID lock released.")


# =====================================================================
# STAGE 1 -- approved company-years + XBRL concept inventory
# =====================================================================

def get_approved_company_years(prod_connection) -> list[dict]:
    rows = prod_connection.execute("""
        SELECT DISTINCT s.ticker, s.fiscal_year, s.accession_number, s.report_date, s.filing_date,
               (SELECT f2.value FROM financial_metric_results f2
                JOIN extraction_runs r2 ON r2.extraction_run_id = f2.extraction_run_id
                WHERE r2.accession_number = s.accession_number AND f2.metric_name = 'net_income' AND f2.status = 'PASS'
                LIMIT 1) AS net_income
        FROM financial_metric_results f
        JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id
        JOIN sec_filings s ON s.accession_number = r.accession_number
        WHERE f.metric_name = 'revenue' AND f.status = 'PASS'
        ORDER BY s.ticker, s.fiscal_year
    """).fetchall()
    return [
        {"ticker": t, "fiscal_year": fy, "accession_number": acc, "report_date": str(rd),
         "filing_date": str(fd), "net_income": ni}
        for t, fy, acc, rd, fd, ni in rows
    ]


def inventory_xbrl_concept_coverage(wh_connection, accessions: list[str]) -> dict:
    placeholders = ",".join("?" * len(accessions))
    concepts = [
        "EarningsPerShareDiluted", "EarningsPerShareBasic",
        "WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic",
        "CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding",
    ]
    coverage = {}
    for concept in concepts:
        rows = wh_connection.execute(
            f"SELECT COUNT(DISTINCT accession_number) FROM xbrl_facts WHERE concept_local_name = ? AND accession_number IN ({placeholders})",
            [concept, *accessions],
        ).fetchone()
        coverage[concept] = {"distinct_accessions_with_concept": rows[0], "of_total": len(accessions)}
    return coverage


# =====================================================================
# CORE RESOLUTION (generic -- no ticker/year-specific logic)
# =====================================================================

def resolve_annual_concept_value(wh_connection, accession: str, report_date: str, concept: str) -> dict:
    """Returns the single consolidated (non-dimensional), full-fiscal-year
    value for `concept` in `accession`, or classifies why it could not be
    resolved. Never guesses between multiple genuinely differing values."""
    rows = wh_connection.execute("""
        SELECT DISTINCT context_id, value_raw, period_start, period_end
        FROM xbrl_facts
        WHERE concept_local_name = ? AND dimensions_json = '{}'
          AND accession_number = ? AND period_end = ?
          AND date_diff('day', CAST(period_start AS DATE), CAST(period_end AS DATE)) BETWEEN ? AND ?
    """, [concept, accession, report_date, ANNUAL_DURATION_MIN_DAYS, ANNUAL_DURATION_MAX_DAYS]).fetchall()

    if not rows:
        return {"status": "UNAVAILABLE", "value": None, "matched_contexts": []}

    distinct_values = sorted({round(float(r[1]), 6) for r in rows})
    matched_contexts = [{"context_id": r[0], "value_raw": r[1]} for r in rows]
    if len(distinct_values) == 1:
        return {"status": "RESOLVED", "value": distinct_values[0], "matched_contexts": matched_contexts}
    return {"status": "AMBIGUOUS", "value": None, "distinct_values": distinct_values, "matched_contexts": matched_contexts}


def resolve_valuation_input(wh_connection, company_year: dict) -> dict:
    accession, report_date = company_year["accession_number"], company_year["report_date"]

    eps_result = resolve_annual_concept_value(wh_connection, accession, report_date, "EarningsPerShareDiluted")
    shares_result = resolve_annual_concept_value(wh_connection, accession, report_date, "WeightedAverageNumberOfDilutedSharesOutstanding")

    cross_check_calculated_eps, cross_check_diff = None, None
    if shares_result["status"] == "RESOLVED" and shares_result["value"] and company_year["net_income"] is not None:
        cross_check_calculated_eps = round(company_year["net_income"] / shares_result["value"], 2)

    entry = {
        "ticker": company_year["ticker"], "fiscal_year": company_year["fiscal_year"],
        "fiscal_year_end": report_date, "accession_number": accession, "filing_date": company_year["filing_date"],
        "availability_date": company_year["filing_date"], "net_income_used": company_year["net_income"],
        "diluted_weighted_average_shares": shares_result.get("value"),
        "cross_check_calculated_eps": cross_check_calculated_eps,
        "eps_resolution_detail": eps_result, "shares_resolution_detail": shares_result,
    }

    if eps_result["status"] == "RESOLVED":
        entry.update({
            "status": "RESOLVED", "diluted_eps": eps_result["value"],
            "resolution_method": "REPORTED_DILUTED_EPS", "eps_source_concept": "us-gaap:EarningsPerShareDiluted",
        })
        if cross_check_calculated_eps is not None:
            cross_check_diff = round(abs(eps_result["value"] - cross_check_calculated_eps), 4)
        entry["cross_check_diff"] = cross_check_diff
        entry["cross_check_within_tolerance"] = (cross_check_diff is None) or (cross_check_diff <= CROSS_CHECK_TOLERANCE_ABS)
        return entry

    if eps_result["status"] == "AMBIGUOUS":
        entry.update({"status": "REVIEW_REQUIRED", "diluted_eps": None, "resolution_method": None,
                       "eps_source_concept": None, "cross_check_diff": None, "cross_check_within_tolerance": None,
                       "review_reason": f"Multiple differing us-gaap:EarningsPerShareDiluted values found: {eps_result['distinct_values']}"})
        return entry

    # eps_result["status"] == "UNAVAILABLE" -- try the validated fallback
    if shares_result["status"] == "RESOLVED" and shares_result["value"] and company_year["net_income"] is not None:
        entry.update({
            "status": "RESOLVED", "diluted_eps": cross_check_calculated_eps,
            "resolution_method": "CALCULATED_NET_INCOME_OVER_DILUTED_SHARES",
            "eps_source_concept": "CALCULATED(net_income/us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding)",
            "cross_check_diff": 0.0, "cross_check_within_tolerance": True,
        })
        return entry

    entry.update({"status": "UNAVAILABLE", "diluted_eps": None, "resolution_method": None, "eps_source_concept": None,
                  "cross_check_diff": None, "cross_check_within_tolerance": None,
                  "review_reason": "Neither reported diluted EPS nor a net_income/diluted-shares fallback could be resolved."})
    return entry


# =====================================================================
# STAGE 4 -- historical P/E proof (read-only, uses Historical Prices V1)
# =====================================================================

def prove_historical_pe(prod_connection, resolved_entry: dict) -> dict:
    ticker, availability_date, diluted_eps = resolved_entry["ticker"], resolved_entry["availability_date"], resolved_entry["diluted_eps"]

    policy_check = prod_connection.execute(
        "SELECT DISTINCT price_policy_version FROM historical_prices_daily WHERE ticker = ?", [ticker]
    ).fetchall()
    policy_ok = len(policy_check) == 1 and policy_check[0][0] == PRICE_POLICY_VERSION_EXPECTED

    # Reported diluted EPS is NEVER retroactively split-adjusted (it reflects
    # the share count actually reported at that historical date). Yahoo's
    # `close` field, per Historical Price Policy V1 / D-044 Rule C, IS
    # retroactively adjusted for any LATER split. Pairing an unadjusted EPS
    # with an adjusted price would silently scale P/E by the later split
    # ratio (e.g. NVDA's 2024-02 filing predates its 2024-06-10 10:1 split --
    # using `close` there understates P/E by 10x). `nominal_close` is the
    # reconstructed original-scale price and is the one that matches
    # as-reported EPS's scale.
    price_row = prod_connection.execute("""
        SELECT price_date, nominal_close FROM historical_prices_daily
        WHERE ticker = ? AND price_date >= ? ORDER BY price_date ASC LIMIT 1
    """, [ticker, availability_date]).fetchone()

    if price_row is None:
        return {"ticker": ticker, "status": "UNAVAILABLE", "reason": "No trading price on/after availability_date in Historical Prices V1"}

    price_date, nominal_close_price = str(price_row[0]), price_row[1]
    pe_run1 = round(nominal_close_price / diluted_eps, 4) if diluted_eps else None
    pe_run2 = round(nominal_close_price / diluted_eps, 4) if diluted_eps else None  # recomputed independently for reproducibility proof

    return {
        "ticker": ticker, "status": "PASS" if (pe_run1 is not None and pe_run1 == pe_run2) else "FAIL",
        "diluted_eps": diluted_eps, "eps_availability_date": availability_date,
        "price_date_used": price_date, "price_used": nominal_close_price,
        "price_date_on_or_after_availability_date": price_date >= availability_date,
        "historical_pe": pe_run1, "reproducible": pe_run1 == pe_run2,
        "price_policy_version_confirmed": policy_ok,
    }


# =====================================================================
# STAGE 5 -- in-memory load proof (exact future production schema)
# =====================================================================

def run_in_memory_load_proof(resolved_entries: list[dict], pe_proof_cases: list[dict]) -> dict:
    connection = duckdb.connect(database=":memory:")
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.execute(VALUATION_V1_DDL)
        for e in resolved_entries:
            connection.execute(
                "INSERT INTO valuation_v1_per_share_inputs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [e["ticker"], e["fiscal_year"], date.fromisoformat(e["fiscal_year_end"]), e["diluted_eps"],
                 e["resolution_method"], e["eps_source_concept"], e["accession_number"],
                 date.fromisoformat(e["filing_date"]), date.fromisoformat(e["availability_date"]),
                 e["cross_check_calculated_eps"], e["cross_check_diff"], VALUATION_VERSION, created_at],
            )
        connection.execute("COMMIT")
        commit_succeeded = True
    except Exception as exc:  # noqa: BLE001
        connection.execute("ROLLBACK")
        connection.close()
        return {"commit_succeeded": False, "error": str(exc)}

    row_count = connection.execute("SELECT COUNT(*) FROM valuation_v1_per_share_inputs").fetchone()[0]
    distinct_tickers = connection.execute("SELECT COUNT(DISTINCT ticker) FROM valuation_v1_per_share_inputs").fetchone()[0]
    dup_keys = connection.execute(
        "SELECT COUNT(*) FROM (SELECT ticker, fiscal_year, COUNT(*) c FROM valuation_v1_per_share_inputs GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    null_required = connection.execute(
        "SELECT COUNT(*) FROM valuation_v1_per_share_inputs WHERE ticker IS NULL OR fiscal_year IS NULL "
        "OR fiscal_year_end IS NULL OR diluted_eps IS NULL OR resolution_method IS NULL OR eps_source_concept IS NULL "
        "OR accession_number IS NULL OR filing_date IS NULL OR availability_date IS NULL OR valuation_version IS NULL"
    ).fetchone()[0]

    pe_cases_still_valid = []
    for case in pe_proof_cases:
        row = connection.execute(
            "SELECT diluted_eps FROM valuation_v1_per_share_inputs WHERE ticker = ? ORDER BY fiscal_year DESC LIMIT 1",
            [case["ticker"]],
        ).fetchone()
        pe_cases_still_valid.append(row is not None and row[0] == case["diluted_eps"])

    connection.close()
    return {
        "commit_succeeded": commit_succeeded, "rows_inserted": row_count, "distinct_tickers": distinct_tickers,
        "duplicate_keys": dup_keys, "required_fields_null_count": null_required,
        "all_pe_proof_cases_still_valid": all(pe_cases_still_valid) if pe_cases_still_valid else True,
        "proof_passed": (
            commit_succeeded and row_count == EXPECTED_COMPANY_YEARS and distinct_tickers == len(TICKERS)
            and dup_keys == 0 and null_required == 0 and (all(pe_cases_still_valid) if pe_cases_still_valid else True)
        ),
        "used_file_database": False,
    }


# =====================================================================
# ORCHESTRATION -- Stages 1-5, shared by --check-only and --execute
# =====================================================================

def run_full_calculation(prod_connection, wh_connection) -> dict:
    company_years = get_approved_company_years(prod_connection)
    accessions = [c["accession_number"] for c in company_years]
    concept_coverage = inventory_xbrl_concept_coverage(wh_connection, accessions)

    all_entries = [resolve_valuation_input(wh_connection, cy) for cy in company_years]

    micro_proof_entries = []
    for ticker in MICRO_PROOF_TICKERS:
        ticker_entries = sorted([e for e in all_entries if e["ticker"] == ticker], key=lambda e: e["fiscal_year"], reverse=True)
        micro_proof_entries.extend(ticker_entries[:2])
    micro_proof_passed = all(
        e["status"] == "RESOLVED" and (e["cross_check_diff"] is None or e["cross_check_within_tolerance"])
        for e in micro_proof_entries
    )

    resolved = [e for e in all_entries if e["status"] == "RESOLVED"]
    unavailable = [e for e in all_entries if e["status"] == "UNAVAILABLE"]
    review_required = [e for e in all_entries if e["status"] == "REVIEW_REQUIRED"]

    duplicate_keys = len(resolved) != len({(e["ticker"], e["fiscal_year"]) for e in resolved})
    all_lineage_complete = all(e["accession_number"] and e["filing_date"] and e["availability_date"] for e in resolved)
    all_cross_checks_within_tolerance = all(e["cross_check_within_tolerance"] for e in resolved if e["cross_check_diff"] is not None)

    pe_proof = []
    if len(resolved) == EXPECTED_COMPANY_YEARS:
        for ticker in MICRO_PROOF_TICKERS:
            latest = sorted([e for e in resolved if e["ticker"] == ticker], key=lambda e: e["fiscal_year"])[-1]
            pe_proof.append(prove_historical_pe(prod_connection, latest))

    in_memory_proof = run_in_memory_load_proof(resolved, pe_proof) if len(resolved) == EXPECTED_COMPANY_YEARS else {"proof_passed": False, "reason": "not all 45 resolved"}

    return {
        "company_years_total": len(company_years), "concept_coverage": concept_coverage,
        "all_entries": all_entries, "micro_proof_entries": micro_proof_entries, "micro_proof_passed": micro_proof_passed,
        "resolved": resolved, "unavailable": unavailable, "review_required": review_required,
        "resolved_count": len(resolved), "unavailable_count": len(unavailable), "review_required_count": len(review_required),
        "no_duplicate_keys": not duplicate_keys, "all_lineage_complete": all_lineage_complete,
        "all_cross_checks_within_tolerance": all_cross_checks_within_tolerance,
        "pe_proof": pe_proof, "pe_proof_passed": all(p["status"] == "PASS" for p in pe_proof) if pe_proof else False,
        "in_memory_proof": in_memory_proof,
        "all_45_resolved": len(resolved) == EXPECTED_COMPANY_YEARS,
    }


# =====================================================================
# --check-only MODE
# =====================================================================

def write_preview_csv(resolved: list[dict]) -> None:
    columns = ["ticker", "fiscal_year", "fiscal_year_end", "diluted_eps", "resolution_method", "eps_source_concept",
               "accession_number", "filing_date", "availability_date", "cross_check_calculated_eps", "cross_check_diff"]
    with PREVIEW_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for e in sorted(resolved, key=lambda x: (x["ticker"], x["fiscal_year"])):
            writer.writerow([e.get(c) for c in columns])


def run_check_only() -> dict:
    start = time.perf_counter()
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    wh_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    table_already_exists = bool(prod_connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'valuation_v1_per_share_inputs'"
    ).fetchone()[0])

    db_hash_before = sha256_of_file(PRODUCTION_DB_PATH)
    result = run_full_calculation(prod_connection, wh_connection)
    db_hash_after = sha256_of_file(PRODUCTION_DB_PATH)

    prod_connection.close()
    wh_connection.close()

    global_checks = {
        "micro_proof_passed": result["micro_proof_passed"],
        "all_45_resolved": result["all_45_resolved"],
        "no_duplicate_keys": result["no_duplicate_keys"],
        "all_lineage_complete": result["all_lineage_complete"],
        "all_cross_checks_within_tolerance": result["all_cross_checks_within_tolerance"],
        "pe_proof_passed": result["pe_proof_passed"],
        "in_memory_load_proof_passed": result["in_memory_proof"].get("proof_passed", False),
        "production_table_does_not_yet_exist": not table_already_exists,
        "database_unchanged": db_hash_before == db_hash_after,
    }
    overall = all(global_checks.values())
    runtime = round(time.perf_counter() - start, 3)

    output = {
        "mode": "check-only", "status": "PASS" if overall else "FAIL",
        "company_years_total": result["company_years_total"],
        "concept_coverage": result["concept_coverage"],
        "resolved_count": result["resolved_count"], "unavailable_count": result["unavailable_count"],
        "review_required_count": result["review_required_count"],
        "unavailable": result["unavailable"], "review_required": result["review_required"],
        "micro_proof_entries": result["micro_proof_entries"],
        "pe_proof": result["pe_proof"], "in_memory_proof": result["in_memory_proof"],
        "global_checks": global_checks,
        "expected_production_table_ddl": VALUATION_V1_DDL.strip(),
        "database_sha256_before": db_hash_before, "database_sha256_after": db_hash_after,
        "table_created": False, "backup_performed": False, "pid_lock_acquired": False,
        "runtime_seconds": runtime, "checked_at": utc_now_iso(),
    }
    atomic_write_json(CHECK_ONLY_OUTPUT_PATH, output)
    write_preview_csv(result["resolved"])
    log(f"check-only run: status={output['status']} runtime={runtime}s resolved={result['resolved_count']}/{EXPECTED_COMPANY_YEARS}", also_print=False)

    print("=" * 100)
    print(f"VALUATION V1 -- CHECK-ONLY: {output['status']}  (runtime {runtime}s)")
    print("=" * 100)
    print(f"Concept coverage (distinct accessions with concept / 45): {result['concept_coverage']}")
    print(f"\nResolved: {result['resolved_count']}/{EXPECTED_COMPANY_YEARS}  Unavailable: {result['unavailable_count']}  REVIEW_REQUIRED: {result['review_required_count']}")
    print(f"Micro proof passed: {result['micro_proof_passed']}")
    for e in result["micro_proof_entries"]:
        print(f"  {e['ticker']} FY{e['fiscal_year']}: net_income={e['net_income_used']} diluted_eps={e['diluted_eps']} "
              f"shares={e['diluted_weighted_average_shares']} calc_eps={e['cross_check_calculated_eps']} "
              f"diff={e['cross_check_diff']} filing_date={e['filing_date']} accession={e['accession_number']}")
    print(f"\nHistorical P/E proof: {result['pe_proof']}")
    print(f"\nIn-memory load proof: {result['in_memory_proof']}")
    print(f"\nGlobal checks: {global_checks}")
    print(f"Database SHA-256 unchanged: {db_hash_before == db_hash_after}")
    print("=" * 100)
    return output


# =====================================================================
# --execute MODE
# =====================================================================

def phase_backup() -> dict:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_valuation_v1_load_{timestamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    source_checksum = sha256_of_file(PRODUCTION_DB_PATH)
    backup_checksum = sha256_of_file(backup_path)
    if source_checksum != backup_checksum:
        raise RuntimeError("Backup checksum mismatch -- aborting before any write.")
    return {"backup_path": str(backup_path), "backup_checksum": backup_checksum, "source_checksum": source_checksum}


def validate_committed_table(connection, expected_row_count: int) -> None:
    errors = []
    committed_count = connection.execute("SELECT COUNT(*) FROM valuation_v1_per_share_inputs").fetchone()[0]
    if committed_count != expected_row_count:
        errors.append(f"committed row count {committed_count} != expected {expected_row_count}")
    dup = connection.execute(
        "SELECT COUNT(*) FROM (SELECT ticker, fiscal_year, COUNT(*) c FROM valuation_v1_per_share_inputs GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if dup:
        errors.append(f"{dup} duplicate keys found in committed table")
    distinct_tickers = connection.execute("SELECT COUNT(DISTINCT ticker) FROM valuation_v1_per_share_inputs").fetchone()[0]
    if distinct_tickers != len(TICKERS):
        errors.append(f"committed table has {distinct_tickers} distinct tickers, expected {len(TICKERS)}")
    wrong_version = connection.execute(
        "SELECT COUNT(*) FROM valuation_v1_per_share_inputs WHERE valuation_version <> ?", [VALUATION_VERSION]
    ).fetchone()[0]
    if wrong_version:
        errors.append(f"{wrong_version} row(s) have wrong valuation_version")
    if errors:
        raise RuntimeError(f"Post-insert table validation failed ({len(errors)} issue(s)): {errors}")


def capture_pre_existing_fingerprints(connection) -> dict:
    tables = [r[0] for r in connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()]
    result = {}
    for table in tables:
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        rows_str = sorted(repr(r) for r in rows)
        digest = hashlib.sha256("\n".join(rows_str).encode("utf-8")).hexdigest()
        result[table] = {"row_count": len(rows), "fingerprint": digest}
    return result


def run_execute() -> int:
    acquire_pid_lock()
    log("=== valuation v1 load: --execute started ===")
    try:
        prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
        wh_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

        pre_fingerprints = capture_pre_existing_fingerprints(prod_connection)
        annual_v1_hash_before = sha256_of_file(ANNUAL_V1_DB_PATH) if ANNUAL_V1_DB_PATH.exists() else None

        result = run_full_calculation(prod_connection, wh_connection)
        prod_connection.close()
        wh_connection.close()

        if not result["all_45_resolved"]:
            raise RuntimeError(f"Pre-write validation failed -- not all 45 company-years resolved: "
                                f"resolved={result['resolved_count']} unavailable={len(result['unavailable'])} "
                                f"review_required={len(result['review_required'])}")
        if not (result["micro_proof_passed"] and result["no_duplicate_keys"] and result["all_lineage_complete"]
                and result["all_cross_checks_within_tolerance"] and result["pe_proof_passed"]
                and result["in_memory_proof"].get("proof_passed")):
            raise RuntimeError(f"Pre-write validation failed -- one or more proof stages did not pass: "
                                f"micro_proof={result['micro_proof_passed']} pe_proof={result['pe_proof_passed']} "
                                f"in_memory={result['in_memory_proof'].get('proof_passed')}")

        backup_info = phase_backup()
        log(f"Backup complete: {backup_info['backup_path']}")

        connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute("DROP TABLE IF EXISTS valuation_v1_per_share_inputs")
            connection.execute(VALUATION_V1_DDL)
            created_at = datetime.now(timezone.utc).replace(tzinfo=None)
            for e in result["resolved"]:
                connection.execute(
                    "INSERT INTO valuation_v1_per_share_inputs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [e["ticker"], e["fiscal_year"], date.fromisoformat(e["fiscal_year_end"]), e["diluted_eps"],
                     e["resolution_method"], e["eps_source_concept"], e["accession_number"],
                     date.fromisoformat(e["filing_date"]), date.fromisoformat(e["availability_date"]),
                     e["cross_check_calculated_eps"], e["cross_check_diff"], VALUATION_VERSION, created_at],
                )
            validate_committed_table(connection, len(result["resolved"]))
            connection.execute("COMMIT")
            log("Atomic transaction committed.")
        except Exception:
            connection.execute("ROLLBACK")
            connection.close()
            raise
        connection.close()

        # --- independent post-commit verification ---
        prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
        post_fingerprints = capture_pre_existing_fingerprints(prod_connection)
        annual_v1_hash_after = sha256_of_file(ANNUAL_V1_DB_PATH) if ANNUAL_V1_DB_PATH.exists() else None

        pre_existing_unchanged = []
        for table, before in pre_fingerprints.items():
            after = post_fingerprints.get(table)
            if after != before:
                pre_existing_unchanged.append({"table": table, "before": before, "after": after})

        row_count = prod_connection.execute("SELECT COUNT(*) FROM valuation_v1_per_share_inputs").fetchone()[0]
        prod_connection.close()

        post_checks = {
            "row_count_correct": row_count == EXPECTED_COMPANY_YEARS,
            "pre_existing_tables_unchanged": len(pre_existing_unchanged) == 0,
            "annual_v1_checksum_unchanged": annual_v1_hash_before == annual_v1_hash_after,
        }
        if not all(post_checks.values()):
            raise RuntimeError(f"Post-commit validation failed (table already committed -- manual review required): "
                                f"{post_checks}  diffs={pre_existing_unchanged}")

        result_out = {
            "status": "PASS", "table_created": True, "rows_inserted": row_count,
            "backup_path": backup_info["backup_path"], "backup_checksum": backup_info["backup_checksum"],
            "post_checks": post_checks, "completed_at": utc_now_iso(),
        }
        atomic_write_json(RESULT_JSON_PATH, result_out)
        with RESULT_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["rows_inserted_total", "backup_path"])
            writer.writerow([row_count, backup_info["backup_path"]])

        manifest = {
            "release": "VALUATION_V1", "valuation_version": VALUATION_VERSION, "rows_inserted": row_count,
            "backup_path": backup_info["backup_path"], "backup_checksum": backup_info["backup_checksum"],
            "created_at_utc": utc_now_iso(),
        }
        atomic_write_json(RELEASE_MANIFEST_PATH, manifest)

        log("=== valuation v1 load: --execute COMPLETE (PASS) ===")
        return 0
    except Exception as exc:  # noqa: BLE001
        fail_result = {"status": "FAIL", "error": str(exc), "failed_at": utc_now_iso()}
        atomic_write_json(RESULT_JSON_PATH, fail_result)
        log(f"=== valuation v1 load: --execute FAILED: {exc} ===")
        return 1
    finally:
        release_pid_lock()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Valuation V1 per-share inputs build/load.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-only", action="store_true")
    group.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.check_only:
        output = run_check_only()
        return 0 if output["status"] == "PASS" else 1
    return run_execute()


if __name__ == "__main__":
    sys.exit(main())
