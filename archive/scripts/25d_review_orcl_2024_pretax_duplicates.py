from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

INPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_2024_pretax_exact_value_candidates.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_2024_pretax_duplicate_review.csv"
)


def main() -> None:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ ההתאמות לא נמצא:\n{INPUT_FILE}"
        )

    df = pd.read_csv(INPUT_FILE)

    required_columns = {
        "fact_name",
        "namespace",
        "local_tag",
        "period_start",
        "period_end",
        "unit_definition",
        "numeric_value",
        "has_dimensions",
        "date_rule_passed",
        "source_file",
    }

    missing = required_columns - set(df.columns)

    if missing:
        raise RuntimeError(
            "בקובץ חסרות עמודות:\n"
            f"{sorted(missing)}"
        )

    if len(df) != 2:
        raise RuntimeError(
            "ציפינו לשתי התאמות בדיוק, "
            f"אך נמצאו {len(df)}."
        )

    review_columns = [
        "fact_name",
        "namespace",
        "local_tag",
        "period_start",
        "period_end",
        "unit_definition",
        "numeric_value",
        "has_dimensions",
        "date_rule_passed",
        "source_file",
    ]

    review_df = df[review_columns].copy()

    semantic_columns = [
        "fact_name",
        "period_start",
        "period_end",
        "unit_definition",
        "numeric_value",
        "has_dimensions",
    ]

    unique_semantic_df = review_df.drop_duplicates(
        subset=semantic_columns
    )

    review_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 150)
    print("Oracle 2024 Pretax Income — duplicate review")
    print("=" * 150)

    print(
        review_df.to_string(
            index=False
        )
    )

    print()
    print(
        "מספר שורות מקור:",
        len(review_df),
    )

    print(
        "מספר עובדות חשבונאיות ייחודיות:",
        len(unique_semantic_df),
    )

    print()
    print(
        f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}"
    )

    if len(unique_semantic_df) == 1:
        selected = unique_semantic_df.iloc[0]

        print()
        print(
            "הבדיקה עברה: שתי השורות הן כפילות טכנית "
            "של אותה עובדת XBRL."
        )

        print(
            "תג מאומת:",
            selected["fact_name"],
        )

        print(
            "תקופה:",
            selected["period_start"],
            "עד",
            selected["period_end"],
        )

        print(
            "ערך:",
            f"{float(selected['numeric_value']):,.0f}",
        )
    else:
        print()
        print(
            "הבדיקה נעצרה: נמצאו שתי עובדות "
            "חשבונאיות שונות עם אותו ערך."
        )

        print(
            "לא מאשרים תג ולא מחשבים NOPAT "
            "עד לבדיקת המקור."
        )


if __name__ == "__main__":
    main()