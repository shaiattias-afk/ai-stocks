from pathlib import Path
import json

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

INPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "sec_raw"
    / "MSFT_companyfacts.json"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_short_term_investments_test.csv"
)


AS_OF_DATES = [
    "2021-04-01",
    "2022-04-01",
    "2023-04-01",
    "2024-04-01",
    "2025-04-01",
]


SHORT_TERM_INVESTMENT_TAGS = [
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
]


def load_company_facts():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המקור לא נמצא:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def find_available_tags(us_gaap_facts):
    available_tags = [
        tag
        for tag in SHORT_TERM_INVESTMENT_TAGS
        if tag in us_gaap_facts
    ]

    if not available_tags:
        raise RuntimeError(
            "לא נמצא אף תג מתאים להשקעות קצרות טווח.\n"
            f"נבדקו: {SHORT_TERM_INVESTMENT_TAGS}"
        )

    print("תגים שנמצאו:")

    for tag in available_tags:
        print(f"  {tag}")

    return available_tags


def build_instant_table(us_gaap_facts, tag):
    units = us_gaap_facts[tag].get("units", {})

    if "USD" not in units:
        raise RuntimeError(
            f"לא נמצאו נתוני USD עבור התג {tag}."
        )

    rows = []

    for item in units["USD"]:
        rows.append(
            {
                "tag": tag,
                "balance_sheet_date": item.get("end"),
                "value": item.get("val"),
                "filing_date": item.get("filed"),
                "form": item.get("form"),
                "fiscal_year": item.get("fy"),
                "fiscal_period": item.get("fp"),
                "accession_number": item.get("accn"),
                "frame": item.get("frame"),
            }
        )

    df = pd.DataFrame(rows)

    df["balance_sheet_date"] = pd.to_datetime(
        df["balance_sheet_date"],
        errors="coerce",
    )

    df["filing_date"] = pd.to_datetime(
        df["filing_date"],
        errors="coerce",
    )

    df["value"] = pd.to_numeric(
        df["value"],
        errors="coerce",
    )

    df = df[
        df["form"].isin(["10-K", "10-Q"])
        & df["balance_sheet_date"].notna()
        & df["filing_date"].notna()
        & df["value"].notna()
    ].copy()

    return df


def select_latest_available(df, as_of_date):
    as_of_timestamp = pd.Timestamp(as_of_date)

    available = df[
        df["filing_date"] <= as_of_timestamp
    ].copy()

    if available.empty:
        return None

    latest_balance_sheet_date = (
        available["balance_sheet_date"].max()
    )

    selected = available[
        available["balance_sheet_date"]
        == latest_balance_sheet_date
    ].copy()

    selected = selected.sort_values(
        by=[
            "filing_date",
            "accession_number",
        ]
    )

    return selected.iloc[-1]


def main():
    company_facts = load_company_facts()
    us_gaap_facts = company_facts["facts"]["us-gaap"]

    available_tags = find_available_tags(
        us_gaap_facts
    )

    fact_tables = []

    for tag in available_tags:
        tag_df = build_instant_table(
            us_gaap_facts,
            tag,
        )

        if not tag_df.empty:
            fact_tables.append(tag_df)

    if not fact_tables:
        raise RuntimeError(
            "נמצאו תגיות, אך לא נמצאו שורות תקינות."
        )

    all_facts_df = pd.concat(
        fact_tables,
        ignore_index=True,
    )

    results = []

    for as_of_date in AS_OF_DATES:
        selected = select_latest_available(
            all_facts_df,
            as_of_date,
        )

        if selected is None:
            results.append(
                {
                    "as_of_date": as_of_date,
                    "balance_sheet_date": None,
                    "filing_date": None,
                    "short_term_investments_usd": None,
                    "short_term_investments_usd_billions": None,
                    "selected_tag": None,
                    "date_rule_passed": False,
                }
            )
            continue

        as_of_timestamp = pd.Timestamp(as_of_date)

        results.append(
            {
                "as_of_date": as_of_date,
                "balance_sheet_date": (
                    selected["balance_sheet_date"].date()
                ),
                "filing_date": (
                    selected["filing_date"].date()
                ),
                "short_term_investments_usd": (
                    selected["value"]
                ),
                "short_term_investments_usd_billions": (
                    selected["value"] / 1_000_000_000
                ),
                "selected_tag": selected["tag"],
                "date_rule_passed": (
                    selected["filing_date"]
                    <= as_of_timestamp
                ),
            }
        )

    result_df = pd.DataFrame(results)

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 120)
    print(
        "Microsoft short-term investments "
        "point-in-time test"
    )
    print("=" * 120)

    print(
        result_df[
            [
                "as_of_date",
                "balance_sheet_date",
                "filing_date",
                "short_term_investments_usd_billions",
                "selected_tag",
                "date_rule_passed",
            ]
        ].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
        )
    )

    print()
    print(f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}")

    if not result_df["date_rule_passed"].all():
        raise RuntimeError(
            "לפחות שורה אחת חסרה או הפרה "
            "את כלל התאריכים."
        )

    print()
    print(
        "הבדיקה עברה: השקעות קצרות טווח נמצאו "
        "רק מדוחות שהיו זמינים בתאריך הבדיקה."
    )


if __name__ == "__main__":
    main()