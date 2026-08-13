"""User-directed small proof (2026-08-13 session), before scaling to the
full ~92-company universe: does a stock trading down from its own
1-year high, WHILE its most recently disclosed quarterly YoY revenue
growth exceeds 20% (the threshold D-080 already found relevant), tend
to recover back to that high within 6/12/24 months?

**Entry point is evaluated at EVERY trading day, not only at filing
availability_date** -- a real methodology shift from D-079/D-080's
quarterly-only entry points, per the user's explicit request. Point-in-
time safety is preserved: for any given trading day, the growth figure
used is whichever quarter's own availability_date is the most recent
one on or before that day -- never a later quarter's figure.

**Uses split-ADJUSTED `close`, not `nominal_close`.** This is a pure
price-to-its-own-history comparison (today's price vs. this SAME
ticker's own trailing high) -- unlike an EPS/P/E ratio, there is no
cross-series scale-matching concern here, and adjusted `close` avoids a
real distortion nominal_close would introduce: a stock that split
between its trailing high and today would show a nominal-terms "high"
that is not economically comparable to today's nominal price.

**Trailing high**: rolling 252-trading-day (~1 year) high, not an all-
time high -- a deliberate, stated choice, different from inputs_v1.py's
existing `distance_from_high` factor (which uses an unbounded all-time
high). An all-time high from years ago, possibly from a single volatile
spike, is a much less meaningful recovery target than a recent 52-week
high; this is the standard "distance from 52-week high" convention.

**Episode deduplication**: consecutive trading days that all satisfy
both conditions (growth>threshold AND pullback>=threshold) are collapsed
into ONE entry, the first qualifying day -- otherwise a single 3-week
decline would be counted as ~15 near-identical "entries", wildly
overstating the sample and understating how autocorrelated adjacent
days are.

5-company proof only (RKLB, MU, APP, MDB, NVDA) -- a deliberately mixed
set: 3 of D-080's strongest growth>20% winners, 1 instructive counter-
example (MDB, which had negative excess returns in several of its own
growth>20% QUARTERS), and 1 large, long-history steady grower.

READ-ONLY. Writes one result JSON. Prints full per-episode detail.

    .venv\\Scripts\\python.exe scripts\\220_pullback_recovery_proof.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

RESULT_PATH = DATA_DIR / "pullback_recovery_proof_result.json"

TICKERS = ["RKLB", "MU", "APP", "MDB", "NVDA"]
GROWTH_THRESHOLD = 0.20
PULLBACK_THRESHOLD = 0.15
TRAILING_HIGH_WINDOW_DAYS = 252
RECOVERY_HORIZONS_MONTHS = (6, 12, 24)


def _quarterly_growth_history(connection: duckdb.DuckDBPyConnection, ticker: str) -> list[dict]:
    """Every (availability_date, growth, trend) this ticker has,
    chronologically -- ALL history, not just the last 12 quarters, since
    we need to know what was knowable as of any arbitrary past trading
    day. `trend` is the SAME acceleration figure D-065/D-079 already
    computed and found NOT predictive on its own (85/86 leave-one-out
    exclusions flipped it) -- captured here not as a standalone filter,
    but as a second dimension WITHIN the already-validated growth>20%
    group, per the user's own framing: does a decelerating high-grower
    behave differently from an accelerating one at the SAME growth level?
    Only includes quarters where both growth AND trend are resolvable
    (status=='PASS' requires 5 quarters of history, one more than growth
    alone needs) -- a small, deliberate scoping choice for this proof."""
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
                "growth": factor["current_yoy_growth"],
                "trend": factor["value"],  # positive = accelerating, negative = decelerating
            })
    return history


def _growth_as_of(history: list[dict], as_of_date: str) -> dict | None:
    """Point-in-time lookup: the most recent (growth, trend) record whose
    availability_date <= as_of_date. None if no quarter was known yet."""
    known = [h for h in history if h["availability_date"] <= as_of_date]
    return known[-1] if known else None


def _price_series(connection: duckdb.DuckDBPyConnection, ticker: str) -> list[tuple[str, float]]:
    rows = connection.execute(
        "SELECT price_date, close FROM historical_prices_daily WHERE ticker = ? ORDER BY price_date",
        [ticker],
    ).fetchall()
    return [(str(d), c) for d, c in rows]


def _add_months(date_str: str, months: int) -> str:
    from datetime import date
    y, m, d = (int(x) for x in date_str.split("-"))
    total_months = (y * 12 + (m - 1)) + months
    ny, nm = divmod(total_months, 12)
    nm += 1
    try:
        return date(ny, nm, d).isoformat()
    except ValueError:
        return date(ny, nm + 1, 1).isoformat()  # month-end overflow, e.g. Feb 30 -> Mar 1


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    all_episodes = []
    for ticker in TICKERS:
        growth_history = _quarterly_growth_history(connection, ticker)
        prices = _price_series(connection, ticker)
        print(f"\n{ticker}: {len(growth_history)} quarterly growth points, {len(prices)} daily price rows")

        # Hysteresis, not a simple "consecutive days" dedup: a plain
        # qualifies-flag transition re-triggers a "new" episode every time
        # pullback flickers a fraction of a percent either side of
        # PULLBACK_THRESHOLD (measured: RKLB alone produced 4 "episodes"
        # within one 3-week span this way, all really the same decline).
        # `armed` only resets once pullback has meaningfully recovered
        # (below half the threshold) -- a real, separate decline, not
        # daily noise around the boundary.
        armed = True
        for i, (price_date, close) in enumerate(prices):
            if i < TRAILING_HIGH_WINDOW_DAYS:
                continue  # not enough history yet for a real trailing high
            window = prices[max(0, i - TRAILING_HIGH_WINDOW_DAYS):i]
            trailing_high = max(c for _, c in window)
            pullback = (trailing_high - close) / trailing_high
            growth_record = _growth_as_of(growth_history, price_date)
            growth = growth_record["growth"] if growth_record else None
            trend = growth_record["trend"] if growth_record else None

            if pullback < PULLBACK_THRESHOLD / 2:
                armed = True

            qualifies = growth is not None and growth > GROWTH_THRESHOLD and pullback >= PULLBACK_THRESHOLD

            if qualifies and armed:
                # New episode -- record it, then check recovery at each horizon.
                # A horizon whose target_date is beyond the latest available
                # price data is CENSORED, not a confirmed non-recovery --
                # e.g. an entry from last month cannot yet be known to have
                # "failed" its 24-month check. Distinguishing this matters:
                # without it, every very recent entry silently counts as a
                # failure just because not enough time has passed yet.
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
                        "recovered_date": recovered_on,
                        "days_to_recovery": days_to_recovery,
                        "censored": censored and recovered_on is None,
                    }
                all_episodes.append({
                    "ticker": ticker, "entry_date": price_date, "close": close,
                    "trailing_252d_high": trailing_high, "pullback": pullback, "growth_as_of_entry": growth,
                    "trend_as_of_entry": trend, "accelerating": trend is not None and trend > 0,
                    "recovery": recovery,
                })
                armed = False

    connection.close()

    print("\n" + "=" * 110)
    print(f"Episodes found: growth>{GROWTH_THRESHOLD:.0%} AND pullback>={PULLBACK_THRESHOLD:.0%} from 252-day high")
    print("=" * 110)
    for e in all_episodes:
        r = e["recovery"]
        flags = "  ".join(
            f"{h}: {'YES (' + str(r[h]['days_to_recovery']) + 'd)' if r[h]['recovered'] else ('too soon to know' if r[h]['censored'] else 'no')}"
            for h in ("6mo", "12mo", "24mo")
        )
        trend_flag = "accelerating" if e["accelerating"] else "decelerating"
        print(f"  {e['ticker']:<6} {e['entry_date']}  pullback={e['pullback']:.1%}  "
              f"growth={e['growth_as_of_entry']:+.1%} ({trend_flag}, trend={e['trend_as_of_entry']:+.1%})  "
              f"recovery: {flags}")

    n = len(all_episodes)
    print(f"\ntotal episodes: {n}")
    for h in ("6mo", "12mo", "24mo"):
        determinable = [e for e in all_episodes if not e["recovery"][h]["censored"]]
        recovered = [e for e in determinable if e["recovery"][h]["recovered"]]
        n_censored = n - len(determinable)
        print(f"  recovered within {h}: {len(recovered)}/{len(determinable)}"
              f"{f'  ({n_censored} too recent to know yet, excluded)' if n_censored else ''}")

    print("\nSplit by trend at entry (same growth>20% group -- does accelerating vs. decelerating matter?):")
    accel = [e for e in all_episodes if e["accelerating"]]
    decel = [e for e in all_episodes if not e["accelerating"]]
    for label, group in [("accelerating growth", accel), ("decelerating growth", decel)]:
        if not group:
            print(f"  {label:<22} n=0")
            continue
        line = f"  {label:<22} n={len(group)}"
        for h in ("6mo", "12mo", "24mo"):
            determinable = [e for e in group if not e["recovery"][h]["censored"]]
            rec = sum(1 for e in determinable if e["recovery"][h]["recovered"])
            line += f"   {h}: {rec}/{len(determinable)}"
        print(line)

    RESULT_PATH.write_text(json.dumps({
        "tickers": TICKERS, "growth_threshold": GROWTH_THRESHOLD, "pullback_threshold": PULLBACK_THRESHOLD,
        "trailing_high_window_days": TRAILING_HIGH_WINDOW_DAYS, "episodes": all_episodes,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
