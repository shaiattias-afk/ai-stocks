from __future__ import annotations

import argparse
import re
from collections import deque
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup, Tag


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
FILINGS_DIR = DATA_DIR / "sec_filings"

OUTPUT_COLUMNS = [
    "ticker",
    "filing_year",
    "table_number",
    "detected_section",
    "inside_financial_statements_area",
    "caption",
    "previous_context",
    "first_rows_preview",
    "row_count",
    "column_count",
    "source_file",
]


FINANCIAL_STATEMENT_START_PHRASES = [
    "financial statements and supplementary data",
    "consolidated financial statements",
    "financial statements",
]

NOTES_START_PHRASES = [
    "notes to consolidated financial statements",
    "notes to financial statements",
]

FINANCIAL_STATEMENT_TITLES = [
    "consolidated statements of income",
    "consolidated statements of operations",
    "consolidated statements of earnings",
    "consolidated statements of comprehensive income",
    "consolidated balance sheets",
    "consolidated statements of financial position",
    "consolidated statements of cash flows",
    "consolidated statements of stockholders",
    "consolidated statements of shareholders",
]


def normalize_text(value: object) -> str:
    text = str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def normalize_for_match(value: object) -> str:
    return normalize_text(value).lower()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Locate the financial-statements area and list tables "
            "without selecting or parsing any statement."
        )
    )

    parser.add_argument(
        "--ticker",
        required=True,
        help="Company ticker, for example META.",
    )

    parser.add_argument(
        "--year",
        required=True,
        type=int,
        help="10-K filing year, for example 2024.",
    )

    return parser.parse_args()


def find_manifest(ticker: str) -> Path | None:
    ticker_lower = ticker.lower()

    candidates = [
        DATA_DIR / f"{ticker_lower}_10k_filings_manifest.csv",
        DATA_DIR / f"{ticker.upper()}_10k_filings_manifest.csv",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


def extract_existing_paths_from_manifest(
    manifest_file: Path,
    filing_year: int,
) -> list[Path]:
    manifest = pd.read_csv(
        manifest_file,
        dtype=str,
        keep_default_na=False,
    )

    year_columns = [
        column
        for column in manifest.columns
        if normalize_for_match(column)
        in {
            "year",
            "filing_year",
            "report_year",
            "fiscal_year",
        }
    ]

    filtered_manifest = manifest.copy()

    if year_columns:
        year_column = year_columns[0]

        filtered_manifest = manifest[
            manifest[year_column].astype(str).str.contains(
                str(filing_year),
                regex=False,
            )
        ].copy()

    path_keywords = [
        "path",
        "file",
        "filename",
        "local",
        "document",
    ]

    path_columns = [
        column
        for column in manifest.columns
        if any(
            keyword in normalize_for_match(column)
            for keyword in path_keywords
        )
    ]

    found_paths: list[Path] = []

    for _, row in filtered_manifest.iterrows():
        for column in path_columns:
            raw_value = normalize_text(row[column])

            if not raw_value:
                continue

            candidate = Path(raw_value)

            if not candidate.is_absolute():
                candidate = PROJECT_DIR / candidate

            if (
                candidate.exists()
                and candidate.is_file()
                and candidate.suffix.lower()
                in {
                    ".html",
                    ".htm",
                    ".txt",
                }
            ):
                found_paths.append(candidate.resolve())

    return list(dict.fromkeys(found_paths))


def search_filing_directory(
    ticker: str,
    filing_year: int,
) -> list[Path]:
    ticker_directory_candidates = [
        FILINGS_DIR / ticker.upper(),
        FILINGS_DIR / ticker.lower(),
    ]

    valid_extensions = {
        ".html",
        ".htm",
        ".txt",
    }

    all_files: list[Path] = []

    for ticker_directory in ticker_directory_candidates:
        if not ticker_directory.exists():
            continue

        for path in ticker_directory.rglob("*"):
            if (
                path.is_file()
                and path.suffix.lower() in valid_extensions
            ):
                all_files.append(path.resolve())

    year_text = str(filing_year)

    files_with_year_in_name = [
        path
        for path in all_files
        if year_text in path.name
        or year_text in str(path.parent)
    ]

    if files_with_year_in_name:
        return sorted(
            files_with_year_in_name,
            key=lambda path: path.stat().st_size,
            reverse=True,
        )

    return sorted(
        all_files,
        key=lambda path: path.stat().st_size,
        reverse=True,
    )


def find_filing_file(
    ticker: str,
    filing_year: int,
) -> Path:
    manifest_file = find_manifest(ticker)

    manifest_paths: list[Path] = []

    if manifest_file is not None:
        manifest_paths = extract_existing_paths_from_manifest(
            manifest_file=manifest_file,
            filing_year=filing_year,
        )

    directory_paths = search_filing_directory(
        ticker=ticker,
        filing_year=filing_year,
    )

    combined_paths = list(
        dict.fromkeys(
            manifest_paths + directory_paths
        )
    )

    if not combined_paths:
        raise FileNotFoundError(
            "לא נמצא קובץ 10-K מתאים.\n"
            f"Ticker: {ticker}\n"
            f"שנה: {filing_year}\n"
            f"תיקייה שנבדקה: {FILINGS_DIR}"
        )

    html_paths = [
        path
        for path in combined_paths
        if path.suffix.lower() in {
            ".html",
            ".htm",
        }
    ]

    candidate_paths = (
        html_paths
        if html_paths
        else combined_paths
    )

    selected_path = max(
        candidate_paths,
        key=lambda path: path.stat().st_size,
    )

    return selected_path


def read_filing_html(filing_file: Path) -> str:
    return filing_file.read_text(
        encoding="utf-8",
        errors="ignore",
    )


def is_useful_text_block(text: str) -> bool:
    normalized = normalize_text(text)

    if not normalized:
        return False

    if len(normalized) < 3:
        return False

    if re.fullmatch(
        r"[\d\s,.$()%\-–—]+",
        normalized,
    ):
        return False

    return True


def detect_section(
    text: str,
    current_section: str,
) -> str:
    normalized = normalize_for_match(text)

    if not normalized:
        return current_section

    for phrase in NOTES_START_PHRASES:
        if phrase in normalized:
            return "notes_to_financial_statements"

    for title in FINANCIAL_STATEMENT_TITLES:
        if title in normalized:
            return "primary_financial_statements"

    for phrase in FINANCIAL_STATEMENT_START_PHRASES:
        if phrase in normalized:
            return "financial_statements_area"

    if "management's discussion and analysis" in normalized:
        return "management_discussion_and_analysis"

    if "controls and procedures" in normalized:
        return "controls_and_procedures"

    return current_section


def extract_table_caption(
    table: Tag,
    previous_context: list[str],
) -> str:
    caption_tag = table.find("caption")

    if caption_tag is not None:
        caption_text = normalize_text(
            caption_tag.get_text(" ", strip=True)
        )

        if caption_text:
            return caption_text

    for context_text in reversed(previous_context):
        normalized = normalize_for_match(context_text)

        if any(
            title in normalized
            for title in FINANCIAL_STATEMENT_TITLES
        ):
            return context_text

    if previous_context:
        return previous_context[-1]

    return ""


def table_to_dataframe(
    table: Tag,
) -> pd.DataFrame:
    try:
        tables = pd.read_html(
            str(table),
            displayed_only=False,
        )
    except (ValueError, ImportError):
        return pd.DataFrame()

    if not tables:
        return pd.DataFrame()

    return tables[0]


def dataframe_preview(
    dataframe: pd.DataFrame,
    maximum_rows: int = 5,
) -> str:
    if dataframe.empty:
        return ""

    preview_rows = []

    for _, row in dataframe.head(maximum_rows).iterrows():
        cells = []

        for value in row.tolist():
            text = normalize_text(value)

            if text and text.lower() != "nan":
                cells.append(text)

        if cells:
            preview_rows.append(
                " | ".join(cells)
            )

    return " || ".join(preview_rows)


def is_inside_financial_area(
    section: str,
) -> bool:
    return section in {
        "financial_statements_area",
        "primary_financial_statements",
        "notes_to_financial_statements",
    }


def locate_tables(
    ticker: str,
    filing_year: int,
    filing_file: Path,
) -> pd.DataFrame:
    html = read_filing_html(filing_file)

    soup = BeautifulSoup(
        html,
        "lxml",
    )

    previous_text_blocks: deque[str] = deque(
        maxlen=8
    )

    current_section = "unknown"
    table_number = 0
    records = []

    body = soup.body or soup

    for element in body.descendants:
        if not isinstance(element, Tag):
            continue

        if element.name in {
            "script",
            "style",
            "noscript",
        }:
            continue

        if element.name == "table":
            table_number += 1

            context_list = list(
                previous_text_blocks
            )

            dataframe = table_to_dataframe(
                element
            )

            records.append(
                {
                    "ticker": ticker.upper(),
                    "filing_year": filing_year,
                    "table_number": table_number,
                    "detected_section": current_section,
                    "inside_financial_statements_area":
                        is_inside_financial_area(
                            current_section
                        ),
                    "caption": extract_table_caption(
                        table=element,
                        previous_context=context_list,
                    ),
                    "previous_context": " || ".join(
                        context_list
                    ),
                    "first_rows_preview":
                        dataframe_preview(
                            dataframe
                        ),
                    "row_count": len(dataframe),
                    "column_count": len(
                        dataframe.columns
                    ),
                    "source_file": str(
                        filing_file
                    ),
                }
            )

            continue

        if element.find_parent("table") is not None:
            continue

        if element.name not in {
            "div",
            "p",
            "span",
            "font",
            "b",
            "strong",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            continue

        direct_text = normalize_text(
            " ".join(
                element.find_all(
                    string=True,
                    recursive=False,
                )
            )
        )

        if not is_useful_text_block(
            direct_text
        ):
            continue

        current_section = detect_section(
            text=direct_text,
            current_section=current_section,
        )

        if (
            not previous_text_blocks
            or previous_text_blocks[-1]
            != direct_text
        ):
            previous_text_blocks.append(
                direct_text
            )

    return pd.DataFrame(
        records,
        columns=OUTPUT_COLUMNS,
    )


def print_summary(
    results: pd.DataFrame,
    filing_file: Path,
) -> None:
    print()
    print("=" * 110)
    print("LOCATE — Financial statements")
    print("=" * 110)

    print(f"קובץ מקור: {filing_file}")
    print(
        f"מספר טבלאות כולל: "
        f"{len(results)}"
    )

    financial_area = results[
        results[
            "inside_financial_statements_area"
        ]
        == True
    ].copy()

    print(
        "מספר טבלאות שסומנו באזור "
        f"הדוחות הכספיים: "
        f"{len(financial_area)}"
    )

    print()

    if financial_area.empty:
        print(
            "לא זוהה עדיין אזור דוחות כספיים."
        )
        print(
            "לא בוצעה בחירה ולא חולצו נתונים."
        )

        return

    display_columns = [
        "table_number",
        "detected_section",
        "caption",
        "row_count",
        "column_count",
    ]

    print(
        financial_area[
            display_columns
        ].to_string(
            index=False,
        )
    )

    print()
    print(
        "לא נבחרה טבלת Income Statement "
        "ולא חולצו ערכים."
    )


def main() -> None:
    arguments = parse_arguments()

    ticker = arguments.ticker.upper()
    filing_year = arguments.year

    filing_file = find_filing_file(
        ticker=ticker,
        filing_year=filing_year,
    )

    results = locate_tables(
        ticker=ticker,
        filing_year=filing_year,
        filing_file=filing_file,
    )

    output_file = (
        DATA_DIR
        / (
            f"{ticker.lower()}_"
            f"{filing_year}_"
            "financial_statements_location.csv"
        )
    )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    results.to_csv(
        output_file,
        index=False,
        encoding="utf-8-sig",
    )

    print_summary(
        results=results,
        filing_file=filing_file,
    )

    print()
    print(
        f"קובץ האבחון נשמר כאן:\n"
        f"{output_file}"
    )


if __name__ == "__main__":
    main()