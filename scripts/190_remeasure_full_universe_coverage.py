"""Read-only coverage re-measurement for every company-year already
loaded via scripts/188, using the CURRENT engine (post D-P1 registrant
narrowing, the "comprehensive"-statement role-exclude fix, the current_
debt exception-isolation fix, and D-P2's capex component aggregate).

Unlike scripts/188, this recomputes EVERY company-year (not just ones
missing from production) and writes NOTHING -- it exists to quantify how
much the engine fixes above actually moved coverage, for comparison
against the stored scripts/188-era numbers already in production.

    .venv\\Scripts\\python.exe scripts\\190_remeasure_full_universe_coverage.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH, WAREHOUSE_DB_PATH
from stock_agent.extraction.core import (
    BUILT_IN_METRICS, TargetRowNotFound, identify_canonical_row,
    match_facts_from_warehouse, reconstruct_presentation_dataframe,
)
from stock_agent.filings import archive as filings_archive
from stock_agent.metrics import production_lookup
from stock_agent.metrics.annual import SUCCESSFUL_STATUSES, compute_company_year
from stock_agent.policies.capex_components import resolve_capex_by_component_aggregate
from stock_agent.policies.prior_fiscal_year_lookup import combine_current_and_prior_invested_capital
from stock_agent.policies.roic_nopat import combine_average_invested_capital_and_nopat_into_roic
from stock_agent.policies.tax_normalization import compute_normalized_tax_nopat

RESULT_PATH = DATA_DIR / "full_universe_remeasure_result.json"

POLICY_METRICS = ["current_debt", "long_term_debt", "total_debt", "cash_and_equivalents",
                  "short_term_investments", "stockholders_equity", "adjusted_net_debt",
                  "invested_capital"]
FLOW_METRICS = ["revenue", "operating_income", "net_income", "pretax_income",
                "income_tax_expense", "operating_cash_flow", "capex"]
DERIVED = ["free_cash_flow", "effective_tax_rate", "nopat",
           "average_invested_capital", "roic"]
ALL_20 = POLICY_METRICS + FLOW_METRICS + DERIVED

# The original frozen baseline (docs/CLEANUP_DECISIONS_PENDING.md,
# tests/test_golden_regression.py) -- the only tickers whose 8 "out of
# scope" flow metrics were ever produced by the separate v14/v15 engine.
FROZEN_NINE_TICKERS = frozenset({
    "AMZN", "CRWD", "GOOGL", "META", "MSFT", "MU", "NVDA", "ORCL", "PANW",
})


def concept_period_type(connection, accession_number, concept_qname) -> str:
    row = connection.execute(
        "SELECT period_type FROM xbrl_concepts WHERE accession_number = ? AND qname = ?",
        [accession_number, concept_qname]).fetchone()
    return (row[0] if row and row[0] else "duration").strip().lower()


def resolve_flow(connection, production, ticker, accession_number, presentation, metric_name, report_date) -> dict:
    # Mirrors metrics.annual.compute_full_company_year's OUT_OF_SCOPE_
    # PASSTHROUGH_METRICS exactly, and ONLY for the frozen 9: their
    # values were produced by a DIFFERENT, separate engine (v14/v15,
    # scripts/60/69) that identify_canonical_row's BUILT_IN_METRICS
    # patterns are not proven equivalent to (out of scope for this
    # engine's own PR, per metrics/annual.py's module docstring).
    # Recomputing them here instead of reading the already-approved
    # production value would compare the frozen 9 against the wrong
    # engine and report a false "regression" for company-years the golden
    # regression already proves are untouched. The wider universe (loaded
    # by scripts/188 via THIS SAME identify_canonical_row path) must
    # always be recomputed here -- passing through its stale, pre-fix
    # production rows would silently hide the improvement this script
    # exists to measure.
    if ticker in FROZEN_NINE_TICKERS:
        looked_up = production_lookup.latest_metric(production, ticker, report_date, metric_name)
        if looked_up is not None:
            return {"status": looked_up["status"], "value": looked_up["value"],
                    "validation_reason": None, "basis": "PRODUCTION_PASSTHROUGH_OUT_OF_SCOPE"}

    if metric_name == "capex":
        aggregate = resolve_capex_by_component_aggregate(
            connection, accession_number, presentation, report_date)
        if aggregate is not None:
            return {
                "status": aggregate["status"], "value": aggregate.get("value"),
                "source_concept": aggregate.get("concept_qname"),
                "selection_tier": aggregate.get("selection_tier"),
                "validation_reason": aggregate.get("error"),
            }
    metric = BUILT_IN_METRICS[metric_name]
    try:
        row, _ = identify_canonical_row(presentation, metric)
    except TargetRowNotFound as exc:
        return {"status": "REVIEW_REQUIRED", "value": None, "validation_reason": str(exc)[:400]}
    concept = row["concept_qname"]
    decision = match_facts_from_warehouse(
        connection, accession_number, concept, report_date,
        concept_period_type(connection, accession_number, concept))
    return {"status": decision["status"], "value": decision.get("value"),
            "source_concept": concept, "label": row.get("label"),
            "selection_tier": row.get("selection_tier"),
            "validation_reason": (str(decision.get("error"))[:400] if decision.get("error") else None)}


def compute_all_20(connection, production, ticker, report_date, accession_number, prior_ic) -> dict[str, dict]:
    metrics: dict[str, dict] = {}

    # Each POLICY_METRIC isolated independently -- compute_company_year
    # itself now fails closed per-metric for the one known crash site
    # (current_debt's ancestry ambiguity), but this loop still isolates
    # any OTHER, not-yet-seen exception to a single metric rather than
    # letting it blank all 8, matching this script's own read-only,
    # diagnostic purpose (never hide a metric that DID resolve just
    # because a sibling metric's resolution broke).
    try:
        policy = compute_company_year(connection, ticker, report_date, accession_number)
        for name in POLICY_METRICS:
            entry = policy[name]
            metrics[name] = {"status": entry.get("status"), "value": entry.get("value"),
                             "source_concept": entry.get("concept_qname"),
                             "selection_tier": entry.get("selection_tier"),
                             "formula": entry.get("basis"),
                             "validation_reason": (str(entry.get("error"))[:400]
                                                   if entry.get("error") else None)}
    except Exception as exc:  # noqa: BLE001
        for name in POLICY_METRICS:
            metrics[name] = {"status": "REVIEW_REQUIRED", "value": None,
                             "validation_reason": f"{type(exc).__name__}: {exc}"[:400]}

    try:
        presentation = reconstruct_presentation_dataframe(connection, accession_number)
        for name in FLOW_METRICS:
            metrics[name] = resolve_flow(connection, production, ticker, accession_number, presentation, name, report_date)
    except Exception as exc:  # noqa: BLE001
        for name in FLOW_METRICS:
            metrics.setdefault(name, {"status": "REVIEW_REQUIRED", "value": None,
                                      "validation_reason": f"{type(exc).__name__}: {exc}"[:400]})

    def ok(name: str) -> bool:
        return metrics.get(name, {}).get("status") in SUCCESSFUL_STATUSES

    metrics["free_cash_flow"] = (
        {"status": "PASS", "value": metrics["operating_cash_flow"]["value"] - metrics["capex"]["value"],
         "formula": "operating_cash_flow - capex", "is_derived": True}
        if ok("operating_cash_flow") and ok("capex")
        else {"status": "REVIEW_REQUIRED", "value": None, "is_derived": True,
              "validation_reason": "component unresolved"})

    rate = nopat = {"status": "REVIEW_REQUIRED", "value": None, "is_derived": True,
                    "validation_reason": "components unresolved"}
    if ok("pretax_income") and ok("income_tax_expense") and ok("operating_income"):
        pretax = metrics["pretax_income"]["value"]
        tax = metrics["income_tax_expense"]["value"]
        operating = metrics["operating_income"]["value"]
        reported = tax / pretax if pretax else None
        if pretax > 0 and reported is not None and 0.0 <= reported <= 1.0:
            rate = {"status": "PASS", "value": reported, "is_derived": True,
                    "formula": "income_tax_expense / pretax_income"}
            nopat = {"status": "PASS", "value": operating * (1 - reported), "is_derived": True,
                     "formula": "operating_income * (1 - effective_tax_rate)"}
        else:
            normalized = compute_normalized_tax_nopat(pretax, tax, operating)
            if normalized and normalized.get("nopat") is not None:
                rate = {"status": normalized["status"],
                        "value": normalized["effective_tax_rate"], "is_derived": True,
                        "formula": normalized["basis"]}
                nopat = {"status": normalized["status"],
                         "value": normalized["nopat"], "is_derived": True,
                         "formula": normalized["basis"]}
            else:
                reason = ("pretax_income <= 0 or reported rate outside [0,1]; "
                          "normalization not applicable (D-015 rule 3)")
                rate = {"status": "REVIEW_REQUIRED", "value": None, "is_derived": True,
                        "validation_reason": reason}
                nopat = dict(rate)
    metrics["effective_tax_rate"] = rate
    metrics["nopat"] = nopat

    if prior_ic is None and ticker in FROZEN_NINE_TICKERS:
        # Mirrors metrics.annual.compute_full_company_year's own special
        # case exactly: this ticker's first locked annual filing in the
        # dataset has no separate, earlier filing to average against, but
        # still carries a valid, already-approved production value
        # computed by a mechanism out of scope here (D-024's earlier,
        # narrower within-filing prior-period lookup, predating scripts/
        # 93's general prior-fiscal-year policy). Passthrough, exactly
        # like the flow metrics above, instead of reporting a false
        # REVIEW_REQUIRED this script's own simplified prior-year tracking
        # would otherwise produce.
        looked_up = production_lookup.latest_metric(production, ticker, report_date, "average_invested_capital")
        avg = (
            {"status": looked_up["status"], "value": looked_up["value"]}
            if looked_up is not None else {"status": "REVIEW_REQUIRED", "value": None}
        )
    else:
        ic = metrics.get("invested_capital", {})
        avg = combine_current_and_prior_invested_capital(
            ic.get("status") or "REVIEW_REQUIRED", ic.get("value"),
            (prior_ic or {}).get("status") or "REVIEW_REQUIRED", (prior_ic or {}).get("value"))
    metrics["average_invested_capital"] = {
        "status": avg.get("status"), "value": avg.get("value"), "is_derived": True,
        "formula": "average(invested_capital current, invested_capital prior)",
        "validation_reason": avg.get("error")}

    roic = combine_average_invested_capital_and_nopat_into_roic(
        avg.get("status"), avg.get("value"), nopat.get("status"), nopat.get("value"))
    metrics["roic"] = {"status": roic.get("status"), "value": roic.get("value"),
                       "is_derived": True, "formula": "nopat / average_invested_capital",
                       "validation_reason": roic.get("error")}
    return metrics


def main() -> None:
    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    stored = {
        (t, str(rd), a): passed
        for t, rd, a, passed in production.execute("""
            SELECT sf.ticker, sf.report_date, sf.accession_number,
                   SUM(CASE WHEN fmr.status LIKE 'PASS%' THEN 1 ELSE 0 END)
            FROM financial_metric_results fmr
            JOIN extraction_runs er ON er.extraction_run_id = fmr.extraction_run_id
            JOIN sec_filings sf ON sf.accession_number = er.accession_number
            GROUP BY 1, 2, 3
        """).fetchall()
    }

    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    warehouse.execute(f"ATTACH '{filings_archive.ARCHIVE_DB_PATH}' AS arch (READ_ONLY)")
    annual = warehouse.execute("""
        SELECT DISTINCT w.ticker, w.report_date, w.accession_number
        FROM warehouse_runs w JOIN arch.filing_archive_manifest m USING (accession_number)
        WHERE w.ticker IS NOT NULL AND m.form = '10-K'
        ORDER BY w.ticker, w.report_date
    """).fetchall()

    print("=" * 92)
    print(f"annual filings in warehouse : {len(annual)}")
    print("=" * 92)

    summary = []
    ic_by_ticker: dict[str, dict[str, dict]] = {}
    total_pass = total_attempts = 0
    improved, regressed = [], []

    for ticker, report_date, accession_number in annual:
        report_date = str(report_date)
        prior_year = str(int(report_date[:4]) - 1)
        prior_ic = next((v for k, v in ic_by_ticker.get(ticker, {}).items()
                         if k.startswith(prior_year)), None)
        metrics = compute_all_20(warehouse, production, ticker, report_date, accession_number, prior_ic)
        ic_by_ticker.setdefault(ticker, {})[report_date] = metrics.get("invested_capital", {})

        passed = sum(1 for n in ALL_20 if metrics.get(n, {}).get("status") in SUCCESSFUL_STATUSES)
        total_pass += passed
        total_attempts += len(ALL_20)

        key = (ticker, report_date, accession_number)
        before = stored.get(key)
        if before is not None and passed != before:
            (improved if passed > before else regressed).append(
                {"ticker": ticker, "report_date": report_date, "before": before, "after": passed})

        summary.append({"ticker": ticker, "report_date": report_date,
                        "accession_number": accession_number, "passed": passed, "total": len(ALL_20)})
        print(f"  {ticker:<6} {report_date}  {passed:>2}/{len(ALL_20)}"
              + (f"   (was {before})" if before is not None and before != passed else ""))

    warehouse.close()
    production.close()

    print()
    print(f"company-years : {len(summary)}")
    print(f"coverage      : {total_pass}/{total_attempts} = "
          f"{100 * total_pass / total_attempts:.1f}%" if total_attempts else "n/a")
    print(f"improved      : {len(improved)} company-years")
    print(f"regressed     : {len(regressed)} company-years")

    payload = {
        "company_years": len(summary),
        "coverage_pass": total_pass, "coverage_total": total_attempts,
        "coverage_pct": round(100 * total_pass / total_attempts, 2) if total_attempts else None,
        "improved": improved, "regressed": regressed, "per_company_year": summary,
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
