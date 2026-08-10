"""filings/download.py — SEC filing discovery and XBRL-only download.

Ported from `scripts/162_download_xbrl_only_filing.py`, with the addition
of *discovery*: the script could fetch one filing whose report date you
already knew, which does not scale past a handful of companies. Expanding
to a real index needs the pipeline to find out for itself which filings
exist for a ticker.

Two things this module deliberately does NOT do:

* It never guesses a filing. Selection is by exact form + exact report
  date, matching the project's accession-first rule (D-002). If a ticker
  has no 10-K for a period, that is reported as a gap, not filled in from
  a nearby filing.
* It never invents coverage. A company acquired mid-window simply stops
  filing; the discovery result says so plainly rather than padding the
  series, because a backtest must see the same absence an investor would
  have seen.

Rate limiting honours SEC's published 10 requests/second ceiling.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb
import pandas as pd

from stock_agent.filings import archive

SEC_BASE_URL = "https://www.sec.gov"
SEC_DATA_URL = "https://data.sec.gov"

USER_AGENT = "Shai Attias shaiattias@gmail.com"
REQUEST_DELAY_SECONDS = 0.1  # SEC's documented limit is 10 req/s
REQUEST_TIMEOUT_SECONDS = 60

ANNUAL_FORMS = ("10-K",)
# Foreign private issuers file 20-F instead of 10-K, and some file 40-F.
# They are recognised here so discovery can REPORT them rather than
# silently returning "no filings" for a company that files perfectly well
# in a different form.
FOREIGN_ANNUAL_FORMS = ("20-F", "40-F")


class SecDownloadError(Exception):
    pass


@dataclass(frozen=True)
class DiscoveredFiling:
    ticker: str
    cik: int
    company_name: str
    form: str
    report_date: str
    filing_date: str
    accession_number: str
    primary_document: str


def _sec_get_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/html,application/xhtml+xml,application/xml,text/plain,*/*",
        "Accept-Encoding": "identity",
        "Connection": "close",
    })
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            content = response.read()
    except Exception as error:  # noqa: BLE001
        raise SecDownloadError(f"SEC request failed for {url}: {error}") from error
    time.sleep(REQUEST_DELAY_SECONDS)
    return content


def _sec_get_json(url: str) -> Any:
    raw = _sec_get_bytes(url)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SecDownloadError(f"SEC did not return valid JSON for {url}") from error


_ticker_cache: dict[str, dict[str, Any]] | None = None


def find_company_record(ticker: str) -> dict[str, Any]:
    """Resolves a ticker to SEC's own company record (CIK + title).

    The full ticker file is fetched once per process and cached: resolving
    25 tickers should not mean 25 downloads of the same 1 MB file.
    """
    global _ticker_cache
    if _ticker_cache is None:
        payload = _sec_get_json(f"{SEC_BASE_URL}/files/company_tickers.json")
        _ticker_cache = {}
        for record in payload.values():
            _ticker_cache.setdefault(str(record.get("ticker", "")).upper(), record)

    record = _ticker_cache.get(ticker.upper())
    if record is None:
        raise SecDownloadError(
            f"ticker {ticker!r} not found in SEC's company_tickers.json. "
            "Delisted or renamed companies may no longer appear there."
        )
    return record


def load_all_filings(cik: int) -> pd.DataFrame:
    """Full filing history: recent filings plus every historical page."""
    cik_padded = str(cik).zfill(10)
    submissions = _sec_get_json(f"{SEC_DATA_URL}/submissions/CIK{cik_padded}.json")
    frames: list[pd.DataFrame] = []

    recent = submissions.get("filings", {}).get("recent", {})
    if recent:
        frames.append(pd.DataFrame(recent))

    for item in submissions.get("filings", {}).get("files", []):
        file_name = str(item.get("name", "")).strip()
        if not file_name:
            continue
        historical = _sec_get_json(f"{SEC_DATA_URL}/submissions/{file_name}")
        frame = pd.DataFrame(historical)
        if not frame.empty:
            frames.append(frame)

    if not frames:
        raise SecDownloadError(f"no filing history returned for CIK {cik}")
    return pd.concat(frames, ignore_index=True, sort=False)


def discover_annual_filings(
    ticker: str,
    start_date: date,
    end_date: date,
    forms: tuple[str, ...] = ANNUAL_FORMS,
    company_name_hint: str | None = None,
) -> dict[str, Any]:
    """Finds every annual filing for `ticker` whose report date falls in
    the window.

    Returns a report rather than a bare list, because *what is missing* is
    as important as what is found: a company acquired mid-window has fewer
    filings, and a foreign private issuer has none of the requested form
    at all. Both are surfaced explicitly.

    `company_name_hint` lets delisted companies be found. SEC's ticker
    file lists only current registrants, so acquired companies (ATVI,
    ALXN, ANSS) are absent from it despite their filings remaining on
    EDGAR; the hint enables the confirmed name-based fallback in
    `filings.cik_lookup`. Without it, the pipeline would be able to fetch
    only survivors -- the exact bias the universe work removes.
    """
    from stock_agent.filings.cik_lookup import resolve_cik

    resolution = resolve_cik(ticker, company_name=company_name_hint,
                             window_start=start_date, window_end=end_date)
    if not resolution.resolved:
        raise SecDownloadError(
            f"{ticker}: could not resolve a CIK ({resolution.status}). {resolution.reason}"
        )
    cik = resolution.cik
    company_name = resolution.company_name or ""

    filings = load_all_filings(cik)
    required = {"accessionNumber", "filingDate", "reportDate", "form", "primaryDocument"}
    missing_columns = required - set(filings.columns)
    if missing_columns:
        raise SecDownloadError(f"{ticker}: filing history missing columns {sorted(missing_columns)}")

    filings = filings.dropna(subset=["reportDate", "form"])
    in_window = filings[
        (filings["reportDate"] >= start_date.isoformat())
        & (filings["reportDate"] <= end_date.isoformat())
    ]

    matched = in_window[in_window["form"].isin(forms)].copy()
    matched = matched.sort_values("reportDate")

    discovered = [
        DiscoveredFiling(
            ticker=ticker.upper(), cik=cik, company_name=company_name,
            form=str(row["form"]), report_date=str(row["reportDate"]),
            filing_date=str(row["filingDate"]),
            accession_number=str(row["accessionNumber"]),
            primary_document=str(row["primaryDocument"]),
        )
        for _, row in matched.iterrows()
    ]

    # duplicate report dates mean the same period was filed twice (e.g. an
    # amended filing slipped through) -- flagged, never silently deduped
    seen: dict[str, int] = {}
    for filing in discovered:
        seen[filing.report_date] = seen.get(filing.report_date, 0) + 1
    duplicates = sorted(d for d, n in seen.items() if n > 1)

    foreign = sorted(set(in_window[in_window["form"].isin(FOREIGN_ANNUAL_FORMS)]["form"]))

    return {
        "ticker": ticker.upper(),
        "cik": cik,
        "company_name": company_name,
        "cik_resolution_method": resolution.method,
        "cik_resolution_evidence": resolution.evidence,
        "forms_requested": list(forms),
        "window": [start_date.isoformat(), end_date.isoformat()],
        "filings": discovered,
        "filing_count": len(discovered),
        "report_dates": [f.report_date for f in discovered],
        "duplicate_report_dates": duplicates,
        "foreign_annual_forms_present": foreign,
        "note": (
            "no filings of the requested form in window; company files "
            f"{foreign} instead" if not discovered and foreign
            else "no annual filings of any recognised form in window" if not discovered
            else ""
        ),
    }


def build_filing_base_url(cik: int, accession_number: str) -> str:
    return f"{SEC_BASE_URL}/Archives/edgar/data/{cik}/{accession_number.replace('-', '')}"


def download_filing_into_archive(
    connection: duckdb.DuckDBPyConnection,
    filing: DiscoveredFiling,
    verbose: bool = False,
) -> dict[str, Any]:
    """Downloads only the XBRL document set for one filing and stores it in
    the compressed archive. Skips filings already archived."""
    already = connection.execute(
        "SELECT COUNT(*) FROM filing_archive_manifest WHERE accession_number = ?",
        [filing.accession_number],
    ).fetchone()[0]
    if already:
        return {"status": "ALREADY_ARCHIVED", "accession_number": filing.accession_number,
                "ticker": filing.ticker, "report_date": filing.report_date}

    base_url = build_filing_base_url(filing.cik, filing.accession_number)
    index = _sec_get_json(f"{base_url}/index.json")
    items = index.get("directory", {}).get("item", [])
    if not isinstance(items, list):
        raise SecDownloadError(f"{filing.accession_number}: unexpected index.json structure")

    records = []
    for item in items:
        file_name = str(item.get("name", "")).strip()
        if not file_name or not archive.is_needed_file(file_name, filing.primary_document):
            continue
        try:
            declared_size = int(item.get("size", ""))
        except (TypeError, ValueError):
            declared_size = None

        content = _sec_get_bytes(f"{base_url}/{file_name}")
        records.append(archive.archive_file(
            connection=connection, accession_number=filing.accession_number,
            file_name=file_name, raw_bytes=content, declared_size=declared_size,
        ))
        if verbose:
            print(f"      {file_name} ({records[-1]['byte_size']:,} -> {records[-1]['compressed_byte_size']:,} gz)")

    if not records:
        raise SecDownloadError(f"{filing.accession_number}: no needed files found in the filing index")
    if filing.primary_document not in {r["file_name"] for r in records}:
        raise SecDownloadError(
            f"{filing.accession_number}: primary document {filing.primary_document} was not archived"
        )

    archived_names = [r["file_name"] for r in records]
    xbrl_present = archive.has_xbrl_document_set(archived_names)

    archive.archive_manifest_row(
        connection=connection, accession_number=filing.accession_number,
        ticker=filing.ticker, cik=filing.cik, company_name=filing.company_name,
        form=filing.form, report_date=filing.report_date, filing_date=filing.filing_date,
        primary_document=filing.primary_document, file_count=len(records),
        total_uncompressed_bytes=sum(r["byte_size"] for r in records),
        total_compressed_bytes=sum(r["compressed_byte_size"] for r in records),
        source="DOWNLOADED" if xbrl_present else archive.NO_XBRL_DOCUMENT_SET,
    )

    if not xbrl_present:
        return {
            "status": archive.NO_XBRL_DOCUMENT_SET,
            "accession_number": filing.accession_number, "ticker": filing.ticker,
            "report_date": filing.report_date, "form": filing.form,
            "file_count": len(records), "archived_files": archived_names,
            "reason": ("filing carries no XBRL document set (no schema, linkbase or "
                       "instance document), so it holds no machine-readable financial "
                       "data. Common for a company's first annual report after an IPO."),
        }

    return {
        "status": "DOWNLOADED", "accession_number": filing.accession_number,
        "ticker": filing.ticker, "report_date": filing.report_date, "form": filing.form,
        "file_count": len(records),
        "total_uncompressed_bytes": sum(r["byte_size"] for r in records),
        "total_compressed_bytes": sum(r["compressed_byte_size"] for r in records),
    }


__all__ = [
    "ANNUAL_FORMS", "FOREIGN_ANNUAL_FORMS", "DiscoveredFiling", "SecDownloadError",
    "build_filing_base_url", "discover_annual_filings", "download_filing_into_archive",
    "find_company_record", "load_all_filings",
]
