"""User-directed follow-up (2026-08-13 session) to D-080: quintiles 1-4
(growth <= ~20%/yr) showed a NEGATIVE median excess return in aggregate
-- but "negative median" does not mean every entry in that group lost.
This finds the individual company-quarters that broke the pattern
(growth <= 20% but still beat QQQ over the next 12 months) and checks
whether operating margin, or which specific tickers/periods, explain
why -- the same kind of question D-080's quintile breakdown itself
answered for the high-growth side, applied to the low-growth side.

Reuses D-079/D-080's exact dataset (same 464-row baseline, same 12-
quarter lookback / 12-month horizon) rather than rebuilding it, so the
"low growth" and "high growth" pictures are directly comparable.

READ-ONLY. Writes one result JSON.

    .venv\\Scripts\\python.exe scripts\\222_low_growth_winners_check.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH

SOURCE_PATH = DATA_DIR / "quarterly_growth_rate_regime_and_quintile_check_result.json"
RESULT_PATH = DATA_DIR / "low_growth_winners_check_result.json"
GROWTH_THRESHOLD = 0.20


def _operating_margin(connection: duckdb.DuckDBPyConnection, ticker: str, availability_date: str) -> float | None:
    """D-079/D-080's own dataset rows carry (ticker, availability_date),
    not (fiscal_year_end, fiscal_quarter) -- look up by the same key
    the source dataset actually has, not one it doesn't."""
    row = connection.execute(
        """
        SELECT MAX(CASE WHEN metric_name = 'operating_income' THEN value END) AS op_income,
               MAX(CASE WHEN metric_name = 'revenue' THEN value END) AS revenue
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qer.ticker = ? AND qmr.availability_date = ?
          AND qmr.result_status = 'PASS'
        """,
        [ticker, availability_date],
    ).fetchone()
    if row is None or row[0] is None or row[1] in (None, 0):
        return None
    return row[0] / row[1]


def main() -> None:
    dataset = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))["dataset"]
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    low_growth_winners = [
        r for r in dataset if r["current_yoy_growth"] <= GROWTH_THRESHOLD and r["excess_return"] > 0
    ]
    low_growth_losers = [
        r for r in dataset if r["current_yoy_growth"] <= GROWTH_THRESHOLD and r["excess_return"] <= 0
    ]
    print(f"low-growth (<= {GROWTH_THRESHOLD:.0%}) entries: {len(low_growth_winners) + len(low_growth_losers)}")
    print(f"  winners (beat QQQ anyway): {len(low_growth_winners)}")
    print(f"  losers: {len(low_growth_losers)}")

    for r in low_growth_winners:
        r["operating_margin"] = _operating_margin(connection, r["ticker"], r["availability_date"])
    for r in low_growth_losers:
        r["operating_margin"] = _operating_margin(connection, r["ticker"], r["availability_date"])

    connection.close()

    print("\n" + "=" * 100)
    print("Low-growth WINNERS -- detail, sorted by excess return")
    print("=" * 100)
    for r in sorted(low_growth_winners, key=lambda r: -r["excess_return"]):
        margin_str = f"{r['operating_margin']:+.1%}" if r["operating_margin"] is not None else "n/a"
        print(f"  {r['ticker']:<6} {r['availability_date']}  growth={r['current_yoy_growth']:+.1%}  "
              f"op_margin={margin_str:<8}  excess_return={r['excess_return']:+.1%}")

    # Compare margin distributions: winners vs losers, both within the low-growth group.
    winner_margins = [r["operating_margin"] for r in low_growth_winners if r["operating_margin"] is not None]
    loser_margins = [r["operating_margin"] for r in low_growth_losers if r["operating_margin"] is not None]

    def _avg(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    print("\n" + "=" * 100)
    print("Does operating margin distinguish low-growth winners from low-growth losers?")
    print("=" * 100)
    print(f"  winners: n={len(winner_margins)}  avg_op_margin={_avg(winner_margins):+.1%}" if winner_margins else "  winners: no margin data")
    print(f"  losers:  n={len(loser_margins)}  avg_op_margin={_avg(loser_margins):+.1%}" if loser_margins else "  losers: no margin data")

    winner_tickers = sorted({r["ticker"] for r in low_growth_winners})
    print(f"\ndistinct tickers among low-growth winners ({len(winner_tickers)}): {', '.join(winner_tickers)}")

    from collections import Counter
    ticker_counts = Counter(r["ticker"] for r in low_growth_winners)
    print("\nrepeat winners (appeared more than once):")
    for ticker, count in ticker_counts.most_common():
        if count > 1:
            print(f"  {ticker}: {count} winning low-growth quarters")

    RESULT_PATH.write_text(json.dumps({
        "growth_threshold": GROWTH_THRESHOLD,
        "n_low_growth_winners": len(low_growth_winners), "n_low_growth_losers": len(low_growth_losers),
        "winner_avg_operating_margin": _avg(winner_margins), "loser_avg_operating_margin": _avg(loser_margins),
        "low_growth_winners": low_growth_winners, "low_growth_losers": low_growth_losers,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
