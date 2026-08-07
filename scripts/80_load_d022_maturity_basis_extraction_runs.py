"""
D-022 — loads the 10 AMZN/GOOGL maturity-basis results (produced by
scripts/79) into the PRODUCTION database (data/database/ai_stock_agent.duckdb)
as 10 brand-new extraction_run rows — never overwriting any prior
result. Natural key `accession_number::engine_version`, and
engine_version here ("v1-maturity-basis (scripts/79, D-022)") differs
from every existing v14/v15/v16 engine_version string for these same
10 accessions, so the old rows are left completely untouched, still
queryable, coexisting with these new ones — the same idempotent
pattern already used by scripts/70 for the D-020 milestone.

Only current_debt, long_term_debt, total_debt, adjusted_net_debt,
invested_capital, average_invested_capital, and roic are written by
this script (the metrics D-022 actually recalculates) — every other
primary metric for these 10 filings is intentionally NOT duplicated
here; it is unchanged and already correctly recorded under the prior
engine_version rows for the same accessions.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
RESULTS_PATH = DATA_DIR / "d022_maturity_basis_results.json"

ENGINE_VERSION = "v1-maturity-basis (scripts/79, D-022)"

WRITTEN_METRICS = [
    "current_debt", "long_term_debt", "total_debt", "adjusted_net_debt",
    "invested_capital", "average_invested_capital", "roic",
]


def main() -> None:
    with RESULTS_PATH.open(encoding="utf-8") as handle:
        all_results = json.load(handle)

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH))

    new_runs = 0
    new_rows = 0

    for key, result in all_results.items():
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
            basis = metric.get("basis")

            validation_reason = None
            if status == "PASS_MATURITY_BASIS":
                validation_reason = (
                    f"basis=MATURITY_PRINCIPAL; source_current_debt_maturity="
                    f"{metric.get('source_current_debt_maturity')}; "
                    f"source_long_term_debt_maturity="
                    f"{metric.get('source_long_term_debt_maturity')}; "
                    f"reported_total={metric.get('reported_total')}; "
                    f"reconciliation_gap={metric.get('reconciliation_gap')}; "
                    f"carrying_value_current_debt="
                    f"{metric.get('carrying_value_current_debt')} "
                    f"({metric.get('carrying_value_current_debt_status')}); "
                    f"carrying_value_long_term_debt="
                    f"{metric.get('carrying_value_long_term_debt')} "
                    f"({metric.get('carrying_value_long_term_debt_status')})"
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
                    None, None, result["report_date"], None, None,
                    None, basis,
                    metric_name in ("total_debt", "adjusted_net_debt",
                                     "invested_capital",
                                     "average_invested_capital", "roic"),
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
