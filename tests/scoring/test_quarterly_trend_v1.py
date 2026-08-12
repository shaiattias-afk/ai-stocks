"""
tests/scoring/test_quarterly_trend_v1.py -- revenue growth acceleration
(quarter-over-quarter change in the YoY growth rate), the factor the
user asked for (2026-08-12 session) to capture intra-year momentum a
single annual snapshot misses.
"""

from __future__ import annotations

import duckdb
import pytest

from stock_agent.scoring.quarterly_trend_v1 import compute_revenue_growth_acceleration

TICKER = "TEST"


@pytest.fixture
def conn():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE quarterly_extraction_runs (run_id VARCHAR, ticker VARCHAR)"
    )
    con.execute(
        "CREATE TABLE quarterly_metric_results (run_id VARCHAR, fiscal_year_end VARCHAR, "
        "fiscal_quarter VARCHAR, metric_name VARCHAR, value DOUBLE)"
    )
    return con


def _insert_quarter(con, fiscal_year_end, fiscal_quarter, revenue):
    run_id = f"{TICKER}-{fiscal_year_end}"
    con.execute("INSERT INTO quarterly_extraction_runs VALUES (?, ?)", [run_id, TICKER])
    con.execute(
        "INSERT INTO quarterly_metric_results VALUES (?, ?, ?, 'revenue', ?)",
        [run_id, fiscal_year_end, fiscal_quarter, revenue],
    )


def test_accelerating_growth_gives_positive_value(conn):
    # Year 1 quarters: flat revenue 100 each quarter (0% YoY baseline, no prior year to compare -- skip)
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        _insert_quarter(conn, "2023-12-31", q, 100.0)
    # Year 2: growth speeding up each quarter (10%, 20%, 30%, 40% YoY)
    _insert_quarter(conn, "2024-12-31", "Q1", 110.0)  # YoY = +10%
    _insert_quarter(conn, "2024-12-31", "Q2", 120.0)  # YoY = +20%
    _insert_quarter(conn, "2024-12-31", "Q3", 130.0)  # YoY = +30%
    _insert_quarter(conn, "2024-12-31", "Q4", 140.0)  # YoY = +40%

    result = compute_revenue_growth_acceleration(conn, TICKER, "2024-12-31", "Q3")
    assert result["status"] == "PASS"
    # Q3 YoY (30%) minus Q2 YoY (20%) = +10 percentage points of acceleration
    assert result["value"] == pytest.approx(0.10)


def test_decelerating_growth_gives_negative_value(conn):
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        _insert_quarter(conn, "2023-12-31", q, 100.0)
    _insert_quarter(conn, "2024-12-31", "Q1", 140.0)  # YoY = +40%
    _insert_quarter(conn, "2024-12-31", "Q2", 130.0)  # YoY = +30%
    _insert_quarter(conn, "2024-12-31", "Q3", 120.0)  # YoY = +20%

    result = compute_revenue_growth_acceleration(conn, TICKER, "2024-12-31", "Q3")
    assert result["status"] == "PASS"
    assert result["value"] == pytest.approx(-0.10)


def test_insufficient_history_fails_closed_not_zero(conn):
    _insert_quarter(conn, "2024-12-31", "Q1", 100.0)
    _insert_quarter(conn, "2024-12-31", "Q2", 110.0)
    result = compute_revenue_growth_acceleration(conn, TICKER, "2024-12-31", "Q2")
    assert result["status"] in ("NO_PRIOR_QUARTER", "INSUFFICIENT_HISTORY")
    assert result["value"] is None


def test_uses_year_over_year_not_sequential_quarter_avoiding_seasonality(conn):
    """A seasonal business (Q4 always much bigger than Q3, every year)
    must NOT show fake 'acceleration' just because Q4 > Q3 -- YoY
    comparison, not sequential, is what makes this seasonality-safe."""
    # Same seasonal pattern both years: Q1=100, Q2=100, Q3=100, Q4=200 -- 0% YoY growth throughout
    for fye in ["2023-12-31", "2024-12-31"]:
        _insert_quarter(conn, fye, "Q1", 100.0)
        _insert_quarter(conn, fye, "Q2", 100.0)
        _insert_quarter(conn, fye, "Q3", 100.0)
        _insert_quarter(conn, fye, "Q4", 200.0)

    result = compute_revenue_growth_acceleration(conn, TICKER, "2024-12-31", "Q4")
    assert result["status"] == "PASS"
    assert result["value"] == pytest.approx(0.0)  # flat YoY growth both quarters -> zero acceleration
