from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent

CANDIDATES_DIR = (
    PROJECT_DIR
    / "data"
    / "meta_2024_income_statement_candidates"
)

TABLE_FILES = {
    7: CANDIDATES_DIR / "table_0007.csv",
    8: CANDIDATES_DIR / "table_0008.csv",
    23: CANDIDATES_DIR / "table_0023.csv",
}

OUTPUT_FILE = (
    CANDIDATES_DIR
    / "table_comparison.csv"
)


def load_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"קובץ הטבלה לא נמצא:\n{path}"
        )

    df = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
    )

    return df.apply(
        lambda column: column.str.strip()
    )


def main() -> None:
    tables = {
        table_number: load_table(path)
        for table_number, path in TABLE_FILES.items()
    }

    rows = []

    for first_number, first_table in tables.items():
        for second_number, second_table in tables.items():
            if first_number >= second_number:
                continue

            same_shape = (
                first_table.shape
                == second_table.shape
            )

            exactly_equal = (
                same_shape
                and first_table.equals(second_table)
            )

            rows.append(
                {
                    "first_table": first_number,
                    "second_table": second_number,
                    "first_rows": len(first_table),
                    "second_rows": len(second_table),
                    "first_columns": len(first_table.columns),
                    "second_columns": len(second_table.columns),
                    "same_shape": same_shape,
                    "exactly_equal": exactly_equal,
                }
            )

    result_df = pd.DataFrame(rows)

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print("=" * 90)
    print("Meta 2024 income-table comparison")
    print("=" * 90)

    print(
        result_df.to_string(
            index=False
        )
    )

    print()
    print(
        f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}"
    )

    if result_df["exactly_equal"].any():
        print()
        print(
            "נמצאה לפחות זוג טבלאות שהן "
            "כפילות מדויקת."
        )
    else:
        print()
        print(
            "שלוש הטבלאות שונות בתוכן. "
            "לא נבחרה אף טבלה אוטומטית."
        )


if __name__ == "__main__":
    main()