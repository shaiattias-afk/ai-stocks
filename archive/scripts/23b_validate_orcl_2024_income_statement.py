from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

MANIFEST_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_10k_filings_manifest.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_2024_income_statement_validation.csv"
)

TARGET_REPORT_DATE = pd.Timestamp("2024-05-31")

REQUIRED_LABELS = [
    "operating income",
    "income before income taxes",
    "provision for income taxes",
]


def normalize_text(value: object) -> str:
    text = str(value)

    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip().lower()


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()

    if isinstance(result.columns, pd.MultiIndex):
        result.columns = [
            " | ".join(
                str(part)
                for part in column
                if str(part).lower() != "nan"
            )
            for column in result.columns
        ]
    else:
        result.columns = [
            str(column)
            for column in result.columns
        ]

    return result


def table_to_searchable_text(df: pd.DataFrame) -> str:
    values = []

    for column in df.columns:
        values.append(normalize_text(column))

    for value in df.astype(str).to_numpy().ravel():
        values.append(normalize_text(value))

    return " ".join(values)


def main() -> None:
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המיפוי לא נמצא:\n{MANIFEST_FILE}"
        )

    manifest_df = pd.read_csv(MANIFEST_FILE)

    required_manifest_columns = {
        "report_date",
        "filing_date",
        "local_document_file",
        "date_rule_passed",
    }

    missing_columns = (
        required_manifest_columns
        - set(manifest_df.columns)
    )

    if missing_columns:
        raise RuntimeError(
            "בקובץ המיפוי חסרות עמודות:\n"
            f"{sorted(missing_columns)}"
        )

    manifest_df["report_date"] = pd.to_datetime(
        manifest_df["report_date"],
        errors="coerce",
    )

    selected_rows = manifest_df[
        manifest_df["report_date"]
        == TARGET_REPORT_DATE
    ]

    if len(selected_rows) != 1:
        raise RuntimeError(
            "ציפינו לדוח Oracle אחד בלבד "
            "ל-31 במאי 2024, אך נמצאו "
            f"{len(selected_rows)}."
        )

    selected_manifest_row = selected_rows.iloc[0]

    html_file = Path(
        str(
            selected_manifest_row[
                "local_document_file"
            ]
        )
    )

    if not html_file.exists():
        raise FileNotFoundError(
            f"קובץ ה-10-K לא נמצא:\n{html_file}"
        )

    print("=" * 80)
    print("Oracle 2024 income-statement validation")
    print("=" * 80)
    print(f"קובץ מקור:\n{html_file}")

    tables = pd.read_html(
        html_file,
        flavor="lxml",
    )

    print(f"\nמספר הטבלאות שנמצאו: {len(tables)}")

    exact_candidates = []

    for table_index, raw_table in enumerate(tables):
        table = flatten_columns(raw_table)

        searchable_text = table_to_searchable_text(
            table
        )

        all_labels_found = all(
            label in searchable_text
            for label in REQUIRED_LABELS
        )

        if all_labels_found:
            exact_candidates.append(
                {
                    "table_index": table_index,
                    "table": table,
                }
            )

    if len(exact_candidates) != 1:
        raise RuntimeError(
            "לא נמצאה בדיוק טבלה אחת שמכילה "
            "את כל שלוש השורות המדויקות.\n"
            f"מספר הטבלאות שנמצאו: "
            f"{len(exact_candidates)}.\n"
            "לא ממשיכים לחישוב עד לבדיקת הטבלה."
        )

    selected_table_index = exact_candidates[0][
        "table_index"
    ]

    selected_table = exact_candidates[0][
        "table"
    ].copy()

    selected_table.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print(
        "נבחרה טבלה מספר:",
        selected_table_index,
    )

    print()
    print("הטבלה שנבחרה:")
    print(
        selected_table.to_string(
            index=False
        )
    )

    print()
    print(
        "הטבלה נשמרה לביקורת כאן:\n"
        f"{OUTPUT_FILE}"
    )

    normalized_rows = (
        selected_table
        .astype(str)
        .apply(
            lambda row: " ".join(
                normalize_text(value)
                for value in row
            ),
            axis=1,
        )
    )

    for required_label in REQUIRED_LABELS:
        matching_rows = normalized_rows[
            normalized_rows.str.contains(
                required_label,
                regex=False,
            )
        ]

        if len(matching_rows) != 1:
            raise RuntimeError(
                f"השורה '{required_label}' "
                "לא נמצאה בדיוק פעם אחת.\n"
                f"מספר התאמות: {len(matching_rows)}"
            )

    print()
    print(
        "הבדיקה עברה: נמצאה טבלה אחת בלבד "
        "ובה שלוש השורות המדויקות."
    )

    print(
        "עדיין לא חושב NOPAT. "
        "השלב הבא יהיה חילוץ מספרים רק מהטבלה המאומתת."
    )


if __name__ == "__main__":
    main()