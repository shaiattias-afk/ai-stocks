"""User-directed full-universe run (2026-08-13 session), scaling up
scripts/220's 5-company proof after two real bugs were found and fixed
there (episode flicker at the pullback threshold boundary, needing
hysteresis; right-censoring of recent entries that haven't had enough
forward time yet to know if they "recovered"). Both fixes are carried
over unchanged.

**Adds the control group the 5-company proof was explicitly missing**:
Group A (growth>20% AND pullback>=15%, the signal being tested) vs.
Group B (pullback>=15% alone, no growth condition at all -- "does the
stock just being down 15% from its own 252-day high already predict
recovery, regardless of fundamentals?"). If Group A does not clearly
outperform Group B, the growth filter is not adding anything a pure
price-dip strategy didn't already have.

Full ~92-company universe (every ticker with both quarterly revenue
data and daily price data -- confirmed by direct query before running).
Full available price/growth history per ticker, not limited to the last
12 quarters (a deliberate difference from D-079/D-080's quarterly-only
analysis -- this is a distinct methodology, evaluated at daily
resolution).

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\221_pullback_recovery_full_universe.py
"""

from __future__ import annotations

import json
import time
from datetime import date

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

RESULT_PATH = DATA_DIR / "pullback_recovery_full_universe_result.json"

GROWTH_THRESHOLD = 0.20
PULLBACK_THRESHOLD = 0.15
TRAILING_HIGH_WINDOW_DAYS = 252
RECOVERY_HORIZONS_MONTHS = (6, 12, 24)


def _universe(connection: duckdb.DuckDBPyConnection) -> list[str]:
    q_tickers = {r[0] for r in connection.execute(
        "SELECT DISTINCT qer.ticker FROM quarterly_extraction_runs qer "
        "JOIN quarterly_metric_results qmr ON qmr.run_id = qer.run_id WHERE qmr.metric_name = 'revenue'"
    ).fetchall()}
    p_tickers = {r[0] for r in connection.execute("SELECT DISTINCT ticker FROM historical_prices_daily").fetchall()}
    return sorted(q_tickers & p_tickers)


def _quarterly_growth_history(connection: duckdb.DuckDBPyConnection, ticker: str) -> list[dict]:
    quarters = connection.execute(
        """
        SELECT DISTINCT qmr.fiscal_year_end, qmr.fiscal_quarter, qmr.availability_date
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qer.ticker = ? AND qmr.metric_name = 'revenue'
        ORDER BY qmr.availability_date
        """,
        [ticker],
    ).fetchall()
    history = []
    for fye, fq, availability_date in quarters:
        factor = compute_revenue_growth_acceleration(connection, ticker, fye, fq)
        if factor["status"] == "PASS":
            history.append({
                "availability_date": str(availability_date),
                "growth": factor["current_yoy_growth"], "trend": factor["value"],
            })
    return history


def _growth_as_of(history: list[dict], as_of_date: str) -> dict | None:
    known = [h for h in history if h["availability_date"] <= as_of_date]
    return known[-1] if known else None


def _price_series(connection: duckdb.DuckDBPyConnection, ticker: str) -> list[tuple[str, float]]:
    rows = connection.execute(
        "SELECT price_date, close FROM historical_prices_daily WHERE ticker = ? ORDER BY price_date", [ticker],
    ).fetchall()
    return [(str(d), c) for d, c in rows]


def _add_months(date_str: str, months: int) -> str:
    y, m, d = (int(x) for x in date_str.split("-"))
    total_months = (y * 12 + (m - 1)) + months
    ny, nm = divmod(total_months, 12)
    nm += 1
    try:
        return date(ny, nm, d).isoformat()
    except ValueError:
        return date(ny, nm + 1, 1).isoformat()


def _recovery_for(prices: list[tuple[str, float]], i: int, price_date: str, trailing_high: float) -> dict:
    latest_available_date = prices[-1][0]
    recovery = {}
    for horizon in RECOVERY_HORIZONS_MONTHS:
        target_date = _add_months(price_date, horizon)
        censored = target_date > latest_available_date
        recovered_on = None
        days_to_recovery = None
        for j in range(i, len(prices)):
            fwd_date, fwd_close = prices[j]
            if fwd_date > target_date:
                break
            if fwd_close >= trailing_high:
                recovered_on = fwd_date
                days_to_recovery = j - i
                break
        recovery[f"{horizon}mo"] = {
            "recovered": recovered_on is not None,
            "days_to_recovery": days_to_recovery,
            "censored": censored and recovered_on is None,
        }
    return recovery


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    tickers = _universe(connection)
    print(f"universe: {len(tickers)} tickers", flush=True)

    group_a, group_b = [], []
    t0 = time.perf_counter()
    for idx, ticker in enumerate(tickers, start=1):
        growth_history = _quarterly_growth_history(connection, ticker)
        prices = _price_series(connection, ticker)

        armed_a, armed_b = True, True
        for i, (price_date, close) in enumerate(prices):
            if i < TRAILING_HIGH_WINDOW_DAYS:
                continue
            window = prices[max(0, i - TRAILING_HIGH_WINDOW_DAYS):i]
            trailing_high = max(c for _, c in window)
            pullback = (trailing_high - close) / trailing_high
            growth_record = _growth_as_of(growth_history, price_date)
            growth = growth_record["growth"] if growth_record else None
            trend = growth_record["trend"] if growth_record else None

            if pullback < PULLBACK_THRESHOLD / 2:
                armed_a = True
                armed_b = True

            qualifies_a = growth is not None and growth > GROWTH_THRESHOLD and pullback >= PULLBACK_THRESHOLD
            qualifies_b = pullback >= PULLBACK_THRESHOLD  # control: price alone, no growth condition

            if qualifies_a and armed_a:
                recovery = _recovery_for(prices, i, price_date, trailing_high)
                group_a.append({
                    "ticker": ticker, "entry_date": price_date, "pullback": pullback,
                    "growth_as_of_entry": growth, "trend_as_of_entry": trend,
                    "accelerating": trend is not None and trend > 0, "recovery": recovery,
                })
                armed_a = False

            if qualifies_b and armed_b:
                recovery = _recovery_for(prices, i, price_date, trailing_high)
                group_b.append({
                    "ticker": ticker, "entry_date": price_date, "pullback": pullback,
                    "growth_as_of_entry": growth, "recovery": recovery,
                })
                armed_b = False

        if idx % 15 == 0:
            elapsed = time.perf_counter() - t0
            print(f"  {idx}/{len(tickers)} tickers processed ({elapsed:.0f}s elapsed)", flush=True)

    connection.close()
    print(f"\ndone: {time.perf_counter() - t0:.0f}s total", flush=True)

    def _summarize(label: str, episodes: list[dict]) -> dict:
        n = len(episodes)
        n_tickers = len({e["ticker"] for e in episodes})
        print(f"\n{label}: {n} episodes, {n_tickers} distinct tickers", flush=True)
        summary = {"n": n, "n_tickers": n_tickers, "by_horizon": {}}
        for h in ("6mo", "12mo", "24mo"):
            determinable = [e for e in episodes if not e["recovery"][h]["censored"]]
            recovered = [e for e in determinable if e["recovery"][h]["recovered"]]
            rate = len(recovered) / len(determinable) if determinable else None
            print(f"  recovered within {h}: {len(recovered)}/{len(determinable)}"
                  f" ({rate:.0%})" if rate is not None else f"  recovered within {h}: n/a", flush=True)
            summary["by_horizon"][h] = {"n_determinable": len(determinable), "n_recovered": len(recovered), "rate": rate}
        return summary

    summary_a = _summarize("GROUP A (growth>20% AND pullback>=15%)", group_a)
    summary_b = _summarize("GROUP B (pullback>=15% alone, control)", group_b)

    print("\nGroup A split by trend at entry:", flush=True)
    accel = [e for e in group_a if e["accelerating"]]
    decel = [e for e in group_a if not e["accelerating"]]
    trend_split = {}
    for label, group in [("accelerating", accel), ("decelerating", decel)]:
        result = {"n": len(group), "by_horizon": {}}
        line = f"  {label:<14} n={len(group)}"
        for h in ("6mo", "12mo", "24mo"):
            determinable = [e for e in group if not e["recovery"][h]["censored"]]
            recovered = [e for e in determinable if e["recovery"][h]["recovered"]]
            rate = len(recovered) / len(determinable) if determinable else None
            line += f"   {h}: {len(recovered)}/{len(determinable)}" + (f" ({rate:.0%})" if rate is not None else "")
            result["by_horizon"][h] = {"n_determinable": len(determinable), "n_recovered": len(recovered), "rate": rate}
        print(line, flush=True)
        trend_split[label] = result

    RESULT_PATH.write_text(json.dumps({
        "universe_size": len(tickers), "growth_threshold": GROWTH_THRESHOLD, "pullback_threshold": PULLBACK_THRESHOLD,
        "trailing_high_window_days": TRAILING_HIGH_WINDOW_DAYS,
        "group_a_summary": summary_a, "group_b_summary": summary_b, "group_a_trend_split": trend_split,
        "group_a_episodes": group_a, "group_b_episodes": group_b,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}", flush=True)


if __name__ == "__main__":
    main()
