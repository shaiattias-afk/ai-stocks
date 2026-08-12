"""quarterly_extension.py -- extends Quarterly Data V1 (frozen, D-042) to
tickers beyond the original 9, per the user's explicit direction
(2026-08-12 session). Does NOT modify the frozen V5 engine
(stock_agent.extraction.quarterly, unchanged) or touch any existing
quarterly_extraction_runs / quarterly_metric_results row -- purely
additive, the same "append, never touch the frozen set" discipline the
annual wider-universe extension (D-051-D-061) already used for
financial_metric_results.

Ported out of scripts/207_quarterly_extension_pilot.py (the first,
3-ticker pilot that found and fixed the 2 real bugs this module's
callers depend on -- see stock_agent/extraction/quarterly.py's
resolve_annual_anchor / lookup_annual_fact_decimals) so the full-universe
batch driver (scripts/212) and the original pilot script both call the
same orchestration code instead of duplicating it, per this project's
own binding rule that engine/library logic lives in src/stock_agent, not
scripts/.

Pipeline (per ticker):
  1. Discover 10-Q filings across the last ~4 fiscal years
     (discover_annual_filings(..., forms=("10-Q",)) -- fully generic,
     not annual-specific despite the function's name).
  2. Download each into the compressed filing archive
     (filing_archive_manifest/filing_archive_files) -- same mechanism
     scripts/191 used for the wider-universe 10-Ks.
  3. Insert sec_filings rows for each newly-downloaded 10-Q
     (accession-first, D-002; fiscal_year = report_date's own year,
     prior_report_date = NULL, matching the existing 9 tickers' 10-Q
     rows' own shape).
  4. Group 10-Qs into fiscal years by matching against this ticker's own
     already-locked 10-K report_dates -- a fiscal year is usable only
     when exactly 3 10-Qs fall strictly between the prior 10-K's report
     date and this one's, chronologically ordered as Q1/Q2/Q3.
  5. Warehouse-load each new 10-Q accession (Arelle, straight from the
     compressed archive -- stock_agent.warehouse.archive_loader, the
     same path the wider-universe annual work uses; NOT the older
     disk-locked loader, confirmed superseded).
  6. Run the frozen, UNCHANGED quarterly engine V5
     (stock_agent.extraction.quarterly.run_quarterly_extraction_engine_v5)
     for each usable fiscal year.
  7. Insert results into quarterly_extraction_runs / quarterly_metric_
     results using the LIVE table schema (named-column INSERT).

Explicitly NOT done here: no attempt to fix a metric that comes back
REVIEW_REQUIRED -- that would mean modifying the frozen engine, which
needs its own new version + full regression per D-042. A ticker/fiscal-
year that doesn't cleanly resolve is reported and left REVIEW_REQUIRED,
exactly as the engine's own fail-closed design intends.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH, WAREHOUSE_DB_PATH
from stock_agent.extraction.quarterly import run_quarterly_extraction_engine_v5
from stock_agent.filings import archive
from stock_agent.filings.download import (
    SecDownloadError,
    discover_annual_filings,
    download_filing_into_archive,
    download_filings_concurrently,
)
from stock_agent.warehouse.archive_loader import ArchiveWarehouseLoadError, run_archive_warehouse_load

FISCAL_YEARS_BACK = 4
ENGINE_VERSION = "QUARTERLY_ENGINE_V5_STANDARD_GAAP_ALLOW_LIST"
SCHEMA_VERSION = "QUARTERLY_EXTENSION_V1_2026_08_12"
SCRATCH_DIR = DATA_DIR / "_scratch_quarterly_extension"
DOWNLOAD_MAX_WORKERS = 8


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def existing_10k_report_dates(prod_connection, ticker: str) -> list[str]:
    rows = prod_connection.execute(
        "SELECT report_date FROM sec_filings WHERE ticker = ? AND form = '10-K' ORDER BY report_date", [ticker]
    ).fetchall()
    return [str(r[0]) for r in rows]


def company_name_hint(archive_connection, ticker: str) -> str | None:
    """A delisted/acquired ticker (ANSS, ATVI, CERN, ALXN, SGEN, XLNX,
    MXIM, WBA, SPLK -- 9 of the 135-company universe, confirmed by direct
    query 2026-08-12) is absent from SEC's CURRENT-listings ticker file,
    so CIK resolution fails outright without a company-name hint to fall
    back to the full historical CIK index (see discover_annual_filings'
    own docstring). The annual pipeline (scripts/190) already handles
    this via each ticker's known index-membership company name; this
    module has no such membership data, but the SAME company name is
    already sitting in filing_archive_manifest from that ticker's own
    already-archived 10-K (downloaded successfully specifically because
    scripts/190 DID supply the hint) -- reused here, not re-derived."""
    row = archive_connection.execute(
        "SELECT company_name FROM filing_archive_manifest WHERE ticker = ? AND company_name IS NOT NULL LIMIT 1",
        [ticker],
    ).fetchone()
    return row[0] if row else None


def discover_and_lock_10q(prod_connection, archive_connection, ticker: str) -> dict:
    """Discovers and downloads 10-Q filings for the last FISCAL_YEARS_BACK
    fiscal years (bounded by this ticker's own already-locked 10-K report
    dates), inserts new sec_filings rows. Returns a summary dict."""
    report_dates = existing_10k_report_dates(prod_connection, ticker)
    if len(report_dates) < 2:
        return {"ticker": ticker, "status": "SKIPPED_INSUFFICIENT_10K_HISTORY", "10k_report_dates": report_dates}

    boundary_dates = report_dates[-(FISCAL_YEARS_BACK + 1):] if len(report_dates) > FISCAL_YEARS_BACK else report_dates
    window_start = date.fromisoformat(boundary_dates[0])
    window_end = date.fromisoformat(boundary_dates[-1])

    try:
        discovery = discover_annual_filings(
            ticker, window_start, window_end, forms=("10-Q",),
            company_name_hint=company_name_hint(archive_connection, ticker),
        )
    except SecDownloadError as error:
        return {"ticker": ticker, "status": "DISCOVERY_FAILED", "error": str(error)}

    downloaded, already, failed = [], [], []
    inserted_sec_filings = []
    for filing in discovery["filings"]:
        try:
            result = download_filing_into_archive(archive_connection, filing)
        except SecDownloadError as error:
            failed.append({"accession_number": filing.accession_number, "error": str(error)[:200]})
            continue
        if result["status"] == "ALREADY_ARCHIVED":
            already.append(filing.accession_number)
        elif result["status"] == archive.NO_XBRL_DOCUMENT_SET:
            failed.append({"accession_number": filing.accession_number, "error": "NO_XBRL_DOCUMENT_SET"})
            continue
        else:
            downloaded.append(filing.accession_number)

        exists = prod_connection.execute(
            "SELECT 1 FROM sec_filings WHERE accession_number = ?", [filing.accession_number]
        ).fetchone()
        if exists is None:
            prod_connection.execute(
                "INSERT INTO sec_filings (accession_number, ticker, form, report_date, filing_date, "
                "fiscal_year, prior_report_date, source_document) VALUES (?,?,?,?,?,?,?,?)",
                [filing.accession_number, filing.ticker, filing.form, filing.report_date, filing.filing_date,
                 int(filing.report_date[:4]), None, filing.primary_document],
            )
            inserted_sec_filings.append(filing.accession_number)

    return {
        "ticker": ticker, "status": "OK", "window": [window_start.isoformat(), window_end.isoformat()],
        "discovered": len(discovery["filings"]), "downloaded": downloaded, "already_archived": already,
        "failed": failed, "inserted_sec_filings": inserted_sec_filings,
    }


def _discover_10q_window(prod_connection, archive_connection, ticker: str) -> dict:
    """Discovery only (no download) -- pure SEC lookup, kept sequential
    across a batch since it's 1-3 requests/ticker (cheap) and touches a
    local CIK-lookup cache file that isn't demonstrated thread-safe."""
    report_dates = existing_10k_report_dates(prod_connection, ticker)
    if len(report_dates) < 2:
        return {"ticker": ticker, "status": "SKIPPED_INSUFFICIENT_10K_HISTORY", "10k_report_dates": report_dates}
    boundary_dates = report_dates[-(FISCAL_YEARS_BACK + 1):] if len(report_dates) > FISCAL_YEARS_BACK else report_dates
    window_start = date.fromisoformat(boundary_dates[0])
    window_end = date.fromisoformat(boundary_dates[-1])
    try:
        discovery = discover_annual_filings(
            ticker, window_start, window_end, forms=("10-Q",),
            company_name_hint=company_name_hint(archive_connection, ticker),
        )
    except SecDownloadError as error:
        return {"ticker": ticker, "status": "DISCOVERY_FAILED", "error": str(error)}
    return {
        "ticker": ticker, "status": "OK", "window": [window_start.isoformat(), window_end.isoformat()],
        "filings": discovery["filings"],
    }


def discover_and_lock_10q_batch(
    prod_connection, archive_connection, tickers: list[str], max_workers: int = DOWNLOAD_MAX_WORKERS,
) -> dict[str, dict]:
    """Batched version of discover_and_lock_10q: discovers each ticker's
    10-Q filings sequentially (cheap), then downloads EVERY filing across
    the WHOLE batch in one concurrent pool (download_filings_concurrently)
    -- pooling across tickers, not just within one, keeps the rate-
    limited pipeline saturated even for tickers with few filings.
    sec_filings inserts happen sequentially afterward (fast, local, no
    network). Returns {ticker: result_dict}, same shape as
    discover_and_lock_10q's own return value."""
    discoveries = {ticker: _discover_10q_window(prod_connection, archive_connection, ticker) for ticker in tickers}

    all_filings = []
    filing_owner: dict[str, str] = {}  # accession_number -> ticker
    for ticker, d in discoveries.items():
        for filing in d.get("filings", []):
            all_filings.append(filing)
            filing_owner[filing.accession_number] = ticker

    download_results = download_filings_concurrently(archive_connection, all_filings, max_workers=max_workers)

    filings_by_accession = {f.accession_number: f for f in all_filings}
    per_ticker: dict[str, dict] = {}
    for ticker, d in discoveries.items():
        per_ticker[ticker] = {
            "ticker": ticker, "status": d["status"],
            "window": d.get("window"), "discovered": len(d.get("filings", [])),
            "downloaded": [], "already_archived": [], "failed": [], "inserted_sec_filings": [],
        }
        if d["status"] != "OK":
            per_ticker[ticker]["error"] = d.get("error")

    for result in download_results:
        accession_number = result["accession_number"]
        ticker = filing_owner.get(accession_number)
        if ticker is None:
            continue
        status = result["status"]
        if status == "DOWNLOADED":
            per_ticker[ticker]["downloaded"].append(accession_number)
        elif status == "ALREADY_ARCHIVED":
            per_ticker[ticker]["already_archived"].append(accession_number)
        else:
            per_ticker[ticker]["failed"].append({"accession_number": accession_number, "error": result.get("error", status)})
            continue

        filing = filings_by_accession[accession_number]
        exists = prod_connection.execute(
            "SELECT 1 FROM sec_filings WHERE accession_number = ?", [accession_number]
        ).fetchone()
        if exists is None:
            prod_connection.execute(
                "INSERT INTO sec_filings (accession_number, ticker, form, report_date, filing_date, "
                "fiscal_year, prior_report_date, source_document) VALUES (?,?,?,?,?,?,?,?)",
                [filing.accession_number, filing.ticker, filing.form, filing.report_date, filing.filing_date,
                 int(filing.report_date[:4]), None, filing.primary_document],
            )
            per_ticker[ticker]["inserted_sec_filings"].append(accession_number)

    return per_ticker


def group_into_fiscal_years(prod_connection, ticker: str) -> list[dict]:
    """Matches this ticker's 10-Q accessions against its own 10-K report
    dates: a fiscal year is usable only when exactly 3 10-Qs fall
    strictly between the prior 10-K's report_date (exclusive) and this
    one's (exclusive), ordered chronologically as Q1/Q2/Q3."""
    tens_k = prod_connection.execute(
        "SELECT report_date, accession_number FROM sec_filings WHERE ticker = ? AND form = '10-K' ORDER BY report_date",
        [ticker],
    ).fetchall()
    tens_q = prod_connection.execute(
        "SELECT report_date, accession_number FROM sec_filings WHERE ticker = ? AND form = '10-Q' ORDER BY report_date",
        [ticker],
    ).fetchall()

    usable = []
    for i in range(1, len(tens_k)):
        prior_date, _ = tens_k[i - 1]
        fy_date, fy_accession = tens_k[i]
        quarters_in_range = [
            (str(rd), acc) for rd, acc in tens_q if str(prior_date) < str(rd) < str(fy_date)
        ]
        if len(quarters_in_range) == 3:
            usable.append({
                "ticker": ticker, "fiscal_year_end": str(fy_date), "fy_accession": fy_accession,
                "q1_accession": quarters_in_range[0][1], "q2_accession": quarters_in_range[1][1],
                "q3_accession": quarters_in_range[2][1],
            })
    return usable


def already_loaded(prod_connection, ticker: str, fiscal_year_end: str) -> bool:
    row = prod_connection.execute(
        "SELECT 1 FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ?", [ticker, fiscal_year_end]
    ).fetchone()
    return row is not None


def warehouse_load_new_10q(archive_connection, warehouse_connection, accession_numbers: list[str]) -> dict:
    already, loaded, failed = [], [], []
    for accession_number in accession_numbers:
        existing = warehouse_connection.execute(
            "SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?", [accession_number]
        ).fetchone()[0]
        if existing > 0:
            already.append(accession_number)
            continue
        try:
            result = run_archive_warehouse_load(archive_connection, accession_number, WAREHOUSE_DB_PATH)
            loaded.append({"accession_number": accession_number, "result": result.get("status")})
        except ArchiveWarehouseLoadError as error:
            failed.append({"accession_number": accession_number, "error": str(error)[:300]})
    return {"already_warehoused": already, "newly_loaded": loaded, "failed": failed}


def rows_from_engine_output(engine_output: dict) -> list[dict]:
    rows = []
    for metric_name, metric_result in engine_output["metrics"].items():
        status = metric_result.get("status")
        reconciliation = metric_result.get("reconciliation", {})
        for quarter_label in ("Q1", "Q2", "Q3", "Q4"):
            quarter = metric_result.get("quarters", {}).get(quarter_label)
            if quarter is None:
                continue
            lineage = quarter["lineage"]
            rows.append({
                "fiscal_quarter": quarter_label, "metric_name": metric_name,
                "value": quarter["value"], "unit": "iso4217:USD",
                "result_status": status, "extraction_basis": quarter["extraction_basis"],
                "period_start": lineage.get("period_start"), "period_end": lineage.get("period_end"),
                "availability_date": quarter["availability_date"],
                "accession_number": lineage.get("accession_number") or lineage.get("annual_accession_number"),
                "concept_qname": lineage.get("concept_qname") or lineage.get("annual_concept_qname"),
                "context_id": lineage.get("context_id") or lineage.get("nine_month_ytd_context_id"),
                "dimensions_json": "{}", "lineage_json": json.dumps(lineage, default=str),
                "reconciliation_status": status,
                "reconciliation_difference": reconciliation.get("difference"),
                "permitted_difference": reconciliation.get("precision_calculation", {}).get("permitted_difference"),
            })
    return rows


def run_engine(target: dict) -> dict:
    """Calls the frozen V5 engine only -- NO database connection may be
    open to PRODUCTION_DB_PATH or WAREHOUSE_DB_PATH anywhere in this
    process while this runs, since the engine opens its own (read-only)
    connections to both and DuckDB refuses a second connection to the
    same file under a different configuration (a real error hit and
    fixed during the first pilot run)."""
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    ticker, fiscal_year_end = target["ticker"], target["fiscal_year_end"]
    json_path = SCRATCH_DIR / f"v5_{ticker}_{fiscal_year_end}.json"
    csv_path = SCRATCH_DIR / f"v5_{ticker}_{fiscal_year_end}.csv"

    return run_quarterly_extraction_engine_v5(
        ticker=ticker, fiscal_year_end=fiscal_year_end,
        q1_accession=target["q1_accession"], q2_accession=target["q2_accession"],
        q3_accession=target["q3_accession"], fy_accession=target["fy_accession"],
        json_output_path=json_path, csv_output_path=csv_path,
    )


def load_engine_output(engine_output: dict, target: dict) -> dict:
    """Opens its OWN short-lived write connection, inserts, closes --
    called only after run_engine's connections are fully closed."""
    ticker, fiscal_year_end = target["ticker"], target["fiscal_year_end"]
    json_path = SCRATCH_DIR / f"v5_{ticker}_{fiscal_year_end}.json"

    rows = rows_from_engine_output(engine_output)
    run_id = str(uuid.uuid4())
    created_at = utc_now_iso()

    prod_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=False)
    prod_connection.execute(
        "INSERT INTO quarterly_extraction_runs "
        "(run_id, ticker, fiscal_year_end, engine_version, schema_version, "
        " q1_accession, q2_accession, q3_accession, fy_accession, source_json_path, "
        " run_status, created_at, completed_at, loaded_at, is_active) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [run_id, ticker, fiscal_year_end, ENGINE_VERSION, SCHEMA_VERSION,
         target["q1_accession"], target["q2_accession"], target["q3_accession"], target["fy_accession"],
         str(json_path), "LOADED", created_at, created_at, created_at, True],
    )
    for row in rows:
        prod_connection.execute(
            "INSERT INTO quarterly_metric_results "
            "(run_id, ticker, fiscal_year_end, fiscal_quarter, metric_name, value, unit, result_status, "
            " extraction_basis, period_start, period_end, availability_date, accession_number, concept_qname, "
            " context_id, dimensions_json, lineage_json, reconciliation_status, reconciliation_difference, "
            " permitted_difference, created_at, engine_version, loaded_at, is_active) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, ticker, fiscal_year_end, row["fiscal_quarter"], row["metric_name"], row["value"], row["unit"],
             row["result_status"], row["extraction_basis"], row["period_start"], row["period_end"],
             row["availability_date"], row["accession_number"], row["concept_qname"], row["context_id"],
             row["dimensions_json"], row["lineage_json"], row["reconciliation_status"],
             row["reconciliation_difference"], row["permitted_difference"], created_at, ENGINE_VERSION,
             created_at, True],
        )
    prod_connection.close()

    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["result_status"]] = status_counts.get(row["result_status"], 0) + 1

    return {"ticker": ticker, "fiscal_year_end": fiscal_year_end, "run_id": run_id,
            "n_rows": len(rows), "status_counts": status_counts}
