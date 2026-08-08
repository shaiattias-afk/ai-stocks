from __future__ import annotations

from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

INPUT_FILE = (
    DATA_DIR
    / "meta_2024_core_income_metrics.csv"
)

OUTPUT_FILE = (
    DATA_DIR
    / "meta_2024_duplicate_income_facts_inspection.csv"
)


def load_candidates() -> pd.DataFrame:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            "קובץ המועמדים לא נמצא:\n"
            f"{INPUT_FILE}"
        )

    candidates = pd.read_csv(
        INPUT_FILE,
        dtype=str,
        keep_default_na=False,
    )

    required_columns = {
        "metric",
        "fiscal_year",
        "concept",
        "period_start",
        "period_end",
        "context_id",
        "unit",
        "value",
        "source_file",
    }

    missing_columns = (
        required_columns
        - set(candidates.columns)
    )

    if missing_columns:
        raise RuntimeError(
            "חסרות עמודות בקובץ הקלט:\n"
            + "\n".join(
                sorted(missing_columns)
            )
        )

    return candidates


def identify_duplicate_groups(
    candidates: pd.DataFrame,
) -> pd.DataFrame:
    group_counts = (
        candidates.groupby(
            [
                "metric",
                "fiscal_year",
            ],
            dropna=False,
        )
        .size()
        .reset_index(
            name="candidate_count"
        )
    )

    duplicate_groups = group_counts[
        group_counts["candidate_count"] > 1
    ].copy()

    return duplicate_groups


def build_duplicate_details(
    candidates: pd.DataFrame,
    duplicate_groups: pd.DataFrame,
) -> pd.DataFrame:
    if duplicate_groups.empty:
        return pd.DataFrame()

    duplicate_details = candidates.merge(
        duplicate_groups,
        on=[
            "metric",
            "fiscal_year",
        ],
        how="inner",
    )

    duplicate_details["value_numeric"] = (
        pd.to_numeric(
            duplicate_details["value"],
            errors="coerce",
        )
    )

    duplicate_details = (
        duplicate_details.sort_values(
            [
                "metric",
                "fiscal_year",
                "concept",
                "period_start",
                "period_end",
                "value_numeric",
                "context_id",
            ]
        )
        .reset_index(drop=True)
    )

    return duplicate_details


def print_group_analysis(
    duplicate_details: pd.DataFrame,
) -> None:
    if duplicate_details.empty:
        print(
            "לא נמצאו קבוצות עם יותר "
            "ממועמד אחד."
        )
        return

    for (
        metric,
        fiscal_year,
    ), group in duplicate_details.groupby(
        [
            "metric",
            "fiscal_year",
        ],
        sort=True,
    ):
        print()
        print("=" * 110)
        print(
            f"{metric} — {fiscal_year}"
        )
        print("=" * 110)

        display_columns = [
            "concept",
            "period_start",
            "period_end",
            "value",
            "unit",
            "context_id",
        ]

        print(
            group[
                display_columns
            ].to_string(
                index=False
            )
        )

        unique_values = (
            group["value"]
            .drop_duplicates()
            .tolist()
        )

        unique_concepts = (
            group["concept"]
            .drop_duplicates()
            .tolist()
        )

        unique_periods = (
            group[
                [
                    "period_start",
                    "period_end",
                ]
            ]
            .drop_duplicates()
        )

        print()
        print(
            "מספר מועמדים: "
            f"{len(group)}"
        )

        print(
            "מספר ערכים שונים: "
            f"{len(unique_values)}"
        )

        print(
            "מספר תגיות שונות: "
            f"{len(unique_concepts)}"
        )

        print(
            "מספר תקופות שונות: "
            f"{len(unique_periods)}"
        )

        if len(unique_values) == 1:
            print(
                "מסקנה זמנית: כל המועמדים "
                "מציגים אותו ערך כספי."
            )
        else:
            print(
                "מסקנה זמנית: קיימים ערכים "
                "כספיים שונים ולכן אסור לבחור "
                "אוטומטית."
            )

        if len(unique_concepts) > 1:
            print(
                "קיימות כמה תגיות XBRL "
                "עבור אותו מדד."
            )

        if len(unique_periods) > 1:
            print(
                "קיימות כמה תקופות דיווח "
                "עבור אותה שנת כספים."
            )


def main() -> None:
    candidates = load_candidates()

    duplicate_groups = (
        identify_duplicate_groups(
            candidates
        )
    )

    duplicate_details = (
        build_duplicate_details(
            candidates=candidates,
            duplicate_groups=duplicate_groups,
        )
    )

    if duplicate_details.empty:
        print()
        print(
            "לא נמצאו כפילויות בקובץ."
        )
        return

    duplicate_details.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 110)
    print(
        "META — DUPLICATE INLINE XBRL "
        "FACTS INSPECTION"
    )
    print("=" * 110)

    print_group_analysis(
        duplicate_details
    )

    print()
    print("=" * 110)
    print(
        "בשלב זה לא נבחר אף מועמד."
    )
    print(
        "הבדיקה נועדה לקבוע האם הכפילויות "
        "זהות או מהותיות."
    )

    print()
    print(
        f"קובץ הבדיקה נשמר כאן:\n"
        f"{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()