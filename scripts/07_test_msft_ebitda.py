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

ADJUSTED_NET_DEBT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_adjusted_net_debt_test.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_ebitda_test.csv"
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

DEPRECIATION_AMORTIZATION_TAGS = [
    "DepreciationDepletionAndAmortization",
    "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
    "DepreciationAndAmortization",
]


def load_company_facts():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ Company Facts לא נמצא:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def find_first_existing_tag(
    us_gaap_facts,
    candidate_tags,
    field_name,
):
    for tag in candidate_tags:
        if tag in us_gaap_facts:
            print(f"{field_name}: {tag}")
            return tag

    raise RuntimeError(
        f"לא נמצא תג מתאים עבור {field_name}.\n"
        f"נבדקו התגים: {candidate_tags}"
    )


def build_annual_fact_table(
    us_gaap_facts,
    tag,
):
    units = us_gaap_facts[tag].get("units", {})

    if "USD" not in units:
        raise RuntimeError(
            f"לא נמצאו נתוני USD עבור {tag}."
        )

    rows = []

    for item in units["USD"]:
        rows.append(
            {
                "start": item.get("start"),
                "end": item.get("end"),
                "value": item.get("val"),
                "filing_date": item.get("filed"),
                "form": item.get("form"),
                "fiscal_period": item.get("fp"),
                "accession_number": item.get("accn"),
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
        & df["start"].notna()
        & df["end"].notna()
        & df["filing_date"].notna()
        & df["value"].notna()
        & df["period_days"].between(300, 400)
    ].copy()

    if df.empty:
        raise RuntimeError(
            f"לא נמצאו שורות שנתיות תקינות עבור {tag}."
        )

    return df


def select_latest_available(
    fact_df,
    as_of_date,
):
    as_of_timestamp = pd.Timestamp(as_of_date)

    available = fact_df[
        fact_df["filing_date"] <= as_of_timestamp
    ].copy()

    if available.empty:
        return None

    latest_period_end = available["end"].max()

    selected = available[
        available["end"] == latest_period_end
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

    operating_income_tag = find_first_existing_tag(
        us_gaap_facts,
        OPERATING_INCOME_TAGS,
        "Operating Income",
    )

    depreciation_amortization_tag = find_first_existing_tag(
        us_gaap_facts,
        DEPRECIATION_AMORTIZATION_TAGS,
        "Depreciation and Amortization",
    )

    operating_income_df = build_annual_fact_table(
        us_gaap_facts,
        operating_income_tag,
    )

    depreciation_amortization_df = build_annual_fact_table(
        us_gaap_facts,
        depreciation_amortization_tag,
    )

    if not ADJUSTED_NET_DEBT_FILE.exists():
        raise FileNotFoundError(
            "קובץ Adjusted Net Debt לא נמצא:\n"
            f"{ADJUSTED_NET_DEBT_FILE}"
        )

    adjusted_net_debt_df = pd.read_csv(
        ADJUSTED_NET_DEBT_FILE
    )

    results = []

    for as_of_date in AS_OF_DATES:
        operating_income_row = select_latest_available(
            operating_income_df,
            as_of_date,
        )

        depreciation_amortization_row = select_latest_available(
            depreciation_amortization_df,
            as_of_date,
        )

        if (
            operating_income_row is None
            or depreciation_amortization_row is None
        ):
            raise RuntimeError(
                f"חסר נתון EBITDA עבור {as_of_date}."
            )

        if (
            operating_income_row["end"]
            != depreciation_amortization_row["end"]
        ):
            raise RuntimeError(
                f"תקופות שונות עבור {as_of_date}:\n"
                f"Operating Income: "
                f"{operating_income_row['end'].date()}\n"
                f"D&A: "
                f"{depreciation_amortization_row['end'].date()}"
            )

        operating_income = float(
            operating_income_row["value"]
        )

        depreciation_amortization = float(
            depreciation_amortization_row["value"]
        )

        ebitda = (
            operating_income
            + depreciation_amortization
        )

        latest_filing_date = max(
            operating_income_row["filing_date"],
            depreciation_amortization_row["filing_date"],
        )

        as_of_timestamp = pd.Timestamp(as_of_date)

        date_rule_passed = (
            operating_income_row["filing_date"]
            <= as_of_timestamp
            and depreciation_amortization_row["filing_date"]
            <= as_of_timestamp
        )

        results.append(
            {
                "as_of_date": as_of_date,
                "period_end": (
                    operating_income_row["end"].date()
                ),
                "latest_filing_date": (
                    latest_filing_date.date()
                ),
                "operating_income_usd": operating_income,
                "depreciation_amortization_usd": (
                    depreciation_amortization
                ),
                "ebitda_usd": ebitda,
                "date_rule_passed": date_rule_passed,
                "operating_income_tag": (
                    operating_income_tag
                ),
                "depreciation_amortization_tag": (
                    depreciation_amortization_tag
                ),
            }
        )

    result_df = pd.DataFrame(results)

    adjusted_columns = [
        "as_of_date",
        "adjusted_net_debt_usd",
    ]

    result_df = result_df.merge(
        adjusted_net_debt_df[adjusted_columns],
        on="as_of_date",
        how="left",
        validate="one_to_one",
    )

    if result_df["adjusted_net_debt_usd"].isna().any():
        raise RuntimeError(
            "חסר Adjusted Net Debt לפחות לתאריך אחד."
        )

    result_df["adjusted_net_debt_to_ebitda"] = (
        result_df["adjusted_net_debt_usd"]
        / result_df["ebitda_usd"]
    )

    money_columns = [
        "operating_income_usd",
        "depreciation_amortization_usd",
        "ebitda_usd",
        "adjusted_net_debt_usd",
    ]

    for column in money_columns:
        result_df[
            column.replace("_usd", "_usd_billions")
        ] = result_df[column] / 1_000_000_000

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    display_columns = [
        "as_of_date",
        "period_end",
        "latest_filing_date",
        "operating_income_usd_billions",
        "depreciation_amortization_usd_billions",
        "ebitda_usd_billions",
        "adjusted_net_debt_usd_billions",
        "adjusted_net_debt_to_ebitda",
        "date_rule_passed",
    ]

    print()
    print("=" * 150)
    print(
        "Microsoft EBITDA and "
        "Adjusted Net Debt / EBITDA test"
    )
    print("=" * 150)

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
        "הבדיקה עברה: EBITDA ו-Adjusted Net Debt / EBITDA "
        "חושבו רק מנתונים שהיו זמינים בתאריך הבדיקה."
    )


if __name__ == "__main__":
    main()