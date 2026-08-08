"""
metrics/production_lookup.py — reads the CURRENT, already-approved
`financial_metric_results` row for a given (ticker, report_date,
metric_name). This is a legitimate, documented part of this pipeline's
own architecture, not a workaround: scripts/93, 94, 95, 96, 98, 101,
102, 103, and 105 all read already-resolved metrics this same way
(each one's own docstring calls itself a "Pure production-DB read +
write" step) — e.g. scripts/94's normalized-tax policy reads
pretax_income/income_tax_expense/operating_income from production
before computing nopat; scripts/93's prior-fiscal-year lookup reads
the prior year's own invested_capital from production.

Ported byte-exact from scripts/93_average_invested_capital_prior_
filing_lookup.py's `latest_metric` (the fullest variant — it also
returns the source accession_number; scripts/94/95/96/98/101/102/103/
105 each carry a byte-identical copy minus that one extra column,
confirmed by diff before this port).
"""

from __future__ import annotations

import duckdb


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
    """Ported byte-exact from scripts/93."""
    rows = connection.execute(
        "SELECT DISTINCT s.ticker, s.report_date, r.accession_number "
        "FROM sec_filings s JOIN extraction_runs r ON r.accession_number = s.accession_number "
        "ORDER BY s.ticker, s.report_date"
    ).fetchall()
    return [(t, str(rd), acc) for t, rd, acc in rows]


def prior_report_date_for(ticker: str, report_date: str, all_dates: list[str]) -> str | None:
    """Ported byte-exact from scripts/93. The prior fiscal year's
    report_date is simply the largest date in this ticker's own known
    dates that is STRICTLY earlier than report_date (handles 52/53-week
    calendars correctly without any naive '-1 year' arithmetic — it
    just picks whichever locked filing this ticker actually has
    immediately before the current one)."""

    earlier = [d for d in all_dates if d < report_date]
    return max(earlier) if earlier else None
