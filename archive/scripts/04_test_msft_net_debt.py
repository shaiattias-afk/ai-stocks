from pathlib import Path
import json

import pandas as pd


# ============================================================
# מיקומי קבצים
# ============================================================

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
    / "msft_net_debt_test.csv"
)


# ============================================================
# תאריכי הבדיקה
# ============================================================

AS_OF_DATES = [
    "2021-04-01",
    "2022-04-01",
    "2023-04-01",
    "2024-04-01",
    "2025-04-01",
]


# ============================================================
# תגי SEC אפשריים
# ============================================================

CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
]

CURRENT_DEBT_TAGS = [
    "LongTermDebtCurrent",
    "LongTermDebtAndFinanceLeaseObligationsCurrent",
    "ShortTermBorrowings",
]

NONCURRENT_DEBT_TAGS = [
    "LongTermDebtNoncurrent",
    "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
]


def load_company_facts():
    """טוען את קובץ ה-Company Facts המקומי."""

    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ המקור לא נמצא:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def find_first_existing_tag(
    us_gaap_facts,
    candidate_tags,
    field_name,
):
    """מוצא את התג הראשון שקיים בקובץ Microsoft."""

    for tag in candidate_tags:
        if tag in us_gaap_facts:
            print(f"{field_name}: {tag}")
            return tag

    raise RuntimeError(
        f"לא נמצא תג מתאים עבור {field_name}.\n"
        f"נבדקו התגים:\n{candidate_tags}"
    )


def build_instant_fact_table(
    us_gaap_facts,
    tag,
):
    """
    בונה טבלה לנתוני מאזן בנקודת זמן.

    נתוני מאזן כוללים end בלבד,
    ואינם חייבים לכלול start.
    """

    fact = us_gaap_facts[tag]
    units = fact.get("units", {})

    if "USD" not in units:
        raise RuntimeError(
            f"לא נמצאו נתוני USD עבור התג {tag}."
        )

    rows = []

    for item in units["USD"]:
        rows.append(
            {
                "end": item.get("end"),
                "value": item.get("val"),
                "accession_number": item.get("accn"),
                "fiscal_year": item.get("fy"),
                "fiscal_period": item.get("fp"),
                "form": item.get("form"),
                "filing_date": item.get("filed"),
                "frame": item.get("frame"),
            }
        )

    df = pd.DataFrame(rows)

    df["end"] = pd.to_datetime(
        df["end"],
        errors="coerce",
    )

    df["filing_date"] = pd.to_datetime(
        df["filing_date"],
        errors="coerce",
    )

    df["value"] = pd.to_numeric(
        df["value"],
        errors="coerce",
    )

    df = df[
        df["form"].isin(["10-K", "10-Q"])
        & df["value"].notna()
        & df["end"].notna()
        & df["filing_date"].notna()
    ].copy()

    if df.empty:
        raise RuntimeError(
            f"לא נמצאו שורות תקינות עבור התג {tag}."
        )

    return df


def select_latest_available_instant(
    fact_df,
    as_of_date,
):
    """
    בוחר את נקודת המאזן המאוחרת ביותר
    שדווחה עד תאריך הבדיקה.
    """

    as_of_timestamp = pd.Timestamp(as_of_date)

    available = fact_df[
        fact_df["filing_date"] <= as_of_timestamp
    ].copy()

    if available.empty:
        return None

    latest_period_end = available["end"].max()

    latest_period_rows = available[
        available["end"] == latest_period_end
    ].copy()

    latest_period_rows = latest_period_rows.sort_values(
        by=[
            "filing_date",
            "accession_number",
        ]
    )

    return latest_period_rows.iloc[-1]


def require_same_period(
    as_of_date,
    cash_row,
    current_debt_row,
    noncurrent_debt_row,
):
    """מוודא שכל נתוני המאזן מתייחסים לאותו תאריך."""

    period_ends = {
        cash_row["end"],
        current_debt_row["end"],
        noncurrent_debt_row["end"],
    }

    if len(period_ends) != 1:
        raise RuntimeError(
            f"ב-{as_of_date} הנתונים אינם מאותו תאריך מאזן:\n"
            f"Cash: {cash_row['end'].date()}\n"
            f"Current debt: "
            f"{current_debt_row['end'].date()}\n"
            f"Noncurrent debt: "
            f"{noncurrent_debt_row['end'].date()}"
        )


def main():
    company_facts = load_company_facts()
    us_gaap_facts = company_facts["facts"]["us-gaap"]

    cash_tag = find_first_existing_tag(
        us_gaap_facts,
        CASH_TAGS,
        "Cash",
    )

    current_debt_tag = find_first_existing_tag(
        us_gaap_facts,
        CURRENT_DEBT_TAGS,
        "Current Debt",
    )

    noncurrent_debt_tag = find_first_existing_tag(
        us_gaap_facts,
        NONCURRENT_DEBT_TAGS,
        "Noncurrent Debt",
    )

    cash_df = build_instant_fact_table(
        us_gaap_facts,
        cash_tag,
    )

    current_debt_df = build_instant_fact_table(
        us_gaap_facts,
        current_debt_tag,
    )

    noncurrent_debt_df = build_instant_fact_table(
        us_gaap_facts,
        noncurrent_debt_tag,
    )

    results = []

    for as_of_date in AS_OF_DATES:
        cash_row = select_latest_available_instant(
            cash_df,
            as_of_date,
        )

        current_debt_row = select_latest_available_instant(
            current_debt_df,
            as_of_date,
        )

        noncurrent_debt_row = select_latest_available_instant(
            noncurrent_debt_df,
            as_of_date,
        )

        if (
            cash_row is None
            or current_debt_row is None
            or noncurrent_debt_row is None
        ):
            raise RuntimeError(
                f"חסר נתון עבור {as_of_date}."
            )

        require_same_period(
            as_of_date,
            cash_row,
            current_debt_row,
            noncurrent_debt_row,
        )

        cash = float(cash_row["value"])
        current_debt = float(
            current_debt_row["value"]
        )
        noncurrent_debt = float(
            noncurrent_debt_row["value"]
        )

        total_debt = (
            current_debt
            + noncurrent_debt
        )

        net_debt = total_debt - cash

        filing_dates = [
            cash_row["filing_date"],
            current_debt_row["filing_date"],
            noncurrent_debt_row["filing_date"],
        ]

        latest_filing_date = max(filing_dates)
        as_of_timestamp = pd.Timestamp(as_of_date)

        date_rule_passed = all(
            filing_date <= as_of_timestamp
            for filing_date in filing_dates
        )

        results.append(
            {
                "as_of_date": as_of_date,
                "balance_sheet_date": (
                    cash_row["end"].date()
                ),
                "latest_filing_date": (
                    latest_filing_date.date()
                ),
                "cash_usd": cash,
                "current_debt_usd": current_debt,
                "noncurrent_debt_usd": noncurrent_debt,
                "total_debt_usd": total_debt,
                "net_debt_usd": net_debt,
                "date_rule_passed": date_rule_passed,
                "cash_tag": cash_tag,
                "current_debt_tag": current_debt_tag,
                "noncurrent_debt_tag": noncurrent_debt_tag,
            }
        )

    result_df = pd.DataFrame(results)

    money_columns = [
        "cash_usd",
        "current_debt_usd",
        "noncurrent_debt_usd",
        "total_debt_usd",
        "net_debt_usd",
    ]

    for column in money_columns:
        result_df[
            column.replace("_usd", "_usd_billions")
        ] = result_df[column] / 1_000_000_000

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    display_columns = [
        "as_of_date",
        "balance_sheet_date",
        "latest_filing_date",
        "cash_usd_billions",
        "current_debt_usd_billions",
        "noncurrent_debt_usd_billions",
        "total_debt_usd_billions",
        "net_debt_usd_billions",
        "date_rule_passed",
    ]

    print()
    print("=" * 145)
    print("Microsoft Net Debt point-in-time test")
    print("=" * 145)

    print(
        result_df[display_columns].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
        )
    )

    print()
    print(f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}")

    if not result_df["date_rule_passed"].all():
        raise RuntimeError(
            "לפחות שורה אחת הפרה את כלל התאריכים."
        )

    print()
    print(
        "הבדיקה עברה: Cash ו-Debt נלקחו רק מדוחות "
        "שהיו זמינים בתאריך הבדיקה."
    )


if __name__ == "__main__":
    main()