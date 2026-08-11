"""
tests/scoring/test_inputs_v1.py -- Scoring Inputs V1
(docs/SCORING_MODEL_V1_BLUEPRINT.md Stage 3), focused on the two
highest-risk correctness properties: point-in-time safety for
"distance from high" (must never read a price after the evaluation
date) and the prior-fiscal-year lookup (must not depend on
sec_filings.prior_report_date, which is measured -- this module's own
small proof against real production data -- to be wrong for some
tickers).

Builds a minimal, synthetic, in-memory DuckDB with just the columns
Scoring Inputs V1 actually reads, for a 3-fiscal-year synthetic company
("TEST").
"""

from __future__ import annotations

import duckdb
import pytest

from stock_agent.scoring.inputs_v1 import compute_scoring_inputs_v1

TICKER = "TEST"


@pytest.fixture
def conn():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE companies (ticker VARCHAR, company_name VARCHAR, cik BIGINT)")
    con.execute(
        "CREATE TABLE sec_filings (accession_number VARCHAR, ticker VARCHAR, form VARCHAR, "
        "report_date DATE, filing_date DATE, fiscal_year INTEGER, prior_report_date DATE, "
        "source_document VARCHAR)"
    )
    con.execute(
        "CREATE TABLE extraction_runs (extraction_run_id VARCHAR, accession_number VARCHAR, "
        "engine_version VARCHAR, loaded_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE financial_metric_results (extraction_run_id VARCHAR, metric_name VARCHAR, "
        "status VARCHAR, value DOUBLE, loaded_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE derived_metric_results (ticker VARCHAR, frequency VARCHAR, "
        "fiscal_year_end DATE, derived_metric VARCHAR, value DOUBLE, "
        "reconciliation_status VARCHAR, loaded_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE historical_prices_daily (ticker VARCHAR, price_date DATE, "
        "nominal_close DOUBLE)"
    )
    return con


def _insert_filing(con, report_date, filing_date, fiscal_year, prior_report_date=None):
    accession = f"ACC-{report_date}"
    con.execute(
        "INSERT INTO sec_filings VALUES (?,?,?,?,?,?,?,?)",
        [accession, TICKER, "10-K", report_date, filing_date, fiscal_year, prior_report_date, "doc.htm"],
    )
    run_id = f"{accession}::v1"
    con.execute("INSERT INTO extraction_runs VALUES (?,?,?,?)", [run_id, accession, "v1", filing_date])
    return run_id


def _insert_metric(con, run_id, metric_name, value, status="PASS"):
    con.execute(
        "INSERT INTO financial_metric_results VALUES (?,?,?,?,?)",
        [run_id, metric_name, status, value, "2024-01-01"],
    )


def _insert_price(con, price_date, close):
    con.execute("INSERT INTO historical_prices_daily VALUES (?,?,?)", [TICKER, price_date, close])


def test_distance_from_high_never_reads_a_future_price(conn):
    """A price spike AFTER the filing date must not affect the trailing
    high -- this is the single most important point-in-time-safety
    property in the whole module."""
    run_id = _insert_filing(conn, "2023-12-31", "2024-02-01", 2023)
    for m in ["revenue", "operating_income", "free_cash_flow", "capex",
              "adjusted_net_debt", "stockholders_equity", "roic"]:
        _insert_metric(conn, run_id, m, 100.0)

    _insert_price(conn, "2024-01-15", 50.0)   # before filing_date -- eligible
    _insert_price(conn, "2024-02-01", 60.0)   # ON filing_date -- eligible, and the "current price"
    _insert_price(conn, "2024-03-01", 999.0)  # AFTER filing_date -- must be invisible

    result = compute_scoring_inputs_v1(conn, TICKER, "2023-12-31")
    assert result["distance_from_high_trailing_high"] == 60.0
    assert result["distance_from_high_price"] == 60.0
    assert result["distance_from_high"] == pytest.approx(0.0)


def test_prior_year_lookup_ignores_a_wrong_stored_prior_report_date(conn):
    """sec_filings.prior_report_date is deliberately given a WRONG value
    here (one day off, mirroring the real MU/NVDA data defect this
    module's own proof found) -- the module must still find the real
    prior year via the robust ticker-history mechanism, not the
    unreliable stored column."""
    run_id_2022 = _insert_filing(conn, "2022-12-31", "2023-02-01", 2022)
    _insert_metric(conn, run_id_2022, "revenue", 100.0)
    _insert_metric(conn, run_id_2022, "roic", 0.10)
    _insert_metric(conn, run_id_2022, "free_cash_flow", 10.0)

    # WRONG prior_report_date on purpose: one day off from the real 2022-12-31 row.
    run_id_2023 = _insert_filing(conn, "2023-12-31", "2024-02-01", 2023, prior_report_date="2022-12-30")
    _insert_metric(conn, run_id_2023, "revenue", 150.0)
    _insert_metric(conn, run_id_2023, "roic", 0.20)
    _insert_metric(conn, run_id_2023, "free_cash_flow", 20.0)
    for m in ["operating_income", "capex", "adjusted_net_debt", "stockholders_equity"]:
        _insert_metric(conn, run_id_2022, m, 10.0)
        _insert_metric(conn, run_id_2023, m, 10.0)
    _insert_price(conn, "2024-02-01", 10.0)
    _insert_price(conn, "2023-02-01", 10.0)

    result = compute_scoring_inputs_v1(conn, TICKER, "2023-12-31")
    assert result["prior_report_date"] == "2022-12-31"
    assert result["roic_trend"] == pytest.approx(0.10)
    assert result["fcf_growth"] == pytest.approx(1.0)  # 20/10 - 1


def test_capex_discipline_uses_only_prior_years_never_current(conn):
    years = [
        ("2021-12-31", "2022-02-01", 2021, 0.10),  # capex/revenue = 10/100
        ("2022-12-31", "2023-02-01", 2022, 0.20),
        ("2023-12-31", "2024-02-01", 2023, 0.30),
        ("2024-12-31", "2025-02-01", 2024, 0.90),  # current year -- a big spike, must NOT pollute its own trailing average
    ]
    run_ids = []
    for report_date, filing_date, fiscal_year, capex_ratio in years:
        run_id = _insert_filing(conn, report_date, filing_date, fiscal_year)
        run_ids.append(run_id)
        _insert_metric(conn, run_id, "revenue", 100.0)
        _insert_metric(conn, run_id, "capex", capex_ratio * 100.0)
        for m in ["operating_income", "free_cash_flow", "adjusted_net_debt", "stockholders_equity", "roic"]:
            _insert_metric(conn, run_id, m, 1.0)
        _insert_price(conn, filing_date, 10.0)

    result = compute_scoring_inputs_v1(conn, TICKER, "2024-12-31")
    # trailing average of 2021/2022/2023 = (0.10+0.20+0.30)/3 = 0.20; current = 0.90
    assert result["capex_discipline_trailing_years_used"] == 3
    assert result["capex_discipline_deviation"] == pytest.approx(0.70)


def test_first_fiscal_year_has_no_growth_factors_not_fabricated(conn):
    run_id = _insert_filing(conn, "2023-12-31", "2024-02-01", 2023)
    for m in ["revenue", "operating_income", "free_cash_flow", "capex",
              "adjusted_net_debt", "stockholders_equity", "roic"]:
        _insert_metric(conn, run_id, m, 1.0)
    _insert_price(conn, "2024-02-01", 10.0)

    result = compute_scoring_inputs_v1(conn, TICKER, "2023-12-31")
    assert result["roic_trend"] is None
    assert result["roic_trend_status"] == "NO_PRIOR_YEAR"
    assert result["fcf_growth"] is None
    assert result["capex_discipline_deviation"] is None
