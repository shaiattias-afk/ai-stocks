from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

MANIFEST_FILE = (
    PROJECT_DIR
    / "data"
    / "meta_10k_filings_manifest.csv"
)

OUTPUT_DIR = (
    PROJECT_DIR
    / "data"
    / "meta_2024_income_statement_candidates"
)

TARGET_REPORT_DATE = pd.Timestamp("2024-12-31")

REQUIRED_LABELS = [
    "revenue",
    "income from operations",
    "net income",
]


def normalize_text(value: object) -> str:
    text = str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip().lower()


def normalize_boolean(
    series: pd.Series,
) -> pd.Series:
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
            "נמצא ערך לא תקין בעמודת date_rule_passed."
        )

    return normalized.astype(bool)


def flatten_columns(
    table: pd.DataFrame,
) -> pd.DataFrame:
    result = table.copy()

    if isinstance(result.columns, pd.MultiIndex):
        result.columns = [
            " | ".join(
                str(part)
                for part in column
                if normalize_text(part)
                not in {
                    "",
                    "nan",
                }
            )
            for column in result.columns
        ]
    else:
        result.columns = [
            str(column)
            for column in result.columns
        ]

    return result


def build_row_texts(
    table: pd.DataFrame,
) -> pd.Series:
    return table.apply(
        lambda row: " ".join(
            normalize_text(value)
            for value in row
            if normalize_text(value)
            not in {
                "",
                "nan",
                "none",
            }
        ),
        axis=1,
    )


def table_contains_label(
    table: pd.DataFrame,
    label: str,
) -> bool:
    row_texts = build_row_texts(
        table
    )

    return any(
        row_text == label
        or row_text.startswith(
            label + " "
        )
        for row_text in row_texts
    )


def read_meta_2024_file() -> Path:
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המיפוי לא נמצא:\n{MANIFEST_FILE}"
        )

    manifest_df = pd.read_csv(
        MANIFEST_FILE
    )

    required_columns = {
        "report_date",
        "local_document_file",
        "date_rule_passed",
    }

    missing_columns = (
        required_columns
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

    manifest_df["date_rule_passed"] = (
        normalize_boolean(
            manifest_df["date_rule_passed"]
        )
    )

    selected = manifest_df[
        manifest_df["report_date"]
        == TARGET_REPORT_DATE
    ].copy()

    if len(selected) != 1:
        raise RuntimeError(
            "ציפינו לדוח Meta אחד בלבד "
            "ל-31 בדצמבר 2024.\n"
            f"נמצאו: {len(selected)}"
        )

    selected_row = selected.iloc[0]

    if not bool(
        selected_row["date_rule_passed"]
    ):
        raise RuntimeError(
            "דוח Meta 2024 נכשל בכלל התאריכים."
        )

    html_file = Path(
        str(
            selected_row[
                "local_document_file"
            ]
        )
    )

    if not html_file.exists():
        raise FileNotFoundError(
            f"קובץ ה-10-K לא נמצא:\n{html_file}"
        )

    return html_file


def main() -> None:
    html_file = read_meta_2024_file()

    print("=" * 90)
    print("Meta 2024 income-statement candidates")
    print("=" * 90)
    print(f"קובץ מקור:\n{html_file}")

    tables = pd.read_html(
        html_file,
        flavor="lxml",
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    summary_rows = []

    for table_index, raw_table in enumerate(
        tables
    ):
        table = flatten_columns(
            raw_table
        )

        matched_labels = [
            label
            for label in REQUIRED_LABELS
            if table_contains_label(
                table,
                label,
            )
        ]

        if len(matched_labels) != len(
            REQUIRED_LABELS
        ):
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

        row_texts = build_row_texts(
            table
        )

        relevant_rows = row_texts[
            row_texts.apply(
                lambda text: any(
                    text == label
                    or text.startswith(
                        label + " "
                    )
                    for label in REQUIRED_LABELS
                )
            )
        ]

        summary_rows.append(
            {
                "table_index": table_index,
                "row_count": len(table),
                "column_count": len(
                    table.columns
                ),
                "matched_labels": " | ".join(
                    matched_labels
                ),
                "relevant_rows": " || ".join(
                    relevant_rows.tolist()
                ),
                "output_file": str(
                    output_file
                ),
            }
        )

    summary_df = pd.DataFrame(
        summary_rows
    )

    if summary_df.empty:
        raise RuntimeError(
            "לא נמצאו טבלאות מועמדות."
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
    print("=" * 145)
    print("טבלאות מועמדות")
    print("=" * 145)

    print(
        summary_df[
            [
                "table_index",
                "row_count",
                "column_count",
                "relevant_rows",
                "output_file",
            ]
        ].to_string(
            index=False
        )
    )

    print()
    print(
        "מספר טבלאות מועמדות:",
        len(summary_df),
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
        "לא חושבו נתונים. נשמרו רק שלוש "
        "טבלאות המקור לצורך זיהוי חד-משמעי."
    )


if __name__ == "__main__":
    main()