"""User-directed follow-up (2026-08-13 session) to scripts/224's "which
companies currently pass the growth>20% + pullback>=15% screen" readout:
before treating that current list as actionable, check each of those 14
tickers' OWN history over the last 12 quarters (this project's binding
lookback, D-064/D-075) -- whenever a point in time showed BOTH an
accelerating growth trend (current YoY growth > prior quarter's YoY
growth, i.e. compute_revenue_growth_acceleration's trend > 0) at
growth>20%, AND a pullback >=15% from the trailing 252-day high, did the
stock go on to actually reach a new high?

This is D-081's own "Group A, accelerating" cut (scripts/220/221),
narrowed to exactly these 14 tickers and to the last 12 quarters only
(scripts/221 deliberately used full available history; this run applies
the project's standard lookback instead, since the question here is
about these specific candidates' recent track record, not the broadest
possible sample).

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\226_current_candidates_historical_accel_pullback_check.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

SOURCE_PATH = DATA_DIR / "current_growth_screen_and_entry_points_result.json"
RESULT_PATH = DATA_DIR / "current_candidates_historical_accel_pullback_check_result.json"

GROWTH_THRESHOLD = 0.20
PULLBACK_THRESHOLD = 0.15
TRAILING_HIGH_WINDOW_DAYS = 252
LOOKBACK_QUARTERS = 12
RECOVERY_HORIZONS_MONTHS = (6, 12, 24)


def _lookback_cutoff() -> str:
    return (date.today() - timedelta(days=int(LOOKBACK_QUARTERS * 365.25 / 4))).isoformat()


def _growth_history(connection: duckdb.DuckDBPyConnection, ticker: str, cutoff_date: str) -> list[dict]:
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
    return [h for h in history if h["availability_date"] >= cutoff_date]


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
            "recovered_on": recovered_on,
            "days_to_recovery": days_to_recovery,
            "censored": censored and recovered_on is None,
        }
    return recovery


def main() -> None:
    candidates = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))["qualifying_companies"]
    tickers = sorted(r["ticker"] for r in candidates if r["entry_signal_now"])
    cutoff_date = _lookback_cutoff()
    print(f"tickers (currently passing growth>20% + pullback>=15% today): {tickers}")
    print(f"lookback cutoff ({LOOKBACK_QUARTERS} quarters back): {cutoff_date}\n")

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    per_ticker: dict[str, list[dict]] = {}
    all_episodes: list[dict] = []
    for ticker in tickers:
        growth_history = _growth_history(connection, ticker, cutoff_date)
        prices = [(d, c) for d, c in _price_series(connection, ticker) if d >= cutoff_date]
        episodes = []
        armed = True
        for i, (price_date, close) in enumerate(prices):
            if i < TRAILING_HIGH_WINDOW_DAYS:
                continue
            window = prices[max(0, i - TRAILING_HIGH_WINDOW_DAYS):i]
            trailing_high = max(c for _, c in window)
            pullback = (trailing_high - close) / trailing_high
            growth_record = _growth_as_of(growth_history, price_date)

            if pullback < PULLBACK_THRESHOLD / 2:
                armed = True

            qualifies = (
                growth_record is not None
                and growth_record["growth"] > GROWTH_THRESHOLD
                and growth_record["trend"] is not None and growth_record["trend"] > 0
                and pullback >= PULLBACK_THRESHOLD
            )
            if qualifies and armed:
                recovery = _recovery_for(prices, i, price_date, trailing_high)
                episode = {
                    "ticker": ticker, "entry_date": price_date, "pullback": pullback,
                    "growth_as_of_entry": growth_record["growth"], "trend_as_of_entry": growth_record["trend"],
                    "recovery": recovery,
                }
                episodes.append(episode)
                all_episodes.append(episode)
                armed = False

        per_ticker[ticker] = episodes
        if episodes:
            for e in episodes:
                r12 = e["recovery"]["12mo"]
                status = "RECOVERED" if r12["recovered"] else ("censored/too soon" if r12["censored"] else "did NOT recover")
                print(f"  {ticker:<6} entry={e['entry_date']}  pullback={e['pullback']:+.1%}  "
                      f"growth={e['growth_as_of_entry']:+.1%}  trend={e['trend_as_of_entry']:+.3f}  12mo: {status}")
        else:
            print(f"  {ticker:<6} no qualifying episode (accelerating growth>20% + pullback>=15%) "
                  f"in the last {LOOKBACK_QUARTERS} quarters")

    connection.close()

    print(f"\n{len(all_episodes)} qualifying episodes across {sum(1 for t in per_ticker if per_ticker[t])} of "
          f"{len(tickers)} tickers.")
    summary = {"by_horizon": {}}
    for h in ("6mo", "12mo", "24mo"):
        determinable = [e for e in all_episodes if not e["recovery"][h]["censored"]]
        recovered = [e for e in determinable if e["recovery"][h]["recovered"]]
        rate = len(recovered) / len(determinable) if determinable else None
        print(f"  recovered to a new high within {h}: {len(recovered)}/{len(determinable)}"
              + (f" ({rate:.0%})" if rate is not None else " (n/a)"))
        summary["by_horizon"][h] = {"n_determinable": len(determinable), "n_recovered": len(recovered), "rate": rate}

    RESULT_PATH.write_text(json.dumps({
        "tickers": tickers, "lookback_quarters": LOOKBACK_QUARTERS, "cutoff_date": cutoff_date,
        "growth_threshold": GROWTH_THRESHOLD, "pullback_threshold": PULLBACK_THRESHOLD,
        "n_episodes": len(all_episodes), "summary": summary,
        "per_ticker": per_ticker, "all_episodes": all_episodes,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
