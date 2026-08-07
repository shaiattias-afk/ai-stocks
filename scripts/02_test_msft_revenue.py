from pathlib import Path
import json

import pandas as pd


# ------------------------------------------------------------
# מיקומי קבצים
# ------------------------------------------------------------

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
    / "msft_revenue_test.csv"
)


# ------------------------------------------------------------
# תאריכי הבדיקה
# ------------------------------------------------------------

AS_OF_DATES = [
    "2021-04-01",
    "2022-04-01",
    "2023-04-01",
    "2024-04-01",
    "2025-04-01",
]


# תגי הכנסה אפשריים ב-SEC
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
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


def find_revenue_tag(us_gaap_facts):
    for tag in REVENUE_TAGS:
        if tag in us_gaap_facts:
            return tag

    raise RuntimeError(
        "לא נמצא תג הכנסות מתאים בקובץ Microsoft."
    )


def build_revenue_table(company_facts):
    us_gaap_facts = company_facts["facts"]["us-gaap"]

    revenue_tag = find_revenue_tag(us_gaap_facts)

    print(f"תג ההכנסות שנבחר: {revenue_tag}")

    revenue_fact = us_gaap_facts[revenue_tag]
    units = revenue_fact.get("units", {})

    if "USD" not in units:
        raise RuntimeError(
            "לא נמצאו נתוני הכנסות ביחידות USD."
        )

    rows = []

    for item in units["USD"]:
        rows.append(
            {
                "start": item.get("start"),
                "end": item.get("end"),
                "value": item.get("val"),
                "accession_number": item.get("accn"),
                "fiscal_year": item.get("fy"),
                "fiscal_period": item.get("fp"),
                "form": item.get("form"),
                "filing_date": item.get("filed"),
                "frame": item.get("frame"),
            }
        )

    df = pd.DataFrame(rows)

    df["start"] = pd.to_datetime(
        df["start"],
        errors="coerce",
    )

    df["end"] = pd.to_datetime(
        df["end"],
        errors="coerce",
    )

    df["filing_date"] = pd.to_datetime(
        df["filing_date"],
        errors="coerce",
    )

    # בשלב ההוכחה משתמשים רק בדוחות שנתיים
    df = df[
        (df["form"] == "10-K")
        & (df["fiscal_period"] == "FY")
        & df["value"].notna()
        & df["end"].notna()
        & df["filing_date"].notna()
    ].copy()

    return df, revenue_tag


def select_latest_available_revenue(
    revenue_df,
    as_of_date,
):
    as_of_timestamp = pd.Timestamp(as_of_date)

    available = revenue_df[
        revenue_df["filing_date"] <= as_of_timestamp
    ].copy()

    if available.empty:
        return None

    # קודם בוחרים את התקופה החשבונאית המאוחרת ביותר
    latest_period_end = available["end"].max()

    latest_period_rows = available[
        available["end"] == latest_period_end
    ].copy()

    # אם אותה תקופה הופיעה בכמה דיווחים,
    # בוחרים את הדיווח האחרון שהיה זמין בזמן
    latest_period_rows = latest_period_rows.sort_values(
        by=[
            "filing_date",
            "accession_number",
        ]
    )

    return latest_period_rows.iloc[-1]


def main():
    company_facts = load_company_facts()

    revenue_df, revenue_tag = build_revenue_table(
        company_facts
    )

    results = []

    for as_of_date in AS_OF_DATES:
        selected = select_latest_available_revenue(
            revenue_df,
            as_of_date,
        )

        if selected is None:
            results.append(
                {
                    "as_of_date": as_of_date,
                    "revenue_tag": revenue_tag,
                    "period_start": None,
                    "period_end": None,
                    "filing_date": None,
                    "revenue_usd": None,
                    "accession_number": None,
                    "date_rule_passed": False,
                }
            )
            continue

        filing_date = selected["filing_date"]
        as_of_timestamp = pd.Timestamp(as_of_date)

        results.append(
            {
                "as_of_date": as_of_date,
                "revenue_tag": revenue_tag,
                "period_start": selected["start"].date(),
                "period_end": selected["end"].date(),
                "filing_date": filing_date.date(),
                "revenue_usd": selected["value"],
                "accession_number": selected[
                    "accession_number"
                ],
                "date_rule_passed": (
                    filing_date <= as_of_timestamp
                ),
            }
        )

    result_df = pd.DataFrame(results)

    result_df["revenue_usd_billions"] = (
        result_df["revenue_usd"] / 1_000_000_000
    )

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 100)
    print("Microsoft revenue point-in-time test")
    print("=" * 100)

    print(
        result_df[
            [
                "as_of_date",
                "period_end",
                "filing_date",
                "revenue_usd_billions",
                "date_rule_passed",
            ]
        ].to_string(index=False)
    )

    print()
    print(f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}")

    if result_df["date_rule_passed"].all():
        print()
        print(
            "הבדיקה עברה: בכל השורות filing_date "
            "קטן או שווה ל-as_of_date."
        )
    else:
        raise RuntimeError(
            "לפחות שורה אחת הפרה את כלל התאריכים."
        )


if __name__ == "__main__":
    main()
