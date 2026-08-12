"""
scoring/quarterly_trend_v1.py -- a quarterly-cadence growth-momentum
factor, per the user's own instruction (2026-08-12 session): quarterly
data can reveal trends before they're fully priced in, which a single
annual snapshot misses. Built ONLY after confirming (a) Quarterly Data
V1 (D-042) is point-in-time safe (availability_date-gated, same
discipline as everything else in this project) and (b) it currently
covers only the original 9 tickers -- 10-Q filings were never locked
for the wider 143-company expansion, so this factor is proved here on
the 9 tickers first (small proof before a full system), not assumed to
generalize.

**revenue growth ACCELERATION**, not the growth rate itself (which
Scoring Inputs V1's annual `revenue_growth` already captures):

    yoy_growth(q)     = revenue(q) / revenue(q - 4 quarters) - 1
    acceleration(q)   = yoy_growth(q) - yoy_growth(q - 1 quarter)

Positive acceleration means the YEAR-OVER-YEAR growth rate itself is
rising quarter to quarter (growth speeding up); negative means it's
decelerating. Using YoY (not sequential quarter-over-quarter) avoids
seasonality confusing the signal -- comparing Q4 to Q3 would conflate a
real trend with a normal seasonal pattern; comparing Q4-this-year's YoY
rate to Q3-this-year's YoY rate does not.

Needs 5 full quarters of revenue history before the entry quarter (4
for the entry quarter's own YoY figure, 1 more for the PRIOR quarter's
YoY figure to compare against) -- returns None when that history isn't
available, never guessed.
"""

from __future__ import annotations

import duckdb

QUARTER_ORDER = ["Q1", "Q2", "Q3", "Q4"]


def _quarter_index(fiscal_year_end: str, fiscal_quarter: str) -> tuple[int, int]:
    """Orders quarters chronologically using the fiscal_year_end's own
    year and the quarter label -- correct for any fiscal-year-end month,
    since fiscal_year_end already encodes the company's own calendar."""
    year = int(fiscal_year_end[:4])
    return year, QUARTER_ORDER.index(fiscal_quarter)


def _quarters_before(
    connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year_end: str, fiscal_quarter: str, n_back: int
) -> list[tuple[str, str]]:
    """All (fiscal_year_end, fiscal_quarter) pairs this ticker has,
    chronologically ordered, ending just before the given quarter --
    used to walk back N quarters regardless of any gaps."""
    rows = connection.execute(
        """
        SELECT DISTINCT qmr.fiscal_year_end, qmr.fiscal_quarter
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qer.ticker = ?
        """,
        [ticker],
    ).fetchall()
    ordered = sorted({(str(fye), str(q)) for fye, q in rows}, key=lambda t: _quarter_index(t[0], t[1]))
    target_idx = _quarter_index(fiscal_year_end, fiscal_quarter)
    earlier = [t for t in ordered if _quarter_index(*t) < target_idx]
    return earlier[-n_back:] if len(earlier) >= n_back else []


def _revenue(connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year_end: str, fiscal_quarter: str) -> float | None:
    row = connection.execute(
        """
        SELECT qmr.value FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qer.ticker = ? AND qmr.fiscal_year_end = ? AND qmr.fiscal_quarter = ?
          AND qmr.metric_name = 'revenue' AND qmr.value IS NOT NULL
        """,
        [ticker, fiscal_year_end, fiscal_quarter],
    ).fetchone()
    return float(row[0]) if row is not None else None


def _yoy_growth(connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year_end: str, fiscal_quarter: str) -> float | None:
    current = _revenue(connection, ticker, fiscal_year_end, fiscal_quarter)
    prior_year_quarters = _quarters_before(connection, ticker, fiscal_year_end, fiscal_quarter, 4)
    if current is None or len(prior_year_quarters) < 4:
        return None
    prior_fye, prior_q = prior_year_quarters[0]
    prior = _revenue(connection, ticker, prior_fye, prior_q)
    if prior is None or prior == 0:
        return None
    return current / prior - 1


def compute_revenue_growth_acceleration(
    connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year_end: str, fiscal_quarter: str
) -> dict[str, object]:
    """Returns {status, value} -- value is the acceleration figure (or
    None); status explains why when it's None. Never guesses when
    history is short."""
    quarters_before = _quarters_before(connection, ticker, fiscal_year_end, fiscal_quarter, 1)
    if not quarters_before:
        return {"status": "NO_PRIOR_QUARTER", "value": None}
    prior_fye, prior_q = quarters_before[0]

    current_yoy = _yoy_growth(connection, ticker, fiscal_year_end, fiscal_quarter)
    prior_yoy = _yoy_growth(connection, ticker, prior_fye, prior_q)
    if current_yoy is None or prior_yoy is None:
        return {"status": "INSUFFICIENT_HISTORY", "value": None}

    return {
        "status": "PASS", "value": current_yoy - prior_yoy,
        "current_yoy_growth": current_yoy, "prior_quarter_yoy_growth": prior_yoy,
    }
