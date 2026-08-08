from __future__ import annotations

from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"

FUNDAMENTALS_FILE = (
    DATA_DIR / "msft_fundamentals_dataset.csv"
)

RETURNS_FILE = (
    DATA_DIR / "msft_price_returns_5_years.csv"
)

OUTPUT_FILE = (
    DATA_DIR / "msft_backtest_dataset.csv"
)


def load_csv(
    path: Path,
    required_columns: set[str],
    description: str,
) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"קובץ {description} לא נמצא:\n{path}"
        )

    df = pd.read_csv(path)

    missing = required_columns - set(df.columns)

    if missing:
        raise RuntimeError(
            f"בקובץ {description} חסרות עמודות:\n"
            f"{sorted(missing)}"
        )

    return df


def normalize_boolean(series: pd.Series) -> pd.Series:
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
            "נמצא ערך לא תקין בעמודת True/False."
        )

    return normalized.astype(bool)


def main() -> None:
    fundamentals_df = load_csv(
        FUNDAMENTALS_FILE,
        {
            "ticker",
            "company_name",
            "as_of_date",
            "revenue_growth_percent",
            "fcf_margin_percent",
            "adjusted_net_debt_to_standardized_ebitda",
            "roic_percent",
            "all_date_rules_passed",
        },
        "Microsoft Fundamentals",
    )

    returns_df = load_csv(
        RETURNS_FILE,
        {
            "start_target_date",
            "end_target_date",
            "start_trading_date",
            "end_trading_date",
            "msft_return_percent",
            "spy_return_percent",
            "qqq_return_percent",
            "excess_vs_spy_percent",
            "excess_vs_qqq_percent",
        },
        "Microsoft Returns",
    )

    fundamentals_df["as_of_date"] = pd.to_datetime(
        fundamentals_df["as_of_date"],
        errors="coerce",
    )

    returns_df["start_target_date"] = pd.to_datetime(
        returns_df["start_target_date"],
        errors="coerce",
    )

    if fundamentals_df["as_of_date"].isna().any():
        raise RuntimeError(
            "נמצאו תאריכים לא תקינים בקובץ הפונדמנטלי."
        )

    if returns_df["start_target_date"].isna().any():
        raise RuntimeError(
            "נמצאו תאריכים לא תקינים בקובץ התשואות."
        )

    fundamentals_df["all_date_rules_passed"] = (
        normalize_boolean(
            fundamentals_df["all_date_rules_passed"]
        )
    )

    result_df = fundamentals_df.merge(
        returns_df,
        left_on="as_of_date",
        right_on="start_target_date",
        how="inner",
        validate="one_to_one",
    )

    if len(result_df) != 5:
        raise RuntimeError(
            "האיחוד לא יצר בדיוק חמש שורות."
        )

    if not result_df[
        "all_date_rules_passed"
    ].all():
        raise RuntimeError(
            "לפחות שורת נתונים אחת נכשלה "
            "בבדיקת התאריכים."
        )

    result_df["beat_spy"] = (
        result_df["excess_vs_spy_percent"] > 0
    )

    result_df["beat_qqq"] = (
        result_df["excess_vs_qqq_percent"] > 0
    )

    result_df = result_df[
        [
            "ticker",
            "company_name",
            "as_of_date",
            "start_trading_date",
            "end_trading_date",
            "revenue_growth_percent",
            "fcf_margin_percent",
            "adjusted_net_debt_to_standardized_ebitda",
            "roic_percent",
            "msft_return_percent",
            "spy_return_percent",
            "qqq_return_percent",
            "excess_vs_spy_percent",
            "excess_vs_qqq_percent",
            "beat_spy",
            "beat_qqq",
            "all_date_rules_passed",
        ]
    ].sort_values("as_of_date")

    result_df["as_of_date"] = (
        result_df["as_of_date"].dt.date
    )

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
        "msft_return_percent",
        "spy_return_percent",
        "qqq_return_percent",
        "beat_spy",
        "beat_qqq",
    ]

    print()
    print("=" * 165)
    print("Microsoft fundamentals and forward returns")
    print("=" * 165)

    print(
        result_df[display_columns].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
        )
    )

    print()
    print(f"הקובץ נשמר כאן:\n{OUTPUT_FILE}")

    print()
    print(
        "הבדיקה עברה: הנתונים שהיו ידועים בכל "
        "1 באפריל חוברו לתשואה שהתקבלה בשנה שאחריו."
    )


if __name__ == "__main__":
    main()