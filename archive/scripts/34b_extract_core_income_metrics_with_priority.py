from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

METRIC_TAG_PRIORITY = {
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
            "Extract annual income metrics from deduplicated "
            "Inline XBRL facts using an explicit concept priority."
        )
    )

    parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker, for example ORCL.",
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
            "core_income_metrics_selected.csv"
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


def collect_candidates(
    facts: pd.DataFrame,
    filing_year: int,
) -> pd.DataFrame:
    target_years = {
        filing_year,
        filing_year - 1,
        filing_year - 2,
    }

    records = []

    for metric, concepts in METRIC_TAG_PRIORITY.items():
        concept_priorities = {
            concept: index + 1
            for index, concept in enumerate(
                concepts
            )
        }

        metric_facts = facts[
            facts["concept"].isin(
                concepts
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

            value = parse_numeric_value(
                row["normalized_value"]
            )

            if value is None:
                continue

            records.append(
                {
                    "metric": metric,
                    "fiscal_year": fiscal_year,
                    "concept": row["concept"],
                    "concept_priority":
                        concept_priorities[
                            row["concept"]
                        ],
                    "period_start":
                        row["period_start"],
                    "period_end":
                        row["period_end"],
                    "context_id":
                        row["context_id"],
                    "unit": row["unit"],
                    "value": value,
                    "source_file":
                        row["source_file"],
                }
            )

    return pd.DataFrame(records)


def select_values(
    candidates: pd.DataFrame,
) -> pd.DataFrame:
    records = []

    for (
        metric,
        fiscal_year,
    ), group in candidates.groupby(
        [
            "metric",
            "fiscal_year",
        ],
        sort=True,
    ):
        minimum_priority = int(
            group["concept_priority"].min()
        )

        preferred = group[
            group["concept_priority"]
            == minimum_priority
        ].copy()

        unique_values = (
            preferred["value"]
            .drop_duplicates()
            .tolist()
        )

        if len(unique_values) == 1:
            status = "PASS"

            if len(preferred) > 1:
                status = (
                    "PASS_IDENTICAL_VALUES"
                )

            selected_value = unique_values[0]
            selected_concept = (
                preferred["concept"].iloc[0]
            )
        else:
            status = "REVIEW_REQUIRED"
            selected_value = None
            selected_concept = (
                preferred["concept"].iloc[0]
            )

        alternative_concepts = (
            group[
                group["concept_priority"]
                > minimum_priority
            ]["concept"]
            .drop_duplicates()
            .tolist()
        )

        alternative_values = (
            group[
                group["concept_priority"]
                > minimum_priority
            ]["value"]
            .drop_duplicates()
            .tolist()
        )

        records.append(
            {
                "metric": metric,
                "fiscal_year": fiscal_year,
                "selected_concept":
                    selected_concept,
                "selected_value":
                    selected_value,
                "preferred_candidate_count":
                    len(preferred),
                "preferred_unique_value_count":
                    len(unique_values),
                "alternative_concepts":
                    " | ".join(
                        alternative_concepts
                    ),
                "alternative_values":
                    " | ".join(
                        f"{value:,.0f}"
                        for value in
                        alternative_values
                    ),
                "status": status,
            }
        )

    return pd.DataFrame(records)


def validate_expected_metrics(
    selected: pd.DataFrame,
    filing_year: int,
) -> None:
    target_years = {
        filing_year,
        filing_year - 1,
        filing_year - 2,
    }

    expected_pairs = {
        (metric, year)
        for metric in METRIC_TAG_PRIORITY
        for year in target_years
    }

    actual_pairs = set(
        zip(
            selected["metric"],
            selected["fiscal_year"],
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
    selected: pd.DataFrame,
) -> None:
    print()
    print("=" * 125)
    print(
        "CORE INCOME METRICS — CONCEPT PRIORITY"
    )
    print("=" * 125)

    display = selected.copy()

    display["selected_value"] = (
        display["selected_value"]
        .map(
            lambda value: (
                ""
                if pd.isna(value)
                else f"{value:,.0f}"
            )
        )
    )

    print(
        display[
            [
                "metric",
                "fiscal_year",
                "selected_concept",
                "selected_value",
                "alternative_concepts",
                "alternative_values",
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

    review_rows = selected[
        selected["status"]
        == "REVIEW_REQUIRED"
    ]

    print()

    if review_rows.empty:
        print(
            "כל המדדים עברו את כלל העדיפות."
        )
    else:
        print(
            "יש מדדים הדורשים בדיקה:"
        )

        print(
            review_rows.to_string(
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

    candidates = collect_candidates(
        facts=facts,
        filing_year=filing_year,
    )

    if candidates.empty:
        raise RuntimeError(
            "לא נמצאו מועמדים למדדים."
        )

    selected = select_values(
        candidates
    )

    validate_expected_metrics(
        selected=selected,
        filing_year=filing_year,
    )

    selected = selected.sort_values(
        [
            "metric",
            "fiscal_year",
        ]
    ).reset_index(drop=True)

    selected.to_csv(
        output_file,
        index=False,
        encoding="utf-8-sig",
    )

    print_results(
        selected
    )

    print()
    print(
        f"קובץ התוצאה נשמר כאן:\n"
        f"{output_file}"
    )


if __name__ == "__main__":
    main()