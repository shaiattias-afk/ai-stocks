from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "data" / "sec_raw"

# החלף בכתובת האימייל שכבר השתמשת בה.
CONTACT_EMAIL = "shaiattias@gmail.com"

APPLICATION_NAME = "AI Stock Agent"
TIMEOUT_SECONDS = 45


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download SEC Company Facts for one company."
    )

    parser.add_argument(
        "--ticker",
        required=True,
        help="Stock ticker, for example ORCL",
    )

    parser.add_argument(
        "--cik",
        required=True,
        help="SEC CIK, for example 0001341439",
    )

    return parser.parse_args()


def validate_inputs(ticker: str, cik: str) -> tuple[str, str]:
    ticker = ticker.strip().upper()
    cik = cik.strip().zfill(10)

    if not ticker:
        raise ValueError("הטיקר ריק.")

    if not cik.isdigit() or len(cik) != 10:
        raise ValueError(
            "ה-CIK חייב להכיל בדיוק 10 ספרות."
        )

    if CONTACT_EMAIL == "YOUR_EMAIL@example.com":
        raise ValueError(
            "יש להחליף את CONTACT_EMAIL "
            "בכתובת האימייל שלך."
        )

    return ticker, cik


def main() -> None:
    args = parse_arguments()

    ticker, cik = validate_inputs(
        args.ticker,
        args.cik,
    )

    url = (
        "https://data.sec.gov/api/xbrl/companyfacts/"
        f"CIK{cik}.json"
    )

    headers = {
        "User-Agent": (
            f"{APPLICATION_NAME} {CONTACT_EMAIL}"
        ),
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }

    print("=" * 70)
    print("SEC Company Facts download")
    print("=" * 70)
    print(f"Ticker: {ticker}")
    print(f"CIK:    {cik}")
    print(f"URL:    {url}")

    response = requests.get(
        url,
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )

    print(f"HTTP status: {response.status_code}")

    response.raise_for_status()

    data = response.json()

    required_fields = {
        "cik",
        "entityName",
        "facts",
    }

    missing_fields = required_fields - set(data)

    if missing_fields:
        raise RuntimeError(
            "תגובת SEC חסרה שדות:\n"
            f"{sorted(missing_fields)}"
        )

    returned_cik = str(data["cik"]).zfill(10)

    if returned_cik != cik:
        raise RuntimeError(
            f"CIK לא תואם: ביקשנו {cik}, "
            f"קיבלנו {returned_cik}."
        )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_file = (
        OUTPUT_DIR
        / f"{ticker}_companyfacts.json"
    )

    temporary_file = output_file.with_suffix(
        ".json.tmp"
    )

    with temporary_file.open(
        mode="w",
        encoding="utf-8",
    ) as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    temporary_file.replace(output_file)

    us_gaap_facts = (
        data.get("facts", {})
        .get("us-gaap", {})
    )

    print()
    print("ההורדה הסתיימה בהצלחה.")
    print(f"חברה: {data.get('entityName')}")
    print(f"מספר תגי us-gaap: {len(us_gaap_facts):,}")
    print(f"הקובץ נשמר כאן:\n{output_file}")


if __name__ == "__main__":
    main()