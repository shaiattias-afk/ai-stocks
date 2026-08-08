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
    / "orcl_nopat_5_years_validated.csv"
)

REQUIRED_LABELS = {
    "operating_income": "operating income",
    "pretax_income": "income before income taxes",
    "tax_expense": "provision for income taxes",
}


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
            "נמצא ערך לא תקין בעמודת date_rule_passed."
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
                not in {"nan", ""}
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
    parts = []

    for column in df.columns:
        parts.append(normalize_text(column))

    for value in df.astype(str).to_numpy().ravel():
        parts.append(normalize_text(value))

    return " ".join(parts)


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


def read_manifest() -> pd.DataFrame:
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המיפוי לא נמצא:\n{MANIFEST_FILE}"
        )

    df = pd.read_csv(MANIFEST_FILE)

    required_columns = {
        "as_of_date",
        "report_date",
        "filing_date",
        "local_document_file",
        "date_rule_passed",
    }

    missing = required_columns - set(df.columns)

    if missing:
        raise RuntimeError(
            "בקובץ המיפוי חסרות עמודות:\n"
            f"{sorted(missing)}"
        )

    for column in [
        "as_of_date",
        "report_date",
        "filing_date",
    ]:
        df[column] = pd.to_datetime(
            df[column],
            errors="coerce",
        )

    df["date_rule_passed"] = normalize_boolean(
        df["date_rule_passed"]
    )

    if df[
        [
            "as_of_date",
            "report_date",
            "filing_date",
        ]
    ].isna().any().any():
        raise RuntimeError(
            "בקובץ המיפוי נמצאו תאריכים לא תקינים."
        )

    return df


def find_single_income_statement(
    html_file: Path,
) -> pd.DataFrame:
    if not html_file.exists():
        raise FileNotFoundError(
            f"קובץ 10-K לא נמצא:\n{html_file}"
        )

    tables = pd.read_html(
        html_file,
        flavor="lxml",
    )

    candidates = []

    for table_index, raw_table in enumerate(tables):
        table = flatten_columns(raw_table)

        searchable_text = table_to_searchable_text(
            table
        )

        if all(
            label in searchable_text
            for label in REQUIRED_LABELS.values()
        ):
            candidates.append(
                {
                    "table_index": table_index,
                    "table": table,
                }
            )

    if len(candidates) != 1:
        raise RuntimeError(
            "לא נמצאה בדיוק טבלת דוח רווח והפסד אחת "
            "עם שלוש השורות המדויקות.\n"
            f"קובץ: {html_file}\n"
            f"מספר מועמדות: {len(candidates)}"
        )

    return candidates[0]["table"]


def find_exact_row(
    table: pd.DataFrame,
    required_label: str,
) -> pd.Series:
    row_texts = table.apply(
        lambda row: " ".join(
            normalize_text(value)
            for value in row
            if normalize_text(value)
            not in {"", "nan", "none"}
        ),
        axis=1,
    )

    matching_indices = row_texts[
        row_texts.str.contains(
            required_label,
            regex=False,
        )
    ].index.tolist()

    if len(matching_indices) != 1:
        raise RuntimeError(
            f"השורה '{required_label}' לא נמצאה "
            "בדיוק פעם אחת.\n"
            f"מספר התאמות: {len(matching_indices)}"
        )

    return table.loc[matching_indices[0]]


def extract_three_year_values(
    row: pd.Series,
    required_label: str,
    report_year: int,
) -> list[float]:
    values = []

    for cell_value in row.tolist():
        parsed = parse_financial_number(
            cell_value
        )

        if parsed is not None:
            values.append(parsed)

    possible_years = {
        report_year,
        report_year - 1,
        report_year - 2,
    }

    values = [
        value
        for value in values
        if not (
            float(value).is_integer()
            and int(value) in possible_years
        )
    ]

    if len(values) != 3:
        raise RuntimeError(
            f"בשורה '{required_label}' ציפינו "
            "לשלושה ערכים שנתיים בדיוק.\n"
            f"נמצאו: {values}"
        )

    return values


def extract_one_filing(
    html_file: Path,
    report_year: int,
) -> dict:
    table = find_single_income_statement(
        html_file
    )

    extracted = {}

    for field_name, required_label in (
        REQUIRED_LABELS.items()
    ):
        row = find_exact_row(
            table,
            required_label,
        )

        values = extract_three_year_values(
            row,
            required_label,
            report_year,
        )

        extracted[field_name] = values[0]

    return extracted


def main() -> None:
    manifest_df = read_manifest()

    if len(manifest_df) != 5:
        raise RuntimeError(
            "ציפינו לחמש נקודות בדיקה בדיוק, "
            f"אך נמצאו {len(manifest_df)}."
        )

    results = []

    for _, manifest_row in manifest_df.iterrows():
        html_file = Path(
            str(
                manifest_row[
                    "local_document_file"
                ]
            )
        )

        report_year = int(
            manifest_row["report_date"].year
        )

        print()
        print("=" * 85)
        print(
            f"בודק Oracle 10-K לשנת {report_year}"
        )
        print(f"קובץ:\n{html_file}")

        extracted = extract_one_filing(
            html_file,
            report_year,
        )

        operating_income = float(
            extracted["operating_income"]
        )

        pretax_income = float(
            extracted["pretax_income"]
        )

        tax_expense = float(
            extracted["tax_expense"]
        )

        if operating_income <= 0:
            raise RuntimeError(
                f"Operating Income אינו חיובי "
                f"לשנת {report_year}."
            )

        if pretax_income <= 0:
            raise RuntimeError(
                f"Pretax Income אינו חיובי "
                f"לשנת {report_year}."
            )

        if tax_expense < 0:
            raise RuntimeError(
                f"Tax Expense שלילי לשנת {report_year}. "
                "נדרשת בדיקת מקור ידנית."
            )

        effective_tax_rate = (
            tax_expense / pretax_income
        )

        if not 0 <= effective_tax_rate <= 1:
            raise RuntimeError(
                f"שיעור המס אינו בטווח 0%–100% "
                f"לשנת {report_year}: "
                f"{effective_tax_rate:.3%}"
            )

        nopat = (
            operating_income
            * (1 - effective_tax_rate)
        )

        date_rule_passed = (
            bool(
                manifest_row[
                    "date_rule_passed"
                ]
            )
            and manifest_row["filing_date"]
            <= manifest_row["as_of_date"]
        )

        print(
            f"Operating Income: {operating_income:,.0f}"
        )
        print(
            f"Pretax Income:    {pretax_income:,.0f}"
        )
        print(
            f"Tax Expense:      {tax_expense:,.0f}"
        )
        print(
            f"Effective Tax:    {effective_tax_rate:.3%}"
        )
        print(
            f"NOPAT:            {nopat:,.0f}"
        )

        results.append(
            {
                "ticker": "ORCL",
                "company_name": "Oracle Corporation",
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
                "operating_income_usd_millions": (
                    operating_income
                ),
                "pretax_income_usd_millions": (
                    pretax_income
                ),
                "tax_expense_usd_millions": (
                    tax_expense
                ),
                "effective_tax_rate": (
                    effective_tax_rate
                ),
                "effective_tax_rate_percent": (
                    effective_tax_rate * 100
                ),
                "nopat_usd_millions": nopat,
                "operating_income_usd": (
                    operating_income
                    * 1_000_000
                ),
                "pretax_income_usd": (
                    pretax_income
                    * 1_000_000
                ),
                "tax_expense_usd": (
                    tax_expense
                    * 1_000_000
                ),
                "nopat_usd": (
                    nopat
                    * 1_000_000
                ),
                "source": (
                    "Oracle full 10-K "
                    "validated income statement"
                ),
                "date_rule_passed": (
                    date_rule_passed
                ),
            }
        )

    result_df = pd.DataFrame(results)

    result_df = result_df.sort_values(
        by="as_of_date"
    )

    if result_df["as_of_date"].duplicated().any():
        raise RuntimeError(
            "נמצאו תאריכי בדיקה כפולים."
        )

    if not result_df[
        "date_rule_passed"
    ].all():
        raise RuntimeError(
            "לפחות שורה אחת הפרה את כלל התאריכים."
        )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    display_columns = [
        "as_of_date",
        "report_date",
        "operating_income_usd_millions",
        "pretax_income_usd_millions",
        "tax_expense_usd_millions",
        "effective_tax_rate_percent",
        "nopat_usd_millions",
        "date_rule_passed",
    ]

    print()
    print("=" * 155)
    print(
        "Oracle NOPAT — five validated "
        "point-in-time observations"
    )
    print("=" * 155)

    print(
        result_df[
            display_columns
        ].to_string(
            index=False,
            float_format=(
                lambda value: f"{value:,.3f}"
            ),
        )
    )

    print()
    print(
        f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}"
    )

    print()
    print(
        "הבדיקה עברה: NOPAT של Oracle חושב "
        "לחמש נקודות הבדיקה ישירות מטבלאות "
        "10-K שאומתו לפי התאמה מדויקת."
    )


if __name__ == "__main__":
    main()