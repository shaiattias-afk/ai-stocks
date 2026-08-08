from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent

CANDIDATES_DIR = (
    PROJECT_DIR
    / "data"
    / "meta_2024_income_statement_candidates"
)

TABLE_FILES = {
    7: CANDIDATES_DIR / "table_0007.csv",
    8: CANDIDATES_DIR / "table_0008.csv",
    23: CANDIDATES_DIR / "table_0023.csv",
}

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "meta_2024_income_candidate_profile.csv"
)

TARGET_LABELS = [
    "revenue",
    "income from operations",
    "net income",
]

EXPECTED_YEARS = {
    2024,
    2023,
    2022,
}


def normalize_text(value: object) -> str:
    text = str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip().lower()


def parse_number(value: object) -> float | None:
    text = str(value).strip()

    if normalize_text(text) in {
        "",
        "nan",
        "none",
        "$",
        "-",
        "—",
        "–",
    }:
        return None

    negative = (
        text.startswith("(")
        and text.endswith(")")
    )

    cleaned = (
        text.replace("$", "")
        .replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("%", "")
        .strip()
    )

    if not re.fullmatch(
        r"-?\d+(?:\.\d+)?",
        cleaned,
    ):
        return None

    number = float(cleaned)

    if negative:
        number = -number

    return number


def load_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"קובץ הטבלה לא נמצא:\n{path}"
        )

    return pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
    )


def build_row_text(row: pd.Series) -> str:
    values = []

    for value in row.tolist():
        normalized = normalize_text(value)

        if normalized not in {
            "",
            "nan",
            "none",
        }:
            values.append(normalized)

    return " ".join(values)


def find_target_rows(
    table: pd.DataFrame,
    target_label: str,
) -> list[pd.Series]:
    matches = []

    for _, row in table.iterrows():
        row_text = build_row_text(row)

        if (
            row_text == target_label
            or row_text.startswith(
                target_label + " "
            )
        ):
            matches.append(row)

    return matches


def extract_numbers(
    row: pd.Series,
) -> list[float]:
    values = []

    for cell in row.tolist():
        number = parse_number(cell)

        if number is None:
            continue

        if (
            float(number).is_integer()
            and int(number) in EXPECTED_YEARS
        ):
            continue

        values.append(number)

    return values


def count_adjacent_duplicates(
    values: list[float],
) -> int:
    duplicate_count = 0

    for index in range(
        1,
        len(values),
    ):
        if values[index] == values[index - 1]:
            duplicate_count += 1

    return duplicate_count


def format_values(
    values: list[float],
) -> str:
    return " | ".join(
        f"{value:,.3f}"
        for value in values
    )


def build_profile() -> pd.DataFrame:
    records = []

    for table_number, table_file in TABLE_FILES.items():
        table = load_table(table_file)

        for target_label in TARGET_LABELS:
            matching_rows = find_target_rows(
                table=table,
                target_label=target_label,
            )

            if len(matching_rows) == 0:
                records.append(
                    {
                        "table_number": table_number,
                        "metric": target_label,
                        "matching_row_count": 0,
                        "numeric_value_count": 0,
                        "distinct_numeric_count": 0,
                        "adjacent_duplicate_count": 0,
                        "minimum_absolute_value": None,
                        "maximum_absolute_value": None,
                        "median_absolute_value": None,
                        "numeric_values": "",
                        "full_row_text": "",
                        "table_rows": len(table),
                        "table_columns": len(
                            table.columns
                        ),
                        "source_file": str(
                            table_file
                        ),
                    }
                )

                continue

            for match_number, row in enumerate(
                matching_rows,
                start=1,
            ):
                values = extract_numbers(row)

                absolute_values = [
                    abs(value)
                    for value in values
                ]

                if absolute_values:
                    minimum_absolute_value = min(
                        absolute_values
                    )

                    maximum_absolute_value = max(
                        absolute_values
                    )

                    median_absolute_value = float(
                        pd.Series(
                            absolute_values
                        ).median()
                    )
                else:
                    minimum_absolute_value = None
                    maximum_absolute_value = None
                    median_absolute_value = None

                records.append(
                    {
                        "table_number": table_number,
                        "metric": target_label,
                        "match_number": match_number,
                        "matching_row_count": len(
                            matching_rows
                        ),
                        "numeric_value_count": len(
                            values
                        ),
                        "distinct_numeric_count": len(
                            set(values)
                        ),
                        "adjacent_duplicate_count":
                            count_adjacent_duplicates(
                                values
                            ),
                        "minimum_absolute_value":
                            minimum_absolute_value,
                        "maximum_absolute_value":
                            maximum_absolute_value,
                        "median_absolute_value":
                            median_absolute_value,
                        "numeric_values":
                            format_values(values),
                        "full_row_text": " | ".join(
                            str(value).strip()
                            for value in row.tolist()
                            if str(value).strip()
                        ),
                        "table_rows": len(table),
                        "table_columns": len(
                            table.columns
                        ),
                        "source_file": str(
                            table_file
                        ),
                    }
                )

    return pd.DataFrame(records)


def print_table_summary(
    profile_df: pd.DataFrame,
) -> None:
    print()
    print("=" * 145)
    print(
        "Meta 2024 — פרופיל אובייקטיבי "
        "של טבלאות דוח רווח והפסד"
    )
    print("=" * 145)

    display_columns = [
        "table_number",
        "metric",
        "matching_row_count",
        "numeric_value_count",
        "distinct_numeric_count",
        "adjacent_duplicate_count",
        "minimum_absolute_value",
        "maximum_absolute_value",
        "median_absolute_value",
        "numeric_values",
    ]

    print(
        profile_df[
            display_columns
        ].to_string(
            index=False
        )
    )


def print_table_level_summary(
    profile_df: pd.DataFrame,
) -> None:
    print()
    print("=" * 145)
    print("סיכום לפי טבלה")
    print("=" * 145)

    for table_number in sorted(
        profile_df["table_number"].unique()
    ):
        table_df = profile_df[
            profile_df["table_number"]
            == table_number
        ]

        metrics_found_once = int(
            (
                table_df["matching_row_count"]
                == 1
            ).sum()
        )

        total_numeric_values = int(
            table_df[
                "numeric_value_count"
            ].sum()
        )

        maximum_values = (
            table_df[
                "maximum_absolute_value"
            ]
            .dropna()
            .tolist()
        )

        print()
        print(f"טבלה {table_number}")
        print(
            "  מספר מדדים שנמצאו פעם אחת: "
            f"{metrics_found_once} מתוך "
            f"{len(TARGET_LABELS)}"
        )
        print(
            "  סך הערכים המספריים: "
            f"{total_numeric_values}"
        )

        if maximum_values:
            print(
                "  הערך המוחלט הגדול ביותר: "
                f"{max(maximum_values):,.3f}"
            )
        else:
            print(
                "  לא נמצאו ערכים מספריים."
            )


def main() -> None:
    profile_df = build_profile()

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    profile_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print_table_summary(profile_df)
    print_table_level_summary(profile_df)

    print()
    print("=" * 145)
    print(
        "בשלב זה לא נבחרה טבלה."
    )
    print(
        "הסקריפט רק מציג את מבנה הערכים "
        "בכל מועמד כדי שנוכל לקבוע כלל "
        "מדויק על בסיס הנתונים בפועל."
    )

    print()
    print(
        f"קובץ התוצאה נשמר כאן:\n"
        f"{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()