"""
XBRL-only SEC filing downloader.

Supersedes scripts/36b_download_accession_locked_filing.py and
scripts/107_download_accession_locked_filing_any_form.py for NEW
downloads going forward (both left unchanged, per D-011 -- this is a
new file, not a patch). Same ticker/report-date/form-driven accession
locking (SEC company_tickers.json -> CIK -> submissions history ->
exact form+reportDate match -> accession), same header/timeout policy,
but two changes:

1. Only downloads files scripts/161_filings_archive_core.py's
   `is_needed_file()` says the pipeline actually reads (primary
   document, .xsd, the four linkbases if present separately,
   FilingSummary.xml, and a standalone XBRL instance document for
   pre-Inline-XBRL filings) -- not all ~106 files SEC serves per
   filing (R*.htm rendered viewer, the full-submission .txt,
   MetaLinks.json, exhibits, index pages).
2. Raised the request rate from 0.25s/request (4 req/s) to SEC's own
   documented limit of 10 req/s (0.1s/request).

Downloaded bytes go straight into the compressed DuckDB archive
(gzip BLOB + SHA-256, scripts/161) -- no loose files are written to
disk for new downloads.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import duckdb
import pandas as pd

from importlib import util as _importlib_util
import sys

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

sys.path.insert(0, str(PROJECT_DIR / "src"))
from stock_agent.ingestion.rate_limiter import SEC_RATE_LIMITER

_spec161 = _importlib_util.spec_from_file_location("s161", PROJECT_DIR / "scripts" / "161_filings_archive_core.py")
s161 = _importlib_util.module_from_spec(_spec161)
sys.modules["s161"] = s161
_spec161.loader.exec_module(s161)

SEC_BASE_URL = "https://www.sec.gov"
SEC_DATA_URL = "https://data.sec.gov"

USER_AGENT = "Shai Attias shaiattias@gmail.com"

REQUEST_DELAY_SECONDS = 0.1  # SEC's documented limit is 10 req/s
REQUEST_TIMEOUT_SECONDS = 60


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download only the XBRL-relevant files of an exact SEC filing into the compressed archive."
    )
    parser.add_argument("--ticker", required=True, help="Ticker, for example MSFT.")
    parser.add_argument("--report-date", required=True, help="Fiscal report date in YYYY-MM-DD format.")
    parser.add_argument("--form", default="10-K", help="Exact SEC form type, e.g. 10-K or 10-Q. Default 10-K.")
    return parser.parse_args()


def build_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/html,application/xhtml+xml,application/xml,text/plain,*/*",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }


def sec_get_bytes(url: str) -> bytes:
    SEC_RATE_LIMITER.acquire()
    request = Request(url, headers=build_headers())
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            content = response.read()
    except HTTPError as error:
        response_body = ""
        try:
            response_body = error.read().decode("utf-8", errors="ignore")
        except Exception:
            response_body = ""
        raise RuntimeError(f"SEC HTTP error.\nCode: {error.code}\nURL: {url}\nResponse: {response_body[:500]}") from error
    except URLError as error:
        raise RuntimeError(f"Could not connect to SEC.\nURL: {url}\nError: {error}") from error

    return content


def sec_get_json(url: str) -> dict[str, Any]:
    raw_content = sec_get_bytes(url)
    try:
        return json.loads(raw_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"SEC did not return valid JSON.\nURL: {url}") from error


def find_company_record(ticker: str) -> dict[str, Any]:
    ticker_data = sec_get_json(f"{SEC_BASE_URL}/files/company_tickers.json")
    matches = [r for r in ticker_data.values() if str(r.get("ticker", "")).upper() == ticker.upper()]
    if len(matches) != 1:
        raise RuntimeError(f"Did not find exactly one company for ticker {ticker}. Matches: {len(matches)}")
    return matches[0]


def load_all_filings(cik: int) -> pd.DataFrame:
    cik_padded = str(cik).zfill(10)
    submissions = sec_get_json(f"{SEC_DATA_URL}/submissions/CIK{cik_padded}.json")
    frames: list[pd.DataFrame] = []

    recent = submissions.get("filings", {}).get("recent", {})
    if recent:
        frames.append(pd.DataFrame(recent))

    for item in submissions.get("filings", {}).get("files", []):
        file_name = str(item.get("name", "")).strip()
        if not file_name:
            continue
        historical_json = sec_get_json(f"{SEC_DATA_URL}/submissions/{file_name}")
        historical_frame = pd.DataFrame(historical_json)
        if not historical_frame.empty:
            frames.append(historical_frame)

    if not frames:
        raise RuntimeError("No filing history found.")
    return pd.concat(frames, ignore_index=True, sort=False)


def select_exact_filing(filings: pd.DataFrame, form: str, report_date: str) -> pd.Series:
    required_columns = {"accessionNumber", "filingDate", "reportDate", "form", "primaryDocument"}
    missing_columns = required_columns - set(filings.columns)
    if missing_columns:
        raise RuntimeError("Missing fields in filing history:\n" + "\n".join(sorted(missing_columns)))

    candidates = filings[(filings["form"] == form) & (filings["reportDate"] == report_date)].copy()
    if len(candidates) != 1:
        raise RuntimeError(
            f"Did not find exactly one {form} filing for reportDate {report_date}. "
            f"Matches: {len(candidates)}"
        )
    return candidates.iloc[0]


def build_filing_base_url(cik: int, accession_number: str) -> str:
    accession_compact = accession_number.replace("-", "")
    return f"{SEC_BASE_URL}/Archives/edgar/data/{cik}/{accession_compact}"


def load_filing_index(filing_base_url: str) -> dict[str, Any]:
    return sec_get_json(f"{filing_base_url}/index.json")


def get_index_items(filing_index: dict[str, Any]) -> list[dict[str, Any]]:
    items = filing_index.get("directory", {}).get("item", [])
    if not isinstance(items, list):
        raise RuntimeError("index.json has an unexpected structure.")
    return items


def download_and_archive_needed_files(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    filing_base_url: str,
    index_items: list[dict[str, Any]],
    primary_document: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    for item in index_items:
        file_name = str(item.get("name", "")).strip()
        if not file_name or not s161.is_needed_file(file_name, primary_document):
            continue

        declared_size_raw = item.get("size", "")
        try:
            declared_size = int(declared_size_raw)
        except (TypeError, ValueError):
            declared_size = None

        file_url = f"{filing_base_url}/{file_name}"
        content = sec_get_bytes(file_url)

        record = s161.archive_file(
            connection=connection,
            accession_number=accession_number,
            file_name=file_name,
            raw_bytes=content,
            declared_size=declared_size,
        )
        records.append(record)
        print(f"Archived: {file_name} ({record['byte_size']:,} bytes -> {record['compressed_byte_size']:,} bytes gz)")

    if not records:
        raise RuntimeError("No needed files were found/downloaded for this filing.")
    return records


def main() -> None:
    arguments = parse_arguments()
    ticker = arguments.ticker.upper()
    report_date = arguments.report_date
    form = arguments.form

    print()
    print("=" * 110)
    print("XBRL-ONLY SEC DOWNLOAD -> COMPRESSED ARCHIVE")
    print("=" * 110)
    print(f"Ticker: {ticker}")
    print(f"Form: {form}")
    print(f"Report date: {report_date}")
    print(f"Request rate: {1 / REQUEST_DELAY_SECONDS:.0f} req/s")

    company_record = find_company_record(ticker)
    cik = int(company_record["cik_str"])
    company_name = str(company_record.get("title", ""))
    print(f"Company: {company_name}")
    print(f"CIK: {cik}")

    filings = load_all_filings(cik)
    filing = select_exact_filing(filings, form, report_date)

    accession_number = str(filing["accessionNumber"])
    filing_date = str(filing["filingDate"])
    primary_document = str(filing["primaryDocument"])

    print()
    print("Exact filing found:")
    print(f"Accession: {accession_number}")
    print(f"Filing date: {filing_date}")
    print(f"Primary document: {primary_document}")

    filing_base_url = build_filing_base_url(cik=cik, accession_number=accession_number)
    filing_index = load_filing_index(filing_base_url)
    index_items = get_index_items(filing_index)

    ARCHIVE_DB_PATH = s161.ARCHIVE_DB_PATH
    ARCHIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=str(ARCHIVE_DB_PATH))
    s161.create_archive_schema(connection)

    print()
    print("Downloading needed files only...")
    records = download_and_archive_needed_files(
        connection=connection,
        accession_number=accession_number,
        filing_base_url=filing_base_url,
        index_items=index_items,
        primary_document=primary_document,
    )

    total_uncompressed = sum(r["byte_size"] for r in records)
    total_compressed = sum(r["compressed_byte_size"] for r in records)

    s161.archive_manifest_row(
        connection=connection,
        accession_number=accession_number,
        ticker=ticker,
        cik=cik,
        company_name=company_name,
        form=form,
        report_date=report_date,
        filing_date=filing_date,
        primary_document=primary_document,
        file_count=len(records),
        total_uncompressed_bytes=total_uncompressed,
        total_compressed_bytes=total_compressed,
        source="DOWNLOADED",
    )
    connection.close()

    print()
    print("=" * 110)
    print("DOWNLOAD + ARCHIVE COMPLETE")
    print("=" * 110)
    print(f"Files archived: {len(records)}")
    print(f"Uncompressed: {total_uncompressed:,} bytes")
    print(f"Compressed:   {total_compressed:,} bytes")
    if total_uncompressed:
        print(f"Ratio: {total_uncompressed / max(total_compressed, 1):.2f}x")
    print(f"Archive DB: {ARCHIVE_DB_PATH.resolve()}")


if __name__ == "__main__":
    main()
