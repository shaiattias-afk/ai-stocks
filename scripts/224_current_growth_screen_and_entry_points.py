"""User-directed practical query (2026-08-13 session): given the validated
growth>20% threshold (D-079/D-080) and the growth+pullback entry-timing
signal (D-081), which companies in the current universe pass the growth
screen RIGHT NOW (using each ticker's most recent available quarter), and
does today's price already meet the pullback>=15%-from-252-day-high entry
condition?

Reuses the exact same functions/thresholds as the validated scripts
(quarterly_trend_v1.compute_revenue_growth_acceleration for growth,
scripts/220-221's 252-day trailing-high pullback definition) -- no new
methodology introduced here, just applied to the latest available data
point instead of historical entry episodes.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\224_current_growth_screen_and_entry_points.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

RESULT_PATH = DATA_DIR / "current_growth_screen_and_entry_points_result.json"

GROWTH_THRESHOLD = 0.20
PULLBACK_THRESHOLD = 0.15
TRAILING_HIGH_WINDOW_DAYS = 252


def _universe(connection: duckdb.DuckDBPyConnection) -> list[str]:
    q_tickers = {r[0] for r in connection.execute(
        "SELECT DISTINCT qer.ticker FROM quarterly_extraction_runs qer "
        "JOIN quarterly_metric_results qmr ON qmr.run_id = qer.run_id WHERE qmr.metric_name = 'revenue'"
    ).fetchall()}
    p_tickers = {r[0] for r in connection.execute("SELECT DISTINCT ticker FROM historical_prices_daily").fetchall()}
    return sorted(q_tickers & p_tickers)


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
                "current_yoy_growth": factor["current_yoy_growth"], "trend": factor["value"],
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
    return {
        "price_date": str(latest_date), "latest_close": latest_close,
        "trailing_252d_high": trailing_high, "pullback": pullback,
    }


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    tickers = _universe(connection)
    names = dict(connection.execute("SELECT ticker, company_name FROM companies").fetchall())
    print(f"universe: {len(tickers)} tickers with both quarterly revenue and price data\n")

    qualifying = []
    for ticker in tickers:
        growth = _latest_growth(connection, ticker)
        if growth is None or growth["current_yoy_growth"] <= GROWTH_THRESHOLD:
            continue
        pullback_info = _pullback_now(connection, ticker)
        row = {
            "ticker": ticker, "company_name": names.get(ticker, ticker),
            "latest_quarter": f"{growth['fiscal_year_end']} {growth['fiscal_quarter']}",
            "growth_as_of": growth["availability_date"],
            "current_yoy_growth": growth["current_yoy_growth"],
            "accelerating": growth["trend"] is not None and growth["trend"] > 0,
            "pullback_now": pullback_info,
            "entry_signal_now": pullback_info is not None and pullback_info["pullback"] >= PULLBACK_THRESHOLD,
        }
        qualifying.append(row)

    connection.close()
    qualifying.sort(key=lambda r: r["current_yoy_growth"], reverse=True)

    print(f"{'Ticker':<7}{'Company':<28}{'Qtr':<10}{'YoY growth':>12}{'Trend':<7}{'Pullback now':>14}{'ENTRY?':>9}")
    print("-" * 95)
    for r in qualifying:
        pb = r["pullback_now"]
        pb_str = f"{pb['pullback']:+.1%}" if pb else "n/a"
        trend_str = "accel" if r["accelerating"] else "decel"
        entry_str = "YES" if r["entry_signal_now"] else "no"
        print(f"{r['ticker']:<7}{r['company_name'][:26]:<28}{r['latest_quarter']:<10}"
              f"{r['current_yoy_growth']:>11.1%} {trend_str:<6}{pb_str:>14}{entry_str:>9}")

    n_entry_now = sum(1 for r in qualifying if r["entry_signal_now"])
    print(f"\n{len(qualifying)} companies pass the growth>{GROWTH_THRESHOLD:.0%} screen right now.")
    print(f"{n_entry_now} of those also meet the pullback>={PULLBACK_THRESHOLD:.0%}-from-252-day-high entry condition today.")

    RESULT_PATH.write_text(json.dumps({
        "universe_size": len(tickers), "growth_threshold": GROWTH_THRESHOLD, "pullback_threshold": PULLBACK_THRESHOLD,
        "trailing_high_window_days": TRAILING_HIGH_WINDOW_DAYS,
        "qualifying_companies": qualifying, "n_qualifying": len(qualifying), "n_entry_signal_now": n_entry_now,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
