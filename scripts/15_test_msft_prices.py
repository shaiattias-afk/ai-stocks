from __future__ import annotations

from pathlib import Path

import pandas as pd
import yfinance as yf


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_price_return_test.csv"
)

TICKERS = [
    "MSFT",
    "SPY",
]

START_TARGET = "2021-04-01"
END_TARGET = "2022-04-01"

# מורידים מעט מעבר לתאריכי היעד,
# כדי למצוא את יום המסחר הראשון הזמין אחריהם.
DOWNLOAD_START = "2021-03-25"
DOWNLOAD_END = "2022-04-15"


def download_prices(ticker: str) -> pd.DataFrame:
    print(f"מוריד מחירים עבור {ticker}...")

    df = yf.download(
        ticker,
        start=DOWNLOAD_START,
        end=DOWNLOAD_END,
        interval="1d",
        auto_adjust=True,
        actions=False,
        progress=False,
        multi_level_index=False,
    )

    if df is None or df.empty:
        raise RuntimeError(
            f"לא התקבלו מחירים עבור {ticker}."
        )

    if "Close" not in df.columns:
        raise RuntimeError(
            f"לא נמצאה עמודת Close עבור {ticker}.\n"
            f"העמודות שהתקבלו: {list(df.columns)}"
        )

    df = df.reset_index()

    if "Date" not in df.columns:
        raise RuntimeError(
            f"לא נמצאה עמודת Date עבור {ticker}."
        )

    df["Date"] = pd.to_datetime(
        df["Date"],
        errors="coerce",
    )

    df["Close"] = pd.to_numeric(
        df["Close"],
        errors="coerce",
    )

    df = df[
        df["Date"].notna()
        & df["Close"].notna()
    ].copy()

    if df.empty:
        raise RuntimeError(
            f"לא נשארו מחירים תקינים עבור {ticker}."
        )

    return df


def select_first_trading_day_on_or_after(
    price_df: pd.DataFrame,
    target_date: str,
) -> pd.Series:
    target_timestamp = pd.Timestamp(target_date)

    available = price_df[
        price_df["Date"] >= target_timestamp
    ].sort_values("Date")

    if available.empty:
        raise RuntimeError(
            f"לא נמצא יום מסחר בתאריך או אחרי {target_date}."
        )

    return available.iloc[0]


def main() -> None:
    results = []

    for ticker in TICKERS:
        price_df = download_prices(ticker)

        start_row = select_first_trading_day_on_or_after(
            price_df,
            START_TARGET,
        )

        end_row = select_first_trading_day_on_or_after(
            price_df,
            END_TARGET,
        )

        start_price = float(start_row["Close"])
        end_price = float(end_row["Close"])

        if start_price <= 0:
            raise RuntimeError(
                f"מחיר הפתיחה אינו חיובי עבור {ticker}."
            )

        total_return = (
            end_price / start_price
        ) - 1

        results.append(
            {
                "ticker": ticker,
                "start_target_date": START_TARGET,
                "start_trading_date": (
                    start_row["Date"].date()
                ),
                "start_adjusted_close": start_price,
                "end_target_date": END_TARGET,
                "end_trading_date": (
                    end_row["Date"].date()
                ),
                "end_adjusted_close": end_price,
                "total_return": total_return,
                "total_return_percent": (
                    total_return * 100
                ),
            }
        )

    result_df = pd.DataFrame(results)

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    msft_return = float(
        result_df.loc[
            result_df["ticker"] == "MSFT",
            "total_return",
        ].iloc[0]
    )

    spy_return = float(
        result_df.loc[
            result_df["ticker"] == "SPY",
            "total_return",
        ].iloc[0]
    )

    excess_return = msft_return - spy_return

    print()
    print("=" * 105)
    print("Microsoft versus SPY price-return test")
    print("=" * 105)

    print(
        result_df[
            [
                "ticker",
                "start_trading_date",
                "start_adjusted_close",
                "end_trading_date",
                "end_adjusted_close",
                "total_return_percent",
            ]
        ].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
        )
    )

    print()
    print(
        "עודף תשואת Microsoft מול SPY: "
        f"{excess_return * 100:,.3f}%"
    )

    print()
    print(f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}")

    print()
    print(
        "בדיקת המחירים עברה: נבחר יום המסחר הראשון "
        "בתאריך היעד או אחריו."
    )


if __name__ == "__main__":
    main()