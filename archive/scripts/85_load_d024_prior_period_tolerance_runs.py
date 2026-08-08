"""
D-024 — loads the 4 affected MU average_invested_capital/roic results
(produced by scripts/84) into the PRODUCTION database
(data/database/ai_stock_agent.duckdb) as 4 brand-new extraction_run
rows — never overwriting any prior result. Natural key
`accession_number::engine_version`, distinct from every existing
engine_version string for these 4 accessions.

Only `average_invested_capital` and `roic` are written — the 2 metrics
this fix actually recalculates. The control year (MU 2025-08-28) is
NOT written: its results are byte-identical to the existing ground
truth (confirmed by scripts/84's control test), so no new row is
needed. Full prior-period date-matching evidence (requested date,
matched date, day difference, tolerance applied) is preserved in
validation_reason for audit lineage.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
RESULTS_PATH = DATA_DIR / "d024_prior_period_tolerance_results.json"

ENGINE_VERSION = "v1-prior-period-tolerance (scripts/84, D-024)"

WRITTEN_KEYS = ["MU_2021-09-02", "MU_2022-09-01", "MU_2023-08-31", "MU_2024-08-29"]
WRITTEN_METRICS = ["average_invested_capital", "roic"]


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

        ic_prior_evidence = result["cash_and_equivalents_prior"].get("date_evidence", {})

        for metric_name in WRITTEN_METRICS:
            metric = result[metric_name]
            status = metric["status"]
            value = metric.get("value")

            if status == "PASS":
                validation_reason = (
                    f"D-024: prior-period lookup used PRIOR_PERIOD_DATE_TOLERANCE_DAYS=10 "
                    f"(same policy as the live engine). requested_prior_date="
                    f"{result['prior_report_date_requested']}; matched_prior_date="
                    f"{ic_prior_evidence.get('matched_date')}; date_diff_days="
                    f"{ic_prior_evidence.get('date_diff_days')}."
                )
            elif status == "REVIEW_REQUIRED":
                validation_reason = (
                    "Not resolved by D-024 — blocked by an unrelated, independent cause "
                    "(nopat/effective_tax_rate), not by prior-period date matching."
                )
            else:
                validation_reason = None

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
                    "iso4217:USD" if metric_name == "average_invested_capital" and value is not None else (
                        "ratio" if metric_name == "roic" and value is not None else None
                    ),
                    None, None, result["report_date"], None, None, None, None,
                    True, None, validation_reason,
                ],
            )
            new_rows += 1

    connection.close()

    print(f"extraction_runs written/confirmed: {new_runs}")
    print(f"financial_metric_results rows written/confirmed: {new_rows}")
    print(f"engine_version: {ENGINE_VERSION}")


if __name__ == "__main__":
    main()
