"""
tests/test_golden_regression.py -- formalizes PR1's throwaway verification
scripts (scripts/171_recompute_annual_company_year.py for annual,
scripts/150_v5_final_release_regression.py for quarterly) into a
permanent, CI-shaped pytest test.

Both tests are READ-ONLY against the live warehouse/production databases
(data/database/xbrl_warehouse_proof.duckdb, data/database/
ai_stock_agent.duckdb) -- every connection below is opened with
read_only=True, nothing is ever written, no network call is made, and
Arelle is never imported or invoked. This module MUST NEVER open a
write connection to any .duckdb file -- if you are editing this file,
do not add one.

Marked @pytest.mark.golden (it reads a 500MB+ warehouse and recomputes
45+ company-years, so it is meaningfully slower than the rest of the
suite) but still collected and run by default with a plain `pytest`
invocation -- never skipped silently. It only actually skips (with an
explicit, visible reason) when the live databases are not present on
disk at all, which keeps this test file portable to a checkout that
has not fetched the (gitignored, multi-hundred-MB) database files.

Scope boundary (explicit, not a silent omission): `derived_metric_
results` (405 rows) and `valuation_v1_per_share_inputs` (45 rows) are
OUT OF SCOPE for this test. PR1 did not port the scripts that produce
them (scripts/153_derived_metrics_v1_load.py, scripts/
160_valuation_v1_per_share_inputs.py) into the stock_agent package, so
there is no package code to regression-test them against.
"""

from __future__ import annotations

import duckdb
import pytest

from stock_agent import PRODUCTION_DB_PATH, WAREHOUSE_DB_PATH
from stock_agent.extraction.quarterly import run_quarterly_extraction_engine_v5
from stock_agent.metrics.annual import ALL_20_METRIC_NAMES, compute_full_company_year

pytestmark = pytest.mark.golden

_LIVE_DATABASES_PRESENT = PRODUCTION_DB_PATH.exists() and WAREHOUSE_DB_PATH.exists()
_SKIP_REASON = (
    "live databases not present at "
    f"{PRODUCTION_DB_PATH} / {WAREHOUSE_DB_PATH} -- the golden regression "
    "reads the real, gitignored production/warehouse databases and cannot "
    "run without them (this is an explicit, visible skip, never a silent one)"
)

# The 5 supplementary prior-fiscal-year accessions (D-029/D-031/D-034):
# each carries an extraction_runs row but ZERO rows in the frozen
# financial_metric_results table (never part of the approved 45 target
# company-years) -- confirmed by direct query in scripts/171, reproduced
# verbatim here.
SUPPLEMENTARY_ACCESSIONS = {
    ("AMZN", "2020-12-31", "0001018724-21-000004"),
    ("CRWD", "2021-01-31", "0001535527-21-000007"),
    ("GOOGL", "2020-12-31", "0001652044-21-000010"),
    ("META", "2019-12-31", "0001326801-20-000013"),
    ("NVDA", "2019-01-27", "0001045810-19-000023"),
}

EXPECTED_TARGET_COMPANY_YEARS = 45
EXPECTED_ANNUAL_COMPARED_PAIRS = 45 * 20  # 900

EXPECTED_QUARTERLY_RUNS = 45
EXPECTED_ROWS_PER_COMPANY_YEAR = 24
EXPECTED_QUARTERLY_TOTAL_ROWS = EXPECTED_QUARTERLY_RUNS * EXPECTED_ROWS_PER_COMPANY_YEAR  # 1080


def _load_target_company_years(prod: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str]]:
    rows = prod.execute(
        """
        SELECT DISTINCT sf.ticker, sf.report_date, sf.accession_number
        FROM sec_filings sf
        JOIN extraction_runs er ON er.accession_number = sf.accession_number
        JOIN financial_metric_results fmr ON fmr.extraction_run_id = er.extraction_run_id
        ORDER BY 1, 2
        """
    ).fetchall()
    return [(t, str(rd), acc) for t, rd, acc in rows]


def _load_live_annual_rows(prod: duckdb.DuckDBPyConnection, accession_number: str) -> dict[str, tuple[str, float | None]]:
    rows = prod.execute(
        """
        SELECT fmr.metric_name, fmr.status, fmr.value
        FROM financial_metric_results fmr
        JOIN extraction_runs er ON er.extraction_run_id = fmr.extraction_run_id
        WHERE er.accession_number = ?
        """,
        [accession_number],
    ).fetchall()
    return {name: (status, value) for name, status, value in rows}


@pytest.mark.skipif(not _LIVE_DATABASES_PRESENT, reason=_SKIP_REASON)
def test_annual_golden_regression_900_rows_byte_identical():
    """Recomputes all 20 primary annual metrics for the 45 approved
    company-years (+ 5 supplementary prior-fiscal-year accessions, needed
    by compute_full_company_year for average_invested_capital) using
    ONLY stock_agent, and asserts byte-identical (value AND status)
    against the live financial_metric_results table. Exact equality
    only -- both sides are Python floats loaded from the same DuckDB
    DOUBLE column type, so `==` is a valid, non-fuzzy comparison here
    (same discipline scripts/171 used)."""
    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    try:
        target_company_years = _load_target_company_years(production)
        assert len(target_company_years) == EXPECTED_TARGET_COMPANY_YEARS, (
            f"expected {EXPECTED_TARGET_COMPANY_YEARS} target company-years in "
            f"financial_metric_results, found {len(target_company_years)}"
        )

        all_company_years = sorted(set(target_company_years) | SUPPLEMENTARY_ACCESSIONS)

        total_compared = 0
        mismatches: list[str] = []

        for ticker, report_date, accession_number in all_company_years:
            result = compute_full_company_year(warehouse, production, ticker, report_date, accession_number)
            live_rows = _load_live_annual_rows(production, accession_number)

            for metric_name in ALL_20_METRIC_NAMES:
                live = live_rows.get(metric_name)
                if live is None:
                    # Supplementary accessions carry no ground-truth row by
                    # design (never part of the frozen 45-company-year,
                    # 900-row dataset) -- nothing to compare, by design.
                    continue

                computed = result.get(metric_name, {})
                computed_status = computed.get("status")
                computed_value = computed.get("value")
                live_status, live_value = live
                total_compared += 1

                status_match = computed_status == live_status
                value_match = (computed_value == live_value) or (
                    computed_value is None and live_value is None
                )
                if not (status_match and value_match):
                    mismatches.append(
                        f"{ticker} {report_date} ({accession_number}) {metric_name}: "
                        f"computed=(status={computed_status}, value={computed_value}) vs "
                        f"live=(status={live_status}, value={live_value})"
                    )
    finally:
        warehouse.close()
        production.close()

    assert total_compared == EXPECTED_ANNUAL_COMPARED_PAIRS, (
        f"expected exactly {EXPECTED_ANNUAL_COMPARED_PAIRS} (accession, metric) "
        f"pairs compared (45 x 20), got {total_compared}"
    )
    assert mismatches == [], (
        f"{len(mismatches)}/{total_compared} annual golden regression mismatches:\n"
        + "\n".join(mismatches)
    )


def _load_quarterly_runs(prod: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = prod.execute(
        "SELECT run_id, ticker, fiscal_year_end, q1_accession, q2_accession, "
        "q3_accession, fy_accession FROM quarterly_extraction_runs ORDER BY ticker, fiscal_year_end"
    ).fetchall()
    return [
        {
            "run_id": r[0], "ticker": r[1], "fiscal_year_end": str(r[2]),
            "q1_accession": r[3], "q2_accession": r[4], "q3_accession": r[5], "fy_accession": r[6],
        }
        for r in rows
    ]


def _load_quarterly_production_rows(prod: duckdb.DuckDBPyConnection, run_id: str) -> dict[tuple[str, str], tuple[float | None, str]]:
    rows = prod.execute(
        "SELECT metric_name, fiscal_quarter, value, reconciliation_status "
        "FROM quarterly_metric_results WHERE run_id = ?",
        [run_id],
    ).fetchall()
    return {(metric_name, fiscal_quarter): (value, status) for metric_name, fiscal_quarter, value, status in rows}


def _values_equal(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1


@pytest.mark.skipif(not _LIVE_DATABASES_PRESENT, reason=_SKIP_REASON)
def test_quarterly_golden_regression_1080_rows_reproduced(tmp_path):
    """Runs stock_agent.extraction.quarterly.run_quarterly_extraction_
    engine_v5 (the ported, in-scope engine — scripts/148 itself now
    imports this function unchanged, per PR1) once per company-year and
    compares every one of its 24 rows against the CURRENT ACTIVE
    quarterly_metric_results production row for that exact
    (ticker, fiscal_year_end, metric_name, fiscal_quarter). JSON/CSV
    engine output is written to pytest's own tmp_path, never into the
    repo or into data/."""
    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    try:
        runs = _load_quarterly_runs(production)
    finally:
        production.close()

    assert len(runs) == EXPECTED_QUARTERLY_RUNS, (
        f"expected {EXPECTED_QUARTERLY_RUNS} quarterly_extraction_runs rows, found {len(runs)}"
    )

    total_compared = 0
    mismatches: list[str] = []

    for cy in runs:
        json_out = tmp_path / f"{cy['ticker']}_{cy['fiscal_year_end']}.json"
        csv_out = tmp_path / f"{cy['ticker']}_{cy['fiscal_year_end']}.csv"

        engine_output = run_quarterly_extraction_engine_v5(
            ticker=cy["ticker"],
            fiscal_year_end=cy["fiscal_year_end"],
            q1_accession=cy["q1_accession"],
            q2_accession=cy["q2_accession"],
            q3_accession=cy["q3_accession"],
            fy_accession=cy["fy_accession"],
            json_output_path=json_out,
            csv_output_path=csv_out,
        )

        production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
        try:
            prod_rows = _load_quarterly_production_rows(production, cy["run_id"])
        finally:
            production.close()

        for metric_name, metric_result in engine_output["metrics"].items():
            for quarter_label in ("Q1", "Q2", "Q3", "Q4"):
                quarter = metric_result.get("quarters", {}).get(quarter_label)
                if quarter is None:
                    continue
                key = (metric_name, quarter_label)
                total_compared += 1
                prod_row = prod_rows.get(key)
                if prod_row is None:
                    mismatches.append(
                        f"{cy['ticker']} {cy['fiscal_year_end']} {key}: no matching production row found"
                    )
                    continue
                prod_value, prod_status = prod_row
                if not _values_equal(quarter["value"], prod_value) or metric_result.get("status") != prod_status:
                    mismatches.append(
                        f"{cy['ticker']} {cy['fiscal_year_end']} {key}: "
                        f"computed=(value={quarter['value']}, status={metric_result.get('status')}) vs "
                        f"production=(value={prod_value}, status={prod_status})"
                    )

    assert total_compared == EXPECTED_QUARTERLY_TOTAL_ROWS, (
        f"expected exactly {EXPECTED_QUARTERLY_TOTAL_ROWS} quarterly rows compared "
        f"(45 x 24), got {total_compared}"
    )
    assert mismatches == [], (
        f"{len(mismatches)}/{total_compared} quarterly golden regression mismatches:\n"
        + "\n".join(mismatches)
    )
