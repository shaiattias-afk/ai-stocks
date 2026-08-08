from __future__ import annotations

from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

INPUT_FILE = (
    DATA_DIR
    / "meta_2024_inline_xbrl_facts.csv"
)

OUTPUT_FILE = (
    DATA_DIR
    / "meta_2024_core_income_metrics.csv"
)

TARGET_YEARS = {
    2022,
    2023,
    2024,
}

METRIC_TAGS = {
    "revenue": [
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    ],
    "operating_income": [
        "us-gaap:OperatingIncomeLoss",
    ],
    "net_income": [
        "us-gaap:NetIncomeLoss",
        "us-gaap:ProfitLoss",
    ],
}


def load_facts() -> pd.DataFrame:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            "קובץ ה־Inline XBRL לא נמצא:\n"
            f"{INPUT_FILE}"
        )

    facts = pd.read_csv(
        INPUT_FILE,
        dtype=str,
        keep_default_na=False,
    )

    required_columns = {
        "concept",
        "period_start",
        "period_end",
        "dimension_count",
        "unit",
        "normalized_value",
        "value_type",
        "context_id",
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


def parse_year(
    date_text: str,
) -> int | None:
    text = str(date_text).strip()

    if len(text) < 4:
        return None

    try:
        return int(text[:4])
    except ValueError:
        return None


def parse_value(
    value_text: str,
) -> float | None:
    text = str(value_text).strip()

    if not text:
        return None

    try:
        return float(text)
    except ValueError:
        return None


def is_annual_period(
    start_date: str,
    end_date: str,
) -> bool:
    try:
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
    except Exception:
        return False

    duration_days = (
        end - start
    ).days

    return 350 <= duration_days <= 380


def extract_candidates(
    facts: pd.DataFrame,
) -> pd.DataFrame:
    records = []

    for metric_name, allowed_tags in METRIC_TAGS.items():
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

            fiscal_year = parse_year(
                row["period_end"]
            )

            if fiscal_year not in TARGET_YEARS:
                continue

            numeric_value = parse_value(
                row["normalized_value"]
            )

            if numeric_value is None:
                continue

            records.append(
                {
                    "metric": metric_name,
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


def validate_unique_candidates(
    candidates: pd.DataFrame,
) -> None:
    expected_pairs = {
        (metric, year)
        for metric in METRIC_TAGS
        for year in TARGET_YEARS
    }

    actual_pairs = set(
        zip(
            candidates["metric"],
            candidates["fiscal_year"],
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

    duplicate_groups = (
        candidates.groupby(
            [
                "metric",
                "fiscal_year",
            ]
        )
        .size()
        .reset_index(
            name="candidate_count"
        )
    )

    duplicates = duplicate_groups[
        duplicate_groups[
            "candidate_count"
        ]
        > 1
    ]

    if not duplicates.empty:
        print()
        print(
            "נמצאו כמה מועמדים לאותו "
            "מדד ושנה:"
        )

        print(
            duplicates.to_string(
                index=False
            )
        )


def main() -> None:
    facts = load_facts()

    candidates = extract_candidates(
        facts
    )

    if candidates.empty:
        raise RuntimeError(
            "לא נמצאו מועמדים לשלושת המדדים."
        )

    candidates = candidates.sort_values(
        [
            "metric",
            "fiscal_year",
            "concept",
            "context_id",
        ]
    ).reset_index(drop=True)

    candidates.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 100)
    print(
        "META — CORE INCOME METRICS"
    )
    print("=" * 100)

    display = candidates[
        [
            "metric",
            "fiscal_year",
            "concept",
            "period_start",
            "period_end",
            "value",
            "context_id",
        ]
    ].copy()

    display["value"] = display[
        "value"
    ].map(
        lambda value: f"{value:,.0f}"
    )

    print(
        display.to_string(
            index=False
        )
    )

    validate_unique_candidates(
        candidates
    )

    print()
    print(
        f"קובץ התוצאה נשמר כאן:\n"
        f"{OUTPUT_FILE}"
    )

    print()
    print(
        "לא נבחר מועמד אוטומטית במקרה "
        "של כפילות. כל כפילות תופיע בפלט."
    )


if __name__ == "__main__":
    main()