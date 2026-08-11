"""
scoring/entry_price_v1.py -- Entry Price Method 1
(docs/SCORING_MODEL_V1_BLUEPRINT.md Stage 4): a company's own P/E,
evaluated at its own 10-K filing_date, compared against its OWN
trailing (up to 5-year) P/E history -- no external estimate, no peer
comparison, no shares-outstanding extraction needed (see the module
docstring below for why).

The blueprint originally listed "shares outstanding" as the
prerequisite this method was blocked on. It no longer is: D-046
(docs/DECISIONS_LOG.md) already resolved reported DILUTED EPS directly
for all 45 company-years from the filings' own per-share fact --
`net_income / shares` was only ever a transient cross-check, never
needed as a stored input. P/E = price / diluted_eps needs no shares
count at all. This module supersedes Stage 6 step 1 for the P/E use
case (P/FCF, the blueprint's other candidate ratio, would still need a
share count -- not built here, since P/E alone is a complete,
already-unblocked Method 1 implementation).

**Critical, previously-measured pitfall (D-046) reused here**: diluted
EPS is always AS-REPORTED, never retroactively split-adjusted. Pairing
it with a retroactively split-adjusted price (`historical_prices_daily
.close`) silently distorts every P/E for a company-year preceding a
later split -- D-046 measured this exact bug on NVDA (P/E 5.66 instead
of 56.56, a 10x error) before fixing it. GOOGL's 2021 diluted EPS
(112.20, vs. 4.56-10.81 in adjacent years, from its 2022 20:1 split) is
the same trap in this dataset. This module ONLY ever uses
`nominal_close` (the as-quoted, non-retroactively-adjusted price),
never `close`.

No entry/buy threshold is hard-coded here. This module reports the
company's own P/E percentile position within its own trailing history
as DATA -- a continuous number -- not a binary "buy" signal. Turning a
specific percentile cutoff into an actual entry rule is a policy
decision requiring its own backtest and calibration (per CLAUDE.md:
every model proposal must be measurable and backtested, and this
project must not invent an unvalidated threshold).
"""

from __future__ import annotations

import duckdb

TRAILING_YEARS = 5


def _price_at_or_before(connection: duckdb.DuckDBPyConnection, ticker: str, as_of_date: str) -> tuple[float | None, str | None]:
    row = connection.execute(
        "SELECT price_date, nominal_close FROM historical_prices_daily "
        "WHERE ticker = ? AND price_date <= ? ORDER BY price_date DESC LIMIT 1",
        [ticker, as_of_date],
    ).fetchone()
    if row is None:
        return None, None
    return float(row[1]), str(row[0])


def _pe_for_fiscal_year(connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year: int) -> dict | None:
    row = connection.execute(
        "SELECT fiscal_year_end, diluted_eps, filing_date FROM valuation_v1_per_share_inputs "
        "WHERE ticker = ? AND fiscal_year = ?",
        [ticker, fiscal_year],
    ).fetchone()
    if row is None:
        return None
    fiscal_year_end, diluted_eps, filing_date = str(row[0]), float(row[1]), str(row[2])

    price, price_date = _price_at_or_before(connection, ticker, filing_date)
    if price is None:
        return {
            "fiscal_year": fiscal_year, "fiscal_year_end": fiscal_year_end, "filing_date": filing_date,
            "diluted_eps": diluted_eps, "price": None, "price_date": None,
            "pe": None, "pe_status": "NO_PRICE_DATA",
        }

    if diluted_eps <= 0:
        pe, status = None, "UNDEFINED_NONPOSITIVE_EPS"
    else:
        pe, status = price / diluted_eps, "PASS"

    return {
        "fiscal_year": fiscal_year, "fiscal_year_end": fiscal_year_end, "filing_date": filing_date,
        "diluted_eps": diluted_eps, "price": price, "price_date": price_date,
        "pe": pe, "pe_status": status,
    }


def compute_entry_price_inputs_v1(
    connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year: int
) -> dict[str, object]:
    """Returns the current fiscal year's P/E (evaluated at its own
    filing_date, using ONLY that date's own nominal_close price and that
    year's own as-reported diluted EPS) plus its position within the
    SAME ticker's own trailing (up to 5, strictly prior) fiscal years'
    P/E history. Every trailing year is itself computed the same way --
    each anchored to its own historical filing_date and price, never a
    later one."""

    current = _pe_for_fiscal_year(connection, ticker, fiscal_year)
    if current is None:
        raise ValueError(f"no valuation_v1_per_share_inputs row for {ticker} FY{fiscal_year}")

    trailing_pe_values: list[float] = []
    trailing_years_used: list[int] = []
    for offset in range(1, TRAILING_YEARS + 1):
        prior_fy = fiscal_year - offset
        prior = _pe_for_fiscal_year(connection, ticker, prior_fy)
        if prior is not None and prior["pe"] is not None:
            trailing_pe_values.append(prior["pe"])
            trailing_years_used.append(prior_fy)

    result: dict[str, object] = {
        "ticker": ticker,
        **current,
        "trailing_pe_values": trailing_pe_values,
        "trailing_years_used": trailing_years_used,
        "trailing_pe_min": min(trailing_pe_values) if trailing_pe_values else None,
        "trailing_pe_max": max(trailing_pe_values) if trailing_pe_values else None,
        "trailing_pe_median": (
            sorted(trailing_pe_values)[len(trailing_pe_values) // 2] if trailing_pe_values else None
        ),
    }

    if current["pe"] is not None and len(trailing_pe_values) >= 2:
        below_or_equal = sum(1 for v in trailing_pe_values if v <= current["pe"])
        result["pe_percentile_within_own_history"] = 100 * below_or_equal / len(trailing_pe_values)
    else:
        result["pe_percentile_within_own_history"] = None

    return result
