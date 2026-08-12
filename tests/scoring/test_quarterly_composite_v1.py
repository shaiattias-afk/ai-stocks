"""
tests/scoring/test_quarterly_composite_v1.py -- the 5-factor quarterly
composite (revenue_growth, operating_margin, fcf_margin, fcf_growth,
distance_from_high), built because Quarterly Data V1 has no balance-sheet
data for ROIC/leverage (see module docstring for the full reasoning).
"""

from __future__ import annotations

import duckdb
import pytest

from stock_agent.scoring.quarterly_composite_v1 import (
    QUARTERLY_FACTOR_WEIGHTS,
    calendar_quarter_key,
    compute_quarterly_composite_scores,
    compute_quarterly_factors,
)

TICKER = "TEST"


@pytest.fixture
def conn():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE quarterly_extraction_runs (run_id VARCHAR, ticker VARCHAR)")
    con.execute(
        "CREATE TABLE quarterly_metric_results (run_id VARCHAR, fiscal_year_end VARCHAR, "
        "fiscal_quarter VARCHAR, metric_name VARCHAR, value DOUBLE)"
    )
    con.execute("CREATE TABLE historical_prices_daily (ticker VARCHAR, price_date VARCHAR, nominal_close DOUBLE)")
    return con


def _insert_quarter(con, ticker, fiscal_year_end, fiscal_quarter, revenue, operating_income, ocf, capex):
    run_id = f"{ticker}-{fiscal_year_end}"
    existing = con.execute("SELECT 1 FROM quarterly_extraction_runs WHERE run_id = ?", [run_id]).fetchone()
    if existing is None:
        con.execute("INSERT INTO quarterly_extraction_runs VALUES (?, ?)", [run_id, ticker])
    for metric_name, value in (
        ("revenue", revenue), ("operating_income", operating_income),
        ("operating_cash_flow", ocf), ("capex", capex),
    ):
        con.execute(
            "INSERT INTO quarterly_metric_results VALUES (?, ?, ?, ?, ?)",
            [run_id, fiscal_year_end, fiscal_quarter, metric_name, value],
        )


def _insert_price(con, ticker, price_date, close):
    con.execute("INSERT INTO historical_prices_daily VALUES (?, ?, ?)", [ticker, price_date, close])


def test_calendar_quarter_key_groups_by_real_calendar_quarter():
    assert calendar_quarter_key("2024-01-28") == "2024Q1"
    assert calendar_quarter_key("2024-03-31") == "2024Q1"
    assert calendar_quarter_key("2024-04-30") == "2024Q2"
    assert calendar_quarter_key("2024-12-31") == "2024Q4"


def test_weights_are_the_same_5_factors_from_composite_v1():
    assert set(QUARTERLY_FACTOR_WEIGHTS) == {
        "revenue_growth", "operating_margin", "fcf_margin", "fcf_growth", "distance_from_high",
    }


def test_operating_margin_and_fcf_margin_computed_from_the_quarter_alone(conn):
    _insert_quarter(conn, TICKER, "2024-12-31", "Q1", revenue=100.0, operating_income=20.0, ocf=30.0, capex=10.0)
    _insert_price(conn, TICKER, "2024-04-30", 50.0)
    result = compute_quarterly_factors(conn, TICKER, "2024-12-31", "Q1", "2024-03-31", "2024-04-30")
    assert result["operating_margin"] == pytest.approx(0.20)
    assert result["fcf_margin"] == pytest.approx(0.20)  # (30-10)/100
    assert result["revenue_growth"] is None  # no prior-year quarter yet
    assert result["fcf_growth"] is None


def test_revenue_growth_and_fcf_growth_are_year_over_year(conn):
    _insert_quarter(conn, TICKER, "2023-12-31", "Q1", revenue=100.0, operating_income=10.0, ocf=15.0, capex=5.0)
    _insert_quarter(conn, TICKER, "2023-12-31", "Q2", revenue=100.0, operating_income=10.0, ocf=15.0, capex=5.0)
    _insert_quarter(conn, TICKER, "2023-12-31", "Q3", revenue=100.0, operating_income=10.0, ocf=15.0, capex=5.0)
    _insert_quarter(conn, TICKER, "2023-12-31", "Q4", revenue=100.0, operating_income=10.0, ocf=15.0, capex=5.0)
    _insert_quarter(conn, TICKER, "2024-12-31", "Q1", revenue=120.0, operating_income=12.0, ocf=20.0, capex=5.0)
    _insert_price(conn, TICKER, "2024-04-30", 50.0)
    result = compute_quarterly_factors(conn, TICKER, "2024-12-31", "Q1", "2024-03-31", "2024-04-30")
    assert result["revenue_growth"] == pytest.approx(0.20)  # 120/100 - 1
    assert result["fcf_growth"] == pytest.approx(0.50)  # (20-5)/(15-5) - 1 = 15/10 - 1


def test_distance_from_high_uses_availability_date_not_a_later_price(conn):
    _insert_quarter(conn, TICKER, "2024-12-31", "Q1", revenue=100.0, operating_income=10.0, ocf=15.0, capex=5.0)
    _insert_price(conn, TICKER, "2024-01-01", 80.0)  # trailing high
    _insert_price(conn, TICKER, "2024-04-30", 60.0)  # price at availability_date
    _insert_price(conn, TICKER, "2024-06-01", 100.0)  # AFTER availability_date -- must be ignored
    result = compute_quarterly_factors(conn, TICKER, "2024-12-31", "Q1", "2024-03-31", "2024-04-30")
    assert result["distance_from_high"] == pytest.approx((80.0 - 60.0) / 80.0)


def test_free_cash_flow_formula_matches_annual_production(conn):
    """ocf - capex, same formula verified byte-identical against annual
    production free_cash_flow (AMZN FY2022, see module docstring)."""
    _insert_quarter(conn, TICKER, "2024-12-31", "Q1", revenue=100.0, operating_income=10.0, ocf=46752.0, capex=63645.0)
    _insert_price(conn, TICKER, "2024-04-30", 50.0)
    result = compute_quarterly_factors(conn, TICKER, "2024-12-31", "Q1", "2024-03-31", "2024-04-30")
    assert result["fcf_margin"] == pytest.approx((46752.0 - 63645.0) / 100.0)


def _factor_row(ticker, calendar_quarter_key_, **factors):
    row = {
        "ticker": ticker, "fiscal_year_end": "2024-12-31", "fiscal_quarter": "Q1",
        "period_end": "2024-03-31", "availability_date": "2024-04-30",
        "calendar_quarter_key": calendar_quarter_key_,
    }
    row.update(factors)
    return row


def test_composite_ranks_within_the_same_calendar_quarter_only():
    rows = [
        _factor_row("A", "2024Q1", revenue_growth=0.30),
        _factor_row("B", "2024Q1", revenue_growth=0.10),
        _factor_row("C", "2024Q2", revenue_growth=0.50),  # different calendar quarter -- must not affect A/B
    ]
    results = {(r["ticker"], r["calendar_quarter_key"]): r for r in compute_quarterly_composite_scores(rows)}
    assert results[("A", "2024Q1")]["factor_scores"]["revenue_growth"] == 100.0
    assert results[("B", "2024Q1")]["factor_scores"]["revenue_growth"] == 0.0
    # C is alone in 2024Q2 -> unrankable
    assert results[("C", "2024Q2")]["composite_score"] is None


def test_composite_missing_factor_excluded_and_weight_renormalized():
    rows = [
        _factor_row("A", "2024Q1", revenue_growth=0.30, operating_margin=0.20),
        _factor_row("B", "2024Q1", operating_margin=0.10),  # missing revenue_growth
    ]
    results = {r["ticker"]: r for r in compute_quarterly_composite_scores(rows)}
    assert "revenue_growth" not in results["B"]["factor_scores"]
    expected_weight = QUARTERLY_FACTOR_WEIGHTS["operating_margin"][0] / sum(
        w for w, _ in QUARTERLY_FACTOR_WEIGHTS.values()
    )
    assert results["B"]["weight_covered"] == pytest.approx(expected_weight)


def test_single_company_in_a_calendar_quarter_cannot_be_ranked():
    rows = [_factor_row("A", "2024Q1", revenue_growth=0.30)]
    results = compute_quarterly_composite_scores(rows)
    assert results[0]["composite_score"] is None
    assert results[0]["weight_covered"] == 0.0
