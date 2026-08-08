"""
scripts/171_recompute_annual_company_year.py — PR1 verification script.

READ-ONLY against both databases (data/database/xbrl_warehouse_proof.duckdb
and data/database/ai_stock_agent.duckdb). Never writes, never executes any
old numbered script, never uses importlib to load a scripts/*.py module —
every computation goes through the new `stock_agent` package only.

For all 45 target company-years plus the 5 supplementary prior-fiscal-year
accessions, recomputes all 20 primary annual metrics via
stock_agent.metrics.annual.compute_full_company_year and compares every
(accession_number, metric_name) -> (value, status) pair against the live
`financial_metric_results` table (900 rows for the 45 targets; the 5
supplementary accessions carry zero rows there by design — see report).

Exact equality only: no rounding, no truncation, no invented tolerance.
Floats are compared bit-for-bit (`==`); the two are Python floats loaded
from the same DuckDB DOUBLE column type on both sides, so this is a valid
exact comparison, not a fuzzy one.

Prints a PASS/FAIL summary with exact counts and full detail on every
mismatch (metric, both values, both statuses).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

from stock_agent.metrics.annual import ALL_20_METRIC_NAMES, compute_full_company_year

# The 5 supplementary prior-fiscal-year accessions (identified read-only
# from extraction_runs/sec_filings — see PR description): these carry an
# extraction_runs row (scripts/98/101/105, D-029/D-031/D-034) but ZERO rows
# in the frozen financial_metric_results table (pruned before/at the
# Annual V1 freeze, since they were never part of the approved 45 target
# company-years — confirmed by direct query, not assumed).
SUPPLEMENTARY_ACCESSIONS = {
    ("AMZN", "2020-12-31", "0001018724-21-000004"),
    ("CRWD", "2021-01-31", "0001535527-21-000007"),
    ("GOOGL", "2020-12-31", "0001652044-21-000010"),
    ("META", "2019-12-31", "0001326801-20-000013"),
    ("NVDA", "2019-01-27", "0001045810-19-000023"),
}


def load_target_company_years(prod: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str]]:
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


def load_live_rows(prod: duckdb.DuckDBPyConnection, accession_number: str) -> dict[str, tuple[str, float | None]]:
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


def main() -> int:
    total_start = time.perf_counter()

    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    target_company_years = load_target_company_years(production)
    print(f"Target company-years (own frozen financial_metric_results rows): {len(target_company_years)}")
    assert len(target_company_years) == 45, f"expected 45 target company-years, found {len(target_company_years)}"

    all_company_years = sorted(set(target_company_years) | SUPPLEMENTARY_ACCESSIONS)
    print(f"Total company-years to recompute (45 target + {len(SUPPLEMENTARY_ACCESSIONS)} supplementary): {len(all_company_years)}")
    print()

    total_compared = 0
    total_match = 0
    total_mismatch = 0
    total_no_ground_truth = 0
    mismatches: list[str] = []
    no_ground_truth: list[str] = []

    for ticker, report_date, accession_number in all_company_years:
        is_supplementary = (ticker, report_date, accession_number) in SUPPLEMENTARY_ACCESSIONS
        label = f"{ticker} {report_date} ({accession_number}){' [supplementary]' if is_supplementary else ''}"
        print(f"=== {label} ===")

        try:
            result = compute_full_company_year(warehouse, production, ticker, report_date, accession_number)
        except Exception as exc:  # noqa: BLE001 - report, never crash the whole run
            print(f"  EXCEPTION during recompute: {exc}")
            mismatches.append(f"{label}: EXCEPTION during recompute: {exc}")
            continue

        live_rows = load_live_rows(production, accession_number)

        for metric_name in ALL_20_METRIC_NAMES:
            computed = result.get(metric_name, {})
            computed_status = computed.get("status")
            computed_value = computed.get("value")

            live = live_rows.get(metric_name)

            if live is None:
                # Supplementary accessions have no ground-truth row by design
                # (never part of the frozen 45-company-year, 900-row dataset).
                no_ground_truth.append(
                    f"{label} {metric_name}: recomputed status={computed_status} value={computed_value} "
                    "(no ground-truth row in financial_metric_results — expected for supplementary accessions)"
                )
                total_no_ground_truth += 1
                continue

            live_status, live_value = live
            total_compared += 1

            status_match = computed_status == live_status
            value_match = (computed_value == live_value) or (computed_value is None and live_value is None)

            if status_match and value_match:
                total_match += 1
            else:
                total_mismatch += 1
                mismatches.append(
                    f"{label} {metric_name}: "
                    f"computed=(status={computed_status}, value={computed_value}) vs "
                    f"live=(status={live_status}, value={live_value})"
                )
                print(f"  MISMATCH {metric_name}: computed=(status={computed_status}, value={computed_value}) "
                      f"vs live=(status={live_status}, value={live_value})")

        print()

    warehouse.close()
    production.close()

    total_elapsed = time.perf_counter() - total_start

    print("=" * 100)
    print("PR1 VERIFICATION SUMMARY")
    print("=" * 100)
    print(f"Company-years recomputed: {len(all_company_years)} (45 target + {len(SUPPLEMENTARY_ACCESSIONS)} supplementary)")
    print(f"(accession_number, metric_name) pairs compared against live ground truth: {total_compared}")
    print(f"  MATCH:    {total_match}")
    print(f"  MISMATCH: {total_mismatch}")
    print(f"Pairs with no ground-truth row (supplementary accessions, expected): {total_no_ground_truth}")
    print(f"total_elapsed_seconds = {total_elapsed:.3f}")
    print()

    if mismatches:
        print(f"MISMATCH DETAIL ({len(mismatches)}):")
        for line in mismatches:
            print(f"  {line}")
        print()

    expected_compared = 45 * 20
    if total_compared != expected_compared:
        print(f"WARNING: expected {expected_compared} compared pairs (45 x 20), got {total_compared}")

    overall_pass = (total_mismatch == 0) and (total_compared == expected_compared)

    print("=" * 100)
    print(f"OVERALL: {'PASS' if overall_pass else 'FAIL'}")
    print("=" * 100)

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
