from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


# ============================================================
# מיקומי קבצים
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

COMPANY_FACTS_FILE = (
    PROJECT_DIR
    / "data"
    / "sec_raw"
    / "MSFT_companyfacts.json"
)

DA_OTHER_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_da_other_test.csv"
)

ADJUSTED_NET_DEBT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_adjusted_net_debt_test.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_standardized_ebitda_test.csv"
)


# ============================================================
# הגדרות
# ============================================================

OPERATING_INCOME_TAG = "OperatingIncomeLoss"

EXPECTED_AS_OF_DATES = [
    "2021-04-01",
    "2022-04-01",
    "2023-04-01",
    "2024-04-01",
    "2025-04-01",
]


# ============================================================
# קריאת נתונים
# ============================================================

def load_company_facts() -> dict:
    if not COMPANY_FACTS_FILE.exists():
        raise FileNotFoundError(
            "קובץ Company Facts לא נמצא:\n"
            f"{COMPANY_FACTS_FILE}"
        )

    with COMPANY_FACTS_FILE.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def load_csv(
    path: Path,
    required_columns: set[str],
    file_description: str,
) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"קובץ {file_description} לא נמצא:\n{path}"
        )

    df = pd.read_csv(path)

    missing_columns = (
        required_columns
        - set(df.columns)
    )

    if missing_columns:
        raise RuntimeError(
            f"בקובץ {file_description} חסרות עמודות:\n"
            f"{sorted(missing_columns)}"
        )

    return df


# ============================================================
# Operating Income
# ============================================================

def build_operating_income_table(
    company_facts: dict,
) -> pd.DataFrame:
    us_gaap = (
        company_facts
        .get("facts", {})
        .get("us-gaap", {})
    )

    if OPERATING_INCOME_TAG not in us_gaap:
        raise RuntimeError(
            f"התג {OPERATING_INCOME_TAG} לא נמצא."
        )

    units = (
        us_gaap[OPERATING_INCOME_TAG]
        .get("units", {})
    )

    if "USD" not in units:
        raise RuntimeError(
            "לא נמצאו נתוני USD עבור Operating Income."
        )

    rows = []

    for item in units["USD"]:
        rows.append(
            {
                "period_start": item.get("start"),
                "period_end": item.get("end"),
                "filing_date": item.get("filed"),
                "operating_income_usd": item.get("val"),
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

    df["operating_income_usd"] = pd.to_numeric(
        df["operating_income_usd"],
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
        & df["operating_income_usd"].notna()
        & df["period_days"].between(300, 400)
    ].copy()

    if df.empty:
        raise RuntimeError(
            "לא נמצאו נתוני Operating Income שנתיים תקינים."
        )

    return df


def select_latest_available_operating_income(
    operating_income_df: pd.DataFrame,
    as_of_date: str,
) -> pd.Series:
    as_of_timestamp = pd.Timestamp(as_of_date)

    available = operating_income_df[
        operating_income_df["filing_date"]
        <= as_of_timestamp
    ].copy()

    if available.empty:
        raise RuntimeError(
            f"לא נמצא Operating Income זמין עד {as_of_date}."
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
        ]
    )

    return selected.iloc[-1]


# ============================================================
# Main
# ============================================================

def main() -> None:
    company_facts = load_company_facts()

    operating_income_table = (
        build_operating_income_table(
            company_facts
        )
    )

    da_other_df = load_csv(
        DA_OTHER_FILE,
        {
            "as_of_date",
            "report_date",
            "filing_date",
            "da_and_other_usd",
            "date_rule_passed",
        },
        "D&A and Other",
    )

    adjusted_net_debt_df = load_csv(
        ADJUSTED_NET_DEBT_FILE,
        {
            "as_of_date",
            "adjusted_net_debt_usd",
            "all_date_rules_passed",
        },
        "Adjusted Net Debt",
    )

    da_other_df["as_of_date"] = pd.to_datetime(
        da_other_df["as_of_date"],
        errors="coerce",
    )

    da_other_df["report_date"] = pd.to_datetime(
        da_other_df["report_date"],
        errors="coerce",
    )

    da_other_df["filing_date"] = pd.to_datetime(
        da_other_df["filing_date"],
        errors="coerce",
    )

    adjusted_net_debt_df["as_of_date"] = (
        pd.to_datetime(
            adjusted_net_debt_df["as_of_date"],
            errors="coerce",
        )
    )

    expected_dates = {
        pd.Timestamp(date)
        for date in EXPECTED_AS_OF_DATES
    }

    actual_dates = set(
        da_other_df["as_of_date"]
        .dropna()
    )

    missing_dates = (
        expected_dates
        - actual_dates
    )

    if missing_dates:
        raise RuntimeError(
            "חסרים תאריכים בקובץ D&A and Other:\n"
            f"{sorted(str(date.date()) for date in missing_dates)}"
        )

    results = []

    for as_of_date in EXPECTED_AS_OF_DATES:
        selected_operating_income = (
            select_latest_available_operating_income(
                operating_income_table,
                as_of_date,
            )
        )

        as_of_timestamp = pd.Timestamp(
            as_of_date
        )

        da_rows = da_other_df[
            da_other_df["as_of_date"]
            == as_of_timestamp
        ]

        if len(da_rows) != 1:
            raise RuntimeError(
                f"ציפינו לשורת D&A אחת עבור {as_of_date}, "
                f"אך נמצאו {len(da_rows)}."
            )

        da_row = da_rows.iloc[0]

        operating_period_end = (
            selected_operating_income[
                "period_end"
            ]
        )

        da_report_date = (
            da_row["report_date"]
        )

        if operating_period_end != da_report_date:
            raise RuntimeError(
                f"תקופות לא תואמות עבור {as_of_date}:\n"
                f"Operating Income period end: "
                f"{operating_period_end.date()}\n"
                f"D&A report date: "
                f"{da_report_date.date()}"
            )

        operating_income_usd = float(
            selected_operating_income[
                "operating_income_usd"
            ]
        )

        da_and_other_usd = float(
            da_row["da_and_other_usd"]
        )

        standardized_ebitda_usd = (
            operating_income_usd
            + da_and_other_usd
        )

        if standardized_ebitda_usd <= 0:
            raise RuntimeError(
                f"Standardized EBITDA אינו חיובי "
                f"עבור {as_of_date}."
            )

        operating_date_rule_passed = (
            selected_operating_income[
                "filing_date"
            ]
            <= as_of_timestamp
        )

        da_date_rule_passed = bool(
            da_row["date_rule_passed"]
        )

        results.append(
            {
                "as_of_date": as_of_timestamp,
                "period_start": (
                    selected_operating_income[
                        "period_start"
                    ]
                ),
                "period_end": operating_period_end,
                "operating_income_filing_date": (
                    selected_operating_income[
                        "filing_date"
                    ]
                ),
                "da_filing_date": (
                    da_row["filing_date"]
                ),
                "operating_income_usd": (
                    operating_income_usd
                ),
                "da_and_other_usd": (
                    da_and_other_usd
                ),
                "standardized_ebitda_usd": (
                    standardized_ebitda_usd
                ),
                "operating_income_date_rule_passed": (
                    operating_date_rule_passed
                ),
                "da_date_rule_passed": (
                    da_date_rule_passed
                ),
            }
        )

    result_df = pd.DataFrame(results)

    result_df = result_df.merge(
        adjusted_net_debt_df[
            [
                "as_of_date",
                "adjusted_net_debt_usd",
                "all_date_rules_passed",
            ]
        ],
        on="as_of_date",
        how="left",
        validate="one_to_one",
    )

    if result_df[
        "adjusted_net_debt_usd"
    ].isna().any():
        raise RuntimeError(
            "חסר Adjusted Net Debt לפחות בתאריך אחד."
        )

    result_df[
        "adjusted_net_debt_to_standardized_ebitda"
    ] = (
        result_df["adjusted_net_debt_usd"]
        / result_df["standardized_ebitda_usd"]
    )

    result_df["all_rules_passed"] = (
        result_df[
            "operating_income_date_rule_passed"
        ]
        & result_df[
            "da_date_rule_passed"
        ]
        & result_df[
            "all_date_rules_passed"
        ]
    )

    money_columns = [
        "operating_income_usd",
        "da_and_other_usd",
        "standardized_ebitda_usd",
        "adjusted_net_debt_usd",
    ]

    for column in money_columns:
        result_df[
            column.replace(
                "_usd",
                "_usd_billions",
            )
        ] = (
            result_df[column]
            / 1_000_000_000
        )

    result_df["as_of_date"] = (
        result_df["as_of_date"]
        .dt.date
    )

    result_df["period_start"] = (
        result_df["period_start"]
        .dt.date
    )

    result_df["period_end"] = (
        result_df["period_end"]
        .dt.date
    )

    result_df[
        "operating_income_filing_date"
    ] = (
        result_df[
            "operating_income_filing_date"
        ]
        .dt.date
    )

    result_df["da_filing_date"] = (
        result_df["da_filing_date"]
        .dt.date
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
        "da_and_other_usd_billions",
        "standardized_ebitda_usd_billions",
        "adjusted_net_debt_usd_billions",
        "adjusted_net_debt_to_standardized_ebitda",
        "all_rules_passed",
    ]

    print()
    print("=" * 165)
    print(
        "Microsoft Standardized EBITDA and "
        "Adjusted Net Debt / Standardized EBITDA"
    )
    print("=" * 165)

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
        "all_rules_passed"
    ].all():
        raise RuntimeError(
            "לפחות שורה אחת הפרה את כללי התאריכים."
        )

    print()
    print(
        "הבדיקה עברה: Standardized EBITDA ויחס "
        "Adjusted Net Debt / Standardized EBITDA "
        "חושבו ללא Look-ahead bias."
    )


if __name__ == "__main__":
    main()