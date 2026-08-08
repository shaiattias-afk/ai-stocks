"""
Annual XBRL Warehouse completion gate — Step 1/3: builds the
authoritative 50-filing expected universe (45 original target
company-years + 5 supplementary prior-fiscal-year filings, per
data/database/ai_stock_agent_annual_v1_manifest.json) and audits each
filing's exact state: locked-package presence, warehouse_runs status,
and fact/context/unit counts. Read-only — does not lock, warehouse, or
modify anything. Prints the audit table and a classification summary
that scripts/115 (the loader) and scripts/116 (final verification +
manifest) will consume.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
ANNUAL_V1_MANIFEST_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1_manifest.json"
LOCKED_FILINGS_DIR = DATA_DIR / "sec_filings_locked"

AUDIT_OUTPUT_PATH = DATA_DIR / "annual_warehouse_audit.json"


def build_expected_universe() -> list[dict]:
    """Derives the authoritative 50-filing list from sec_filings
    metadata + the Annual V1 manifest's supplementary-filings list —
    never assumed, never hardcoded beyond what these two authoritative
    sources already state."""

    with ANNUAL_V1_MANIFEST_PATH.open(encoding="utf-8") as handle:
        annual_v1_manifest = json.load(handle)

    supplementary_accessions = set(annual_v1_manifest["supplementary_prior_year_filings"])
    expected_target_count = annual_v1_manifest["target_company_years"]

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    # The Annual Warehouse universe is 10-K filings only (45 target
    # company-years + 5 supplementary prior-year filings, ALL form=10-K
    # per the Annual V1 manifest). sec_filings also now contains 9
    # form=10-Q rows from the separate MSFT/AMZN/ORCL quarterly proofs
    # (D-034-adjacent work) — those are a different universe entirely,
    # excluded here by form, not by assumption: every one of the 45+5
    # annual filings is a 10-K, and no 10-Q has ever been part of this
    # count in any prior milestone/report.
    all_filings = prod_connection.execute(
        "SELECT accession_number, ticker, form, report_date, filing_date FROM sec_filings "
        "WHERE form = '10-K' ORDER BY ticker, report_date"
    ).fetchall()
    prod_connection.close()

    universe: list[dict] = []
    target_count = 0
    supplementary_count = 0

    for accession_number, ticker, form, report_date, filing_date in all_filings:
        is_supplementary = accession_number in supplementary_accessions
        universe.append({
            "accession_number": accession_number,
            "ticker": ticker,
            "form": form,
            "report_date": str(report_date),
            "filing_date": str(filing_date) if filing_date else None,
            "role": "supplementary_prior_year" if is_supplementary else "target_company_year",
        })
        if is_supplementary:
            supplementary_count += 1
        else:
            target_count += 1

    if target_count != expected_target_count:
        raise RuntimeError(
            f"Expected {expected_target_count} target company-years per Annual V1 manifest, "
            f"but sec_filings (excluding the 5 supplementary accessions) has {target_count}. "
            "Refusing to proceed on an unverified count."
        )
    if supplementary_count != len(supplementary_accessions):
        raise RuntimeError(
            f"Expected {len(supplementary_accessions)} supplementary accessions, found "
            f"{supplementary_count} matching sec_filings rows. Refusing to proceed."
        )

    return universe


def check_locked_package(ticker: str, accession_number: str) -> dict:
    accession_compact = accession_number.replace("-", "")
    locked_dir = LOCKED_FILINGS_DIR / ticker.upper() / accession_compact
    manifest_path = locked_dir / "locked_filing_manifest.json"

    if not manifest_path.exists():
        return {"locked": False, "manifest": None, "primary_document_path": None}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    primary_document_path = Path(manifest["primary_document_path"])
    return {
        "locked": True,
        "manifest": manifest,
        "primary_document_exists": primary_document_path.exists(),
        "primary_document_path": str(primary_document_path),
    }


def check_warehouse_status(warehouse_connection: duckdb.DuckDBPyConnection, accession_number: str) -> dict:
    runs = warehouse_connection.execute(
        "SELECT status, script_name, fact_extraction_seconds, total_elapsed_seconds, row_counts_json, completed_at_utc "
        "FROM warehouse_runs WHERE accession_number = ? ORDER BY completed_at_utc",
        [accession_number],
    ).fetchall()

    fact_count = warehouse_connection.execute(
        "SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?", [accession_number]
    ).fetchone()[0]
    context_count = warehouse_connection.execute(
        "SELECT COUNT(*) FROM xbrl_contexts WHERE accession_number = ?", [accession_number]
    ).fetchone()[0]
    unit_count = warehouse_connection.execute(
        "SELECT COUNT(*) FROM xbrl_units WHERE accession_number = ?", [accession_number]
    ).fetchone()[0]

    return {
        "warehouse_run_count": len(runs),
        "runs": [{"status": r[0], "script_name": r[1], "completed_at_utc": r[5]} for r in runs],
        "fact_count": fact_count,
        "context_count": context_count,
        "unit_count": unit_count,
    }


def classify(locked_info: dict, warehouse_info: dict) -> str:
    has_pass_run = any(r["status"] == "PASS" for r in warehouse_info["runs"])
    counts_nonzero = (
        warehouse_info["fact_count"] > 0
        and warehouse_info["context_count"] > 0
        and warehouse_info["unit_count"] > 0
    )

    if has_pass_run and counts_nonzero:
        return "COMPLETE"
    if warehouse_info["warehouse_run_count"] > 0 and not (has_pass_run and counts_nonzero):
        return "WAREHOUSE_FAILED"
    if locked_info["locked"] and locked_info.get("primary_document_exists"):
        return "LOCKED_NOT_WAREHOUSED"
    if not locked_info["locked"]:
        return "NOT_LOCKED"
    return "METADATA_MISMATCH"


def main() -> None:
    print("=" * 100)
    print("ANNUAL XBRL WAREHOUSE AUDIT — STEP 1: BUILD AUTHORITATIVE EXPECTED UNIVERSE")
    print("=" * 100)

    universe = build_expected_universe()
    print(f"Expected filings (derived, not assumed): {len(universe)} "
          f"(target company-years + supplementary prior-year filings)")
    print()

    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    audit_records = []
    classification_counts: dict[str, int] = {}

    for entry in universe:
        locked_info = check_locked_package(entry["ticker"], entry["accession_number"])
        warehouse_info = check_warehouse_status(warehouse_connection, entry["accession_number"])
        status = classify(locked_info, warehouse_info)
        classification_counts[status] = classification_counts.get(status, 0) + 1

        record = {
            **entry,
            "locked": locked_info["locked"],
            "primary_document_path": locked_info.get("primary_document_path"),
            "warehouse_run_count": warehouse_info["warehouse_run_count"],
            "fact_count": warehouse_info["fact_count"],
            "context_count": warehouse_info["context_count"],
            "unit_count": warehouse_info["unit_count"],
            "warehouse_runs": warehouse_info["runs"],
            "classification": status,
        }
        audit_records.append(record)

        print(f"  [{status:22s}] {entry['ticker']:6s} {entry['form']:5s} {entry['report_date']} "
              f"acc={entry['accession_number']:22s} locked={locked_info['locked']!s:5s} "
              f"facts={warehouse_info['fact_count']:5d} ctx={warehouse_info['context_count']:5d} "
              f"units={warehouse_info['unit_count']:3d} ({entry['role']})")

    warehouse_connection.close()

    print()
    print("=" * 100)
    print("CLASSIFICATION SUMMARY")
    for status in ("COMPLETE", "LOCKED_NOT_WAREHOUSED", "NOT_LOCKED", "WAREHOUSE_FAILED", "METADATA_MISMATCH"):
        print(f"  {status:22s}: {classification_counts.get(status, 0)}")
    print(f"  TOTAL EXPECTED: {len(universe)}")
    print("=" * 100)

    missing = [r for r in audit_records if r["classification"] != "COMPLETE"]
    print(f"\nFilings NOT complete ({len(missing)}):")
    for r in missing:
        print(f"  {r['ticker']:6s} {r['report_date']} acc={r['accession_number']} -> {r['classification']}")

    with AUDIT_OUTPUT_PATH.open("w", encoding="utf-8") as handle:
        json.dump({
            "expected_count": len(universe),
            "classification_counts": classification_counts,
            "records": audit_records,
        }, handle, indent=2, ensure_ascii=False, default=str)
    print(f"\nFull audit written to {AUDIT_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
