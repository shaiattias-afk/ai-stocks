"""
tests/test_quarterly_balance_sheet.py -- proves extraction.quarterly_
balance_sheet before it is trusted for a full-universe load.

The core proof: Q4's (accession_number, period_end) already point at
the SAME 10-K + fiscal-year-end the annual engine itself uses, so Q4's
balance-sheet metrics computed by this new module must be byte-identical
to the already-frozen annual `financial_metric_results` values for that
company-year. If they are not, something about the new module's
instant-fact resolution disagrees with the proven annual engine, and
that must be fixed before any production write happens.

Read-only against the live warehouse/production databases -- same
_LIVE_DATABASES_PRESENT skip pattern as test_golden_regression.py.
"""

from __future__ import annotations

import duckdb
import pytest

from stock_agent import PRODUCTION_DB_PATH, WAREHOUSE_DB_PATH
from stock_agent.extraction.quarterly_balance_sheet import (
    ALL_NEW_QUARTERLY_METRIC_NAMES,
    BALANCE_SHEET_METRIC_NAMES,
    ENGINE_VERSION,
    already_loaded,
    build_balance_sheet_cache,
    compute_all_new_quarterly_metrics,
    compute_derived_metrics_from_cache,
    compute_quarterly_balance_sheet_metrics,
    compute_ttm_nopat,
    list_all_engine_v5_quarters,
    write_quarterly_balance_sheet_metrics,
)
from stock_agent.scoring.quarterly_trend_v1 import _quarters_before

pytestmark = pytest.mark.golden

_LIVE_DATABASES_PRESENT = PRODUCTION_DB_PATH.exists() and WAREHOUSE_DB_PATH.exists()
_SKIP_REASON = (
    "live databases not present -- this test reads the real, gitignored "
    "production/warehouse databases and cannot run without them"
)

# Q4 (ticker, fiscal_year_end) pairs spanning several tickers and years,
# chosen from the frozen 9-ticker baseline so the "expected" side is
# already-approved production data, not something this test itself computed.
Q4_PROOF_CASES = [
    ("AMZN", "2024-12-31"),
    ("AMZN", "2023-12-31"),
    ("MSFT", "2024-06-30"),
    ("MSFT", "2023-06-30"),
    ("PANW", "2024-07-31"),
    ("GOOGL", "2023-12-31"),
    ("META", "2023-12-31"),
    ("NVDA", "2023-01-29"),
    ("ORCL", "2023-05-31"),
]


def _live_annual_balance_sheet_rows(prod: duckdb.DuckDBPyConnection, ticker: str, report_date: str) -> dict[str, tuple[str, float | None]]:
    rows = prod.execute(
        """
        SELECT fmr.metric_name, fmr.status, fmr.value
        FROM financial_metric_results fmr
        JOIN extraction_runs er ON er.extraction_run_id = fmr.extraction_run_id
        JOIN sec_filings sf ON sf.accession_number = er.accession_number
        WHERE sf.ticker = ? AND sf.report_date = ?
        """,
        [ticker, report_date],
    ).fetchall()
    return {name: (status, value) for name, status, value in rows}


@pytest.mark.skipif(not _LIVE_DATABASES_PRESENT, reason=_SKIP_REASON)
@pytest.mark.parametrize("ticker,fiscal_year_end", Q4_PROOF_CASES)
def test_q4_balance_sheet_metrics_byte_match_frozen_annual(ticker, fiscal_year_end):
    """Q4's own accession/report_date ARE the annual 10-K's -- so this
    module's Q4 output must equal the already-approved annual values
    exactly, both status and value, for every one of the 8 metrics."""
    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    try:
        computed = compute_quarterly_balance_sheet_metrics(warehouse, production, ticker, fiscal_year_end, "Q4")
        assert computed is not None, f"{ticker} {fiscal_year_end} Q4 was never loaded by Engine V5"

        live = _live_annual_balance_sheet_rows(production, ticker, fiscal_year_end)

        mismatches = []
        for metric_name in BALANCE_SHEET_METRIC_NAMES:
            expected = live.get(metric_name)
            actual = (computed[metric_name]["status"], computed[metric_name].get("value"))
            if expected != actual:
                mismatches.append(f"{metric_name}: computed={actual} vs annual={expected}")

        assert mismatches == [], f"{ticker} {fiscal_year_end} Q4 mismatches:\n" + "\n".join(mismatches)
    finally:
        warehouse.close()
        production.close()


@pytest.mark.skipif(not _LIVE_DATABASES_PRESENT, reason=_SKIP_REASON)
def test_q1_q2_q3_resolve_without_crashing_for_a_sample_of_tickers():
    """Non-Q4 quarters use the SAME instant-fact resolvers on a 10-Q
    accession instead of a 10-K -- no frozen ground truth to byte-match
    against (10-Qs are never part of Annual Data V1), so this just proves
    the mechanism doesn't crash and produces a real status for every
    metric, across several tickers and quarters."""
    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    try:
        cases = [
            ("AMZN", "2024-12-31", "Q1"), ("AMZN", "2024-12-31", "Q2"), ("AMZN", "2024-12-31", "Q3"),
            ("MSFT", "2024-06-30", "Q2"), ("PANW", "2024-07-31", "Q3"), ("CRWD", "2024-01-31", "Q1"),
        ]
        for ticker, fye, quarter in cases:
            result = compute_quarterly_balance_sheet_metrics(warehouse, production, ticker, fye, quarter)
            assert result is not None, f"{ticker} {fye} {quarter} was never loaded by Engine V5"
            for metric_name in BALANCE_SHEET_METRIC_NAMES:
                status = result[metric_name]["status"]
                assert status in {
                    "PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "REVIEW_REQUIRED",
                }, f"{ticker} {fye} {quarter} {metric_name}: unexpected status {status!r}"
    finally:
        warehouse.close()
        production.close()


@pytest.mark.skipif(not _LIVE_DATABASES_PRESENT, reason=_SKIP_REASON)
def test_ttm_nopat_matches_manual_sum_for_one_case():
    """TTM nopat sums 4 quarters of pretax_income/income_tax_expense/
    operating_income and applies the plain (or normalized) formula --
    verify against an independently-computed manual sum, not the
    function's own internals."""
    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    try:
        ticker, fye, quarter = "AMZN", "2024-12-31", "Q4"
        quarters = _quarters_before(production, ticker, fye, quarter, 3) + [(fye, quarter)]
        assert len(quarters) == 4

        pretax_sum = tax_sum = op_sum = 0.0
        for q_fye, q in quarters:
            for metric_name, accumulator in [("pretax_income", "pretax"), ("income_tax_expense", "tax"), ("operating_income", "op")]:
                row = production.execute(
                    """
                    SELECT qmr.value FROM quarterly_metric_results qmr
                    JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
                    WHERE qer.ticker = ? AND qmr.fiscal_year_end = ? AND qmr.fiscal_quarter = ?
                      AND qmr.metric_name = ? AND qmr.result_status = 'PASS'
                    """,
                    [ticker, q_fye, q, metric_name],
                ).fetchone()
                assert row is not None, f"missing {metric_name} at {q_fye} {q}"
                if accumulator == "pretax":
                    pretax_sum += row[0]
                elif accumulator == "tax":
                    tax_sum += row[0]
                else:
                    op_sum += row[0]

        expected_rate = tax_sum / pretax_sum
        expected_nopat = op_sum * (1 - expected_rate) if 0 <= expected_rate <= 1 and pretax_sum > 0 else op_sum * (1 - 0.21)

        result = compute_ttm_nopat(production, ticker, fye, quarter, _quarters_before)
        assert result["nopat"]["status"] in {"PASS", "PASS_NORMALIZED_TAX"}
        assert result["nopat"]["value"] == pytest.approx(expected_nopat, rel=1e-9)
    finally:
        production.close()


@pytest.mark.skipif(not _LIVE_DATABASES_PRESENT, reason=_SKIP_REASON)
def test_first_year_ticker_quarter_fails_closed_not_crashes():
    """A quarter with fewer than 4 trailing quarters of history (e.g. a
    ticker's earliest loaded quarter) must report REVIEW_REQUIRED for
    average_invested_capital/roic, never crash and never guess."""
    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    try:
        # NVDA's earliest loaded quarter in the frozen baseline.
        result = compute_all_new_quarterly_metrics(warehouse, production, "NVDA", "2020-01-26", "Q1", _quarters_before)
        assert result is not None
        assert result["average_invested_capital"]["status"] == "REVIEW_REQUIRED"
        assert result["roic"]["status"] == "REVIEW_REQUIRED"
    finally:
        warehouse.close()
        production.close()


_QUARTERLY_METRIC_RESULTS_DDL = """
CREATE TABLE quarterly_metric_results (
    run_id VARCHAR NOT NULL, ticker VARCHAR NOT NULL, fiscal_year_end VARCHAR NOT NULL,
    fiscal_quarter VARCHAR NOT NULL, metric_name VARCHAR NOT NULL, value DOUBLE, unit VARCHAR,
    result_status VARCHAR NOT NULL, extraction_basis VARCHAR NOT NULL, period_start VARCHAR,
    period_end VARCHAR, availability_date VARCHAR, accession_number VARCHAR NOT NULL,
    concept_qname VARCHAR, context_id VARCHAR, dimensions_json VARCHAR NOT NULL,
    lineage_json VARCHAR NOT NULL, reconciliation_status VARCHAR NOT NULL,
    reconciliation_difference DOUBLE, permitted_difference DOUBLE, created_at VARCHAR NOT NULL,
    engine_version VARCHAR NOT NULL, loaded_at TIMESTAMP NOT NULL, is_active BOOLEAN NOT NULL,
    PRIMARY KEY (run_id, fiscal_quarter, metric_name)
)
"""


def test_write_and_already_loaded_are_idempotent_and_additive():
    """The write function must never collide with Engine V5's existing
    6-metric rows (different metric_name, same PK columns) and
    already_loaded must correctly detect this module's own prior write
    without depending on Engine V5's rows at all."""
    con = duckdb.connect(":memory:")
    con.execute(_QUARTERLY_METRIC_RESULTS_DDL)
    run_id = "test-run-1"
    # Simulate Engine V5's own pre-existing row for this run/quarter.
    con.execute(
        "INSERT INTO quarterly_metric_results VALUES "
        "(?, 'TEST', '2024-12-31', 'Q1', 'revenue', 100.0, 'iso4217:USD', 'PASS', 'DIRECT_QUARTER', "
        " '2024-01-01', '2024-03-31', '2024-05-01', 'acc-1', 'us-gaap:Revenue', 'c-1', '{}', '{}', "
        " 'PASS', 0.0, 100.0, '2026-01-01', 'v1', now(), true)",
        [run_id],
    )

    assert already_loaded(con, run_id, "Q1") is False

    fake_metrics = {
        "stockholders_equity": {"status": "PASS", "value": 500.0, "concept_qname": "us-gaap:StockholdersEquity"},
        "roic": {"status": "REVIEW_REQUIRED", "value": None, "error": "fewer than 4 quarters of history"},
    }
    n = write_quarterly_balance_sheet_metrics(con, run_id, "TEST", "2024-12-31", "Q1", "2024-03-31", "acc-1", fake_metrics)
    assert n == 2

    assert already_loaded(con, run_id, "Q1") is True

    # The pre-existing revenue row must be untouched.
    revenue_row = con.execute(
        "SELECT value, engine_version FROM quarterly_metric_results WHERE run_id=? AND metric_name='revenue'", [run_id]
    ).fetchone()
    assert revenue_row == (100.0, "v1")

    equity_row = con.execute(
        "SELECT value, result_status, engine_version, reconciliation_status, accession_number, period_end "
        "FROM quarterly_metric_results WHERE run_id=? AND metric_name='stockholders_equity'", [run_id]
    ).fetchone()
    assert equity_row == (500.0, "PASS", ENGINE_VERSION, "NOT_APPLICABLE_INSTANT_FACT", "acc-1", "2024-03-31")

    roic_row = con.execute(
        "SELECT value, result_status FROM quarterly_metric_results WHERE run_id=? AND metric_name='roic'", [run_id]
    ).fetchone()
    assert roic_row == (None, "REVIEW_REQUIRED")


def test_all_new_metric_names_constant_matches_what_gets_written():
    assert set(ALL_NEW_QUARTERLY_METRIC_NAMES) == {
        "current_debt", "long_term_debt", "total_debt", "cash_and_equivalents",
        "short_term_investments", "stockholders_equity", "adjusted_net_debt", "invested_capital",
        "effective_tax_rate", "nopat", "average_invested_capital", "roic",
    }


@pytest.mark.skipif(not _LIVE_DATABASES_PRESENT, reason=_SKIP_REASON)
def test_cached_batch_path_matches_direct_path_exactly():
    """The full-universe load script (scripts/214) uses build_balance_
    sheet_cache + compute_derived_metrics_from_cache instead of recomputing
    compute_company_year per quarter (for speed across ~1,400
    company-quarters). This proves the cached path is not just faster but
    produces byte-identical output to the already-proven direct path,
    before the cached path is trusted for a real production write."""
    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    try:
        all_quarters = list_all_engine_v5_quarters(production)
        subset = [q for q in all_quarters if q["ticker"] in {"AMZN", "MSFT", "PANW"}]
        assert len(subset) > 10

        cache = build_balance_sheet_cache(str(WAREHOUSE_DB_PATH), subset, max_workers=6)

        target = next(q for q in subset if q["ticker"] == "AMZN" and q["fiscal_year_end"] == "2024-12-31" and q["fiscal_quarter"] == "Q4")
        cached_result = compute_derived_metrics_from_cache(warehouse, production, cache, target, _quarters_before)
        direct_result = compute_all_new_quarterly_metrics(warehouse, production, "AMZN", "2024-12-31", "Q4", _quarters_before)

        mismatches = [
            (name, cached_result[name]["status"], cached_result[name].get("value"),
             direct_result[name]["status"], direct_result[name].get("value"))
            for name in direct_result
            if (cached_result[name]["status"], cached_result[name].get("value"))
            != (direct_result[name]["status"], direct_result[name].get("value"))
        ]
        assert mismatches == [], f"cached vs direct mismatches: {mismatches}"
    finally:
        warehouse.close()
        production.close()
