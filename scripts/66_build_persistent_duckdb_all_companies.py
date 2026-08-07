"""
Full persistent DuckDB load — all 9 validated companies.

Extends the ORCL-only proof (scripts\\64_build_persistent_duckdb_orcl.py)
to the complete historical dataset: ORCL, MSFT, META, NVDA, GOOGL, AMZN,
MU, CRWD, PANW. Same schema, same natural-key PRIMARY KEY +
"ON CONFLICT DO NOTHING" idempotency architecture, verified in the ORCL
proof — unchanged here, only the ticker filter is widened from one
company to all nine (in fact, no ticker filter at all: every row in the
historical dataset is loaded). Running this against the existing
database (which already has ORCL loaded from the proof) is itself the
first idempotency test — ORCL's rows must not duplicate or change.

Does NOT call the XBRL engine, Arelle, or SEC EDGAR. Does NOT modify
data\\historical_dataset_v1.json or any other historical source file —
strictly read-only with respect to them. Does NOT change any accounting
policy — this script contains no metric-derivation logic at all, only
data movement from the already-produced dataset into DuckDB tables.

Schema (identical to scripts\\64, not redefined differently):
  companies, sec_filings, extraction_runs, financial_metric_results,
  historical_review_items — see scripts\\64 for the full column-level
  rationale.

Timing: five phases are measured separately (connect+schema,
source-file read, company/filing/run loading, metric loading,
validation) plus a total, printed at the end.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DATASET_JSON = DATA_DIR / "historical_dataset_v1.json"
REVIEW_REQUIRED_JSON = DATA_DIR / "historical_review_required_v1.json"
QUALITY_REPORT_JSON = DATA_DIR / "historical_quality_report_v1.json"

DATABASE_DIR = DATA_DIR / "database"
DATABASE_PATH = DATABASE_DIR / "ai_stock_agent.duckdb"

TARGET_TICKERS = [
    "ORCL", "MSFT", "META", "NVDA", "GOOGL", "AMZN", "MU", "CRWD", "PANW",
]

ANCHOR_YEAR = {
    "ORCL": "2024-05-31",
    "MSFT": "2024-06-30",
    "META": "2024-12-31",
    "NVDA": "2024-01-28",
    "GOOGL": "2025-12-31",
    "AMZN": "2025-12-31",
    "MU": "2025-08-28",
    "CRWD": "2026-01-31",
    "PANW": "2025-07-31",
}

# Identical schema to scripts\64_build_persistent_duckdb_orcl.py — not
# redefined, just re-applied with IF NOT EXISTS (a no-op against the
# already-created tables from the ORCL proof).
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
    with REVIEW_REQUIRED_JSON.open(encoding="utf-8-sig") as handle:
        data = json.load(handle)

    lookup: dict[tuple[str, str, str], str] = {}

    for root_cause, cases in data["cases_by_root_cause"].items():
        for case in cases:
            key = (case["ticker"], case["report_date"], case["metric"])
            lookup[key] = root_cause

    return lookup


def load_target_rows() -> list[dict[str, Any]]:
    with DATASET_JSON.open(encoding="utf-8-sig") as handle:
        rows = json.load(handle)

    return [row for row in rows if row["ticker"] in TARGET_TICKERS]


def extraction_run_id(accession_number: str, engine_version: str) -> str:
    return f"{accession_number}::{engine_version}"


def main() -> None:
    timings: dict[str, float] = {}
    total_start = time.perf_counter()

    # --- Phase: connect + schema ----------------------------------------
    phase_start = time.perf_counter()
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=str(DATABASE_PATH))
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    timings["connect_and_schema"] = time.perf_counter() - phase_start

    # --- Phase: source-file reading --------------------------------------
    phase_start = time.perf_counter()
    all_rows = load_target_rows()
    root_cause_lookup = load_root_cause_lookup()

    if not all_rows:
        raise RuntimeError(
            f"לא נמצאו שורות עבור {TARGET_TICKERS} ב-{DATASET_JSON}"
        )

    tickers_present = sorted(set(row["ticker"] for row in all_rows))
    missing_tickers = [t for t in TARGET_TICKERS if t not in tickers_present]
    if missing_tickers:
        raise RuntimeError(
            f"חסרות חברות במקור הנתונים ההיסטורי: {missing_tickers}"
        )
    timings["read_source_files"] = time.perf_counter() - phase_start

    # --- Phase: company / filing / extraction_run loading ----------------
    phase_start = time.perf_counter()

    companies_by_ticker: dict[str, dict[str, Any]] = {}
    filings_by_accession: dict[str, dict[str, Any]] = {}

    for row in all_rows:
        companies_by_ticker.setdefault(
            row["ticker"],
            {
                "ticker": row["ticker"],
                "company_name": row["company_name"],
                "cik": row["cik"],
            },
        )
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

    for company in companies_by_ticker.values():
        connection.execute(
            """
            INSERT INTO companies (ticker, company_name, cik)
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [company["ticker"], company["company_name"], company["cik"]],
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

    timings["load_companies_filings_runs"] = time.perf_counter() - phase_start

    # --- Phase: metric loading ---------------------------------------------
    phase_start = time.perf_counter()
    review_items_inserted = 0

    for row in all_rows:
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

    timings["load_metrics"] = time.perf_counter() - phase_start

    # --- Phase: validation (row counts + reconciliation vs. quality report)
    phase_start = time.perf_counter()

    with QUALITY_REPORT_JSON.open(encoding="utf-8-sig") as handle:
        quality_report = json.load(handle)

    row_counts = {}
    for table in [
        "companies",
        "sec_filings",
        "extraction_runs",
        "financial_metric_results",
        "historical_review_items",
    ]:
        row_counts[table] = connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]

    primary_status_counts = dict(
        connection.execute(
            """
            SELECT status, COUNT(*)
            FROM financial_metric_results
            WHERE is_primary_metric = true
            GROUP BY status
            """
        ).fetchall()
    )

    dup_filings = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT accession_number, COUNT(*) AS n
            FROM sec_filings GROUP BY accession_number HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]

    dup_metric_combo = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT er.accession_number, fmr.extraction_run_id,
                   fmr.metric_name, COUNT(*) AS n
            FROM financial_metric_results fmr
            JOIN extraction_runs er
              ON fmr.extraction_run_id = er.extraction_run_id
            GROUP BY er.accession_number, fmr.extraction_run_id,
                     fmr.metric_name
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]

    unlinked_metrics = connection.execute(
        """
        SELECT COUNT(*)
        FROM financial_metric_results fmr
        LEFT JOIN extraction_runs er
          ON fmr.extraction_run_id = er.extraction_run_id
        LEFT JOIN sec_filings sf
          ON er.accession_number = sf.accession_number
        WHERE er.extraction_run_id IS NULL
           OR sf.accession_number IS NULL
           OR sf.filing_date IS NULL
           OR er.engine_version IS NULL
        """
    ).fetchone()[0]

    anchor_traceable = []
    for ticker, anchor_date in ANCHOR_YEAR.items():
        found = connection.execute(
            """
            SELECT COUNT(*)
            FROM financial_metric_results fmr
            JOIN extraction_runs er
              ON fmr.extraction_run_id = er.extraction_run_id
            JOIN sec_filings sf
              ON er.accession_number = sf.accession_number
            WHERE sf.ticker = ? AND sf.report_date = ?
              AND fmr.is_primary_metric = true
            """,
            [ticker, anchor_date],
        ).fetchone()[0]
        anchor_traceable.append((ticker, anchor_date, found))

    timings["validation"] = time.perf_counter() - phase_start
    timings["total"] = time.perf_counter() - total_start

    # --- Report ---------------------------------------------------------
    print(f"מסד נתונים: {DATABASE_PATH}")
    print(f"שורות שעובדו ממקור הנתונים (9 חברות): {len(all_rows)}")
    print(f"שורות REVIEW_REQUIRED (ראשוניות) שעובדו: {review_items_inserted}")
    print()

    print("=== ספירת שורות לפי טבלה ===")
    for table, count in row_counts.items():
        print(f"{table:28s} {count}")
    print()

    print("=== התאמת סטטוסים מול דוח האיכות ===")
    quality_overall = quality_report["overall"]
    print(
        f"DB: PASS={primary_status_counts.get('PASS', 0)}, "
        f"REVIEW_REQUIRED={primary_status_counts.get('REVIEW_REQUIRED', 0)}, "
        f"FAIL={primary_status_counts.get('FAIL', 0)}, "
        f"TIMEOUT={primary_status_counts.get('TIMEOUT', 0)}"
    )
    print(
        f"דוח איכות: PASS={quality_overall['PASS']}, "
        f"REVIEW_REQUIRED={quality_overall['REVIEW_REQUIRED']}, "
        f"FAIL={quality_overall['FAIL']}, "
        f"TIMEOUT={quality_overall['TIMEOUT']}"
    )
    print()

    print("=== כפילויות ===")
    print(f"accession_number כפולים: {dup_filings}")
    print(f"צירופי הגשה+extraction_run+מדד כפולים: {dup_metric_combo}")
    print()

    print("=== שלמות שושלת (lineage) ===")
    print(
        f"תוצאות מדד ללא קישור מלא ל-run/הגשה/תאריך-הגשה/גרסת-מנוע: "
        f"{unlinked_metrics}"
    )
    print()

    print("=== מעקב שנת עוגן לכל חברה ===")
    for ticker, anchor_date, found in anchor_traceable:
        print(f"{ticker:8s} {anchor_date:12s} תוצאות ראשוניות שנמצאו: {found}")
    print()

    print("=== זמני ריצה בפועל (שניות) ===")
    for phase, duration in timings.items():
        print(f"{phase:28s} {duration:.4f}")

    connection.close()


if __name__ == "__main__":
    main()
