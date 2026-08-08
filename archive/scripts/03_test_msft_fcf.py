from pathlib import Path
import json

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
    / "msft_fcf_test.csv"
)


# ============================================================
# תאריכי הבדיקה
# ============================================================

AS_OF_DATES = [
    "2021-04-01",
    "2022-04-01",
    "2023-04-01",
    "2024-04-01",
    "2025-04-01",
]


# ============================================================
# תגי SEC אפשריים
# ============================================================

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]

OPERATING_CASH_FLOW_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByOperatingActivities",
]

CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsForAdditionsToPropertyPlantAndEquipment",
]


def load_company_facts():
    """טוען את קובץ Company Facts המקומי."""

    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המקור לא נמצא:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def find_first_existing_tag(us_gaap_facts, candidate_tags, field_name):
    """מוצא את התג הראשון שקיים בקובץ החברה."""

    for tag in candidate_tags:
        if tag in us_gaap_facts:
            print(f"{field_name}: {tag}")
            return tag

    raise RuntimeError(
        f"לא נמצא תג מתאים עבור {field_name}.\n"
        f"נבדקו התגים: {candidate_tags}"
    )


def build_annual_fact_table(us_gaap_facts, tag):
    """
    בונה טבלת עובדות שנתיות עבור תג SEC מסוים.

    בשלב זה אנחנו משתמשים רק ב:
    - Form 10-K
    - Fiscal period FY
    - תקופה באורך שנתי סביר
    """

    fact = us_gaap_facts[tag]
    units = fact.get("units", {})

    if "USD" not in units:
        raise RuntimeError(
            f"לא נמצאו נתוני USD עבור התג {tag}."
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

    df["value"] = pd.to_numeric(
        df["value"],
        errors="coerce",
    )

    df["period_days"] = (
        df["end"] - df["start"]
    ).dt.days

    df = df[
        (df["form"] == "10-K")
        & (df["fiscal_period"] == "FY")
        & df["value"].notna()
        & df["start"].notna()
        & df["end"].notna()
        & df["filing_date"].notna()
        & df["period_days"].between(300, 400)
    ].copy()

    if df.empty:
        raise RuntimeError(
            f"לא נמצאו שורות שנתיות תקינות עבור התג {tag}."
        )

    return df


def select_latest_available_fact(fact_df, as_of_date):
    """
    בוחר את התקופה החשבונאית המאוחרת ביותר
    שדווחה עד תאריך הבדיקה.
    """

    as_of_timestamp = pd.Timestamp(as_of_date)

    available = fact_df[
        fact_df["filing_date"] <= as_of_timestamp
    ].copy()

    if available.empty:
        return None

    latest_period_end = available["end"].max()

    latest_period_rows = available[
        available["end"] == latest_period_end
    ].copy()

    latest_period_rows = latest_period_rows.sort_values(
        by=[
            "filing_date",
            "accession_number",
        ]
    )

    return latest_period_rows.iloc[-1]


def require_same_period(
    as_of_date,
    revenue_row,
    operating_cash_flow_row,
    capex_row,
):
    """מוודא שכל שלושת הנתונים מתייחסים לאותה שנה."""

    period_ends = {
        revenue_row["end"],
        operating_cash_flow_row["end"],
        capex_row["end"],
    }

    if len(period_ends) != 1:
        raise RuntimeError(
            f"ב-{as_of_date} הנתונים אינם מאותה תקופה:\n"
            f"Revenue period end: "
            f"{revenue_row['end'].date()}\n"
            f"Operating cash flow period end: "
            f"{operating_cash_flow_row['end'].date()}\n"
            f"Capex period end: "
            f"{capex_row['end'].date()}"
        )


def main():
    company_facts = load_company_facts()
    us_gaap_facts = company_facts["facts"]["us-gaap"]

    revenue_tag = find_first_existing_tag(
        us_gaap_facts,
        REVENUE_TAGS,
        "Revenue",
    )

    operating_cash_flow_tag = find_first_existing_tag(
        us_gaap_facts,
        OPERATING_CASH_FLOW_TAGS,
        "Operating Cash Flow",
    )

    capex_tag = find_first_existing_tag(
        us_gaap_facts,
        CAPEX_TAGS,
        "Capex",
    )

    revenue_df = build_annual_fact_table(
        us_gaap_facts,
        revenue_tag,
    )

    operating_cash_flow_df = build_annual_fact_table(
        us_gaap_facts,
        operating_cash_flow_tag,
    )

    capex_df = build_annual_fact_table(
        us_gaap_facts,
        capex_tag,
    )

    results = []

    for as_of_date in AS_OF_DATES:
        revenue_row = select_latest_available_fact(
            revenue_df,
            as_of_date,
        )

        operating_cash_flow_row = select_latest_available_fact(
            operating_cash_flow_df,
            as_of_date,
        )

        capex_row = select_latest_available_fact(
            capex_df,
            as_of_date,
        )

        if (
            revenue_row is None
            or operating_cash_flow_row is None
            or capex_row is None
        ):
            raise RuntimeError(
                f"חסר נתון עבור {as_of_date}."
            )

        require_same_period(
            as_of_date,
            revenue_row,
            operating_cash_flow_row,
            capex_row,
        )

        revenue = float(revenue_row["value"])
        operating_cash_flow = float(
            operating_cash_flow_row["value"]
        )
        capex = float(capex_row["value"])

        if revenue <= 0:
            raise RuntimeError(
                f"הכנסות לא תקינות עבור {as_of_date}: "
                f"{revenue}"
            )

        if capex < 0:
            raise RuntimeError(
                f"Capex שלילי עבור {as_of_date}: {capex}\n"
                "לא נהפוך סימן אוטומטית לפני שנבדוק את המקור."
            )

        free_cash_flow = operating_cash_flow - capex
        free_cash_flow_margin = free_cash_flow / revenue

        as_of_timestamp = pd.Timestamp(as_of_date)

        filing_dates = [
            revenue_row["filing_date"],
            operating_cash_flow_row["filing_date"],
            capex_row["filing_date"],
        ]

        latest_filing_date = max(filing_dates)

        date_rule_passed = all(
            filing_date <= as_of_timestamp
            for filing_date in filing_dates
        )

        results.append(
            {
                "as_of_date": as_of_date,
                "period_start": revenue_row["start"].date(),
                "period_end": revenue_row["end"].date(),
                "latest_filing_date": latest_filing_date.date(),
                "revenue_usd": revenue,
                "operating_cash_flow_usd": operating_cash_flow,
                "capex_usd": capex,
                "fcf_usd": free_cash_flow,
                "fcf_margin": free_cash_flow_margin,
                "date_rule_passed": date_rule_passed,
                "revenue_tag": revenue_tag,
                "operating_cash_flow_tag": operating_cash_flow_tag,
                "capex_tag": capex_tag,
            }
        )

    result_df = pd.DataFrame(results)

    result_df["revenue_usd_billions"] = (
        result_df["revenue_usd"] / 1_000_000_000
    )

    result_df["operating_cash_flow_usd_billions"] = (
        result_df["operating_cash_flow_usd"]
        / 1_000_000_000
    )

    result_df["capex_usd_billions"] = (
        result_df["capex_usd"] / 1_000_000_000
    )

    result_df["fcf_usd_billions"] = (
        result_df["fcf_usd"] / 1_000_000_000
    )

    result_df["fcf_margin_percent"] = (
        result_df["fcf_margin"] * 100
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

    display_columns = [
        "as_of_date",
        "period_end",
        "latest_filing_date",
        "revenue_usd_billions",
        "operating_cash_flow_usd_billions",
        "capex_usd_billions",
        "fcf_usd_billions",
        "fcf_margin_percent",
        "date_rule_passed",
    ]

    print()
    print("=" * 140)
    print("Microsoft FCF point-in-time test")
    print("=" * 140)

    print(
        result_df[display_columns].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
        )
    )

    print()
    print(f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}")

    if not result_df["date_rule_passed"].all():
        raise RuntimeError(
            "לפחות שורה אחת הפרה את כלל התאריכים."
        )

    print()
    print(
        "הבדיקה עברה: Revenue, Operating Cash Flow ו-Capex "
        "נלקחו רק מדוחות שהיו זמינים בתאריך הבדיקה."
    )


if __name__ == "__main__":
    main()