"""
TASK_141 — read-only global warehouse-ingestion integrity audit.

Audits every 10-K and 10-Q accession registered in `sec_filings` (185
total: 135 10-Q + 50 10-K) against its locked filing package and the
XBRL warehouse, classifying each into exactly one anomaly category using
the same read-only, no-Arelle entry-point-detection logic proven in
`scripts/139_corrected_warehouse_loader_entry_point_detection.py`
(imported, not modified, not re-implemented) plus new per-accession
warehouse-state cross-checks this script adds.

Both databases are opened `read_only=True`. No filing is downloaded, no
Arelle process is run, no warehouse loader is created or modified, no
accession is re-warehoused, and no row in either production database is
written. Writes only two new output files:
data/warehouse_global_integrity_audit.json and .csv.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
LOCKED_FILINGS_DIR = DATA_DIR / "sec_filings_locked"

JSON_OUTPUT_PATH = DATA_DIR / "warehouse_global_integrity_audit.json"
CSV_OUTPUT_PATH = DATA_DIR / "warehouse_global_integrity_audit.csv"

WAREHOUSE_TABLES = [
    "xbrl_facts", "xbrl_contexts", "xbrl_units", "xbrl_concepts", "xbrl_labels",
    "xbrl_presentation_relationships", "xbrl_calculation_relationships",
    "xbrl_definition_relationships", "xbrl_roles",
]

# reuse (not modify, not copy) scripts/139's read-only, no-Arelle entry-point detection
_spec139 = importlib.util.spec_from_file_location("s139", PROJECT_DIR / "scripts" / "139_corrected_warehouse_loader_entry_point_detection.py")
s139 = importlib.util.module_from_spec(_spec139)
sys.modules["s139"] = s139
_spec139.loader.exec_module(s139)


def _file_head(path: Path, n_bytes: int) -> str:
    with path.open("rb") as handle:
        return handle.read(n_bytes).decode("utf-8", errors="ignore")


def count_traditional_instance_candidates(locked_dir: Path) -> list[str]:
    """Same scan as s139.detect_entry_point's second branch, exposed here
    standalone so this audit can distinguish 'zero candidates' from
    'multiple candidates' as two distinct anomaly categories (s139 itself
    only needs to know 'resolved or not')."""
    candidates = []
    for p in sorted(locked_dir.glob("*.xml")):
        name_lower = p.name.lower()
        if any(name_lower.endswith(suf) for suf in ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml")):
            continue
        if name_lower == "filingsummary.xml":
            continue
        head = _file_head(p, 2000)
        if "<xbrli:xbrl" in head or "<xbrl " in head.lower() or head.lstrip().lower().startswith("<xbrl"):
            candidates.append(p.name)
    return candidates


def locate_locked_package(ticker: str, accession_number: str) -> Path:
    return LOCKED_FILINGS_DIR / ticker.upper() / accession_number.replace("-", "")


def audit_one_accession(ticker, form, report_date, filing_date, accession_number, source_document,
                          physical_counts_by_table, runs_by_accession) -> dict:
    locked_dir = locate_locked_package(ticker, accession_number)
    locked_package_exists = locked_dir.is_dir()

    record = {
        "ticker": ticker, "form": form, "report_date": str(report_date), "filing_date": str(filing_date),
        "accession_number": accession_number, "source_document_sec_filings": source_document,
        "locked_package_path": str(locked_dir), "locked_package_exists": locked_package_exists,
        "manifest_exists": False, "manifest_readable": False, "manifest_error": None,
        "primary_document": None, "primary_document_exists": None,
        "detected_format": None, "selected_entry_point": None,
        "standalone_instance_candidate_count": None, "standalone_instance_candidates": None,
        "entry_point_evidence": None,
    }

    physical_counts = {t: physical_counts_by_table[t].get(accession_number, 0) for t in WAREHOUSE_TABLES}
    record["physical_row_counts"] = physical_counts

    runs = runs_by_accession.get(accession_number, [])
    record["warehouse_runs_count"] = len(runs)
    record["all_warehouse_runs"] = runs
    latest_run = max(runs, key=lambda r: (r["completed_at_utc"] or r["started_at_utc"] or "")) if runs else None
    record["latest_run_status"] = latest_run["status"] if latest_run else None
    record["latest_row_counts_json"] = latest_run["row_counts_json"] if latest_run else None

    recorded_counts = None
    if latest_run and latest_run["row_counts_json"]:
        try:
            recorded_counts = json.loads(latest_run["row_counts_json"])
        except (TypeError, ValueError):
            recorded_counts = None
    record["recorded_counts_match_physical"] = (
        recorded_counts is not None and all(recorded_counts.get(t) == physical_counts[t] for t in WAREHOUSE_TABLES)
    ) if recorded_counts is not None else None

    valid_nonzero_load = physical_counts["xbrl_facts"] > 0 and physical_counts["xbrl_contexts"] > 0 and physical_counts["xbrl_concepts"] > 0
    record["valid_nonzero_warehouse_load"] = valid_nonzero_load

    # --- package / manifest / entry-point checks ---
    if not locked_package_exists:
        record["category"] = "LOCKED_PACKAGE_MISSING"
        record["evidence"] = f"No directory found at {locked_dir}"
        return record

    manifest_path = locked_dir / "locked_filing_manifest.json"
    record["manifest_exists"] = manifest_path.exists()
    if not record["manifest_exists"]:
        record["category"] = "MANIFEST_MISSING_OR_INVALID"
        record["evidence"] = f"locked_filing_manifest.json not found in {locked_dir}"
        return record

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record["manifest_readable"] = True
    except Exception as exc:  # noqa: BLE001
        record["manifest_readable"] = False
        record["manifest_error"] = str(exc)
        record["category"] = "MANIFEST_MISSING_OR_INVALID"
        record["evidence"] = f"manifest exists but is not readable/valid JSON: {exc}"
        return record

    record["primary_document"] = manifest.get("primary_document")
    primary_document_path = Path(manifest.get("primary_document_path", ""))
    record["primary_document_exists"] = primary_document_path.exists()
    if not record["primary_document_exists"]:
        record["category"] = "LOCKED_PACKAGE_MISSING"
        record["evidence"] = f"manifest's primary_document_path does not exist on disk: {primary_document_path}"
        return record

    detection = s139.detect_entry_point(locked_dir, primary_document_path)
    record["entry_point_evidence"] = detection

    if not detection["resolved"]:
        candidates = count_traditional_instance_candidates(locked_dir)
        record["standalone_instance_candidate_count"] = len(candidates)
        record["standalone_instance_candidates"] = candidates
        if len(candidates) > 1:
            record["category"] = "MULTIPLE_INSTANCE_CANDIDATES"
            record["evidence"] = f"{len(candidates)} ambiguous standalone XBRL instance candidates: {candidates}"
        else:
            record["category"] = "ENTRY_POINT_NOT_RESOLVED"
            record["evidence"] = detection["reason"]
        return record

    record["detected_format"] = detection["detected_format"]
    record["selected_entry_point"] = detection["selected_entry_point"]
    if detection["detected_format"] == "TRADITIONAL_XBRL_SEPARATE_INSTANCE":
        record["standalone_instance_candidate_count"] = 1
        record["standalone_instance_candidates"] = [Path(detection["selected_entry_point"]).name]

    # --- warehouse-state checks (entry point IS resolved) ---
    if latest_run is None:
        if any(physical_counts[t] > 0 for t in WAREHOUSE_TABLES):
            record["category"] = "PHYSICAL_CONTENT_WITHOUT_VALID_RUN"
            record["evidence"] = f"physical warehouse rows exist ({physical_counts}) but no warehouse_runs row was ever recorded"
        else:
            record["category"] = "NO_VALID_WAREHOUSE_RUN"
            record["evidence"] = "locked and entry-point-resolved, but no warehouse_runs row exists and no physical rows exist — never warehoused"
        return record

    if latest_run["status"] == "PASS":
        if physical_counts["xbrl_facts"] == 0 and physical_counts["xbrl_contexts"] == 0 and physical_counts["xbrl_concepts"] == 0:
            record["category"] = "FALSE_PASS_ZERO_CONTENT"
            record["evidence"] = f"latest warehouse_runs row says status=PASS but physical xbrl_facts/contexts/concepts are all 0 (row_counts_json={record['latest_row_counts_json']})"
        elif record["recorded_counts_match_physical"] is False:
            record["category"] = "RUN_COUNT_DATABASE_MISMATCH"
            record["evidence"] = f"recorded row_counts_json ({recorded_counts}) does not match physical counts ({physical_counts})"
        elif not valid_nonzero_load:
            record["category"] = "PARTIAL_CONTENT_INCONSISTENCY"
            record["evidence"] = f"status=PASS with some non-zero tables but facts/contexts/concepts not all non-zero: {physical_counts}"
        else:
            record["category"] = "VALID_TRADITIONAL_XBRL" if detection["detected_format"] == "TRADITIONAL_XBRL_SEPARATE_INSTANCE" else "VALID_INLINE_XBRL"
            record["evidence"] = f"status=PASS, physical counts non-zero and internally consistent, recorded counts match physical: {physical_counts}"
        return record

    # latest run recorded but not PASS
    if any(physical_counts[t] > 0 for t in WAREHOUSE_TABLES):
        record["category"] = "PHYSICAL_CONTENT_WITHOUT_VALID_RUN"
        record["evidence"] = f"latest run status={latest_run['status']!r} (not PASS) but physical rows exist: {physical_counts}"
    else:
        record["category"] = "NO_VALID_WAREHOUSE_RUN"
        record["evidence"] = f"latest run status={latest_run['status']!r} (not PASS), zero physical rows"
    return record


def main() -> dict:
    start_time = time.perf_counter()
    print("=" * 100)
    print("TASK_141 — GLOBAL WAREHOUSE-INGESTION INTEGRITY AUDIT (read-only)")
    print("=" * 100)

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    pre_counts = {
        "quarterly_extraction_runs": prod_connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
        "quarterly_metric_results": prod_connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
        "financial_metric_results": prod_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
    }
    total_warehouse_facts_before = warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]

    filings = prod_connection.execute(
        "SELECT ticker, form, report_date, filing_date, accession_number, source_document "
        "FROM sec_filings WHERE form IN ('10-K', '10-Q') ORDER BY ticker, report_date"
    ).fetchall()
    print(f"Registered 10-K/10-Q accessions in sec_filings: {len(filings)}")

    # bulk per-table physical counts (one GROUP BY query per table, not per accession)
    physical_counts_by_table: dict[str, dict[str, int]] = {}
    for table in WAREHOUSE_TABLES:
        rows = warehouse_connection.execute(f"SELECT accession_number, COUNT(*) FROM {table} GROUP BY accession_number").fetchall()
        physical_counts_by_table[table] = {acc: cnt for acc, cnt in rows}

    # all warehouse_runs rows, grouped by accession
    runs_rows = warehouse_connection.execute(
        "SELECT accession_number, warehouse_run_id, status, started_at_utc, completed_at_utc, row_counts_json, script_name "
        "FROM warehouse_runs ORDER BY accession_number, started_at_utc"
    ).fetchall()
    runs_by_accession: dict[str, list[dict]] = defaultdict(list)
    for acc, run_id, status, started, completed, row_counts_json, script_name in runs_rows:
        runs_by_accession[acc].append({
            "warehouse_run_id": run_id, "status": status, "started_at_utc": started,
            "completed_at_utc": completed, "row_counts_json": row_counts_json, "script_name": script_name,
        })

    prod_connection.close()
    warehouse_connection.close()

    records = []
    seen_accessions = set()
    unresolved = []
    for ticker, form, report_date, filing_date, accession_number, source_document in filings:
        if accession_number in seen_accessions:
            unresolved.append({"accession_number": accession_number, "reason": "duplicate accession_number in sec_filings"})
            continue
        seen_accessions.add(accession_number)
        try:
            record = audit_one_accession(ticker, form, report_date, filing_date, accession_number, source_document,
                                          physical_counts_by_table, runs_by_accession)
        except Exception as exc:  # noqa: BLE001
            unresolved.append({"accession_number": accession_number, "reason": f"exception during audit: {exc}"})
            continue
        records.append(record)
        print(f"  {ticker} {form} {report_date} {accession_number}: {record['category']}")

    # --- fail-closed checks ---
    dup_check = Counter(f[4] for f in filings)
    duplicates = [acc for acc, cnt in dup_check.items() if cnt > 1]

    fail_reasons = []
    if len(records) != len(filings) - len(unresolved):
        fail_reasons.append(f"record count {len(records)} does not match expected {len(filings) - len(unresolved)}")
    if unresolved:
        fail_reasons.append(f"{len(unresolved)} accession(s) could not be classified: {unresolved}")
    if duplicates:
        fail_reasons.append(f"duplicate accession_number records found in sec_filings itself: {duplicates}")

    # re-verify production DB counts unchanged (this script never wrote to it)
    prod_check = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    post_counts = {
        "quarterly_extraction_runs": prod_check.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0],
        "quarterly_metric_results": prod_check.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0],
        "financial_metric_results": prod_check.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0],
    }
    prod_check.close()
    warehouse_check = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    total_warehouse_facts_after = warehouse_check.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
    warehouse_check.close()

    databases_unchanged = (pre_counts == post_counts) and (total_warehouse_facts_before == total_warehouse_facts_after)
    if not databases_unchanged:
        fail_reasons.append("production database or warehouse row counts changed during the audit — this must never happen for a read-only script")

    task_status = "FAIL" if fail_reasons else "PASS"

    # --- summaries ---
    by_form = Counter(r["form"] for r in records)
    by_ticker = Counter(r["ticker"] for r in records)
    by_format = Counter(r["detected_format"] or "UNRESOLVED" for r in records)
    by_category = Counter(r["category"] for r in records)

    false_pass_records = [r for r in records if r["category"] == "FALSE_PASS_ZERO_CONTENT"]
    false_pass_tickers = sorted({r["ticker"] for r in false_pass_records})
    nvda_only_false_pass = false_pass_tickers == ["NVDA"]

    count_mismatch_records = [r for r in records if r["category"] == "RUN_COUNT_DATABASE_MISMATCH"]
    traditional_xbrl_records = [r for r in records if r["detected_format"] == "TRADITIONAL_XBRL_SEPARATE_INSTANCE"]
    needs_rewarehouse_categories = {"FALSE_PASS_ZERO_CONTENT", "RUN_COUNT_DATABASE_MISMATCH",
                                     "PARTIAL_CONTENT_INCONSISTENCY", "PHYSICAL_CONTENT_WITHOUT_VALID_RUN",
                                     "NO_VALID_WAREHOUSE_RUN"}
    needs_rewarehouse_records = [r for r in records if r["category"] in needs_rewarehouse_categories]
    unproven_categories = {"ENTRY_POINT_NOT_RESOLVED", "MULTIPLE_INSTANCE_CANDIDATES",
                            "LOCKED_PACKAGE_MISSING", "MANIFEST_MISSING_OR_INVALID"}
    unproven_records = [r for r in records if r["category"] in unproven_categories]

    summary = {
        "total_accessions_audited": len(records),
        "counts_by_form": dict(by_form), "counts_by_ticker": dict(by_ticker),
        "counts_by_detected_format": dict(by_format), "counts_by_anomaly_category": dict(by_category),
        "false_pass_accessions": [f"{r['ticker']} {r['accession_number']}" for r in false_pass_records],
        "nvda_is_only_false_pass": nvda_only_false_pass,
        "count_mismatch_accessions": [f"{r['ticker']} {r['accession_number']}" for r in count_mismatch_records],
        "traditional_xbrl_accessions": [f"{r['ticker']} {r['accession_number']}" for r in traditional_xbrl_records],
        "accessions_requiring_rewarehouse": [f"{r['ticker']} {r['accession_number']} ({r['category']})" for r in needs_rewarehouse_records],
        "accessions_with_unproven_state": [f"{r['ticker']} {r['accession_number']} ({r['category']})" for r in unproven_records],
        "unresolved_accessions": unresolved,
        "databases_remained_read_only_and_unchanged": databases_unchanged,
        "pre_counts": pre_counts, "post_counts": post_counts,
        "total_warehouse_facts_before": total_warehouse_facts_before, "total_warehouse_facts_after": total_warehouse_facts_after,
    }

    print("\n" + "=" * 100)
    print("SUMMARY")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("=" * 100)

    output = {
        "task_id": "TASK_141_GLOBAL_WAREHOUSE_INTEGRITY_AUDIT", "status": task_status,
        "fail_reasons": fail_reasons, "summary": summary, "records": records,
        "runtime_seconds": round(time.perf_counter() - start_time, 2),
    }
    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")

    csv_columns = ["ticker", "form", "report_date", "filing_date", "accession_number", "locked_package_exists",
                   "manifest_exists", "primary_document", "detected_format", "selected_entry_point",
                   "standalone_instance_candidate_count", "warehouse_runs_count", "latest_run_status",
                   "recorded_counts_match_physical", "valid_nonzero_warehouse_load", "category", "evidence"]
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns)
        writer.writeheader()
        for r in records:
            writer.writerow({k: r.get(k) for k in csv_columns})
    print(f"CSV written to {CSV_OUTPUT_PATH} ({len(records)} rows)")

    print(f"\nTASK STATUS: {task_status}")
    if fail_reasons:
        for reason in fail_reasons:
            print(f"  FAIL REASON: {reason}")

    return output


if __name__ == "__main__":
    main()
