from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

METRIC_TAGS = {
    "revenue": [
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:Revenues",
    ],
    "operating_income": [
        "us-gaap:OperatingIncomeLoss",
    ],
    "net_income": [
        "us-gaap:NetIncomeLoss",
        "us-gaap:ProfitLoss",
    ],
}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract annual core income-statement metrics "
            "from deduplicated Inline XBRL facts."
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
        help="10-K filing year, for example 2024.",
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
            "inline_xbrl_facts_deduplicated.csv"
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
            "core_income_metrics.csv"
        )
    )


def load_facts(
    input_file: Path,
) -> pd.DataFrame:
    if not input_file.exists():
        raise FileNotFoundError(
            "קובץ העובדות הנקי לא נמצא:\n"
            f"{input_file}"
        )

    facts = pd.read_csv(
        input_file,
        dtype=str,
        keep_default_na=False,
    )

    required_columns = {
        "concept",
        "context_id",
        "period_start",
        "period_end",
        "dimension_count",
        "unit",
        "normalized_value",
        "value_type",
        "source_file",
    }

    missing_columns = (
        required_columns
        - set(facts.columns)
    )

    if missing_columns:
        raise RuntimeError(
            "חסרות עמודות בקובץ הקלט:\n"
            + "\n".join(
                sorted(missing_columns)
            )
        )

    return facts


def parse_date(
    value: str,
) -> pd.Timestamp | None:
    try:
        return pd.Timestamp(value)
    except Exception:
        return None


def is_annual_period(
    start_date: str,
    end_date: str,
) -> bool:
    start = parse_date(start_date)
    end = parse_date(end_date)

    if start is None or end is None:
        return False

    duration_days = (
        end - start
    ).days

    return 350 <= duration_days <= 380


def parse_numeric_value(
    value: str,
) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def extract_candidates(
    facts: pd.DataFrame,
    filing_year: int,
) -> pd.DataFrame:
    target_years = {
        filing_year,
        filing_year - 1,
        filing_year - 2,
    }

    records = []

    for metric, allowed_tags in METRIC_TAGS.items():
        metric_facts = facts[
            facts["concept"].isin(
                allowed_tags
            )
        ].copy()

        for _, row in metric_facts.iterrows():
            if row["value_type"] != "numeric":
                continue

            try:
                dimension_count = int(
                    row["dimension_count"]
                )
            except ValueError:
                continue

            if dimension_count != 0:
                continue

            if "USD" not in row["unit"]:
                continue

            if not is_annual_period(
                row["period_start"],
                row["period_end"],
            ):
                continue

            period_end = parse_date(
                row["period_end"]
            )

            if period_end is None:
                continue

            fiscal_year = int(
                period_end.year
            )

            if fiscal_year not in target_years:
                continue

            numeric_value = parse_numeric_value(
                row["normalized_value"]
            )

            if numeric_value is None:
                continue

            records.append(
                {
                    "metric": metric,
                    "fiscal_year": fiscal_year,
                    "concept": row["concept"],
                    "period_start": row["period_start"],
                    "period_end": row["period_end"],
                    "context_id": row["context_id"],
                    "unit": row["unit"],
                    "value": numeric_value,
                    "source_file": row["source_file"],
                }
            )

    return pd.DataFrame(records)


def summarize_candidates(
    candidates: pd.DataFrame,
) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()

    summary = (
        candidates.groupby(
            [
                "metric",
                "fiscal_year",
            ],
            dropna=False,
        )
        .agg(
            candidate_count=(
                "value",
                "count",
            ),
            unique_value_count=(
                "value",
                "nunique",
            ),
            unique_concept_count=(
                "concept",
                "nunique",
            ),
            selected_value=(
                "value",
                "first",
            ),
        )
        .reset_index()
    )

    summary["status"] = summary.apply(
        determine_status,
        axis=1,
    )

    return summary


def determine_status(
    row: pd.Series,
) -> str:
    if (
        row["candidate_count"] == 1
        and row["unique_value_count"] == 1
    ):
        return "PASS"

    if row["unique_value_count"] == 1:
        return "PASS_IDENTICAL_VALUES"

    return "REVIEW_REQUIRED"


def validate_expected_metrics(
    summary: pd.DataFrame,
    filing_year: int,
) -> None:
    target_years = {
        filing_year,
        filing_year - 1,
        filing_year - 2,
    }

    expected_pairs = {
        (metric, year)
        for metric in METRIC_TAGS
        for year in target_years
    }

    actual_pairs = set(
        zip(
            summary["metric"],
            summary["fiscal_year"],
        )
    )

    missing_pairs = (
        expected_pairs
        - actual_pairs
    )

    if missing_pairs:
        print()
        print("חסרים מדדים:")

        for metric, year in sorted(
            missing_pairs
        ):
            print(
                f"- {metric}, {year}"
            )


def print_results(
    summary: pd.DataFrame,
) -> None:
    print()
    print("=" * 110)
    print(
        "CORE INCOME METRICS — INLINE XBRL"
    )
    print("=" * 110)

    if summary.empty:
        print(
            "לא נמצאו מדדים מתאימים."
        )
        return

    display = summary.copy()

    display["selected_value"] = (
        display["selected_value"]
        .map(
            lambda value: f"{value:,.0f}"
        )
    )

    print(
        display[
            [
                "metric",
                "fiscal_year",
                "selected_value",
                "candidate_count",
                "unique_value_count",
                "unique_concept_count",
                "status",
            ]
        ].sort_values(
            [
                "metric",
                "fiscal_year",
            ]
        ).to_string(
            index=False
        )
    )

    review_rows = summary[
        summary["status"]
        == "REVIEW_REQUIRED"
    ]

    print()

    if review_rows.empty:
        print(
            "כל המדדים עברו את בדיקת הייחודיות."
        )
    else:
        print(
            "יש מדדים הדורשים בדיקה:"
        )

        print(
            review_rows[
                [
                    "metric",
                    "fiscal_year",
                    "candidate_count",
                    "unique_value_count",
                ]
            ].to_string(
                index=False
            )
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

    facts = load_facts(
        input_file
    )

    candidates = extract_candidates(
        facts=facts,
        filing_year=filing_year,
    )

    if candidates.empty:
        raise RuntimeError(
            "לא נמצאו מועמדים למדדים."
        )

    summary = summarize_candidates(
        candidates
    )

    validate_expected_metrics(
        summary=summary,
        filing_year=filing_year,
    )

    summary = summary.sort_values(
        [
            "metric",
            "fiscal_year",
        ]
    ).reset_index(drop=True)

    summary.to_csv(
        output_file,
        index=False,
        encoding="utf-8-sig",
    )

    print_results(summary)

    print()
    print(
        f"קובץ התוצאה נשמר כאן:\n"
        f"{output_file}"
    )


if __name__ == "__main__":
    main()