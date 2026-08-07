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

OUTPUT_DIR = (
    PROJECT_DIR
    / "data"
    / "orcl_2020_income_table_candidates"
)

TARGET_REPORT_DATE = pd.Timestamp("2020-05-31")

SEARCH_TERMS = [
    "operating income",
    "income from operations",
    "income before income taxes",
    "income before provision for income taxes",
    "provision for income taxes",
    "income tax provision",
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
                if normalize_text(part) not in {"", "nan"}
            )
            for column in result.columns
        ]
    else:
        result.columns = [
            str(column)
            for column in result.columns
        ]

    return result


def table_to_text(df: pd.DataFrame) -> str:
    parts = []

    for column in df.columns:
        parts.append(normalize_text(column))

    for value in df.astype(str).to_numpy().ravel():
        parts.append(normalize_text(value))

    return " ".join(parts)


def main() -> None:
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המיפוי לא נמצא:\n{MANIFEST_FILE}"
        )

    manifest_df = pd.read_csv(MANIFEST_FILE)

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
            "ציפינו לדוח Oracle אחד לשנת 2020, "
            f"אך נמצאו {len(selected_rows)}."
        )

    html_file = Path(
        str(
            selected_rows.iloc[0][
                "local_document_file"
            ]
        )
    )

    if not html_file.exists():
        raise FileNotFoundError(
            f"קובץ דוח 2020 לא נמצא:\n{html_file}"
        )

    print("=" * 80)
    print("Oracle 2020 income-table inspection")
    print("=" * 80)
    print(f"קובץ מקור:\n{html_file}")

    tables = pd.read_html(
        html_file,
        flavor="lxml",
    )

    print()
    print(f"מספר הטבלאות בדוח: {len(tables)}")

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    summary_rows = []

    for table_index, raw_table in enumerate(tables):
        table = flatten_columns(raw_table)

        searchable_text = table_to_text(
            table
        )

        matched_terms = [
            term
            for term in SEARCH_TERMS
            if term in searchable_text
        ]

        if not matched_terms:
            continue

        output_file = (
            OUTPUT_DIR
            / f"table_{table_index:04d}.csv"
        )

        table.to_csv(
            output_file,
            index=False,
            encoding="utf-8-sig",
        )

        summary_rows.append(
            {
                "table_index": table_index,
                "matched_terms": " | ".join(
                    matched_terms
                ),
                "row_count": len(table),
                "column_count": len(table.columns),
                "output_file": str(output_file),
            }
        )

    summary_df = pd.DataFrame(
        summary_rows
    )

    if summary_df.empty:
        raise RuntimeError(
            "לא נמצאה אף טבלה שמכילה אחד "
            "ממונחי החיפוש. לא מבצעים חישוב."
        )

    summary_file = (
        OUTPUT_DIR
        / "summary.csv"
    )

    summary_df.to_csv(
        summary_file,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 120)
    print("טבלאות מועמדות מדוח Oracle 2020")
    print("=" * 120)

    print(
        summary_df.to_string(
            index=False
        )
    )

    print()
    print(
        "הטבלאות נשמרו כאן:\n"
        f"{OUTPUT_DIR}"
    )

    print()
    print(
        "קובץ הסיכום נשמר כאן:\n"
        f"{summary_file}"
    )

    print()
    print(
        "לא בוצע חישוב NOPAT. "
        "נשמרו רק טבלאות מקור לביקורת."
    )


if __name__ == "__main__":
    main()