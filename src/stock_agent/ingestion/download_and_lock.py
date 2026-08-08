"""Downloads and disk-locks a SEC filing for ANY ticker -- currently
listed or long delisted -- using the SAME `locked_filing_manifest.json`
format and directory layout `scripts/107_download_accession_locked_
filing_any_form.py` already produces (which `warehouse/loader.py`'s
`find_locked_manifest()` already reads unchanged). CIK resolution is
routed through `ingestion.cik_resolver.resolve_company_record()` instead
of a direct `company_tickers.json`-only lookup, so the exact same
function runs whether the ticker is still listed or was removed from an
index years ago -- no special-casing anywhere in this module for
"delisted"; only `cik_resolver` decides, per ticker, which resolution
tier actually returns a result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

from stock_agent import DATA_DIR
from stock_agent.ingestion.cik_resolver import resolve_company_record
from stock_agent.ingestion.rate_limiter import SEC_RATE_LIMITER

SEC_BASE_URL = "https://www.sec.gov"
SEC_DATA_URL = "https://data.sec.gov"
USER_AGENT = "Shai Attias shaiattias@gmail.com"
REQUEST_TIMEOUT_SECONDS = 60
LOCKED_FILINGS_DIR = DATA_DIR / "sec_filings_locked"


def _sec_get_bytes(url: str) -> bytes:
    SEC_RATE_LIMITER.acquire()
    request = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/html,application/xhtml+xml,application/xml,text/plain,*/*",
        "Accept-Encoding": "identity", "Connection": "close",
    })
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as error:
        raise RuntimeError(f"SEC HTTP error {error.code} for {url}") from error
    except URLError as error:
        raise RuntimeError(f"Could not connect to SEC: {url}: {error}") from error


def _sec_get_json(url: str) -> dict[str, Any]:
    return json.loads(_sec_get_bytes(url).decode("utf-8"))


def load_all_filings(cik: int) -> pd.DataFrame:
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
        historical_frame = pd.DataFrame(_sec_get_json(f"{SEC_DATA_URL}/submissions/{file_name}"))
        if not historical_frame.empty:
            frames.append(historical_frame)
    if not frames:
        raise RuntimeError(f"No filing history found for CIK {cik}.")
    return pd.concat(frames, ignore_index=True, sort=False)


def select_most_recent_filing(filings: pd.DataFrame, form: str) -> pd.Series:
    """Selects the single most recent `form` filing by reportDate -- used
    for a delisted company where the caller wants "whatever the last one
    was" rather than a specific, already-known report_date."""
    candidates = filings[filings["form"] == form].copy()
    if candidates.empty:
        raise RuntimeError(f"No {form} filings found.")
    return candidates.sort_values("reportDate", ascending=False).iloc[0]


def select_exact_filing(filings: pd.DataFrame, form: str, report_date: str) -> pd.Series:
    candidates = filings[(filings["form"] == form) & (filings["reportDate"] == report_date)].copy()
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one {form} filing for reportDate {report_date}, found {len(candidates)}.")
    return candidates.iloc[0]


def _should_download(file_name: str) -> bool:
    allowed_suffixes = (".htm", ".html", ".xml", ".xsd", ".json", ".txt")
    return file_name.lower().endswith(allowed_suffixes)


def download_and_lock_filing(
    ticker: str, form: str, report_date: str | None = None, company_name_hint: str | None = None,
) -> dict:
    """Resolves CIK, locates the exact filing (or the most recent `form`
    filing if `report_date` is None), downloads every needed file, and
    writes `locked_filing_manifest.json` in the same format/location
    scripts/107 already uses. Returns the manifest dict."""
    company_record = resolve_company_record(ticker, company_name_hint=company_name_hint, form=form)
    cik = int(company_record["cik_str"])
    company_name = str(company_record.get("title", ""))

    filings = load_all_filings(cik)
    filing = select_exact_filing(filings, form, report_date) if report_date is not None else select_most_recent_filing(filings, form)

    accession_number = str(filing["accessionNumber"])
    accession_compact = accession_number.replace("-", "")
    output_directory = LOCKED_FILINGS_DIR / ticker.upper() / accession_compact
    output_directory.mkdir(parents=True, exist_ok=True)

    filing_base_url = f"{SEC_BASE_URL}/Archives/edgar/data/{cik}/{accession_compact}"
    filing_index = _sec_get_json(f"{filing_base_url}/index.json")
    index_items = filing_index.get("directory", {}).get("item", [])
    if not isinstance(index_items, list):
        raise RuntimeError("index.json has an unexpected structure.")

    downloaded_records: list[dict[str, Any]] = []
    for item in index_items:
        file_name = str(item.get("name", "")).strip()
        if not file_name or not _should_download(file_name):
            continue
        content = _sec_get_bytes(f"{filing_base_url}/{file_name}")
        (output_directory / file_name).write_bytes(content)
        downloaded_records.append({
            "file_name": file_name, "downloaded_size": len(content), "sec_reported_size": item.get("size", ""),
        })
    if not downloaded_records:
        raise RuntimeError(f"No files downloaded for {ticker} {form} accession {accession_number}.")

    primary_document = str(filing["primaryDocument"])
    primary_document_path = output_directory / primary_document
    if not primary_document_path.exists():
        raise FileNotFoundError(f"Primary document not found after download: {primary_document_path}")

    manifest = {
        "ticker": ticker.upper(), "company_name": company_name, "cik": cik,
        "form": str(filing["form"]), "report_date": str(filing["reportDate"]),
        "filing_date": str(filing["filingDate"]), "accession_number": accession_number,
        "primary_document": primary_document, "primary_document_path": str(primary_document_path.resolve()),
        "filing_base_url": filing_base_url, "output_directory": str(output_directory.resolve()),
        "downloaded_file_count": len(downloaded_records), "sec_user_agent": USER_AGENT,
        "cik_resolution_method": company_record["resolution_method"],
    }
    (output_directory / "locked_filing_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pd.DataFrame(downloaded_records).to_csv(output_directory / "downloaded_files_manifest.csv", index=False, encoding="utf-8-sig")
    return manifest
