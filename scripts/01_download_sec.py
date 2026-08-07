import argparse
import json
from pathlib import Path

import requests

from sec_companyfacts import get_companyfacts_path, get_project_dir


CIK_BY_TICKER = {
    "MSFT": "0000789019",
    "TSLA": "0001318605",
}

# החלף לכתובת האימייל האמיתית שלך
CONTACT_EMAIL = "shaiattias@gmail.com"


def build_sec_url(cik: str) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download SEC company facts for a ticker")
    parser.add_argument("ticker", nargs="?", default="MSFT", help="Ticker symbol to download, e.g. MSFT or TSLA")
    return parser.parse_args()


def main():
    if CONTACT_EMAIL == "YOUR_EMAIL@example.com":
        raise ValueError(
            "יש להחליף את CONTACT_EMAIL לכתובת האימייל שלך."
        )

    args = parse_args()
    ticker = args.ticker.strip().upper()

    if ticker not in CIK_BY_TICKER:
        raise ValueError(
            f"Ticker {ticker} is not supported. Supported values: {', '.join(sorted(CIK_BY_TICKER))}"
        )

    cik = CIK_BY_TICKER[ticker]
    sec_url = build_sec_url(cik)
    output_file = get_companyfacts_path(ticker, get_project_dir())
    output_file.parent.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": f"AI Stock Agent {CONTACT_EMAIL}",
        "Accept": "application/json",
    }

    print(f"מוריד נתונים עבור {ticker}...")
    print(sec_url)

    response = requests.get(
        sec_url,
        headers=headers,
        timeout=30,
    )

    print(f"HTTP status: {response.status_code}")
    response.raise_for_status()

    data = response.json()

    if "facts" not in data:
        raise RuntimeError(
            "התקבלה תשובה מה-SEC, אך לא נמצא השדה facts."
        )

    with output_file.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("ההורדה הסתיימה בהצלחה.")
    print(f"חברה: {data.get('entityName')}")
    print(f"הקובץ נשמר כאן: {output_file}")


if __name__ == "__main__":
    main()