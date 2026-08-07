"""
Read-only validation of data\\database\\ai_stock_agent.duckdb after the
Oracle-only load (scripts\\64_build_persistent_duckdb_orcl.py).

Never writes to the database — every statement here is a SELECT. Safe
to run any number of times, including between repeated runs of the
loader, to confirm idempotency.

Validations required for the bounded ORCL persistent-database proof:
  1. Exactly 1 company.
  2. Exactly 5 Oracle filings.
  3. Exactly 100 primary metric results.
  4. No duplicate filing records (accession_number).
  5. No duplicate (extraction_run_id, metric_name) combinations in
     financial_metric_results.
  6. Oracle FY2021 ROIC is REVIEW_REQUIRED with value IS NULL.
  7. All five Oracle ROIC rows match the independently-verified
     historical results (0.01 percentage point tolerance for PASS
     rows).
"""

from __future__ import annotations

from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_DIR / "data" / "database" / "ai_stock_agent.duckdb"

BASELINE_ROIC = {
    "2020-05-31": 0.28489595072429047,
    "2021-05-31": None,
    "2022-05-31": 0.20896225022072454,
    "2023-05-31": 0.18762351031368552,
    "2024-05-31": 0.16362596788407108,
}
TOLERANCE = 0.0001


def main() -> None:
    if not DATABASE_PATH.exists():
        raise FileNotFoundError(f"מסד הנתונים לא נמצא: {DATABASE_PATH}")

    connection = duckdb.connect(database=str(DATABASE_PATH), read_only=True)

    print(f"מאמת: {DATABASE_PATH}")
    print()

    print("=== ספירת שורות לפי טבלה ===")
    row_counts = {}
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
        row_counts[table] = count
        print(f"{table:28s} {count}")
    print()

    findings: list[tuple[str, bool, str]] = []

    # --- 1. exactly 1 company ------------------------------------------
    company_count = row_counts["companies"]
    findings.append(
        (
            "1. בדיוק חברה אחת בטבלת companies",
            company_count == 1,
            f"נמצאו {company_count} חברות (צפוי: 1)",
        )
    )

    # --- 2. exactly 5 Oracle filings ------------------------------------
    orcl_filing_count = connection.execute(
        "SELECT COUNT(*) FROM sec_filings WHERE ticker = 'ORCL'"
    ).fetchone()[0]
    findings.append(
        (
            "2. בדיוק 5 הגשות של ORCL בטבלת sec_filings",
            orcl_filing_count == 5,
            f"נמצאו {orcl_filing_count} הגשות (צפוי: 5)",
        )
    )

    # --- 3. exactly 100 primary metric results --------------------------
    primary_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM financial_metric_results fmr
        JOIN extraction_runs er
          ON fmr.extraction_run_id = er.extraction_run_id
        JOIN sec_filings sf
          ON er.accession_number = sf.accession_number
        WHERE sf.ticker = 'ORCL' AND fmr.is_primary_metric = true
        """
    ).fetchone()[0]
    findings.append(
        (
            "3. בדיוק 100 תוצאות מדד ראשוניות (20 מדדים x 5 שנים)",
            primary_count == 100,
            f"נמצאו {primary_count} תוצאות (צפוי: 100)",
        )
    )

    # --- 4. no duplicate filing records (accession_number) --------------
    dup_filings = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT accession_number, COUNT(*) AS n
            FROM sec_filings
            GROUP BY accession_number
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    findings.append(
        (
            "4. אין רשומות הגשה כפולות (accession_number)",
            dup_filings == 0,
            f"נמצאו {dup_filings} קבוצות accession_number כפולות (צפוי: 0)",
        )
    )

    # --- 5. no duplicate filing + extraction_run + metric combinations --
    dup_metrics = connection.execute(
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
            "5. אין צירופי הגשה+extraction_run+מדד כפולים",
            dup_metrics == 0,
            f"נמצאו {dup_metrics} צירופים כפולים (צפוי: 0)",
        )
    )

    # --- 6 + 7: Oracle ROIC rows ------------------------------------------
    roic_rows = connection.execute(
        """
        SELECT CAST(sf.report_date AS VARCHAR) AS report_date,
               fmr.value, fmr.status
        FROM financial_metric_results fmr
        JOIN extraction_runs er
          ON fmr.extraction_run_id = er.extraction_run_id
        JOIN sec_filings sf
          ON er.accession_number = sf.accession_number
        WHERE sf.ticker = 'ORCL' AND fmr.metric_name = 'roic'
        ORDER BY sf.report_date
        """
    ).fetchall()

    print("=== חמש שורות ה-ROIC של ORCL ===")
    print(f"{'report_date':12s} {'value':>22s}  status")
    for report_date, value, status in roic_rows:
        value_repr = f"{value:.10f}" if value is not None else "NULL"
        print(f"{report_date:12s} {value_repr:>22s}  {status}")
    print()

    fy2021 = next((r for r in roic_rows if r[0] == "2021-05-31"), None)
    check_fy2021 = (
        fy2021 is not None
        and fy2021[2] == "REVIEW_REQUIRED"
        and fy2021[1] is None
    )
    findings.append(
        (
            "6. ORCL FY2021 ROIC: REVIEW_REQUIRED וגם value=NULL",
            check_fy2021,
            f"status={fy2021[2]!r}, value={fy2021[1]!r}"
            if fy2021 is not None
            else "שורת FY2021 לא נמצאה",
        )
    )

    mismatches = []
    checked = 0
    for report_date, value, status in roic_rows:
        baseline = BASELINE_ROIC.get(report_date)
        if status == "PASS":
            checked += 1
            if baseline is None or abs(value - baseline) > TOLERANCE:
                mismatches.append(
                    f"{report_date}: {value} מול בסיס {baseline}"
                )
        elif baseline is not None:
            mismatches.append(
                f"{report_date}: צפוי PASS מול ערך בסיס אך status={status!r}"
            )

    check_all_match = (
        len(roic_rows) == 5 and checked == 4 and not mismatches
    )
    findings.append(
        (
            "7. כל חמש שורות ה-ROIC תואמות לתוצאות ההיסטוריות המאומתות",
            check_all_match,
            f"{checked}/4 ערכי PASS תואמים בטווח {TOLERANCE}, "
            f"{len(mismatches)} חריגות: {mismatches}",
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
