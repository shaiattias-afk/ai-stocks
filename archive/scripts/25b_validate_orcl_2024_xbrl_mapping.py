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
    / "orcl_2024_xbrl_mapping_validation.csv"
)


TARGET_REPORT_DATE = pd.Timestamp("2024-05-31")

# ערכים שאומתו ידנית מתוך טבלת ה-10-K הרשמית.
VALIDATED_VALUES_USD = {
    "operating_income": 15_353_000_000,
    "pretax_income": 11_741_000_000,
    "tax_expense": 1_274_000_000,
}

# תג סטנדרטי מותר כאשר הוא תואם בדיוק לערך המאומת.
ALLOWED_STANDARD_TAGS = {
    "operating_income": {
        "us-gaap:OperatingIncomeLoss",
    },
    "pretax_income": {
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "us-gaap:IncomeBeforeTaxExpenseBenefit",
    },
    "tax_expense": {
        "us-gaap:IncomeTaxExpenseBenefit",
    },
}


def main() -> None:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ עובדות ה-XBRL לא נמצא:\n{INPUT_FILE}"
        )

    facts_df = pd.read_csv(INPUT_FILE)

    required_columns = {
        "report_date",
        "fact_name",
        "period_start",
        "period_end",
        "unit_definition",
        "numeric_value",
        "has_dimensions",
        "date_rule_passed",
        "source_file",
    }

    missing_columns = required_columns - set(facts_df.columns)

    if missing_columns:
        raise RuntimeError(
            "בקובץ עובדות ה-XBRL חסרות עמודות:\n"
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

    report_df = facts_df[
        facts_df["report_date"] == TARGET_REPORT_DATE
    ].copy()

    if report_df.empty:
        raise RuntimeError(
            "לא נמצאו עובדות XBRL עבור דוח Oracle 2024."
        )

    results = []

    for field_name, validated_value in (
        VALIDATED_VALUES_USD.items()
    ):
        allowed_tags = ALLOWED_STANDARD_TAGS[
            field_name
        ]

        candidates = report_df[
            report_df["fact_name"].isin(
                allowed_tags
            )
            & (
                report_df["numeric_value"]
                == validated_value
            )
        ].copy()

        candidates = candidates.drop_duplicates(
            subset=[
                "fact_name",
                "period_start",
                "period_end",
                "numeric_value",
                "source_file",
            ]
        )

        if len(candidates) == 0:
            results.append(
                {
                    "field_name": field_name,
                    "validated_value_usd": validated_value,
                    "matched_fact_name": None,
                    "matched_value_usd": None,
                    "match_count": 0,
                    "mapping_status": "NO_EXACT_MATCH",
                    "approved_for_use": False,
                }
            )

            continue

        if len(candidates) > 1:
            results.append(
                {
                    "field_name": field_name,
                    "validated_value_usd": validated_value,
                    "matched_fact_name": " | ".join(
                        sorted(
                            candidates[
                                "fact_name"
                            ].unique()
                        )
                    ),
                    "matched_value_usd": validated_value,
                    "match_count": len(candidates),
                    "mapping_status": "MULTIPLE_EXACT_MATCHES",
                    "approved_for_use": False,
                }
            )

            continue

        selected = candidates.iloc[0]

        results.append(
            {
                "field_name": field_name,
                "validated_value_usd": validated_value,
                "matched_fact_name": selected["fact_name"],
                "matched_value_usd": selected["numeric_value"],
                "period_start": selected["period_start"],
                "period_end": selected["period_end"],
                "unit_definition": selected["unit_definition"],
                "source_file": selected["source_file"],
                "match_count": 1,
                "mapping_status": "EXACT_MATCH",
                "approved_for_use": True,
            }
        )

    result_df = pd.DataFrame(results)

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 125)
    print("Oracle 2024 XBRL mapping validation")
    print("=" * 125)

    print(
        result_df[
            [
                "field_name",
                "validated_value_usd",
                "matched_fact_name",
                "match_count",
                "mapping_status",
                "approved_for_use",
            ]
        ].to_string(
            index=False
        )
    )

    print()
    print(
        f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}"
    )

    approved_count = int(
        result_df["approved_for_use"].sum()
    )

    print()
    print(
        f"אושרו {approved_count} מתוך "
        f"{len(VALIDATED_VALUES_USD)} מיפויים."
    )

    if approved_count < len(
        VALIDATED_VALUES_USD
    ):
        print()
        print(
            "הבדיקה נעצרה בבטחה: לא לכל הנתונים "
            "נמצאה התאמת XBRL מדויקת. "
            "לא מחושב NOPAT מה-XBRL בשלב זה."
        )
    else:
        print()
        print(
            "כל שלושת המיפויים עברו התאמה מדויקת "
            "לערכים שאומתו בטבלת ה-10-K."
        )


if __name__ == "__main__":
    main()