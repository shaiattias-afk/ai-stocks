"""
tests/scoring/test_backtest_v1.py -- Scoring Model V1's backtest engine
(docs/SCORING_MODEL_V1_BLUEPRINT.md Stage 6 step 5).
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from stock_agent.scoring.backtest_v1 import _add_months, _forward_return, run_top_bottom_backtest_v1


def test_add_months_simple():
    assert _add_months(date(2023, 1, 15), 6) == date(2023, 7, 15)
    assert _add_months(date(2023, 1, 15), 12) == date(2024, 1, 15)


def test_add_months_year_rollover():
    assert _add_months(date(2023, 11, 1), 3) == date(2024, 2, 1)


def test_add_months_clamps_day_for_shorter_month():
    # Jan 31 + 1 month -> Feb has no 31st.
    assert _add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)


def test_add_months_leap_year():
    assert _add_months(date(2023, 1, 29), 13) == date(2024, 2, 29)


@pytest.fixture
def conn():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE historical_prices_daily (ticker VARCHAR, price_date DATE, adj_close DOUBLE)")
    return con


def _insert(con, ticker, price_date, adj_close):
    con.execute("INSERT INTO historical_prices_daily VALUES (?,?,?)", [ticker, price_date, adj_close])


def test_forward_return_basic(conn):
    _insert(conn, "X", "2023-01-10", 100.0)
    _insert(conn, "X", "2023-07-10", 120.0)
    result = _forward_return(conn, "X", "2023-01-10", 6)
    assert result["return"] == pytest.approx(0.20)


def test_forward_return_none_when_exit_date_beyond_available_data(conn):
    """Must never fabricate a return when the target exit date has no
    price data yet -- this is the single most important fail-closed
    property in the backtest engine."""
    _insert(conn, "X", "2023-01-10", 100.0)
    # no price data anywhere near 2023-07-10
    result = _forward_return(conn, "X", "2023-01-10", 6)
    assert result is None


def test_forward_return_uses_nearest_trading_day_on_or_after_target(conn):
    _insert(conn, "X", "2023-01-10", 100.0)
    _insert(conn, "X", "2023-07-08", 999.0)   # before target -- must be ignored
    _insert(conn, "X", "2023-07-11", 130.0)   # first trading day on/after 2023-07-10 target
    _insert(conn, "X", "2023-07-15", 999.0)   # later -- must be ignored (nearest, not furthest)
    result = _forward_return(conn, "X", "2023-01-10", 6)
    assert result["exit_price_date"] == "2023-07-11"
    assert result["return"] == pytest.approx(0.30)


def test_top_bottom_backtest_smoke(monkeypatch, conn):
    """End-to-end smoke test with a synthetic 4-company universe (schema-
    only for scoring_composite_v1/sec_filings, since the real tables need
    the full production schema) -- proves the aggregation logic runs and
    top/bottom are correctly split without touching real data."""
    conn.execute(
        "CREATE TABLE scoring_composite_v1 (ticker VARCHAR, report_date DATE, fiscal_year INTEGER, composite_score DOUBLE)"
    )
    conn.execute("CREATE TABLE sec_filings (ticker VARCHAR, report_date DATE, filing_date DATE)")

    companies = [("A", 90.0), ("B", 70.0), ("C", 30.0), ("D", 10.0)]
    for ticker, score in companies:
        conn.execute(
            "INSERT INTO scoring_composite_v1 VALUES (?,?,?,?)",
            [ticker, "2023-12-31", 2023, score],
        )
        conn.execute(
            "INSERT INTO sec_filings VALUES (?,?,?)",
            [ticker, "2023-12-31", "2024-02-01"],
        )
        _insert(conn, ticker, "2024-02-01", 100.0)

    _insert(conn, "QQQ", "2024-02-01", 100.0)
    for offset, (ticker, _score) in zip([0.5, 0.3, -0.1, -0.3], companies):
        _insert(conn, ticker, "2024-08-01", 100.0 * (1 + offset))
    _insert(conn, "QQQ", "2024-08-01", 105.0)

    result = run_top_bottom_backtest_v1(conn, top_n=2)
    year = result["per_year"][0]
    assert year["fiscal_year"] == 2023
    h6 = year["horizons"][6]
    # top-2 = A, B (returns +50%, +30% -> avg 40%); bottom-2 = C, D (-10%, -30% -> avg -20%)
    assert h6["top_avg_return"] == pytest.approx(0.40)
    assert h6["bottom_avg_return"] == pytest.approx(-0.20)
    assert h6["top_minus_bottom_spread"] == pytest.approx(0.60)
    assert h6["benchmark_avg_return"] == pytest.approx(0.05)
