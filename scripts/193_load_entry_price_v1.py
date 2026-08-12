"""Computes and loads Entry Price Method 1
(docs/SCORING_MODEL_V1_BLUEPRINT.md Stage 4) for the 45 frozen
company-years: each fiscal year's own P/E (own filing_date price /
own as-reported diluted EPS), plus its percentile position within
that SAME ticker's own trailing (up to 5-year) P/E history. Reads only
already-frozen data (Valuation V1, Historical Prices V1) -- no new
extraction.

    --check-only   compute everything, write nothing, report a summary
    --execute      back up, create the table if needed, append through
                   the guard, re-verify read-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.entry_price_v1 import compute_entry_price_inputs_v1
from stock_agent.storage.write_guard import guarded_versioned_append

ENGINE_VERSION = "v1-entry-price-method1 (scripts/193)"
RESULT_PATH = DATA_DIR / "entry_price_v1_load_result.json"
BACKUP_DIR = DATA_DIR / "database" / "backups"

TABLE = "entry_price_v1"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    ticker VARCHAR,
    fiscal_year INTEGER,
    fiscal_year_end DATE,
    filing_date DATE,
    diluted_eps DOUBLE,
    price DOUBLE,
    price_date DATE,
    pe DOUBLE,
    pe_status VARCHAR,
    trailing_pe_values_json JSON,
    trailing_years_used_json JSON,
    trailing_pe_min DOUBLE,
    trailing_pe_max DOUBLE,
    trailing_pe_median DOUBLE,
    pe_percentile_within_own_history DOUBLE,
    engine_version VARCHAR,
    loaded_at TIMESTAMP,
    is_active BOOLEAN,
    PRIMARY KEY (ticker, fiscal_year)
)
"""

COLUMNS = [
    "ticker", "fiscal_year", "fiscal_year_end", "filing_date", "diluted_eps",
    "price", "price_date", "pe", "pe_status",
    "trailing_pe_values_json", "trailing_years_used_json",
    "trailing_pe_min", "trailing_pe_max", "trailing_pe_median",
    "pe_percentile_within_own_history",
    "engine_version", "loaded_at", "is_active",
]


def sha256_of_file(path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-only", action="store_true")
    group.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    targets = connection.execute(
        "SELECT ticker, fiscal_year FROM valuation_v1_per_share_inputs ORDER BY ticker, fiscal_year"
    ).fetchall()
    print(f"target company-years (Valuation V1): {len(targets)}")

    loaded_at = datetime.now(timezone.utc)
    results = []
    for ticker, fiscal_year in targets:
        results.append(compute_entry_price_inputs_v1(connection, ticker, fiscal_year))
    connection.close()

    rows = []
    for r in results:
        rows.append((
            r["ticker"], r["fiscal_year"], r["fiscal_year_end"], r["filing_date"], r["diluted_eps"],
            r["price"], r["price_date"], r["pe"], r["pe_status"],
            json.dumps(r["trailing_pe_values"]), json.dumps(r["trailing_years_used"]),
            r["trailing_pe_min"], r["trailing_pe_max"], r["trailing_pe_median"],
            r["pe_percentile_within_own_history"],
            ENGINE_VERSION, loaded_at, True,
        ))

    summary = {
        "company_years": len(results),
        "pe_resolved": sum(1 for r in results if r["pe"] is not None),
        "pe_undefined_nonpositive_eps": sum(1 for r in results if r["pe_status"] == "UNDEFINED_NONPOSITIVE_EPS"),
        "with_percentile_position": sum(1 for r in results if r["pe_percentile_within_own_history"] is not None),
    }
    print(json.dumps(summary, indent=2))

    payload = {"mode": "check-only" if args.check_only else "execute", **summary, "engine_version": ENGINE_VERSION}

    if args.check_only:
        payload["note"] = "nothing was written"
        RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\nCHECK-ONLY: nothing written. {RESULT_PATH}")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = loaded_at.strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"ai_stock_agent_pre_entry_price_v1_{stamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    if sha256_of_file(PRODUCTION_DB_PATH) != sha256_of_file(backup_path):
        raise SystemExit("backup checksum mismatch -- refusing to write")
    print(f"\nbackup verified: {backup_path.name}")

    ddl_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=False)
    ddl_connection.execute(DDL)
    ddl_connection.close()
    print(f"table ensured: {TABLE}")

    result = guarded_versioned_append(PRODUCTION_DB_PATH, TABLE, COLUMNS, rows, len(rows))

    verify = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    row_count = verify.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    dup_keys = verify.execute(
        f"SELECT COUNT(*) FROM (SELECT ticker, fiscal_year, COUNT(*) c FROM {TABLE} GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    verify.close()

    ok = row_count == len(rows) and dup_keys == 0
    payload.update({
        "rows_written": row_count, "duplicate_keys": dup_keys,
        "backup_path": str(backup_path), "guard_result": result,
        "status": "PASS" if ok else "FAIL",
    })
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    print(f"written: {RESULT_PATH}")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
