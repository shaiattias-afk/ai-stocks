"""
Bounded DuckDB proof test — in-memory only. (Fixes a bug found in
scripts\\62_duckdb_proof_test.py's own first run: DuckDB's read_csv_auto
correctly auto-detects `report_date` as a native DATE column, not a
string — the prior script's Python-side string formatting/comparison
code (`f"{report_date:12s}"`, `report_date == "2021-05-31"`) was wrong
for a `datetime.date` value: `date.__format__` with a non-empty spec
calls `strftime()`, so ":12s" was silently treated as a literal
strftime pattern and printed "12s" instead of the date; the string
equality check against "2021-05-31" then never matched, causing two
false FAIL results even though the underlying data was correct. Fixed
here by casting report_date to VARCHAR in SQL and comparing strings
consistently. 62 is preserved unmodified as the historical record of
this finding — exactly what this proof was designed to catch.)

Loads data\\historical_dataset_v1.csv into an IN-MEMORY DuckDB
connection (":memory:", never a file on disk) and runs one
reconstruction query (Oracle ROIC, all 5 fiscal years) to prove the
dataset is safe to load before committing to a permanent database.

Does NOT create or modify any persistent .duckdb file. Does NOT call
the XBRL engine, Arelle, or SEC EDGAR — this script only reads the
already-produced data\\historical_dataset_v1.csv.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATASET_CSV = PROJECT_DIR / "data" / "historical_dataset_v1.csv"

# Independently-verified baseline ROIC values for Oracle, from the
# already-completed and already-reported historical extraction
# milestone (docs/CURRENT_STATE.md, "Historical multi-year point-in-time
# extraction — first proof (Oracle, 5 years)"). FY2021 has no baseline
# value — it is expected to be REVIEW_REQUIRED with a NULL value.
BASELINE_ROIC = {
    "2020-05-31": 0.28489595072429047,
    "2021-05-31": None,
    "2022-05-31": 0.20896225022072454,
    "2023-05-31": 0.18762351031368552,
    "2024-05-31": 0.16362596788407108,
}

# 0.01 percentage point == 0.0001 in raw decimal (ROIC is stored as a
# fraction, e.g. 0.1636 == 16.36%).
TOLERANCE = 0.0001


def main() -> None:
    if not DATASET_CSV.exists():
        raise FileNotFoundError(f"מערך הנתונים לא נמצא: {DATASET_CSV}")

    # ":memory:" — no file is created or touched on disk. This is
    # explicitly NOT the permanent database.
    connection = duckdb.connect(database=":memory:")

    connection.execute(
        f"""
        CREATE TABLE dataset AS
        SELECT *
        FROM read_csv_auto(
            '{DATASET_CSV.as_posix()}',
            header = true,
            all_varchar = false
        )
        """
    )

    total_rows = connection.execute(
        "SELECT COUNT(*) FROM dataset"
    ).fetchone()[0]
    print(f"נטענו {total_rows} שורות לטבלת DuckDB בזיכרון (לא נשמר לדיסק).")
    print()

    # --- Reconstruction query: Oracle ROIC, all 5 fiscal years -------
    # report_date is cast to VARCHAR explicitly (DuckDB auto-detected
    # it as a native DATE column) so downstream Python comparisons are
    # unambiguous string-to-string, not date-to-string.
    rows = connection.execute(
        """
        SELECT CAST(report_date AS VARCHAR) AS report_date, value, status
        FROM dataset
        WHERE ticker = 'ORCL' AND metric_name = 'roic'
        ORDER BY report_date
        """
    ).fetchall()

    print("=== שאילתת שחזור: ORCL ROIC, כל 5 שנות הדיווח ===")
    print(f"{'report_date':12s} {'value':>22s}  status")
    for report_date, value, status in rows:
        value_repr = f"{value:.10f}" if value is not None else "NULL"
        print(f"{report_date:12s} {value_repr:>22s}  {status}")
    print()

    findings: list[tuple[str, bool, str]] = []

    # --- Validation 1: exactly 5 rows ---------------------------------
    check_1 = len(rows) == 5
    findings.append(
        (
            "1. בדיוק 5 שורות ROIC של ORCL הוחזרו",
            check_1,
            f"התקבלו {len(rows)} שורות (צפוי: 5)",
        )
    )

    # --- Validation 2: no duplicate (ticker, report_date, metric_name)
    #     across the FULL dataset, not just the ROIC query -----------
    dup_count = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT ticker, report_date, metric_name, COUNT(*) AS n
            FROM dataset
            GROUP BY ticker, report_date, metric_name
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    check_2 = dup_count == 0
    findings.append(
        (
            "2. אין כפילויות ticker+report_date+metric_name במערך המלא",
            check_2,
            f"נמצאו {dup_count} קבוצות כפולות (צפוי: 0)",
        )
    )

    # --- Validation 3: FY2021 REVIEW_REQUIRED + NULL value -----------
    fy2021 = next((r for r in rows if r[0] == "2021-05-31"), None)
    check_3 = (
        fy2021 is not None
        and fy2021[2] == "REVIEW_REQUIRED"
        and fy2021[1] is None
    )
    fy2021_repr = (
        f"status={fy2021[2]!r}, value={fy2021[1]!r}"
        if fy2021 is not None
        else "שורת FY2021 לא נמצאה כלל"
    )
    findings.append(
        (
            "3. FY2021: status=REVIEW_REQUIRED וגם value=NULL",
            check_3,
            fy2021_repr,
        )
    )

    # --- Validation 4: PASS-status values match baseline within
    #     0.01 percentage point (0.0001 raw decimal) -------------------
    tolerance_failures = []
    tolerance_checked = 0

    for report_date, value, status in rows:
        baseline = BASELINE_ROIC.get(report_date)

        if status == "PASS":
            tolerance_checked += 1
            if baseline is None:
                tolerance_failures.append(
                    f"{report_date}: PASS אך אין ערך בסיס להשוואה"
                )
            elif abs(value - baseline) > TOLERANCE:
                tolerance_failures.append(
                    f"{report_date}: {value:.6f} מול בסיס {baseline:.6f} "
                    f"(הפרש {abs(value - baseline):.6f} > {TOLERANCE})"
                )
        elif baseline is not None:
            tolerance_failures.append(
                f"{report_date}: צפוי PASS מול ערך בסיס אך status={status!r}"
            )

    check_4 = tolerance_checked == 4 and not tolerance_failures
    findings.append(
        (
            "4. כל ערכי ROIC (סטטוס PASS) תואמים לבסיס בטווח 0.01 "
            "נקודת אחוז",
            check_4,
            (
                f"{tolerance_checked} ערכים נבדקו, "
                f"{len(tolerance_failures)} חריגות: {tolerance_failures}"
                if tolerance_failures or tolerance_checked != 4
                else f"{tolerance_checked}/4 ערכים תואמים בדיוק הנדרש"
            ),
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
