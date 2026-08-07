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
    / "meta_2024_duplicate_fact_details.csv"
)

TARGET_CONCEPTS = {
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:OperatingIncomeLoss",
    "us-gaap:NetIncomeLoss",
}

TARGET_START_DATE = "2022-01-01"
TARGET_END_DATE = "2022-12-31"


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
        "fact_number",
        "fact_id",
        "concept",
        "context_id",
        "period_start",
        "period_end",
        "dimension_count",
        "unit",
        "decimals",
        "scale",
        "sign",
        "raw_value",
        "normalized_value",
        "value_type",
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


def select_target_facts(
    facts: pd.DataFrame,
) -> pd.DataFrame:
    selected = facts[
        facts["concept"].isin(
            TARGET_CONCEPTS
        )
        & (
            facts["period_start"]
            == TARGET_START_DATE
        )
        & (
            facts["period_end"]
            == TARGET_END_DATE
        )
        & (
            facts["dimension_count"]
            == "0"
        )
        & (
            facts["value_type"]
            == "numeric"
        )
    ].copy()

    return selected


def add_duplicate_analysis(
    selected: pd.DataFrame,
) -> pd.DataFrame:
    if selected.empty:
        return selected

    group_columns = [
        "concept",
        "context_id",
        "period_start",
        "period_end",
        "unit",
    ]

    selected["same_context_count"] = (
        selected.groupby(
            group_columns
        )["fact_number"]
        .transform("count")
    )

    selected["unique_raw_values"] = (
        selected.groupby(
            group_columns
        )["raw_value"]
        .transform("nunique")
    )

    selected["unique_normalized_values"] = (
        selected.groupby(
            group_columns
        )["normalized_value"]
        .transform("nunique")
    )

    return selected


def print_results(
    selected: pd.DataFrame,
) -> None:
    print()
    print("=" * 145)
    print(
        "META 2022 — INLINE XBRL DUPLICATE DETAILS"
    )
    print("=" * 145)

    if selected.empty:
        print(
            "לא נמצאו Facts מתאימים."
        )
        return

    display_columns = [
        "concept",
        "fact_number",
        "fact_id",
        "context_id",
        "raw_value",
        "scale",
        "sign",
        "normalized_value",
        "decimals",
        "unit",
        "same_context_count",
        "unique_raw_values",
        "unique_normalized_values",
    ]

    print(
        selected[
            display_columns
        ].to_string(
            index=False
        )
    )

    print()
    print("=" * 145)
    print("סיכום לפי Concept ו־Context")
    print("=" * 145)

    summary = (
        selected.groupby(
            [
                "concept",
                "context_id",
                "period_start",
                "period_end",
                "unit",
            ],
            dropna=False,
        )
        .agg(
            fact_count=(
                "fact_number",
                "count",
            ),
            unique_fact_ids=(
                "fact_id",
                "nunique",
            ),
            unique_raw_values=(
                "raw_value",
                "nunique",
            ),
            unique_normalized_values=(
                "normalized_value",
                "nunique",
            ),
            unique_scales=(
                "scale",
                "nunique",
            ),
            unique_signs=(
                "sign",
                "nunique",
            ),
        )
        .reset_index()
    )

    print(
        summary.to_string(
            index=False
        )
    )

    print()
    print(
        "כלל בטוח אפשרי:"
    )
    print(
        "אם Concept, Context, תקופה, יחידה "
        "והערך המנורמל זהים — מדובר בכפילות "
        "טכנית שניתן להסיר."
    )
    print(
        "אם הערך המנורמל שונה — לא בוחרים "
        "עד שנמצא מקור ההבדל."
    )


def main() -> None:
    facts = load_facts()

    selected = select_target_facts(
        facts
    )

    selected = add_duplicate_analysis(
        selected
    )

    selected = selected.sort_values(
        [
            "concept",
            "context_id",
            "fact_number",
        ]
    ).reset_index(drop=True)

    selected.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print_results(selected)

    print()
    print(
        f"קובץ התוצאה נשמר כאן:\n"
        f"{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()