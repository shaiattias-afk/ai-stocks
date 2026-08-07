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
    / "orcl_10k_filings_manifest.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_inline_xbrl_candidates_2020_2024.csv"
)


# ============================================================
# הגדרות
# ============================================================

TARGET_REPORT_DATES = {
    pd.Timestamp("2020-05-31"),
    pd.Timestamp("2024-05-31"),
}

# אלה רק מונחי חיפוש בשמות התגים.
# הסקריפט אינו בוחר תג ואינו מחשב NOPAT.
TAG_SEARCH_TERMS = [
    "operatingincome",
    "incomefromoperations",
    "beforeincometax",
    "beforetax",
    "pretax",
    "incometaxexpense",
    "taxexpense",
    "provisionforincometaxes",
]


# ============================================================
# פונקציות עזר
# ============================================================

def normalize_text(value: object) -> str:
    text = str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


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
            "נמצא ערך שאינו True/False "
            "בעמודת date_rule_passed."
        )

    return normalized.astype(bool)


def read_manifest() -> pd.DataFrame:
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המיפוי לא נמצא:\n{MANIFEST_FILE}"
        )

    manifest_df = pd.read_csv(
        MANIFEST_FILE
    )

    required_columns = {
        "as_of_date",
        "report_date",
        "filing_date",
        "local_document_file",
        "date_rule_passed",
        "accession_number",
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

    if manifest_df[
        [
            "as_of_date",
            "report_date",
            "filing_date",
        ]
    ].isna().any().any():
        raise RuntimeError(
            "בקובץ המיפוי נמצאו תאריכים לא תקינים."
        )

    selected = manifest_df[
        manifest_df["report_date"].isin(
            TARGET_REPORT_DATES
        )
    ].copy()

    if len(selected) != 2:
        raise RuntimeError(
            "ציפינו למצוא בדיוק את דוחות "
            "Oracle לשנים 2020 ו-2024.\n"
            f"נמצאו {len(selected)} דוחות."
        )

    return selected.sort_values(
        "report_date"
    )


def get_attribute(
    tag,
    attribute_name: str,
) -> str | None:
    """
    מחפש מאפיין בלי תלות באותיות גדולות/קטנות.
    """

    for key, value in tag.attrs.items():
        if str(key).lower() == attribute_name.lower():
            return str(value)

    return None


def parse_scale(
    scale_text: str | None,
) -> int:
    if scale_text is None:
        return 0

    try:
        return int(scale_text)
    except ValueError as exc:
        raise RuntimeError(
            f"ערך scale אינו תקין: {scale_text}"
        ) from exc


def parse_sign(
    sign_text: str | None,
) -> int:
    if sign_text is None:
        return 1

    sign_text = sign_text.strip()

    if sign_text == "-":
        return -1

    if sign_text in {"", "+"}:
        return 1

    raise RuntimeError(
        f"ערך sign אינו תקין: {sign_text}"
    )


def parse_numeric_fact(
    raw_text: str,
    scale_text: str | None,
    sign_text: str | None,
) -> float | None:
    """
    ממיר עובדת Inline XBRL לערך מספרי.

    לא מנחש ערכים שאינם מספריים.
    """

    cleaned = normalize_text(raw_text)

    if cleaned in {
        "",
        "-",
        "—",
        "–",
        "nil",
    }:
        return None

    negative_parentheses = (
        cleaned.startswith("(")
        and cleaned.endswith(")")
    )

    cleaned = (
        cleaned.replace("$", "")
        .replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("%", "")
        .strip()
    )

    if not re.fullmatch(
        r"-?\d+(?:\.\d+)?",
        cleaned,
    ):
        return None

    value = float(cleaned)

    if negative_parentheses:
        value = -value

    scale = parse_scale(
        scale_text
    )

    sign = parse_sign(
        sign_text
    )

    return (
        value
        * (10 ** scale)
        * sign
    )


def build_context_map(
    soup: BeautifulSoup,
) -> dict[str, dict]:
    """
    בונה מפה של contextRef לתקופת הדיווח.
    """

    context_map = {}

    for context in soup.find_all(
        lambda tag: (
            tag.name
            and tag.name.lower()
            in {
                "xbrli:context",
                "context",
            }
        )
    ):
        context_id = get_attribute(
            context,
            "id",
        )

        if not context_id:
            continue

        start_tag = context.find(
            lambda tag: (
                tag.name
                and tag.name.lower()
                in {
                    "xbrli:startdate",
                    "startdate",
                }
            )
        )

        end_tag = context.find(
            lambda tag: (
                tag.name
                and tag.name.lower()
                in {
                    "xbrli:enddate",
                    "enddate",
                }
            )
        )

        instant_tag = context.find(
            lambda tag: (
                tag.name
                and tag.name.lower()
                in {
                    "xbrli:instant",
                    "instant",
                }
            )
        )

        explicit_members = []

        for member in context.find_all(
            lambda tag: (
                tag.name
                and tag.name.lower()
                in {
                    "xbrldi:explicitmember",
                    "explicitmember",
                }
            )
        ):
            dimension = get_attribute(
                member,
                "dimension",
            )

            member_value = normalize_text(
                member.get_text(
                    " ",
                    strip=True,
                )
            )

            explicit_members.append(
                {
                    "dimension": dimension,
                    "member": member_value,
                }
            )

        context_map[context_id] = {
            "period_start": (
                normalize_text(
                    start_tag.get_text(
                        " ",
                        strip=True,
                    )
                )
                if start_tag
                else None
            ),
            "period_end": (
                normalize_text(
                    end_tag.get_text(
                        " ",
                        strip=True,
                    )
                )
                if end_tag
                else None
            ),
            "instant_date": (
                normalize_text(
                    instant_tag.get_text(
                        " ",
                        strip=True,
                    )
                )
                if instant_tag
                else None
            ),
            "has_dimensions": bool(
                explicit_members
            ),
            "dimensions": " | ".join(
                (
                    f"{item['dimension']}="
                    f"{item['member']}"
                )
                for item in explicit_members
            ),
        }

    if not context_map:
        raise RuntimeError(
            "לא נמצאו Contexts של XBRL בדוח."
        )

    return context_map


def build_unit_map(
    soup: BeautifulSoup,
) -> dict[str, str]:
    unit_map = {}

    for unit in soup.find_all(
        lambda tag: (
            tag.name
            and tag.name.lower()
            in {
                "xbrli:unit",
                "unit",
            }
        )
    ):
        unit_id = get_attribute(
            unit,
            "id",
        )

        if not unit_id:
            continue

        unit_text = normalize_text(
            unit.get_text(
                " ",
                strip=True,
            )
        )

        unit_map[unit_id] = unit_text

    return unit_map


def tag_matches_search(
    fact_name: str,
) -> bool:
    normalized_name = (
        fact_name.lower()
        .replace(":", "")
        .replace("_", "")
        .replace("-", "")
    )

    return any(
        search_term in normalized_name
        for search_term in TAG_SEARCH_TERMS
    )


def extract_candidate_facts(
    html_file: Path,
    report_date: pd.Timestamp,
    filing_date: pd.Timestamp,
    as_of_date: pd.Timestamp,
    accession_number: str,
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

    context_map = build_context_map(
        soup
    )

    unit_map = build_unit_map(
        soup
    )

    results = []

    inline_facts = soup.find_all(
        lambda tag: (
            tag.name
            and tag.name.lower()
            in {
                "ix:nonfraction",
                "nonfraction",
            }
        )
    )

    if not inline_facts:
        raise RuntimeError(
            f"לא נמצאו עובדות ix:nonFraction בדוח:\n"
            f"{html_file}"
        )

    for fact in inline_facts:
        fact_name = get_attribute(
            fact,
            "name",
        )

        if not fact_name:
            continue

        if not tag_matches_search(
            fact_name
        ):
            continue

        context_ref = get_attribute(
            fact,
            "contextref",
        )

        unit_ref = get_attribute(
            fact,
            "unitref",
        )

        scale_text = get_attribute(
            fact,
            "scale",
        )

        sign_text = get_attribute(
            fact,
            "sign",
        )

        decimals = get_attribute(
            fact,
            "decimals",
        )

        raw_text = normalize_text(
            fact.get_text(
                " ",
                strip=True,
            )
        )

        numeric_value = parse_numeric_fact(
            raw_text,
            scale_text,
            sign_text,
        )

        context = context_map.get(
            context_ref,
            {},
        )

        namespace = (
            fact_name.split(":", 1)[0]
            if ":" in fact_name
            else None
        )

        local_tag = (
            fact_name.split(":", 1)[1]
            if ":" in fact_name
            else fact_name
        )

        results.append(
            {
                "report_date": (
                    report_date.date()
                ),
                "filing_date": (
                    filing_date.date()
                ),
                "as_of_date": (
                    as_of_date.date()
                ),
                "accession_number": (
                    accession_number
                ),
                "source_file": str(
                    html_file
                ),
                "fact_name": fact_name,
                "namespace": namespace,
                "local_tag": local_tag,
                "context_ref": context_ref,
                "period_start": context.get(
                    "period_start"
                ),
                "period_end": context.get(
                    "period_end"
                ),
                "instant_date": context.get(
                    "instant_date"
                ),
                "has_dimensions": context.get(
                    "has_dimensions"
                ),
                "dimensions": context.get(
                    "dimensions"
                ),
                "unit_ref": unit_ref,
                "unit_definition": (
                    unit_map.get(
                        unit_ref
                    )
                ),
                "raw_text": raw_text,
                "scale": scale_text,
                "sign": sign_text,
                "decimals": decimals,
                "numeric_value": numeric_value,
                "numeric_value_usd_billions": (
                    numeric_value
                    / 1_000_000_000
                    if (
                        numeric_value
                        is not None
                    )
                    else None
                ),
                "date_rule_passed": (
                    filing_date
                    <= as_of_date
                ),
            }
        )

    return results


# ============================================================
# Main
# ============================================================

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

        print()
        print("=" * 85)
        print(
            "בודק Inline XBRL בדוח Oracle:"
        )
        print(
            "Report date:",
            manifest_row[
                "report_date"
            ].date(),
        )
        print(f"קובץ:\n{html_file}")

        filing_results = (
            extract_candidate_facts(
                html_file=html_file,
                report_date=manifest_row[
                    "report_date"
                ],
                filing_date=manifest_row[
                    "filing_date"
                ],
                as_of_date=manifest_row[
                    "as_of_date"
                ],
                accession_number=str(
                    manifest_row[
                        "accession_number"
                    ]
                ),
            )
        )

        print(
            "מספר עובדות מועמדות שנמצאו:",
            len(filing_results),
        )

        results.extend(
            filing_results
        )

    result_df = pd.DataFrame(
        results
    )

    if result_df.empty:
        raise RuntimeError(
            "לא נמצאו עובדות Inline XBRL "
            "המתאימות למונחי החיפוש."
        )

    result_df = result_df.sort_values(
        by=[
            "report_date",
            "local_tag",
            "period_end",
            "context_ref",
        ],
        na_position="last",
    )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    summary_df = (
        result_df[
            [
                "report_date",
                "fact_name",
                "period_start",
                "period_end",
                "instant_date",
                "has_dimensions",
                "unit_definition",
                "raw_text",
                "numeric_value_usd_billions",
                "date_rule_passed",
            ]
        ]
        .drop_duplicates()
        .sort_values(
            by=[
                "report_date",
                "fact_name",
                "period_end",
            ],
            na_position="last",
        )
    )

    print()
    print("=" * 175)
    print(
        "Oracle Inline XBRL candidates — "
        "2020 and 2024"
    )
    print("=" * 175)

    print(
        summary_df.to_string(
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
            "לפחות עובדה אחת שויכה לדוח "
            "שהוגש לאחר תאריך הבדיקה."
        )

    print()
    print(
        "הבדיקה הסתיימה ללא חישוב: "
        "נשמרו שמות התגים, ההקשרים, "
        "התקופות, היחידות והערכים "
        "מתוך דוחות ה-10-K עצמם."
    )


if __name__ == "__main__":
    main()