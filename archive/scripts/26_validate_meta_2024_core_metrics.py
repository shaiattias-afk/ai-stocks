from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


# ============================================================
# מיקומי קבצים
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

MANIFEST_FILE = (
    PROJECT_DIR
    / "data"
    / "meta_10k_filings_manifest.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "meta_2024_core_metrics_validated.csv"
)

TARGET_REPORT_DATE = pd.Timestamp("2024-12-31")


# ============================================================
# שמות שורות מדויקים מתוך ה-10-K
# ============================================================

REVENUE_LABEL = "revenue"

OPERATING_CASH_FLOW_LABEL = (
    "net cash provided by operating activities"
)

CAPEX_LABEL = (
    "purchases of property and equipment"
)


# ============================================================
# פונקציות עזר
# ============================================================

def normalize_text(value: object) -> str:
    text = str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip().lower()


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
            "נמצא ערך לא תקין בעמודת True/False."
        )

    return normalized.astype(bool)


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()

    if isinstance(result.columns, pd.MultiIndex):
        result.columns = [
            " | ".join(
                str(part)
                for part in column
                if normalize_text(part)
                not in {"", "nan"}
            )
            for column in result.columns
        ]
    else:
        result.columns = [
            str(column)
            for column in result.columns
        ]

    return result


def parse_financial_number(
    value: object,
) -> float | None:
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

    negative_parentheses = (
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

    if negative_parentheses:
        number = -number

    return number


def table_row_texts(
    table: pd.DataFrame,
) -> pd.Series:
    return table.apply(
        lambda row: " ".join(
            normalize_text(value)
            for value in row
            if normalize_text(value)
            not in {"", "nan", "none"}
        ),
        axis=1,
    )


def table_contains_exact_row(
    table: pd.DataFrame,
    exact_label: str,
) -> bool:
    row_texts = table_row_texts(table)

    return any(
        row_text == exact_label
        or row_text.startswith(
            exact_label + " "
        )
        for row_text in row_texts
    )


def find_single_table(
    tables: list[pd.DataFrame],
    required_labels: list[str],
    description: str,
) -> pd.DataFrame:
    candidates = []

    for table_index, raw_table in enumerate(
        tables
    ):
        table = flatten_columns(raw_table)

        if all(
            table_contains_exact_row(
                table,
                label,
            )
            for label in required_labels
        ):
            candidates.append(
                {
                    "table_index": table_index,
                    "table": table,
                }
            )

    if len(candidates) != 1:
        raise RuntimeError(
            f"עבור {description} ציפינו לטבלה אחת "
            "בדיוק שמכילה את השורות הנדרשות.\n"
            f"מספר טבלאות שנמצאו: {len(candidates)}"
        )

    selected = candidates[0]

    print()
    print(
        f"טבלת {description}: "
        f"{selected['table_index']}"
    )

    return selected["table"]


def find_exact_row(
    table: pd.DataFrame,
    exact_label: str,
) -> pd.Series:
    row_texts = table_row_texts(table)

    matching_indices = [
        index
        for index, row_text in row_texts.items()
        if (
            row_text == exact_label
            or row_text.startswith(
                exact_label + " "
            )
        )
    ]

    if len(matching_indices) != 1:
        raise RuntimeError(
            f"השורה '{exact_label}' לא נמצאה "
            "בדיוק פעם אחת.\n"
            f"מספר התאמות: {len(matching_indices)}"
        )

    return table.loc[
        matching_indices[0]
    ]


def extract_three_annual_values(
    row: pd.Series,
    exact_label: str,
) -> list[float]:
    values = []

    for cell_value in row.tolist():
        number = parse_financial_number(
            cell_value
        )

        if number is not None:
            values.append(number)

    # מסירים שנות כותרת, אם הן הוטמעו בשורה.
    values = [
        value
        for value in values
        if not (
            float(value).is_integer()
            and int(value)
            in {
                2024,
                2023,
                2022,
            }
        )
    ]

    if len(values) != 3:
        raise RuntimeError(
            f"בשורה '{exact_label}' ציפינו "
            "לשלושה ערכים שנתיים בדיוק.\n"
            f"נמצאו: {values}"
        )

    return values


# ============================================================
# Main
# ============================================================

def main() -> None:
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המיפוי לא נמצא:\n"
            f"{MANIFEST_FILE}"
        )

    manifest_df = pd.read_csv(
        MANIFEST_FILE
    )

    required_manifest_columns = {
        "as_of_date",
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

    for column in [
        "as_of_date",
        "report_date",
        "filing_date",
    ]:
        manifest_df[column] = pd.to_datetime(
            manifest_df[column],
            errors="coerce",
        )

    manifest_df["date_rule_passed"] = (
        normalize_boolean(
            manifest_df["date_rule_passed"]
        )
    )

    selected_rows = manifest_df[
        manifest_df["report_date"]
        == TARGET_REPORT_DATE
    ].copy()

    if len(selected_rows) != 1:
        raise RuntimeError(
            "ציפינו לדוח Meta אחד בלבד "
            "ל-31 בדצמבר 2024.\n"
            f"נמצאו: {len(selected_rows)}"
        )

    manifest_row = selected_rows.iloc[0]

    if not bool(
        manifest_row["date_rule_passed"]
    ):
        raise RuntimeError(
            "דוח 2024 נכשל בכלל התאריכים."
        )

    html_file = Path(
        str(
            manifest_row[
                "local_document_file"
            ]
        )
    )

    if not html_file.exists():
        raise FileNotFoundError(
            f"קובץ ה-10-K לא נמצא:\n"
            f"{html_file}"
        )

    print("=" * 90)
    print("Meta 2024 core-metrics validation")
    print("=" * 90)
    print(f"קובץ מקור:\n{html_file}")

    tables = pd.read_html(
        html_file,
        flavor="lxml",
    )

    print()
    print(
        "מספר הטבלאות בדוח:",
        len(tables),
    )

    # טבלת רווח והפסד:
    # Revenue יחד עם Income from operations
    # כדי לא לבחור טבלת הכנסות משנית.
    income_statement = find_single_table(
        tables,
        [
            REVENUE_LABEL,
            "income from operations",
            "net income",
        ],
        "Consolidated Statements of Income",
    )

    # טבלת תזרים מזומנים:
    cash_flow_statement = find_single_table(
        tables,
        [
            OPERATING_CASH_FLOW_LABEL,
            CAPEX_LABEL,
        ],
        "Consolidated Statements of Cash Flows",
    )

    revenue_row = find_exact_row(
        income_statement,
        REVENUE_LABEL,
    )

    operating_cash_flow_row = find_exact_row(
        cash_flow_statement,
        OPERATING_CASH_FLOW_LABEL,
    )

    capex_row = find_exact_row(
        cash_flow_statement,
        CAPEX_LABEL,
    )

    revenue_values = extract_three_annual_values(
        revenue_row,
        REVENUE_LABEL,
    )

    operating_cash_flow_values = (
        extract_three_annual_values(
            operating_cash_flow_row,
            OPERATING_CASH_FLOW_LABEL,
        )
    )

    capex_values = extract_three_annual_values(
        capex_row,
        CAPEX_LABEL,
    )

    # העמודה הראשונה היא שנת 2024.
    revenue_2024 = float(
        revenue_values[0]
    )

    operating_cash_flow_2024 = float(
        operating_cash_flow_values[0]
    )

    reported_capex_2024 = float(
        capex_values[0]
    )

    # בדוח תזרים מזומנים Capex מוצג בדרך כלל
    # כמספר שלילי משום שמדובר ביציאת מזומן.
    if reported_capex_2024 >= 0:
        raise RuntimeError(
            "Capex אינו מוצג כיציאת מזומן שלילית. "
            "נדרשת בדיקת מקור לפני חישוב."
        )

    capex_2024 = abs(
        reported_capex_2024
    )

    if revenue_2024 <= 0:
        raise RuntimeError(
            "Revenue אינו חיובי."
        )

    if operating_cash_flow_2024 <= 0:
        raise RuntimeError(
            "Operating Cash Flow אינו חיובי."
        )

    # ההגדרה האחידה של הפרויקט:
    # FCF = Operating Cash Flow - Capex
    fcf_2024 = (
        operating_cash_flow_2024
        - capex_2024
    )

    fcf_margin_2024 = (
        fcf_2024
        / revenue_2024
    )

    date_rule_passed = (
        manifest_row["filing_date"]
        <= manifest_row["as_of_date"]
        and bool(
            manifest_row[
                "date_rule_passed"
            ]
        )
    )

    result_df = pd.DataFrame(
        [
            {
                "ticker": "META",
                "company_name": (
                    "Meta Platforms, Inc."
                ),
                "as_of_date": (
                    manifest_row[
                        "as_of_date"
                    ].date()
                ),
                "report_date": (
                    manifest_row[
                        "report_date"
                    ].date()
                ),
                "filing_date": (
                    manifest_row[
                        "filing_date"
                    ].date()
                ),
                "revenue_usd_millions": (
                    revenue_2024
                ),
                "operating_cash_flow_usd_millions": (
                    operating_cash_flow_2024
                ),
                "reported_capex_usd_millions": (
                    reported_capex_2024
                ),
                "capex_usd_millions": (
                    capex_2024
                ),
                "fcf_usd_millions": (
                    fcf_2024
                ),
                "fcf_margin": (
                    fcf_margin_2024
                ),
                "fcf_margin_percent": (
                    fcf_margin_2024
                    * 100
                ),
                "revenue_row_label": (
                    REVENUE_LABEL
                ),
                "operating_cash_flow_row_label": (
                    OPERATING_CASH_FLOW_LABEL
                ),
                "capex_row_label": (
                    CAPEX_LABEL
                ),
                "source_file": str(
                    html_file
                ),
                "date_rule_passed": (
                    date_rule_passed
                ),
                "validation_passed": True,
            }
        ]
    )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 90)
    print("Meta 2024 validated core metrics")
    print("=" * 90)

    print(
        f"Revenue:             "
        f"{revenue_2024:,.0f} "
        "מיליון דולר"
    )

    print(
        f"Operating Cash Flow: "
        f"{operating_cash_flow_2024:,.0f} "
        "מיליון דולר"
    )

    print(
        f"Capex:               "
        f"{capex_2024:,.0f} "
        "מיליון דולר"
    )

    print(
        f"FCF:                 "
        f"{fcf_2024:,.0f} "
        "מיליון דולר"
    )

    print(
        f"FCF Margin:          "
        f"{fcf_margin_2024:.3%}"
    )

    print()
    print(
        f"התוצאה נשמרה כאן:\n"
        f"{OUTPUT_FILE}"
    )

    if not date_rule_passed:
        raise RuntimeError(
            "דוח המקור הפר את כלל התאריכים."
        )

    print()
    print(
        "הבדיקה עברה: נתוני הליבה של Meta 2024 "
        "חולצו ישירות מטבלאות ה-10-K הרשמי."
    )


if __name__ == "__main__":
    main()