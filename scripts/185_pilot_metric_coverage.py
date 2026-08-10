"""Coverage test: run the extraction engine over every company-year in
the warehouse and report how many metrics resolve cleanly.

READ-ONLY. Writes a JSON report and nothing else -- no production table
is touched. The purpose is to find out how the engine behaves on
companies it has never seen, BEFORE committing any of it to production.

Why this matters: every accounting policy in `docs/DECISIONS_LOG.md`
(D-015 onward) was written while looking at 9 technology companies. The
pilot cohort adds a regulated utility, a medical-device maker, a
biotech, and three companies that stopped filing mid-window. Wherever
those hit a pattern the policies never saw, the engine should fail
closed with REVIEW_REQUIRED rather than produce a wrong number -- and
this report shows exactly where that happens.

Period type is read from each concept's OWN declared `period_type` in the
warehouse rather than assumed, because balance-sheet items are `instant`
and income-statement / cash-flow items are `duration`.

    .venv\\Scripts\\python.exe scripts\\185_pilot_metric_coverage.py
"""

from __future__ import annotations

import json
from collections import Counter

import duckdb

from stock_agent import DATA_DIR, WAREHOUSE_DB_PATH
from stock_agent.filings import archive as filings_archive
from stock_agent.extraction.core import (
    BUILT_IN_METRICS,
    TargetRowNotFound,
    identify_canonical_row,
    match_facts_from_warehouse,
    reconstruct_presentation_dataframe,
)

RESULT_PATH = DATA_DIR / "pilot_metric_coverage.json"

METRICS = sorted(BUILT_IN_METRICS)
PASS_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "PASS_NORMALIZED_TAX"}


def concept_period_type(connection, accession_number: str, concept_qname: str) -> str:
    """The concept's own declared period type, never assumed."""
    row = connection.execute(
        "SELECT period_type FROM xbrl_concepts WHERE accession_number = ? AND qname = ?",
        [accession_number, concept_qname],
    ).fetchone()
    return (row[0] if row and row[0] else "duration").strip().lower()


def extract_one(connection, accession_number: str, report_date: str) -> dict[str, dict]:
    presentation = reconstruct_presentation_dataframe(connection, accession_number)
    results: dict[str, dict] = {}

    for metric_name in METRICS:
        metric = BUILT_IN_METRICS[metric_name]
        try:
            row, _candidates = identify_canonical_row(presentation, metric)
        except TargetRowNotFound as exc:
            results[metric_name] = {"status": "REVIEW_REQUIRED", "reason": "ROW_NOT_FOUND",
                                    "error": str(exc)[:200], "value": None}
            continue

        concept = row["concept_qname"]
        period_type = concept_period_type(connection, accession_number, concept)
        decision = match_facts_from_warehouse(
            connection, accession_number, concept, report_date, period_type
        )
        results[metric_name] = {
            "status": decision["status"],
            "reason": None if decision["status"] in PASS_STATUSES else "FACT_MATCH",
            "error": (str(decision.get("error"))[:200] if decision.get("error") else None),
            "value": decision.get("value"),
            "concept_qname": concept,
            "period_type": period_type,
        }
    return results


def main() -> None:
    connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    try:
        # ANNUAL FILINGS ONLY. These are annual metrics: matching them
        # against a 10-Q compares a full-year figure to a quarter and
        # fails by construction, which would understate coverage badly
        # (the 9 production companies hold ~16 quarterly filings each).
        # The form lives in the archive manifest, so attach it.
        connection.execute(
            f"ATTACH '{filings_archive.ARCHIVE_DB_PATH}' AS arch (READ_ONLY)"
        )
        annual = connection.execute("""
            SELECT DISTINCT w.ticker, w.report_date, w.accession_number
            FROM warehouse_runs w
            JOIN arch.filing_archive_manifest m USING (accession_number)
            WHERE w.ticker IS NOT NULL AND m.form = '10-K'
            ORDER BY w.ticker, w.report_date
        """).fetchall()

        print("=" * 104)
        print(f"METRIC COVERAGE over {len(annual)} company-years x {len(METRICS)} metrics (read-only)")
        print("=" * 104)

        per_company: dict[str, Counter] = {}
        rows_out = []
        failures_by_metric: Counter = Counter()

        for ticker, report_date, accession_number in annual:
            try:
                results = extract_one(connection, accession_number, report_date)
            except Exception as exc:  # noqa: BLE001
                print(f"  {ticker:<6} {report_date}  ENGINE ERROR: {type(exc).__name__}: {exc}")
                rows_out.append({"ticker": ticker, "report_date": report_date,
                                 "accession_number": accession_number,
                                 "engine_error": f"{type(exc).__name__}: {exc}"})
                per_company.setdefault(ticker, Counter())["ENGINE_ERROR"] += 1
                continue

            passed = sum(1 for r in results.values() if r["status"] in PASS_STATUSES)
            counter = per_company.setdefault(ticker, Counter())
            counter["pass"] += passed
            counter["total"] += len(METRICS)
            for metric_name, r in results.items():
                if r["status"] not in PASS_STATUSES:
                    failures_by_metric[metric_name] += 1

            rows_out.append({"ticker": ticker, "report_date": report_date,
                             "accession_number": accession_number,
                             "passed": passed, "total": len(METRICS),
                             "metrics": results})

        print(f"\n{'ticker':<8}{'company-years':<15}{'metrics passed':<18}{'rate'}")
        print("-" * 60)
        overall_pass = overall_total = 0
        for ticker in sorted(per_company):
            counter = per_company[ticker]
            years = sum(1 for r in rows_out if r["ticker"] == ticker)
            passed, total = counter["pass"], counter["total"]
            overall_pass += passed
            overall_total += total
            rate = f"{100 * passed / total:.0f}%" if total else "n/a"
            print(f"{ticker:<8}{years:<15}{f'{passed}/{total}':<18}{rate}")

        print("-" * 60)
        print(f"{'TOTAL':<8}{len(rows_out):<15}{f'{overall_pass}/{overall_total}':<18}"
              f"{100 * overall_pass / overall_total:.1f}%" if overall_total else "")

        print("\nmetrics needing review most often:")
        for metric_name, count in failures_by_metric.most_common():
            print(f"   {metric_name:<26}{count}")

        RESULT_PATH.write_text(json.dumps({
            "company_years": len(rows_out),
            "metrics_per_company_year": len(METRICS),
            "overall_pass": overall_pass,
            "overall_total": overall_total,
            "pass_rate_pct": round(100 * overall_pass / overall_total, 1) if overall_total else None,
            "failures_by_metric": dict(failures_by_metric),
            "results": rows_out,
        }, indent=2, default=str), encoding="utf-8")
        print(f"\nwritten: {RESULT_PATH}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
