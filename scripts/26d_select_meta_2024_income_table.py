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
    / "meta_2024_selected_income_statement.csv"
)

VALIDATION_FILE = (
    PROJECT_DIR
    / "data"
    / "meta_2024_income_statement_selection_validation.csv"
)

TARGET_LABELS = [
    "revenue",
    "income from operations",
    "net income",
]

EXPECTED_YEARS = [
    2024,
    2023,
    2022,
]


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


def find_target_row(
    table: pd.DataFrame,
    target_label: str,
) -> pd.Series:
    matches = []

    for row_index, row in table.iterrows():
        row_text = build_row_text(row)

        if (
            row_text == target_label
            or row_text.startswith(
                target_label + " "
            )
        ):
            matches.append(row_index)

    if len(matches) != 1:
        raise RuntimeError(
            f"השורה '{target_label}' נמצאה "
            f"{len(matches)} פעמים במקום פעם אחת."
        )

    return table.loc[matches[0]]


def extract_metric_values(
    row: pd.Series,
) -> list[float]:
    numbers = []

    for value in row.tolist():
        parsed = parse_number(value)

        if parsed is None:
            continue

        if (
            float(parsed).is_integer()
            and int(parsed) in EXPECTED_YEARS
        ):
            continue

        numbers.append(parsed)

    return numbers


def compare_tables() -> pd.DataFrame:
    records = []

    for table_number, table_file in TABLE_FILES.items():
        table = load_table(table_file)

        for target_label in TARGET_LABELS:
            row = find_target_row(
                table,
                target_label,
            )

            values = extract_metric_values(row)

            records.append(
                {
                    "table_number": table_number,
                    "metric": target_label,
                    "value_count": len(values),
                    "values": values,
                    "values_text": " | ".join(
                        f"{value:,.3f}"
                        for value in values
                    ),
                    "source_file": str(table_file),
                }
            )

    return pd.DataFrame(records)


def validate_candidate_pair(
    comparison_df: pd.DataFrame,
    first_table: int,
    second_table: int,
) -> tuple[bool, list[str]]:
    problems = []

    for metric in TARGET_LABELS:
        first_row = comparison_df[
            (
                comparison_df["table_number"]
                == first_table
            )
            & (
                comparison_df["metric"]
                == metric
            )
        ]

        second_row = comparison_df[
            (
                comparison_df["table_number"]
                == second_table
            )
            & (
                comparison_df["metric"]
                == metric
            )
        ]

        if len(first_row) != 1:
            problems.append(
                f"טבלה {first_table}: "
                f"לא נמצאה תוצאה יחידה עבור {metric}"
            )
            continue

        if len(second_row) != 1:
            problems.append(
                f"טבלה {second_table}: "
                f"לא נמצאה תוצאה יחידה עבור {metric}"
            )
            continue

        first_values = first_row.iloc[0]["values"]
        second_values = second_row.iloc[0]["values"]

        if first_values != second_values:
            problems.append(
                f"אי־התאמה במדד '{metric}': "
                f"טבלה {first_table} = {first_values}, "
                f"טבלה {second_table} = {second_values}"
            )

    return len(problems) == 0, problems


def save_validation(
    comparison_df: pd.DataFrame,
) -> None:
    output_df = comparison_df.copy()

    output_df = output_df[
        [
            "table_number",
            "metric",
            "value_count",
            "values_text",
            "source_file",
        ]
    ]

    output_df.to_csv(
        VALIDATION_FILE,
        index=False,
        encoding="utf-8-sig",
    )


def main() -> None:
    comparison_df = compare_tables()

    save_validation(comparison_df)

    print()
    print("=" * 110)
    print("Meta 2024 — בחירת טבלת דוח רווח והפסד")
    print("=" * 110)

    print(
        comparison_df[
            [
                "table_number",
                "metric",
                "value_count",
                "values_text",
            ]
        ].to_string(index=False)
    )

    print()

    tables_match, problems = validate_candidate_pair(
        comparison_df=comparison_df,
        first_table=7,
        second_table=8,
    )

    if not tables_match:
        print("לא נבחרה טבלה.")
        print()
        print(
            "טבלאות 7 ו־8 אינן זהות "
            "בשורות החשבונאיות המרכזיות:"
        )

        for problem in problems:
            print(f"- {problem}")

        print()
        print(
            "הסקריפט נעצר כדי למנוע בחירה "
            "המבוססת על ניחוש."
        )

        print()
        print(
            f"קובץ הבדיקה נשמר כאן:\n"
            f"{VALIDATION_FILE}"
        )

        return

    selected_table_number = 7
    selected_table_file = TABLE_FILES[
        selected_table_number
    ]

    selected_table = load_table(
        selected_table_file
    )

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    selected_table.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print(
        "טבלאות 7 ו־8 מציגות ערכים זהים "
        "עבור:"
    )

    for metric in TARGET_LABELS:
        print(f"- {metric}")

    print()
    print(
        f"נבחרה טבלה {selected_table_number}."
    )

    print()
    print(
        f"הטבלה נשמרה כאן:\n{OUTPUT_FILE}"
    )

    print()
    print(
        f"קובץ הבדיקה נשמר כאן:\n"
        f"{VALIDATION_FILE}"
    )


if __name__ == "__main__":
    main()