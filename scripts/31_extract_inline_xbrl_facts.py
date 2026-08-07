from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup, Tag


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
FILINGS_DIR = DATA_DIR / "sec_filings"


def normalize_text(value: object) -> str:
    text = str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def normalize_name(value: object) -> str:
    return normalize_text(value).lower()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract Inline XBRL facts directly from an official 10-K filing."
        )
    )

    parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker, for example META.",
    )

    parser.add_argument(
        "--year",
        required=True,
        type=int,
        help="Filing year, for example 2024.",
    )

    return parser.parse_args()


def find_manifest(ticker: str) -> Path | None:
    candidates = [
        DATA_DIR / f"{ticker.lower()}_10k_filings_manifest.csv",
        DATA_DIR / f"{ticker.upper()}_10k_filings_manifest.csv",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


def extract_paths_from_manifest(
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
        if normalize_name(column)
        in {
            "year",
            "filing_year",
            "report_year",
            "fiscal_year",
        }
    ]

    filtered = manifest.copy()

    if year_columns:
        year_column = year_columns[0]

        filtered = manifest[
            manifest[year_column]
            .astype(str)
            .str.contains(
                str(filing_year),
                regex=False,
            )
        ].copy()

    path_columns = [
        column
        for column in manifest.columns
        if any(
            keyword in normalize_name(column)
            for keyword in {
                "path",
                "file",
                "filename",
                "local",
                "document",
            }
        )
    ]

    found_paths: list[Path] = []

    for _, row in filtered.iterrows():
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
                found_paths.append(
                    candidate.resolve()
                )

    return list(dict.fromkeys(found_paths))


def search_filing_directory(
    ticker: str,
    filing_year: int,
) -> list[Path]:
    directories = [
        FILINGS_DIR / ticker.upper(),
        FILINGS_DIR / ticker.lower(),
    ]

    all_files: list[Path] = []

    for directory in directories:
        if not directory.exists():
            continue

        for path in directory.rglob("*"):
            if (
                path.is_file()
                and path.suffix.lower()
                in {
                    ".html",
                    ".htm",
                    ".txt",
                }
            ):
                all_files.append(
                    path.resolve()
                )

    year_text = str(filing_year)

    year_matches = [
        path
        for path in all_files
        if (
            year_text in path.name
            or year_text in str(path.parent)
        )
    ]

    candidates = (
        year_matches
        if year_matches
        else all_files
    )

    return sorted(
        candidates,
        key=lambda path: path.stat().st_size,
        reverse=True,
    )


def find_filing_file(
    ticker: str,
    filing_year: int,
) -> Path:
    candidate_paths: list[Path] = []

    manifest_file = find_manifest(ticker)

    if manifest_file is not None:
        candidate_paths.extend(
            extract_paths_from_manifest(
                manifest_file=manifest_file,
                filing_year=filing_year,
            )
        )

    candidate_paths.extend(
        search_filing_directory(
            ticker=ticker,
            filing_year=filing_year,
        )
    )

    candidate_paths = list(
        dict.fromkeys(candidate_paths)
    )

    if not candidate_paths:
        raise FileNotFoundError(
            "לא נמצא קובץ 10-K מתאים.\n"
            f"Ticker: {ticker}\n"
            f"שנה: {filing_year}\n"
            f"תיקייה: {FILINGS_DIR}"
        )

    html_paths = [
        path
        for path in candidate_paths
        if path.suffix.lower()
        in {
            ".html",
            ".htm",
        }
    ]

    candidates = (
        html_paths
        if html_paths
        else candidate_paths
    )

    return max(
        candidates,
        key=lambda path: path.stat().st_size,
    )


def read_filing(path: Path) -> str:
    return path.read_text(
        encoding="utf-8",
        errors="ignore",
    )


def get_attribute(
    tag: Tag,
    attribute_name: str,
) -> str:
    target = attribute_name.lower()

    for key, value in tag.attrs.items():
        if str(key).lower() == target:
            if isinstance(value, list):
                return " ".join(
                    str(item)
                    for item in value
                )

            return normalize_text(value)

    return ""


def local_tag_name(tag: Tag) -> str:
    name = str(tag.name).lower()

    if ":" in name:
        return name.split(":", 1)[1]

    return name


def parse_contexts(
    soup: BeautifulSoup,
) -> dict[str, dict[str, object]]:
    contexts: dict[str, dict[str, object]] = {}

    for tag in soup.find_all(True):
        if local_tag_name(tag) != "context":
            continue

        context_id = get_attribute(tag, "id")

        if not context_id:
            continue

        entity_identifier = ""
        entity_scheme = ""
        start_date = ""
        end_date = ""
        instant_date = ""
        dimensions: list[dict[str, str]] = []

        for child in tag.find_all(True):
            child_name = local_tag_name(child)

            if child_name == "identifier":
                entity_identifier = normalize_text(
                    child.get_text(
                        " ",
                        strip=True,
                    )
                )
                entity_scheme = get_attribute(
                    child,
                    "scheme",
                )

            elif child_name == "startdate":
                start_date = normalize_text(
                    child.get_text(
                        " ",
                        strip=True,
                    )
                )

            elif child_name == "enddate":
                end_date = normalize_text(
                    child.get_text(
                        " ",
                        strip=True,
                    )
                )

            elif child_name == "instant":
                instant_date = normalize_text(
                    child.get_text(
                        " ",
                        strip=True,
                    )
                )

            elif child_name in {
                "explicitmember",
                "typedmember",
            }:
                dimension_name = get_attribute(
                    child,
                    "dimension",
                )

                member_value = normalize_text(
                    child.get_text(
                        " ",
                        strip=True,
                    )
                )

                dimensions.append(
                    {
                        "dimension": dimension_name,
                        "member": member_value,
                        "type": child_name,
                    }
                )

        contexts[context_id] = {
            "entity_identifier": entity_identifier,
            "entity_scheme": entity_scheme,
            "period_start": start_date,
            "period_end": end_date,
            "instant_date": instant_date,
            "dimensions": dimensions,
            "dimension_count": len(dimensions),
        }

    return contexts


def parse_units(
    soup: BeautifulSoup,
) -> dict[str, str]:
    units: dict[str, str] = {}

    for tag in soup.find_all(True):
        if local_tag_name(tag) != "unit":
            continue

        unit_id = get_attribute(tag, "id")

        if not unit_id:
            continue

        measures = []

        for child in tag.find_all(True):
            if local_tag_name(child) == "measure":
                measure = normalize_text(
                    child.get_text(
                        " ",
                        strip=True,
                    )
                )

                if measure:
                    measures.append(measure)

        units[unit_id] = " / ".join(measures)

    return units


def clean_numeric_text(
    raw_text: str,
) -> str:
    text = normalize_text(raw_text)

    text = text.replace(",", "")
    text = text.replace("$", "")
    text = text.replace("€", "")
    text = text.replace("£", "")
    text = text.replace("%", "")
    text = text.replace("(", "-")
    text = text.replace(")", "")
    text = text.replace("−", "-")
    text = text.replace("–", "-")
    text = text.replace("—", "")
    text = text.strip()

    return text


def parse_decimal(
    raw_text: str,
) -> Decimal | None:
    cleaned = clean_numeric_text(raw_text)

    if cleaned.lower() in {
        "",
        "-",
        "nil",
        "none",
        "nan",
    }:
        return None

    cleaned = re.sub(
        r"[^0-9eE+\-.]",
        "",
        cleaned,
    )

    if cleaned in {
        "",
        "-",
        "+",
        ".",
    }:
        return None

    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def apply_scale_and_sign(
    number: Decimal,
    scale_text: str,
    sign_text: str,
) -> Decimal:
    adjusted = number

    if scale_text:
        try:
            scale = int(scale_text)
            adjusted = adjusted * (
                Decimal(10) ** scale
            )
        except ValueError:
            pass

    if sign_text == "-":
        adjusted = -abs(adjusted)

    return adjusted


def find_inline_fact_tags(
    soup: BeautifulSoup,
) -> list[Tag]:
    fact_tags: list[Tag] = []

    for tag in soup.find_all(True):
        tag_name = local_tag_name(tag)

        if tag_name in {
            "nonfraction",
            "nonnumeric",
            "fraction",
        }:
            fact_tags.append(tag)

    return fact_tags


def extract_fact_value(
    tag: Tag,
) -> tuple[str, str, str]:
    fact_type = local_tag_name(tag)

    raw_text = normalize_text(
        tag.get_text(
            " ",
            strip=True,
        )
    )

    if fact_type == "nonnumeric":
        return raw_text, raw_text, "text"

    if fact_type == "fraction":
        return raw_text, "", "fraction"

    number = parse_decimal(raw_text)

    if number is None:
        return raw_text, "", "unparsed_numeric"

    scale_text = get_attribute(tag, "scale")
    sign_text = get_attribute(tag, "sign")

    adjusted = apply_scale_and_sign(
        number=number,
        scale_text=scale_text,
        sign_text=sign_text,
    )

    return (
        raw_text,
        format(adjusted, "f"),
        "numeric",
    )


def extract_inline_xbrl_facts(
    ticker: str,
    filing_year: int,
    filing_file: Path,
) -> pd.DataFrame:
    html = read_filing(filing_file)

    soup = BeautifulSoup(
        html,
        "lxml",
    )

    contexts = parse_contexts(soup)
    units = parse_units(soup)
    fact_tags = find_inline_fact_tags(soup)

    records = []

    for fact_number, tag in enumerate(
        fact_tags,
        start=1,
    ):
        concept = get_attribute(tag, "name")
        context_id = get_attribute(
            tag,
            "contextref",
        )
        unit_id = get_attribute(
            tag,
            "unitref",
        )
        decimals = get_attribute(
            tag,
            "decimals",
        )
        scale = get_attribute(
            tag,
            "scale",
        )
        sign = get_attribute(
            tag,
            "sign",
        )
        format_name = get_attribute(
            tag,
            "format",
        )
        fact_id = get_attribute(tag, "id")

        raw_value, normalized_value, value_type = (
            extract_fact_value(tag)
        )

        context = contexts.get(
            context_id,
            {},
        )

        dimensions = context.get(
            "dimensions",
            [],
        )

        records.append(
            {
                "ticker": ticker.upper(),
                "filing_year": filing_year,
                "fact_number": fact_number,
                "fact_id": fact_id,
                "fact_type": local_tag_name(tag),
                "concept": concept,
                "namespace": (
                    concept.split(":", 1)[0]
                    if ":" in concept
                    else ""
                ),
                "concept_name": (
                    concept.split(":", 1)[1]
                    if ":" in concept
                    else concept
                ),
                "context_id": context_id,
                "entity_identifier": context.get(
                    "entity_identifier",
                    "",
                ),
                "period_start": context.get(
                    "period_start",
                    "",
                ),
                "period_end": context.get(
                    "period_end",
                    "",
                ),
                "instant_date": context.get(
                    "instant_date",
                    "",
                ),
                "dimension_count": context.get(
                    "dimension_count",
                    0,
                ),
                "dimensions_json": json.dumps(
                    dimensions,
                    ensure_ascii=False,
                ),
                "unit_id": unit_id,
                "unit": units.get(
                    unit_id,
                    "",
                ),
                "decimals": decimals,
                "scale": scale,
                "sign": sign,
                "format": format_name,
                "raw_value": raw_value,
                "normalized_value": normalized_value,
                "value_type": value_type,
                "source_file": str(filing_file),
            }
        )

    return pd.DataFrame(records)


def print_summary(
    facts: pd.DataFrame,
    filing_file: Path,
) -> None:
    print()
    print("=" * 100)
    print("INLINE XBRL — FACT EXTRACTION")
    print("=" * 100)

    print(f"קובץ מקור: {filing_file}")
    print(f"מספר Facts כולל: {len(facts):,}")

    if facts.empty:
        print()
        print(
            "לא נמצאו תגיות Inline XBRL בקובץ."
        )
        return

    numeric_count = int(
        (
            facts["value_type"]
            == "numeric"
        ).sum()
    )

    text_count = int(
        (
            facts["value_type"]
            == "text"
        ).sum()
    )

    dimensionless_count = int(
        (
            facts["dimension_count"]
            == 0
        ).sum()
    )

    unique_concepts = int(
        facts["concept"].nunique()
    )

    print(
        f"Facts מספריים: {numeric_count:,}"
    )
    print(
        f"Facts טקסטואליים: {text_count:,}"
    )
    print(
        "Facts ללא Dimensions: "
        f"{dimensionless_count:,}"
    )
    print(
        f"Concepts ייחודיים: {unique_concepts:,}"
    )

    print()
    print("דוגמה לעובדות מספריות:")

    preview = facts[
        facts["value_type"] == "numeric"
    ][
        [
            "concept",
            "period_start",
            "period_end",
            "instant_date",
            "dimension_count",
            "unit",
            "normalized_value",
        ]
    ].head(20)

    print(
        preview.to_string(
            index=False
        )
    )


def main() -> None:
    arguments = parse_arguments()

    ticker = arguments.ticker.upper()
    filing_year = arguments.year

    filing_file = find_filing_file(
        ticker=ticker,
        filing_year=filing_year,
    )

    facts = extract_inline_xbrl_facts(
        ticker=ticker,
        filing_year=filing_year,
        filing_file=filing_file,
    )

    output_file = (
        DATA_DIR
        / (
            f"{ticker.lower()}_"
            f"{filing_year}_"
            "inline_xbrl_facts.csv"
        )
    )

    facts.to_csv(
        output_file,
        index=False,
        encoding="utf-8-sig",
    )

    print_summary(
        facts=facts,
        filing_file=filing_file,
    )

    print()
    print(
        f"קובץ התוצאה נשמר כאן:\n"
        f"{output_file}"
    )

    print()
    print(
        "בשלב זה לא בוצע Mapping למדדים "
        "ולא נבחרו ערכים חשבונאיים."
    )


if __name__ == "__main__":
    main()