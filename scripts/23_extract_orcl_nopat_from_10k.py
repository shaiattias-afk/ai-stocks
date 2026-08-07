from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup


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
    / "orcl_nopat_from_10k.csv"
)


TARGET_ROWS = {
    "operating_income": [
        "operating income",
        "income from operations",
    ],
    "pretax_income": [
        "income before income taxes",
        "income before provision for income taxes",
    ],
    "tax_expense": [
        "provision for income taxes",
        "income tax provision",
    ],
}


def normalize_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def parse_number(value: str) -> float | None:
    cleaned = normalize_text(value)

    if cleaned in {
        "",
        "$",
        "-",
        "—",
        "–",
    }:
        return None

    negative = (
        cleaned.startswith("(")
        and cleaned.endswith(")")
    )

    cleaned = cleaned.replace("$", "")
    cleaned = cleaned.replace(",", "")
    cleaned = cleaned.replace("(", "")
    cleaned = cleaned.replace(")", "")
    cleaned = cleaned.strip()

    if not re.fullmatch(
        r"-?\d+(?:\.\d+)?",
        cleaned,
    ):
        return None

    number = float(cleaned)

    if negative:
        number = -number

    return number


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


def read_manifest() -> pd.DataFrame:
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המיפוי לא נמצא:\n{MANIFEST_FILE}"
        )

    df = pd.read_csv(
        MANIFEST_FILE
    )

    required_columns = {
        "as_of_date",
        "report_date",
        "filing_date",
        "local_document_file",
        "date_rule_passed",
    }

    missing_columns = (
        required_columns
        - set(df.columns)
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
        df[column] = pd.to_datetime(
            df[column],
            errors="coerce",
        )

    df["date_rule_passed"] = (
        normalize_boolean(
            df["date_rule_passed"]
        )
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


def extract_row_data(row) -> dict:
    cells = row.find_all(
        ["td", "th"]
    )

    cell_texts = [
        normalize_text(
            cell.get_text(
                " ",
                strip=True,
            )
        )
        for cell in cells
    ]

    row_text = normalize_text(
        " | ".join(cell_texts)
    )

    numeric_values = []

    for cell_text in cell_texts:
        number = parse_number(
            cell_text
        )

        if number is not None:
            numeric_values.append(
                number
            )

    return {
        "row_text": row_text,
        "numeric_values": numeric_values,
    }


def find_best_financial_row(
    soup: BeautifulSoup,
    field_name: str,
) -> dict:
    phrases = TARGET_ROWS[field_name]

    candidates = []
    seen_rows = set()

    for row in soup.find_all("tr"):
        extracted = extract_row_data(
            row
        )

        row_text = extracted["row_text"]

        if not row_text:
            continue

        lower_text = row_text.lower()

        matched_phrase = next(
            (
                phrase
                for phrase in phrases
                if phrase in lower_text
            ),
            None,
        )

        if matched_phrase is None:
            continue

        numeric_values = extracted[
            "numeric_values"
        ]

        if len(numeric_values) < 3:
            continue

        if len(row_text) > 500:
            continue

        if row_text in seen_rows:
            continue

        seen_rows.add(row_text)

        phrase_position = lower_text.find(
            matched_phrase
        )

        begins_with_phrase = (
            phrase_position <= 10
        )

        candidates.append(
            {
                "row_text": row_text,
                "numeric_values": numeric_values,
                "matched_phrase": matched_phrase,
                "begins_with_phrase": (
                    begins_with_phrase
                ),
            }
        )

    if not candidates:
        raise RuntimeError(
            f"לא נמצאה שורה כספית מתאימה עבור "
            f"{field_name}."
        )

    candidates.sort(
        key=lambda candidate: (
            not candidate[
                "begins_with_phrase"
            ],
            abs(
                len(
                    candidate[
                        "numeric_values"
                    ]
                )
                - 3
            ),
            len(
                candidate["row_text"]
            ),
        )
    )

    selected = candidates[0]

    print()
    print(
        f"שורה שנבחרה עבור {field_name}:"
    )
    print(selected["row_text"])
    print(
        "ערכים:",
        selected["numeric_values"],
    )

    return selected


def extract_current_year_values(
    html_file: Path,
) -> dict:
    if not html_file.exists():
        raise FileNotFoundError(
            f"קובץ הדוח לא נמצא:\n{html_file}"
        )

    html = html_file.read_text(
        encoding="utf-8",
        errors="ignore",
    )

    soup = BeautifulSoup(
        html,
        "lxml",
    )

    operating_row = (
        find_best_financial_row(
            soup,
            "operating_income",
        )
    )

    pretax_row = (
        find_best_financial_row(
            soup,
            "pretax_income",
        )
    )

    tax_row = (
        find_best_financial_row(
            soup,
            "tax_expense",
        )
    )

    return {
        "operating_income_millions": (
            operating_row[
                "numeric_values"
            ][0]
        ),
        "pretax_income_millions": (
            pretax_row[
                "numeric_values"
            ][0]
        ),
        "tax_expense_millions": (
            tax_row[
                "numeric_values"
            ][0]
        ),
        "operating_income_row": (
            operating_row["row_text"]
        ),
        "pretax_income_row": (
            pretax_row["row_text"]
        ),
        "tax_expense_row": (
            tax_row["row_text"]
        ),
    }


def main() -> None:
    manifest_df = read_manifest()

    results = []

    for _, manifest_row in (
        manifest_df.iterrows()
    ):
        html_file = Path(
            str(
                manifest_row[
                    "local_document_file"
                ]
            )
        )

        report_year = (
            manifest_row[
                "report_date"
            ].year
        )

        print()
        print("=" * 90)
        print(
            f"בודק Oracle 10-K לשנת "
            f"{report_year}"
        )
        print(f"קובץ:\n{html_file}")

        extracted = (
            extract_current_year_values(
                html_file
            )
        )

        operating_income = float(
            extracted[
                "operating_income_millions"
            ]
        )

        pretax_income = float(
            extracted[
                "pretax_income_millions"
            ]
        )

        tax_expense = float(
            extracted[
                "tax_expense_millions"
            ]
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

        effective_tax_rate = (
            tax_expense
            / pretax_income
        )

        if not 0 <= effective_tax_rate <= 1:
            raise RuntimeError(
                f"שיעור המס אינו בטווח תקין "
                f"לשנת {report_year}: "
                f"{effective_tax_rate:.2%}"
            )

        nopat_millions = (
            operating_income
            * (
                1
                - effective_tax_rate
            )
        )

        date_rule_passed = (
            manifest_row[
                "filing_date"
            ]
            <= manifest_row[
                "as_of_date"
            ]
            and bool(
                manifest_row[
                    "date_rule_passed"
                ]
            )
        )

        results.append(
            {
                "ticker": "ORCL",
                "company_name": (
                    "Oracle Corporation"
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
                    effective_tax_rate
                    * 100
                ),
                "nopat_usd_millions": (
                    nopat_millions
                ),
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
                    nopat_millions
                    * 1_000_000
                ),
                "operating_income_row": (
                    extracted[
                        "operating_income_row"
                    ]
                ),
                "pretax_income_row": (
                    extracted[
                        "pretax_income_row"
                    ]
                ),
                "tax_expense_row": (
                    extracted[
                        "tax_expense_row"
                    ]
                ),
                "date_rule_passed": (
                    date_rule_passed
                ),
            }
        )

    result_df = pd.DataFrame(
        results
    ).sort_values(
        by="as_of_date"
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
        "Oracle NOPAT extracted "
        "from full 10-K filings"
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
        f"התוצאה נשמרה כאן:\n"
        f"{OUTPUT_FILE}"
    )

    if not result_df[
        "date_rule_passed"
    ].all():
        raise RuntimeError(
            "לפחות דוח אחד הפר את כלל התאריכים."
        )

    print()
    print(
        "הבדיקה עברה: Operating Income, "
        "Pretax Income, Tax Expense ו-NOPAT "
        "חולצו ישירות מדוחות ה-10-K."
    )


if __name__ == "__main__":
    main()