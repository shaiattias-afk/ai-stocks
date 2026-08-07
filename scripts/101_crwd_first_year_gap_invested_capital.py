"""
D-031 — computes current_debt/long_term_debt/total_debt/cash/short_term_
investments/stockholders_equity/adjusted_net_debt/invested_capital for
CRWD's newly-warehoused FY2021 (2021-01-31) filing, applying the EXACT
SAME already-approved policy engine as scripts/92 (Policies A/B/C) —
reused via importlib, not duplicated. Same pattern as scripts/98
(D-029), which did the identical thing for AMZN/GOOGL FY2020 and META
FY2019.

Treats CRWD FY2021 as a genuine addition to the historical dataset (its
own accession_number, its own extraction_run, its own primary-metric
rows) — not a special "_prior" metric name. This is what lets
scripts/93 (average_invested_capital via previous-locked-filing lookup)
and scripts/95 (final roic combination), BOTH already-approved,
unchanged, and ticker-agnostic, pick this new year up automatically on
their next run without any new code.

Point-in-time correctness: only facts from CRWD FY2021's own warehouse
entry are read — no fact from CRWD FY2022 is read here, and vice versa.
No other company is touched.
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

ENGINE_VERSION = "v1-crwd-first-year-gap-invested-capital (scripts/101, D-031)"

TARGET_FILINGS: list[tuple[str, str, str]] = [
    ("CRWD", "2021-01-31", "0001535527-21-000007"),
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
                f"D-031 (CRWD first-fiscal-year addition, reusing scripts/92's "
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
        print(f"=== {ticker} {report_date} ({accession_number}) — first fiscal year gap ===")
        print("  arelle_required=NO (already warehoused by scripts/100)")

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
    print(f"CRWD FIRST-YEAR-GAP INVESTED CAPITAL COMPLETE — total_elapsed_seconds = {total_elapsed:.3f}")
    print("=" * 100)


if __name__ == "__main__":
    main()
