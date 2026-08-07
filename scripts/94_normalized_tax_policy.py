"""
D-027 (Policy D — normalized tax) — applies the approved 21%
normalized-tax-rate policy wherever valid reported tax and pretax facts
exist but the reported effective tax rate is unusable for NOPAT:
  - pretax_income <= 0, OR
  - reported effective_tax_rate < 0, OR
  - reported effective_tax_rate > 1

normalized_tax_rate = 0.21
NOPAT = operating_income * (1 - 0.21)
status = PASS_NORMALIZED_TAX, basis = FIXED_NORMALIZED_TAX_RATE_21_PERCENT

Negative operating_income is allowed to produce negative NOPAT — never
blocked on that basis alone. Only blocked if operating_income itself is
missing/invalid, or pretax_income/income_tax_expense are themselves not
PASS (source facts genuinely ambiguous/corrupted) — none of the 11
affected company-years hit that block (verified below; all have PASS
pretax_income, income_tax_expense, operating_income).

Pure production-DB read + write. No warehouse, no Arelle. Scans ALL 45
company-years generically (ticker-agnostic) for the two numeric
conditions above — the ticker examples in the governing task (ORCL
FY2021, AMZN loss years, CRWD loss years, MU FY2023, PANW FY2021/2022/
2024) are a confirmation set, not an allow-list; NVDA FY2023 was found
by the same generic scan and is included on identical grounds.

This script recomputes effective_tax_rate + nopat only. roic itself is
recomputed by a later, separate final pass (script 95) that combines
the latest nopat (from here) with the latest average_invested_capital
(from script 93) for every company-year still short of a passing roic.
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

ENGINE_VERSION = "v1-normalized-tax-policy (scripts/94, D-027)"
NORMALIZED_TAX_RATE = 0.21


def latest_metric(
    connection: duckdb.DuckDBPyConnection, ticker: str, report_date: str, metric_name: str
) -> dict[str, object] | None:
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

    conversions: list[str] = []
    runs_written = 0
    rows_written = 0

    for ticker, report_date, accession_number in company_years:
        report_date = str(report_date)

        etr = latest_metric(connection, ticker, report_date, "effective_tax_rate")
        if etr and etr["status"] in ("PASS", "PASS_NORMALIZED_TAX"):
            continue  # already fine, no action

        pretax = latest_metric(connection, ticker, report_date, "pretax_income")
        tax_expense = latest_metric(connection, ticker, report_date, "income_tax_expense")
        operating_income = latest_metric(connection, ticker, report_date, "operating_income")

        if not pretax or pretax["status"] != "PASS":
            continue
        if not tax_expense or tax_expense["status"] != "PASS":
            continue
        if not operating_income or operating_income["status"] != "PASS":
            continue

        pretax_value = pretax["value"]
        tax_expense_value = tax_expense["value"]
        operating_income_value = operating_income["value"]

        reported_rate = (
            tax_expense_value / pretax_value if pretax_value not in (0, None) else None
        )

        needs_normalization = (
            pretax_value <= 0
            or (reported_rate is not None and (reported_rate < 0 or reported_rate > 1))
        )

        if not needs_normalization:
            continue  # genuinely a different, independent blocker — not this policy's job

        normalized_nopat = operating_income_value * (1 - NORMALIZED_TAX_RATE)

        run_id = f"{accession_number}::{ENGINE_VERSION}"
        connection.execute(
            """
            INSERT INTO extraction_runs (extraction_run_id, accession_number, engine_version)
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [run_id, accession_number, ENGINE_VERSION],
        )
        runs_written += 1

        lineage_note = (
            f"D-027 Policy D: reported pretax_income={pretax_value}, reported "
            f"income_tax_expense={tax_expense_value}, calculated reported "
            f"effective_tax_rate={reported_rate}, reason=("
            f"{'pretax_income<=0' if pretax_value <= 0 else 'reported rate outside [0,1]'}"
            f"), normalized_rate={NORMALIZED_TAX_RATE}"
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
            VALUES
            (?, 'effective_tax_rate', TRUE, 'PASS_NORMALIZED_TAX', ?, 'ratio',
             NULL, NULL, ?, NULL, NULL, NULL, 'fixed_normalized_rate', FALSE,
             NULL, ?),
            (?, 'nopat', TRUE, 'PASS_NORMALIZED_TAX', ?, 'iso4217:USD',
             NULL, NULL, ?, NULL, NULL, NULL, 'fixed_normalized_rate', TRUE,
             'operating_income * (1 - 0.21)', ?)
            ON CONFLICT DO NOTHING
            """,
            [
                run_id, NORMALIZED_TAX_RATE, report_date, lineage_note,
                run_id, normalized_nopat, report_date, lineage_note,
            ],
        )
        rows_written += 2

        conversions.append(
            f"{ticker} {report_date}: effective_tax_rate/nopat -> PASS_NORMALIZED_TAX "
            f"(nopat={normalized_nopat:.2f}, reported_rate={reported_rate})"
        )
        print(f"{ticker} {report_date}: effective_tax_rate/nopat -> PASS_NORMALIZED_TAX "
              f"nopat={normalized_nopat:.2f} (operating_income={operating_income_value}, "
              f"reported_rate={reported_rate})")

    connection.close()
    total_elapsed = time.perf_counter() - total_start

    print("\n" + "=" * 100)
    print(f"CONVERSIONS ({len(conversions)}):")
    for line in conversions:
        print(f"  {line}")
    print(f"\nextraction_runs written: {runs_written}")
    print(f"financial_metric_results rows written: {rows_written}")
    print(f"total_elapsed_seconds = {total_elapsed:.3f}")


if __name__ == "__main__":
    main()
