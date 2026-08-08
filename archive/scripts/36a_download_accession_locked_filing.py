from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

SEC_BASE_URL = "https://www.sec.gov"
SEC_DATA_URL = "https://data.sec.gov"

# מזהה ברור לבקשות ל-SEC.
USER_AGENT = "AI-Stock-Agent/1.0 personal-research"

REQUEST_DELAY_SECONDS = 0.15
REQUEST_TIMEOUT_SECONDS = 60


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Locate an exact SEC 10-K by ticker and report date, "
            "then download its complete filing package."
        )
    )

    parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker, for example ORCL.",
    )

    parser.add_argument(
        "--report-date",
        required=True,
        help="Fiscal report date in YYYY-MM-DD format.",
    )

    return parser.parse_args()


def sec_get_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
            "Host": url.split("/")[2],
        },
    )

    try:
        with urlopen(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            content = response.read()

    except HTTPError as error:
        raise RuntimeError(
            f"שגיאת HTTP מה־SEC: {error.code}\n"
            f"כתובת: {url}"
        ) from error

    except URLError as error:
        raise RuntimeError(
            "לא ניתן להתחבר ל־SEC.\n"
            f"כתובת: {url}\n"
            f"שגיאה: {error}"
        ) from error

    time.sleep(REQUEST_DELAY_SECONDS)

    return content


def sec_get_json(url: str) -> dict[str, Any]:
    raw_content = sec_get_bytes(url)

    try:
        return json.loads(
            raw_content.decode("utf-8")
        )

    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise RuntimeError(
            "ה־SEC לא החזיר JSON תקין.\n"
            f"כתובת: {url}"
        ) from error


def find_company_record(
    ticker: str,
) -> dict[str, Any]:
    tickers_url = (
        f"{SEC_BASE_URL}/files/company_tickers.json"
    )

    ticker_data = sec_get_json(tickers_url)

    matching_records = []

    for record in ticker_data.values():
        record_ticker = str(
            record.get("ticker", "")
        ).upper()

        if record_ticker == ticker.upper():
            matching_records.append(record)

    if len(matching_records) != 1:
        raise RuntimeError(
            "לא נמצאה חברה יחידה עבור ה־Ticker.\n"
            f"Ticker: {ticker}\n"
            f"מספר התאמות: {len(matching_records)}"
        )

    return matching_records[0]


def normalize_recent_filings(
    submissions: dict[str, Any],
) -> pd.DataFrame:
    recent = (
        submissions
        .get("filings", {})
        .get("recent", {})
    )

    if not recent:
        return pd.DataFrame()

    return pd.DataFrame(recent)


def load_historical_submission_files(
    submissions: dict[str, Any],
) -> list[pd.DataFrame]:
    historical_frames: list[pd.DataFrame] = []

    historical_files = (
        submissions
        .get("filings", {})
        .get("files", [])
    )

    for historical_file in historical_files:
        file_name = str(
            historical_file.get("name", "")
        ).strip()

        if not file_name:
            continue

        historical_url = (
            f"{SEC_DATA_URL}/submissions/{file_name}"
        )

        historical_json = sec_get_json(
            historical_url
        )

        historical_frame = pd.DataFrame(
            historical_json
        )

        if not historical_frame.empty:
            historical_frames.append(
                historical_frame
            )

    return historical_frames


def load_all_filings(
    cik: int,
) -> pd.DataFrame:
    cik_padded = str(cik).zfill(10)

    submissions_url = (
        f"{SEC_DATA_URL}/submissions/"
        f"CIK{cik_padded}.json"
    )

    submissions = sec_get_json(
        submissions_url
    )

    frames = []

    recent_frame = normalize_recent_filings(
        submissions
    )

    if not recent_frame.empty:
        frames.append(recent_frame)

    frames.extend(
        load_historical_submission_files(
            submissions
        )
    )

    if not frames:
        raise RuntimeError(
            "לא נמצאה היסטוריית הגשות ב־SEC."
        )

    combined = pd.concat(
        frames,
        ignore_index=True,
        sort=False,
    )

    return combined


def select_exact_filing(
    filings: pd.DataFrame,
    report_date: str,
) -> pd.Series:
    required_columns = {
        "accessionNumber",
        "filingDate",
        "reportDate",
        "form",
        "primaryDocument",
    }

    missing_columns = (
        required_columns
        - set(filings.columns)
    )

    if missing_columns:
        raise RuntimeError(
            "חסרים שדות בהיסטוריית ההגשות:\n"
            + "\n".join(
                sorted(missing_columns)
            )
        )

    candidates = filings[
        (filings["form"] == "10-K")
        & (
            filings["reportDate"]
            == report_date
        )
    ].copy()

    if len(candidates) != 1:
        print()
        print(
            "הגשות 10-K שנמצאו בסביבת התאריך:"
        )

        nearby = filings[
            filings["form"] == "10-K"
        ][
            [
                "reportDate",
                "filingDate",
                "accessionNumber",
                "primaryDocument",
            ]
        ].sort_values(
            "reportDate",
            ascending=False,
        ).head(10)

        print(
            nearby.to_string(
                index=False
            )
        )

        raise RuntimeError(
            "לא נמצאה הגשת 10-K יחידה "
            "התואמת בדיוק ל־reportDate.\n"
            f"reportDate: {report_date}\n"
            f"מספר התאמות: {len(candidates)}"
        )

    return candidates.iloc[0]


def build_filing_base_url(
    cik: int,
    accession_number: str,
) -> str:
    accession_without_dashes = (
        accession_number.replace("-", "")
    )

    return (
        f"{SEC_BASE_URL}/Archives/edgar/data/"
        f"{cik}/{accession_without_dashes}"
    )


def load_filing_index(
    filing_base_url: str,
) -> dict[str, Any]:
    index_url = (
        f"{filing_base_url}/index.json"
    )

    return sec_get_json(index_url)


def extract_index_items(
    filing_index: dict[str, Any],
) -> list[dict[str, Any]]:
    directory = filing_index.get(
        "directory",
        {}
    )

    items = directory.get(
        "item",
        []
    )

    if not isinstance(items, list):
        raise RuntimeError(
            "מבנה index.json של ההגשה אינו תקין."
        )

    return items


def should_download_file(
    file_name: str,
) -> bool:
    lower_name = file_name.lower()

    allowed_suffixes = {
        ".htm",
        ".html",
        ".xml",
        ".xsd",
        ".json",
        ".txt",
    }

    return any(
        lower_name.endswith(suffix)
        for suffix in allowed_suffixes
    )


def download_filing_package(
    filing_base_url: str,
    output_directory: Path,
    index_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    downloaded_records = []

    for item in index_items:
        file_name = str(
            item.get("name", "")
        ).strip()

        if (
            not file_name
            or not should_download_file(file_name)
        ):
            continue

        file_url = (
            f"{filing_base_url}/{file_name}"
        )

        local_file = (
            output_directory / file_name
        )

        file_content = sec_get_bytes(
            file_url
        )

        local_file.write_bytes(
            file_content
        )

        downloaded_records.append(
            {
                "file_name": file_name,
                "file_url": file_url,
                "local_file": str(
                    local_file.resolve()
                ),
                "downloaded_size": len(
                    file_content
                ),
                "sec_reported_size": item.get(
                    "size",
                    "",
                ),
                "last_modified": item.get(
                    "last-modified",
                    "",
                ),
            }
        )

        print(
            f"הורד: {file_name}"
        )

    if not downloaded_records:
        raise RuntimeError(
            "לא הורדו קבצים מחבילת ההגשה."
        )

    return downloaded_records


def save_locked_filing_manifest(
    ticker: str,
    cik: int,
    filing: pd.Series,
    filing_base_url: str,
    output_directory: Path,
    downloaded_records: list[dict[str, Any]],
) -> tuple[Path, Path]:
    accession_number = str(
        filing["accessionNumber"]
    )

    primary_document = str(
        filing["primaryDocument"]
    )

    primary_document_path = (
        output_directory
        / primary_document
    )

    if not primary_document_path.exists():
        raise FileNotFoundError(
            "ה־Primary Document שנרשם ב־SEC "
            "לא נמצא בחבילה שהורדה:\n"
            f"{primary_document_path}"
        )

    filing_manifest = {
        "ticker": ticker.upper(),
        "cik": cik,
        "company_name": "",
        "form": str(filing["form"]),
        "report_date": str(
            filing["reportDate"]
        ),
        "filing_date": str(
            filing["filingDate"]
        ),
        "accession_number": accession_number,
        "primary_document": primary_document,
        "primary_document_path": str(
            primary_document_path.resolve()
        ),
        "filing_base_url": filing_base_url,
        "output_directory": str(
            output_directory.resolve()
        ),
        "downloaded_file_count": len(
            downloaded_records
        ),
    }

    json_manifest_file = (
        output_directory
        / "locked_filing_manifest.json"
    )

    json_manifest_file.write_text(
        json.dumps(
            filing_manifest,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    csv_manifest_file = (
        output_directory
        / "downloaded_files_manifest.csv"
    )

    pd.DataFrame(
        downloaded_records
    ).to_csv(
        csv_manifest_file,
        index=False,
        encoding="utf-8-sig",
    )

    return (
        json_manifest_file,
        csv_manifest_file,
    )


def main() -> None:
    arguments = parse_arguments()

    ticker = arguments.ticker.upper()
    report_date = arguments.report_date

    print()
    print("=" * 100)
    print(
        "ACCESSION-LOCKED SEC FILING DOWNLOAD"
    )
    print("=" * 100)

    print(f"Ticker: {ticker}")
    print(f"Report date: {report_date}")

    company_record = find_company_record(
        ticker
    )

    cik = int(
        company_record["cik_str"]
    )

    company_name = str(
        company_record.get("title", "")
    )

    print(f"Company: {company_name}")
    print(f"CIK: {cik}")

    filings = load_all_filings(
        cik
    )

    filing = select_exact_filing(
        filings=filings,
        report_date=report_date,
    )

    accession_number = str(
        filing["accessionNumber"]
    )

    filing_date = str(
        filing["filingDate"]
    )

    primary_document = str(
        filing["primaryDocument"]
    )

    print()
    print("נמצאה הגשה מדויקת:")
    print(f"Form: {filing['form']}")
    print(
        f"Accession: {accession_number}"
    )
    print(f"Filing date: {filing_date}")
    print(
        f"Primary document: {primary_document}"
    )

    filing_base_url = build_filing_base_url(
        cik=cik,
        accession_number=accession_number,
    )

    accession_without_dashes = (
        accession_number.replace("-", "")
    )

    output_directory = (
        DATA_DIR
        / "sec_filings_locked"
        / ticker
        / accession_without_dashes
    )

    filing_index = load_filing_index(
        filing_base_url
    )

    index_items = extract_index_items(
        filing_index
    )

    print()
    print(
        "מוריד את חבילת ההגשה המלאה..."
    )

    downloaded_records = (
        download_filing_package(
            filing_base_url=filing_base_url,
            output_directory=output_directory,
            index_items=index_items,
        )
    )

    (
        json_manifest_file,
        csv_manifest_file,
    ) = save_locked_filing_manifest(
        ticker=ticker,
        cik=cik,
        filing=filing,
        filing_base_url=filing_base_url,
        output_directory=output_directory,
        downloaded_records=downloaded_records,
    )

    primary_document_path = (
        output_directory
        / primary_document
    )

    print()
    print("=" * 100)
    print("הורדת ההגשה הסתיימה בהצלחה")
    print("=" * 100)

    print(
        f"Accession: {accession_number}"
    )
    print(
        f"Report date: {report_date}"
    )
    print(
        f"Filing date: {filing_date}"
    )
    print(
        f"מספר קבצים שהורדו: "
        f"{len(downloaded_records)}"
    )

    print()
    print(
        f"קובץ ה־10-K הראשי:\n"
        f"{primary_document_path.resolve()}"
    )

    print()
    print(
        f"Manifest נעילת ההגשה:\n"
        f"{json_manifest_file.resolve()}"
    )

    print()
    print(
        f"רשימת הקבצים שהורדו:\n"
        f"{csv_manifest_file.resolve()}"
    )

    print()
    print(
        "ההגשה נבחרה לפי Form=10-K "
        "ו־reportDate מדויק בלבד."
    )


if __name__ == "__main__":
    main()