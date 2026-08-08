from __future__ import annotations

from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

INPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_inline_xbrl_candidates_2020_2024.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_annual_xbrl_facts_2020_2024.csv"
)


def normalize_boolean(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series

    normalized = (
        series.astype(str)
        .str.strip()
        .str.lower()
        .map(
            {
                "true": True,
                "false": False,
                "1": True,
                "0": False,
            }
        )
    )

    if normalized.isna().any():
        raise RuntimeError(
            "נמצא ערך שאינו True/False."
        )

    return normalized.astype(bool)


def main() -> None:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המקור לא נמצא:\n{INPUT_FILE}"
        )

    df = pd.read_csv(INPUT_FILE)

    required_columns = {
        "report_date",
        "fact_name",
        "namespace",
        "local_tag",
        "period_start",
        "period_end",
        "has_dimensions",
        "unit_definition",
        "numeric_value",
        "numeric_value_usd_billions",
        "date_rule_passed",
        "source_file",
    }

    missing_columns = required_columns - set(df.columns)

    if missing_columns:
        raise RuntimeError(
            "בקובץ המקור חסרות עמודות:\n"
            f"{sorted(missing_columns)}"
        )

    df["report_date"] = pd.to_datetime(
        df["report_date"],
        errors="coerce",
    )

    df["period_start"] = pd.to_datetime(
        df["period_start"],
        errors="coerce",
    )

    df["period_end"] = pd.to_datetime(
        df["period_end"],
        errors="coerce",
    )

    df["numeric_value"] = pd.to_numeric(
        df["numeric_value"],
        errors="coerce",
    )

    df["numeric_value_usd_billions"] = pd.to_numeric(
        df["numeric_value_usd_billions"],
        errors="coerce",
    )

    df["has_dimensions"] = normalize_boolean(
        df["has_dimensions"]
    )

    df["date_rule_passed"] = normalize_boolean(
        df["date_rule_passed"]
    )

    df["period_days"] = (
        df["period_end"]
        - df["period_start"]
    ).dt.days

    # סינון קשיח בלבד — ללא דירוג וללא בחירה לפי הסתברות.
    filtered_df = df[
        df["report_date"].notna()
        & df["period_start"].notna()
        & df["period_end"].notna()
        & df["numeric_value"].notna()
        & (df["period_end"] == df["report_date"])
        & df["period_days"].between(300, 400)
        & (~df["has_dimensions"])
        & df["date_rule_passed"]
    ].copy()

    # בודקים שהיחידה היא USD.
    filtered_df = filtered_df[
        filtered_df["unit_definition"]
        .astype(str)
        .str.upper()
        .str.contains(
            "USD",
            regex=False,
            na=False,
        )
    ].copy()

    if filtered_df.empty:
        raise RuntimeError(
            "לא נמצאו עובדות שנתיות שעברו "
            "את כל תנאי הסינון הקשיחים."
        )

    filtered_df = filtered_df[
        [
            "report_date",
            "fact_name",
            "namespace",
            "local_tag",
            "period_start",
            "period_end",
            "period_days",
            "unit_definition",
            "numeric_value",
            "numeric_value_usd_billions",
            "has_dimensions",
            "date_rule_passed",
            "source_file",
        ]
    ].drop_duplicates().sort_values(
        by=[
            "report_date",
            "fact_name",
            "numeric_value",
        ]
    )

    filtered_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 175)
    print(
        "Oracle annual Inline XBRL facts — "
        "strictly filtered"
    )
    print("=" * 175)

    print(
        filtered_df[
            [
                "report_date",
                "fact_name",
                "period_start",
                "period_end",
                "unit_definition",
                "numeric_value_usd_billions",
            ]
        ].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
        )
    )

    print()
    print(
        f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}"
    )

    print()
    print(
        "לא נבחר תג ולא חושב NOPAT. "
        "הוצגו רק עובדות שנתיות שעברו "
        "את כל תנאי הסינון הקשיחים."
    )


if __name__ == "__main__":
    main()