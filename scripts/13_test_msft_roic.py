from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


# ============================================================
# מיקומי קבצים
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

COMPANY_FACTS_FILE = (
    PROJECT_DIR
    / "data"
    / "sec_raw"
    / "MSFT_companyfacts.json"
)

NOPAT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_nopat_test.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_roic_test.csv"
)


# ============================================================
# תגי SEC אפשריים
# ============================================================

EQUITY_TAGS = [
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
]

CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
]

SHORT_TERM_INVESTMENT_TAGS = [
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
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


# ============================================================
# טעינת נתונים
# ============================================================

def load_company_facts() -> dict:
    if not COMPANY_FACTS_FILE.exists():
        raise FileNotFoundError(
            f"קובץ Company Facts לא נמצא:\n{COMPANY_FACTS_FILE}"
        )

    with COMPANY_FACTS_FILE.open(
        mode="r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def load_nopat() -> pd.DataFrame:
    if not NOPAT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ NOPAT לא נמצא:\n{NOPAT_FILE}"
        )

    df = pd.read_csv(NOPAT_FILE)

    required_columns = {
        "as_of_date",
        "period_end",
        "nopat_usd",
        "date_rule_passed",
    }

    missing_columns = required_columns - set(df.columns)

    if missing_columns:
        raise RuntimeError(
            "בקובץ NOPAT חסרות עמודות:\n"
            f"{sorted(missing_columns)}"
        )

    df["as_of_date"] = pd.to_datetime(
        df["as_of_date"],
        errors="coerce",
    )

    df["period_end"] = pd.to_datetime(
        df["period_end"],
        errors="coerce",
    )

    df["nopat_usd"] = pd.to_numeric(
        df["nopat_usd"],
        errors="coerce",
    )

    if (
        df["as_of_date"].isna().any()
        or df["period_end"].isna().any()
        or df["nopat_usd"].isna().any()
    ):
        raise RuntimeError(
            "בקובץ NOPAT נמצאו ערכים חסרים או לא תקינים."
        )

    return df


# ============================================================
# בניית טבלת נתון מאזני
# ============================================================

def build_instant_table(
    us_gaap: dict,
    candidate_tags: list[str],
    value_column: str,
    field_name: str,
) -> pd.DataFrame:
    rows = []
    tags_found = []

    for tag in candidate_tags:
        if tag not in us_gaap:
            continue

        tags_found.append(tag)

        units = (
            us_gaap[tag]
            .get("units", {})
            .get("USD", [])
        )

        for item in units:
            rows.append(
                {
                    "tag": tag,
                    "balance_date": item.get("end"),
                    "filing_date": item.get("filed"),
                    value_column: item.get("val"),
                    "form": item.get("form"),
                    "accession_number": item.get("accn"),
                }
            )

    if not tags_found:
        raise RuntimeError(
            f"לא נמצא תג מתאים עבור {field_name}."
        )

    print(f"{field_name} — תגים שנמצאו:")

    for tag in tags_found:
        print(f"  {tag}")

    df = pd.DataFrame(rows)

    df["balance_date"] = pd.to_datetime(
        df["balance_date"],
        errors="coerce",
    )

    df["filing_date"] = pd.to_datetime(
        df["filing_date"],
        errors="coerce",
    )

    df[value_column] = pd.to_numeric(
        df[value_column],
        errors="coerce",
    )

    df = df[
        df["form"].isin(["10-K", "10-Q"])
        & df["balance_date"].notna()
        & df["filing_date"].notna()
        & df[value_column].notna()
    ].copy()

    if df.empty:
        raise RuntimeError(
            f"לא נמצאו נתונים תקינים עבור {field_name}."
        )

    return df


# ============================================================
# בחירת נתון שהיה זמין בתאריך הבדיקה
# ============================================================

def select_value_for_balance_date(
    df: pd.DataFrame,
    value_column: str,
    balance_date: pd.Timestamp,
    as_of_date: pd.Timestamp,
    field_name: str,
) -> pd.Series:
    available = df[
        (df["balance_date"] == balance_date)
        & (df["filing_date"] <= as_of_date)
    ].copy()

    if available.empty:
        raise RuntimeError(
            f"לא נמצא {field_name} לתאריך המאזן "
            f"{balance_date.date()}, שהיה זמין עד "
            f"{as_of_date.date()}."
        )

    available = available.sort_values(
        by=[
            "filing_date",
            "accession_number",
            "tag",
        ]
    )

    return available.iloc[-1]


def find_previous_annual_balance_date(
    equity_df: pd.DataFrame,
    current_balance_date: pd.Timestamp,
    as_of_date: pd.Timestamp,
) -> pd.Timestamp:
    available = equity_df[
        (equity_df["balance_date"] < current_balance_date)
        & (equity_df["filing_date"] <= as_of_date)
        & (equity_df["form"] == "10-K")
    ].copy()

    if available.empty:
        raise RuntimeError(
            f"לא נמצא תאריך מאזן קודם לפני "
            f"{current_balance_date.date()}."
        )

    return available["balance_date"].max()


# ============================================================
# חישוב הון מושקע
# ============================================================

def calculate_invested_capital(
    balance_date: pd.Timestamp,
    as_of_date: pd.Timestamp,
    equity_df: pd.DataFrame,
    cash_df: pd.DataFrame,
    investments_df: pd.DataFrame,
    current_debt_df: pd.DataFrame,
    noncurrent_debt_df: pd.DataFrame,
) -> dict:
    equity_row = select_value_for_balance_date(
        equity_df,
        "equity_usd",
        balance_date,
        as_of_date,
        "Shareholders' Equity",
    )

    cash_row = select_value_for_balance_date(
        cash_df,
        "cash_usd",
        balance_date,
        as_of_date,
        "Cash",
    )

    investments_row = select_value_for_balance_date(
        investments_df,
        "short_term_investments_usd",
        balance_date,
        as_of_date,
        "Short-Term Investments",
    )

    current_debt_row = select_value_for_balance_date(
        current_debt_df,
        "current_debt_usd",
        balance_date,
        as_of_date,
        "Current Debt",
    )

    noncurrent_debt_row = select_value_for_balance_date(
        noncurrent_debt_df,
        "noncurrent_debt_usd",
        balance_date,
        as_of_date,
        "Noncurrent Debt",
    )

    equity = float(equity_row["equity_usd"])
    cash = float(cash_row["cash_usd"])

    short_term_investments = float(
        investments_row["short_term_investments_usd"]
    )

    current_debt = float(
        current_debt_row["current_debt_usd"]
    )

    noncurrent_debt = float(
        noncurrent_debt_row["noncurrent_debt_usd"]
    )

    total_debt = current_debt + noncurrent_debt

    invested_capital = (
        total_debt
        + equity
        - cash
        - short_term_investments
    )

    filing_dates = [
        equity_row["filing_date"],
        cash_row["filing_date"],
        investments_row["filing_date"],
        current_debt_row["filing_date"],
        noncurrent_debt_row["filing_date"],
    ]

    return {
        "balance_date": balance_date,
        "equity_usd": equity,
        "cash_usd": cash,
        "short_term_investments_usd": short_term_investments,
        "current_debt_usd": current_debt,
        "noncurrent_debt_usd": noncurrent_debt,
        "total_debt_usd": total_debt,
        "invested_capital_usd": invested_capital,
        "latest_filing_date": max(filing_dates),
        "date_rule_passed": all(
            filing_date <= as_of_date
            for filing_date in filing_dates
        ),
    }


# ============================================================
# Main
# ============================================================

def main() -> None:
    company_facts = load_company_facts()
    nopat_df = load_nopat()

    us_gaap = company_facts["facts"]["us-gaap"]

    equity_df = build_instant_table(
        us_gaap,
        EQUITY_TAGS,
        "equity_usd",
        "Shareholders' Equity",
    )

    cash_df = build_instant_table(
        us_gaap,
        CASH_TAGS,
        "cash_usd",
        "Cash",
    )

    investments_df = build_instant_table(
        us_gaap,
        SHORT_TERM_INVESTMENT_TAGS,
        "short_term_investments_usd",
        "Short-Term Investments",
    )

    current_debt_df = build_instant_table(
        us_gaap,
        CURRENT_DEBT_TAGS,
        "current_debt_usd",
        "Current Debt",
    )

    noncurrent_debt_df = build_instant_table(
        us_gaap,
        NONCURRENT_DEBT_TAGS,
        "noncurrent_debt_usd",
        "Noncurrent Debt",
    )

    results = []

    for _, nopat_row in nopat_df.iterrows():
        as_of_date = nopat_row["as_of_date"]
        ending_balance_date = nopat_row["period_end"]

        beginning_balance_date = (
            find_previous_annual_balance_date(
                equity_df,
                ending_balance_date,
                as_of_date,
            )
        )

        beginning = calculate_invested_capital(
            beginning_balance_date,
            as_of_date,
            equity_df,
            cash_df,
            investments_df,
            current_debt_df,
            noncurrent_debt_df,
        )

        ending = calculate_invested_capital(
            ending_balance_date,
            as_of_date,
            equity_df,
            cash_df,
            investments_df,
            current_debt_df,
            noncurrent_debt_df,
        )

        average_invested_capital = (
            beginning["invested_capital_usd"]
            + ending["invested_capital_usd"]
        ) / 2

        if average_invested_capital <= 0:
            raise RuntimeError(
                f"Average Invested Capital אינו חיובי עבור "
                f"{as_of_date.date()}."
            )

        nopat = float(nopat_row["nopat_usd"])

        roic = nopat / average_invested_capital

        results.append(
            {
                "as_of_date": as_of_date.date(),
                "beginning_balance_date": (
                    beginning_balance_date.date()
                ),
                "ending_balance_date": (
                    ending_balance_date.date()
                ),
                "nopat_usd": nopat,
                "beginning_invested_capital_usd": (
                    beginning["invested_capital_usd"]
                ),
                "ending_invested_capital_usd": (
                    ending["invested_capital_usd"]
                ),
                "average_invested_capital_usd": (
                    average_invested_capital
                ),
                "roic": roic,
                "roic_percent": roic * 100,
                "beginning_latest_filing_date": (
                    beginning["latest_filing_date"].date()
                ),
                "ending_latest_filing_date": (
                    ending["latest_filing_date"].date()
                ),
                "date_rule_passed": (
                    beginning["date_rule_passed"]
                    and ending["date_rule_passed"]
                    and bool(nopat_row["date_rule_passed"])
                ),
            }
        )

    result_df = pd.DataFrame(results)

    for column in [
        "nopat_usd",
        "beginning_invested_capital_usd",
        "ending_invested_capital_usd",
        "average_invested_capital_usd",
    ]:
        result_df[
            column.replace("_usd", "_usd_billions")
        ] = result_df[column] / 1_000_000_000

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    display_columns = [
        "as_of_date",
        "beginning_balance_date",
        "ending_balance_date",
        "nopat_usd_billions",
        "beginning_invested_capital_usd_billions",
        "ending_invested_capital_usd_billions",
        "average_invested_capital_usd_billions",
        "roic_percent",
        "date_rule_passed",
    ]

    print()
    print("=" * 165)
    print("Microsoft ROIC point-in-time test")
    print("=" * 165)

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
        "הבדיקה עברה: ROIC חושב באמצעות NOPAT "
        "ו-Average Invested Capital ללא Look-ahead bias."
    )


if __name__ == "__main__":
    main()