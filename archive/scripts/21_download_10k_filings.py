from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd
import requests


# ============================================================
# מיקומי קבצים והגדרות
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

APPLICATION_NAME = "AI Stock Agent"
CONTACT_EMAIL = "shaiattias@gmail.com"

AS_OF_DATES = [
    "2021-04-01",
    "2022-04-01",
    "2023-04-01",
    "2024-04-01",
    "2025-04-01",
]

REQUEST_TIMEOUT_SECONDS = 45
REQUEST_DELAY_SECONDS = 0.2


# ============================================================
# פרמטרים מה-Terminal
# ============================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download SEC 10-K filings available "
            "at historical as-of dates."
        )
    )

    parser.add_argument(
        "--ticker",
        required=True,
        help="Stock ticker, for example META",
    )

    parser.add_argument(
        "--cik",
        required=True,
        help="SEC CIK, for example 0001326801",
    )

    parser.add_argument(
        "--company-name",
        required=True,
        help='Company name, for example "Meta Platforms, Inc."',
    )

    return parser.parse_args()


def validate_inputs(
    ticker: str,
    cik: str,
    company_name: str,
) -> tuple[str, str, str]:
    ticker = ticker.strip().upper()
    cik = cik.strip().zfill(10)
    company_name = company_name.strip()

    if not ticker:
        raise ValueError("הטיקר ריק.")

    if not cik.isdigit() or len(cik) != 10:
        raise ValueError(
            "ה-CIK חייב להכיל בדיוק 10 ספרות."
        )

    if not company_name:
        raise ValueError("שם החברה ריק.")

    if not CONTACT_EMAIL:
        raise ValueError(
            "כתובת CONTACT_EMAIL חסרה."
        )

    return ticker, cik, company_name


# ============================================================
# תקשורת עם SEC
# ============================================================

def build_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            f"{APPLICATION_NAME} {CONTACT_EMAIL}"
        ),
        "Accept": (
            "application/json,text/html,"
            "application/xhtml+xml"
        ),
        "Accept-Encoding": "gzip, deflate",
    }


def download_bytes(
    url: str,
    headers: dict[str, str],
) -> bytes:
    print()
    print(f"מוריד:\n{url}")

    response = requests.get(
        url,
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    print(f"HTTP status: {response.status_code}")

    response.raise_for_status()

    time.sleep(REQUEST_DELAY_SECONDS)

    return response.content


def download_json(
    url: str,
    headers: dict[str, str],
) -> dict:
    content = download_bytes(
        url,
        headers,
    )

    try:
        return json.loads(
            content.decode("utf-8")
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError(
            f"הקובץ שהתקבל אינו JSON תקין:\n{url}"
        ) from exc


# ============================================================
# הורדת submissions
# ============================================================

def download_submissions(
    cik: str,
    output_dir: Path,
    headers: dict[str, str],
) -> dict:
    url = (
        "https://data.sec.gov/submissions/"
        f"CIK{cik}.json"
    )

    submissions = download_json(
        url,
        headers,
    )

    required_keys = {
        "cik",
        "name",
        "filings",
    }

    missing_keys = (
        required_keys
        - set(submissions)
    )

    if missing_keys:
        raise RuntimeError(
            "בקובץ submissions חסרים שדות:\n"
            f"{sorted(missing_keys)}"
        )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    submissions_file = (
        output_dir
        / "submissions.json"
    )

    with submissions_file.open(
        mode="w",
        encoding="utf-8",
    ) as file:
        json.dump(
            submissions,
            file,
            ensure_ascii=False,
            indent=2,
        )

    return submissions


# ============================================================
# בניית טבלת דיווחים מלאה:
# recent + historical files
# ============================================================

def dataframe_from_filing_block(
    filing_block: dict,
    source_name: str,
) -> pd.DataFrame:
    required_columns = [
        "accessionNumber",
        "filingDate",
        "reportDate",
        "form",
        "primaryDocument",
        "primaryDocDescription",
    ]

    missing_columns = [
        column
        for column in required_columns
        if column not in filing_block
    ]

    if missing_columns:
        raise RuntimeError(
            f"במקור {source_name} חסרות עמודות:\n"
            f"{missing_columns}"
        )

    lengths = {
        len(filing_block[column])
        for column in required_columns
    }

    if len(lengths) != 1:
        raise RuntimeError(
            f"במקור {source_name} אורכי העמודות "
            "אינם זהים."
        )

    df = pd.DataFrame(
        {
            column: filing_block[column]
            for column in required_columns
        }
    )

    df["source_name"] = source_name

    return df


def build_filings_table(
    submissions: dict,
    headers: dict[str, str],
) -> pd.DataFrame:
    tables = []

    filings_section = submissions.get(
        "filings",
        {},
    )

    recent = filings_section.get(
        "recent",
        {},
    )

    if recent:
        recent_df = dataframe_from_filing_block(
            recent,
            "recent",
        )

        tables.append(recent_df)

    historical_files = filings_section.get(
        "files",
        [],
    )

    for file_info in historical_files:
        file_name = file_info.get("name")

        if not file_name:
            continue

        url = (
            "https://data.sec.gov/submissions/"
            f"{file_name}"
        )

        historical = download_json(
            url,
            headers,
        )

        historical_df = dataframe_from_filing_block(
            historical,
            file_name,
        )

        tables.append(historical_df)

    if not tables:
        raise RuntimeError(
            "לא נמצאו רשומות דיווח ב-submissions."
        )

    df = pd.concat(
        tables,
        ignore_index=True,
    )

    df["filingDate"] = pd.to_datetime(
        df["filingDate"],
        errors="coerce",
    )

    df["reportDate"] = pd.to_datetime(
        df["reportDate"],
        errors="coerce",
    )

    df["accessionNumber"] = (
        df["accessionNumber"]
        .astype(str)
        .str.strip()
    )

    df["form"] = (
        df["form"]
        .astype(str)
        .str.strip()
    )

    df["primaryDocument"] = (
        df["primaryDocument"]
        .astype(str)
        .str.strip()
    )

    df = df[
        df["filingDate"].notna()
        & df["accessionNumber"].ne("")
        & df["primaryDocument"].ne("")
    ].copy()

    df = df.drop_duplicates(
        subset=[
            "accessionNumber",
            "form",
            "primaryDocument",
        ]
    )

    if df.empty:
        raise RuntimeError(
            "לא נשארו דיווחים תקינים לאחר הניקוי."
        )

    return df


# ============================================================
# בחירת 10-K שהיה זמין בכל תאריך בדיקה
# ============================================================

def select_latest_10k(
    filings_df: pd.DataFrame,
    as_of_date: str,
) -> pd.Series:
    as_of_timestamp = pd.Timestamp(
        as_of_date
    )

    available = filings_df[
        filings_df["form"].isin(
            [
                "10-K",
                "10-K/A",
            ]
        )
        & (
            filings_df["filingDate"]
            <= as_of_timestamp
        )
    ].copy()

    if available.empty:
        raise RuntimeError(
            f"לא נמצא 10-K שהיה זמין עד "
            f"{as_of_date}."
        )

    available["form_priority"] = (
        available["form"]
        .map(
            {
                "10-K": 1,
                "10-K/A": 0,
            }
        )
        .fillna(0)
    )

    available = available.sort_values(
        by=[
            "filingDate",
            "form_priority",
            "accessionNumber",
        ]
    )

    return available.iloc[-1]


# ============================================================
# כתובות SEC ושמירת הדוחות
# ============================================================

def build_filing_urls(
    cik_archive: str,
    accession_number: str,
    primary_document: str,
) -> tuple[str, str]:
    accession_without_dashes = (
        accession_number.replace("-", "")
    )

    filing_base_url = (
        "https://www.sec.gov/Archives/"
        f"edgar/data/{cik_archive}/"
        f"{accession_without_dashes}"
    )

    primary_document_url = (
        f"{filing_base_url}/"
        f"{primary_document}"
    )

    filing_index_url = (
        f"{filing_base_url}/index.json"
    )

    return (
        primary_document_url,
        filing_index_url,
    )


def save_filing(
    selected: pd.Series,
    cik_archive: str,
    output_dir: Path,
    headers: dict[str, str],
) -> dict:
    accession_number = str(
        selected["accessionNumber"]
    )

    primary_document = str(
        selected["primaryDocument"]
    )

    (
        primary_document_url,
        filing_index_url,
    ) = build_filing_urls(
        cik_archive,
        accession_number,
        primary_document,
    )

    accession_without_dashes = (
        accession_number.replace("-", "")
    )

    filing_dir = (
        output_dir
        / accession_without_dashes
    )

    filing_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    document_content = download_bytes(
        primary_document_url,
        headers,
    )

    document_file = (
        filing_dir
        / primary_document
    )

    document_file.write_bytes(
        document_content
    )

    index_content = download_bytes(
        filing_index_url,
        headers,
    )

    index_file = (
        filing_dir
        / "index.json"
    )

    index_file.write_bytes(
        index_content
    )

    return {
        "accession_number": accession_number,
        "form": selected["form"],
        "filing_date": (
            selected["filingDate"].date()
        ),
        "report_date": (
            selected["reportDate"].date()
            if pd.notna(
                selected["reportDate"]
            )
            else None
        ),
        "primary_document": primary_document,
        "primary_document_url": (
            primary_document_url
        ),
        "filing_index_url": (
            filing_index_url
        ),
        "local_document_file": str(
            document_file
        ),
        "local_index_file": str(
            index_file
        ),
        "submissions_source": (
            selected["source_name"]
        ),
    }


# ============================================================
# Main
# ============================================================

def main() -> None:
    args = parse_arguments()

    (
        ticker,
        cik,
        company_name,
    ) = validate_inputs(
        args.ticker,
        args.cik,
        args.company_name,
    )

    cik_archive = str(
        int(cik)
    )

    output_dir = (
        PROJECT_DIR
        / "data"
        / "sec_filings"
        / ticker
    )

    manifest_file = (
        PROJECT_DIR
        / "data"
        / f"{ticker.lower()}_10k_filings_manifest.csv"
    )

    headers = build_headers()

    print("=" * 85)
    print("SEC 10-K filings download")
    print("=" * 85)

    print(f"Ticker:  {ticker}")
    print(f"CIK:     {cik}")
    print(f"Company: {company_name}")

    submissions = download_submissions(
        cik,
        output_dir,
        headers,
    )

    filings_df = build_filings_table(
        submissions,
        headers,
    )

    ten_k_count = int(
        filings_df["form"]
        .isin(
            [
                "10-K",
                "10-K/A",
            ]
        )
        .sum()
    )

    print()
    print(
        "מספר דיווחי 10-K/10-K/A שנמצאו:",
        ten_k_count,
    )

    selections = []

    for as_of_date in AS_OF_DATES:
        selected = select_latest_10k(
            filings_df,
            as_of_date,
        )

        selections.append(
            {
                "as_of_date": as_of_date,
                "accession_number": (
                    selected["accessionNumber"]
                ),
                "form": selected["form"],
                "filing_date": (
                    selected["filingDate"]
                ),
                "report_date": (
                    selected["reportDate"]
                ),
                "primary_document": (
                    selected["primaryDocument"]
                ),
                "source_name": (
                    selected["source_name"]
                ),
            }
        )

    selection_df = pd.DataFrame(
        selections
    )

    unique_accessions = (
        selection_df[
            "accession_number"
        ]
        .drop_duplicates()
        .tolist()
    )

    downloaded_filings = {}

    for accession_number in unique_accessions:
        matching_rows = filings_df[
            filings_df["accessionNumber"]
            == accession_number
        ].copy()

        if matching_rows.empty:
            raise RuntimeError(
                f"לא נמצאה שורת מקור עבור "
                f"{accession_number}."
            )

        selected_row = (
            matching_rows
            .sort_values(
                by=[
                    "filingDate",
                    "form",
                ]
            )
            .iloc[-1]
        )

        downloaded_filings[
            accession_number
        ] = save_filing(
            selected_row,
            cik_archive,
            output_dir,
            headers,
        )

    manifest_rows = []

    for selection in selections:
        accession_number = (
            selection["accession_number"]
        )

        downloaded = downloaded_filings[
            accession_number
        ]

        as_of_timestamp = pd.Timestamp(
            selection["as_of_date"]
        )

        filing_timestamp = pd.Timestamp(
            downloaded["filing_date"]
        )

        manifest_rows.append(
            {
                "ticker": ticker,
                "company_name": company_name,
                "cik": cik,
                "as_of_date": (
                    selection["as_of_date"]
                ),
                **downloaded,
                "date_rule_passed": (
                    filing_timestamp
                    <= as_of_timestamp
                ),
            }
        )

    manifest_df = pd.DataFrame(
        manifest_rows
    )

    manifest_df.to_csv(
        manifest_file,
        index=False,
        encoding="utf-8-sig",
    )

    display_columns = [
        "as_of_date",
        "report_date",
        "filing_date",
        "form",
        "accession_number",
        "primary_document",
        "submissions_source",
        "date_rule_passed",
    ]

    print()
    print("=" * 150)
    print(f"Selected {ticker} 10-K filings")
    print("=" * 150)

    print(
        manifest_df[
            display_columns
        ].to_string(
            index=False
        )
    )

    print()
    print(
        "הדוחות נשמרו כאן:\n"
        f"{output_dir}"
    )

    print()
    print(
        "קובץ המיפוי נשמר כאן:\n"
        f"{manifest_file}"
    )

    if len(manifest_df) != len(
        AS_OF_DATES
    ):
        raise RuntimeError(
            "לא נוצרו בדיוק חמש שורות מיפוי."
        )

    if not manifest_df[
        "date_rule_passed"
    ].all():
        raise RuntimeError(
            "לפחות דוח אחד הוגש לאחר "
            "תאריך הבדיקה."
        )

    print()
    print(
        "הבדיקה עברה: נבחרו והורדו דוחות "
        "10-K שהיו זמינים בכל אחד מחמשת "
        "תאריכי הבדיקה."
    )


if __name__ == "__main__":
    main()