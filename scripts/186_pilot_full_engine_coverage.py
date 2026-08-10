"""Coverage with the FULL accounting engine, not just row identification.

scripts/185 measured a floor (80.1%) using only label-based row lookup.
It deliberately skipped the accounting policies in D-016..D-028 -- the
tiered current-debt resolution, maturity-basis fallback, proven-zero
inference, direct-aggregate totals -- which exist precisely to resolve
the debt metrics that made up 42% of that run's failures.

This script runs those policies via metrics.annual.compute_company_year
for the 8 debt/cash/equity metrics, and the row-identification path for
the 7 income-statement and cash-flow metrics, plus free_cash_flow as
operating_cash_flow - capex. That is the same 16-metric shape the
production annual pipeline produces, minus the tax/ROIC chain which
needs a prior fiscal year and is out of scope for a coverage check.

READ-ONLY: writes a JSON report, touches no production table.

    .venv\\Scripts\\python.exe scripts\\186_pilot_full_engine_coverage.py
"""

from __future__ import annotations

import json
from collections import Counter

import duckdb

from stock_agent import DATA_DIR, WAREHOUSE_DB_PATH
from stock_agent.extraction.core import (
    BUILT_IN_METRICS,
    TargetRowNotFound,
    identify_canonical_row,
    match_facts_from_warehouse,
    reconstruct_presentation_dataframe,
)
from stock_agent.filings import archive as filings_archive
from stock_agent.metrics.annual import SUCCESSFUL_STATUSES, compute_company_year

RESULT_PATH = DATA_DIR / "pilot_full_engine_coverage.json"
BASELINE_PATH = DATA_DIR / "pilot_metric_coverage.json"

POLICY_METRICS = ["current_debt", "long_term_debt", "total_debt", "cash_and_equivalents",
                  "short_term_investments", "stockholders_equity", "adjusted_net_debt",
                  "invested_capital"]
FLOW_METRICS = ["revenue", "operating_income", "net_income", "pretax_income",
                "income_tax_expense", "operating_cash_flow", "capex"]
ALL_METRICS = POLICY_METRICS + FLOW_METRICS + ["free_cash_flow"]


def concept_period_type(connection, accession_number: str, concept_qname: str) -> str:
    row = connection.execute(
        "SELECT period_type FROM xbrl_concepts WHERE accession_number = ? AND qname = ?",
        [accession_number, concept_qname],
    ).fetchone()
    return (row[0] if row and row[0] else "duration").strip().lower()


def resolve_flow_metric(connection, accession_number, presentation, metric_name, report_date) -> dict:
    metric = BUILT_IN_METRICS[metric_name]
    try:
        row, _ = identify_canonical_row(presentation, metric)
    except TargetRowNotFound as exc:
        return {"status": "REVIEW_REQUIRED", "reason": "ROW_NOT_FOUND",
                "error": str(exc)[:200], "value": None}
    concept = row["concept_qname"]
    decision = match_facts_from_warehouse(
        connection, accession_number, concept, report_date,
        concept_period_type(connection, accession_number, concept),
    )
    return {"status": decision["status"],
            "reason": None if decision["status"] in SUCCESSFUL_STATUSES else "FACT_MATCH",
            "error": (str(decision.get("error"))[:200] if decision.get("error") else None),
            "value": decision.get("value"), "concept_qname": concept}


def main() -> None:
    connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    try:
        connection.execute(f"ATTACH '{filings_archive.ARCHIVE_DB_PATH}' AS arch (READ_ONLY)")
        annual = connection.execute("""
            SELECT DISTINCT w.ticker, w.report_date, w.accession_number
            FROM warehouse_runs w
            JOIN arch.filing_archive_manifest m USING (accession_number)
            WHERE w.ticker IS NOT NULL AND m.form = '10-K'
            ORDER BY w.ticker, w.report_date
        """).fetchall()

        print("=" * 104)
        print(f"FULL-ENGINE COVERAGE: {len(annual)} company-years x {len(ALL_METRICS)} metrics")
        print("=" * 104)

        per_company: dict[str, Counter] = {}
        failures_by_metric: Counter = Counter()
        rows_out = []

        for ticker, report_date, accession_number in annual:
            metrics: dict[str, dict] = {}
            try:
                policy = compute_company_year(connection, ticker, report_date, accession_number)
                for name in POLICY_METRICS:
                    entry = policy[name]
                    metrics[name] = {"status": entry.get("status"), "value": entry.get("value"),
                                     "basis": entry.get("basis")}
            except Exception as exc:  # noqa: BLE001
                for name in POLICY_METRICS:
                    metrics[name] = {"status": "REVIEW_REQUIRED",
                                     "error": f"{type(exc).__name__}: {exc}"[:200], "value": None}

            try:
                presentation = reconstruct_presentation_dataframe(connection, accession_number)
                for name in FLOW_METRICS:
                    metrics[name] = resolve_flow_metric(
                        connection, accession_number, presentation, name, report_date)
            except Exception as exc:  # noqa: BLE001
                for name in FLOW_METRICS:
                    metrics.setdefault(name, {"status": "REVIEW_REQUIRED",
                                              "error": f"{type(exc).__name__}: {exc}"[:200], "value": None})

            ocf, capex = metrics.get("operating_cash_flow", {}), metrics.get("capex", {})
            if ocf.get("status") in SUCCESSFUL_STATUSES and capex.get("status") in SUCCESSFUL_STATUSES:
                metrics["free_cash_flow"] = {"status": "PASS",
                                             "value": ocf["value"] - capex["value"],
                                             "basis": "operating_cash_flow - capex"}
            else:
                metrics["free_cash_flow"] = {"status": "REVIEW_REQUIRED", "value": None,
                                             "reason": "COMPONENT_UNRESOLVED"}

            passed = sum(1 for m in metrics.values() if m.get("status") in SUCCESSFUL_STATUSES)
            counter = per_company.setdefault(ticker, Counter())
            counter["pass"] += passed
            counter["total"] += len(ALL_METRICS)
            for name, m in metrics.items():
                if m.get("status") not in SUCCESSFUL_STATUSES:
                    failures_by_metric[name] += 1

            rows_out.append({"ticker": ticker, "report_date": report_date,
                             "accession_number": accession_number,
                             "passed": passed, "total": len(ALL_METRICS), "metrics": metrics})

        baseline = {}
        if BASELINE_PATH.exists():
            for row in json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["results"]:
                if "passed" in row:
                    b = baseline.setdefault(row["ticker"], [0, 0])
                    b[0] += row["passed"]
                    b[1] += row["total"]

        print(f"\n{'ticker':<8}{'years':<7}{'full engine':<16}{'rate':<9}{'row-only baseline':<20}{'change'}")
        print("-" * 78)
        overall_pass = overall_total = 0
        for ticker in sorted(per_company):
            c = per_company[ticker]
            years = sum(1 for r in rows_out if r["ticker"] == ticker)
            overall_pass += c["pass"]
            overall_total += c["total"]
            rate = 100 * c["pass"] / c["total"]
            if ticker in baseline and baseline[ticker][1]:
                b_rate = 100 * baseline[ticker][0] / baseline[ticker][1]
                base_txt = f"{baseline[ticker][0]}/{baseline[ticker][1]} ({b_rate:.0f}%)"
                change = f"{rate - b_rate:+.0f} pts"
            else:
                base_txt, change = "-", "-"
            passed_of_total = f"{c['pass']}/{c['total']}"
            print(f"{ticker:<8}{years:<7}{passed_of_total:<16}"
                  f"{rate:.0f}%{'':<5}{base_txt:<20}{change}")

        print("-" * 78)
        print(f"{'TOTAL':<8}{len(rows_out):<7}{f'{overall_pass}/{overall_total}':<16}"
              f"{100 * overall_pass / overall_total:.1f}%")

        print("\nmetrics still needing review:")
        for name, count in failures_by_metric.most_common():
            print(f"   {name:<26}{count:>4} of {len(rows_out)}")

        RESULT_PATH.write_text(json.dumps({
            "company_years": len(rows_out),
            "metrics_per_company_year": len(ALL_METRICS),
            "overall_pass": overall_pass, "overall_total": overall_total,
            "pass_rate_pct": round(100 * overall_pass / overall_total, 1),
            "failures_by_metric": dict(failures_by_metric),
            "results": rows_out,
        }, indent=2, default=str), encoding="utf-8")
        print(f"\nwritten: {RESULT_PATH}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
