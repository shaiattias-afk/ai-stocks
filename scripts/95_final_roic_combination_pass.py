"""
Final ROIC combination pass — every one of the 5 cleanup groups lists
roic as a metric to recalculate; this closes that out for whichever
company-years now have both a passing average_invested_capital (from
scripts/93) and a passing nopat (unchanged, or normalized by
scripts/94) but still show roic=REVIEW_REQUIRED from before those
fixes existed.

Pure production-DB read + write. No warehouse, no Arelle — this policy
requires zero filing parsing of any kind.

Processes ONE company-year at a time; after each write, immediately
reads the row back to verify it landed before moving to the next
(no batch-then-check-at-the-end) — cheap here since there is no Arelle
step, but doing it anyway per the same one-at-a-time discipline used
for the parsing-heavy groups.
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

ENGINE_VERSION = "v1-final-roic-combination (scripts/95, D-027)"
SUCCESSFUL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "PASS_NORMALIZED_TAX"}


def latest_metric(connection, ticker, report_date, metric_name):
    row = connection.execute(
        """
        WITH ranked AS (
            SELECT f.status, f.value,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.accession_number, f.metric_name
                       ORDER BY r.loaded_at DESC
                   ) rn
            FROM financial_metric_results f
            JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id
            JOIN sec_filings s ON s.accession_number = r.accession_number
            WHERE s.ticker = ? AND s.report_date = ? AND f.metric_name = ?
        )
        SELECT status, value FROM ranked WHERE rn = 1
        """,
        [ticker, report_date, metric_name],
    ).fetchone()
    return {"status": row[0], "value": row[1]} if row else None


def main() -> None:
    total_start = time.perf_counter()
    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH))

    company_years = connection.execute(
        "SELECT DISTINCT s.ticker, s.report_date, r.accession_number "
        "FROM sec_filings s JOIN extraction_runs r ON r.accession_number = s.accession_number "
        "ORDER BY s.ticker, s.report_date"
    ).fetchall()

    completed: list[str] = []
    skipped_not_ready: list[str] = []
    already_pass: list[str] = []
    failed: list[str] = []

    for ticker, report_date, accession_number in company_years:
        report_date = str(report_date)
        item_label = f"{ticker} {report_date}"

        try:
            roic = latest_metric(connection, ticker, report_date, "roic")
            if roic and roic["status"] in SUCCESSFUL_STATUSES:
                already_pass.append(item_label)
                continue

            avg_ic = latest_metric(connection, ticker, report_date, "average_invested_capital")
            nopat = latest_metric(connection, ticker, report_date, "nopat")

            if not avg_ic or avg_ic["status"] not in SUCCESSFUL_STATUSES:
                skipped_not_ready.append(f"{item_label} (average_invested_capital={avg_ic['status'] if avg_ic else 'MISSING'})")
                continue
            if not nopat or nopat["status"] not in SUCCESSFUL_STATUSES:
                skipped_not_ready.append(f"{item_label} (nopat={nopat['status'] if nopat else 'MISSING'})")
                continue
            if avg_ic["value"] is None or avg_ic["value"] == 0:
                skipped_not_ready.append(f"{item_label} (average_invested_capital value is zero/None)")
                continue

            roic_value = nopat["value"] / avg_ic["value"]
            status = (
                "PASS_DIRECT_AGGREGATE" if "PASS_DIRECT_AGGREGATE" in (avg_ic["status"], nopat["status"])
                else "PASS_NORMALIZED_TAX" if nopat["status"] == "PASS_NORMALIZED_TAX"
                else "PASS_MATURITY_BASIS" if avg_ic["status"] == "PASS_MATURITY_BASIS"
                else "PASS"
            )

            run_id = f"{accession_number}::{ENGINE_VERSION}"
            connection.execute(
                """
                INSERT INTO extraction_runs (extraction_run_id, accession_number, engine_version)
                VALUES (?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                [run_id, accession_number, ENGINE_VERSION],
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
                VALUES (?, 'roic', TRUE, ?, ?, 'ratio', NULL, NULL, ?,
                        NULL, NULL, NULL, 'final_combination', TRUE,
                        'nopat / average_invested_capital', ?)
                ON CONFLICT DO NOTHING
                """,
                [
                    run_id, status, roic_value, report_date,
                    f"D-027 final combination: nopat={nopat['value']} (status={nopat['status']}) / "
                    f"average_invested_capital={avg_ic['value']} (status={avg_ic['status']})",
                ],
            )

            # verify immediately
            check = latest_metric(connection, ticker, report_date, "roic")
            if not check or check["status"] != status or check["value"] != roic_value:
                failed.append(f"{item_label} — write did not verify back correctly")
                continue

            completed.append(f"{item_label}: roic -> {status} ({roic_value:.6f})")
            print(f"{item_label}: roic -> {status} ({roic_value:.6f}) [verified]")

        except Exception as exc:  # noqa: BLE001 - report and continue, never crash the whole pass
            failed.append(f"{item_label} — exception: {exc}")
            print(f"{item_label}: FAILED — {exc}")

    connection.close()
    total_elapsed = time.perf_counter() - total_start

    print("\n" + "=" * 100)
    print(f"Completed ({len(completed)}):")
    for line in completed:
        print(f"  {line}")
    print(f"\nAlready PASS (skipped): {len(already_pass)}")
    print(f"\nNot ready yet ({len(skipped_not_ready)}):")
    for line in skipped_not_ready:
        print(f"  {line}")
    print(f"\nFailed ({len(failed)}):")
    for line in failed:
        print(f"  {line}")
    print(f"\ntotal_elapsed_seconds = {total_elapsed:.3f}")


if __name__ == "__main__":
    main()
