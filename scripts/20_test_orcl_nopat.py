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
    / "orcl_nopat_test.csv"
)

AS_OF_DATES = [
    "2021-04-01",
    "2022-04-01",
    "2023-04-01",
    "2024-04-01",
    "2025-04-01",
]

OPERATING_INCOME_TAGS = [
    "OperatingIncomeLoss",
]

PRETAX_INCOME_TAGS = [
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "IncomeBeforeTaxExpenseBenefit",
]

TAX_EXPENSE_TAGS = [
    "IncomeTaxExpenseBenefit",
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


def build_annual_table(
    us_gaap: dict,
    candidate_tags: list[str],
    value_column: str,
    field_name: str,
) -> pd.DataFrame:
    rows = []
    tags_found = []

    for tag in candidate_tags:
        if tag not in us_gaap:
            continue

        tags_found.append(tag)

        units = (
            us_gaap[tag]
            .get("units", {})
            .get("USD", [])
        )

        for item in units:
            rows.append(
                {
                    "tag": tag,
                    "period_start": item.get("start"),
                    "period_end": item.get("end"),
                    "filing_date": item.get("filed"),
                    value_column: item.get("val"),
                    "form": item.get("form"),
                    "accession_number": item.get("accn"),
                }
            )

    if not tags_found:
        raise RuntimeError(
            f"לא נמצא תג מתאים עבור {field_name}."
        )

    print(f"{field_name} — תגים שנמצאו:")

    for tag in tags_found:
        print(f"  {tag}")

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError(
            f"לא נמצאו נתונים עבור {field_name}."
        )

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
            f"לא נמצאו נתונים שנתיים תקינים עבור {field_name}."
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

    latest_period_end = (
        available["period_end"].max()
    )

    selected = available[
        available["period_end"]
        == latest_period_end
    ].copy()

    selected = selected.sort_values(
        by=[
            "filing_date",
            "accession_number",
            "tag",
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

    operating_income_df = build_annual_table(
        us_gaap,
        OPERATING_INCOME_TAGS,
        "operating_income_usd",
        "Operating Income",
    )

    pretax_income_df = build_annual_table(
        us_gaap,
        PRETAX_INCOME_TAGS,
        "pretax_income_usd",
        "Pretax Income",
    )

    tax_expense_df = build_annual_table(
        us_gaap,
        TAX_EXPENSE_TAGS,
        "tax_expense_usd",
        "Income Tax Expense",
    )

    results = []

    for as_of_date in AS_OF_DATES:
        operating_row = select_latest_available(
            operating_income_df,
            as_of_date,
        )

        pretax_row = select_latest_available(
            pretax_income_df,
            as_of_date,
        )

        tax_row = select_latest_available(
            tax_expense_df,
            as_of_date,
        )

        period_ends = {
            operating_row["period_end"],
            pretax_row["period_end"],
            tax_row["period_end"],
        }

        if len(period_ends) != 1:
            raise RuntimeError(
                f"הנתונים אינם מאותה תקופה עבור {as_of_date}:\n"
                f"Operating Income: "
                f"{operating_row['period_end'].date()}\n"
                f"Pretax Income: "
                f"{pretax_row['period_end'].date()}\n"
                f"Tax Expense: "
                f"{tax_row['period_end'].date()}"
            )

        operating_income = float(
            operating_row["operating_income_usd"]
        )

        pretax_income = float(
            pretax_row["pretax_income_usd"]
        )

        tax_expense = float(
            tax_row["tax_expense_usd"]
        )

        if pretax_income <= 0:
            raise RuntimeError(
                f"Pretax Income אינו חיובי עבור {as_of_date}."
            )

        effective_tax_rate = (
            tax_expense / pretax_income
        )

        if not 0 <= effective_tax_rate <= 1:
            raise RuntimeError(
                f"שיעור המס אינו בטווח 0%–100% "
                f"עבור {as_of_date}: "
                f"{effective_tax_rate:.2%}"
            )

        nopat = (
            operating_income
            * (1 - effective_tax_rate)
        )

        as_of_timestamp = pd.Timestamp(
            as_of_date
        )

        filing_dates = [
            operating_row["filing_date"],
            pretax_row["filing_date"],
            tax_row["filing_date"],
        ]

        results.append(
            {
                "ticker": "ORCL",
                "company_name": (
                    company_facts.get("entityName")
                ),
                "as_of_date": as_of_date,
                "period_end": (
                    operating_row["period_end"].date()
                ),
                "latest_filing_date": (
                    max(filing_dates).date()
                ),
                "operating_income_usd": operating_income,
                "pretax_income_usd": pretax_income,
                "tax_expense_usd": tax_expense,
                "effective_tax_rate": effective_tax_rate,
                "effective_tax_rate_percent": (
                    effective_tax_rate * 100
                ),
                "nopat_usd": nopat,
                "pretax_income_tag": (
                    pretax_row["tag"]
                ),
                "date_rule_passed": all(
                    filing_date <= as_of_timestamp
                    for filing_date in filing_dates
                ),
            }
        )

    result_df = pd.DataFrame(results)

    for column in [
        "operating_income_usd",
        "pretax_income_usd",
        "tax_expense_usd",
        "nopat_usd",
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
        "operating_income_usd_billions",
        "pretax_income_usd_billions",
        "tax_expense_usd_billions",
        "effective_tax_rate_percent",
        "nopat_usd_billions",
        "date_rule_passed",
    ]

    print()
    print("=" * 145)
    print("Oracle NOPAT point-in-time test")
    print("=" * 145)

    print(
        result_df[
            display_columns
        ].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
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
        "הבדיקה עברה: NOPAT חושב ל-Oracle "
        "לחמש נקודות הבדיקה ללא Look-ahead bias."
    )


if __name__ == "__main__":
    main()