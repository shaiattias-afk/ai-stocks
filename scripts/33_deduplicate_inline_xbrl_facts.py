from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

DEDUPLICATION_KEY = [
    "concept",
    "context_id",
    "period_start",
    "period_end",
    "instant_date",
    "dimension_count",
    "dimensions_json",
    "unit",
    "normalized_value",
    "value_type",
]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove technical duplicate Inline XBRL facts "
            "without choosing between different accounting values."
        )
    )

    parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker, for example META.",
    )

    parser.add_argument(
        "--year",
        required=True,
        type=int,
        help="Filing year, for example 2024.",
    )

    return parser.parse_args()


def build_input_file(
    ticker: str,
    filing_year: int,
) -> Path:
    return (
        DATA_DIR
        / (
            f"{ticker.lower()}_"
            f"{filing_year}_"
            "inline_xbrl_facts.csv"
        )
    )


def build_output_file(
    ticker: str,
    filing_year: int,
) -> Path:
    return (
        DATA_DIR
        / (
            f"{ticker.lower()}_"
            f"{filing_year}_"
            "inline_xbrl_facts_deduplicated.csv"
        )
    )


def build_duplicate_report_file(
    ticker: str,
    filing_year: int,
) -> Path:
    return (
        DATA_DIR
        / (
            f"{ticker.lower()}_"
            f"{filing_year}_"
            "inline_xbrl_duplicate_report.csv"
        )
    )


def load_facts(
    input_file: Path,
) -> pd.DataFrame:
    if not input_file.exists():
        raise FileNotFoundError(
            "קובץ ה־Inline XBRL לא נמצא:\n"
            f"{input_file}"
        )

    facts = pd.read_csv(
        input_file,
        dtype=str,
        keep_default_na=False,
    )

    missing_columns = (
        set(DEDUPLICATION_KEY)
        - set(facts.columns)
    )

    if missing_columns:
        raise RuntimeError(
            "חסרות עמודות הדרושות להסרת כפילויות:\n"
            + "\n".join(
                sorted(missing_columns)
            )
        )

    return facts


def build_duplicate_report(
    facts: pd.DataFrame,
) -> pd.DataFrame:
    group_summary = (
        facts.groupby(
            DEDUPLICATION_KEY,
            dropna=False,
        )
        .agg(
            occurrence_count=(
                "fact_number",
                "count",
            ),
            unique_fact_ids=(
                "fact_id",
                "nunique",
            ),
            first_fact_number=(
                "fact_number",
                "min",
            ),
        )
        .reset_index()
    )

    duplicate_report = group_summary[
        group_summary[
            "occurrence_count"
        ]
        > 1
    ].copy()

    duplicate_report = (
        duplicate_report.sort_values(
            [
                "occurrence_count",
                "concept",
                "period_end",
                "instant_date",
            ],
            ascending=[
                False,
                True,
                True,
                True,
            ],
        )
        .reset_index(drop=True)
    )

    return duplicate_report


def deduplicate_facts(
    facts: pd.DataFrame,
) -> pd.DataFrame:
    working = facts.copy()

    working["_original_order"] = range(
        len(working)
    )

    deduplicated = (
        working.sort_values(
            "_original_order"
        )
        .drop_duplicates(
            subset=DEDUPLICATION_KEY,
            keep="first",
        )
        .sort_values(
            "_original_order"
        )
        .drop(
            columns=[
                "_original_order",
            ]
        )
        .reset_index(drop=True)
    )

    return deduplicated


def validate_result(
    original: pd.DataFrame,
    deduplicated: pd.DataFrame,
) -> None:
    remaining_duplicates = (
        deduplicated.duplicated(
            subset=DEDUPLICATION_KEY,
            keep=False,
        )
    )

    if remaining_duplicates.any():
        raise RuntimeError(
            "לאחר ההסרה עדיין נשארו "
            "כפילויות לפי המפתח החשבונאי."
        )

    original_unique_keys = len(
        original[
            DEDUPLICATION_KEY
        ].drop_duplicates()
    )

    if (
        original_unique_keys
        != len(deduplicated)
    ):
        raise RuntimeError(
            "מספר העובדות לאחר ההסרה "
            "אינו תואם למספר המפתחות "
            "החשבונאיים הייחודיים."
        )


def print_summary(
    original: pd.DataFrame,
    deduplicated: pd.DataFrame,
    duplicate_report: pd.DataFrame,
    output_file: Path,
    duplicate_report_file: Path,
) -> None:
    removed_count = (
        len(original)
        - len(deduplicated)
    )

    duplicate_group_count = len(
        duplicate_report
    )

    print()
    print("=" * 100)
    print(
        "INLINE XBRL — TECHNICAL DEDUPLICATION"
    )
    print("=" * 100)

    print(
        "מספר Facts לפני הסרת כפילויות: "
        f"{len(original):,}"
    )

    print(
        "מספר Facts לאחר הסרת כפילויות: "
        f"{len(deduplicated):,}"
    )

    print(
        "מספר מופעים כפולים שהוסרו: "
        f"{removed_count:,}"
    )

    print(
        "מספר קבוצות כפולות: "
        f"{duplicate_group_count:,}"
    )

    print()

    if duplicate_report.empty:
        print(
            "לא נמצאו כפילויות טכניות."
        )
    else:
        print(
            "דוגמה לקבוצות כפולות:"
        )

        display_columns = [
            "concept",
            "context_id",
            "period_start",
            "period_end",
            "instant_date",
            "unit",
            "normalized_value",
            "occurrence_count",
            "unique_fact_ids",
        ]

        print(
            duplicate_report[
                display_columns
            ]
            .head(20)
            .to_string(
                index=False
            )
        )

    print()
    print(
        "הוסרו רק Facts שבהם כל השדות "
        "החשבונאיים והערך המנורמל זהים."
    )

    print(
        "Facts עם ערכים שונים, Context שונה, "
        "תקופה שונה או Dimensions שונים "
        "לא הוסרו."
    )

    print()
    print(
        f"קובץ העובדות הנקי נשמר כאן:\n"
        f"{output_file}"
    )

    print()
    print(
        f"דוח הכפילויות נשמר כאן:\n"
        f"{duplicate_report_file}"
    )


def main() -> None:
    arguments = parse_arguments()

    ticker = arguments.ticker.upper()
    filing_year = arguments.year

    input_file = build_input_file(
        ticker=ticker,
        filing_year=filing_year,
    )

    output_file = build_output_file(
        ticker=ticker,
        filing_year=filing_year,
    )

    duplicate_report_file = (
        build_duplicate_report_file(
            ticker=ticker,
            filing_year=filing_year,
        )
    )

    facts = load_facts(
        input_file
    )

    duplicate_report = (
        build_duplicate_report(
            facts
        )
    )

    deduplicated = deduplicate_facts(
        facts
    )

    validate_result(
        original=facts,
        deduplicated=deduplicated,
    )

    deduplicated.to_csv(
        output_file,
        index=False,
        encoding="utf-8-sig",
    )

    duplicate_report.to_csv(
        duplicate_report_file,
        index=False,
        encoding="utf-8-sig",
    )

    print_summary(
        original=facts,
        deduplicated=deduplicated,
        duplicate_report=duplicate_report,
        output_file=output_file,
        duplicate_report_file=(
            duplicate_report_file
        ),
    )


if __name__ == "__main__":
    main()