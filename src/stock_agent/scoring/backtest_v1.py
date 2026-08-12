"""
scoring/backtest_v1.py -- point-in-time backtest for Scoring Model V1
(docs/SCORING_MODEL_V1_BLUEPRINT.md Stage 6 step 5), measured against
QQQ (the Nasdaq-100 tracking ETF, docs/DECISIONS_LOG.md's benchmark
load) per the user's stated current-stage goal (excess return over
Nasdaq-100).

*** READ THIS BEFORE TRUSTING ANY NUMBER THIS MODULE PRODUCES ***

**Severe survivorship bias, by construction, not an oversight.** The
9-company universe (docs/PROJECT_CONTEXT.md's original watchlist:
ORCL, MSFT, META, NVDA, GOOGL, AMZN, MU, CRWD, PANW) was hand-picked as
an initial watchlist of already-prominent companies, NOT selected via
any point-in-time, objective process (e.g. "Nasdaq-100 constituents as
of date X"). Every one of these 9 is, with the benefit of hindsight, a
company that did well over the 2020-2026 window. A backtest confined to
exactly these 9 will tend to show good absolute returns almost
regardless of the scoring methodology, because the universe itself was
chosen by hindsight. This module's numbers say, AT MOST, "does the
score differentiate winners from losers WITHIN this pre-selected group"
-- they say nothing trustworthy about whether Scoring Model V1 would
identify winners from an unbiased universe. A genuine test needs the
survivorship-free ~150-company universe already loaded in production
from an earlier session (docs/DECISIONS_LOG.md D-051-D-056) -- not
built here; flagged as the obvious next step.

**Small-sample statistics.** Each fiscal year is one independent entry
point; there are only 5-6 of them. Hit rate, mean/median excess return
are reported as descriptive numbers, not statistically significant
estimates. No max-drawdown or volatility figure is computed -- both
need a genuinely continuous daily-return path, which a handful of
independent annual entry points cannot honestly provide; fabricating
one here would violate this project's explicit "do not guess" rule.

**No overlapping-period compounding is claimed.** Each (entry fiscal
year, holding horizon) pair is reported as its own independent
observation -- this module never claims a single continuously-
compounded multi-year equity curve, which would require careful
handling of overlapping holding periods this proof does not attempt.

Returns use `adj_close` (Yahoo's dividend- and split-adjusted series)
consistently for BOTH entry and exit, for both the strategy and QQQ --
the correct, standard basis for a total-return comparison (as opposed
to `nominal_close`, which Entry Price Method 1 uses instead, for a
different purpose: reconstructing the actual quoted price at one
specific historical moment).
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb

HORIZON_MONTHS = (6, 12, 24, 36)
BENCHMARK_TICKER = "QQQ"


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _adj_close_on_or_after(connection: duckdb.DuckDBPyConnection, ticker: str, target_date: str) -> tuple[float | None, str | None]:
    row = connection.execute(
        "SELECT price_date, adj_close FROM historical_prices_daily "
        "WHERE ticker = ? AND price_date >= ? ORDER BY price_date ASC LIMIT 1",
        [ticker, target_date],
    ).fetchone()
    if row is None:
        return None, None
    return float(row[1]), str(row[0])


def _adj_close_on_or_before(connection: duckdb.DuckDBPyConnection, ticker: str, target_date: str) -> tuple[float | None, str | None]:
    row = connection.execute(
        "SELECT price_date, adj_close FROM historical_prices_daily "
        "WHERE ticker = ? AND price_date <= ? ORDER BY price_date DESC LIMIT 1",
        [ticker, target_date],
    ).fetchone()
    if row is None:
        return None, None
    return float(row[1]), str(row[0])


def _forward_return(
    connection: duckdb.DuckDBPyConnection, ticker: str, entry_date: str, horizon_months: int
) -> dict | None:
    entry_price, entry_price_date = _adj_close_on_or_before(connection, ticker, entry_date)
    if entry_price is None:
        return None
    target_exit = _add_months(date.fromisoformat(entry_date), horizon_months).isoformat()
    exit_price, exit_price_date = _adj_close_on_or_after(connection, ticker, target_exit)
    if exit_price is None:
        return None  # target exit date is beyond available price data -- not fabricated
    return {
        "entry_price_date": entry_price_date, "entry_price": entry_price,
        "exit_price_date": exit_price_date, "exit_price": exit_price,
        "return": exit_price / entry_price - 1,
    }


def run_top_bottom_backtest_v1(
    connection: duckdb.DuckDBPyConnection, top_n: int = 3
) -> dict[str, object]:
    """For each fiscal year with at least `top_n` ranked companies,
    selects the top-N and bottom-N by composite_score (equal weight,
    entered at each company's OWN filing_date), and measures forward
    return at each of HORIZON_MONTHS against QQQ over the SAME exact
    entry/exit dates. See module docstring for what this can and cannot
    tell you before reading any number below."""

    composite_rows = connection.execute(
        "SELECT ticker, report_date, fiscal_year, composite_score FROM scoring_composite_v1 "
        "WHERE composite_score IS NOT NULL ORDER BY fiscal_year, composite_score DESC"
    ).fetchall()

    filing_dates = dict(connection.execute(
        "SELECT ticker || '|' || report_date, filing_date FROM sec_filings"
    ).fetchall())

    by_year: dict[int, list[tuple[str, str, float]]] = {}
    for ticker, report_date, fiscal_year, score in composite_rows:
        by_year.setdefault(fiscal_year, []).append((ticker, str(report_date), float(score)))

    per_year_results = []
    for fiscal_year in sorted(by_year):
        ranked = sorted(by_year[fiscal_year], key=lambda r: r[2], reverse=True)
        if len(ranked) < top_n:
            continue
        top = ranked[:top_n]
        bottom = ranked[-top_n:]

        year_result: dict[str, object] = {"fiscal_year": fiscal_year, "universe_size": len(ranked), "horizons": {}}
        for horizon in HORIZON_MONTHS:
            top_returns, bottom_returns, benchmark_returns = [], [], []
            detail = []
            for group_name, group in (("top", top), ("bottom", bottom)):
                for ticker, report_date, score in group:
                    filing_date = filing_dates.get(f"{ticker}|{report_date}")
                    if filing_date is None:
                        continue
                    filing_date = str(filing_date)
                    stock_fwd = _forward_return(connection, ticker, filing_date, horizon)
                    bench_fwd = _forward_return(connection, BENCHMARK_TICKER, filing_date, horizon)
                    if stock_fwd is None or bench_fwd is None:
                        continue
                    (top_returns if group_name == "top" else bottom_returns).append(stock_fwd["return"])
                    benchmark_returns.append(bench_fwd["return"])
                    detail.append({
                        "group": group_name, "ticker": ticker, "composite_score": score,
                        "filing_date": filing_date, "stock_return": stock_fwd["return"],
                        "benchmark_return": bench_fwd["return"],
                        "excess_return": stock_fwd["return"] - bench_fwd["return"],
                    })
            if not top_returns and not bottom_returns:
                continue
            year_result["horizons"][horizon] = {
                "top_avg_return": sum(top_returns) / len(top_returns) if top_returns else None,
                "bottom_avg_return": sum(bottom_returns) / len(bottom_returns) if bottom_returns else None,
                "benchmark_avg_return": sum(benchmark_returns) / len(benchmark_returns) if benchmark_returns else None,
                "top_excess_vs_benchmark": (
                    (sum(top_returns) / len(top_returns)) - (sum(benchmark_returns) / len(benchmark_returns))
                    if top_returns and benchmark_returns else None
                ),
                "top_minus_bottom_spread": (
                    (sum(top_returns) / len(top_returns)) - (sum(bottom_returns) / len(bottom_returns))
                    if top_returns and bottom_returns else None
                ),
                "detail": detail,
            }
        per_year_results.append(year_result)

    # Aggregate hit rate + mean/median excess return per horizon, across
    # independent entry years -- NOT a compounded equity curve.
    horizon_summary = {}
    for horizon in HORIZON_MONTHS:
        excess_values = [
            yr["horizons"][horizon]["top_excess_vs_benchmark"]
            for yr in per_year_results
            if horizon in yr["horizons"] and yr["horizons"][horizon]["top_excess_vs_benchmark"] is not None
        ]
        if not excess_values:
            horizon_summary[horizon] = {"observations": 0}
            continue
        hits = sum(1 for v in excess_values if v > 0)
        sorted_values = sorted(excess_values)
        horizon_summary[horizon] = {
            "observations": len(excess_values),
            "hit_rate_vs_benchmark": hits / len(excess_values),
            "mean_excess_return": sum(excess_values) / len(excess_values),
            "median_excess_return": sorted_values[len(sorted_values) // 2],
        }

    return {
        "top_n": top_n,
        "per_year": per_year_results,
        "horizon_summary": horizon_summary,
    }
