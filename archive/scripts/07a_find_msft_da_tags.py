from pathlib import Path
import json

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

INPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "sec_raw"
    / "MSFT_companyfacts.json"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_da_candidate_tags.csv"
)


INCLUDE_WORDS = [
    "depreciation",
    "amortization",
]

EXCLUDE_WORDS = [
    "future",
    "deferred",
    "accumulated",
    "schedule",
    "yearone",
    "yeartwo",
    "yearthree",
    "yearfour",
    "yearfive",
    "thereafter",
]


def main():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המקור לא נמצא:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        company_facts = json.load(file)

    candidates = []

    for namespace, namespace_facts in company_facts["facts"].items():
        for tag, details in namespace_facts.items():
            label = details.get("label", "")
            searchable = f"{tag} {label}".lower()

            contains_required_word = any(
                word in searchable
                for word in INCLUDE_WORDS
            )

            contains_excluded_word = any(
                word in searchable
                for word in EXCLUDE_WORDS
            )

            if not contains_required_word or contains_excluded_word:
                continue

            usd_rows = details.get("units", {}).get("USD", [])

            annual_rows = []

            for item in usd_rows:
                if (
                    item.get("form") == "10-K"
                    and item.get("fp") == "FY"
                    and item.get("start")
                    and item.get("end")
                    and item.get("filed")
                ):
                    annual_rows.append(item)

            if not annual_rows:
                continue

            annual_rows = sorted(
                annual_rows,
                key=lambda item: (
                    item.get("end", ""),
                    item.get("filed", ""),
                ),
            )

            latest = annual_rows[-1]

            candidates.append(
                {
                    "namespace": namespace,
                    "tag": tag,
                    "label": label,
                    "annual_rows": len(annual_rows),
                    "latest_period_start": latest.get("start"),
                    "latest_period_end": latest.get("end"),
                    "latest_filing_date": latest.get("filed"),
                    "latest_value_usd": latest.get("val"),
                    "latest_value_usd_billions": (
                        latest.get("val") / 1_000_000_000
                        if latest.get("val") is not None
                        else None
                    ),
                }
            )

    result_df = pd.DataFrame(candidates)

    if result_df.empty:
        raise RuntimeError(
            "לא נמצאו תגי D&A עם נתוני 10-K שנתיים."
        )

    result_df = result_df.sort_values(
        by=[
            "namespace",
            "tag",
        ]
    )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 130)
    print("Microsoft usable D&A candidate tags")
    print("=" * 130)

    print(
        result_df[
            [
                "namespace",
                "tag",
                "label",
                "annual_rows",
                "latest_period_end",
                "latest_value_usd_billions",
            ]
        ].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
        )
    )

    print()
    print(f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}")


if __name__ == "__main__":
    main()