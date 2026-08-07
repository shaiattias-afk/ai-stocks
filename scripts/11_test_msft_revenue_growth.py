from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


# ============================================================
# מיקומי קבצים
# ============================================================

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
    / "msft_revenue_growth_test.csv"
)


# ============================================================
# הגדרות
# ============================================================

AS_OF_DATES = [
    "2021-04-01",
    "2022-04-01",
    "2023-04-01",
    "2024-04-01",
    "2025-04-01",
]

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]


# ============================================================
# טעינת נתונים
# ============================================================

def load_company_facts() -> dict:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המקור לא נמצא:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def find_revenue_tag(
    us_gaap_facts: dict,
) -> str:
    for tag in REVENUE_TAGS:
        if tag in us_gaap_facts:
            print(f"תג ההכנסות שנבחר: {tag}")
            return tag

    raise RuntimeError(
        "לא נמצא תג הכנסות מתאים.\n"
        f"נבדקו: {REVENUE_TAGS}"
    )


# ============================================================
# בניית טבלת הכנסות שנתיות
# ============================================================

def build_annual_revenue_table(
    us_gaap_facts: dict,
    revenue_tag: str,
) -> pd.DataFrame:
    units = (
        us_gaap_facts[revenue_tag]
        .get("units", {})
    )

    if "USD" not in units:
        raise RuntimeError(
            f"לא נמצאו נתוני USD עבור {revenue_tag}."
        )

    rows = []

    for item in units["USD"]:
        rows.append(
            {
                "period_start": item.get("start"),
                "period_end": item.get("end"),
                "filing_date": item.get("filed"),
                "revenue_usd": item.get("val"),
                "form": item.get("form"),
                "fiscal_period": item.get("fp"),
                "fiscal_year": item.get("fy"),
                "accession_number": item.get("accn"),
            }
        )

    df = pd.DataFrame(rows)

    df["period_start"] = pd.to_datetime(
        df["period_start"],
        errors="coerce",
    )

    df["period_end"] = pd.to_datetime(
        df["period_end"],
        errors="coerce",
    )

    df["filing_date"] = pd.to_datetime(
        df["filing_date"],
        errors="coerce",
    )

    df["revenue_usd"] = pd.to_numeric(
        df["revenue_usd"],
        errors="coerce",
    )

    df["period_days"] = (
        df["period_end"]
        - df["period_start"]
    ).dt.days

    df = df[
        (df["form"] == "10-K")
        & (df["fiscal_period"] == "FY")
        & df["period_start"].notna()
        & df["period_end"].notna()
        & df["filing_date"].notna()
        & df["revenue_usd"].notna()
        & df["period_days"].between(300, 400)
    ].copy()

    if df.empty:
        raise RuntimeError(
            "לא נמצאו נתוני הכנסות שנתיים תקינים."
        )

    return df


# ============================================================
# בחירת שתי השנים האחרונות שהיו זמינות
# ============================================================

def select_latest_two_periods(
    revenue_df: pd.DataFrame,
    as_of_date: str,
) -> tuple[pd.Series, pd.Series]:
    as_of_timestamp = pd.Timestamp(as_of_date)

    available = revenue_df[
        revenue_df["filing_date"]
        <= as_of_timestamp
    ].copy()

    if available.empty:
        raise RuntimeError(
            f"לא נמצאו הכנסות זמינות עד {as_of_date}."
        )

    # אם אותה תקופה הופיעה בכמה דיווחים,
    # נבחר את הדיווח האחרון שהיה זמין בזמן.
    available = available.sort_values(
        by=[
            "period_end",
            "filing_date",
            "accession_number",
        ]
    )

    available = available.drop_duplicates(
        subset=["period_end"],
        keep="last",
    )

    available = available.sort_values(
        by="period_end"
    )

    if len(available) < 2:
        raise RuntimeError(
            f"אין שתי שנות הכנסות זמינות עד {as_of_date}."
        )

    current_row = available.iloc[-1]
    previous_row = available.iloc[-2]

    return current_row, previous_row


# ============================================================
# Main
# ============================================================

def main() -> None:
    company_facts = load_company_facts()

    us_gaap_facts = (
        company_facts
        .get("facts", {})
        .get("us-gaap", {})
    )

    revenue_tag = find_revenue_tag(
        us_gaap_facts
    )

    revenue_df = build_annual_revenue_table(
        us_gaap_facts,
        revenue_tag,
    )

    results = []

    for as_of_date in AS_OF_DATES:
        current_row, previous_row = (
            select_latest_two_periods(
                revenue_df,
                as_of_date,
            )
        )

        current_revenue = float(
            current_row["revenue_usd"]
        )

        previous_revenue = float(
            previous_row["revenue_usd"]
        )

        if previous_revenue <= 0:
            raise RuntimeError(
                f"הכנסות השנה הקודמת אינן חיוביות "
                f"ב-{as_of_date}."
            )

        revenue_growth = (
            current_revenue
            / previous_revenue
            - 1
        )

        as_of_timestamp = pd.Timestamp(
            as_of_date
        )

        date_rule_passed = (
            current_row["filing_date"]
            <= as_of_timestamp
            and previous_row["filing_date"]
            <= as_of_timestamp
        )

        results.append(
            {
                "as_of_date": as_of_date,
                "current_period_end": (
                    current_row["period_end"].date()
                ),
                "previous_period_end": (
                    previous_row["period_end"].date()
                ),
                "current_filing_date": (
                    current_row["filing_date"].date()
                ),
                "previous_filing_date": (
                    previous_row["filing_date"].date()
                ),
                "current_revenue_usd": (
                    current_revenue
                ),
                "previous_revenue_usd": (
                    previous_revenue
                ),
                "revenue_growth": revenue_growth,
                "revenue_growth_percent": (
                    revenue_growth * 100
                ),
                "revenue_tag": revenue_tag,
                "date_rule_passed": (
                    date_rule_passed
                ),
            }
        )

    result_df = pd.DataFrame(results)

    result_df[
        "current_revenue_usd_billions"
    ] = (
        result_df["current_revenue_usd"]
        / 1_000_000_000
    )

    result_df[
        "previous_revenue_usd_billions"
    ] = (
        result_df["previous_revenue_usd"]
        / 1_000_000_000
    )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    display_columns = [
        "as_of_date",
        "previous_period_end",
        "current_period_end",
        "previous_revenue_usd_billions",
        "current_revenue_usd_billions",
        "revenue_growth_percent",
        "date_rule_passed",
    ]

    print()
    print("=" * 135)
    print("Microsoft Revenue Growth point-in-time test")
    print("=" * 135)

    print(
        result_df[
            display_columns
        ].to_string(
            index=False,
            float_format=(
                lambda value: f"{value:,.3f}"
            ),
        )
    )

    print()
    print(
        "התוצאה נשמרה כאן:\n"
        f"{OUTPUT_FILE}"
    )

    if not result_df[
        "date_rule_passed"
    ].all():
        raise RuntimeError(
            "לפחות שורה אחת הפרה את כלל התאריכים."
        )

    print()
    print(
        "הבדיקה עברה: צמיחת ההכנסות חושבה "
        "רק מנתונים שהיו זמינים בתאריך הבדיקה."
    )


if __name__ == "__main__":
    main()