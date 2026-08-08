from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

INPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_2024_income_statement_validation.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_2024_nopat_validated.csv"
)


EXPECTED_YEARS = [
    2024,
    2023,
    2022,
]

REQUIRED_ROWS = {
    "operating_income": "operating income",
    "pretax_income": "income before income taxes",
    "tax_expense": "provision for income taxes",
}


def normalize_text(value: object) -> str:
    text = str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip().lower()


def parse_financial_number(
    value: object,
) -> float | None:
    text = str(value).strip()

    if text.lower() in {
        "",
        "nan",
        "none",
        "$",
        "-",
        "—",
        "–",
    }:
        return None

    is_negative = (
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

    if is_negative:
        number = -number

    return number


def find_exact_row(
    table_df: pd.DataFrame,
    required_label: str,
) -> pd.Series:
    row_texts = table_df.apply(
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

    return table_df.loc[
        matching_indices[0]
    ]


def extract_three_values(
    row: pd.Series,
    required_label: str,
) -> list[float]:
    values = []

    for cell_value in row.tolist():
        number = parse_financial_number(
            cell_value
        )

        if number is not None:
            values.append(number)

    # מסירים ערכים שנראים כשנות כותרת,
    # אם מבנה ה-CSV כלל אותם בשורה.
    values = [
        value
        for value in values
        if int(value) not in EXPECTED_YEARS
    ]

    if len(values) != 3:
        raise RuntimeError(
            f"בשורה '{required_label}' ציפינו "
            "לשלושה ערכים שנתיים בדיוק, "
            f"אך נמצאו {len(values)}:\n{values}"
        )

    return values


def main() -> None:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            "קובץ הטבלה המאומתת לא נמצא:\n"
            f"{INPUT_FILE}"
        )

    table_df = pd.read_csv(
        INPUT_FILE,
        dtype=str,
        keep_default_na=False,
    )

    if table_df.empty:
        raise RuntimeError(
            "קובץ הטבלה המאומתת ריק."
        )

    extracted_rows = {}

    for field_name, required_label in (
        REQUIRED_ROWS.items()
    ):
        exact_row = find_exact_row(
            table_df,
            required_label,
        )

        values = extract_three_values(
            exact_row,
            required_label,
        )

        extracted_rows[field_name] = {
            "label": required_label,
            "values": values,
        }

        print()
        print(
            f"{required_label}:"
        )

        for year, value in zip(
            EXPECTED_YEARS,
            values,
        ):
            print(
                f"  {year}: "
                f"{value:,.0f} מיליון דולר"
            )

    operating_income_2024 = (
        extracted_rows[
            "operating_income"
        ]["values"][0]
    )

    pretax_income_2024 = (
        extracted_rows[
            "pretax_income"
        ]["values"][0]
    )

    tax_expense_2024 = (
        extracted_rows[
            "tax_expense"
        ]["values"][0]
    )

    if operating_income_2024 <= 0:
        raise RuntimeError(
            "Operating Income של 2024 "
            "אינו חיובי."
        )

    if pretax_income_2024 <= 0:
        raise RuntimeError(
            "Pretax Income של 2024 "
            "אינו חיובי."
        )

    if tax_expense_2024 < 0:
        raise RuntimeError(
            "Tax Expense של 2024 שלילי. "
            "נדרשת בדיקת מקור לפני חישוב."
        )

    effective_tax_rate = (
        tax_expense_2024
        / pretax_income_2024
    )

    if not 0 <= effective_tax_rate <= 1:
        raise RuntimeError(
            "שיעור המס האפקטיבי אינו בטווח "
            f"0%–100%: {effective_tax_rate:.3%}"
        )

    nopat_2024 = (
        operating_income_2024
        * (
            1
            - effective_tax_rate
        )
    )

    result_df = pd.DataFrame(
        [
            {
                "ticker": "ORCL",
                "report_year": 2024,
                "operating_income_usd_millions": (
                    operating_income_2024
                ),
                "pretax_income_usd_millions": (
                    pretax_income_2024
                ),
                "tax_expense_usd_millions": (
                    tax_expense_2024
                ),
                "effective_tax_rate": (
                    effective_tax_rate
                ),
                "effective_tax_rate_percent": (
                    effective_tax_rate * 100
                ),
                "nopat_usd_millions": (
                    nopat_2024
                ),
                "nopat_usd": (
                    nopat_2024
                    * 1_000_000
                ),
                "source": (
                    "Oracle 2024 full 10-K "
                    "validated income statement"
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
    print("=" * 80)
    print("Oracle 2024 validated NOPAT")
    print("=" * 80)

    print(
        f"Operating Income: "
        f"{operating_income_2024:,.0f} "
        "מיליון דולר"
    )

    print(
        f"Pretax Income:     "
        f"{pretax_income_2024:,.0f} "
        "מיליון דולר"
    )

    print(
        f"Tax Expense:       "
        f"{tax_expense_2024:,.0f} "
        "מיליון דולר"
    )

    print(
        f"Effective Tax Rate: "
        f"{effective_tax_rate:.3%}"
    )

    print(
        f"NOPAT:              "
        f"{nopat_2024:,.0f} "
        "מיליון דולר"
    )

    print()
    print(
        "התוצאה נשמרה כאן:\n"
        f"{OUTPUT_FILE}"
    )

    print()
    print(
        "הבדיקה עברה: NOPAT של Oracle לשנת "
        "2024 חושב רק מטבלה שאומתה מראש "
        "בדוח ה-10-K המלא."
    )


if __name__ == "__main__":
    main()