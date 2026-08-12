"""Re-derives and loads all 20 annual metrics for exactly the 732
accessions scripts/188 originally loaded (engine_version =
'v2-pilot-warehouse-native (scripts/188)'), using the fixed engine from
D-051 through D-054 (docs/DECISIONS_LOG.md): D-P1 registrant narrowing,
the "comprehensive"-statement role-exclude fix, the current_debt
exception-isolation fix, and D-P2's capex component aggregate.

Appends a NEW engine version's rows through the write guard -- never
touches a single existing row. companies/sec_filings are untouched (all
732 accessions are already registered from scripts/188's original load).
The original frozen 45 company-years (engine_version NOT LIKE
'%scripts/188%') are never in scope here and are never re-derived or
re-inserted.

    --check-only   compute everything, write nothing, report coverage
    --execute      back up, append through the guard, re-verify read-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH, WAREHOUSE_DB_PATH
from stock_agent.extraction.core import (
    BUILT_IN_METRICS, TargetRowNotFound, identify_canonical_row,
    match_facts_from_warehouse, reconstruct_presentation_dataframe,
)
from stock_agent.metrics import production_lookup
from stock_agent.metrics.annual import SUCCESSFUL_STATUSES, compute_company_year
from stock_agent.policies.capex_components import resolve_capex_by_component_aggregate
from stock_agent.policies.prior_fiscal_year_lookup import combine_current_and_prior_invested_capital
from stock_agent.policies.roic_nopat import combine_average_invested_capital_and_nopat_into_roic
from stock_agent.policies.tax_normalization import compute_normalized_tax_nopat
from stock_agent.storage.write_guard import guarded_versioned_append

ENGINE_VERSION = "v3-vocabulary-cleanup (scripts/191, D-051-D-054)"
PRIOR_ENGINE_VERSION = "v2-pilot-warehouse-native (scripts/188)"
RESULT_PATH = DATA_DIR / "universe_metrics_v3_reload_result.json"
BACKUP_DIR = DATA_DIR / "database" / "backups"

POLICY_METRICS = ["current_debt", "long_term_debt", "total_debt", "cash_and_equivalents",
                  "short_term_investments", "stockholders_equity", "adjusted_net_debt",
                  "invested_capital"]
FLOW_METRICS = ["revenue", "operating_income", "net_income", "pretax_income",
                "income_tax_expense", "operating_cash_flow", "capex"]
DERIVED = ["free_cash_flow", "effective_tax_rate", "nopat",
           "average_invested_capital", "roic"]
ALL_20 = POLICY_METRICS + FLOW_METRICS + DERIVED

RESULT_COLUMNS = [
    "extraction_run_id", "metric_name", "is_primary_metric", "status", "value", "unit",
    "context_id", "period_start", "period_end", "source_concept", "label",
    "statement_role_definition", "selection_tier", "is_derived_metric", "formula",
    "validation_reason", "engine_version", "loaded_at", "is_active",
]
RUN_COLUMNS = ["extraction_run_id", "accession_number", "engine_version", "loaded_at"]


def sha256_of_file(path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def concept_period_type(connection, accession_number, concept_qname) -> str:
    row = connection.execute(
        "SELECT period_type FROM xbrl_concepts WHERE accession_number = ? AND qname = ?",
        [accession_number, concept_qname]).fetchone()
    return (row[0] if row and row[0] else "duration").strip().lower()


def resolve_flow(connection, production, ticker, accession_number, presentation, metric_name, report_date) -> dict:
    # Unlike scripts/190 (which recomputes the FULL universe, including
    # the truly frozen v14/v15 accessions, and so needs a per-ticker
    # passthrough gate), every accession THIS script ever processes is,
    # by construction, tagged PRIOR_ENGINE_VERSION (scripts/188) -- never
    # a frozen-original one. A frozen ticker's LATEST fiscal year can
    # still be a scripts/188 accession (its earlier years are frozen,
    # its newest one was not), so gating passthrough on ticker identity
    # alone was wrong: it silently passed through that accession's OWN
    # already-computed (and possibly still-buggy) value instead of
    # recomputing it with the fixed engine -- measured on META/MSFT/
    # NVDA/ORCL's 2025 fiscal year, where it silently kept a stale
    # REVIEW_REQUIRED. Always recompute here; nothing in this script's
    # scope is ever legitimately out of the fixed engine's reach.
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


def compute_all_20(connection, production, ticker, report_date, accession_number) -> dict[str, dict]:
    metrics: dict[str, dict] = {}

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

    # Mirrors metrics.annual.compute_full_company_year's own prior-fiscal-
    # year mechanism exactly, rather than tracking "prior year" through
    # this script's OWN processing order (which was wrong: it only knew
    # about a ticker's prior year if that prior year was ALSO in this
    # script's own limited target list -- for a frozen ticker's newest
    # fiscal year, whose earlier years are frozen and so never appear in
    # this script's own loop, that made a genuine, resolvable prior year
    # look like a missing one). Looks across ALL of this ticker's known
    # report dates in production (frozen or scripts/188, doesn't matter),
    # then ALWAYS recomputes that prior year's invested_capital fresh
    # from the warehouse (D-P3 option B) -- never reads a stored
    # average_invested_capital passthrough unless truly no prior year
    # exists anywhere in the dataset.
    company_years = production_lookup.all_company_years(production)
    all_dates = [rd for (tkr, rd, _acc) in company_years if tkr == ticker]
    prior_report_date = production_lookup.prior_report_date_for(ticker, report_date, all_dates)

    if prior_report_date is None:
        looked_up = production_lookup.latest_metric(production, ticker, report_date, "average_invested_capital")
        avg = (
            {"status": looked_up["status"], "value": looked_up["value"]}
            if looked_up is not None else {"status": "REVIEW_REQUIRED", "value": None}
        )
    else:
        prior_accession_row = production.execute(
            "SELECT accession_number FROM sec_filings WHERE ticker = ? AND report_date = ?",
            [ticker, prior_report_date],
        ).fetchone()
        if prior_accession_row is None:
            prior_ic_status, prior_ic_value = "REVIEW_REQUIRED", None
        else:
            try:
                prior_core = compute_company_year(connection, ticker, prior_report_date, prior_accession_row[0])
                prior_ic_status = prior_core["invested_capital"]["status"]
                prior_ic_value = prior_core["invested_capital"]["value"]
            except Exception as exc:  # noqa: BLE001
                # A second, not-yet-fixed uncaught-exception site (this
                # one inside resolve_long_term_debt's own internal
                # current-debt-ancestry check, debt_current_long_term.py
                # ~line 726) -- same shape as D-053's current_debt fix,
                # but a different call site, deliberately left for the
                # engine itself later (out of scope for this load).
                # Fails closed here rather than crashing the whole load.
                prior_ic_status, prior_ic_value = "REVIEW_REQUIRED", None

        ic = metrics.get("invested_capital", {})
        avg = combine_current_and_prior_invested_capital(
            ic.get("status") or "REVIEW_REQUIRED", ic.get("value"),
            prior_ic_status, prior_ic_value)
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
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-only", action="store_true")
    group.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    targets = production.execute(
        """
        SELECT DISTINCT sf.ticker, sf.report_date, sf.accession_number
        FROM sec_filings sf
        JOIN extraction_runs er ON er.accession_number = sf.accession_number
        WHERE er.engine_version = ?
        ORDER BY sf.ticker, sf.report_date
        """,
        [PRIOR_ENGINE_VERSION],
    ).fetchall()
    rows_before = production.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    runs_before = production.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0]
    frozen_rows_before = production.execute(
        "SELECT COUNT(*) FROM financial_metric_results fmr "
        "JOIN extraction_runs er ON er.extraction_run_id = fmr.extraction_run_id "
        "WHERE er.engine_version != ?", [PRIOR_ENGINE_VERSION]).fetchone()[0]

    print("=" * 92)
    print(f"accessions to re-derive (engine_version = {PRIOR_ENGINE_VERSION!r}) : {len(targets)}")
    print("=" * 92)

    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)

    loaded_at = datetime.now(timezone.utc)
    result_rows, run_rows, summary = [], [], []

    for ticker, report_date, accession_number in targets:
        report_date = str(report_date)
        metrics = compute_all_20(warehouse, production, ticker, report_date, accession_number)

        run_id = f"{accession_number}::{ENGINE_VERSION}"
        run_rows.append((run_id, accession_number, ENGINE_VERSION, loaded_at))
        for name in ALL_20:
            m = metrics.get(name, {"status": "REVIEW_REQUIRED", "value": None})
            result_rows.append((
                run_id, name, True, m.get("status"), m.get("value"), "iso4217:USD",
                None, None, report_date, m.get("source_concept"), m.get("label"),
                None, m.get("selection_tier"), bool(m.get("is_derived", False)),
                m.get("formula"), m.get("validation_reason"), ENGINE_VERSION, loaded_at, True,
            ))
        passed = sum(1 for n in ALL_20 if metrics.get(n, {}).get("status") in SUCCESSFUL_STATUSES)
        summary.append({"ticker": ticker, "report_date": report_date,
                        "accession_number": accession_number, "passed": passed, "total": len(ALL_20)})
        print(f"  {ticker:<6} {report_date}  {passed:>2}/{len(ALL_20)}")

    warehouse.close()

    total_pass = sum(s["passed"] for s in summary)
    total_attempts = sum(s["total"] for s in summary)
    print()
    print(f"company-years : {len(summary)}")
    print(f"result rows   : {len(result_rows)}")
    print(f"coverage      : {total_pass}/{total_attempts} = "
          f"{100 * total_pass / total_attempts:.1f}%" if total_attempts else "n/a")

    payload = {
        "mode": "check-only" if args.check_only else "execute",
        "company_years": len(summary), "result_rows": len(result_rows),
        "extraction_runs": len(run_rows),
        "coverage_pass": total_pass, "coverage_total": total_attempts,
        "rows_before": rows_before, "runs_before": runs_before,
        "engine_version": ENGINE_VERSION, "per_company_year": summary,
    }

    if args.check_only:
        payload["note"] = "nothing was written"
        RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        production.close()
        print(f"\nCHECK-ONLY: nothing written. {RESULT_PATH}")
        return

    production.close()

    if not result_rows:
        print("nothing to load.")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = loaded_at.strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"ai_stock_agent_pre_v3_reload_{stamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    if sha256_of_file(PRODUCTION_DB_PATH) != sha256_of_file(backup_path):
        raise SystemExit("backup checksum mismatch -- refusing to write")
    print(f"\nbackup verified: {backup_path.name}")

    runs_result = guarded_versioned_append(
        PRODUCTION_DB_PATH, "extraction_runs", RUN_COLUMNS, run_rows, len(run_rows))
    results_result = guarded_versioned_append(
        PRODUCTION_DB_PATH, "financial_metric_results", RESULT_COLUMNS,
        result_rows, len(result_rows))

    verify = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    rows_after = verify.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    runs_after = verify.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0]
    frozen_rows_after = verify.execute(
        "SELECT COUNT(*) FROM financial_metric_results fmr "
        "JOIN extraction_runs er ON er.extraction_run_id = fmr.extraction_run_id "
        "WHERE er.engine_version != ?", [PRIOR_ENGINE_VERSION]).fetchone()[0]
    # frozen_rows_after must equal frozen_rows_before + our own new rows
    # (every one of THIS load's rows carries ENGINE_VERSION, which is
    # != PRIOR_ENGINE_VERSION, so it counts as "frozen" by this filter
    # too) -- the real invariant is that no PRE-EXISTING non-scripts/188
    # row count changed, which the write guard's own checksum already
    # proves; this is a second, independent read-only cross-check.
    pre_existing_non_v2_intact = verify.execute(
        "SELECT COUNT(*) FROM financial_metric_results WHERE loaded_at < ? "
        "AND extraction_run_id NOT IN (SELECT extraction_run_id FROM extraction_runs WHERE engine_version = ?)",
        [loaded_at, PRIOR_ENGINE_VERSION]).fetchone()[0]
    verify.close()

    print(f"\nfinancial_metric_results : {rows_before} -> {rows_after} (+{rows_after - rows_before})")
    print(f"extraction_runs          : {runs_before} -> {runs_after} (+{runs_after - runs_before})")
    print(f"frozen (non-scripts/188) rows before reload: {frozen_rows_before}")
    print(f"pre-existing non-scripts/188 rows still present: {pre_existing_non_v2_intact}")

    ok = (rows_after - rows_before == len(result_rows)
          and runs_after - runs_before == len(run_rows)
          and pre_existing_non_v2_intact == frozen_rows_before)
    payload.update({"rows_after": rows_after, "runs_after": runs_after,
                    "frozen_rows_before": frozen_rows_before,
                    "pre_existing_non_v2_rows_intact": pre_existing_non_v2_intact == frozen_rows_before,
                    "backup_path": str(backup_path),
                    "guard_runs": runs_result, "guard_results": results_result,
                    "status": "PASS" if ok else "FAIL"})
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    print(f"written: {RESULT_PATH}")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
