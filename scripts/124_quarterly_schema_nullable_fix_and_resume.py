"""
Fixes the real bug identified in the 45-company-year quarterly batch
report (docs/LAST_CLAUDE_REPORT.md): quarterly_metric_results declared
concept_qname / reconciliation_difference / permitted_difference as
NOT NULL, which rejected the honest NULL placeholders scripts/123's new
fallback loader writes for genuinely-unresolved metrics. Three phases,
run in one restartable script:

  PHASE 1 — backup ai_stock_agent.duckdb, verify checksum, record
  pre-counts, then ALTER TABLE ... ALTER COLUMN ... DROP NOT NULL on the
  three columns (DuckDB 1.5.5 supports this directly — confirmed by a
  throwaway in-memory test before writing this script; no replacement
  table needed). Idempotent: skips the ALTER if already nullable (safe
  to rerun).

  PHASE 2 — proves the fix on exactly one company-year, GOOGL FY2021,
  reusing its already-locked/warehoused filings (no download, no Arelle
  reload). Stops here (no Phase 3) if this does not commit and validate
  cleanly.

  PHASE 3 — only runs if Phase 2 passed. Resumes the remaining 21
  schema-blocked company-years (reuse-only, same as Phase 2) plus the 5
  network-retry company-years (bounded-backoff re-lock, max 3 attempts,
  >=2s between attempts, reusing any already-complete partial download
  and skipping re-fetch of non-essential rendering-preview files).

Reuses scripts/107/118/120 unmodified via importlib, and copies the same
Arelle warehouse-loading internals already proven in scripts/108/110/112/
115/121/123 (needed here only for the 5 network-retry filings that were
never successfully warehoused). The REVIEW_REQUIRED-tolerant loader
(`load_company_year_allowing_review_required`) is copied from scripts/123
unchanged in logic — only the schema it targets has changed (this script
does not modify that function's behavior, only the table it writes to).

Checkpointing: writes data/quarterly_9companies_5years_batch_result.json
and data/quarterly_9companies_5years_batch_progress.log after EVERY
filing lock/warehouse step and after every company-year (not just every
company-year, as scripts/123 did) — plus a background heartbeat thread
that touches a heartbeat file at least once every 30 seconds regardless
of what the main thread is blocked on.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "arelle_cache"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
ANNUAL_V1_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"
ANNUAL_V1_MANIFEST_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1_manifest.json"
BACKUPS_DIR = DATA_DIR / "database" / "backups"
LOCKED_FILINGS_DIR = DATA_DIR / "sec_filings_locked"

SCRIPT_NAME = "124_quarterly_schema_nullable_fix_and_resume.py"
CHILD_PROCESS_TIMEOUT_SECONDS = 300
INTERNET_TIMEOUT_SECONDS = 20
STANDARD_NAMESPACE_PATTERN = r"fasb\.org|xbrl\.sec\.gov|xbrl\.org|w3\.org"
MAX_LOCK_ATTEMPTS = 3
MIN_SECONDS_BETWEEN_ATTEMPTS = 2
INACTIVITY_STOP_SECONDS = 600
HEARTBEAT_INTERVAL_SECONDS = 30

EXPECTED_ANNUAL_V1_CHECKSUM = "e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814"
EXPECTED_PRE_FMR_COUNT = 900
EXPECTED_PRE_RUNS_COUNT = 18
EXPECTED_PRE_QMR_COUNT = 432

CHECKPOINT_PATH = DATA_DIR / "quarterly_9companies_5years_batch_result.json"
PROGRESS_LOG_PATH = DATA_DIR / "quarterly_9companies_5years_batch_progress.log"
HEARTBEAT_PATH = DATA_DIR / "quarterly_9companies_5years_batch_heartbeat.txt"

RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

PROOF_COMPANY_YEAR = ("GOOGL", "2021-12-31")

SCHEMA_BLOCKED_LIST = [
    ("CRWD", "2023-01-31"), ("CRWD", "2024-01-31"), ("CRWD", "2026-01-31"),
    ("GOOGL", "2022-12-31"), ("GOOGL", "2023-12-31"), ("GOOGL", "2025-12-31"),
    ("META", "2021-12-31"), ("META", "2022-12-31"), ("META", "2023-12-31"), ("META", "2024-12-31"),
    ("MU", "2021-09-02"), ("MU", "2022-09-01"), ("MU", "2023-08-31"), ("MU", "2024-08-29"), ("MU", "2025-08-28"),
    ("NVDA", "2020-01-26"), ("NVDA", "2021-01-31"), ("NVDA", "2022-01-30"), ("NVDA", "2023-01-29"), ("NVDA", "2024-01-28"),
    ("PANW", "2021-07-31"),
]
NETWORK_RETRY_LIST = [
    ("CRWD", "2022-01-31"), ("PANW", "2022-07-31"), ("PANW", "2023-07-31"),
    ("PANW", "2024-07-31"), ("PANW", "2025-07-31"),
]

METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense",
           "operating_cash_flow", "capex"]
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_DIR / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


s107 = _load_module("s107", "107_download_accession_locked_filing_any_form.py")
s118 = _load_module("s118", "118_quarterly_extraction_engine.py")
s120 = _load_module("s120", "120_quarterly_production_schema_load.py")


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# =====================================================================
# HEARTBEAT — background thread, independent of what the main thread is
# blocked on (network call, Arelle child process, etc.)
# =====================================================================

_heartbeat_stop = threading.Event()


def _heartbeat_loop(state: dict) -> None:
    while not _heartbeat_stop.is_set():
        HEARTBEAT_PATH.write_text(
            json.dumps({"utc": datetime.now(timezone.utc).isoformat(), "current": state.get("current")}),
            encoding="utf-8",
        )
        _heartbeat_stop.wait(HEARTBEAT_INTERVAL_SECONDS)


def start_heartbeat(state: dict) -> threading.Thread:
    thread = threading.Thread(target=_heartbeat_loop, args=(state,), daemon=True)
    thread.start()
    return thread


# =====================================================================
# PHASE 1 — BACKUP + SCHEMA FIX
# =====================================================================

def phase1_backup_and_fix_schema() -> dict:
    print("=" * 100)
    print("PHASE 1 — BACKUP AND SCHEMA FIX")
    print("=" * 100)

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    fmr_count = connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    runs_count = connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0]
    qmr_count = connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
    connection.close()

    print(f"Pre-migration counts: financial_metric_results={fmr_count} (expected {EXPECTED_PRE_FMR_COUNT}), "
          f"quarterly_extraction_runs={runs_count} (expected {EXPECTED_PRE_RUNS_COUNT}), "
          f"quarterly_metric_results={qmr_count} (expected {EXPECTED_PRE_QMR_COUNT})")

    if fmr_count != EXPECTED_PRE_FMR_COUNT or runs_count != EXPECTED_PRE_RUNS_COUNT or qmr_count != EXPECTED_PRE_QMR_COUNT:
        raise RuntimeError(
            f"Pre-migration counts do not match the expected verified state "
            f"({fmr_count}/{runs_count}/{qmr_count} vs {EXPECTED_PRE_FMR_COUNT}/{EXPECTED_PRE_RUNS_COUNT}/{EXPECTED_PRE_QMR_COUNT}). "
            "Refusing to proceed with an unverified starting state."
        )

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_quarterly_nullable_fix_{RUN_TIMESTAMP}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    source_checksum = sha256_of_file(PRODUCTION_DB_PATH)
    backup_checksum = sha256_of_file(backup_path)
    print(f"Backup: {backup_path}")
    print(f"  source checksum: {source_checksum}")
    print(f"  backup checksum: {backup_checksum}")
    if source_checksum != backup_checksum:
        raise RuntimeError("Backup checksum does not match source — aborting before any schema change.")

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
    schema_before = connection.execute("DESCRIBE quarterly_metric_results").fetchall()

    already_nullable = all(
        row[3] == "YES" for row in schema_before
        if row[0] in ("concept_qname", "reconciliation_difference", "permitted_difference")
    )

    if already_nullable:
        print("Columns are already nullable — schema fix already applied (idempotent skip).")
        schema_after = schema_before
    else:
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute("ALTER TABLE quarterly_metric_results ALTER COLUMN concept_qname DROP NOT NULL")
            connection.execute("ALTER TABLE quarterly_metric_results ALTER COLUMN reconciliation_difference DROP NOT NULL")
            connection.execute("ALTER TABLE quarterly_metric_results ALTER COLUMN permitted_difference DROP NOT NULL")

            post_qmr_count = connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
            post_fmr_count = connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
            dup = connection.execute(
                "SELECT run_id, metric_name, fiscal_quarter, COUNT(*) c FROM quarterly_metric_results "
                "GROUP BY run_id, metric_name, fiscal_quarter HAVING COUNT(*) > 1"
            ).fetchall()

            if post_qmr_count != EXPECTED_PRE_QMR_COUNT or post_fmr_count != EXPECTED_PRE_FMR_COUNT or dup:
                connection.execute("ROLLBACK")
                raise RuntimeError(
                    f"Post-ALTER validation failed (qmr={post_qmr_count}, fmr={post_fmr_count}, dup={dup}) — rolled back."
                )

            connection.execute("COMMIT")
            print("ALTER COLUMN ... DROP NOT NULL applied and committed for all 3 columns.")
        except Exception:
            connection.execute("ROLLBACK")
            connection.close()
            raise

        schema_after = connection.execute("DESCRIBE quarterly_metric_results").fetchall()

    final_qmr_count = connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
    final_fmr_count = connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    connection.close()

    actual_v1_checksum = sha256_of_file(ANNUAL_V1_DB_PATH)
    if actual_v1_checksum != EXPECTED_ANNUAL_V1_CHECKSUM:
        raise RuntimeError("Annual V1 checksum changed unexpectedly — this must never happen. Stopping immediately.")

    print(f"Post-migration: quarterly_metric_results={final_qmr_count}, financial_metric_results={final_fmr_count}, "
          f"Annual V1 checksum unchanged={actual_v1_checksum == EXPECTED_ANNUAL_V1_CHECKSUM}")

    return {
        "backup_path": str(backup_path), "source_checksum": source_checksum, "backup_checksum": backup_checksum,
        "schema_before": [list(r) for r in schema_before], "schema_after": [list(r) for r in schema_after],
        "pre_counts": {"financial_metric_results": fmr_count, "quarterly_extraction_runs": runs_count, "quarterly_metric_results": qmr_count},
        "post_counts": {"financial_metric_results": final_fmr_count, "quarterly_metric_results": final_qmr_count},
        "annual_v1_checksum_unchanged": True,
    }


# =====================================================================
# FISCAL-YEAR / FILING RESOLUTION (unchanged logic from scripts/121/123)
# =====================================================================

def compute_prior_fiscal_year_end(fiscal_year_end: str) -> str:
    d = date.fromisoformat(fiscal_year_end)
    try:
        prior = d.replace(year=d.year - 1)
    except ValueError:
        prior = d.replace(year=d.year - 1, day=28)
    return prior.isoformat()


def resolve_quarters_for_fiscal_year(filings_df: pd.DataFrame, fiscal_year_end: str) -> dict:
    prior_fiscal_year_end = compute_prior_fiscal_year_end(fiscal_year_end)
    quarterly = filings_df[filings_df["form"] == "10-Q"].copy()
    quarterly = quarterly[
        (quarterly["reportDate"] > prior_fiscal_year_end) & (quarterly["reportDate"] <= fiscal_year_end)
    ].sort_values("reportDate")

    if len(quarterly) != 3:
        raise RuntimeError(
            f"Expected exactly 3 10-Q filings with reportDate in "
            f"({prior_fiscal_year_end}, {fiscal_year_end}], found {len(quarterly)}."
        )

    labels = ["Q1", "Q2", "Q3"]
    result = {}
    for label, (_, row) in zip(labels, quarterly.iterrows()):
        result[label] = {
            "form": "10-Q", "report_date": str(row["reportDate"]),
            "filing_date": str(row["filingDate"]), "accession_number": str(row["accessionNumber"]),
            "primary_document": str(row["primaryDocument"]),
        }
    return result


def resolve_fy_10k(prod_connection, warehouse_connection, ticker: str, fiscal_year_end: str) -> dict:
    row = prod_connection.execute(
        "SELECT accession_number, filing_date FROM sec_filings WHERE ticker = ? AND form = '10-K' AND report_date = ?",
        [ticker, fiscal_year_end],
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No 10-K found in sec_filings for {ticker} report_date={fiscal_year_end}.")
    accession_number, filing_date = row

    run = warehouse_connection.execute(
        "SELECT status FROM warehouse_runs WHERE accession_number = ? AND status = 'PASS'", [accession_number]
    ).fetchone()
    fact_count, context_count, unit_count = warehouse_connection.execute(
        "SELECT (SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?), "
        "(SELECT COUNT(*) FROM xbrl_contexts WHERE accession_number = ?), "
        "(SELECT COUNT(*) FROM xbrl_units WHERE accession_number = ?)",
        [accession_number, accession_number, accession_number],
    ).fetchone()
    if run is None or fact_count == 0 or context_count == 0 or unit_count == 0:
        raise RuntimeError(f"10-K {accession_number} for {ticker} {fiscal_year_end} is not warehouse PASS.")

    return {"form": "10-K", "report_date": fiscal_year_end, "filing_date": str(filing_date),
            "accession_number": accession_number}


def is_already_locked(ticker: str, accession_number: str) -> bool:
    accession_compact = accession_number.replace("-", "")
    manifest_path = LOCKED_FILINGS_DIR / ticker / accession_compact / "locked_filing_manifest.json"
    return manifest_path.exists()


def is_already_warehoused(accession_number: str) -> tuple[bool, dict]:
    connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    run = connection.execute(
        "SELECT status FROM warehouse_runs WHERE accession_number = ? AND status = 'PASS'", [accession_number]
    ).fetchone()
    fact_count, context_count, unit_count = connection.execute(
        "SELECT (SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?), "
        "(SELECT COUNT(*) FROM xbrl_contexts WHERE accession_number = ?), "
        "(SELECT COUNT(*) FROM xbrl_units WHERE accession_number = ?)",
        [accession_number, accession_number, accession_number],
    ).fetchone()
    connection.close()
    is_complete = run is not None and fact_count > 0 and context_count > 0 and unit_count > 0
    return is_complete, {"fact_count": fact_count, "context_count": context_count, "unit_count": unit_count}


def lock_10q_if_missing(ticker: str, cik: int, company_name: str, quarter_info: dict) -> str:
    accession_number = quarter_info["accession_number"]
    filing_base_url = s107.build_filing_base_url(cik=cik, accession_number=accession_number)
    accession_compact = accession_number.replace("-", "")
    output_directory = LOCKED_FILINGS_DIR / ticker / accession_compact

    filing_index = s107.load_filing_index(filing_base_url)
    index_items = s107.get_index_items(filing_index)
    downloaded_records = s107.download_filing_files(
        filing_base_url=filing_base_url, output_directory=output_directory, index_items=index_items
    )
    filing_series = pd.Series({
        "form": quarter_info["form"], "reportDate": quarter_info["report_date"],
        "filingDate": quarter_info["filing_date"], "accessionNumber": accession_number,
        "primaryDocument": quarter_info["primary_document"],
    })
    s107.save_manifests(
        ticker=ticker, company_name=company_name, cik=cik, filing=filing_series,
        filing_base_url=filing_base_url, output_directory=output_directory,
        downloaded_records=downloaded_records,
    )
    return "NEWLY_LOCKED"


def is_locked_package_sufficient_for_arelle(output_directory: Path, primary_document: str) -> bool:
    primary_path = output_directory / primary_document
    if not primary_path.exists():
        return False
    return any(output_directory.glob("*.xsd"))


def write_partial_manifest(ticker, cik, company_name, quarter_info, output_directory: Path) -> None:
    primary_document = quarter_info["primary_document"]
    primary_document_path = output_directory / primary_document
    filing_manifest = {
        "ticker": ticker, "company_name": company_name, "cik": cik,
        "form": quarter_info["form"], "report_date": quarter_info["report_date"],
        "filing_date": quarter_info["filing_date"], "accession_number": quarter_info["accession_number"],
        "primary_document": primary_document, "primary_document_path": str(primary_document_path.resolve()),
        "filing_base_url": s107.build_filing_base_url(cik=cik, accession_number=quarter_info["accession_number"]),
        "output_directory": str(output_directory.resolve()),
        "downloaded_file_count": len(list(output_directory.iterdir())), "sec_user_agent": s107.USER_AGENT,
    }
    (output_directory / "locked_filing_manifest.json").write_text(
        json.dumps(filing_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def lock_10q_with_retry(ticker: str, cik: int, company_name: str, quarter_info: dict) -> str:
    """Reuse-first, then reuse-partial-if-sufficient (never re-fetches
    rendering-preview files Arelle doesn't need when the real package is
    already complete), then bounded-retry fresh download (max 3 attempts,
    exponential backoff, >=2s between attempts)."""

    accession_number = quarter_info["accession_number"]
    if is_already_locked(ticker, accession_number):
        return "REUSED"

    accession_compact = accession_number.replace("-", "")
    output_directory = LOCKED_FILINGS_DIR / ticker / accession_compact
    if output_directory.exists() and is_locked_package_sufficient_for_arelle(output_directory, quarter_info["primary_document"]):
        write_partial_manifest(ticker, cik, company_name, quarter_info, output_directory)
        return "REUSED_PARTIAL_SUFFICIENT"

    last_error: Exception | None = None
    for attempt in range(1, MAX_LOCK_ATTEMPTS + 1):
        try:
            lock_10q_if_missing(ticker, cik, company_name, quarter_info)
            return "NEWLY_LOCKED" if attempt == 1 else f"NEWLY_LOCKED_AFTER_RETRY_{attempt}"
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"    lock attempt {attempt}/{MAX_LOCK_ATTEMPTS} failed for {ticker}/{accession_number}: {exc}")
            if attempt < MAX_LOCK_ATTEMPTS:
                backoff_seconds = MIN_SECONDS_BETWEEN_ATTEMPTS * (2 ** (attempt - 1))
                time.sleep(backoff_seconds)

    raise RuntimeError(f"Failed to lock {ticker}/{accession_number} after {MAX_LOCK_ATTEMPTS} attempts: {last_error}")


# --- Arelle warehouse-loading internals (unchanged logic, copied from scripts/121/123) ---

def _role_definition(model_xbrl, role_uri):
    role_types = model_xbrl.roleTypes.get(role_uri, [])
    for role_type in role_types:
        definition = getattr(role_type, "definition", "")
        if definition:
            return str(definition)
    return ""


def _dim_value_repr(dim_value):
    member = getattr(dim_value, "memberQname", None)
    if member is not None:
        return str(member)
    typed_member = getattr(dim_value, "typedMember", None)
    if typed_member is not None:
        return str(getattr(typed_member, "text", typed_member))
    return str(dim_value)


def _context_period_fields(context):
    is_instant = bool(getattr(context, "isInstantPeriod", False))
    is_duration = bool(getattr(context, "isStartEndPeriod", False))
    period_start = None
    period_end = None
    instant_date = None
    if is_duration:
        start_dt = context.startDatetime
        end_dt = context.endDatetime
        if start_dt is not None:
            period_start = start_dt.date().isoformat()
        if end_dt is not None:
            period_end = (end_dt - timedelta(days=1)).date().isoformat()
    elif is_instant:
        instant_dt = context.instantDatetime
        if instant_dt is not None:
            instant_date = (instant_dt - timedelta(days=1)).date().isoformat()
    return {"period_start": period_start, "period_end": period_end, "instant_date": instant_date}


def extract_facts(model_xbrl, accession_number, source_document):
    records = []
    for fact_index, fact in enumerate(model_xbrl.facts):
        concept = getattr(fact, "concept", None)
        if concept is None:
            continue
        context = fact.context
        period_fields = _context_period_fields(context) if context is not None else {}
        dims = getattr(context, "qnameDims", {}) or {} if context is not None else {}
        dimensions_json = json.dumps({str(k): _dim_value_repr(v) for k, v in dims.items()}, ensure_ascii=False)
        is_nil = bool(getattr(fact, "isNil", False))
        value_raw = None if is_nil else fact.value
        value_numeric = None
        if not is_nil:
            try:
                value_numeric = float(fact.xValue)
            except (TypeError, ValueError):
                try:
                    value_numeric = float(fact.value)
                except (TypeError, ValueError):
                    value_numeric = None
        records.append({
            "accession_number": accession_number, "fact_index": fact_index,
            "concept_namespace": str(getattr(concept.qname, "namespaceURI", "")),
            "concept_local_name": str(getattr(concept.qname, "localName", "")),
            "concept_qname": str(concept.qname), "value_raw": value_raw, "value_numeric": value_numeric,
            "decimals": str(fact.decimals) if getattr(fact, "decimals", None) is not None else None,
            "precision": str(fact.precision) if getattr(fact, "precision", None) is not None else None,
            "unit_id": fact.unitID, "context_id": fact.contextID,
            "period_type": str(getattr(concept, "periodType", "")),
            "period_start": period_fields.get("period_start"), "period_end": period_fields.get("period_end"),
            "instant_date": period_fields.get("instant_date"), "dimensions_json": dimensions_json,
            "is_nil": is_nil, "source_document": source_document, "source_line": getattr(fact, "sourceline", None),
        })
    return pd.DataFrame(records)


def extract_contexts(model_xbrl, accession_number):
    records = []
    for context_id, context in model_xbrl.contexts.items():
        period_fields = _context_period_fields(context)
        entity_id_tuple = getattr(context, "entityIdentifier", None)
        entity_identifier = str(entity_id_tuple[1]) if entity_id_tuple else None
        seg_dims = getattr(context, "segDimValues", {}) or {}
        scen_dims = getattr(context, "scenDimValues", {}) or {}
        segment_json = json.dumps({str(k): _dim_value_repr(v) for k, v in seg_dims.items()}, ensure_ascii=False)
        scenario_json = json.dumps({str(k): _dim_value_repr(v) for k, v in scen_dims.items()}, ensure_ascii=False)
        all_dims = getattr(context, "qnameDims", {}) or {}
        dimensions_json = json.dumps({str(k): _dim_value_repr(v) for k, v in all_dims.items()}, ensure_ascii=False)
        records.append({
            "accession_number": accession_number, "context_id": str(context_id), "entity_identifier": entity_identifier,
            "period_start": period_fields.get("period_start"), "period_end": period_fields.get("period_end"),
            "instant_date": period_fields.get("instant_date"), "dimensions_json": dimensions_json,
            "scenario_json": scenario_json, "segment_json": segment_json,
        })
    return pd.DataFrame(records)


def extract_units(model_xbrl, accession_number):
    records = []
    for unit_id, unit in model_xbrl.units.items():
        measures = getattr(unit, "measures", None) or ((), ())
        numerator = measures[0] if len(measures) > 0 else ()
        denominator = measures[1] if len(measures) > 1 else ()
        records.append({
            "accession_number": accession_number, "unit_id": str(unit_id),
            "numerator_measures": ",".join(str(m) for m in numerator) or None,
            "denominator_measures": ",".join(str(m) for m in denominator) or None,
        })
    return pd.DataFrame(records)


def _walk_relationship_edges(relationship_set, role_uri, concept, edges, visited, extra_arc_attrs):
    concept_qname = str(getattr(concept, "qname", ""))
    for relationship in relationship_set.fromModelObject(concept):
        child = relationship.toModelObject
        if child is None:
            continue
        child_qname = str(getattr(child, "qname", ""))
        visit_key = (concept_qname, child_qname)
        edge = {"role_uri": role_uri, "parent_concept": concept_qname, "child_concept": child_qname,
                "order_value": float(getattr(relationship, "order", 0) or 0)}
        if extra_arc_attrs:
            edge["weight"] = float(getattr(relationship, "weight", 0) or 0)
        else:
            edge["preferred_label"] = str(getattr(relationship, "preferredLabel", "") or "")
        edges.append(edge)
        if visit_key in visited:
            continue
        visited.add(visit_key)
        _walk_relationship_edges(relationship_set, role_uri, child, edges, visited, extra_arc_attrs)


def extract_presentation_relationships(model_xbrl, accession_number):
    from arelle import XbrlConst
    records = []
    global_relationship_set = model_xbrl.relationshipSet(XbrlConst.parentChild)
    for role_uri in sorted(global_relationship_set.linkRoleUris):
        relationship_set = model_xbrl.relationshipSet(XbrlConst.parentChild, role_uri)
        role_definition = _role_definition(model_xbrl, role_uri)
        roots = sorted(relationship_set.rootConcepts, key=lambda c: str(getattr(c, "qname", "")))
        for root in roots:
            edges = []
            _walk_depth_first_presentation(relationship_set, role_uri, root, edges, set(), depth=0, parent_qname="")
            for edge in edges:
                edge["accession_number"] = accession_number
                edge["role_definition"] = role_definition
                records.append(edge)
    return pd.DataFrame(records)


def _walk_depth_first_presentation(relationship_set, role_uri, concept, records, visited, depth, parent_qname):
    concept_qname = str(getattr(concept, "qname", ""))
    visit_key = (parent_qname, concept_qname, depth)
    if visit_key in visited:
        return
    visited.add(visit_key)
    relationships = sorted(
        relationship_set.fromModelObject(concept),
        key=lambda r: (float(getattr(r, "order", 0) or 0), str(getattr(r.toModelObject, "qname", ""))),
    )
    for relationship in relationships:
        child = relationship.toModelObject
        if child is None:
            continue
        child_qname = str(getattr(child, "qname", ""))
        records.append({
            "role_uri": role_uri, "parent_concept": concept_qname, "child_concept": child_qname,
            "order_value": float(getattr(relationship, "order", 0) or 0),
            "preferred_label": str(getattr(relationship, "preferredLabel", "") or ""), "depth": depth + 1,
        })
        _walk_depth_first_presentation(relationship_set, role_uri, child, records, visited, depth + 1, concept_qname)


def extract_calculation_relationships(model_xbrl, accession_number):
    from arelle import XbrlConst
    records = []
    global_relationship_set = model_xbrl.relationshipSet(XbrlConst.summationItem)
    for role_uri in sorted(global_relationship_set.linkRoleUris):
        relationship_set = model_xbrl.relationshipSet(XbrlConst.summationItem, role_uri)
        role_definition = _role_definition(model_xbrl, role_uri)
        roots = sorted(relationship_set.rootConcepts, key=lambda c: str(getattr(c, "qname", "")))
        for root in roots:
            edges = []
            _walk_relationship_edges(relationship_set, role_uri, root, edges, set(), extra_arc_attrs=True)
            for edge in edges:
                edge["accession_number"] = accession_number
                edge["role_definition"] = role_definition
                records.append(edge)
    return pd.DataFrame(records)


def extract_definition_relationships(model_xbrl, accession_number):
    from arelle import XbrlConst
    arcroles = {
        "all": XbrlConst.all, "notAll": XbrlConst.notAll,
        "hypercubeDimension": XbrlConst.hypercubeDimension, "dimensionDomain": XbrlConst.dimensionDomain,
        "domainMember": XbrlConst.domainMember, "dimensionDefault": XbrlConst.dimensionDefault,
    }
    records = []
    for arcrole_name, arcrole_uri in arcroles.items():
        global_relationship_set = model_xbrl.relationshipSet(arcrole_uri)
        for role_uri in sorted(global_relationship_set.linkRoleUris):
            relationship_set = model_xbrl.relationshipSet(arcrole_uri, role_uri)
            role_definition = _role_definition(model_xbrl, role_uri)
            roots = sorted(relationship_set.rootConcepts, key=lambda c: str(getattr(c, "qname", "")))
            for root in roots:
                _extract_definition_edges_recursive(relationship_set, root, records, set(), accession_number, arcrole_name, role_uri, role_definition)
    return pd.DataFrame(records)


def _extract_definition_edges_recursive(relationship_set, concept, records, visited, accession_number, arcrole_name, role_uri, role_definition):
    concept_qname = str(getattr(concept, "qname", ""))
    for relationship in relationship_set.fromModelObject(concept):
        child = relationship.toModelObject
        if child is None:
            continue
        child_qname = str(getattr(child, "qname", ""))
        visit_key = (concept_qname, child_qname)
        records.append({
            "accession_number": accession_number, "arcrole": arcrole_name, "role_uri": role_uri,
            "role_definition": role_definition, "parent_concept": concept_qname, "child_concept": child_qname,
            "order_value": float(getattr(relationship, "order", 0) or 0),
            "closed_attr": bool(relationship.closed) if getattr(relationship, "closed", None) is not None else None,
            "context_element": getattr(relationship, "contextElement", None),
            "usable_attr": bool(relationship.usable) if getattr(relationship, "usable", None) is not None else None,
        })
        if visit_key in visited:
            continue
        visited.add(visit_key)
        _extract_definition_edges_recursive(relationship_set, child, records, visited, accession_number, arcrole_name, role_uri, role_definition)


def referenced_concept_qnames(facts_df, presentation_df, calculation_df, definition_df):
    referenced = set()
    if not facts_df.empty:
        referenced |= set(facts_df["concept_qname"].dropna().unique())
    for df in (presentation_df, calculation_df, definition_df):
        if df.empty:
            continue
        referenced |= set(df["parent_concept"].dropna().unique())
        referenced |= set(df["child_concept"].dropna().unique())
    referenced.discard("")
    return referenced


def extract_concepts(model_xbrl, accession_number, qnames):
    records = []
    qname_concepts = model_xbrl.qnameConcepts
    by_str = {str(qname): concept for qname, concept in qname_concepts.items()}
    for qname_str in sorted(qnames):
        concept = by_str.get(qname_str)
        if concept is None:
            continue
        namespace = str(getattr(concept.qname, "namespaceURI", ""))
        is_extension = not bool(re.search(STANDARD_NAMESPACE_PATTERN, namespace, re.IGNORECASE))
        data_type = ""
        type_obj = getattr(concept, "type", None)
        if type_obj is not None:
            data_type = str(getattr(type_obj, "qname", "") or "")
        records.append({
            "accession_number": accession_number, "qname": qname_str, "namespace": namespace,
            "local_name": str(getattr(concept.qname, "localName", "")), "data_type": data_type,
            "balance_type": str(getattr(concept, "balance", "") or ""), "period_type": str(getattr(concept, "periodType", "")),
            "is_abstract": bool(getattr(concept, "isAbstract", False)), "is_extension": is_extension,
        })
    return pd.DataFrame(records)


def extract_labels(model_xbrl, accession_number, qnames):
    from arelle import XbrlConst
    records = []
    label_relationship_set = model_xbrl.relationshipSet(XbrlConst.conceptLabel)
    qname_concepts = model_xbrl.qnameConcepts
    by_str = {str(qname): concept for qname, concept in qname_concepts.items()}
    for qname_str in sorted(qnames):
        concept = by_str.get(qname_str)
        if concept is None:
            continue
        for relationship in label_relationship_set.fromModelObject(concept):
            label_resource = relationship.toModelObject
            if label_resource is None:
                continue
            text = getattr(label_resource, "textValue", None) or getattr(label_resource, "stringValue", None) or ""
            records.append({
                "accession_number": accession_number, "concept_qname": qname_str,
                "label_role": str(getattr(label_resource, "role", "") or ""),
                "language": str(getattr(label_resource, "xmlLang", "") or ""), "label_text": str(text),
            })
    return pd.DataFrame(records)


def extract_roles(model_xbrl, accession_number):
    from arelle import XbrlConst
    records = []
    arcrole_by_type = {"presentation": XbrlConst.parentChild, "calculation": XbrlConst.summationItem}
    for relationship_type, arcrole in arcrole_by_type.items():
        relationship_set = model_xbrl.relationshipSet(arcrole)
        for role_uri in sorted(relationship_set.linkRoleUris):
            records.append({
                "accession_number": accession_number, "role_uri": role_uri,
                "role_definition": _role_definition(model_xbrl, role_uri), "relationship_type": relationship_type,
            })
    definition_arcroles = [
        XbrlConst.all, XbrlConst.notAll, XbrlConst.hypercubeDimension,
        XbrlConst.dimensionDomain, XbrlConst.domainMember, XbrlConst.dimensionDefault,
    ]
    definition_role_uris = set()
    for arcrole in definition_arcroles:
        definition_role_uris |= set(model_xbrl.relationshipSet(arcrole).linkRoleUris)
    for role_uri in sorted(definition_role_uris):
        records.append({
            "accession_number": accession_number, "role_uri": role_uri,
            "role_definition": _role_definition(model_xbrl, role_uri), "relationship_type": "definition",
        })
    return pd.DataFrame(records)


def create_warehouse_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_facts (
        accession_number VARCHAR, fact_index INTEGER, concept_namespace VARCHAR,
        concept_local_name VARCHAR, concept_qname VARCHAR, value_raw VARCHAR,
        value_numeric DOUBLE, decimals VARCHAR, precision VARCHAR, unit_id VARCHAR,
        context_id VARCHAR, period_type VARCHAR, period_start VARCHAR, period_end VARCHAR,
        instant_date VARCHAR, dimensions_json VARCHAR, is_nil BOOLEAN, source_document VARCHAR,
        source_line INTEGER, PRIMARY KEY (accession_number, fact_index))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_contexts (
        accession_number VARCHAR, context_id VARCHAR, entity_identifier VARCHAR,
        period_start VARCHAR, period_end VARCHAR, instant_date VARCHAR, dimensions_json VARCHAR,
        scenario_json VARCHAR, segment_json VARCHAR, PRIMARY KEY (accession_number, context_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_units (
        accession_number VARCHAR, unit_id VARCHAR, numerator_measures VARCHAR,
        denominator_measures VARCHAR, PRIMARY KEY (accession_number, unit_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_concepts (
        accession_number VARCHAR, qname VARCHAR, namespace VARCHAR, local_name VARCHAR,
        data_type VARCHAR, balance_type VARCHAR, period_type VARCHAR, is_abstract BOOLEAN,
        is_extension BOOLEAN, PRIMARY KEY (accession_number, qname))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_labels (
        accession_number VARCHAR, concept_qname VARCHAR, label_role VARCHAR,
        language VARCHAR, label_text VARCHAR)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_presentation_relationships (
        accession_number VARCHAR, role_uri VARCHAR, role_definition VARCHAR,
        parent_concept VARCHAR, child_concept VARCHAR, order_value DOUBLE,
        preferred_label VARCHAR, depth INTEGER)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_calculation_relationships (
        accession_number VARCHAR, role_uri VARCHAR, role_definition VARCHAR,
        parent_concept VARCHAR, child_concept VARCHAR, weight DOUBLE, order_value DOUBLE)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_definition_relationships (
        accession_number VARCHAR, arcrole VARCHAR, role_uri VARCHAR, role_definition VARCHAR,
        parent_concept VARCHAR, child_concept VARCHAR, order_value DOUBLE, closed_attr BOOLEAN,
        context_element VARCHAR, usable_attr BOOLEAN)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_roles (
        accession_number VARCHAR, role_uri VARCHAR, role_definition VARCHAR,
        relationship_type VARCHAR, PRIMARY KEY (accession_number, role_uri, relationship_type))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS warehouse_runs (
        warehouse_run_id VARCHAR PRIMARY KEY, accession_number VARCHAR, ticker VARCHAR,
        report_date VARCHAR, parser_version VARCHAR, arelle_version VARCHAR, script_name VARCHAR,
        started_at_utc VARCHAR, completed_at_utc VARCHAR, local_filing_load_seconds DOUBLE,
        taxonomy_dts_and_parse_seconds DOUBLE, fact_extraction_seconds DOUBLE,
        relationship_extraction_seconds DOUBLE, duckdb_write_seconds DOUBLE,
        total_elapsed_seconds DOUBLE, row_counts_json VARCHAR, status VARCHAR)""")


def write_table(connection, table_name, df, accession_number):
    connection.execute(f"DELETE FROM {table_name} WHERE accession_number = ?", [accession_number])
    if df.empty and len(df.columns) == 0:
        return 0
    connection.register("df_tmp", df)
    connection.execute(f"INSERT INTO {table_name} BY NAME SELECT * FROM df_tmp")
    connection.unregister("df_tmp")
    return int(len(df))


def load_and_warehouse_one_10q(ticker: str, report_date: str) -> dict:
    from arelle.RuntimeOptions import RuntimeOptions
    from arelle.api.Session import Session
    from arelle import Version as ArelleVersion

    total_start = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc).isoformat()

    locked_dir = LOCKED_FILINGS_DIR / ticker.upper()
    manifests = sorted(locked_dir.glob("*/locked_filing_manifest.json"))
    matching = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in manifests]
    matching = [(p, m) for p, m in matching if m.get("report_date") == report_date and m.get("form") == "10-Q"]
    if len(matching) != 1:
        raise RuntimeError(f"Expected exactly 1 locked 10-Q manifest for {ticker}/{report_date}, found {len(matching)}.")
    _, manifest = matching[0]
    primary_document_path = Path(manifest["primary_document_path"]).resolve()
    if not primary_document_path.exists():
        raise FileNotFoundError(f"Primary document not found: {primary_document_path}")

    accession_number = manifest["accession_number"]
    source_document = manifest["primary_document"]
    sec_user_agent = str(manifest.get("sec_user_agent", "")).strip()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "database").mkdir(parents=True, exist_ok=True)

    options = RuntimeOptions(
        entrypointFile=str(primary_document_path), internetConnectivity="online",
        cacheDirectory=str(CACHE_DIR), internetTimeout=INTERNET_TIMEOUT_SECONDS,
        httpUserAgent=sec_user_agent, keepOpen=True,
        logFile=str(DATA_DIR / "database" / "xbrl_warehouse_proof_arelle.log"),
        logFormat="[%(levelname)s] [%(messageCode)s] %(message)s - %(file)s",
    )

    dts_parse_start = time.perf_counter()
    row_counts = {}
    status = "FAIL"

    with Session() as session:
        session.run(options)
        taxonomy_dts_and_parse_seconds = time.perf_counter() - dts_parse_start
        models = session.get_models()
        if len(models) != 1:
            raise RuntimeError(f"Arelle did not return a single model. Model count: {len(models)}")
        model_xbrl = models[0]
        if model_xbrl is None:
            raise RuntimeError("Arelle failed to load the XBRL model.")

        fact_extraction_start = time.perf_counter()
        facts_df = extract_facts(model_xbrl, accession_number, source_document)
        contexts_df = extract_contexts(model_xbrl, accession_number)
        units_df = extract_units(model_xbrl, accession_number)
        fact_extraction_seconds = time.perf_counter() - fact_extraction_start

        relationship_extraction_start = time.perf_counter()
        presentation_df = extract_presentation_relationships(model_xbrl, accession_number)
        calculation_df = extract_calculation_relationships(model_xbrl, accession_number)
        definition_df = extract_definition_relationships(model_xbrl, accession_number)
        roles_df = extract_roles(model_xbrl, accession_number)
        qnames = referenced_concept_qnames(facts_df, presentation_df, calculation_df, definition_df)
        concepts_df = extract_concepts(model_xbrl, accession_number, qnames)
        labels_df = extract_labels(model_xbrl, accession_number, qnames)
        relationship_extraction_seconds = time.perf_counter() - relationship_extraction_start

        duckdb_write_start = time.perf_counter()
        connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH))
        create_warehouse_schema(connection)
        row_counts["xbrl_facts"] = write_table(connection, "xbrl_facts", facts_df, accession_number)
        row_counts["xbrl_contexts"] = write_table(connection, "xbrl_contexts", contexts_df, accession_number)
        row_counts["xbrl_units"] = write_table(connection, "xbrl_units", units_df, accession_number)
        row_counts["xbrl_concepts"] = write_table(connection, "xbrl_concepts", concepts_df, accession_number)
        row_counts["xbrl_labels"] = write_table(connection, "xbrl_labels", labels_df, accession_number)
        row_counts["xbrl_presentation_relationships"] = write_table(connection, "xbrl_presentation_relationships", presentation_df, accession_number)
        row_counts["xbrl_calculation_relationships"] = write_table(connection, "xbrl_calculation_relationships", calculation_df, accession_number)
        row_counts["xbrl_definition_relationships"] = write_table(connection, "xbrl_definition_relationships", definition_df, accession_number)
        row_counts["xbrl_roles"] = write_table(connection, "xbrl_roles", roles_df, accession_number)
        duckdb_write_seconds = time.perf_counter() - duckdb_write_start
        status = "PASS"

    completed_at_utc = datetime.now(timezone.utc).isoformat()
    total_elapsed_seconds = time.perf_counter() - total_start
    warehouse_run_id = f"{accession_number}::{SCRIPT_NAME}::{started_at_utc}"
    connection.execute(
        "INSERT INTO warehouse_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [warehouse_run_id, accession_number, ticker, report_date, "generic-xbrl-warehouse-proof-v1",
         ArelleVersion.getVersion(), SCRIPT_NAME, started_at_utc, completed_at_utc,
         0.0, round(taxonomy_dts_and_parse_seconds, 6), round(fact_extraction_seconds, 6),
         round(relationship_extraction_seconds, 6), round(duckdb_write_seconds, 6),
         round(total_elapsed_seconds, 6), json.dumps(row_counts), status],
    )
    connection.close()

    return {"ticker": ticker, "report_date": report_date, "accession_number": accession_number,
            "status": status, "row_counts": row_counts, "total_elapsed_seconds": round(total_elapsed_seconds, 3)}


def run_worker(ticker: str, report_date: str) -> None:
    summary = load_and_warehouse_one_10q(ticker, report_date)
    print("WORKER_RESULT_JSON=" + json.dumps(summary))


def warehouse_10q_in_child_process(ticker: str, report_date: str) -> dict:
    filing_start = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--single-ticker", ticker, "--single-report-date", report_date],
            timeout=CHILD_PROCESS_TIMEOUT_SECONDS, capture_output=True, text=True, encoding="utf-8",
        )
        elapsed = time.perf_counter() - filing_start
        if result.returncode != 0:
            return {"status": "FAIL", "elapsed_seconds": round(elapsed, 3), "error": f"child exited {result.returncode}",
                     "stderr": result.stderr[-2000:]}
        worker_line = next((line for line in result.stdout.splitlines() if line.startswith("WORKER_RESULT_JSON=")), None)
        if worker_line is None:
            return {"status": "FAIL", "elapsed_seconds": round(elapsed, 3), "error": "no WORKER_RESULT_JSON line"}
        summary = json.loads(worker_line[len("WORKER_RESULT_JSON="):])
        summary["elapsed_seconds"] = round(elapsed, 3)
        return summary
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - filing_start
        return {"status": "TIMEOUT", "elapsed_seconds": round(elapsed, 3), "error": f"exceeded {CHILD_PROCESS_TIMEOUT_SECONDS}s"}


def ensure_sec_filings_row(connection, ticker: str, fiscal_year_end: str, quarter_info: dict) -> str:
    accession_number = quarter_info["accession_number"]
    existing = connection.execute("SELECT accession_number FROM sec_filings WHERE accession_number = ?", [accession_number]).fetchone()
    if existing is not None:
        return "ALREADY_REGISTERED"
    fiscal_year = int(fiscal_year_end[:4])
    connection.execute(
        "INSERT INTO sec_filings (accession_number, ticker, form, report_date, filing_date, "
        "fiscal_year, prior_report_date, source_document) VALUES (?,?,?,?,?,?,?,?)",
        [accession_number, ticker, quarter_info["form"], quarter_info["report_date"],
         quarter_info["filing_date"], fiscal_year, None, quarter_info["primary_document"]],
    )
    return "NEWLY_REGISTERED"


def verify_existing_company_year(prod_connection, ticker: str, fiscal_year_end: str) -> dict:
    run_row = prod_connection.execute(
        "SELECT run_id, run_status FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ? AND engine_version = ?",
        [ticker, fiscal_year_end, s120.ENGINE_VERSION],
    ).fetchone()
    if run_row is None:
        return {"already_complete": False}
    run_id = run_row[0]
    rows = prod_connection.execute(
        "SELECT metric_name, fiscal_quarter, availability_date, accession_number, lineage_json "
        "FROM quarterly_metric_results WHERE run_id = ?", [run_id]
    ).fetchdf()
    checks = {
        "run_status_terminal": run_row[1] in ("PASS", "PASS_WITH_REVIEW_REQUIRED"),
        "row_count_24": len(rows) == 24,
        "no_duplicate_keys": rows.groupby(["metric_name", "fiscal_quarter"]).size().max() == 1 if len(rows) else False,
        "lineage_complete": bool(rows["lineage_json"].notna().all()) if len(rows) else False,
        "availability_dates_present": bool(rows["availability_date"].notna().all()) if len(rows) else False,
    }
    return {"already_complete": all(checks.values()), "checks": checks, "run_id": run_id, "row_count": len(rows)}


# =====================================================================
# REVIEW_REQUIRED-tolerant loader (same logic as scripts/123; now the
# schema it writes to actually accepts the honest NULLs)
# =====================================================================

def is_structurally_complete(engine_output: dict) -> bool:
    for metric_name in METRICS:
        quarters = engine_output["metrics"].get(metric_name, {}).get("quarters", {})
        if len(quarters) != 4:
            return False
    return True


REQUIRED_NON_NULL_FIELDS_STRICT = [
    "unit", "result_status", "extraction_basis", "period_end",
    "availability_date", "accession_number", "dimensions_json",
    "lineage_json", "reconciliation_status",
]


def build_quarter_row_allowing_review_required(run_id, ticker, fiscal_year_end, quarter, metric_name, filings, metric_result, created_at):
    period_end = filings[quarter]["report_date"] if quarter != "Q4" else filings["FY"]["report_date"]
    availability_date = filings[quarter]["filing_date"] if quarter != "Q4" else filings["FY"]["filing_date"]
    accession_number = filings[quarter]["accession_number"] if quarter != "Q4" else filings["FY"]["accession_number"]

    quarters = metric_result.get("quarters", {})
    if quarter in quarters:
        q = quarters[quarter]
        lineage = q["lineage"]
        reconciliation = metric_result.get("reconciliation")
        return {
            "run_id": run_id, "ticker": ticker, "fiscal_year_end": fiscal_year_end,
            "fiscal_quarter": quarter, "metric_name": metric_name,
            "value": q["value"], "unit": "iso4217:USD", "result_status": "PASS",
            "extraction_basis": q["extraction_basis"],
            "period_start": lineage.get("period_start"), "period_end": period_end,
            "availability_date": q["availability_date"],
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
        "run_id": run_id, "ticker": ticker, "fiscal_year_end": fiscal_year_end,
        "fiscal_quarter": quarter, "metric_name": metric_name,
        "value": None, "unit": "iso4217:USD", "result_status": "REVIEW_REQUIRED",
        "extraction_basis": "UNRESOLVED", "period_start": None, "period_end": period_end,
        "availability_date": availability_date, "accession_number": accession_number,
        "concept_qname": None, "context_id": None, "dimensions_json": "{}",
        "lineage_json": json.dumps({"error": error_text, "source": "engine could not resolve this metric"}, ensure_ascii=False),
        "reconciliation_status": "REVIEW_REQUIRED", "reconciliation_difference": None, "permitted_difference": None,
        "created_at": created_at,
    }


def load_company_year_allowing_review_required(connection, ticker: str, spec: dict, engine_output: dict) -> dict:
    fiscal_year_end = spec["fiscal_year_end"]

    already_loaded = connection.execute(
        "SELECT run_id FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ? AND engine_version = ?",
        [ticker, fiscal_year_end, s120.ENGINE_VERSION],
    ).fetchone()
    if already_loaded is not None:
        return {"ticker": ticker, "status": "SKIPPED_ALREADY_LOADED", "run_id": already_loaded[0]}

    filings = engine_output["filings"]
    run_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    has_any_review_required = any(len(engine_output["metrics"].get(m, {}).get("quarters", {})) != 4 for m in METRICS)
    run_status_on_success = "PASS_WITH_REVIEW_REQUIRED" if has_any_review_required else "PASS"

    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            "INSERT INTO quarterly_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, ticker, fiscal_year_end, s120.ENGINE_VERSION, s120.SCHEMA_VERSION,
             filings["Q1"]["accession_number"], filings["Q2"]["accession_number"],
             filings["Q3"]["accession_number"], filings["FY"]["accession_number"],
             str(spec["engine_json"]), "LOADING", created_at, None],
        )

        rows_inserted = 0
        for metric_name in METRICS:
            metric_result = engine_output["metrics"][metric_name]
            for quarter in QUARTERS:
                row = build_quarter_row_allowing_review_required(run_id, ticker, fiscal_year_end, quarter, metric_name, filings, metric_result, created_at)
                connection.execute(
                    "INSERT INTO quarterly_metric_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [row["run_id"], row["ticker"], row["fiscal_year_end"], row["fiscal_quarter"],
                     row["metric_name"], row["value"], row["unit"], row["result_status"],
                     row["extraction_basis"], row["period_start"], row["period_end"],
                     row["availability_date"], row["accession_number"], row["concept_qname"],
                     row["context_id"], row["dimensions_json"], row["lineage_json"],
                     row["reconciliation_status"], row["reconciliation_difference"],
                     row["permitted_difference"], row["created_at"]],
                )
                rows_inserted += 1

        validation_errors = []
        committed = connection.execute(
            "SELECT fiscal_quarter, metric_name, value, extraction_basis, accession_number, concept_qname, "
            "dimensions_json, lineage_json, reconciliation_status, result_status, unit, period_end, availability_date "
            "FROM quarterly_metric_results WHERE run_id = ?", [run_id],
        ).fetchdf()

        if len(committed) != 24:
            validation_errors.append(f"row count = {len(committed)}, expected 24")

        for field in REQUIRED_NON_NULL_FIELDS_STRICT:
            null_count = committed[field].isna().sum() if field in committed.columns else 24
            if null_count > 0:
                validation_errors.append(f"required field {field!r} is null in {null_count} row(s)")

        for field in ("value", "concept_qname"):
            bad = committed[(committed[field].isna()) & (committed["reconciliation_status"] != "REVIEW_REQUIRED")]
            if len(bad) > 0:
                validation_errors.append(f"{field!r} is null on {len(bad)} row(s) NOT marked REVIEW_REQUIRED")

        duplicate_keys = committed.groupby(["metric_name", "fiscal_quarter"]).size()
        duplicates = duplicate_keys[duplicate_keys > 1]
        if len(duplicates) > 0:
            validation_errors.append(f"duplicate natural keys: {duplicates.to_dict()}")

        for _, row in committed.iterrows():
            source_metric = engine_output["metrics"][row["metric_name"]]
            source_quarters = source_metric.get("quarters", {})
            if row["fiscal_quarter"] in source_quarters:
                source_value = source_quarters[row["fiscal_quarter"]]["value"]
                if pd.isna(row["value"]) or abs(float(row["value"]) - float(source_value)) >= 1:
                    validation_errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: value mismatch")
                source_basis = source_quarters[row["fiscal_quarter"]]["extraction_basis"]
                if row["extraction_basis"] != source_basis:
                    validation_errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: basis mismatch")
            else:
                # DuckDB NULL surfaces as NaN (not Python None) for a DOUBLE
                # column once read back via .fetchdf() — pd.isna() is the
                # correct check; `is not None` silently treats every
                # legitimately-NULL numeric value as "present" (NaN is not
                # None), which would reject every honest REVIEW_REQUIRED row.
                if not pd.isna(row["value"]) or row["reconciliation_status"] != "REVIEW_REQUIRED":
                    validation_errors.append(f"{row['metric_name']}/{row['fiscal_quarter']}: unresolved row must be NULL/REVIEW_REQUIRED")

        if validation_errors:
            connection.execute("ROLLBACK")
            return {"ticker": ticker, "status": "ROLLED_BACK", "run_id": run_id, "validation_errors": validation_errors}

        connection.execute(
            "UPDATE quarterly_extraction_runs SET run_status = ?, completed_at = ? WHERE run_id = ?",
            [run_status_on_success, datetime.now(timezone.utc).isoformat(), run_id],
        )
        connection.execute("COMMIT")
        return {"ticker": ticker, "status": "COMMITTED", "run_id": run_id, "rows_committed": int(len(committed)),
                "run_status": run_status_on_success}

    except Exception as exc:  # noqa: BLE001
        connection.execute("ROLLBACK")
        return {"ticker": ticker, "status": "ROLLED_BACK", "run_id": run_id, "error": str(exc)}


# =====================================================================
# CHECKPOINT / PROGRESS LOG
# =====================================================================

def atomic_write_json(path: Path, data: dict) -> None:
    temp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
    os.replace(temp_path, path)


def load_existing_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        try:
            return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_checkpoint(all_target_company_years: list, company_year_results: dict, batch_start: float, phase: str) -> None:
    checkpoint = {
        "phase": phase,
        "target_company_years_total": len(all_target_company_years),
        "completed_count": sum(
            1 for k in all_target_company_years
            if company_year_results.get(k, {}).get("status") in ("SKIPPED_ALREADY_COMPLETE", "COMMITTED", "COMMITTED_WITH_REVIEW_REQUIRED")
        ),
        "elapsed_seconds_so_far": round(time.perf_counter() - batch_start, 2),
        "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        "company_year_records": [
            company_year_results.get(k, {"ticker": k[0], "fiscal_year_end": k[1], "status": "NOT_YET_PROCESSED"})
            for k in all_target_company_years
        ],
    }
    atomic_write_json(CHECKPOINT_PATH, checkpoint)
    if not CHECKPOINT_PATH.exists():
        raise RuntimeError(f"Checkpoint file was not saved: {CHECKPOINT_PATH}")


def append_progress_line(message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} | {message}\n"
    with PROGRESS_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line)
    if not PROGRESS_LOG_PATH.exists():
        raise RuntimeError(f"Progress log was not saved: {PROGRESS_LOG_PATH}")


# =====================================================================
# PROCESS ONE COMPANY-YEAR (shared by proof + resume; reuse-first)
# =====================================================================

def process_one_company_year(ticker: str, fiscal_year_end: str, filings_cache: dict, use_retry_lock: bool, heartbeat_state: dict) -> dict:
    cy_start = time.perf_counter()
    heartbeat_state["current"] = f"{ticker} {fiscal_year_end}"

    warehouse_read_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    fy_info = resolve_fy_10k(prod_connection, warehouse_read_connection, ticker, fiscal_year_end)
    prod_connection.close()
    warehouse_read_connection.close()

    if ticker not in filings_cache:
        company_record = s107.find_company_record(ticker)
        cik = int(company_record["cik_str"])
        company_name = str(company_record.get("title", ""))
        filings_df = s107.load_all_filings(cik)
        filings_cache[ticker] = (cik, company_name, filings_df)
    cik, company_name, filings_df = filings_cache[ticker]

    quarters = resolve_quarters_for_fiscal_year(filings_df, fiscal_year_end)
    print(f"  Resolved: Q1={quarters['Q1']['accession_number']}  Q2={quarters['Q2']['accession_number']}  "
          f"Q3={quarters['Q3']['accession_number']}  FY={fy_info['accession_number']}")

    quarter_status = {}
    company_year_failed = False
    for label in ("Q1", "Q2", "Q3"):
        q = quarters[label]
        heartbeat_state["current"] = f"{ticker} {fiscal_year_end} / {label} lock"
        if use_retry_lock:
            lock_result = lock_10q_with_retry(ticker, cik, company_name, q)
        else:
            lock_result = "REUSED" if is_already_locked(ticker, q["accession_number"]) else lock_10q_if_missing(ticker, cik, company_name, q)

        already_warehoused, counts = is_already_warehoused(q["accession_number"])
        if already_warehoused:
            quarter_status[label] = {"lock_result": lock_result, "warehouse_result": "REUSED", **counts}
            print(f"  {label} {q['accession_number']}: lock={lock_result}, warehouse=REUSED")
        else:
            heartbeat_state["current"] = f"{ticker} {fiscal_year_end} / {label} warehouse"
            warehouse_result = warehouse_10q_in_child_process(ticker, q["report_date"])
            quarter_status[label] = {"lock_result": lock_result, "warehouse_result": warehouse_result.get("status"),
                                      **warehouse_result.get("row_counts", {})}
            print(f"  {label} {q['accession_number']}: lock={lock_result}, warehouse={warehouse_result.get('status')}")
            if warehouse_result.get("status") != "PASS":
                company_year_failed = True
                quarter_status[label]["error"] = warehouse_result.get("error")

    if company_year_failed:
        return {"ticker": ticker, "fiscal_year_end": fiscal_year_end, "status": "FAIL_WAREHOUSE",
                "quarter_status": quarter_status, "elapsed_seconds": round(time.perf_counter() - cy_start, 2), "row_count": 0}

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
    for label in ("Q1", "Q2", "Q3"):
        registration = ensure_sec_filings_row(prod_connection, ticker, fiscal_year_end, quarters[label])
        quarter_status[label]["sec_filings_registration"] = registration
    prod_connection.close()

    heartbeat_state["current"] = f"{ticker} {fiscal_year_end} / engine"
    engine_json_path = DATA_DIR / f"quarterly_engine_{ticker.lower()}_fy{fiscal_year_end[:4]}.json"
    engine_csv_path = DATA_DIR / f"quarterly_engine_{ticker.lower()}_fy{fiscal_year_end[:4]}.csv"
    engine_output = s118.run_quarterly_extraction_engine(
        ticker=ticker, fiscal_year_end=fiscal_year_end,
        q1_accession=quarters["Q1"]["accession_number"], q2_accession=quarters["Q2"]["accession_number"],
        q3_accession=quarters["Q3"]["accession_number"], fy_accession=fy_info["accession_number"],
        json_output_path=engine_json_path, csv_output_path=engine_csv_path,
    )
    metric_statuses = {m: engine_output["metrics"][m].get("status") for m in METRICS}
    row_count = sum(len(engine_output["metrics"][m].get("quarters", {})) for m in METRICS)
    print(f"  Engine: {row_count} rows resolved, statuses={metric_statuses}")

    heartbeat_state["current"] = f"{ticker} {fiscal_year_end} / load"
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
    spec = {"fiscal_year_end": fiscal_year_end, "engine_json": engine_json_path}
    if is_structurally_complete(engine_output):
        load_result = s120.load_one_company(prod_connection, ticker, spec, engine_output)
    else:
        load_result = load_company_year_allowing_review_required(prod_connection, ticker, spec, engine_output)
    prod_connection.close()
    print(f"  Production load: {load_result['status']}")

    elapsed = time.perf_counter() - cy_start
    if load_result["status"] != "COMMITTED":
        return {"ticker": ticker, "fiscal_year_end": fiscal_year_end, "status": "LOAD_FAILED",
                "quarter_status": quarter_status, "metric_statuses": metric_statuses, "engine_row_count": row_count,
                "load_result": load_result, "elapsed_seconds": round(elapsed, 2), "row_count": 0}

    final_status = "COMMITTED" if load_result.get("run_status", "PASS") == "PASS" else "COMMITTED_WITH_REVIEW_REQUIRED"
    return {"ticker": ticker, "fiscal_year_end": fiscal_year_end, "status": final_status,
            "quarter_status": quarter_status, "metric_statuses": metric_statuses, "engine_row_count": row_count,
            "load_result": load_result, "elapsed_seconds": round(elapsed, 2), "row_count": load_result.get("rows_committed", 0)}


# =====================================================================
# MAIN
# =====================================================================

def main() -> None:
    batch_start = time.perf_counter()
    heartbeat_state = {"current": "starting"}
    start_heartbeat(heartbeat_state)

    phase1_result = phase1_backup_and_fix_schema()

    print("\n" + "=" * 100)
    print("PHASE 2 — ONE-COMPANY-YEAR PROOF: GOOGL FY2021")
    print("=" * 100)

    all_target_company_years = [PROOF_COMPANY_YEAR] + SCHEMA_BLOCKED_LIST + NETWORK_RETRY_LIST
    company_year_results: dict[tuple, dict] = {}
    filings_cache: dict[str, tuple] = {}

    heartbeat_state["current"] = "GOOGL FY2021 proof"
    try:
        proof_result = process_one_company_year("GOOGL", "2021-12-31", filings_cache, use_retry_lock=False, heartbeat_state=heartbeat_state)
    except Exception as exc:  # noqa: BLE001
        proof_result = {"ticker": "GOOGL", "fiscal_year_end": "2021-12-31", "status": "EXCEPTION", "error": str(exc), "row_count": 0}

    company_year_results[PROOF_COMPANY_YEAR] = proof_result
    save_checkpoint(all_target_company_years, company_year_results, batch_start, phase="phase2_proof")
    append_progress_line(f"PHASE2_PROOF | GOOGL FY(end=2021-12-31) | status={proof_result['status']} | rows={proof_result.get('row_count', 0)}")

    print(f"\nProof result: {proof_result['status']}, rows={proof_result.get('row_count', 0)}")

    proof_passed = proof_result["status"] in ("COMMITTED", "COMMITTED_WITH_REVIEW_REQUIRED")

    if not proof_passed:
        print("\nPROOF FAILED — stopping before Phase 3, per instructions.")
        _heartbeat_stop.set()
        write_final_report(phase1_result, company_year_results, all_target_company_years, batch_start, proof_passed=False)
        return

    print("\nPROOF PASSED — proceeding to Phase 3 (resume remaining company-years).")

    print("\n" + "=" * 100)
    print("PHASE 3 — RESUME REMAINING COMPANY-YEARS")
    print("=" * 100)

    for ticker, fiscal_year_end in SCHEMA_BLOCKED_LIST:
        key = (ticker, fiscal_year_end)
        print(f"\n>>> {ticker} FY(end={fiscal_year_end}) <<<")
        prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
        existing = verify_existing_company_year(prod_connection, ticker, fiscal_year_end)
        prod_connection.close()
        if existing["already_complete"]:
            print("  SKIPPED (already complete)")
            company_year_results[key] = {"ticker": ticker, "fiscal_year_end": fiscal_year_end,
                                          "status": "SKIPPED_ALREADY_COMPLETE", "row_count": existing["row_count"]}
        else:
            try:
                company_year_results[key] = process_one_company_year(ticker, fiscal_year_end, filings_cache, use_retry_lock=False, heartbeat_state=heartbeat_state)
            except Exception as exc:  # noqa: BLE001
                print(f"  EXCEPTION: {exc}")
                company_year_results[key] = {"ticker": ticker, "fiscal_year_end": fiscal_year_end, "status": "EXCEPTION",
                                              "error": str(exc), "row_count": 0}
        save_checkpoint(all_target_company_years, company_year_results, batch_start, phase="phase3_schema_blocked")
        append_progress_line(f"PHASE3 | {ticker} FY(end={fiscal_year_end}) | status={company_year_results[key]['status']} | rows={company_year_results[key].get('row_count', 0)}")

    for ticker, fiscal_year_end in NETWORK_RETRY_LIST:
        key = (ticker, fiscal_year_end)
        print(f"\n>>> {ticker} FY(end={fiscal_year_end}) [network-retry] <<<")
        prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
        existing = verify_existing_company_year(prod_connection, ticker, fiscal_year_end)
        prod_connection.close()
        if existing["already_complete"]:
            print("  SKIPPED (already complete)")
            company_year_results[key] = {"ticker": ticker, "fiscal_year_end": fiscal_year_end,
                                          "status": "SKIPPED_ALREADY_COMPLETE", "row_count": existing["row_count"]}
        else:
            try:
                company_year_results[key] = process_one_company_year(ticker, fiscal_year_end, filings_cache, use_retry_lock=True, heartbeat_state=heartbeat_state)
            except Exception as exc:  # noqa: BLE001
                print(f"  EXCEPTION: {exc}")
                company_year_results[key] = {"ticker": ticker, "fiscal_year_end": fiscal_year_end, "status": "EXCEPTION",
                                              "error": str(exc), "row_count": 0}
        save_checkpoint(all_target_company_years, company_year_results, batch_start, phase="phase3_network_retry")
        append_progress_line(f"PHASE3_RETRY | {ticker} FY(end={fiscal_year_end}) | status={company_year_results[key]['status']} | rows={company_year_results[key].get('row_count', 0)}")

    _heartbeat_stop.set()
    write_final_report(phase1_result, company_year_results, all_target_company_years, batch_start, proof_passed=True)


def write_final_report(phase1_result, company_year_results, all_target_company_years, batch_start, proof_passed: bool) -> None:
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    total_runs = prod_connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs WHERE engine_version = ?", [s120.ENGINE_VERSION]).fetchone()[0]
    total_rows = prod_connection.execute(
        "SELECT COUNT(*) FROM quarterly_metric_results r JOIN quarterly_extraction_runs e ON e.run_id = r.run_id WHERE e.engine_version = ?",
        [s120.ENGINE_VERSION],
    ).fetchone()[0]
    basis_counts = dict(prod_connection.execute(
        "SELECT extraction_basis, COUNT(*) FROM quarterly_metric_results r JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
        "WHERE e.engine_version = ? GROUP BY extraction_basis", [s120.ENGINE_VERSION]
    ).fetchall())
    reconciliation_counts = dict(prod_connection.execute(
        "SELECT reconciliation_status, COUNT(*) FROM ("
        "SELECT DISTINCT run_id, metric_name, reconciliation_status FROM quarterly_metric_results"
        ") x JOIN quarterly_extraction_runs e ON e.run_id = x.run_id WHERE e.engine_version = ? GROUP BY reconciliation_status",
        [s120.ENGINE_VERSION],
    ).fetchall())
    duplicate_check = prod_connection.execute(
        "SELECT run_id, metric_name, fiscal_quarter, COUNT(*) c FROM quarterly_metric_results "
        "GROUP BY run_id, metric_name, fiscal_quarter HAVING COUNT(*) > 1"
    ).fetchall()
    null_counts = prod_connection.execute(
        "SELECT SUM(CASE WHEN concept_qname IS NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN reconciliation_difference IS NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN permitted_difference IS NULL THEN 1 ELSE 0 END) FROM quarterly_metric_results"
    ).fetchone()
    review_required_details = prod_connection.execute(
        "SELECT ticker, fiscal_year_end, metric_name, fiscal_quarter, lineage_json FROM quarterly_metric_results "
        "WHERE reconciliation_status = 'REVIEW_REQUIRED' ORDER BY ticker, fiscal_year_end, metric_name, fiscal_quarter"
    ).fetchall()
    missing_lineage = prod_connection.execute("SELECT COUNT(*) FROM quarterly_metric_results WHERE lineage_json IS NULL").fetchone()[0]
    avail_mismatch = prod_connection.execute(
        "SELECT COUNT(*) FROM quarterly_metric_results r JOIN sec_filings s ON s.accession_number = r.accession_number "
        "WHERE r.availability_date != CAST(s.filing_date AS VARCHAR)"
    ).fetchone()[0]
    fmr_count = prod_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    prod_connection.close()

    actual_v1_checksum = sha256_of_file(ANNUAL_V1_DB_PATH)

    final_result = {
        "proof_passed": proof_passed,
        "phase1": phase1_result,
        "company_year_results": {f"{k[0]}|{k[1]}": v for k, v in company_year_results.items()},
        "total_extraction_runs": total_runs,
        "total_quarterly_metric_results": total_rows,
        "extraction_basis_counts": basis_counts,
        "reconciliation_status_counts": reconciliation_counts,
        "duplicate_natural_keys": duplicate_check,
        "null_counts": {"concept_qname": null_counts[0], "reconciliation_difference": null_counts[1], "permitted_difference": null_counts[2]},
        "review_required_count": len(review_required_details),
        "missing_lineage_count": missing_lineage,
        "availability_date_mismatches": avail_mismatch,
        "financial_metric_results_count": fmr_count,
        "annual_v1_checksum_unchanged": actual_v1_checksum == EXPECTED_ANNUAL_V1_CHECKSUM,
        "batch_elapsed_seconds": round(time.perf_counter() - batch_start, 2),
    }

    result_path = DATA_DIR / f"quarterly_schema_fix_and_resume_result_{RUN_TIMESTAMP}.json"
    result_path.write_text(json.dumps(final_result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    save_checkpoint(all_target_company_years, company_year_results, batch_start,
                     phase="complete" if proof_passed else "stopped_proof_failed")

    print("\n" + "=" * 100)
    print("FINAL SUMMARY")
    print(json.dumps(final_result, indent=2, ensure_ascii=False, default=str))
    print(f"\nResult written to {result_path}")
    print("=" * 100)


def parse_worker_arguments():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--single-ticker", default=None)
    parser.add_argument("--single-report-date", default=None)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_worker_arguments()
    if arguments.single_report_date:
        run_worker(arguments.single_ticker, arguments.single_report_date)
    else:
        main()
