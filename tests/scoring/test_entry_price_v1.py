"""
tests/scoring/test_entry_price_v1.py -- Entry Price Method 1
(docs/SCORING_MODEL_V1_BLUEPRINT.md Stage 4), focused on the two
highest-risk correctness properties: never pairing a retroactively
split-adjusted price with as-reported EPS (D-046's own measured pitfall,
reused here), and never reading a price after the evaluation date.
"""

from __future__ import annotations

import duckdb
import pytest

from stock_agent.scoring.entry_price_v1 import compute_entry_price_inputs_v1

TICKER = "TEST"


@pytest.fixture
def conn():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE valuation_v1_per_share_inputs (ticker VARCHAR, fiscal_year INTEGER, "
        "fiscal_year_end DATE, diluted_eps DOUBLE, filing_date DATE)"
    )
    con.execute("CREATE TABLE historical_prices_daily (ticker VARCHAR, price_date DATE, nominal_close DOUBLE)")
    return con


def _insert_valuation(con, fiscal_year, fiscal_year_end, diluted_eps, filing_date):
    con.execute(
        "INSERT INTO valuation_v1_per_share_inputs VALUES (?,?,?,?,?)",
        [TICKER, fiscal_year, fiscal_year_end, diluted_eps, filing_date],
    )


def _insert_price(con, price_date, nominal_close):
    con.execute("INSERT INTO historical_prices_daily VALUES (?,?,?)", [TICKER, price_date, nominal_close])


def test_pe_uses_nominal_close_paired_with_as_reported_eps(conn):
    """D-046's measured pitfall: a split-adjusted price paired with
    as-reported EPS silently distorts P/E. nominal_close (never
    retroactively adjusted) is the only price this module may read."""
    _insert_valuation(conn, 2023, "2023-12-31", 10.0, "2024-02-01")
    _insert_price(conn, "2024-02-01", 500.0)  # nominal_close as-quoted at the time
    result = compute_entry_price_inputs_v1(conn, TICKER, 2023)
    assert result["pe"] == pytest.approx(50.0)


def test_negative_eps_gives_undefined_pe_not_a_negative_ratio(conn):
    _insert_valuation(conn, 2023, "2023-12-31", -2.0, "2024-02-01")
    _insert_price(conn, "2024-02-01", 100.0)
    result = compute_entry_price_inputs_v1(conn, TICKER, 2023)
    assert result["pe"] is None
    assert result["pe_status"] == "UNDEFINED_NONPOSITIVE_EPS"


def test_trailing_history_never_includes_a_later_fiscal_year(conn):
    _insert_valuation(conn, 2021, "2021-12-31", 5.0, "2022-02-01")
    _insert_valuation(conn, 2022, "2022-12-31", 8.0, "2023-02-01")
    _insert_valuation(conn, 2023, "2023-12-31", 10.0, "2024-02-01")
    _insert_valuation(conn, 2024, "2024-12-31", 12.0, "2025-02-01")  # future relative to FY2023
    for d in ["2022-02-01", "2023-02-01", "2024-02-01", "2025-02-01"]:
        _insert_price(conn, d, 100.0)

    result = compute_entry_price_inputs_v1(conn, TICKER, 2023)
    assert sorted(result["trailing_years_used"]) == [2021, 2022]
    assert 2024 not in result["trailing_years_used"]


def test_trailing_window_caps_at_five_years(conn):
    for fy, eps in [(2017, 1.0), (2018, 2.0), (2019, 3.0), (2020, 4.0), (2021, 5.0), (2022, 6.0)]:
        _insert_valuation(conn, fy, f"{fy}-12-31", eps, f"{fy+1}-02-01")
        _insert_price(conn, f"{fy+1}-02-01", 100.0)
    _insert_valuation(conn, 2023, "2023-12-31", 10.0, "2024-02-01")
    _insert_price(conn, "2024-02-01", 100.0)

    result = compute_entry_price_inputs_v1(conn, TICKER, 2023)
    # 2023's trailing window is FY2018-2022 (5 years); FY2017 must be excluded.
    assert 2017 not in result["trailing_years_used"]
    assert len(result["trailing_years_used"]) == 5


def test_no_price_data_fails_closed(conn):
    _insert_valuation(conn, 2023, "2023-12-31", 10.0, "2024-02-01")
    result = compute_entry_price_inputs_v1(conn, TICKER, 2023)
    assert result["pe"] is None
    assert result["pe_status"] == "NO_PRICE_DATA"
