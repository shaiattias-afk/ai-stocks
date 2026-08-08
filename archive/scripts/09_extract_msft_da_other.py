from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup


# ============================================================
# מיקומי קבצים
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

MANIFEST_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_10k_filings_manifest.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_da_other_test.csv"
)


# ============================================================
# הגדרות
# ============================================================

TARGET_TEXT = "depreciation, amortization, and other"

EXPECTED_AS_OF_DATES = [
    "2021-04-01",
    "2022-04-01",
    "2023-04-01",
    "2024-04-01",
    "2025-04-01",
]


# ============================================================
# פונקציות עזר
# ============================================================

def normalize_text(value: str) -> str:
    """
    מנקה רווחים ותווים מיוחדים כדי לאפשר
    השוואה אחידה של טקסט מתוך HTML.
    """

    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def parse_financial_number(text: str) -> float | None:
    """
    ממיר ערך שמופיע בטבלה למספר.

    דוגמאות:
    22,287      -> 22287
    (1,250)     -> -1250
    $17,482     -> 17482
    —           -> None
    """

    cleaned = normalize_text(text)

    if cleaned in {
        "",
        "-",
        "—",
        "–",
        "$",
    }:
        return None

    is_negative = (
        cleaned.startswith("(")
        and cleaned.endswith(")")
    )

    cleaned = cleaned.replace("$", "")
    cleaned = cleaned.replace(",", "")
    cleaned = cleaned.replace("(", "")
    cleaned = cleaned.replace(")", "")
    cleaned = cleaned.strip()

    # משאירים רק מספר תקין, ללא אחוזים או מלל.
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return None

    value = float(cleaned)

    if is_negative:
        value = -value

    return value


def read_manifest() -> pd.DataFrame:
    """קורא ובודק את קובץ מיפוי הדוחות."""

    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(
            "קובץ המיפוי לא נמצא:\n"
            f"{MANIFEST_FILE}"
        )

    manifest_df = pd.read_csv(
        MANIFEST_FILE
    )

    required_columns = {
        "as_of_date",
        "report_date",
        "filing_date",
        "form",
        "accession_number",
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

    manifest_df["as_of_date"] = pd.to_datetime(
        manifest_df["as_of_date"],
        errors="coerce",
    )

    manifest_df["report_date"] = pd.to_datetime(
        manifest_df["report_date"],
        errors="coerce",
    )

    manifest_df["filing_date"] = pd.to_datetime(
        manifest_df["filing_date"],
        errors="coerce",
    )

    manifest_df = manifest_df[
        manifest_df["as_of_date"].notna()
        & manifest_df["report_date"].notna()
        & manifest_df["filing_date"].notna()
    ].copy()

    if manifest_df.empty:
        raise RuntimeError(
            "קובץ המיפוי אינו מכיל שורות תקינות."
        )

    return manifest_df


def find_candidate_rows(
    html_file: Path,
) -> list[dict]:
    """
    מאתר בדוח את כל שורות הטבלה שמכילות את הביטוי:
    Depreciation, amortization, and other
    """

    if not html_file.exists():
        raise FileNotFoundError(
            f"קובץ הדוח לא נמצא:\n{html_file}"
        )

    html_content = html_file.read_text(
        encoding="utf-8",
        errors="ignore",
    )

    soup = BeautifulSoup(
        html_content,
        "lxml",
    )

    candidates = []

    for row in soup.find_all("tr"):
        cells = row.find_all(
            ["td", "th"],
            recursive=False,
        )

        if not cells:
            continue

        cell_texts = [
            normalize_text(
                cell.get_text(
                    " ",
                    strip=True,
                )
            )
            for cell in cells
        ]

        full_row_text = normalize_text(
            " ".join(cell_texts)
        ).lower()

        if TARGET_TEXT not in full_row_text:
            continue

        parsed_values = []

        for cell_index, cell_text in enumerate(cell_texts):
            parsed_value = parse_financial_number(
                cell_text
            )

            if parsed_value is not None:
                parsed_values.append(
                    {
                        "cell_index": cell_index,
                        "raw_text": cell_text,
                        "value": parsed_value,
                    }
                )

        candidates.append(
            {
                "row_text": normalize_text(
                    " | ".join(cell_texts)
                ),
                "cell_texts": cell_texts,
                "parsed_values": parsed_values,
                "numeric_value_count": len(
                    parsed_values
                ),
            }
        )

    return candidates


def select_best_candidate(
    candidates: list[dict],
    html_file: Path,
) -> dict:
    """
    בוחר את השורה הסבירה ביותר.

    בדוח השנתי של Microsoft השורה הרלוונטית
    אמורה להכיל לפחות שלושה ערכים שנתיים.
    """

    usable_candidates = [
        candidate
        for candidate in candidates
        if candidate["numeric_value_count"] >= 3
    ]

    if not usable_candidates:
        candidate_texts = "\n\n".join(
            candidate["row_text"]
            for candidate in candidates
        )

        raise RuntimeError(
            "הביטוי נמצא בדוח, אך לא נמצאה שורת טבלה "
            "עם לפחות שלושה ערכים מספריים.\n\n"
            f"קובץ:\n{html_file}\n\n"
            f"שורות שנמצאו:\n{candidate_texts}"
        )

    # מעדיפים את השורה עם מספר הערכים הרב ביותר.
    usable_candidates.sort(
        key=lambda candidate: (
            candidate["numeric_value_count"],
            len(candidate["row_text"]),
        ),
        reverse=True,
    )

    return usable_candidates[0]


def extract_current_year_value(
    html_file: Path,
) -> dict:
    """
    מחלץ את הערך של שנת הדוח.

    בטבלאות Microsoft הערכים מוצגים מהשנה החדשה
    לישנה, ולכן הערך המספרי הראשון בשורה הוא שנת הדוח.
    את כל הערכים נשמור כדי לאפשר ביקורת ידנית.
    """

    candidates = find_candidate_rows(
        html_file
    )

    if not candidates:
        raise RuntimeError(
            "לא נמצאה בדוח השורה:\n"
            "Depreciation, amortization, and other\n\n"
            f"קובץ:\n{html_file}"
        )

    selected = select_best_candidate(
        candidates,
        html_file,
    )

    numeric_values = selected[
        "parsed_values"
    ]

    current_year_value_millions = (
        numeric_values[0]["value"]
    )

    return {
        "selected_row_text": selected[
            "row_text"
        ],
        "numeric_values_raw": " | ".join(
            item["raw_text"]
            for item in numeric_values
        ),
        "current_year_value_millions": (
            current_year_value_millions
        ),
        "candidate_count": len(candidates),
    }


# ============================================================
# Main
# ============================================================

def main() -> None:
    manifest_df = read_manifest()

    available_as_of_dates = set(
        manifest_df["as_of_date"]
        .dt.strftime("%Y-%m-%d")
    )

    missing_dates = (
        set(EXPECTED_AS_OF_DATES)
        - available_as_of_dates
    )

    if missing_dates:
        raise RuntimeError(
            "חסרים תאריכי בדיקה בקובץ המיפוי:\n"
            f"{sorted(missing_dates)}"
        )

    results = []

    for _, row in manifest_df.iterrows():
        as_of_date = row["as_of_date"]
        report_date = row["report_date"]
        filing_date = row["filing_date"]

        html_file = Path(
            str(row["local_document_file"])
        )

        print()
        print("=" * 75)
        print(
            "בודק דוח לשנת:",
            report_date.year,
        )
        print(f"קובץ: {html_file}")

        extracted = extract_current_year_value(
            html_file
        )

        value_millions = extracted[
            "current_year_value_millions"
        ]

        value_usd = (
            value_millions
            * 1_000_000
        )

        print(
            "ערכים שנמצאו בשורה:",
            extracted["numeric_values_raw"],
        )

        print(
            "הערך שנבחר לשנת הדוח:",
            f"{value_millions:,.0f} מיליון דולר",
        )

        results.append(
            {
                "as_of_date": (
                    as_of_date.date()
                ),
                "report_year": (
                    report_date.year
                ),
                "report_date": (
                    report_date.date()
                ),
                "filing_date": (
                    filing_date.date()
                ),
                "form": row["form"],
                "accession_number": (
                    row["accession_number"]
                ),
                "local_document_file": str(
                    html_file
                ),
                "da_and_other_usd_millions": (
                    value_millions
                ),
                "da_and_other_usd": (
                    value_usd
                ),
                "da_and_other_usd_billions": (
                    value_usd
                    / 1_000_000_000
                ),
                "all_numeric_values_in_row": (
                    extracted[
                        "numeric_values_raw"
                    ]
                ),
                "selected_row_text": (
                    extracted[
                        "selected_row_text"
                    ]
                ),
                "candidate_count": (
                    extracted[
                        "candidate_count"
                    ]
                ),
                "date_rule_passed": (
                    filing_date
                    <= as_of_date
                ),
            }
        )

    result_df = pd.DataFrame(
        results
    )

    result_df = result_df.sort_values(
        by="as_of_date"
    )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    display_columns = [
        "as_of_date",
        "report_year",
        "filing_date",
        "da_and_other_usd_billions",
        "date_rule_passed",
    ]

    print()
    print("=" * 105)
    print(
        "Microsoft Depreciation, "
        "Amortization and Other extraction"
    )
    print("=" * 105)

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
        "התוצאה נשמרה כאן:\n"
        f"{OUTPUT_FILE}"
    )

    if not result_df[
        "date_rule_passed"
    ].all():
        raise RuntimeError(
            "לפחות דוח אחד הוגש לאחר תאריך הבדיקה."
        )

    if result_df[
        "da_and_other_usd"
    ].isna().any():
        raise RuntimeError(
            "חסר ערך לפחות בשנת בדיקה אחת."
        )

    print()
    print(
        "הבדיקה עברה: השורה חולצה מהדוחות המלאים "
        "ורק מדוחות שהיו זמינים בתאריך הבדיקה."
    )


if __name__ == "__main__":
    main()