from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests


# ============================================================
# הגדרות
# ============================================================

TICKER = "MSFT"
CIK_PADDED = "0000789019"
CIK_ARCHIVE = str(int(CIK_PADDED))

CONTACT_EMAIL = "shaiattias@gmail.com"
APPLICATION_NAME = "AI Stock Agent"

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
# תיקיות
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

OUTPUT_DIR = (
    PROJECT_DIR
    / "data"
    / "sec_filings"
    / TICKER
)

SUBMISSIONS_FILE = (
    OUTPUT_DIR
    / f"{TICKER}_submissions.json"
)

MANIFEST_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_10k_filings_manifest.csv"
)


# ============================================================
# HTTP
# ============================================================

def build_headers() -> dict[str, str]:
    if CONTACT_EMAIL == "YOUR_EMAIL@example.com":
        raise ValueError(
            "יש להחליף את CONTACT_EMAIL "
            "בכתובת האימייל שלך."
        )

    return {
        "User-Agent": (
            f"{APPLICATION_NAME} "
            f"{CONTACT_EMAIL}"
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


# ============================================================
# Submissions
# ============================================================

def download_submissions(
    headers: dict[str, str],
) -> dict:
    url = (
        "https://data.sec.gov/submissions/"
        f"CIK{CIK_PADDED}.json"
    )

    content = download_bytes(
        url,
        headers,
    )

    try:
        data = json.loads(
            content.decode("utf-8")
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError(
            "קובץ submissions שהתקבל אינו JSON תקין."
        ) from exc

    required_keys = {
        "cik",
        "name",
        "filings",
    }

    missing_keys = required_keys - set(data)

    if missing_keys:
        raise RuntimeError(
            "בקובץ submissions חסרים שדות:\n"
            f"{sorted(missing_keys)}"
        )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    with SUBMISSIONS_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    return data


def build_recent_filings_table(
    submissions: dict,
) -> pd.DataFrame:
    recent = (
        submissions
        .get("filings", {})
        .get("recent", {})
    )

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
        if column not in recent
    ]

    if missing_columns:
        raise RuntimeError(
            "ב-submissions חסרות עמודות:\n"
            f"{missing_columns}"
        )

    df = pd.DataFrame(
        {
            column: recent[column]
            for column in required_columns
        }
    )

    df["filingDate"] = pd.to_datetime(
        df["filingDate"],
        errors="coerce",
    )

    df["reportDate"] = pd.to_datetime(
        df["reportDate"],
        errors="coerce",
    )

    df = df[
        df["filingDate"].notna()
        & df["accessionNumber"].notna()
        & df["primaryDocument"].notna()
    ].copy()

    return df


# ============================================================
# בחירת הדוחות
# ============================================================

def select_latest_10k(
    filings_df: pd.DataFrame,
    as_of_date: str,
) -> pd.Series:
    as_of_timestamp = pd.Timestamp(as_of_date)

    available = filings_df[
        filings_df["form"].isin(
            ["10-K", "10-K/A"]
        )
        & (
            filings_df["filingDate"]
            <= as_of_timestamp
        )
    ].copy()

    if available.empty:
        raise RuntimeError(
            f"לא נמצא 10-K זמין עד {as_of_date}."
        )

    # מעדיפים 10-K רגיל על תיקון 10-K/A.
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


def build_filing_urls(
    accession_number: str,
    primary_document: str,
) -> tuple[str, str]:
    accession_without_dashes = (
        accession_number.replace("-", "")
    )

    filing_base_url = (
        "https://www.sec.gov/Archives/"
        f"edgar/data/{CIK_ARCHIVE}/"
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


# ============================================================
# הורדת הדוחות
# ============================================================

def save_filing(
    selected: pd.Series,
    headers: dict[str, str],
) -> dict:
    accession_number = str(
        selected["accessionNumber"]
    )

    primary_document = str(
        selected["primaryDocument"]
    )

    filing_date = selected["filingDate"]
    report_date = selected["reportDate"]

    (
        primary_document_url,
        filing_index_url,
    ) = build_filing_urls(
        accession_number,
        primary_document,
    )

    accession_without_dashes = (
        accession_number.replace("-", "")
    )

    filing_dir = (
        OUTPUT_DIR
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
        "filing_date": filing_date.date(),
        "report_date": (
            report_date.date()
            if pd.notna(report_date)
            else None
        ),
        "primary_document": primary_document,
        "primary_document_url": (
            primary_document_url
        ),
        "filing_index_url": filing_index_url,
        "local_document_file": str(
            document_file
        ),
        "local_index_file": str(
            index_file
        ),
    }


# ============================================================
# Main
# ============================================================

def main() -> None:
    headers = build_headers()

    print("=" * 75)
    print("Microsoft full 10-K filings download")
    print("=" * 75)

    submissions = download_submissions(
        headers
    )

    print()
    print(
        "חברה:",
        submissions.get("name"),
    )

    filings_df = build_recent_filings_table(
        submissions
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
            }
        )

    selection_df = pd.DataFrame(
        selections
    )

    # אותו 10-K עשוי לשרת כמה תאריכי דירוג.
    unique_accessions = (
        selection_df[
            "accession_number"
        ]
        .drop_duplicates()
        .tolist()
    )

    downloaded_filings = {}

    for accession_number in unique_accessions:
        selected_row = filings_df[
            filings_df["accessionNumber"]
            == accession_number
        ].sort_values(
            by=[
                "filingDate",
                "form",
            ]
        ).iloc[-1]

        downloaded_filings[
            accession_number
        ] = save_filing(
            selected_row,
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

    MANIFEST_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest_df.to_csv(
        MANIFEST_FILE,
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
        "date_rule_passed",
    ]

    print()
    print("=" * 115)
    print("Selected Microsoft filings")
    print("=" * 115)

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
        f"{OUTPUT_DIR}"
    )

    print()
    print(
        "קובץ המיפוי נשמר כאן:\n"
        f"{MANIFEST_FILE}"
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
        "הבדיקה עברה: לכל תאריך נבחר "
        "רק 10-K שהיה זמין באותו מועד."
    )


if __name__ == "__main__":
    main()