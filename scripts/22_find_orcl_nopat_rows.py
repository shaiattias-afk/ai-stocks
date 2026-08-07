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
    / "orcl_nopat_row_candidates.csv"
)


SEARCH_PHRASES = [
    "income before provision for income taxes",
    "income before income taxes",
    "provision for income taxes",
    "income tax provision",
]


def normalize_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


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

    missing_columns = required_columns - set(df.columns)

    if missing_columns:
        raise RuntimeError(
            "בקובץ המיפוי חסרות עמודות:\n"
            f"{sorted(missing_columns)}"
        )

    return df


def find_candidate_rows(
    html_file: Path,
) -> list[dict]:
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

    candidates = []

    for row_number, row in enumerate(
        soup.find_all("tr"),
        start=1,
    ):
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

        row_text = normalize_text(
            " | ".join(cell_texts)
        )

        lower_text = row_text.lower()

        matched_phrases = [
            phrase
            for phrase in SEARCH_PHRASES
            if phrase in lower_text
        ]

        if not matched_phrases:
            continue

        candidates.append(
            {
                "html_row_number": row_number,
                "matched_phrases": " | ".join(
                    matched_phrases
                ),
                "row_text": row_text,
                "cell_count": len(cell_texts),
            }
        )

    return candidates


def main() -> None:
    manifest_df = read_manifest()

    results = []

    for _, manifest_row in manifest_df.iterrows():
        html_file = Path(
            str(
                manifest_row[
                    "local_document_file"
                ]
            )
        )

        print()
        print("=" * 80)
        print(
            f"בודק דוח: "
            f"{manifest_row['report_date']}"
        )
        print(f"קובץ: {html_file}")

        candidates = find_candidate_rows(
            html_file
        )

        print(
            f"נמצאו {len(candidates)} "
            "שורות מועמדות."
        )

        for candidate in candidates:
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
                    "local_document_file": str(
                        html_file
                    ),
                    "html_row_number": (
                        candidate[
                            "html_row_number"
                        ]
                    ),
                    "matched_phrases": (
                        candidate[
                            "matched_phrases"
                        ]
                    ),
                    "cell_count": (
                        candidate["cell_count"]
                    ),
                    "row_text": (
                        candidate["row_text"]
                    ),
                    "date_rule_passed": (
                        manifest_row[
                            "date_rule_passed"
                        ]
                    ),
                }
            )

    result_df = pd.DataFrame(results)

    if result_df.empty:
        raise RuntimeError(
            "לא נמצאו בדוחות שורות מתאימות "
            "לרווח לפני מס או להוצאות מס."
        )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 130)
    print("Oracle NOPAT row candidates")
    print("=" * 130)

    print(
        result_df[
            [
                "report_date",
                "matched_phrases",
                "cell_count",
                "row_text",
            ]
        ].to_string(
            index=False,
        )
    )

    print()
    print(
        f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()