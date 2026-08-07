"""
Read-only validation of data\\database\\ai_stock_agent.duckdb after the
full 9-company load (scripts\\66_build_persistent_duckdb_all_companies.py).

Never writes to the database — every statement here is a SELECT. Safe
to run any number of times, including between repeated runs of the
loader, to confirm idempotency.

Required validations:
  1. Reconcile database status counts against
     data\\historical_quality_report_v1.json (900 primary results:
     678 PASS, 222 REVIEW_REQUIRED, 0 FAIL, 0 TIMEOUT).
  2. No duplicate accession_number; no duplicate
     (filing, extraction_run, metric_name) combination.
  3. Every metric result is connected to a filing, an extraction run,
     an accession number, a filing date, and an engine version.
  4. All 9 latest-year regression anchors remain traceable (exactly 20
     primary metric results each).
  5. Company/filing/company-year counts (9 / 45 / 45).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import json

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DATABASE_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
QUALITY_REPORT_JSON = DATA_DIR / "historical_quality_report_v1.json"

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


def main() -> None:
    if not DATABASE_PATH.exists():
        raise FileNotFoundError(f"מסד הנתונים לא נמצא: {DATABASE_PATH}")

    connection = duckdb.connect(database=str(DATABASE_PATH), read_only=True)

    print(f"מאמת: {DATABASE_PATH}")
    print(f"גודל קובץ: {DATABASE_PATH.stat().st_size:,} bytes")
    print()

    row_counts = {}
    for table in [
        "companies", "sec_filings", "extraction_runs",
        "financial_metric_results", "historical_review_items",
    ]:
        row_counts[table] = connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]

    print("=== ספירת שורות לפי טבלה ===")
    for table, count in row_counts.items():
        print(f"{table:28s} {count}")
    print()

    findings: list[tuple[str, bool, str]] = []

    # --- companies / filings / company-years ----------------------------
    findings.append(
        (
            "companies = 9",
            row_counts["companies"] == 9,
            f"נמצאו {row_counts['companies']}",
        )
    )
    findings.append(
        (
            "sec_filings = 45",
            row_counts["sec_filings"] == 45,
            f"נמצאו {row_counts['sec_filings']}",
        )
    )
    company_year_count = connection.execute(
        "SELECT COUNT(DISTINCT (ticker, report_date)) FROM sec_filings"
    ).fetchone()[0]
    findings.append(
        (
            "company-years נבדלים = 45",
            company_year_count == 45,
            f"נמצאו {company_year_count}",
        )
    )

    # --- 1. status reconciliation vs. quality report ---------------------
    with QUALITY_REPORT_JSON.open(encoding="utf-8-sig") as handle:
        quality_report = json.load(handle)["overall"]

    db_status_counts = dict(
        connection.execute(
            """
            SELECT status, COUNT(*)
            FROM financial_metric_results
            WHERE is_primary_metric = true
            GROUP BY status
            """
        ).fetchall()
    )
    db_total_primary = connection.execute(
        "SELECT COUNT(*) FROM financial_metric_results WHERE is_primary_metric = true"
    ).fetchone()[0]

    status_reconciles = (
        db_total_primary == quality_report["total"]
        and db_status_counts.get("PASS", 0) == quality_report["PASS"]
        and db_status_counts.get("REVIEW_REQUIRED", 0)
        == quality_report["REVIEW_REQUIRED"]
        and db_status_counts.get("FAIL", 0) == quality_report["FAIL"]
        and db_status_counts.get("TIMEOUT", 0) == quality_report["TIMEOUT"]
    )
    findings.append(
        (
            "1. התאמת סטטוסים מול historical_quality_report_v1.json",
            status_reconciles,
            f"DB: total={db_total_primary}, {db_status_counts} | "
            f"דוח: total={quality_report['total']}, PASS={quality_report['PASS']}, "
            f"REVIEW_REQUIRED={quality_report['REVIEW_REQUIRED']}, "
            f"FAIL={quality_report['FAIL']}, TIMEOUT={quality_report['TIMEOUT']}",
        )
    )

    # --- 2. no duplicate natural keys -------------------------------------
    dup_filings = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT accession_number, COUNT(*) AS n
            FROM sec_filings GROUP BY accession_number HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    findings.append(
        (
            "2a. אין accession_number כפול",
            dup_filings == 0,
            f"{dup_filings} קבוצות כפולות",
        )
    )

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
    findings.append(
        (
            "2b. אין צירוף הגשה+extraction_run+מדד כפול",
            dup_metric_combo == 0,
            f"{dup_metric_combo} צירופים כפולים",
        )
    )

    # --- 3. every metric result fully linked ------------------------------
    unlinked = connection.execute(
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
    findings.append(
        (
            "3. כל תוצאת מדד מקושרת להגשה+run+accession+filing_date+engine_version",
            unlinked == 0,
            f"{unlinked} תוצאות עם קישור חסר",
        )
    )

    # --- 4. all 9 anchor years traceable, 20 primary results each --------
    anchor_ok = True
    anchor_details = []
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
        if found != 20:
            anchor_ok = False
        anchor_details.append(f"{ticker}={found}")
    findings.append(
        (
            "4. כל 9 שנות העוגן ניתנות למעקב (20 תוצאות ראשוניות כל אחת)",
            anchor_ok,
            ", ".join(anchor_details),
        )
    )

    print("=== תוצאות אימות ===")
    all_passed = True
    for description, passed, detail in findings:
        status_label = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"[{status_label}] {description}")
        print(f"       {detail}")

    print()
    print(f"תוצאה כוללת: {'PASS' if all_passed else 'FAIL'}")

    connection.close()


if __name__ == "__main__":
    main()
