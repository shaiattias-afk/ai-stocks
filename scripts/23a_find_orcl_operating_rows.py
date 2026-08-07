from pathlib import Path
import re

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
    / "orcl_operating_row_candidates.csv"
)


TARGET_PHRASES = [
    "operating income",
    "income from operations",
    "operating loss",
    "loss from operations",
]


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


def main() -> None:
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המיפוי לא נמצא:\n{MANIFEST_FILE}"
        )

    manifest_df = pd.read_csv(
        MANIFEST_FILE
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

        for row_number, row in enumerate(
            soup.find_all("tr"),
            start=1,
        ):
            # לא משתמשים ב-recursive=False,
            # משום שבדוחות SEC יש לעיתים תאים מקוננים.
            cells = row.find_all(
                ["td", "th"]
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

            row_text = normalize_text(
                " | ".join(cell_texts)
            )

            lower_text = row_text.lower()

            matched_phrases = [
                phrase
                for phrase in TARGET_PHRASES
                if phrase in lower_text
            ]

            if not matched_phrases:
                continue

            numeric_values = []

            for cell_text in cell_texts:
                number = parse_number(
                    cell_text
                )

                if number is not None:
                    numeric_values.append(
                        number
                    )

            # שורת דוח שנתית צריכה להכיל
            # לפחות שלושה מספרים: שלוש שנות השוואה.
            if len(numeric_values) < 3:
                continue

            # פסקאות תוכן עניינים ארוכות נפסלות.
            if len(row_text) > 500:
                continue

            results.append(
                {
                    "as_of_date": (
                        manifest_row["as_of_date"]
                    ),
                    "report_date": (
                        manifest_row["report_date"]
                    ),
                    "filing_date": (
                        manifest_row["filing_date"]
                    ),
                    "html_row_number": row_number,
                    "matched_phrases": (
                        " | ".join(
                            matched_phrases
                        )
                    ),
                    "numeric_count": (
                        len(numeric_values)
                    ),
                    "numeric_values": (
                        " | ".join(
                            f"{value:,.0f}"
                            for value in numeric_values
                        )
                    ),
                    "row_text": row_text,
                    "local_document_file": str(
                        html_file
                    ),
                }
            )

    result_df = pd.DataFrame(
        results
    )

    if result_df.empty:
        raise RuntimeError(
            "לא נמצאו שורות כספיות מתאימות "
            "לרווח תפעולי בדוחות Oracle."
        )

    result_df = result_df.drop_duplicates(
        subset=[
            "report_date",
            "row_text",
        ]
    ).sort_values(
        by=[
            "report_date",
            "html_row_number",
        ]
    )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 145)
    print("Oracle operating-income row candidates")
    print("=" * 145)

    print(
        result_df[
            [
                "report_date",
                "matched_phrases",
                "numeric_values",
                "row_text",
            ]
        ].to_string(
            index=False
        )
    )

    print()
    print(
        f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()