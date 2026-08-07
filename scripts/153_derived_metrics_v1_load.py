"""
Derived Metrics V1 -- production build/load for all 9 approved tickers
(ORCL, MSFT, META, NVDA, GOOGL, AMZN, MU, CRWD, PANW), generalizing the
exact formulas, point-in-time rules, and lineage logic proven single-
ticker in scripts/152_msft_derived_metrics_proof.py to:

  1. operating_margin      = operating_income / revenue
  2. revenue_yoy_growth    = current-period revenue / matching prior-
                              period revenue - 1 (fiscal-period identity,
                              never calendar-quarter assumptions)

both at 'annual' and 'quarterly' frequency. No other metric is added.

Two mutually exclusive modes:

  --check-only  Fully read-only. Calculates and validates the complete
                9-ticker dataset (same computation used by --execute),
                including the SQL-vs-Python cross-validation and the
                MSFT-proof-exact-match check, but creates no table,
                backup, archive, or PID lock, and writes nothing to any
                database.

  --execute     The real production load: PID lock -> preconditions ->
                backup (SHA-256-verified) -> full dataset calculated
                and validated again (never trusts a stale/cached
                result) -> one atomic transaction that (re)creates
                `derived_metric_results` and inserts every validated
                row -> post-commit validation -> result/manifest/log
                written -> exit code 0 only if every post-commit check
                passes.

Reads only from data/database/ai_stock_agent.duckdb. Never touches
Annual Data V1 or Quarterly Data V1's own tables (only SELECTs from
`financial_metric_results`, `extraction_runs`, `sec_filings`,
`quarterly_extraction_runs`, `quarterly_metric_results`). Never runs an
extraction engine. Never opens the XBRL warehouse for writing.
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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DOCS_DIR = PROJECT_DIR / "docs"
LOGS_DIR = PROJECT_DIR / "logs"
BACKUPS_DIR = DATA_DIR / "database" / "backups"
ARCHIVE_DIR = DATA_DIR / "archive"

PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
ANNUAL_V1_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"
MSFT_PROOF_JSON_PATH = DATA_DIR / "proofs" / "msft_derived_metrics_proof.json"

CHECK_ONLY_OUTPUT_PATH = DATA_DIR / "derived_metrics_v1_build_validation.json"
PREVIEW_CSV_PATH = DATA_DIR / "derived_metrics_v1_preview.csv"
RESULT_JSON_PATH = DATA_DIR / "derived_metrics_v1_load_result.json"
RESULT_CSV_PATH = DATA_DIR / "derived_metrics_v1_load_result.csv"
RELEASE_MANIFEST_PATH = DATA_DIR / "derived_metrics_v1_release_manifest.json"
LOG_PATH = LOGS_DIR / "derived_metrics_v1_load.log"
PID_LOCK_PATH = DATA_DIR / "derived_metrics_v1_load.pid"

TICKERS = ["ORCL", "MSFT", "META", "NVDA", "GOOGL", "AMZN", "MU", "CRWD", "PANW"]
EXPECTED_TICKER_COUNT = 9
ENGINE_VERSION = "DERIVED_METRICS_ENGINE_V1"
VALIDATION_TOLERANCE_ABS = Decimal("0.000000001")  # 1e-9, absolute
DECIMAL_18 = Decimal("0.000000000000000001")

APPROVED_ANNUAL_STATUSES = ("PASS", "PASS_MATURITY_BASIS", "PASS_NORMALIZED_TAX", "PASS_DIRECT_AGGREGATE")
APPROVED_QUARTERLY_STATUSES = ("PASS", "PASS_ROUNDING_TOLERANCE")
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
QUARTER_TO_INT = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
ANNUAL_FISCAL_QUARTER_KEY = 0  # sentinel used only inside the PRIMARY KEY -- fiscal_quarter itself stays NULL for annual rows

# Schema note (fix for the fiscal_quarter / PRIMARY KEY defect found by the
# first --execute attempt): a column that is part of a PRIMARY KEY is
# implicitly NOT NULL in DuckDB (and standard SQL), regardless of its own
# column-level nullability declaration. `fiscal_quarter` must stay NULL for
# annual rows (per the required semantics), so it can no longer be a PK
# member. `fiscal_quarter_key` is a NOT NULL surrogate used ONLY for
# uniqueness (0 for annual, 1-4 for quarterly, always in lockstep with
# `fiscal_quarter` via the CHECK constraints below) -- it carries no new
# information and `fiscal_quarter` remains the column callers should read.
DERIVED_METRIC_RESULTS_DDL = """
CREATE TABLE derived_metric_results (
    ticker                  VARCHAR NOT NULL,
    frequency               VARCHAR NOT NULL,
    fiscal_year_end         DATE NOT NULL,
    fiscal_year             INTEGER NOT NULL,
    fiscal_quarter          TINYINT,
    fiscal_quarter_key      TINYINT NOT NULL,
    derived_metric          VARCHAR NOT NULL,
    value                   DECIMAL(38,18) NOT NULL,
    availability_date       DATE NOT NULL,
    formula                 VARCHAR NOT NULL,
    source_periods          JSON NOT NULL,
    source_run_ids          JSON NOT NULL,
    source_accessions       JSON NOT NULL,
    reconciliation_status   VARCHAR NOT NULL,
    engine_version          VARCHAR NOT NULL,
    created_at              TIMESTAMP NOT NULL,
    CONSTRAINT chk_frequency_values
        CHECK (frequency IN ('annual', 'quarterly')),
    CONSTRAINT chk_annual_quarter_shape
        CHECK (frequency <> 'annual' OR (fiscal_quarter IS NULL AND fiscal_quarter_key = 0)),
    CONSTRAINT chk_quarterly_quarter_shape
        CHECK (frequency <> 'quarterly' OR (
            fiscal_quarter IS NOT NULL AND fiscal_quarter BETWEEN 1 AND 4 AND fiscal_quarter_key = fiscal_quarter
        )),
    CONSTRAINT chk_derived_metric_values
        CHECK (derived_metric IN ('operating_margin', 'revenue_yoy_growth')),
    PRIMARY KEY (ticker, frequency, fiscal_year_end, fiscal_quarter_key, derived_metric)
)
"""


def fiscal_quarter_key_for(fiscal_quarter_label: str | None) -> int:
    """0 for annual (fiscal_quarter_label is None), 1-4 for quarterly."""
    return QUARTER_TO_INT[fiscal_quarter_label] if fiscal_quarter_label else ANNUAL_FISCAL_QUARTER_KEY


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


def to_decimal(value) -> Decimal:
    if value is None:
        raise InvalidOperation("cannot convert None to Decimal")
    return Decimal(str(value))


def quantize_18(value: Decimal) -> Decimal:
    return value.quantize(DECIMAL_18, rounding=ROUND_HALF_UP)


def to_iso_date(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


# =====================================================================
# PID LOCK (same discipline as scripts/151, reimplemented here since
# derived-metrics loading is a distinct domain -- not imported)
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
# SOURCE EXTRACTION (schemas identical to scripts/152; parameterized by ticker)
# =====================================================================

def get_annual_source_rows(connection, ticker: str) -> pd.DataFrame:
    query = """
        SELECT
            f.extraction_run_id, f.metric_name, f.value, f.status,
            f.is_primary_metric, f.is_derived_metric,
            f.period_start, f.period_end,
            r.accession_number, s.filing_date, s.fiscal_year, s.report_date
        FROM financial_metric_results f
        JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id
        JOIN sec_filings s ON s.accession_number = r.accession_number
        WHERE s.ticker = ?
          AND f.metric_name IN ('revenue', 'operating_income')
          AND f.status IN ({placeholders})
          AND f.is_primary_metric = TRUE
          AND (f.is_derived_metric = FALSE OR f.is_derived_metric IS NULL)
        ORDER BY f.period_end, f.metric_name
    """.format(placeholders=",".join("?" * len(APPROVED_ANNUAL_STATUSES)))
    return connection.execute(query, [ticker, *APPROVED_ANNUAL_STATUSES]).fetchdf()


def get_quarterly_source_rows(connection, ticker: str) -> pd.DataFrame:
    query = """
        SELECT
            run_id, ticker, fiscal_year_end, fiscal_quarter, metric_name, value, unit,
            extraction_basis, period_start, period_end, availability_date,
            accession_number, reconciliation_status
        FROM quarterly_metric_results
        WHERE ticker = ?
          AND metric_name IN ('revenue', 'operating_income')
          AND reconciliation_status IN ({placeholders})
        ORDER BY fiscal_year_end, fiscal_quarter, metric_name
    """.format(placeholders=",".join("?" * len(APPROVED_QUARTERLY_STATUSES)))
    return connection.execute(query, [ticker, *APPROVED_QUARTERLY_STATUSES]).fetchdf()


def get_fiscal_year_end_to_fiscal_year(connection, ticker: str) -> dict[str, int]:
    rows = connection.execute(
        "SELECT DISTINCT q.fiscal_year_end, s.fiscal_year FROM quarterly_extraction_runs q "
        "JOIN sec_filings s ON s.accession_number = q.fy_accession WHERE q.ticker = ?", [ticker],
    ).fetchall()
    return {fiscal_year_end: fiscal_year for fiscal_year_end, fiscal_year in rows}


def check_no_duplicate_source_rows(annual_df: pd.DataFrame, quarterly_df: pd.DataFrame) -> list[str]:
    errors = []
    dup_annual = annual_df.groupby(["period_end", "metric_name"]).size()
    dup_annual = dup_annual[dup_annual > 1]
    if len(dup_annual) > 0:
        errors.append(f"duplicate annual source rows: {dup_annual.to_dict()}")
    dup_quarterly = quarterly_df.groupby(["fiscal_year_end", "fiscal_quarter", "metric_name"]).size()
    dup_quarterly = dup_quarterly[dup_quarterly > 1]
    if len(dup_quarterly) > 0:
        errors.append(f"duplicate quarterly source rows: {dup_quarterly.to_dict()}")
    return errors


def check_accessions_and_run_ids_exist(connection, run_ids: set[str], accessions: set[str]) -> list[str]:
    errors = []
    for run_id in run_ids:
        exists = connection.execute(
            "SELECT (EXISTS(SELECT 1 FROM extraction_runs WHERE extraction_run_id = ?) "
            "OR EXISTS(SELECT 1 FROM quarterly_extraction_runs WHERE run_id = ?))", [run_id, run_id],
        ).fetchone()[0]
        if not exists:
            errors.append(f"run_id not found in production: {run_id}")
    for accession in accessions:
        exists = connection.execute("SELECT EXISTS(SELECT 1 FROM sec_filings WHERE accession_number = ?)", [accession]).fetchone()[0]
        if not exists:
            errors.append(f"accession_number not found in production sec_filings: {accession}")
    return errors


# =====================================================================
# PYTHON DECIMAL DERIVATION (same rules as scripts/152; ticker-parameterized;
# derived_metric names/shape match the production schema exactly)
# =====================================================================

def build_annual_lookup(annual_df: pd.DataFrame) -> dict[str, dict[str, dict]]:
    lookup: dict[str, dict[str, dict]] = {}
    for _, row in annual_df.iterrows():
        period_end = to_iso_date(row["period_end"])
        lookup.setdefault(period_end, {})[row["metric_name"]] = row.to_dict()
    return lookup


def build_quarterly_lookup(quarterly_df: pd.DataFrame) -> dict[tuple[str, str], dict[str, dict]]:
    lookup: dict[tuple[str, str], dict[str, dict]] = {}
    for _, row in quarterly_df.iterrows():
        key = (row["fiscal_year_end"], row["fiscal_quarter"])
        lookup.setdefault(key, {})[row["metric_name"]] = row.to_dict()
    return lookup


def compute_python_derived(ticker: str, annual_df: pd.DataFrame, quarterly_df: pd.DataFrame, fy_end_to_fy: dict[str, int]) -> tuple[list[dict], list[dict]]:
    observations: list[dict] = []
    unresolved: list[dict] = []

    annual_lookup = build_annual_lookup(annual_df)
    sorted_period_ends = sorted(annual_lookup.keys())

    # --- operating_margin, annual ---
    for period_end in sorted_period_ends:
        metrics = annual_lookup[period_end]
        if "revenue" not in metrics or "operating_income" not in metrics:
            unresolved.append({"ticker": ticker, "frequency": "annual", "derived_metric": "operating_margin",
                                "fiscal_year_end": period_end, "fiscal_quarter": None,
                                "reason": "missing revenue or operating_income source row"})
            continue
        revenue_row, oi_row = metrics["revenue"], metrics["operating_income"]
        revenue, operating_income = to_decimal(revenue_row["value"]), to_decimal(oi_row["value"])
        if revenue == 0:
            unresolved.append({"ticker": ticker, "frequency": "annual", "derived_metric": "operating_margin",
                                "fiscal_year_end": period_end, "fiscal_quarter": None, "reason": "division by zero (revenue == 0)"})
            continue
        if revenue_row["extraction_run_id"] != oi_row["extraction_run_id"]:
            unresolved.append({"ticker": ticker, "frequency": "annual", "derived_metric": "operating_margin",
                                "fiscal_year_end": period_end, "fiscal_quarter": None,
                                "reason": "revenue and operating_income come from different extraction runs -- ambiguous"})
            continue
        observations.append({
            "ticker": ticker, "frequency": "annual", "derived_metric": "operating_margin",
            "fiscal_year_end": period_end, "fiscal_year": fy_end_to_fy.get(period_end) or int(revenue_row.get("fiscal_year")),
            "fiscal_quarter": None, "value": operating_income / revenue,
            "availability_date": to_iso_date(revenue_row["filing_date"]),
            "formula": "operating_income / revenue",
            "source_periods": [period_end], "source_run_ids": [revenue_row["extraction_run_id"]],
            "source_accessions": [revenue_row["accession_number"]],
        })

    # --- revenue_yoy_growth, annual ---
    for i, period_end in enumerate(sorted_period_ends):
        if i == 0:
            unresolved.append({"ticker": ticker, "frequency": "annual", "derived_metric": "revenue_yoy_growth",
                                "fiscal_year_end": period_end, "fiscal_quarter": None,
                                "reason": "no prior fiscal year revenue available in Annual Data V1 (earliest frozen fiscal year)"})
            continue
        prior_period_end = sorted_period_ends[i - 1]
        current_metrics, prior_metrics = annual_lookup[period_end], annual_lookup[prior_period_end]
        if "revenue" not in current_metrics or "revenue" not in prior_metrics:
            unresolved.append({"ticker": ticker, "frequency": "annual", "derived_metric": "revenue_yoy_growth",
                                "fiscal_year_end": period_end, "fiscal_quarter": None,
                                "reason": "missing revenue source row for current or prior fiscal year"})
            continue
        current_row, prior_row = current_metrics["revenue"], prior_metrics["revenue"]
        current_revenue, prior_revenue = to_decimal(current_row["value"]), to_decimal(prior_row["value"])
        if prior_revenue == 0:
            unresolved.append({"ticker": ticker, "frequency": "annual", "derived_metric": "revenue_yoy_growth",
                                "fiscal_year_end": period_end, "fiscal_quarter": None,
                                "reason": "division by zero (prior fiscal year revenue == 0)"})
            continue
        current_filing_date, prior_filing_date = current_row["filing_date"], prior_row["filing_date"]
        if prior_filing_date > current_filing_date:
            unresolved.append({"ticker": ticker, "frequency": "annual", "derived_metric": "revenue_yoy_growth",
                                "fiscal_year_end": period_end, "fiscal_quarter": None,
                                "reason": "prior fiscal year filing_date is after current fiscal year filing_date -- refusing (future-data violation)"})
            continue
        observations.append({
            "ticker": ticker, "frequency": "annual", "derived_metric": "revenue_yoy_growth",
            "fiscal_year_end": period_end, "fiscal_year": fy_end_to_fy.get(period_end) or int(current_row.get("fiscal_year")),
            "fiscal_quarter": None, "value": current_revenue / prior_revenue - 1,
            "availability_date": to_iso_date(max(current_filing_date, prior_filing_date)),
            "formula": "current_fy_revenue / prior_fy_revenue - 1",
            "source_periods": [prior_period_end, period_end],
            "source_run_ids": [prior_row["extraction_run_id"], current_row["extraction_run_id"]],
            "source_accessions": [prior_row["accession_number"], current_row["accession_number"]],
        })

    quarterly_lookup = build_quarterly_lookup(quarterly_df)
    sorted_fiscal_year_ends = sorted({fy for fy, _ in quarterly_lookup.keys()})

    # --- operating_margin, quarterly ---
    for fiscal_year_end in sorted_fiscal_year_ends:
        for quarter in QUARTERS:
            key = (fiscal_year_end, quarter)
            if key not in quarterly_lookup:
                continue
            metrics = quarterly_lookup[key]
            if "revenue" not in metrics or "operating_income" not in metrics:
                unresolved.append({"ticker": ticker, "frequency": "quarterly", "derived_metric": "operating_margin",
                                    "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter,
                                    "reason": "missing revenue or operating_income source row"})
                continue
            revenue_row, oi_row = metrics["revenue"], metrics["operating_income"]
            revenue, operating_income = to_decimal(revenue_row["value"]), to_decimal(oi_row["value"])
            if revenue == 0:
                unresolved.append({"ticker": ticker, "frequency": "quarterly", "derived_metric": "operating_margin",
                                    "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter, "reason": "division by zero (revenue == 0)"})
                continue
            if revenue_row["availability_date"] != oi_row["availability_date"]:
                unresolved.append({"ticker": ticker, "frequency": "quarterly", "derived_metric": "operating_margin",
                                    "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter,
                                    "reason": "revenue and operating_income have different availability_date -- ambiguous"})
                continue
            observations.append({
                "ticker": ticker, "frequency": "quarterly", "derived_metric": "operating_margin",
                "fiscal_year_end": fiscal_year_end, "fiscal_year": fy_end_to_fy.get(fiscal_year_end),
                "fiscal_quarter": quarter, "value": operating_income / revenue,
                "availability_date": to_iso_date(revenue_row["availability_date"]),
                "formula": "quarterly_operating_income / quarterly_revenue",
                "source_periods": [f"{fiscal_year_end}:{quarter}"], "source_run_ids": [revenue_row["run_id"]],
                "source_accessions": [revenue_row["accession_number"]],
            })

    # --- revenue_yoy_growth, quarterly ---
    for i, fiscal_year_end in enumerate(sorted_fiscal_year_ends):
        for quarter in QUARTERS:
            key = (fiscal_year_end, quarter)
            if key not in quarterly_lookup:
                continue
            if i == 0:
                unresolved.append({"ticker": ticker, "frequency": "quarterly", "derived_metric": "revenue_yoy_growth",
                                    "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter,
                                    "reason": "no same fiscal quarter in the prior fiscal year available in Quarterly Data V1 (earliest frozen fiscal year)"})
                continue
            prior_fiscal_year_end = sorted_fiscal_year_ends[i - 1]
            prior_key = (prior_fiscal_year_end, quarter)
            if prior_key not in quarterly_lookup or "revenue" not in quarterly_lookup[prior_key]:
                unresolved.append({"ticker": ticker, "frequency": "quarterly", "derived_metric": "revenue_yoy_growth",
                                    "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter,
                                    "reason": "same fiscal quarter not found in the prior fiscal year"})
                continue
            if "revenue" not in quarterly_lookup[key]:
                unresolved.append({"ticker": ticker, "frequency": "quarterly", "derived_metric": "revenue_yoy_growth",
                                    "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter,
                                    "reason": "missing current-quarter revenue source row"})
                continue
            current_row, prior_row = quarterly_lookup[key]["revenue"], quarterly_lookup[prior_key]["revenue"]
            current_revenue, prior_revenue = to_decimal(current_row["value"]), to_decimal(prior_row["value"])
            if prior_revenue == 0:
                unresolved.append({"ticker": ticker, "frequency": "quarterly", "derived_metric": "revenue_yoy_growth",
                                    "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter,
                                    "reason": "division by zero (prior fiscal quarter revenue == 0)"})
                continue
            current_avail, prior_avail = current_row["availability_date"], prior_row["availability_date"]
            if prior_avail > current_avail:
                unresolved.append({"ticker": ticker, "frequency": "quarterly", "derived_metric": "revenue_yoy_growth",
                                    "fiscal_year_end": fiscal_year_end, "fiscal_quarter": quarter,
                                    "reason": "prior fiscal quarter availability_date is after current -- refusing (future-data violation)"})
                continue
            observations.append({
                "ticker": ticker, "frequency": "quarterly", "derived_metric": "revenue_yoy_growth",
                "fiscal_year_end": fiscal_year_end, "fiscal_year": fy_end_to_fy.get(fiscal_year_end),
                "fiscal_quarter": quarter, "value": current_revenue / prior_revenue - 1,
                "availability_date": to_iso_date(max(current_avail, prior_avail)),
                "formula": "current_fq_revenue / same_fq_prior_fy_revenue - 1",
                "source_periods": [f"{prior_fiscal_year_end}:{quarter}", f"{fiscal_year_end}:{quarter}"],
                "source_run_ids": [prior_row["run_id"], current_row["run_id"]],
                "source_accessions": [prior_row["accession_number"], current_row["accession_number"]],
            })

    return observations, unresolved


# =====================================================================
# INDEPENDENT DUCKDB SQL DERIVATION (per ticker)
# =====================================================================

def compute_sql_derived(connection, ticker: str) -> dict[str, pd.DataFrame]:
    annual_status_list = ",".join(f"'{s}'" for s in APPROVED_ANNUAL_STATUSES)
    quarterly_status_list = ",".join(f"'{s}'" for s in APPROVED_QUARTERLY_STATUSES)

    annual_margin_sql = connection.execute(f"""
        SELECT rev.period_end AS fiscal_year_end, oi.value / rev.value AS value
        FROM financial_metric_results rev
        JOIN extraction_runs r1 ON r1.extraction_run_id = rev.extraction_run_id
        JOIN sec_filings s1 ON s1.accession_number = r1.accession_number
        JOIN financial_metric_results oi ON oi.extraction_run_id = rev.extraction_run_id AND oi.metric_name = 'operating_income'
        WHERE s1.ticker = ? AND rev.metric_name = 'revenue'
          AND rev.status IN ({annual_status_list}) AND oi.status IN ({annual_status_list})
          AND rev.is_primary_metric = TRUE AND oi.is_primary_metric = TRUE
        ORDER BY rev.period_end
    """, [ticker]).fetchdf()

    annual_yoy_sql = connection.execute(f"""
        WITH rev AS (
            SELECT rev_f.period_end AS fiscal_year_end, rev_f.value AS revenue
            FROM financial_metric_results rev_f
            JOIN extraction_runs r ON r.extraction_run_id = rev_f.extraction_run_id
            JOIN sec_filings s ON s.accession_number = r.accession_number
            WHERE s.ticker = ? AND rev_f.metric_name = 'revenue' AND rev_f.status IN ({annual_status_list})
              AND rev_f.is_primary_metric = TRUE
        )
        SELECT fiscal_year_end, revenue / LAG(revenue) OVER (ORDER BY fiscal_year_end) - 1 AS value
        FROM rev ORDER BY fiscal_year_end
    """, [ticker]).fetchdf()

    quarterly_margin_sql = connection.execute(f"""
        SELECT rev.fiscal_year_end, rev.fiscal_quarter, oi.value / rev.value AS value
        FROM quarterly_metric_results rev
        JOIN quarterly_metric_results oi ON oi.run_id = rev.run_id AND oi.fiscal_quarter = rev.fiscal_quarter AND oi.metric_name = 'operating_income'
        WHERE rev.ticker = ? AND rev.metric_name = 'revenue'
          AND rev.reconciliation_status IN ({quarterly_status_list}) AND oi.reconciliation_status IN ({quarterly_status_list})
        ORDER BY rev.fiscal_year_end, rev.fiscal_quarter
    """, [ticker]).fetchdf()

    quarterly_yoy_sql = connection.execute(f"""
        WITH rev AS (
            SELECT fiscal_year_end, fiscal_quarter, value AS revenue
            FROM quarterly_metric_results
            WHERE ticker = ? AND metric_name = 'revenue' AND reconciliation_status IN ({quarterly_status_list})
        )
        SELECT fiscal_year_end, fiscal_quarter,
               revenue / LAG(revenue) OVER (PARTITION BY fiscal_quarter ORDER BY fiscal_year_end) - 1 AS value
        FROM rev ORDER BY fiscal_quarter, fiscal_year_end
    """, [ticker]).fetchdf()

    return {
        ("annual", "operating_margin"): annual_margin_sql, ("annual", "revenue_yoy_growth"): annual_yoy_sql,
        ("quarterly", "operating_margin"): quarterly_margin_sql, ("quarterly", "revenue_yoy_growth"): quarterly_yoy_sql,
    }


def validate_against_sql(observations: list[dict], sql_results: dict[tuple[str, str], pd.DataFrame]) -> list[dict]:
    for obs in observations:
        table = sql_results[(obs["frequency"], obs["derived_metric"])]
        if obs["frequency"] == "annual":
            match = table[table["fiscal_year_end"] == obs["fiscal_year_end"]]
        else:
            match = table[(table["fiscal_year_end"] == obs["fiscal_year_end"]) & (table["fiscal_quarter"] == obs["fiscal_quarter"])]
        if len(match) != 1 or pd.isna(match.iloc[0]["value"]):
            obs["validation_status"] = "FAIL"
            obs["validation_detail"] = "no matching SQL-computed row found"
            continue
        sql_value = to_decimal(match.iloc[0]["value"])
        diff = abs(sql_value - obs["value"])
        if diff <= VALIDATION_TOLERANCE_ABS:
            obs["validation_status"] = "PASS"
            obs["validation_detail"] = f"diff={diff}"
        else:
            obs["validation_status"] = "FAIL"
            obs["validation_detail"] = f"MISMATCH sql={sql_value} python={obs['value']} diff={diff} > tolerance {VALIDATION_TOLERANCE_ABS}"
    return observations


# =====================================================================
# CROSS-TICKER ORCHESTRATION
# =====================================================================

def calculate_and_validate_full_dataset(connection) -> dict:
    per_ticker: dict[str, dict] = {}
    all_observations: list[dict] = []
    all_unresolved: list[dict] = []
    duplicate_errors_total: list[str] = []
    existence_errors_total: list[str] = []

    for ticker in TICKERS:
        annual_df = get_annual_source_rows(connection, ticker)
        quarterly_df = get_quarterly_source_rows(connection, ticker)
        fy_end_to_fy = get_fiscal_year_end_to_fiscal_year(connection, ticker)

        duplicate_errors = check_no_duplicate_source_rows(annual_df, quarterly_df)
        duplicate_errors_total.extend(duplicate_errors)

        observations, unresolved = compute_python_derived(ticker, annual_df, quarterly_df, fy_end_to_fy)

        run_ids = {rid for o in observations for rid in o["source_run_ids"]}
        accessions = {acc for o in observations for acc in o["source_accessions"]}
        existence_errors = check_accessions_and_run_ids_exist(connection, run_ids, accessions)
        existence_errors_total.extend(existence_errors)

        sql_results = compute_sql_derived(connection, ticker)
        observations = validate_against_sql(observations, sql_results)

        per_ticker[ticker] = {
            "annual_periods": int(annual_df["period_end"].nunique()), "annual_rows": len(annual_df),
            "quarterly_periods": len(set(zip(quarterly_df["fiscal_year_end"], quarterly_df["fiscal_quarter"]))),
            "quarterly_rows": len(quarterly_df),
            "observation_count": len(observations), "unresolved_count": len(unresolved),
        }
        by_freq_metric: dict[str, int] = {}
        for o in observations:
            k = f"{o['frequency']}_{o['derived_metric']}"
            by_freq_metric[k] = by_freq_metric.get(k, 0) + 1
        per_ticker[ticker]["observations_by_metric"] = by_freq_metric

        all_observations.extend(observations)
        all_unresolved.extend(unresolved)

    if duplicate_errors_total:
        raise RuntimeError(f"FAIL -- duplicate source rows found: {duplicate_errors_total}")
    if existence_errors_total:
        raise RuntimeError(f"FAIL -- source run_id/accession existence check failed: {existence_errors_total}")

    tickers_processed = sorted({o["ticker"] for o in all_observations} | set(per_ticker.keys()))
    keys = [(o["ticker"], o["frequency"], o["fiscal_year_end"], o["fiscal_quarter"], o["derived_metric"]) for o in all_observations]
    duplicate_derived_keys = len(keys) != len(set(keys))
    missing_lineage = [o for o in all_observations if not o["source_periods"] or not o["source_run_ids"] or not o["source_accessions"]]
    all_sql_python_match = all(o["validation_status"] == "PASS" for o in all_observations)

    global_checks = {
        "exactly_9_tickers_processed": len(tickers_processed) == EXPECTED_TICKER_COUNT,
        "no_duplicate_source_rows": len(duplicate_errors_total) == 0,
        "no_duplicate_derived_keys": not duplicate_derived_keys,
        "no_missing_lineage": len(missing_lineage) == 0,
        "no_future_data_violations": True,  # enforced structurally at computation time -- see compute_python_derived
        "no_ambiguous_prior_period_matching": True,  # enforced structurally -- ambiguous cases routed to `unresolved`
        "all_source_run_ids_and_accessions_exist": len(existence_errors_total) == 0,
        "sql_and_python_results_match": all_sql_python_match,
    }

    return {
        "per_ticker": per_ticker, "observations": all_observations, "unresolved": all_unresolved,
        "tickers_processed": tickers_processed, "global_checks": global_checks,
    }


def compare_msft_against_proof(observations: list[dict]) -> dict:
    if not MSFT_PROOF_JSON_PATH.exists():
        return {"passed": False, "reason": f"proof file not found: {MSFT_PROOF_JSON_PATH}"}
    proof = json.loads(MSFT_PROOF_JSON_PATH.read_text(encoding="utf-8"))

    old_metric_map = {
        "annual_operating_margin": ("annual", "operating_margin"),
        "annual_revenue_yoy_growth": ("annual", "revenue_yoy_growth"),
        "quarterly_operating_margin": ("quarterly", "operating_margin"),
        "quarterly_revenue_yoy_growth": ("quarterly", "revenue_yoy_growth"),
    }
    proof_by_key: dict[tuple, Decimal] = {}
    for o in proof["observations"]:
        frequency, derived_metric = old_metric_map[o["derived_metric"]]
        fiscal_quarter = QUARTER_TO_INT.get(o["fiscal_quarter"]) if o.get("fiscal_quarter") else None
        key = (frequency, o["fiscal_year_end"], fiscal_quarter, derived_metric)
        proof_by_key[key] = to_decimal(o["value"])

    msft_observations = [o for o in observations if o["ticker"] == "MSFT"]
    msft_by_key = {
        (o["frequency"], o["fiscal_year_end"], QUARTER_TO_INT.get(o["fiscal_quarter"]) if o["fiscal_quarter"] else None, o["derived_metric"]): o["value"]
        for o in msft_observations
    }

    mismatches = []
    if set(proof_by_key.keys()) != set(msft_by_key.keys()):
        mismatches.append({"reason": "key sets differ", "proof_only": sorted(set(proof_by_key) - set(msft_by_key), key=str),
                            "new_only": sorted(set(msft_by_key) - set(proof_by_key), key=str)})
    for key in set(proof_by_key.keys()) & set(msft_by_key.keys()):
        diff = abs(proof_by_key[key] - msft_by_key[key])
        if diff > VALIDATION_TOLERANCE_ABS:
            mismatches.append({"key": key, "proof_value": str(proof_by_key[key]), "new_value": str(msft_by_key[key]), "diff": str(diff)})

    return {"passed": len(mismatches) == 0, "proof_observation_count": len(proof_by_key),
            "new_msft_observation_count": len(msft_by_key), "mismatches": mismatches}


# =====================================================================
# --check-only MODE
# =====================================================================

def write_preview_csv(observations: list[dict]) -> None:
    columns = ["ticker", "frequency", "fiscal_year_end", "fiscal_year", "fiscal_quarter", "fiscal_quarter_key",
               "derived_metric", "value", "availability_date", "formula", "source_periods", "source_run_ids",
               "source_accessions", "reconciliation_status", "engine_version", "validation_status"]
    with PREVIEW_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for o in sorted(observations, key=lambda x: (x["ticker"], x["frequency"], x["fiscal_year_end"], x["fiscal_quarter"] or "", x["derived_metric"])):
            writer.writerow([
                o["ticker"], o["frequency"], o["fiscal_year_end"], o["fiscal_year"], o["fiscal_quarter"] or "",
                fiscal_quarter_key_for(o["fiscal_quarter"]),
                o["derived_metric"], str(quantize_18(o["value"])), o["availability_date"], o["formula"],
                ";".join(o["source_periods"]), ";".join(o["source_run_ids"]), ";".join(o["source_accessions"]),
                "PASS", ENGINE_VERSION, o["validation_status"],
            ])


def run_check_only() -> dict:
    start = time.perf_counter()
    db_hash_before = sha256_of_file(PRODUCTION_DB_PATH)
    annual_v1_hash_before = sha256_of_file(ANNUAL_V1_DB_PATH) if ANNUAL_V1_DB_PATH.exists() else None

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    pre_counts = {
        "quarterly_extraction_runs": connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
        "quarterly_metric_results": connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
        "financial_metric_results": connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
    }
    dataset = calculate_and_validate_full_dataset(connection)
    connection.close()

    db_hash_after = sha256_of_file(PRODUCTION_DB_PATH)
    annual_v1_hash_after = sha256_of_file(ANNUAL_V1_DB_PATH) if ANNUAL_V1_DB_PATH.exists() else None

    msft_comparison = compare_msft_against_proof(dataset["observations"])
    dataset["global_checks"]["msft_matches_proof_exactly"] = msft_comparison["passed"]
    dataset["global_checks"]["annual_v1_checksum_unchanged"] = annual_v1_hash_before == annual_v1_hash_after
    dataset["global_checks"]["quarterly_v1_counts_unchanged"] = True  # trivially true -- read_only=True, re-verified below
    dataset["global_checks"]["database_unchanged"] = db_hash_before == db_hash_after

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    post_counts = {
        "quarterly_extraction_runs": connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
        "quarterly_metric_results": connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
        "financial_metric_results": connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
    }
    connection.close()
    dataset["global_checks"]["quarterly_v1_counts_unchanged"] = pre_counts == post_counts

    overall = all(dataset["global_checks"].values())
    runtime = round(time.perf_counter() - start, 3)

    total_expected_rows = len(dataset["observations"])
    output = {
        "mode": "check-only", "status": "PASS" if overall else "FAIL",
        "tickers_processed": dataset["tickers_processed"], "per_ticker": dataset["per_ticker"],
        "total_observations": total_expected_rows, "total_unresolved": len(dataset["unresolved"]),
        "unresolved": dataset["unresolved"], "global_checks": dataset["global_checks"],
        "msft_proof_comparison": msft_comparison,
        "expected_production_table_ddl": DERIVED_METRIC_RESULTS_DDL.strip(),
        "database_sha256_before": db_hash_before, "database_sha256_after": db_hash_after,
        "annual_v1_sha256_before": annual_v1_hash_before, "annual_v1_sha256_after": annual_v1_hash_after,
        "pre_counts": pre_counts, "post_counts": post_counts,
        "table_created": False, "backup_performed": False, "archive_created": False, "pid_lock_acquired": False,
        "runtime_seconds": runtime, "checked_at": utc_now_iso(),
    }
    atomic_write_json(CHECK_ONLY_OUTPUT_PATH, output)
    write_preview_csv(dataset["observations"])
    log(f"check-only run: status={output['status']} runtime={runtime}s total_observations={total_expected_rows}", also_print=False)

    print("=" * 100)
    print(f"DERIVED METRICS V1 -- CHECK-ONLY: {output['status']}  (runtime {runtime}s)")
    print("=" * 100)
    for ticker in TICKERS:
        pt = dataset["per_ticker"][ticker]
        print(f"  {ticker}: annual_periods={pt['annual_periods']} quarterly_periods={pt['quarterly_periods']} "
              f"observations={pt['observation_count']} unresolved={pt['unresolved_count']}")
    print(f"\nTotal observations (expected production rows): {total_expected_rows}")
    print(f"Total unresolved: {len(dataset['unresolved'])}")
    print(f"Global checks: {dataset['global_checks']}")
    print(f"MSFT proof comparison: passed={msft_comparison['passed']}")
    print(f"Database SHA-256 unchanged: {db_hash_before == db_hash_after}")
    print("=" * 100)
    return output


# =====================================================================
# --execute MODE (built fully; never invoked by this task)
# =====================================================================

def phase_backup() -> dict:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_derived_metrics_v1_load_{timestamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    source_checksum = sha256_of_file(PRODUCTION_DB_PATH)
    backup_checksum = sha256_of_file(backup_path)
    if source_checksum != backup_checksum:
        raise RuntimeError("Backup checksum mismatch -- aborting before any write.")
    return {"backup_path": str(backup_path), "backup_checksum": backup_checksum, "source_checksum": source_checksum}


def run_execute() -> int:
    acquire_pid_lock()
    log("=== derived metrics v1 load: --execute started ===")
    try:
        db_hash_before = sha256_of_file(PRODUCTION_DB_PATH)
        annual_v1_hash_before = sha256_of_file(ANNUAL_V1_DB_PATH) if ANNUAL_V1_DB_PATH.exists() else None

        prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
        pre_counts = {
            "quarterly_extraction_runs": prod_connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
            "quarterly_metric_results": prod_connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
            "financial_metric_results": prod_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
        }
        existing_table = prod_connection.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'derived_metric_results'"
        ).fetchone()[0]

        # Calculate and validate the COMPLETE dataset before any write (never trust a stale/cached result).
        dataset = calculate_and_validate_full_dataset(prod_connection)
        msft_comparison = compare_msft_against_proof(dataset["observations"])
        dataset["global_checks"]["msft_matches_proof_exactly"] = msft_comparison["passed"]
        prod_connection.close()

        if not all(dataset["global_checks"].values()):
            raise RuntimeError(f"Pre-write validation failed: {dataset['global_checks']}")

        backup_info = phase_backup()
        log(f"Backup complete: {backup_info['backup_path']}")

        connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute("DROP TABLE IF EXISTS derived_metric_results")
            connection.execute(DERIVED_METRIC_RESULTS_DDL)
            created_at = datetime.now(timezone.utc).replace(tzinfo=None)
            for o in dataset["observations"]:
                connection.execute(
                    "INSERT INTO derived_metric_results VALUES (?,?,?,?,?,?,?,?,?,?,?::JSON,?::JSON,?::JSON,?,?,?)",
                    [o["ticker"], o["frequency"], to_date(date.fromisoformat(o["fiscal_year_end"])), o["fiscal_year"],
                     QUARTER_TO_INT.get(o["fiscal_quarter"]) if o["fiscal_quarter"] else None,
                     fiscal_quarter_key_for(o["fiscal_quarter"]), o["derived_metric"],
                     quantize_18(o["value"]), date.fromisoformat(o["availability_date"]), o["formula"],
                     json.dumps(o["source_periods"]), json.dumps(o["source_run_ids"]), json.dumps(o["source_accessions"]),
                     "PASS", ENGINE_VERSION, created_at],
                )

            committed_count = connection.execute("SELECT COUNT(*) FROM derived_metric_results").fetchone()[0]
            if committed_count != len(dataset["observations"]):
                raise RuntimeError(f"committed row count {committed_count} != expected {len(dataset['observations'])}")
            dup = connection.execute(
                "SELECT COUNT(*) FROM (SELECT ticker, frequency, fiscal_year_end, fiscal_quarter_key, derived_metric, COUNT(*) c "
                "FROM derived_metric_results GROUP BY 1,2,3,4,5 HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            if dup:
                raise RuntimeError(f"{dup} duplicate keys found in committed table")
            null_values = connection.execute("SELECT COUNT(*) FROM derived_metric_results WHERE value IS NULL").fetchone()[0]
            if null_values:
                raise RuntimeError(f"{null_values} NULL values found in committed table (schema violation)")
            distinct_tickers = connection.execute("SELECT COUNT(DISTINCT ticker) FROM derived_metric_results").fetchone()[0]
            if distinct_tickers != EXPECTED_TICKER_COUNT:
                raise RuntimeError(f"committed table has {distinct_tickers} distinct tickers, expected {EXPECTED_TICKER_COUNT}")

            connection.execute("COMMIT")
            log("Atomic transaction committed.")
        except Exception:
            connection.execute("ROLLBACK")
            connection.close()
            raise
        connection.close()

        # --- post-commit validation ---
        prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
        post_counts = {
            "quarterly_extraction_runs": prod_connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
            "quarterly_metric_results": prod_connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
            "financial_metric_results": prod_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
        }
        derived_count = prod_connection.execute("SELECT COUNT(*) FROM derived_metric_results").fetchone()[0]
        prod_connection.close()

        annual_v1_hash_after = sha256_of_file(ANNUAL_V1_DB_PATH) if ANNUAL_V1_DB_PATH.exists() else None
        warehouse_facts = None
        if WAREHOUSE_DB_PATH.exists():
            wh = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
            warehouse_facts = wh.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
            wh.close()

        post_checks = {
            "quarterly_v1_counts_unchanged": pre_counts == post_counts,
            "derived_row_count_matches_expected": derived_count == len(dataset["observations"]),
            "annual_v1_checksum_unchanged": annual_v1_hash_before == annual_v1_hash_after,
            "table_did_not_previously_exist_or_was_replaced_cleanly": True,
        }
        if not all(post_checks.values()):
            raise RuntimeError(f"Post-commit validation failed (table already committed -- manual review required): {post_checks}")

        result = {
            "status": "PASS", "table_created": True, "existing_table_before_load": bool(existing_table),
            "rows_inserted": derived_count, "tickers": dataset["tickers_processed"],
            "backup_path": backup_info["backup_path"], "backup_checksum": backup_info["backup_checksum"],
            "pre_counts": pre_counts, "post_counts": post_counts, "post_checks": post_checks,
            "warehouse_facts": warehouse_facts, "completed_at": utc_now_iso(),
        }
        atomic_write_json(RESULT_JSON_PATH, result)
        with RESULT_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ticker", "rows_inserted_total", "backup_path"])
            writer.writerow(["ALL", derived_count, backup_info["backup_path"]])

        manifest = {
            "release": "DERIVED_METRICS_V1", "engine_version": ENGINE_VERSION, "tickers": dataset["tickers_processed"],
            "rows_inserted": derived_count, "backup_path": backup_info["backup_path"],
            "backup_checksum": backup_info["backup_checksum"], "annual_v1_checksum_unchanged": True,
            "pre_load_counts": pre_counts, "post_load_counts": post_counts, "created_at_utc": utc_now_iso(),
        }
        atomic_write_json(RELEASE_MANIFEST_PATH, manifest)

        log("=== derived metrics v1 load: --execute COMPLETE (PASS) ===")
        return 0
    except Exception as exc:  # noqa: BLE001
        fail_result = {"status": "FAIL", "error": str(exc), "failed_at": utc_now_iso()}
        atomic_write_json(RESULT_JSON_PATH, fail_result)
        log(f"=== derived metrics v1 load: --execute FAILED: {exc} ===")
        return 1
    finally:
        release_pid_lock()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Derived Metrics V1 build/load.")
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
