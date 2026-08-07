"""
D-023 — loads the MU cash-label-fix results (produced by scripts/82)
into the PRODUCTION database (data/database/ai_stock_agent.duckdb) as
4 brand-new extraction_run rows (MU 2021-2024) — never overwriting any
prior result. Natural key `accession_number::engine_version`, and
engine_version here ("v1-cash-label-fix (scripts/82, D-023)") differs
from every existing engine_version string for these 4 accessions.

Only `cash_and_equivalents`, `adjusted_net_debt`, and `invested_capital`
are written — the 3 metrics verified correct via the MU 2025 control
check (byte-identical to the pre-existing ground truth on all 3).
`average_invested_capital`/`roic` are DELIBERATELY NOT written here:
recalculating them exposed a separate, pre-existing prior-period date-
matching gap in the warehouse reconstruction (unrelated to the cash-
label fix — confirmed because it also affects the MU 2025 CONTROL year,
whose average_invested_capital/roic were already PASS before this task
and could not be reproduced by the same code path) — writing a
REVIEW_REQUIRED result over an existing PASS would be a real
regression, not a general evidence improvement, so those two metrics
are left completely untouched for all 5 MU years, exactly as they were
before this task. See docs/LAST_CLAUDE_REPORT.md for the full finding.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
RESULTS_PATH = DATA_DIR / "d023_mu_cash_fix_results.json"

ENGINE_VERSION = "v1-cash-label-fix (scripts/82, D-023)"

# Only the 4 actually-affected years get a new extraction_run — the
# control year (MU 2025-08-28) is NOT written here: its results are
# byte-identical to the existing ground truth, so writing a duplicate
# row would add nothing and risks confusion about which run is
# authoritative for an unchanged filing.
WRITTEN_KEYS = ["MU_2021-09-02", "MU_2022-09-01", "MU_2023-08-31", "MU_2024-08-29"]

WRITTEN_METRICS = ["cash_and_equivalents", "adjusted_net_debt", "invested_capital"]


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
                    "D-023: label pattern broadened to accept 'Cash and "
                    "equivalents' alongside 'Cash and cash equivalents' — "
                    "same concept us-gaap:CashAndCashEquivalentsAtCarryingValue, "
                    "same balance-sheet role and structural position as the "
                    "already-verified MU 2025-08-28 control year."
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
                    "iso4217:USD" if value is not None else None,
                    None, None, result["report_date"],
                    "us-gaap:CashAndCashEquivalentsAtCarryingValue"
                    if metric_name == "cash_and_equivalents" else None,
                    None, None, None,
                    metric_name in ("adjusted_net_debt", "invested_capital"),
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
