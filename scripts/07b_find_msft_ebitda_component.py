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

# הערכים הרשמיים של Microsoft, במיליוני דולר
EXPECTED_VALUES = {
    2021: 11_686_000_000,
    2022: 14_460_000_000,
    2023: 13_861_000_000,
    2024: 22_287_000_000,
}


def main():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המקור לא נמצא:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open("r", encoding="utf-8") as file:
        company_facts = json.load(file)

    matches = []

    for namespace, facts in company_facts.get("facts", {}).items():
        for tag, details in facts.items():
            label = details.get("label", "")
            usd_rows = details.get("units", {}).get("USD", [])

            for item in usd_rows:
                value = item.get("val")
                period_end = item.get("end")
                filing_date = item.get("filed")

                if value is None or period_end is None:
                    continue

                try:
                    year = pd.Timestamp(period_end).year
                except (TypeError, ValueError):
                    continue

                expected_value = EXPECTED_VALUES.get(year)

                if expected_value is None:
                    continue

                # דורשים התאמה מדויקת לערך הרשמי
                if value == expected_value:
                    matches.append(
                        {
                            "year": year,
                            "namespace": namespace,
                            "tag": tag,
                            "label": label,
                            "value_usd": value,
                            "value_usd_billions": value / 1_000_000_000,
                            "period_start": item.get("start"),
                            "period_end": period_end,
                            "filing_date": filing_date,
                            "form": item.get("form"),
                            "fiscal_period": item.get("fp"),
                            "accession_number": item.get("accn"),
                        }
                    )

    result_df = pd.DataFrame(matches)

    if result_df.empty:
        raise RuntimeError(
            "לא נמצא תג שתואם לערכי D&A and other הרשמיים."
        )

    result_df = result_df.sort_values(
        by=["namespace", "tag", "year", "filing_date"]
    )

    print()
    print("=" * 130)
    print("Microsoft EBITDA component – exact value matches")
    print("=" * 130)

    print(
        result_df[
            [
                "year",
                "namespace",
                "tag",
                "label",
                "value_usd_billions",
                "period_end",
                "filing_date",
                "form",
            ]
        ].to_string(index=False)
    )

    output_file = (
        PROJECT_DIR
        / "data"
        / "msft_ebitda_component_matches.csv"
    )

    result_df.to_csv(
        output_file,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print(f"התוצאה נשמרה כאן:\n{output_file}")


if __name__ == "__main__":
    main()