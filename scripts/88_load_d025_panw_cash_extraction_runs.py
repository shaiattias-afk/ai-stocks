"""
D-025 — loads the PANW cash-role-fix results (produced by scripts/87)
into the PRODUCTION database (data/database/ai_stock_agent.duckdb) as 3
brand-new extraction_run rows (PANW 2021-2023) — never overwriting any
prior result. Natural key `accession_number::engine_version`, distinct
from every existing engine_version string for these 3 accessions.

All 5 recalculated metrics are written — cash_and_equivalents,
adjusted_net_debt, invested_capital, average_invested_capital, roic —
including the ones that correctly REMAIN REVIEW_REQUIRED (PANW 2023's
adjusted_net_debt/invested_capital/average_invested_capital/roic,
blocked by the already-documented, independent
long_term_debt::ancestry_confirmed_absent finding; PANW 2021/2022's
roic, blocked by the already-documented, independent
effective_tax_rate::pretax_income_not_positive finding) — these are
legitimately recalculated results, not skipped, and their
validation_reason preserves exactly why they remain blocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
RESULTS_PATH = DATA_DIR / "d025_panw_cash_fix_results.json"

ENGINE_VERSION = "v1-cash-reconciliation-role-fix (scripts/87, D-025)"

WRITTEN_KEYS = ["PANW_2021-07-31", "PANW_2022-07-31", "PANW_2023-07-31"]
WRITTEN_METRICS = [
    "cash_and_equivalents", "adjusted_net_debt", "invested_capital",
    "average_invested_capital", "roic",
]


def main() -> None:
    with RESULTS_PATH.open(encoding="utf-8") as handle:
        all_results = json.load(handle)

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH))

    new_runs = 0
    new_rows = 0

    for key in WRITTEN_KEYS:
        result = all_results[key]
        accession_number = result["accession_number"]
        run_id = f"{accession_number}::{ENGINE_VERSION}"

        connection.execute(
            """
            INSERT INTO extraction_runs (
                extraction_run_id, accession_number, engine_version
            )
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [run_id, accession_number, ENGINE_VERSION],
        )
        new_runs += 1

        for metric_name in WRITTEN_METRICS:
            metric = result[metric_name]
            status = metric["status"]
            value = metric.get("value")

            validation_reason = None
            if metric_name == "cash_and_equivalents" and status == "PASS":
                validation_reason = (
                    "D-025: role_exclude_pattern broadened to also exclude "
                    "roles whose OWN title contains 'reconciliation' — the "
                    "ASC 230 cash-flow-to-balance-sheet reconciliation "
                    "table's title mentions 'balance sheets' and was "
                    "previously mistaken for the primary balance sheet role. "
                    "Same concept us-gaap:CashAndCashEquivalentsAtCarryingValue, "
                    "true balance-sheet role, as the verified PANW 2024-07-31 "
                    "control."
                )
            elif status == "REVIEW_REQUIRED":
                validation_reason = metric.get("error")

            connection.execute(
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
                    run_id, metric_name, True, status, value,
                    "iso4217:USD" if metric_name != "roic" and value is not None else (
                        "ratio" if metric_name == "roic" and value is not None else None
                    ),
                    None, None, result["report_date"],
                    "us-gaap:CashAndCashEquivalentsAtCarryingValue"
                    if metric_name == "cash_and_equivalents" else None,
                    None, None, None,
                    metric_name != "cash_and_equivalents",
                    None, validation_reason,
                ],
            )
            new_rows += 1

    connection.close()

    print(f"extraction_runs written/confirmed: {new_runs}")
    print(f"financial_metric_results rows written/confirmed: {new_rows}")
    print(f"engine_version: {ENGINE_VERSION}")


if __name__ == "__main__":
    main()
