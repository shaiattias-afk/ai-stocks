from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

NET_DEBT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_net_debt_test.csv"
)

SHORT_TERM_INVESTMENTS_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_short_term_investments_test.csv"
)

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_adjusted_net_debt_test.csv"
)


def main():
    if not NET_DEBT_FILE.exists():
        raise FileNotFoundError(
            f"קובץ Net Debt לא נמצא:\n{NET_DEBT_FILE}"
        )

    if not SHORT_TERM_INVESTMENTS_FILE.exists():
        raise FileNotFoundError(
            "קובץ ההשקעות קצרות הטווח לא נמצא:\n"
            f"{SHORT_TERM_INVESTMENTS_FILE}"
        )

    net_debt_df = pd.read_csv(NET_DEBT_FILE)
    investments_df = pd.read_csv(
        SHORT_TERM_INVESTMENTS_FILE
    )

    required_net_debt_columns = {
        "as_of_date",
        "cash_usd",
        "total_debt_usd",
        "net_debt_usd",
        "date_rule_passed",
    }

    required_investment_columns = {
        "as_of_date",
        "short_term_investments_usd",
        "date_rule_passed",
    }

    missing_net_debt = (
        required_net_debt_columns
        - set(net_debt_df.columns)
    )

    missing_investments = (
        required_investment_columns
        - set(investments_df.columns)
    )

    if missing_net_debt:
        raise RuntimeError(
            "חסרות עמודות בקובץ Net Debt:\n"
            f"{sorted(missing_net_debt)}"
        )

    if missing_investments:
        raise RuntimeError(
            "חסרות עמודות בקובץ ההשקעות:\n"
            f"{sorted(missing_investments)}"
        )

    investments_df = investments_df.rename(
        columns={
            "date_rule_passed": (
                "investments_date_rule_passed"
            )
        }
    )

    net_debt_df = net_debt_df.rename(
        columns={
            "date_rule_passed": (
                "net_debt_date_rule_passed"
            )
        }
    )

    result_df = net_debt_df.merge(
        investments_df[
            [
                "as_of_date",
                "short_term_investments_usd",
                "investments_date_rule_passed",
            ]
        ],
        on="as_of_date",
        how="inner",
        validate="one_to_one",
    )

    if len(result_df) != 5:
        raise RuntimeError(
            "החיבור לא הפיק בדיוק חמש שורות."
        )

    result_df["basic_net_debt_usd"] = (
        result_df["total_debt_usd"]
        - result_df["cash_usd"]
    )

    result_df["adjusted_net_debt_usd"] = (
        result_df["total_debt_usd"]
        - result_df["cash_usd"]
        - result_df["short_term_investments_usd"]
    )

    result_df["adjusted_net_cash_usd"] = (
        -result_df["adjusted_net_debt_usd"]
    ).clip(lower=0)

    result_df["all_date_rules_passed"] = (
        result_df["net_debt_date_rule_passed"]
        & result_df["investments_date_rule_passed"]
    )

    money_columns = [
        "total_debt_usd",
        "cash_usd",
        "short_term_investments_usd",
        "basic_net_debt_usd",
        "adjusted_net_debt_usd",
        "adjusted_net_cash_usd",
    ]

    for column in money_columns:
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
        "total_debt_usd_billions",
        "cash_usd_billions",
        "short_term_investments_usd_billions",
        "basic_net_debt_usd_billions",
        "adjusted_net_debt_usd_billions",
        "adjusted_net_cash_usd_billions",
        "all_date_rules_passed",
    ]

    print()
    print("=" * 145)
    print("Microsoft Adjusted Net Debt test")
    print("=" * 145)

    print(
        result_df[display_columns].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
        )
    )

    print()
    print(f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}")

    if not result_df["all_date_rules_passed"].all():
        raise RuntimeError(
            "לפחות אחד מנתוני המקור הפר את כלל התאריכים."
        )

    print()
    print(
        "הבדיקה עברה: Adjusted Net Debt חושב באמצעות "
        "Debt פחות Cash פחות Short-Term Investments."
    )


if __name__ == "__main__":
    main()