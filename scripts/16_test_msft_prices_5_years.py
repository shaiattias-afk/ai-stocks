from __future__ import annotations

from pathlib import Path

import pandas as pd
import yfinance as yf


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

OUTPUT_FILE = (
    PROJECT_DIR
    / "data"
    / "msft_price_returns_5_years.csv"
)

TICKERS = [
    "MSFT",
    "SPY",
    "QQQ",
]

PERIODS = [
    ("2021-04-01", "2022-04-01"),
    ("2022-04-01", "2023-04-01"),
    ("2023-04-01", "2024-04-01"),
    ("2024-04-01", "2025-04-01"),
    ("2025-04-01", "2026-04-01"),
]

DOWNLOAD_START = "2021-03-25"
DOWNLOAD_END = "2026-04-15"


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
            f"לא נמצאה עמודת Close עבור {ticker}."
        )

    df = df.reset_index()

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
    price_tables = {
        ticker: download_prices(ticker)
        for ticker in TICKERS
    }

    results = []

    for start_target, end_target in PERIODS:
        period_returns = {}

        for ticker in TICKERS:
            price_df = price_tables[ticker]

            start_row = select_first_trading_day_on_or_after(
                price_df,
                start_target,
            )

            end_row = select_first_trading_day_on_or_after(
                price_df,
                end_target,
            )

            start_price = float(start_row["Close"])
            end_price = float(end_row["Close"])

            if start_price <= 0:
                raise RuntimeError(
                    f"מחיר פתיחה לא תקין עבור {ticker}."
                )

            total_return = (
                end_price / start_price
            ) - 1

            period_returns[ticker] = {
                "start_trading_date": start_row["Date"].date(),
                "end_trading_date": end_row["Date"].date(),
                "start_price": start_price,
                "end_price": end_price,
                "return": total_return,
            }

        msft_return = period_returns["MSFT"]["return"]
        spy_return = period_returns["SPY"]["return"]
        qqq_return = period_returns["QQQ"]["return"]

        results.append(
            {
                "start_target_date": start_target,
                "end_target_date": end_target,
                "start_trading_date": (
                    period_returns["MSFT"][
                        "start_trading_date"
                    ]
                ),
                "end_trading_date": (
                    period_returns["MSFT"][
                        "end_trading_date"
                    ]
                ),
                "msft_start_adjusted_close": (
                    period_returns["MSFT"]["start_price"]
                ),
                "msft_end_adjusted_close": (
                    period_returns["MSFT"]["end_price"]
                ),
                "msft_return": msft_return,
                "msft_return_percent": msft_return * 100,
                "spy_return": spy_return,
                "spy_return_percent": spy_return * 100,
                "qqq_return": qqq_return,
                "qqq_return_percent": qqq_return * 100,
                "excess_vs_spy": (
                    msft_return - spy_return
                ),
                "excess_vs_spy_percent": (
                    msft_return - spy_return
                ) * 100,
                "excess_vs_qqq": (
                    msft_return - qqq_return
                ),
                "excess_vs_qqq_percent": (
                    msft_return - qqq_return
                ) * 100,
            }
        )

    result_df = pd.DataFrame(results)

    result_df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    display_columns = [
        "start_target_date",
        "end_target_date",
        "start_trading_date",
        "end_trading_date",
        "msft_return_percent",
        "spy_return_percent",
        "qqq_return_percent",
        "excess_vs_spy_percent",
        "excess_vs_qqq_percent",
    ]

    print()
    print("=" * 155)
    print("Microsoft versus SPY and QQQ — five annual periods")
    print("=" * 155)

    print(
        result_df[display_columns].to_string(
            index=False,
            float_format=lambda value: f"{value:,.3f}",
        )
    )

    print()
    print(f"התוצאה נשמרה כאן:\n{OUTPUT_FILE}")

    print()
    print(
        "הבדיקה עברה: חושבו חמש תקופות שנתיות "
        "ל-MSFT, SPY ו-QQQ."
    )


if __name__ == "__main__":
    main()