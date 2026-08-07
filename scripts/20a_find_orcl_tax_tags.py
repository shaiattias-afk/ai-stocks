from pathlib import Path
import json

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

INPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "sec_raw"
    / "ORCL_companyfacts.json"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "orcl_tax_candidate_tags.csv"
)

SEARCH_WORDS = [
    "beforeincometax",
    "beforetax",
    "pretax",
    "incometaxexpense",
    "taxexpense",
    "provisionforincometaxes",
]


def main() -> None:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ Oracle לא נמצא:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        company_facts = json.load(file)

    results = []

    for namespace, facts in company_facts.get(
        "facts",
        {},
    ).items():
        for tag, details in facts.items():
            label = details.get("label", "")

            searchable = (
                f"{tag} {label}"
                .lower()
                .replace(" ", "")
            )

            if not any(
                word in searchable
                for word in SEARCH_WORDS
            ):
                continue

            usd_rows = (
                details
                .get("units", {})
                .get("USD", [])
            )

            for item in usd_rows:
                start = pd.to_datetime(
                    item.get("start"),
                    errors="coerce",
                )

                end = pd.to_datetime(
                    item.get("end"),
                    errors="coerce",
                )

                filed = pd.to_datetime(
                    item.get("filed"),
                    errors="coerce",
                )

                value = item.get("val")

                if (
                    pd.isna(start)
                    or pd.isna(end)
                    or pd.isna(filed)
                    or value is None
                ):
                    continue

                period_days = (end - start).days

                if not 300 <= period_days <= 400:
                    continue

                if item.get("form") != "10-K":
                    continue

                results.append(
                    {
                        "namespace": namespace,
                        "tag": tag,
                        "label": label,
                        "period_start": start.date(),
                        "period_end": end.date(),
                        "filing_date": filed.date(),
                        "value_usd": value,
                        "value_usd_billions": (
                            value / 1_000_000_000
                        ),
                        "form": item.get("form"),
                        "fiscal_year": item.get("fy"),
                        "accession_number": (
                            item.get("accn")
                        ),
                    }
                )

    result_df = pd.DataFrame(results)

    if result_df.empty:
        raise RuntimeError(
            "לא נמצאו תגי מס או Pretax שנתיים."
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
            annual_rows=(
                "period_end",
                "count",
            ),
            earliest_period_end=(
                "period_end",
                "min",
            ),
            latest_period_end=(
                "period_end",
                "max",
            ),
            latest_filing_date=(
                "filing_date",
                "max",
            ),
        )
        .reset_index()
    )

    print()
    print("=" * 155)
    print("Oracle Pretax Income and Tax candidate tags")
    print("=" * 155)

    print(
        summary_df.to_string(
            index=False
        )
    )

    print()
    print(
        f"התוצאה המלאה נשמרה כאן:\n"
        f"{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()