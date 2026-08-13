"""User-directed (2026-08-13 session): now that the SWING track is
focused specifically on the 10 semiconductor/AI tickers (D-092/D-095),
this answers the practical question directly -- using each ticker's
LATEST available data, which of them shows the entry signal (growth>20%
+ pullback>=15% from its own 252-trading-day high) RIGHT NOW, and what
does the group's own tested track record say to expect if entered today.

This reuses the exact same, already-validated definitions -- nothing new
is being tried here, just applied to the current data point instead of
historical entry episodes (same pattern as scripts/224 for the long-term
track, restricted here to the 10-ticker swing universe).

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\239_swing_current_entry_screen.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

SECTOR_SOURCE = DATA_DIR / "pullback_excess_return_sector_exclusion_check_result.json"
SWING_RESULT_SOURCE = DATA_DIR / "swing_6month_hard_cap_no_stoploss_result.json"
RESULT_PATH = DATA_DIR / "swing_current_entry_screen_result.json"

GROWTH_THRESHOLD = 0.20
PULLBACK_THRESHOLD = 0.15
TRAILING_HIGH_WINDOW_DAYS = 252


def _latest_growth(connection: duckdb.DuckDBPyConnection, ticker: str) -> dict | None:
    quarters = connection.execute(
        """
        SELECT DISTINCT qmr.fiscal_year_end, qmr.fiscal_quarter, qmr.availability_date
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qer.ticker = ? AND qmr.metric_name = 'revenue'
        ORDER BY qmr.availability_date DESC
        """,
        [ticker],
    ).fetchall()
    for fye, fq, availability_date in quarters:
        factor = compute_revenue_growth_acceleration(connection, ticker, fye, fq)
        if factor["status"] == "PASS":
            return {
                "fiscal_year_end": str(fye), "fiscal_quarter": fq, "availability_date": str(availability_date),
                "current_yoy_growth": factor["current_yoy_growth"],
            }
    return None


def _pullback_now(connection: duckdb.DuckDBPyConnection, ticker: str) -> dict | None:
    prices = connection.execute(
        "SELECT price_date, close FROM historical_prices_daily WHERE ticker = ? ORDER BY price_date", [ticker],
    ).fetchall()
    if len(prices) < TRAILING_HIGH_WINDOW_DAYS + 1:
        return None
    latest_date, latest_close = prices[-1]
    window = prices[-(TRAILING_HIGH_WINDOW_DAYS + 1):-1]
    trailing_high = max(c for _, c in window)
    pullback = (trailing_high - latest_close) / trailing_high
    return {"price_date": str(latest_date), "latest_close": latest_close,
            "trailing_252d_high": trailing_high, "pullback": pullback}


def main() -> None:
    semi_tickers = sorted(json.loads(SECTOR_SOURCE.read_text(encoding="utf-8"))["excluded_tickers"])
    swing_summary = json.loads(SWING_RESULT_SOURCE.read_text(encoding="utf-8"))
    tested_tickers = sorted({e["ticker"] for e in swing_summary["hard_cap_episodes"]
                              if e["outcome"] != "censored_too_recent"})

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    names = dict(connection.execute(
        f"SELECT ticker, company_name FROM companies WHERE ticker IN ({','.join('?' * len(semi_tickers))})",
        semi_tickers,
    ).fetchall())

    print(f"SWING universe (10 semiconductor/AI tickers): {semi_tickers}")
    print(f"of these, {len(tested_tickers)} have real tested 6-month track record: {tested_tickers}")
    untested = sorted(set(semi_tickers) - set(tested_tickers))
    print(f"no tested history yet (too recent to have a determinable 126-day outcome): {untested}\n")

    print(f"{'Ticker':<7}{'Company':<26}{'Qtr':<10}{'YoY growth':>12}{'Pullback now':>14}{'ENTRY SIGNAL?':>15}")
    print("-" * 90)

    rows = []
    for ticker in semi_tickers:
        growth = _latest_growth(connection, ticker)
        pullback_info = _pullback_now(connection, ticker)
        has_growth = growth is not None and growth["current_yoy_growth"] > GROWTH_THRESHOLD
        has_pullback = pullback_info is not None and pullback_info["pullback"] >= PULLBACK_THRESHOLD
        entry_signal = has_growth and has_pullback

        growth_str = f"{growth['current_yoy_growth']:+.1%}" if growth else "n/a"
        qtr_str = f"{growth['fiscal_year_end']} {growth['fiscal_quarter']}" if growth else "n/a"
        pb_str = f"{pullback_info['pullback']:+.1%}" if pullback_info else "n/a"
        signal_str = "YES -- ENTER" if entry_signal else "no"
        tested_flag = "" if ticker in tested_tickers else " (no tested history)"

        print(f"{ticker:<7}{(names.get(ticker, ticker))[:24]:<26}{qtr_str:<10}{growth_str:>12}{pb_str:>14}"
              f"{signal_str:>15}{tested_flag}")

        rows.append({
            "ticker": ticker, "company_name": names.get(ticker, ticker),
            "latest_quarter": qtr_str, "growth_as_of": growth["availability_date"] if growth else None,
            "current_yoy_growth": growth["current_yoy_growth"] if growth else None,
            "pullback_now": pullback_info["pullback"] if pullback_info else None,
            "entry_signal_now": entry_signal, "has_tested_swing_history": ticker in tested_tickers,
        })

    connection.close()

    n_signals = sum(1 for r in rows if r["entry_signal_now"])
    print(f"\n{n_signals} of {len(semi_tickers)} companies show an active SWING entry signal right now.")
    print("\nWhat the group's own tested track record says to expect if you enter today (D-095, 6-month hard cap, no stop-loss):")
    print(f"  80% chance of reaching +30% within 6 months (usually much faster -- median ~2 months)")
    print(f"  17% chance of still being in a real loss at the 6-month mark (historical median -33%, worst -44%)")
    print(f"  2% chance of still being open with a small gain, not yet at target")
    print(f"  Blended expected outcome across all cases: average +22.7%, median +31.8%")
    print(f"  This is the SAME 9-10 companies' own history repeating -- not a guarantee, not independent evidence.")

    RESULT_PATH.write_text(json.dumps({
        "semiconductor_ai_universe": semi_tickers, "tested_tickers": tested_tickers, "untested_tickers": untested,
        "n_entry_signals_now": n_signals, "candidates": rows,
        "group_track_record_reminder": swing_summary.get("n_hit"),
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
