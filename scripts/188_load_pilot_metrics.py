"""Computes all 20 annual metrics for the pilot's NEW companies and loads
them into production through the append-only write guard.

Why a new computation path: metrics.annual.compute_full_company_year
reads 8 of its 20 metrics straight out of production ("passthrough"),
which works only for companies already loaded. A company being added for
the first time has nothing to read, so every metric here is computed from
the warehouse.

The existing 9 companies are NOT touched. Their 900 rows are frozen
production data; this only appends company-years that are absent.

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
from stock_agent.filings import archive as filings_archive
from stock_agent.metrics.annual import SUCCESSFUL_STATUSES, compute_company_year
from stock_agent.policies.prior_fiscal_year_lookup import combine_current_and_prior_invested_capital
from stock_agent.policies.roic_nopat import combine_average_invested_capital_and_nopat_into_roic
from stock_agent.policies.tax_normalization import compute_normalized_tax_nopat
from stock_agent.storage.write_guard import guarded_versioned_append

ENGINE_VERSION = "v2-pilot-warehouse-native (scripts/188)"
RESULT_PATH = DATA_DIR / "pilot_metrics_load_result.json"
BACKUP_DIR = DATA_DIR / "database" / "backups"

POLICY_METRICS = ["current_debt", "long_term_debt", "total_debt", "cash_and_equivalents",
                  "short_term_investments", "stockholders_equity", "adjusted_net_debt",
                  "invested_capital"]
FLOW_METRICS = ["revenue", "operating_income", "net_income", "pretax_income",
                "income_tax_expense", "operating_cash_flow", "capex"]
DERIVED = ["free_cash_flow", "effective_tax_rate", "nopat",
           "average_invested_capital", "roic"]
ALL_20 = POLICY_METRICS + FLOW_METRICS + DERIVED

COMPANY_COLUMNS = ["ticker", "company_name", "cik"]
FILING_COLUMNS = ["accession_number", "ticker", "form", "report_date", "filing_date",
                  "fiscal_year", "prior_report_date", "source_document"]
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


def resolve_flow(connection, accession_number, presentation, metric_name, report_date) -> dict:
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


def compute_all_20(connection, ticker, report_date, accession_number, prior_ic) -> dict[str, dict]:
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
            metrics[name] = resolve_flow(connection, accession_number, presentation, name, report_date)
    except Exception as exc:  # noqa: BLE001
        for name in FLOW_METRICS:
            metrics.setdefault(name, {"status": "REVIEW_REQUIRED", "value": None,
                                      "validation_reason": f"{type(exc).__name__}: {exc}"[:400]})

    def ok(name: str) -> bool:
        return metrics.get(name, {}).get("status") in SUCCESSFUL_STATUSES

    # free cash flow
    metrics["free_cash_flow"] = (
        {"status": "PASS", "value": metrics["operating_cash_flow"]["value"] - metrics["capex"]["value"],
         "formula": "operating_cash_flow - capex", "is_derived": True}
        if ok("operating_cash_flow") and ok("capex")
        else {"status": "REVIEW_REQUIRED", "value": None, "is_derived": True,
              "validation_reason": "component unresolved"})

    # effective tax rate + NOPAT (D-015 rules 3-4; D-027 Policy D fallback)
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
            # NOTE the key is "nopat", not "value" -- reading the wrong key
            # here silently produced a PASS status carrying None.
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

    # average invested capital + ROIC
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
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-only", action="store_true")
    group.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    existing_accessions = {r[0] for r in production.execute(
        "SELECT DISTINCT accession_number FROM extraction_runs").fetchall()}
    existing_filings = {r[0] for r in production.execute(
        "SELECT accession_number FROM sec_filings").fetchall()}
    existing_tickers = {r[0] for r in production.execute(
        "SELECT ticker FROM companies").fetchall()}
    rows_before = production.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    runs_before = production.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0]
    production.close()

    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    warehouse.execute(f"ATTACH '{filings_archive.ARCHIVE_DB_PATH}' AS arch (READ_ONLY)")
    annual = warehouse.execute("""
        SELECT DISTINCT w.ticker, w.report_date, w.accession_number
        FROM warehouse_runs w JOIN arch.filing_archive_manifest m USING (accession_number)
        WHERE w.ticker IS NOT NULL AND m.form = '10-K'
        ORDER BY w.ticker, w.report_date
    """).fetchall()

    todo = [(t, d, a) for t, d, a in annual if a not in existing_accessions]
    print("=" * 92)
    print(f"annual filings in warehouse : {len(annual)}")
    print(f"already in production       : {len(annual) - len(todo)}")
    print(f"to compute and load         : {len(todo)}  "
          f"({len({t for t, _, _ in todo})} companies)")
    print("=" * 92)

    # financial_metric_results -> extraction_runs -> sec_filings -> companies.
    # The foreign keys mean the parents must be appended first; a new
    # company-year needs its filing registered, and a brand-new company
    # needs its companies row.
    archive_connection = duckdb.connect(str(filings_archive.ARCHIVE_DB_PATH), read_only=True)
    manifest = {
        row[0]: row for row in archive_connection.execute(
            "SELECT accession_number, ticker, cik, company_name, form, report_date, "
            "filing_date, primary_document FROM filing_archive_manifest").fetchall()
    }
    archive_connection.close()

    loaded_at = datetime.now(timezone.utc)
    result_rows, run_rows, summary = [], [], []
    company_rows: list[tuple] = []
    filing_rows: list[tuple] = []
    seen_tickers: set[str] = set()
    ic_by_ticker: dict[str, dict[str, dict]] = {}

    for ticker, report_date, accession_number in todo:
        prior_year = str(int(report_date[:4]) - 1)
        prior_ic = next((v for k, v in ic_by_ticker.get(ticker, {}).items()
                         if k.startswith(prior_year)), None)
        metrics = compute_all_20(warehouse, ticker, report_date, accession_number, prior_ic)
        ic_by_ticker.setdefault(ticker, {})[report_date] = metrics.get("invested_capital", {})

        entry = manifest.get(accession_number)
        if entry is None:
            raise SystemExit(f"{accession_number} is in the warehouse but not the archive manifest")
        _, m_ticker, m_cik, m_company, m_form, m_report, m_filing, m_primary = entry

        if m_ticker not in existing_tickers and m_ticker not in seen_tickers:
            company_rows.append((m_ticker, m_company, int(m_cik) if m_cik is not None else None))
            seen_tickers.add(m_ticker)

        if accession_number not in existing_filings:
            prior_report = next(
                (d for d in sorted(ic_by_ticker.get(ticker, {}), reverse=True) if d < report_date),
                None)
            filing_rows.append((accession_number, m_ticker, m_form, m_report, m_filing,
                                int(str(m_report)[:4]), prior_report, m_primary))

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
        print(f"\nCHECK-ONLY: nothing written. {RESULT_PATH}")
        return

    if not result_rows:
        print("nothing to load.")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = loaded_at.strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"ai_stock_agent_pre_pilot_metrics_{stamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    if sha256_of_file(PRODUCTION_DB_PATH) != sha256_of_file(backup_path):
        raise SystemExit("backup checksum mismatch -- refusing to write")
    print(f"\nbackup verified: {backup_path.name}")

    # parents first, so no foreign key is ever dangling mid-load
    if company_rows:
        guarded_versioned_append(PRODUCTION_DB_PATH, "companies", COMPANY_COLUMNS,
                                 company_rows, len(company_rows))
        print(f"companies appended       : {len(company_rows)}")
    if filing_rows:
        guarded_versioned_append(PRODUCTION_DB_PATH, "sec_filings", FILING_COLUMNS,
                                 filing_rows, len(filing_rows))
        print(f"sec_filings appended     : {len(filing_rows)}")

    runs_result = guarded_versioned_append(
        PRODUCTION_DB_PATH, "extraction_runs", RUN_COLUMNS, run_rows, len(run_rows))
    results_result = guarded_versioned_append(
        PRODUCTION_DB_PATH, "financial_metric_results", RESULT_COLUMNS,
        result_rows, len(result_rows))

    verify = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    rows_after = verify.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    runs_after = verify.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0]
    # Count pre-existing rows by LOAD TIMESTAMP, not engine_version.
    # engine_version is constant across every run of this script, so a
    # second run counts the FIRST run's rows as its own and reports a
    # false failure: after the pilot's 1,660 rows, this read 2,560 - 1,660
    # = 900 and called a perfectly good load FAIL. loaded_at distinguishes
    # the runs; the write guard is what actually proves the pre-existing
    # rows are unchanged, by checksumming them before and after.
    frozen_intact = verify.execute(
        "SELECT COUNT(*) FROM financial_metric_results WHERE loaded_at < ?",
        [loaded_at]).fetchone()[0]
    tickers_now = verify.execute("""
        SELECT COUNT(DISTINCT m.accession_number) FROM financial_metric_results r
        JOIN extraction_runs m USING (extraction_run_id)""").fetchone()[0]
    verify.close()

    print(f"\nfinancial_metric_results : {rows_before} -> {rows_after} (+{rows_after - rows_before})")
    print(f"extraction_runs          : {runs_before} -> {runs_after} (+{runs_after - runs_before})")
    print(f"pre-existing rows intact : {frozen_intact} (expected {rows_before})")
    print(f"accessions with results  : {tickers_now}")

    ok = (rows_after - rows_before == len(result_rows)
          and runs_after - runs_before == len(run_rows)
          and frozen_intact == rows_before)
    payload.update({"rows_after": rows_after, "runs_after": runs_after,
                    "pre_existing_intact": frozen_intact == rows_before,
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
