from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

INPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "sec_raw"
    / "ORCL_companyfacts.json"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_core_metrics_test.csv"
)


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

OPERATING_CASH_FLOW_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByOperatingActivities",
]

CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsForAdditionsToPropertyPlantAndEquipment",
]


def load_company_facts() -> dict:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ Oracle לא נמצא:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def find_first_existing_tag(
    us_gaap: dict,
    candidate_tags: list[str],
    field_name: str,
) -> str:
    for tag in candidate_tags:
        if tag in us_gaap:
            print(f"{field_name}: {tag}")
            return tag

    raise RuntimeError(
        f"לא נמצא תג מתאים עבור {field_name}.\n"
        f"נבדקו:\n{candidate_tags}"
    )


def build_annual_table(
    us_gaap: dict,
    tag: str,
    value_column: str,
) -> pd.DataFrame:
    units = (
        us_gaap[tag]
        .get("units", {})
        .get("USD", [])
    )

    if not units:
        raise RuntimeError(
            f"לא נמצאו נתוני USD עבור {tag}."
        )

    rows = []

    for item in units:
        rows.append(
            {
                "period_start": item.get("start"),
                "period_end": item.get("end"),
                "filing_date": item.get("filed"),
                value_column: item.get("val"),
                "form": item.get("form"),
                "accession_number": item.get("accn"),
                "fiscal_year": item.get("fy"),
                "fiscal_period": item.get("fp"),
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

    df[value_column] = pd.to_numeric(
        df[value_column],
        errors="coerce",
    )

    df["period_days"] = (
        df["period_end"]
        - df["period_start"]
    ).dt.days

    df = df[
        (df["form"] == "10-K")
        & df["period_start"].notna()
        & df["period_end"].notna()
        & df["filing_date"].notna()
        & df[value_column].notna()
        & df["period_days"].between(300, 400)
    ].copy()

    if df.empty:
        raise RuntimeError(
            f"לא נמצאו נתונים שנתיים תקינים עבור {tag}."
        )

    return df


def select_latest_available(
    df: pd.DataFrame,
    as_of_date: str,
) -> pd.Series:
    as_of_timestamp = pd.Timestamp(as_of_date)

    available = df[
        df["filing_date"] <= as_of_timestamp
    ].copy()

    if available.empty:
        raise RuntimeError(
            f"לא נמצאו נתונים זמינים עד {as_of_date}."
        )

    latest_period_end = available["period_end"].max()

    selected = available[
        available["period_end"] == latest_period_end
    ].copy()

    selected = selected.sort_values(
        by=[
            "filing_date",
            "accession_number",
        ]
    )

    return selected.iloc[-1]


def select_previous_period(
    df: pd.DataFrame,
    current_period_end: pd.Timestamp,
    as_of_date: str,
) -> pd.Series:
    as_of_timestamp = pd.Timestamp(as_of_date)

    available = df[
        (df["filing_date"] <= as_of_timestamp)
        & (df["period_end"] < current_period_end)
    ].copy()

    if available.empty:
        raise RuntimeError(
            f"לא נמצאה שנת הכנסות קודמת עבור {as_of_date}."
        )

    previous_period_end = available["period_end"].max()

    selected = available[
        available["period_end"] == previous_period_end
    ].copy()

    selected = selected.sort_values(
        by=[
            "filing_date",
            "accession_number",
        ]
    )

    return selected.iloc[-1]


def main() -> None:
    company_facts = load_company_facts()

    us_gaap = (
        company_facts
        .get("facts", {})
        .get("us-gaap", {})
    )

    revenue_tag = find_first_existing_tag(
        us_gaap,
        REVENUE_TAGS,
        "Revenue",
    )

    operating_cash_flow_tag = find_first_existing_tag(
        us_gaap,
        OPERATING_CASH_FLOW_TAGS,
        "Operating Cash Flow",
    )

    capex_tag = find_first_existing_tag(
        us_gaap,
        CAPEX_TAGS,
        "Capex",
    )

    revenue_df = build_annual_table(
        us_gaap,
        revenue_tag,
        "revenue_usd",
    )

    operating_cash_flow_df = build_annual_table(
        us_gaap,
        operating_cash_flow_tag,
        "operating_cash_flow_usd",
    )

    capex_df = build_annual_table(
        us_gaap,
        capex_tag,
        "capex_usd",
    )

    results = []

    for as_of_date in AS_OF_DATES:
        revenue_row = select_latest_available(
            revenue_df,
            as_of_date,
        )

        previous_revenue_row = select_previous_period(
            revenue_df,
            revenue_row["period_end"],
            as_of_date,
        )

        operating_cash_flow_row = select_latest_available(
            operating_cash_flow_df,
            as_of_date,
        )

        capex_row = select_latest_available(
            capex_df,
            as_of_date,
        )

        period_ends = {
            revenue_row["period_end"],
            operating_cash_flow_row["period_end"],
            capex_row["period_end"],
        }

        if len(period_ends) != 1:
            raise RuntimeError(
                f"הנתונים אינם מאותה תקופה עבור {as_of_date}:\n"
                f"Revenue: {revenue_row['period_end'].date()}\n"
                f"Operating Cash Flow: "
                f"{operating_cash_flow_row['period_end'].date()}\n"
                f"Capex: {capex_row['period_end'].date()}"
            )

        revenue = float(
            revenue_row["revenue_usd"]
        )

        previous_revenue = float(
            previous_revenue_row["revenue_usd"]
        )

        operating_cash_flow = float(
            operating_cash_flow_row[
                "operating_cash_flow_usd"
            ]
        )

        capex = float(
            capex_row["capex_usd"]
        )

        if revenue <= 0 or previous_revenue <= 0:
            raise RuntimeError(
                f"הכנסות לא תקינות עבור {as_of_date}."
            )

        if capex < 0:
            raise RuntimeError(
                f"Capex שלילי עבור {as_of_date}. "
                "לא נהפוך סימן ללא בדיקת מקור."
            )

        revenue_growth = (
            revenue / previous_revenue
        ) - 1

        fcf = (
            operating_cash_flow
            - capex
        )

        fcf_margin = (
            fcf / revenue
        )

        as_of_timestamp = pd.Timestamp(
            as_of_date
        )

        filing_dates = [
            revenue_row["filing_date"],
            previous_revenue_row["filing_date"],
            operating_cash_flow_row["filing_date"],
            capex_row["filing_date"],
        ]

        results.append(
            {
                "ticker": "ORCL",
                "company_name": (
                    company_facts.get("entityName")
                ),
                "as_of_date": as_of_date,
                "period_end": (
                    revenue_row["period_end"].date()
                ),
                "latest_filing_date": (
                    max(filing_dates).date()
                ),
                "revenue_usd": revenue,
                "previous_revenue_usd": previous_revenue,
                "revenue_growth": revenue_growth,
                "revenue_growth_percent": (
                    revenue_growth * 100
                ),
                "operating_cash_flow_usd": (
                    operating_cash_flow
                ),
                "capex_usd": capex,
                "fcf_usd": fcf,
                "fcf_margin": fcf_margin,
                "fcf_margin_percent": (
                    fcf_margin * 100
                ),
                "date_rule_passed": all(
                    filing_date <= as_of_timestamp
                    for filing_date in filing_dates
                ),
                "revenue_tag": revenue_tag,
                "operating_cash_flow_tag": (
                    operating_cash_flow_tag
                ),
                "capex_tag": capex_tag,
            }
        )

    result_df = pd.DataFrame(results)

    for column in [
        "revenue_usd",
        "previous_revenue_usd",
        "operating_cash_flow_usd",
        "capex_usd",
        "fcf_usd",
    ]:
        result_df[
            column.replace(
                "_usd",
                "_usd_billions",
            )
        ] = (
            result_df[column]
            / 1_000_000_000
        )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    display_columns = [
        "as_of_date",
        "period_end",
        "revenue_usd_billions",
        "revenue_growth_percent",
        "fcf_usd_billions",
        "fcf_margin_percent",
        "date_rule_passed",
    ]

    print()
    print("=" * 135)
    print("Oracle core metrics point-in-time test")
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
        f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}"
    )

    if not result_df[
        "date_rule_passed"
    ].all():
        raise RuntimeError(
            "לפחות שורה אחת הפרה את כלל התאריכים."
        )

    print()
    print(
        "הבדיקה עברה: Revenue, Revenue Growth "
        "ו-FCF Margin חושבו ל-Oracle "
        "ללא Look-ahead bias."
    )


if __name__ == "__main__":
    main()