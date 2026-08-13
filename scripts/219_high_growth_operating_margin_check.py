"""User-directed follow-up (2026-08-13 session) to D-080: within the
high-growth group that D-080 found actually shows a positive edge
(quarterly YoY revenue growth > 20%/yr), does splitting further by
operating margin (operating_income / revenue, already-extracted
quarterly metrics, no new extraction needed) reveal anything -- e.g. are
the winners in that group specifically the ones that are ALSO
profitable, or does growth alone carry the effect regardless of margin?

Deliberately scoped to ONLY the growth>20% subset (not the full 464-row
dataset) per the user's own framing: this is a follow-up question about
the group D-080 already flagged as relevant, not a fresh factor search.

EBITDA was also requested but is NOT available -- the quarterly engine
does not extract depreciation & amortization, so EBITDA cannot be
computed without new extraction work (out of scope for this quick
check, flagged separately).

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\219_high_growth_operating_margin_check.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

RESULT_PATH = DATA_DIR / "high_growth_operating_margin_check_result.json"
LOOKBACK_QUARTERS = 12
HORIZON_MONTHS = 12
GROWTH_THRESHOLD = 0.20


def _operating_margin(connection: duckdb.DuckDBPyConnection, ticker: str, fye: str, fq: str) -> float | None:
    row = connection.execute(
        """
        SELECT MAX(CASE WHEN metric_name = 'operating_income' THEN value END) AS op_income,
               MAX(CASE WHEN metric_name = 'revenue' THEN value END) AS revenue
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qer.ticker = ? AND qmr.fiscal_year_end = ? AND qmr.fiscal_quarter = ?
          AND qmr.result_status = 'PASS'
        """,
        [ticker, fye, fq],
    ).fetchone()
    if row is None or row[0] is None or row[1] in (None, 0):
        return None
    return row[0] / row[1]


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    cutoff_date = (date.today() - timedelta(days=int(LOOKBACK_QUARTERS * 365.25 / 4))).isoformat()
    quarters = connection.execute(
        """
        SELECT DISTINCT qer.ticker, qmr.fiscal_year_end, qmr.fiscal_quarter, qmr.availability_date
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qmr.availability_date >= ? AND qmr.metric_name = 'revenue'
        ORDER BY qer.ticker, qmr.availability_date
        """,
        [cutoff_date],
    ).fetchall()

    high_growth_rows = []
    for ticker, fye, fq, availability_date in quarters:
        availability_date = str(availability_date)
        factor = compute_revenue_growth_acceleration(connection, ticker, fye, fq)
        if factor["status"] != "PASS" or factor["current_yoy_growth"] is None:
            continue
        if factor["current_yoy_growth"] <= GROWTH_THRESHOLD:
            continue  # not in the relevant (>20%) group -- excluded, per the user's request

        margin = _operating_margin(connection, ticker, fye, fq)
        stock_fwd = _forward_return(connection, ticker, availability_date, HORIZON_MONTHS)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, availability_date, HORIZON_MONTHS)
        if stock_fwd is None or bench_fwd is None:
            continue

        high_growth_rows.append({
            "ticker": ticker, "fiscal_year_end": fye, "fiscal_quarter": fq, "availability_date": availability_date,
            "growth": factor["current_yoy_growth"], "operating_margin": margin,
            "excess_return": stock_fwd["return"] - bench_fwd["return"],
        })

    connection.close()

    print(f"growth>{GROWTH_THRESHOLD:.0%} entries with forward return resolved: {len(high_growth_rows)}")
    with_margin = [r for r in high_growth_rows if r["operating_margin"] is not None]
    print(f"of those, operating margin resolved: {len(with_margin)} "
          f"({len(high_growth_rows) - len(with_margin)} missing margin data)")

    profitable = [r for r in with_margin if r["operating_margin"] > 0]
    unprofitable = [r for r in with_margin if r["operating_margin"] <= 0]

    def _summary(label: str, rows: list[dict]) -> dict:
        if not rows:
            print(f"  {label:<38} n=0")
            return {"n": 0}
        excess = [r["excess_return"] for r in rows]
        mean_e = sum(excess) / len(excess)
        median_e = sorted(excess)[len(excess) // 2]
        win = sum(1 for e in excess if e > 0) / len(excess)
        n_tickers = len({r["ticker"] for r in rows})
        print(f"  {label:<38} n={len(rows):<4} tickers={n_tickers:<4} "
              f"mean_excess={mean_e:+.1%}  median_excess={median_e:+.1%}  win_rate={win:.0%}")
        return {"n": len(rows), "n_tickers": n_tickers, "mean_excess": mean_e, "median_excess": median_e, "win_rate": win}

    print(f"\nWithin the growth>{GROWTH_THRESHOLD:.0%} group, split by operating margin:")
    profitable_summary = _summary("operating margin > 0 (profitable)", profitable)
    unprofitable_summary = _summary("operating margin <= 0 (unprofitable)", unprofitable)

    print("\nFull detail, profitable group:")
    for r in sorted(profitable, key=lambda r: -r["excess_return"]):
        print(f"  {r['ticker']:<6} {r['availability_date']}  growth={r['growth']:+.1%}  "
              f"op_margin={r['operating_margin']:+.1%}  excess={r['excess_return']:+.1%}")

    print("\nFull detail, unprofitable group:")
    for r in sorted(unprofitable, key=lambda r: -r["excess_return"]):
        print(f"  {r['ticker']:<6} {r['availability_date']}  growth={r['growth']:+.1%}  "
              f"op_margin={r['operating_margin']:+.1%}  excess={r['excess_return']:+.1%}")

    payload = {
        "growth_threshold": GROWTH_THRESHOLD, "lookback_quarters": LOOKBACK_QUARTERS, "horizon_months": HORIZON_MONTHS,
        "n_high_growth": len(high_growth_rows), "n_with_margin": len(with_margin),
        "n_missing_margin": len(high_growth_rows) - len(with_margin),
        "profitable": profitable_summary, "unprofitable": unprofitable_summary,
        "note_ebitda": "EBITDA not available -- quarterly engine does not extract depreciation & amortization.",
        "rows": with_margin,
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
