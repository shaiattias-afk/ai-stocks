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
    / "msft_pretax_candidate_tags.csv"
)


SEARCH_WORDS = [
    "beforeincometax",
    "beforetax",
    "pretax",
    "income before tax",
    "income before income taxes",
]


def main():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ Company Facts לא נמצא:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open("r", encoding="utf-8") as file:
        company_facts = json.load(file)

    results = []

    for namespace, facts in company_facts.get("facts", {}).items():
        for tag, details in facts.items():
            label = details.get("label", "")

            searchable_text = (
                f"{tag} {label}"
            ).lower().replace(" ", "")

            if not any(
                word.replace(" ", "") in searchable_text
                for word in SEARCH_WORDS
            ):
                continue

            usd_rows = (
                details
                .get("units", {})
                .get("USD", [])
            )

            for item in usd_rows:
                start = item.get("start")
                end = item.get("end")
                filed = item.get("filed")

                if not start or not end or not filed:
                    continue

                start_date = pd.to_datetime(
                    start,
                    errors="coerce",
                )

                end_date = pd.to_datetime(
                    end,
                    errors="coerce",
                )

                filing_date = pd.to_datetime(
                    filed,
                    errors="coerce",
                )

                if (
                    pd.isna(start_date)
                    or pd.isna(end_date)
                    or pd.isna(filing_date)
                ):
                    continue

                period_days = (
                    end_date - start_date
                ).days

                if not 300 <= period_days <= 400:
                    continue

                results.append(
                    {
                        "namespace": namespace,
                        "tag": tag,
                        "label": label,
                        "period_start": start_date.date(),
                        "period_end": end_date.date(),
                        "filing_date": filing_date.date(),
                        "value_usd": item.get("val"),
                        "value_usd_billions": (
                            item.get("val") / 1_000_000_000
                            if item.get("val") is not None
                            else None
                        ),
                        "form": item.get("form"),
                        "fiscal_year": item.get("fy"),
                        "accession_number": item.get("accn"),
                    }
                )

    result_df = pd.DataFrame(results)

    if result_df.empty:
        raise RuntimeError(
            "לא נמצאו תגי Pretax Income עם תקופות שנתיות."
        )

    result_df = result_df.sort_values(
        by=[
            "namespace",
            "tag",
            "period_end",
            "filing_date",
        ]
    )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    summary_df = (
        result_df
        .groupby(
            [
                "namespace",
                "tag",
                "label",
            ],
            dropna=False,
        )
        .agg(
            annual_rows=("period_end", "count"),
            earliest_period_end=("period_end", "min"),
            latest_period_end=("period_end", "max"),
            earliest_filing_date=("filing_date", "min"),
            latest_filing_date=("filing_date", "max"),
        )
        .reset_index()
    )

    print()
    print("=" * 150)
    print("Microsoft Pretax Income candidate tags")
    print("=" * 150)

    print(
        summary_df.to_string(
            index=False
        )
    )

    print()
    print(f"התוצאה המלאה נשמרה כאן:\n{OUTPUT_FILE}")


if __name__ == "__main__":
    main()