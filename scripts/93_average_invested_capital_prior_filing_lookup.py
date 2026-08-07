"""
D-027 (Policy A item 7) — average_invested_capital via the previous
FISCAL YEAR'S OWN locked filing and its latest already-approved
invested_capital result, instead of trying to derive an
"invested_capital_prior" from comparative facts embedded inside the
CURRENT filing (which structurally cannot use a maturity-basis fallback
— the debt-maturity schedule is only ever anchored to the current
balance-sheet date, confirmed in D-022/D-024/D-026's own reports).

"Do not require the prior-year value to appear comparatively inside the
current filing. This preserves point-in-time correctness because the
previous filing was already available." — the prior fiscal year is a
SEPARATE, already-locked 10-K in this project's own dataset; using its
own already-computed, already-approved invested_capital is exactly as
point-in-time-correct as using the current filing's own comparative
column would have been, without requiring the (structurally absent)
maturity fallback to exist twice inside one filing.

Pure production-DB read + write. No warehouse, no Arelle. Applies
uniformly (ticker-agnostic) to EVERY company-year in the 45-year
dataset whose average_invested_capital is currently REVIEW_REQUIRED,
wherever both its own invested_capital and its prior fiscal year's
invested_capital already have a successful status (PASS,
PASS_MATURITY_BASIS, or PASS_DIRECT_AGGREGATE) in the production
database — this is what naturally resolves Group 2 (AMZN/GOOGL) and
extends the same fix to whichever Group 1/3/4 years happen to qualify,
with no ticker-specific code anywhere.

Each company's FIRST year in the dataset has no prior locked filing at
all (e.g. AMZN/GOOGL/PANW/MU 2021, META/NVDA/ORCL/MSFT 2020, CRWD 2022)
— average_invested_capital genuinely cannot be computed for those years
under this or any other approved policy; they are correctly left
REVIEW_REQUIRED (missing source data, not a bug).
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

ENGINE_VERSION = "v1-average-invested-capital-prior-filing-lookup (scripts/93, D-027)"

SUCCESSFUL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "PASS_NORMALIZED_TAX"}


def latest_metric(
    connection: duckdb.DuckDBPyConnection, ticker: str, report_date: str, metric_name: str
) -> dict[str, object] | None:
    row = connection.execute(
        """
        WITH ranked AS (
            SELECT f.status, f.value, r.accession_number,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.accession_number, f.metric_name
                       ORDER BY r.loaded_at DESC
                   ) rn
            FROM financial_metric_results f
            JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id
            JOIN sec_filings s ON s.accession_number = r.accession_number
            WHERE s.ticker = ? AND s.report_date = ? AND f.metric_name = ?
        )
        SELECT status, value, accession_number FROM ranked WHERE rn = 1
        """,
        [ticker, report_date, metric_name],
    ).fetchone()

    if row is None:
        return None

    return {"status": row[0], "value": row[1], "accession_number": row[2]}


def all_company_years(connection: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str]]:
    rows = connection.execute(
        "SELECT DISTINCT s.ticker, s.report_date, r.accession_number "
        "FROM sec_filings s JOIN extraction_runs r ON r.accession_number = s.accession_number "
        "ORDER BY s.ticker, s.report_date"
    ).fetchall()
    return [(t, str(rd), acc) for t, rd, acc in rows]


def prior_report_date_for(ticker: str, report_date: str, all_dates: list[str]) -> str | None:
    """The prior fiscal year's report_date is simply the largest date in
    this ticker's own known dates that is STRICTLY earlier than
    report_date (handles 52/53-week calendars correctly without any
    naive '-1 year' arithmetic — it just picks whichever locked filing
    this ticker actually has immediately before the current one)."""

    earlier = [d for d in all_dates if d < report_date]
    return max(earlier) if earlier else None


def main() -> None:
    total_start = time.perf_counter()
    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH))

    company_years = all_company_years(connection)
    dates_by_ticker: dict[str, list[str]] = {}
    for ticker, report_date, _acc in company_years:
        dates_by_ticker.setdefault(ticker, []).append(report_date)

    conversions: list[str] = []
    skipped_no_prior: list[str] = []
    skipped_prior_not_ready: list[str] = []
    skipped_already_pass: list[str] = []
    rows_written = 0
    runs_written = 0

    for ticker, report_date, accession_number in company_years:
        current_avg = latest_metric(connection, ticker, report_date, "average_invested_capital")
        if current_avg and current_avg["status"] in SUCCESSFUL_STATUSES:
            skipped_already_pass.append(f"{ticker} {report_date}")
            continue

        current_ic = latest_metric(connection, ticker, report_date, "invested_capital")
        if not current_ic or current_ic["status"] not in SUCCESSFUL_STATUSES:
            continue  # not this policy's job — invested_capital itself isn't resolved

        prior_report_date = prior_report_date_for(ticker, report_date, dates_by_ticker[ticker])
        if prior_report_date is None:
            skipped_no_prior.append(f"{ticker} {report_date}")
            continue

        prior_ic = latest_metric(connection, ticker, prior_report_date, "invested_capital")
        if not prior_ic or prior_ic["status"] not in SUCCESSFUL_STATUSES:
            skipped_prior_not_ready.append(
                f"{ticker} {report_date} (prior {prior_report_date} invested_capital="
                f"{prior_ic['status'] if prior_ic else 'MISSING'})"
            )
            continue

        avg_value = (current_ic["value"] + prior_ic["value"]) / 2
        status = (
            "PASS_DIRECT_AGGREGATE" if "PASS_DIRECT_AGGREGATE" in (current_ic["status"], prior_ic["status"])
            else "PASS_MATURITY_BASIS" if "PASS_MATURITY_BASIS" in (current_ic["status"], prior_ic["status"])
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
        runs_written += 1

        validation_reason = (
            f"D-027 Policy A item 7: average of this year's own invested_capital "
            f"({current_ic['value']}, status={current_ic['status']}) and the PREVIOUS "
            f"fiscal year's locked filing ({prior_report_date}, accession="
            f"{prior_ic['accession_number']}) own latest-approved invested_capital "
            f"({prior_ic['value']}, status={prior_ic['status']}) — not derived from "
            "comparative facts inside the current filing."
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
            VALUES (?, 'average_invested_capital', TRUE, ?, ?, 'iso4217:USD',
                    NULL, NULL, ?, NULL, NULL, NULL,
                    'prior_fiscal_year_filing_lookup', TRUE,
                    '(invested_capital + prior_fiscal_year_invested_capital) / 2', ?)
            ON CONFLICT DO NOTHING
            """,
            [run_id, status, avg_value, report_date, validation_reason],
        )
        rows_written += 1

        conversions.append(
            f"{ticker} {report_date}: average_invested_capital REVIEW_REQUIRED -> "
            f"{status} ({avg_value}) [prior={prior_report_date}]"
        )
        print(f"{ticker} {report_date}: average_invested_capital -> {status} value={avg_value:.2f} "
              f"(current_ic={current_ic['value']:.2f}/{current_ic['status']}, "
              f"prior_ic={prior_ic['value']:.2f}/{prior_ic['status']} from {prior_report_date})")

    connection.close()
    total_elapsed = time.perf_counter() - total_start

    print("\n" + "=" * 100)
    print(f"CONVERSIONS ({len(conversions)}):")
    for line in conversions:
        print(f"  {line}")
    print(f"\nAlready PASS/skipped (no action needed): {len(skipped_already_pass)}")
    print(f"Skipped — no prior filing in dataset (genuinely missing, permanent): {len(skipped_no_prior)}")
    for line in skipped_no_prior:
        print(f"  {line}")
    print(f"Skipped — prior filing's invested_capital not yet resolved: {len(skipped_prior_not_ready)}")
    for line in skipped_prior_not_ready:
        print(f"  {line}")
    print(f"\nextraction_runs written: {runs_written}")
    print(f"financial_metric_results rows written: {rows_written}")
    print(f"total_elapsed_seconds = {total_elapsed:.3f}")


if __name__ == "__main__":
    main()
