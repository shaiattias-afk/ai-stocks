from __future__ import annotations

from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"

FCF_FILE = DATA_DIR / "msft_fcf_test.csv"
REVENUE_GROWTH_FILE = DATA_DIR / "msft_revenue_growth_test.csv"
EBITDA_FILE = DATA_DIR / "msft_standardized_ebitda_test.csv"
ROIC_FILE = DATA_DIR / "msft_roic_test.csv"
NOPAT_FILE = DATA_DIR / "msft_nopat_test.csv"

OUTPUT_FILE = DATA_DIR / "msft_fundamentals_dataset.csv"


EXPECTED_DATES = [
    "2021-04-01",
    "2022-04-01",
    "2023-04-01",
    "2024-04-01",
    "2025-04-01",
]


def read_csv_checked(
    path: Path,
    required_columns: set[str],
    description: str,
) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"קובץ {description} לא נמצא:\n{path}"
        )

    df = pd.read_csv(path)

    missing_columns = required_columns - set(df.columns)

    if missing_columns:
        raise RuntimeError(
            f"בקובץ {description} חסרות עמודות:\n"
            f"{sorted(missing_columns)}"
        )

    df["as_of_date"] = pd.to_datetime(
        df["as_of_date"],
        errors="coerce",
    )

    if df["as_of_date"].isna().any():
        raise RuntimeError(
            f"בקובץ {description} נמצאו תאריכים לא תקינים."
        )

    if df["as_of_date"].duplicated().any():
        raise RuntimeError(
            f"בקובץ {description} נמצאו תאריכים כפולים."
        )

    return df


def normalize_boolean(series: pd.Series) -> pd.Series:
    """
    ממיר True/False גם כאשר הם נשמרו ב-CSV כמחרוזות.
    """

    if series.dtype == bool:
        return series

    normalized = (
        series.astype(str)
        .str.strip()
        .str.lower()
        .map(
            {
                "true": True,
                "false": False,
                "1": True,
                "0": False,
            }
        )
    )

    if normalized.isna().any():
        raise RuntimeError(
            "נמצא ערך שאינו True או False בעמודת בדיקה."
        )

    return normalized.astype(bool)


def main() -> None:
    fcf_df = read_csv_checked(
        FCF_FILE,
        {
            "as_of_date",
            "period_end",
            "revenue_usd",
            "fcf_usd",
            "fcf_margin",
            "fcf_margin_percent",
            "date_rule_passed",
        },
        "FCF",
    )

    revenue_growth_df = read_csv_checked(
        REVENUE_GROWTH_FILE,
        {
            "as_of_date",
            "revenue_growth",
            "revenue_growth_percent",
            "date_rule_passed",
        },
        "Revenue Growth",
    )

    ebitda_df = read_csv_checked(
        EBITDA_FILE,
        {
            "as_of_date",
            "standardized_ebitda_usd",
            "adjusted_net_debt_usd",
            "adjusted_net_debt_to_standardized_ebitda",
            "all_rules_passed",
        },
        "Standardized EBITDA",
    )

    roic_df = read_csv_checked(
        ROIC_FILE,
        {
            "as_of_date",
            "nopat_usd",
            "average_invested_capital_usd",
            "roic",
            "roic_percent",
            "date_rule_passed",
        },
        "ROIC",
    )

    nopat_df = read_csv_checked(
        NOPAT_FILE,
        {
            "as_of_date",
            "effective_tax_rate",
            "effective_tax_rate_percent",
            "date_rule_passed",
        },
        "NOPAT",
    )

    fcf_df["fcf_rule_passed"] = normalize_boolean(
        fcf_df["date_rule_passed"]
    )

    revenue_growth_df["growth_rule_passed"] = normalize_boolean(
        revenue_growth_df["date_rule_passed"]
    )

    ebitda_df["ebitda_rule_passed"] = normalize_boolean(
        ebitda_df["all_rules_passed"]
    )

    roic_df["roic_rule_passed"] = normalize_boolean(
        roic_df["date_rule_passed"]
    )

    nopat_df["nopat_rule_passed"] = normalize_boolean(
        nopat_df["date_rule_passed"]
    )

    result_df = fcf_df[
        [
            "as_of_date",
            "period_end",
            "revenue_usd",
            "fcf_usd",
            "fcf_margin",
            "fcf_margin_percent",
            "fcf_rule_passed",
        ]
    ].merge(
        revenue_growth_df[
            [
                "as_of_date",
                "revenue_growth",
                "revenue_growth_percent",
                "growth_rule_passed",
            ]
        ],
        on="as_of_date",
        how="inner",
        validate="one_to_one",
    )

    result_df = result_df.merge(
        ebitda_df[
            [
                "as_of_date",
                "standardized_ebitda_usd",
                "adjusted_net_debt_usd",
                "adjusted_net_debt_to_standardized_ebitda",
                "ebitda_rule_passed",
            ]
        ],
        on="as_of_date",
        how="inner",
        validate="one_to_one",
    )

    result_df = result_df.merge(
        roic_df[
            [
                "as_of_date",
                "nopat_usd",
                "average_invested_capital_usd",
                "roic",
                "roic_percent",
                "roic_rule_passed",
            ]
        ],
        on="as_of_date",
        how="inner",
        validate="one_to_one",
    )

    result_df = result_df.merge(
        nopat_df[
            [
                "as_of_date",
                "effective_tax_rate",
                "effective_tax_rate_percent",
                "nopat_rule_passed",
            ]
        ],
        on="as_of_date",
        how="inner",
        validate="one_to_one",
    )

    expected_dates = {
        pd.Timestamp(date)
        for date in EXPECTED_DATES
    }

    actual_dates = set(result_df["as_of_date"])

    if actual_dates != expected_dates:
        raise RuntimeError(
            "קובץ האיחוד אינו כולל בדיוק את חמשת "
            "תאריכי הבדיקה הצפויים."
        )

    result_df["all_date_rules_passed"] = (
        result_df["fcf_rule_passed"]
        & result_df["growth_rule_passed"]
        & result_df["ebitda_rule_passed"]
        & result_df["roic_rule_passed"]
        & result_df["nopat_rule_passed"]
    )

    result_df["ticker"] = "MSFT"
    result_df["company_name"] = "Microsoft Corporation"

    result_df = result_df[
        [
            "ticker",
            "company_name",
            "as_of_date",
            "period_end",
            "revenue_usd",
            "revenue_growth",
            "revenue_growth_percent",
            "fcf_usd",
            "fcf_margin",
            "fcf_margin_percent",
            "standardized_ebitda_usd",
            "adjusted_net_debt_usd",
            "adjusted_net_debt_to_standardized_ebitda",
            "effective_tax_rate",
            "effective_tax_rate_percent",
            "nopat_usd",
            "average_invested_capital_usd",
            "roic",
            "roic_percent",
            "all_date_rules_passed",
        ]
    ].sort_values("as_of_date")

    result_df["as_of_date"] = (
        result_df["as_of_date"].dt.date
    )

    result_df["period_end"] = pd.to_datetime(
        result_df["period_end"],
        errors="coerce",
    ).dt.date

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    display_columns = [
        "as_of_date",
        "revenue_growth_percent",
        "fcf_margin_percent",
        "adjusted_net_debt_to_standardized_ebitda",
        "roic_percent",
        "all_date_rules_passed",
    ]

    print()
    print("=" * 125)
    print("Microsoft consolidated fundamentals dataset")
    print("=" * 125)

    print(
        result_df[display_columns].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
        )
    )

    print()
    print(f"הקובץ נשמר כאן:\n{OUTPUT_FILE}")

    if not result_df["all_date_rules_passed"].all():
        raise RuntimeError(
            "לפחות שורה אחת נכשלה בבדיקות התאריכים."
        )

    print()
    print(
        "שער 1 עבר: נוצר קובץ Microsoft מאוחד "
        "עם חמש נקודות בדיקה תקינות."
    )


if __name__ == "__main__":
    main()