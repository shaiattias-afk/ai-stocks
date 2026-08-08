from __future__ import annotations

from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

INPUT_FILE = (
    DATA_DIR
    / "orcl_2024_inline_xbrl_facts_deduplicated.csv"
)

OUTPUT_FILE = (
    DATA_DIR
    / "orcl_2024_net_income_conflicts.csv"
)

TARGET_CONCEPTS = {
    "us-gaap:NetIncomeLoss",
    "us-gaap:ProfitLoss",
}

TARGET_PERIOD_ENDS = {
    "2022-05-31",
    "2023-05-31",
}


def load_facts() -> pd.DataFrame:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            "קובץ Oracle הנקי לא נמצא:\n"
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
        "instant_date",
        "dimension_count",
        "dimensions_json",
        "unit",
        "decimals",
        "scale",
        "sign",
        "raw_value",
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


def select_conflicts(
    facts: pd.DataFrame,
) -> pd.DataFrame:
    selected = facts[
        facts["concept"].isin(
            TARGET_CONCEPTS
        )
        & facts["period_end"].isin(
            TARGET_PERIOD_ENDS
        )
        & (
            facts["dimension_count"]
            == "0"
        )
        & (
            facts["value_type"]
            == "numeric"
        )
        & facts["unit"].str.contains(
            "USD",
            case=False,
            na=False,
        )
    ].copy()

    selected = selected[
        selected.apply(
            lambda row: is_annual_period(
                row["period_start"],
                row["period_end"],
            ),
            axis=1,
        )
    ].copy()

    selected["fiscal_year"] = (
        selected["period_end"]
        .str[:4]
    )

    selected["value_numeric"] = (
        pd.to_numeric(
            selected["normalized_value"],
            errors="coerce",
        )
    )

    selected = selected.sort_values(
        [
            "fiscal_year",
            "concept",
            "value_numeric",
            "context_id",
        ]
    ).reset_index(drop=True)

    return selected


def print_results(
    selected: pd.DataFrame,
) -> None:
    print()
    print("=" * 130)
    print(
        "ORACLE — NET INCOME CONFLICT INSPECTION"
    )
    print("=" * 130)

    if selected.empty:
        print(
            "לא נמצאו Facts מתאימים."
        )
        return

    display_columns = [
        "fiscal_year",
        "concept",
        "period_start",
        "period_end",
        "normalized_value",
        "raw_value",
        "context_id",
        "dimension_count",
        "unit",
        "scale",
        "sign",
        "fact_id",
    ]

    print(
        selected[
            display_columns
        ].to_string(
            index=False
        )
    )

    print()
    print("=" * 130)
    print("סיכום לפי שנה")
    print("=" * 130)

    for fiscal_year, group in selected.groupby(
        "fiscal_year",
        sort=True,
    ):
        print()
        print(f"שנת כספים {fiscal_year}")

        print(
            f"מספר מועמדים: {len(group)}"
        )

        print(
            "מספר ערכים שונים: "
            f"{group['normalized_value'].nunique()}"
        )

        print(
            "מספר Concepts שונים: "
            f"{group['concept'].nunique()}"
        )

        print(
            "Concepts:"
        )

        for concept in (
            group["concept"]
            .drop_duplicates()
            .tolist()
        ):
            print(f"- {concept}")

        print(
            "ערכים:"
        )

        for value in (
            group["value_numeric"]
            .dropna()
            .drop_duplicates()
            .sort_values()
            .tolist()
        ):
            print(
                f"- {value:,.0f}"
            )

    print()
    print("=" * 130)
    print(
        "לא נבחר ערך. הפלט נועד לזהות "
        "האם ההבדל נובע מתגיות שונות, "
        "Context שונה או משמעות חשבונאית שונה."
    )


def main() -> None:
    facts = load_facts()

    selected = select_conflicts(
        facts
    )

    if selected.empty:
        raise RuntimeError(
            "לא נמצאו מועמדי Net Income "
            "עבור Oracle בשנים 2022–2023."
        )

    selected.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print_results(
        selected
    )

    print()
    print(
        f"קובץ הבדיקה נשמר כאן:\n"
        f"{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()