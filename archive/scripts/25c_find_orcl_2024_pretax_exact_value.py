from __future__ import annotations

from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

INPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_annual_xbrl_facts_2020_2024.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_2024_pretax_exact_value_candidates.csv"
)

TARGET_REPORT_DATE = pd.Timestamp("2024-05-31")
VALIDATED_PRETAX_USD = 11_741_000_000


def main() -> None:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ עובדות ה-XBRL לא נמצא:\n{INPUT_FILE}"
        )

    facts_df = pd.read_csv(INPUT_FILE)

    required_columns = {
        "report_date",
        "fact_name",
        "namespace",
        "local_tag",
        "period_start",
        "period_end",
        "period_days",
        "unit_definition",
        "numeric_value",
        "has_dimensions",
        "date_rule_passed",
        "source_file",
    }

    missing_columns = required_columns - set(facts_df.columns)

    if missing_columns:
        raise RuntimeError(
            "בקובץ המקור חסרות עמודות:\n"
            f"{sorted(missing_columns)}"
        )

    facts_df["report_date"] = pd.to_datetime(
        facts_df["report_date"],
        errors="coerce",
    )

    facts_df["numeric_value"] = pd.to_numeric(
        facts_df["numeric_value"],
        errors="coerce",
    )

    candidates = facts_df[
        (facts_df["report_date"] == TARGET_REPORT_DATE)
        & (facts_df["numeric_value"] == VALIDATED_PRETAX_USD)
    ].copy()

    candidates = candidates.drop_duplicates(
        subset=[
            "fact_name",
            "period_start",
            "period_end",
            "numeric_value",
            "source_file",
        ]
    ).sort_values(
        by=[
            "namespace",
            "fact_name",
            "period_start",
            "period_end",
        ]
    )

    candidates.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 145)
    print("Oracle 2024 Pretax Income — exact-value candidates")
    print("=" * 145)

    if candidates.empty:
        print(
            "לא נמצאה אף עובדת XBRL שנתית "
            "שערכה המדויק הוא 11,741,000,000 דולר."
        )
    else:
        print(
            candidates[
                [
                    "fact_name",
                    "namespace",
                    "local_tag",
                    "period_start",
                    "period_end",
                    "unit_definition",
                    "numeric_value",
                    "has_dimensions",
                    "date_rule_passed",
                ]
            ].to_string(
                index=False
            )
        )

    print()
    print(
        f"מספר התאמות מדויקות: {len(candidates)}"
    )

    print()
    print(
        f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}"
    )

    print()
    print(
        "לא אושר תג ולא חושב NOPAT. "
        "הוצגו רק עובדות שערכן זהה בדיוק "
        "ל-Pretax Income שאומת בטבלת ה-10-K."
    )


if __name__ == "__main__":
    main()