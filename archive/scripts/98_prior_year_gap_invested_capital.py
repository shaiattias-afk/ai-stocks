"""
D-029 — computes current_debt/long_term_debt/total_debt/cash/short_term_
investments/stockholders_equity/adjusted_net_debt/invested_capital for
the 3 newly-warehoused prior-fiscal-year filings (AMZN FY2020, GOOGL
FY2020, META FY2019), applying the EXACT SAME already-approved policy
engine as scripts/92 (Policies A/B/C: maturity-basis debt, undrawn-
facility zero, direct-aggregate total) — reused via importlib, not
duplicated, since scripts/92 is preserved unchanged and this is a
read-only reuse of its already-verified `compute_company_year`.

This treats each prior fiscal year as a genuine addition to the
historical dataset (its own accession_number, its own extraction_run,
its own primary-metric rows) — not a special "_prior" metric name.
This is what lets scripts/93 (average_invested_capital via previous-
locked-filing lookup) and scripts/95 (final roic combination), BOTH
already-approved, unchanged, and ticker-agnostic, pick these new years
up automatically on their next run without any new code.

Point-in-time correctness: only facts from each prior filing's own
warehouse entry are read (accession-scoped queries throughout,
inherited unchanged from scripts/92's warehouse-reconstruction
functions) — no fact from AMZN/GOOGL FY2021 or META FY2020 is read
here, and vice versa.

Writes new versioned extraction_run rows for the 3 new accessions only.
Does not touch any existing row for any other accession.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

ENGINE_VERSION = "v1-prior-year-gap-invested-capital (scripts/98, D-029)"

TARGET_FILINGS: list[tuple[str, str, str]] = [
    ("AMZN", "2020-12-31", "0001018724-21-000004"),
    ("GOOGL", "2020-12-31", "0001652044-21-000010"),
    ("META", "2019-12-31", "0001326801-20-000013"),
]

WRITTEN_METRICS = [
    "current_debt", "long_term_debt", "total_debt", "cash_and_equivalents",
    "short_term_investments", "stockholders_equity", "adjusted_net_debt",
    "invested_capital",
]

# Reuse scripts/92's already-approved, unchanged policy engine (Policies A/B/C).
_spec = importlib.util.spec_from_file_location(
    "s92", PROJECT_DIR / "scripts" / "92_groups_1_3_4_debt_facility_aggregate_policy.py"
)
s92 = importlib.util.module_from_spec(_spec)
sys.modules["s92"] = s92
_spec.loader.exec_module(s92)


def write_result_to_production_db(prod_connection: duckdb.DuckDBPyConnection, result: dict) -> int:
    accession_number = result["accession_number"]
    run_id = f"{accession_number}::{ENGINE_VERSION}"

    prod_connection.execute(
        """
        INSERT INTO extraction_runs (extraction_run_id, accession_number, engine_version)
        VALUES (?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        [run_id, accession_number, ENGINE_VERSION],
    )

    rows_written = 0
    for metric_name in WRITTEN_METRICS:
        metric = result[metric_name]
        status = metric["status"]
        value = metric.get("value")
        selection_tier = metric.get("selection_tier")
        source_concept = metric.get("concept_qname")

        validation_reason = None
        if status == "REVIEW_REQUIRED":
            validation_reason = metric.get("error")
        elif metric.get("basis"):
            validation_reason = (
                f"D-029 (prior-fiscal-year addition, reusing scripts/92's "
                f"already-approved policy engine unchanged): basis={metric.get('basis')}, "
                f"lineage={metric.get('lineage')}"
            )

        unit = "iso4217:USD" if value is not None else None

        prod_connection.execute(
            """
            INSERT INTO financial_metric_results (
                extraction_run_id, metric_name, is_primary_metric,
                status, value, unit, context_id, period_start,
                period_end, source_concept, label,
                statement_role_definition, selection_tier,
                is_derived_metric, formula, validation_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [
                run_id, metric_name,
                metric_name in ("current_debt", "long_term_debt", "total_debt"),
                status, value, unit, None, None, result["report_date"],
                source_concept, None, None, selection_tier,
                metric_name not in ("current_debt", "long_term_debt", "cash_and_equivalents",
                                     "short_term_investments", "stockholders_equity"),
                None, validation_reason,
            ],
        )
        rows_written += 1

    return rows_written


def main() -> None:
    total_start = time.perf_counter()
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH))

    for ticker, report_date, accession_number in TARGET_FILINGS:
        filing_start = time.perf_counter()
        print(f"=== {ticker} {report_date} ({accession_number}) — prior fiscal year ===")
        print("  arelle_required=NO (already warehoused by scripts/97)")

        result = s92.compute_company_year(warehouse_connection, ticker, report_date, accession_number)
        rows_written = write_result_to_production_db(prod_connection, result)

        for metric_name in WRITTEN_METRICS:
            m = result[metric_name]
            print(f"  {metric_name:22s} status={m['status']:22s} value={m.get('value')} basis={m.get('basis')}")

        filing_elapsed = time.perf_counter() - filing_start
        print(f"  db_write_status=OK rows_written={rows_written} elapsed={filing_elapsed:.3f}s")
        print()

    warehouse_connection.close()
    prod_connection.close()

    total_elapsed = time.perf_counter() - total_start
    print("=" * 100)
    print(f"PRIOR-YEAR-GAP INVESTED CAPITAL COMPLETE — total_elapsed_seconds = {total_elapsed:.3f}")
    print("=" * 100)


if __name__ == "__main__":
    main()
