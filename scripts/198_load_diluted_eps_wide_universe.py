"""Extends D-046's diluted EPS resolution (originally 9 tickers) to the
full ~150-company universe, via stock_agent.scoring.valuation_wide_v1
(same resolution rule as D-046, verified exact match on all 45 original
rows before this was trusted -- see that module's own docstring).

Appends to the existing `valuation_v1_per_share_inputs` table (a new
engine version, D-046's original 45 rows untouched -- same versioned-
append pattern D-057/D-061 already used for the scoring tables).

    --check-only   compute everything, write nothing, report a summary
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
from stock_agent.scoring.valuation_wide_v1 import resolve_diluted_eps
from stock_agent.storage.write_guard import guarded_versioned_append

ENGINE_VERSION = "v1-diluted-eps-wide-universe (scripts/198)"
VALUATION_VERSION = "VALUATION_V1"
RESULT_PATH = DATA_DIR / "diluted_eps_wide_universe_load_result.json"
BACKUP_DIR = DATA_DIR / "database" / "backups"

TABLE = "valuation_v1_per_share_inputs"
COLUMNS = [
    "ticker", "fiscal_year", "fiscal_year_end", "diluted_eps", "resolution_method",
    "eps_source_concept", "accession_number", "filing_date", "availability_date",
    "cross_check_calculated_eps", "cross_check_diff", "valuation_version",
    "created_at", "engine_version", "loaded_at", "is_active",
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

    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    already_loaded = {
        (t, fy) for t, fy in production.execute(f"SELECT ticker, fiscal_year FROM {TABLE}").fetchall()
    }
    targets = production.execute(
        """
        SELECT DISTINCT sf.ticker, sf.report_date, sf.fiscal_year, sf.accession_number, sf.filing_date
        FROM sec_filings sf
        JOIN extraction_runs er ON er.accession_number = sf.accession_number
        WHERE er.engine_version LIKE '%scripts/188%'
        ORDER BY 1, 2
        """
    ).fetchall()
    targets = [t for t in targets if (t[0], t[2]) not in already_loaded]

    # valuation_v1_per_share_inputs's primary key is (ticker, fiscal_year)
    # -- D-046's own frozen design, not something to change here. Two
    # tickers (CDNS, ILMN) changed their fiscal year-end convention
    # mid-stream, so sec_filings.fiscal_year genuinely collides across
    # two distinct report_dates for one fiscal_year label (measured:
    # CDNS 2022 has both 2022-01-01 and 2022-12-31; ILMN 2023 similarly).
    # Keep the later report_date -- the completed, non-transition fiscal
    # year -- and drop the earlier one rather than let a PRIMARY KEY
    # collision abort the whole load.
    best_by_key: dict[tuple[str, int], tuple] = {}
    dropped_as_duplicate = []
    for t in targets:
        ticker, report_date, fiscal_year = t[0], t[1], t[2]
        key = (ticker, fiscal_year)
        existing = best_by_key.get(key)
        if existing is None or report_date > existing[1]:
            if existing is not None:
                dropped_as_duplicate.append(existing)
            best_by_key[key] = t
        else:
            dropped_as_duplicate.append(t)
    targets = sorted(best_by_key.values(), key=lambda t: (t[0], t[1]))
    if dropped_as_duplicate:
        print(f"dropped as duplicate (ticker, fiscal_year) -- fiscal-year-end change: {dropped_as_duplicate}")

    print(f"already loaded (D-046, frozen 45): {len(already_loaded)}")
    print(f"new targets (wide universe): {len(targets)}")

    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    warehouse.execute("SET enable_progress_bar=false")

    loaded_at = datetime.now(timezone.utc)
    rows = []
    status_counts = {"PASS": 0, "REVIEW_REQUIRED": 0, "UNAVAILABLE": 0}
    for ticker, report_date, fiscal_year, accession_number, filing_date in targets:
        result = resolve_diluted_eps(warehouse, accession_number, str(report_date))
        status_counts[result["status"]] += 1
        if result["status"] != "PASS":
            continue
        rows.append((
            ticker, fiscal_year, report_date, result["value"], "REPORTED_DILUTED_EPS",
            "us-gaap:EarningsPerShareDiluted", accession_number, filing_date, filing_date,
            None, None, VALUATION_VERSION, loaded_at, ENGINE_VERSION, loaded_at, True,
        ))
    warehouse.close()
    production.close()

    print(json.dumps(status_counts, indent=2))
    print(f"rows to append: {len(rows)}")

    payload = {"mode": "check-only" if args.check_only else "execute", **status_counts,
               "rows_to_append": len(rows), "engine_version": ENGINE_VERSION}

    if args.check_only:
        payload["note"] = "nothing was written"
        RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\nCHECK-ONLY: nothing written. {RESULT_PATH}")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = loaded_at.strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"ai_stock_agent_pre_diluted_eps_wide_{stamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    if sha256_of_file(PRODUCTION_DB_PATH) != sha256_of_file(backup_path):
        raise SystemExit("backup checksum mismatch -- refusing to write")
    print(f"\nbackup verified: {backup_path.name}")

    result = guarded_versioned_append(PRODUCTION_DB_PATH, TABLE, COLUMNS, rows, len(rows))

    verify = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    row_count = verify.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    dup_keys = verify.execute(
        f"SELECT COUNT(*) FROM (SELECT ticker, fiscal_year, COUNT(*) c FROM {TABLE} GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    verify.close()

    ok = row_count == len(already_loaded) + len(rows) and dup_keys == 0
    payload.update({"rows_written": len(rows), "table_total": row_count, "duplicate_keys": dup_keys,
                    "backup_path": str(backup_path), "guard_result": result,
                    "status": "PASS" if ok else "FAIL"})
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    print(f"written: {RESULT_PATH}")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
