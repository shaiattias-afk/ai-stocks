"""
D-026 — loads the PANW zero-long-term-debt-policy results (produced by
scripts/89) into the PRODUCTION database (data/database/ai_stock_agent.duckdb)
as 2 brand-new extraction_run rows (PANW 2023-07-31, PANW 2024-07-31) —
never overwriting any prior result. Natural key
`accession_number::engine_version`, distinct from every existing
engine_version string for these 2 accessions.

PANW 2025-07-31 is NOT written — it is not present in the XBRL warehouse
and loading it would require Arelle, which the governing task explicitly
forbade.

All 6 recalculated metrics are written — long_term_debt, total_debt,
adjusted_net_debt, invested_capital, average_invested_capital, roic —
including PANW 2024's roic, which correctly REMAINS REVIEW_REQUIRED,
blocked by the already-documented, independent
effective_tax_rate::pretax_income_out_of_range finding (nopat blocked,
unrelated to the debt-classification fix). current_debt and
cash_and_equivalents are NOT written here — they are unaffected by this
policy and their existing PASS rows remain the latest/authoritative ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
RESULTS_PATH = DATA_DIR / "d026_panw_zero_ltd_results.json"

ENGINE_VERSION = "v1-zero-long-term-debt-policy (scripts/89, D-026)"

WRITTEN_KEYS = ["PANW_2023-07-31", "PANW_2024-07-31"]
WRITTEN_METRICS = [
    "long_term_debt", "total_debt", "adjusted_net_debt", "invested_capital",
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
            selection_tier = metric.get("selection_tier")
            source_concept = metric.get("concept_qname")

            if metric_name == "long_term_debt" and status == "PASS":
                lineage = metric.get("lineage")
                validation_reason = (
                    "D-026: approved debt maturity policy item 6 — filing "
                    "proves no financial debt remains outstanding beyond "
                    "what current_debt already claimed. Sole debt "
                    "instrument (us-gaap:ConvertibleDebtCurrent) is fully "
                    "current in both the D-019 ancestry resolver and the "
                    "broadened debt-vocabulary search across every "
                    "balance-sheet role; zero unclaimed candidates found. "
                    f"lineage={lineage}"
                )
            elif status == "REVIEW_REQUIRED":
                validation_reason = metric.get("error") or (
                    "blocked by independent, pre-existing "
                    "effective_tax_rate::pretax_income_out_of_range finding "
                    "(nopat unavailable) — unrelated to D-026 debt policy"
                    if metric_name == "roic" else None
                )

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
                    run_id, metric_name,
                    metric_name in ("long_term_debt", "total_debt"),
                    status, value,
                    "iso4217:USD" if metric_name != "roic" and value is not None else (
                        "ratio" if metric_name == "roic" and value is not None else None
                    ),
                    None, None, result["report_date"],
                    source_concept if metric_name == "long_term_debt" else None,
                    None, None, selection_tier,
                    metric_name not in ("long_term_debt",),
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
