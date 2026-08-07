"""
Bounded PERSISTENT DuckDB proof — Oracle only.

Creates data\\database\\ai_stock_agent.duckdb (a real file on disk, the
first persistent step after the in-memory proof in
scripts\\63_duckdb_proof_test.py) with the minimum 5 tables needed for
this proof, and loads ONLY Oracle's 5 already-extracted fiscal years
from data\\historical_dataset_v1.json and
data\\historical_review_required_v1.json.

Does NOT call the XBRL engine, Arelle, or SEC EDGAR — read-only with
respect to extraction. Does NOT load the other 8 companies.

Idempotent by design: every table has a natural-key PRIMARY KEY (no
surrogate auto-increment ids), and every INSERT uses
"ON CONFLICT DO NOTHING", so running this script a second time with the
same source data reloads nothing and creates zero duplicate rows —
consistent with the project's point-in-time rule that a later run must
never silently replace an earlier point-in-time result. A genuinely new
extraction (a newer engine_version for the same accession) would land
as an ADDITIONAL extraction_runs row, never an overwrite of an existing
one.

Schema:
  companies                 — one row per ticker
  sec_filings                — one row per accession_number (a locked
                                10-K filing)
  extraction_runs             — one row per (accession_number,
                                engine_version) — which engine version
                                produced which metric results, so a
                                future re-extraction with a newer engine
                                is a new row, never a silent overwrite
  financial_metric_results    — one row per (extraction_run, metric) —
                                value, unit, status, validation_reason,
                                lineage; NULL is preserved distinctly
                                from 0 throughout (no coercion)
  historical_review_items     — one row per (extraction_run, metric)
                                where status = REVIEW_REQUIRED, with a
                                root_cause label joined in from
                                data\\historical_review_required_v1.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DATASET_JSON = DATA_DIR / "historical_dataset_v1.json"
REVIEW_REQUIRED_JSON = DATA_DIR / "historical_review_required_v1.json"

DATABASE_DIR = DATA_DIR / "database"
DATABASE_PATH = DATABASE_DIR / "ai_stock_agent.duckdb"

TARGET_TICKER = "ORCL"

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS companies (
        ticker VARCHAR PRIMARY KEY,
        company_name VARCHAR,
        cik BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_filings (
        accession_number VARCHAR PRIMARY KEY,
        ticker VARCHAR NOT NULL REFERENCES companies(ticker),
        form VARCHAR,
        report_date DATE NOT NULL,
        filing_date DATE,
        fiscal_year INTEGER,
        prior_report_date DATE,
        source_document VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS extraction_runs (
        extraction_run_id VARCHAR PRIMARY KEY,
        accession_number VARCHAR NOT NULL REFERENCES sec_filings(accession_number),
        engine_version VARCHAR NOT NULL,
        loaded_at TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financial_metric_results (
        extraction_run_id VARCHAR NOT NULL REFERENCES extraction_runs(extraction_run_id),
        metric_name VARCHAR NOT NULL,
        is_primary_metric BOOLEAN,
        status VARCHAR,
        value DOUBLE,
        unit VARCHAR,
        context_id VARCHAR,
        period_start DATE,
        period_end DATE,
        source_concept VARCHAR,
        label VARCHAR,
        statement_role_definition VARCHAR,
        selection_tier VARCHAR,
        is_derived_metric BOOLEAN,
        formula VARCHAR,
        validation_reason VARCHAR,
        PRIMARY KEY (extraction_run_id, metric_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS historical_review_items (
        extraction_run_id VARCHAR NOT NULL REFERENCES extraction_runs(extraction_run_id),
        ticker VARCHAR NOT NULL,
        report_date DATE NOT NULL,
        metric_name VARCHAR NOT NULL,
        root_cause VARCHAR,
        validation_reason VARCHAR,
        PRIMARY KEY (extraction_run_id, metric_name)
    )
    """,
]


def load_root_cause_lookup() -> dict[tuple[str, str, str], str]:
    """(ticker, report_date, metric_name) -> root_cause label."""

    with REVIEW_REQUIRED_JSON.open(encoding="utf-8-sig") as handle:
        data = json.load(handle)

    lookup: dict[tuple[str, str, str], str] = {}

    for root_cause, cases in data["cases_by_root_cause"].items():
        for case in cases:
            key = (case["ticker"], case["report_date"], case["metric"])
            lookup[key] = root_cause

    return lookup


def load_oracle_rows() -> list[dict[str, Any]]:
    with DATASET_JSON.open(encoding="utf-8-sig") as handle:
        rows = json.load(handle)

    return [row for row in rows if row["ticker"] == TARGET_TICKER]


def extraction_run_id(accession_number: str, engine_version: str) -> str:
    return f"{accession_number}::{engine_version}"


def main() -> None:
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)

    oracle_rows = load_oracle_rows()

    if not oracle_rows:
        raise RuntimeError(
            f"לא נמצאו שורות עבור {TARGET_TICKER} ב-{DATASET_JSON}"
        )

    root_cause_lookup = load_root_cause_lookup()

    connection = duckdb.connect(database=str(DATABASE_PATH))

    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)

    # --- companies -----------------------------------------------------
    company_row = oracle_rows[0]
    connection.execute(
        """
        INSERT INTO companies (ticker, company_name, cik)
        VALUES (?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        [TARGET_TICKER, company_row["company_name"], company_row["cik"]],
    )

    # --- sec_filings + extraction_runs (one per accession_number) -----
    filings_by_accession: dict[str, dict[str, Any]] = {}

    for row in oracle_rows:
        filings_by_accession.setdefault(
            row["accession_number"],
            {
                "accession_number": row["accession_number"],
                "ticker": row["ticker"],
                "form": row["form"],
                "report_date": row["report_date"],
                "filing_date": row["filing_date"],
                "fiscal_year": row["fiscal_year"],
                "prior_report_date": row["prior_report_date"],
                "source_document": row["source_document"],
                "engine_version": row["engine_version"],
            },
        )

    for filing in filings_by_accession.values():
        connection.execute(
            """
            INSERT INTO sec_filings (
                accession_number, ticker, form, report_date, filing_date,
                fiscal_year, prior_report_date, source_document
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [
                filing["accession_number"],
                filing["ticker"],
                filing["form"],
                filing["report_date"],
                filing["filing_date"],
                filing["fiscal_year"],
                filing["prior_report_date"],
                filing["source_document"],
            ],
        )

        run_id = extraction_run_id(
            filing["accession_number"], filing["engine_version"]
        )
        connection.execute(
            """
            INSERT INTO extraction_runs (
                extraction_run_id, accession_number, engine_version
            )
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [run_id, filing["accession_number"], filing["engine_version"]],
        )

    # --- financial_metric_results + historical_review_items -----------
    review_items_inserted = 0

    for row in oracle_rows:
        run_id = extraction_run_id(
            row["accession_number"], row["engine_version"]
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
                run_id,
                row["metric_name"],
                row["is_primary_metric"],
                row["status"],
                row["value"],
                row["unit"],
                row["context_id"],
                row["period_start"],
                row["period_end"],
                row["source_concept"],
                row["label"],
                row["statement_role_definition"],
                row["selection_tier"],
                row["is_derived_metric"],
                row["formula"],
                row["validation_reason"],
            ],
        )

        if row["is_primary_metric"] and row["status"] == "REVIEW_REQUIRED":
            root_cause = root_cause_lookup.get(
                (row["ticker"], row["report_date"], row["metric_name"]),
                "unclassified",
            )
            connection.execute(
                """
                INSERT INTO historical_review_items (
                    extraction_run_id, ticker, report_date, metric_name,
                    root_cause, validation_reason
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                [
                    run_id,
                    row["ticker"],
                    row["report_date"],
                    row["metric_name"],
                    root_cause,
                    row["validation_reason"],
                ],
            )
            review_items_inserted += 1

    print(f"מסד נתונים: {DATABASE_PATH}")
    print(f"שורות ORCL שעובדו ממקור הנתונים: {len(oracle_rows)}")
    print(f"שורות REVIEW_REQUIRED (ראשוניות) שעובדו: {review_items_inserted}")
    print()

    print("=== ספירת שורות לפי טבלה ===")
    for table in [
        "companies",
        "sec_filings",
        "extraction_runs",
        "financial_metric_results",
        "historical_review_items",
    ]:
        count = connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        print(f"{table:28s} {count}")

    connection.close()


if __name__ == "__main__":
    main()
