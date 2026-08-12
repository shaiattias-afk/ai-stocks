"""Extends Scoring Model V1 (scripts/192) to the full survivorship-free
~150-company universe (D-051-D-056), computing the same 9 factors +
composite for every company-year NOT already loaded (the original 45
company-years, engine_version 'v1-scoring-model (scripts/192)', are
left untouched -- this only appends new rows).

Uses the exact same inputs_v1/composite_v1 modules, unchanged, now
computing revenue_growth/operating_margin directly from financial_
metric_results (2026-08-11 revision) instead of the frozen 9-ticker
Derived Metrics V1 table -- see src/stock_agent/scoring/inputs_v1.py's
own module docstring for the verification this was based on.

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

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.composite_v1 import compute_composite_scores_v1
from stock_agent.scoring.inputs_v1 import compute_scoring_inputs_v1
from stock_agent.storage.write_guard import guarded_versioned_append

ENGINE_VERSION = "v1-scoring-model-wide-universe (scripts/196)"
SCORING_VERSION = "ScoringModelV1"
RESULT_PATH = DATA_DIR / "scoring_model_v1_wide_universe_load_result.json"
BACKUP_DIR = DATA_DIR / "database" / "backups"

INPUTS_TABLE = "scoring_inputs_v1"
COMPOSITE_TABLE = "scoring_composite_v1"

INPUTS_COLUMNS = [
    "ticker", "report_date", "fiscal_year", "filing_date", "accession_number", "prior_report_date",
    "revenue_growth", "revenue_growth_status",
    "roic_level", "roic_level_status",
    "roic_trend", "roic_trend_status",
    "operating_margin", "operating_margin_status",
    "fcf_margin", "fcf_margin_status",
    "fcf_growth", "fcf_growth_status",
    "balance_sheet_strength_ratio", "balance_sheet_strength_ratio_status",
    "capex_discipline_deviation", "capex_discipline_deviation_status",
    "capex_discipline_trailing_years_used",
    "distance_from_high", "distance_from_high_status",
    "distance_from_high_price_date", "distance_from_high_trailing_high", "distance_from_high_price",
    "scoring_version", "created_at", "engine_version", "loaded_at", "is_active",
]

COMPOSITE_COLUMNS = [
    "ticker", "report_date", "fiscal_year", "composite_score", "weight_covered", "factors_used",
    "factor_scores_json", "scoring_version", "created_at", "engine_version", "loaded_at", "is_active",
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
    already_loaded = set(connection.execute(
        f"SELECT ticker, report_date FROM {COMPOSITE_TABLE}"
    ).fetchall())
    already_loaded = {(t, str(rd)) for t, rd in already_loaded}

    all_company_years = connection.execute(
        """
        SELECT DISTINCT sf.ticker, sf.report_date, sf.fiscal_year
        FROM sec_filings sf
        JOIN extraction_runs er ON er.accession_number = sf.accession_number
        JOIN financial_metric_results fmr ON fmr.extraction_run_id = er.extraction_run_id
        ORDER BY 1, 2
        """
    ).fetchall()
    targets = [(t, rd, fy) for t, rd, fy in all_company_years if (t, str(rd)) not in already_loaded]
    print(f"total company-years seen: {len(all_company_years)}")
    print(f"already loaded (scripts/192, frozen 45): {len(already_loaded)}")
    print(f"new targets (wide universe): {len(targets)}")

    loaded_at = datetime.now(timezone.utc)
    all_inputs = []
    for ticker, report_date, fiscal_year in targets:
        result = compute_scoring_inputs_v1(connection, ticker, str(report_date))
        result["fiscal_year"] = fiscal_year
        all_inputs.append(result)

    composites = compute_composite_scores_v1(all_inputs)
    composite_by_key = {(c["ticker"], c["report_date"]): c for c in composites}
    connection.close()

    inputs_rows = []
    for r in all_inputs:
        inputs_rows.append((
            r["ticker"], r["report_date"], r["fiscal_year"], r["filing_date"], r["accession_number"], r["prior_report_date"],
            r.get("revenue_growth"), r.get("revenue_growth_status"),
            r.get("roic_level"), r.get("roic_level_status"),
            r.get("roic_trend"), r.get("roic_trend_status"),
            r.get("operating_margin"), r.get("operating_margin_status"),
            r.get("fcf_margin"), r.get("fcf_margin_status"),
            r.get("fcf_growth"), r.get("fcf_growth_status"),
            r.get("balance_sheet_strength_ratio"), r.get("balance_sheet_strength_ratio_status"),
            r.get("capex_discipline_deviation"), r.get("capex_discipline_deviation_status"),
            r.get("capex_discipline_trailing_years_used"),
            r.get("distance_from_high"), r.get("distance_from_high_status"),
            r.get("distance_from_high_price_date"), r.get("distance_from_high_trailing_high"), r.get("distance_from_high_price"),
            SCORING_VERSION, loaded_at, ENGINE_VERSION, loaded_at, True,
        ))

    composite_rows = []
    for r in all_inputs:
        key = (r["ticker"], r["report_date"])
        c = composite_by_key[key]
        composite_rows.append((
            c["ticker"], c["report_date"], c["fiscal_year"], c["composite_score"], c["weight_covered"], c["factors_used"],
            json.dumps(c["factor_scores"]),
            SCORING_VERSION, loaded_at, ENGINE_VERSION, loaded_at, True,
        ))

    summary = {
        "company_years": len(all_inputs),
        "composites_with_full_coverage": sum(1 for c in composites if c["weight_covered"] == 1.0),
        "composites_unrankable": sum(1 for c in composites if c["composite_score"] is None),
        "mean_weight_covered": (sum(c["weight_covered"] for c in composites) / len(composites)) if composites else None,
    }
    print(json.dumps(summary, indent=2))

    payload = {"mode": "check-only" if args.check_only else "execute", **summary,
               "engine_version": ENGINE_VERSION, "scoring_version": SCORING_VERSION}

    if args.check_only:
        payload["note"] = "nothing was written"
        RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\nCHECK-ONLY: nothing written. {RESULT_PATH}")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = loaded_at.strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"ai_stock_agent_pre_scoring_v1_wide_{stamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    if sha256_of_file(PRODUCTION_DB_PATH) != sha256_of_file(backup_path):
        raise SystemExit("backup checksum mismatch -- refusing to write")
    print(f"\nbackup verified: {backup_path.name}")

    inputs_result = guarded_versioned_append(
        PRODUCTION_DB_PATH, INPUTS_TABLE, INPUTS_COLUMNS, inputs_rows, len(inputs_rows))
    composite_result = guarded_versioned_append(
        PRODUCTION_DB_PATH, COMPOSITE_TABLE, COMPOSITE_COLUMNS, composite_rows, len(composite_rows))

    verify = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    inputs_count = verify.execute(f"SELECT COUNT(*) FROM {INPUTS_TABLE}").fetchone()[0]
    composite_count = verify.execute(f"SELECT COUNT(*) FROM {COMPOSITE_TABLE}").fetchone()[0]
    distinct_tickers = verify.execute(f"SELECT COUNT(DISTINCT ticker) FROM {INPUTS_TABLE}").fetchone()[0]
    dup_keys = verify.execute(
        f"SELECT COUNT(*) FROM (SELECT ticker, report_date, COUNT(*) c FROM {INPUTS_TABLE} "
        f"GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    verify.close()

    ok = (inputs_count == len(already_loaded) + len(inputs_rows)
          and composite_count == len(already_loaded) + len(composite_rows)
          and dup_keys == 0)
    payload.update({
        "inputs_rows_written": len(inputs_rows), "composite_rows_written": len(composite_rows),
        "inputs_table_total": inputs_count, "composite_table_total": composite_count,
        "distinct_tickers": distinct_tickers, "duplicate_keys": dup_keys,
        "backup_path": str(backup_path),
        "guard_inputs": inputs_result, "guard_composite": composite_result,
        "status": "PASS" if ok else "FAIL",
    })
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    print(f"written: {RESULT_PATH}")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
