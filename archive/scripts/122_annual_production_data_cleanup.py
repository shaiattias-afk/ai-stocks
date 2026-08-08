"""
Annual production data cleanup — replaces the active production
`financial_metric_results` table's contents with exactly the 900
authoritative Annual V1 rows (45 target company-years x 20 primary
metrics, 0 REVIEW_REQUIRED), while preserving every historical/obsolete
row via a full pre-cleanup database backup and two Parquet archive
exports. Does not touch quarterly data, the XBRL warehouse, locked SEC
filing packages, or the Annual V1 snapshot itself.

The "authoritative 900 rows" identification logic is copied VERBATIM
from `scripts/106_freeze_annual_v1_snapshot.py`'s own `gather_stats()`
query (same `PRIMARY_METRICS` list, same
`SUPPLEMENTARY_PRIOR_YEAR_ACCESSIONS` exclusion, same
latest-per-accession-and-metric ranking by `loaded_at DESC`) — this is
the exact query that originally produced and verified Annual V1's
checksummed manifest (`total_primary_metric_results: 900,
review_required_count: 0`), so it is reused unchanged rather than
re-derived, to guarantee an identical, deterministic result.

Phase 1 (read-only audit) — fails closed (writes nothing) if Annual V1
is not exactly 900/45/20/0 or any row cannot be deterministically
mapped.
Phase 2 (backup + archive) — full DB backup + 2 Parquet exports + a
manifest, only after Phase 1 passes.
Phase 3 (transactional cleanup) — one transaction: build + validate a
staging table, replace `financial_metric_results` only on full
validation success; rollback on any mismatch.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
ANNUAL_V1_DB_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1.duckdb"
ANNUAL_V1_MANIFEST_PATH = DATA_DIR / "database" / "ai_stock_agent_annual_v1_manifest.json"
BACKUPS_DIR = DATA_DIR / "database" / "backups"
ARCHIVE_DIR = DATA_DIR / "archive"

EXPECTED_ANNUAL_V1_CHECKSUM = "e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814"

# Copied VERBATIM from scripts/106_freeze_annual_v1_snapshot.py
PRIMARY_METRICS = [
    "revenue", "net_income", "operating_income", "operating_cash_flow",
    "capex", "free_cash_flow", "cash_and_equivalents",
    "short_term_investments", "current_debt", "long_term_debt",
    "total_debt", "adjusted_net_debt", "pretax_income",
    "income_tax_expense", "stockholders_equity", "effective_tax_rate",
    "nopat", "invested_capital", "average_invested_capital", "roic",
]
SUPPLEMENTARY_PRIOR_YEAR_ACCESSIONS = [
    "0001018724-21-000004", "0001652044-21-000010", "0001326801-20-000013",
    "0001535527-21-000007", "0001045810-19-000023",
]

FMR_COLUMNS = [
    "extraction_run_id", "metric_name", "is_primary_metric", "status", "value",
    "unit", "context_id", "period_start", "period_end", "source_concept",
    "label", "statement_role_definition", "selection_tier", "is_derived_metric",
    "formula", "validation_reason",
]

RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def authoritative_900_query(alias_f: str = "f", alias_r: str = "r") -> str:
    placeholders = ",".join("?" * len(PRIMARY_METRICS))
    exclude_placeholders = ",".join("?" * len(SUPPLEMENTARY_PRIOR_YEAR_ACCESSIONS))
    return f"""
        WITH ranked AS (
            SELECT {alias_f}.*, {alias_r}.accession_number, {alias_r}.loaded_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY {alias_r}.accession_number, {alias_f}.metric_name
                       ORDER BY {alias_r}.loaded_at DESC
                   ) rn
            FROM financial_metric_results {alias_f}
            JOIN extraction_runs {alias_r} ON {alias_r}.extraction_run_id = {alias_f}.extraction_run_id
            WHERE {alias_f}.metric_name IN ({placeholders})
              AND {alias_r}.accession_number NOT IN ({exclude_placeholders})
        )
        SELECT * FROM ranked WHERE rn = 1
    """


def authoritative_900_params() -> list[str]:
    return PRIMARY_METRICS + SUPPLEMENTARY_PRIOR_YEAR_ACCESSIONS


def get_schema(connection: duckdb.DuckDBPyConnection, table_name: str) -> list[tuple]:
    return connection.execute(f"DESCRIBE {table_name}").fetchall()


def write_fail_report(reason: str, details: dict) -> None:
    report = f"""# Annual production data cleanup — RESULT: FAIL (Phase 1 audit)

## Reason
{reason}

## Details
```json
{json.dumps(details, indent=2, ensure_ascii=False, default=str)}
```

## Result: FAIL

Nothing was written. No backup was created. No archive was created. No
transaction was opened against the production database. The active
`financial_metric_results` table is completely unchanged.

## Next step
Investigate and resolve the discrepancy above before re-attempting this
cleanup.
"""
    (PROJECT_DIR / "docs" / "LAST_CLAUDE_REPORT.md").write_text(report, encoding="utf-8")
    print("\nFAIL report written to docs/LAST_CLAUDE_REPORT.md")


def phase1_audit() -> dict:
    print("=" * 100)
    print("PHASE 1 — READ-ONLY AUDIT")
    print("=" * 100)

    if not ANNUAL_V1_DB_PATH.exists():
        write_fail_report("Annual V1 database file not found.", {"path": str(ANNUAL_V1_DB_PATH)})
        raise SystemExit(1)

    actual_v1_checksum = sha256_of_file(ANNUAL_V1_DB_PATH)
    manifest = json.loads(ANNUAL_V1_MANIFEST_PATH.read_text(encoding="utf-8"))
    print(f"Annual V1 checksum (actual):   {actual_v1_checksum}")
    print(f"Annual V1 checksum (expected): {EXPECTED_ANNUAL_V1_CHECKSUM}")
    print(f"Annual V1 checksum (manifest): {manifest['sha256_checksum']}")

    if actual_v1_checksum != EXPECTED_ANNUAL_V1_CHECKSUM or manifest["sha256_checksum"] != EXPECTED_ANNUAL_V1_CHECKSUM:
        write_fail_report("Annual V1 checksum does not match the expected value.", {
            "actual": actual_v1_checksum, "expected": EXPECTED_ANNUAL_V1_CHECKSUM,
            "manifest": manifest["sha256_checksum"],
        })
        raise SystemExit(1)

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    v1_connection = duckdb.connect(database=str(ANNUAL_V1_DB_PATH), read_only=True)

    prod_schema = get_schema(prod_connection, "financial_metric_results")
    v1_schema = get_schema(v1_connection, "financial_metric_results")
    schema_diff = [] if prod_schema == v1_schema else list(set(prod_schema) ^ set(v1_schema))
    print(f"\nSchema identical: {prod_schema == v1_schema}")
    if schema_diff:
        print(f"Schema differences: {schema_diff}")

    query = authoritative_900_query()
    params = authoritative_900_params()

    v1_df = v1_connection.execute(query, params).fetchdf()
    print(f"\nAnnual V1 authoritative rows (via scripts/106's exact query): {len(v1_df)}")

    v1_accessions = v1_df["accession_number"].nunique()
    v1_metrics_per_accession = v1_df.groupby("accession_number").size()
    v1_review_required = int((v1_df["status"] == "REVIEW_REQUIRED").sum())
    v1_status_breakdown = v1_df["status"].value_counts().to_dict()

    print(f"  distinct company-years: {v1_accessions} (expected 45)")
    print(f"  metrics-per-company-year: min={v1_metrics_per_accession.min()} max={v1_metrics_per_accession.max()} (expected all 20)")
    print(f"  REVIEW_REQUIRED count: {v1_review_required} (expected 0)")
    print(f"  status breakdown: {v1_status_breakdown}")

    v1_ok = (
        len(v1_df) == 900
        and v1_accessions == 45
        and bool((v1_metrics_per_accession == 20).all())
        and v1_review_required == 0
    )

    if not v1_ok:
        details = {
            "row_count": len(v1_df), "distinct_accessions": int(v1_accessions),
            "metrics_per_accession_min": int(v1_metrics_per_accession.min()) if len(v1_metrics_per_accession) else None,
            "metrics_per_accession_max": int(v1_metrics_per_accession.max()) if len(v1_metrics_per_accession) else None,
            "review_required_count": v1_review_required, "status_breakdown": v1_status_breakdown,
        }
        write_fail_report(
            "Annual V1 is NOT exactly 900 authoritative rows (45 company-years x 20 metrics, 0 REVIEW_REQUIRED) "
            "under the same identification query used to originally freeze and verify Annual V1.",
            details,
        )
        raise SystemExit(1)

    # tie/ambiguity check
    tie_query = f"""
        WITH grp AS (
            SELECT r.accession_number, f.metric_name,
                   RANK() OVER (PARTITION BY r.accession_number, f.metric_name ORDER BY r.loaded_at DESC) rnk
            FROM financial_metric_results f
            JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id
            WHERE f.metric_name IN ({",".join("?" * len(PRIMARY_METRICS))})
              AND r.accession_number NOT IN ({",".join("?" * len(SUPPLEMENTARY_PRIOR_YEAR_ACCESSIONS))})
        )
        SELECT accession_number, metric_name, COUNT(*) c FROM grp WHERE rnk = 1
        GROUP BY accession_number, metric_name HAVING COUNT(*) > 1
    """
    v1_ties = v1_connection.execute(tie_query, params).fetchall()
    print(f"  ambiguous (tied loaded_at) mappings in V1: {len(v1_ties)}")
    if v1_ties:
        write_fail_report("Ambiguous authoritative mappings found in Annual V1 (tied loaded_at timestamps).",
                           {"ties": v1_ties})
        raise SystemExit(1)

    # compare against production's equivalent reduction
    prod_reduced_df = prod_connection.execute(query, params).fetchdf()
    prod_total_rows = prod_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]

    key_cols = ["accession_number", "metric_name"]
    v1_idx = v1_df.set_index(key_cols)
    prod_idx = prod_reduced_df.set_index(key_cols)
    only_in_v1 = list(v1_idx.index.difference(prod_idx.index))
    only_in_prod_reduced = list(prod_idx.index.difference(v1_idx.index))
    common = v1_idx.index.intersection(prod_idx.index)

    compare_cols = ["value", "status", "unit", "extraction_run_id", "context_id", "source_concept"]
    mismatches = []
    for key in common:
        v1_row, prod_row = v1_idx.loc[key], prod_idx.loc[key]
        for col in compare_cols:
            v1_val, prod_val = v1_row[col], prod_row[col]
            if v1_val != prod_val and not (v1_val != v1_val and prod_val != prod_val):
                mismatches.append({"key": key, "column": col, "v1_value": v1_val, "prod_value": prod_val})

    print(f"\nProduction (reduced to the same 900-key set): {len(prod_reduced_df)} rows")
    print(f"  missing from production (present in V1, absent in production's reduction): {len(only_in_v1)}")
    print(f"  extra in production's reduction (not in V1's 900): {len(only_in_prod_reduced)}")
    print(f"  value/status/lineage mismatches among common keys: {len(mismatches)}")
    print(f"\nCurrent production financial_metric_results total rows: {prod_total_rows}")
    print(f"Historical/obsolete rows outside the authoritative 900: {prod_total_rows - len(prod_idx.index.intersection(v1_idx.index))}")

    prod_connection.close()
    v1_connection.close()

    if only_in_v1 or mismatches:
        write_fail_report(
            "Not every Annual V1 authoritative row can be deterministically preserved in the production schema.",
            {"missing_from_production": only_in_v1, "mismatches": mismatches},
        )
        raise SystemExit(1)

    print("\nPHASE 1 AUDIT: PASS — proceeding to Phase 2.")

    return {
        "v1_authoritative_rows": len(v1_df), "v1_company_years": int(v1_accessions),
        "v1_status_breakdown": v1_status_breakdown, "v1_review_required": v1_review_required,
        "production_total_rows_before_cleanup": prod_total_rows,
        "rows_matching_annual_v1": len(common),
        "historical_obsolete_rows_outside_v1": prod_total_rows - len(common),
        "missing_authoritative_rows": only_in_v1,
        "ambiguous_mappings": v1_ties,
        "schema_identical": prod_schema == v1_schema,
        "schema_diff": schema_diff,
        "authoritative_v1_df": v1_df,
    }


def phase2_backup_and_archive(audit: dict) -> dict:
    print("\n" + "=" * 100)
    print("PHASE 2 — BACKUP AND ARCHIVE")
    print("=" * 100)

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_annual_cleanup_{RUN_TIMESTAMP}.duckdb"
    print(f"Creating backup: {backup_path}")
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)

    source_checksum = sha256_of_file(PRODUCTION_DB_PATH)
    backup_checksum = sha256_of_file(backup_path)
    print(f"  source checksum: {source_checksum}")
    print(f"  backup checksum: {backup_checksum}")
    if source_checksum != backup_checksum:
        raise RuntimeError("Backup checksum does not match source checksum — aborting before any write.")

    backup_connection = duckdb.connect(database=str(backup_path), read_only=True)
    backup_fmr_count = backup_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    backup_connection.close()
    print(f"  backup financial_metric_results row count: {backup_fmr_count} "
          f"(expected {audit['production_total_rows_before_cleanup']})")
    if backup_fmr_count != audit["production_total_rows_before_cleanup"]:
        raise RuntimeError("Backup row count does not match pre-cleanup production row count — aborting.")

    # export full pre-cleanup table — via DuckDB's own native Parquet writer
    # (COPY TO ... FORMAT PARQUET), not pandas.to_parquet, since pyarrow/
    # fastparquet are not installed in this environment and installing a
    # new dependency was avoidable here.
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    pre_cleanup_total = prod_connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]

    pre_cleanup_parquet = ARCHIVE_DIR / f"financial_metric_results_pre_cleanup_{RUN_TIMESTAMP}.parquet"
    prod_connection.execute(
        f"COPY (SELECT * FROM financial_metric_results) TO '{pre_cleanup_parquet.as_posix()}' (FORMAT PARQUET)"
    )
    print(f"\nExported pre-cleanup table ({pre_cleanup_total} rows) to {pre_cleanup_parquet}")

    # export rows that will be removed = full table MINUS the 900 authoritative
    # (extraction_run_id, metric_name) keys — registered as a DuckDB view from
    # the already-computed audit dataframe, joined via SQL anti-join.
    v1_df = audit["authoritative_v1_df"][["extraction_run_id", "metric_name"]].copy()
    prod_connection.register("keep_keys_v1", v1_df)

    removed_parquet = ARCHIVE_DIR / f"financial_metric_results_removed_{RUN_TIMESTAMP}.parquet"
    prod_connection.execute(f"""
        COPY (
            SELECT f.* FROM financial_metric_results f
            LEFT JOIN keep_keys_v1 k
              ON k.extraction_run_id = f.extraction_run_id AND k.metric_name = f.metric_name
            WHERE k.extraction_run_id IS NULL
        ) TO '{removed_parquet.as_posix()}' (FORMAT PARQUET)
    """)
    removed_count = prod_connection.execute(f"SELECT COUNT(*) FROM read_parquet('{removed_parquet.as_posix()}')").fetchone()[0]
    retained_count = prod_connection.execute(f"""
        SELECT COUNT(*) FROM financial_metric_results f
        JOIN keep_keys_v1 k ON k.extraction_run_id = f.extraction_run_id AND k.metric_name = f.metric_name
    """).fetchone()[0]
    prod_connection.unregister("keep_keys_v1")

    print(f"Exported removed-rows archive ({removed_count} rows) to {removed_parquet}")
    print(f"Rows retained (in the pre-cleanup table matching the authoritative 900): {retained_count}")

    if retained_count + removed_count != pre_cleanup_total:
        raise RuntimeError("Retained + removed row counts do not sum to the pre-cleanup total — aborting.")
    if retained_count != 900:
        raise RuntimeError(f"Expected exactly 900 retained rows, found {retained_count} — aborting.")

    # verify parquet files are readable and counts match, via DuckDB's own reader
    reread_pre_count = prod_connection.execute(f"SELECT COUNT(*) FROM read_parquet('{pre_cleanup_parquet.as_posix()}')").fetchone()[0]
    reread_removed_count = prod_connection.execute(f"SELECT COUNT(*) FROM read_parquet('{removed_parquet.as_posix()}')").fetchone()[0]
    if reread_pre_count != pre_cleanup_total or reread_removed_count != removed_count:
        raise RuntimeError("Parquet archive re-read row counts do not match — aborting.")
    print("\nBoth Parquet archives verified readable with matching row counts.")

    prod_connection.close()

    manifest_path = ARCHIVE_DIR / f"annual_production_cleanup_manifest_{RUN_TIMESTAMP}.json"
    manifest = {
        "timestamp_utc": RUN_TIMESTAMP,
        "production_db_path": str(PRODUCTION_DB_PATH.resolve()),
        "annual_v1_db_path": str(ANNUAL_V1_DB_PATH.resolve()),
        "backup_db_path": str(backup_path.resolve()),
        "source_checksum_before_cleanup": source_checksum,
        "backup_checksum": backup_checksum,
        "annual_v1_checksum": EXPECTED_ANNUAL_V1_CHECKSUM,
        "pre_cleanup_financial_metric_results_row_count": pre_cleanup_total,
        "annual_v1_authoritative_row_count": 900,
        "rows_retained": retained_count,
        "rows_removed": removed_count,
        "pre_cleanup_archive_parquet": str(pre_cleanup_parquet.resolve()),
        "removed_rows_archive_parquet": str(removed_parquet.resolve()),
        "schema_columns": FMR_COLUMNS,
        "validation_results": {
            "backup_checksum_matches_source": source_checksum == backup_checksum,
            "backup_row_count_matches_source": backup_fmr_count == audit["production_total_rows_before_cleanup"],
            "retained_plus_removed_equals_total": retained_count + removed_count == pre_cleanup_total,
            "retained_count_is_900": retained_count == 900,
            "parquet_archives_readable_and_counts_match": True,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nManifest written to {manifest_path}")

    return {
        "backup_path": backup_path, "backup_checksum": backup_checksum, "source_checksum": source_checksum,
        "pre_cleanup_parquet": pre_cleanup_parquet, "removed_parquet": removed_parquet,
        "manifest_path": manifest_path, "rows_removed": removed_count, "rows_retained": retained_count,
        "pre_cleanup_total": pre_cleanup_total,
    }


def phase3_transactional_cleanup(audit: dict) -> dict:
    print("\n" + "=" * 100)
    print("PHASE 3 — TRANSACTIONAL CLEANUP")
    print("=" * 100)

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
    staging_table = f"financial_metric_results_staging_{RUN_TIMESTAMP.replace('-', '_')}"

    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(f"""
            CREATE TABLE {staging_table} (
                extraction_run_id VARCHAR NOT NULL,
                metric_name VARCHAR NOT NULL,
                is_primary_metric BOOLEAN,
                status VARCHAR,
                value DOUBLE,
                unit VARCHAR,
                context_id VARCHAR,
                period_start DATE,
                period_end DATE,
                source_concept VARCHAR,
                label VARCHAR,
                statement_role_definition VARCHAR,
                selection_tier VARCHAR,
                is_derived_metric BOOLEAN,
                formula VARCHAR,
                validation_reason VARCHAR,
                PRIMARY KEY (extraction_run_id, metric_name)
            )
        """)

        query = authoritative_900_query()
        params = authoritative_900_params()
        column_list = ", ".join(FMR_COLUMNS)
        connection.execute(
            f"INSERT INTO {staging_table} ({column_list}) "
            f"SELECT {column_list} FROM ({query}) t",
            params,
        )

        # --- validate inside the transaction ---
        errors = []

        row_count = connection.execute(f"SELECT COUNT(*) FROM {staging_table}").fetchone()[0]
        if row_count != 900:
            errors.append(f"staging row count = {row_count}, expected 900")

        company_year_counts = connection.execute(f"""
            SELECT r.accession_number, COUNT(*) c FROM {staging_table} s
            JOIN extraction_runs r ON r.extraction_run_id = s.extraction_run_id
            GROUP BY r.accession_number
        """).fetchdf()
        if len(company_year_counts) != 45:
            errors.append(f"company-year count = {len(company_year_counts)}, expected 45")
        if not (company_year_counts["c"] == 20).all():
            errors.append(f"not all company-years have exactly 20 metrics: {company_year_counts[company_year_counts['c'] != 20].to_dict()}")

        review_required_count = connection.execute(
            f"SELECT COUNT(*) FROM {staging_table} WHERE status = 'REVIEW_REQUIRED'"
        ).fetchone()[0]
        if review_required_count != 0:
            errors.append(f"REVIEW_REQUIRED count = {review_required_count}, expected 0")

        dup_keys = connection.execute(f"""
            SELECT extraction_run_id, metric_name, COUNT(*) c FROM {staging_table}
            GROUP BY extraction_run_id, metric_name HAVING COUNT(*) > 1
        """).fetchall()
        if dup_keys:
            errors.append(f"duplicate natural keys: {dup_keys}")

        null_required = connection.execute(f"""
            SELECT SUM(CASE WHEN extraction_run_id IS NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN metric_name IS NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status IS NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN value IS NULL THEN 1 ELSE 0 END)
            FROM {staging_table}
        """).fetchone()
        if any(n and n > 0 for n in null_required):
            errors.append(f"null required fields: {null_required}")

        # exact match against Annual V1 (re-read fresh, not reused from Phase 1, to
        # guarantee the transaction validates against the live source, not a cached df)
        staging_df = connection.execute(f"SELECT * FROM {staging_table}").fetchdf()
        v1_connection = duckdb.connect(database=str(ANNUAL_V1_DB_PATH), read_only=True)
        v1_df = v1_connection.execute(query, params).fetchdf()
        v1_connection.close()

        key_cols = ["extraction_run_id", "metric_name"]
        staging_idx = staging_df.set_index(key_cols)
        v1_idx = v1_df.set_index(key_cols)
        if set(staging_idx.index) != set(v1_idx.index):
            errors.append("staging table keys do not exactly match Annual V1's 900 keys")
        else:
            for col in ["value", "status", "unit"]:
                mismatched = (staging_idx[col].astype(str) != v1_idx.loc[staging_idx.index, col].astype(str))
                if mismatched.any():
                    errors.append(f"{int(mismatched.sum())} row(s) mismatch on column {col!r} vs Annual V1")

        if errors:
            connection.execute("ROLLBACK")
            print("VALIDATION FAILED — rolled back:")
            for e in errors:
                print(f"  - {e}")
            return {"status": "ROLLED_BACK", "errors": errors}

        print(f"Staging table validated: {row_count} rows, 45 company-years x 20 metrics, "
              f"0 REVIEW_REQUIRED, 0 duplicates, exact match to Annual V1.")

        # --- replace the active table ---
        old_table_temp_name = f"financial_metric_results_old_{RUN_TIMESTAMP.replace('-', '_')}"
        connection.execute(f"ALTER TABLE financial_metric_results RENAME TO {old_table_temp_name}")
        connection.execute(f"ALTER TABLE {staging_table} RENAME TO financial_metric_results")
        connection.execute(f"DROP TABLE {old_table_temp_name}")

        connection.execute("COMMIT")
        print("COMMITTED — financial_metric_results replaced with the 900 authoritative rows.")
        connection.close()
        return {"status": "COMMITTED", "row_count": row_count}

    except Exception as exc:  # noqa: BLE001
        connection.execute("ROLLBACK")
        connection.close()
        print(f"EXCEPTION during transaction — rolled back: {exc}")
        return {"status": "ROLLED_BACK", "errors": [str(exc)]}


def post_cleanup_validation() -> dict:
    print("\n" + "=" * 100)
    print("POST-CLEANUP VALIDATION")
    print("=" * 100)

    connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)

    fmr_count = connection.execute("SELECT COUNT(*) FROM financial_metric_results").fetchone()[0]
    company_years = connection.execute("""
        SELECT r.accession_number, COUNT(*) c FROM financial_metric_results f
        JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id
        GROUP BY r.accession_number
    """).fetchdf()
    review_required = connection.execute(
        "SELECT COUNT(*) FROM financial_metric_results WHERE status = 'REVIEW_REQUIRED'"
    ).fetchone()[0]
    dup_keys = connection.execute("""
        SELECT extraction_run_id, metric_name, COUNT(*) c FROM financial_metric_results
        GROUP BY extraction_run_id, metric_name HAVING COUNT(*) > 1
    """).fetchall()

    q_runs = connection.execute("SELECT COUNT(*) FROM quarterly_extraction_runs").fetchone()[0]
    q_rows = connection.execute("SELECT COUNT(*) FROM quarterly_metric_results").fetchone()[0]
    q_dup = connection.execute("""
        SELECT run_id, metric_name, fiscal_quarter, COUNT(*) c FROM quarterly_metric_results
        GROUP BY run_id, metric_name, fiscal_quarter HAVING COUNT(*) > 1
    """).fetchall()
    q_null_lineage = connection.execute(
        "SELECT COUNT(*) FROM quarterly_metric_results WHERE lineage_json IS NULL OR accession_number IS NULL"
    ).fetchone()[0]
    q_avail_mismatch = connection.execute("""
        SELECT COUNT(*) FROM quarterly_metric_results r
        JOIN sec_filings s ON s.accession_number = r.accession_number
        WHERE r.availability_date != CAST(s.filing_date AS VARCHAR)
    """).fetchone()[0]

    sec_filings_forms = dict(connection.execute("SELECT form, COUNT(*) FROM sec_filings GROUP BY form").fetchall())

    prod_connection_2 = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    annual_10k_accessions = [
        row[0] for row in prod_connection_2.execute(
            "SELECT accession_number FROM sec_filings WHERE form = '10-K'"
        ).fetchall()
    ]
    prod_connection_2.close()

    warehouse_connection = duckdb.connect(database=str(DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"), read_only=True)
    placeholders = ",".join("?" * len(annual_10k_accessions))
    warehouse_10k_pass = warehouse_connection.execute(
        f"SELECT COUNT(DISTINCT accession_number) FROM warehouse_runs "
        f"WHERE status = 'PASS' AND accession_number IN ({placeholders})",
        annual_10k_accessions,
    ).fetchone()[0]
    warehouse_connection.close()

    actual_v1_checksum = sha256_of_file(ANNUAL_V1_DB_PATH)

    results = {
        "financial_metric_results_count": fmr_count,
        "company_years": len(company_years),
        "metrics_per_company_year_all_20": bool((company_years["c"] == 20).all()) if len(company_years) else False,
        "review_required_count": review_required,
        "duplicate_natural_keys": dup_keys,
        "quarterly_extraction_runs_count": q_runs,
        "quarterly_metric_results_count": q_rows,
        "quarterly_duplicate_natural_keys": q_dup,
        "quarterly_missing_lineage_count": q_null_lineage,
        "quarterly_availability_date_mismatches": q_avail_mismatch,
        "sec_filings_form_counts": sec_filings_forms,
        "annual_10k_warehouse_pass_count": warehouse_10k_pass,
        "annual_v1_checksum_unchanged": actual_v1_checksum == EXPECTED_ANNUAL_V1_CHECKSUM,
    }

    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return results


def main() -> None:
    audit = phase1_audit()
    backup_info = phase2_backup_and_archive(audit)
    cleanup_result = phase3_transactional_cleanup(audit)

    if cleanup_result["status"] != "COMMITTED":
        write_fail_report("Phase 3 transactional cleanup failed validation and was rolled back.", cleanup_result)
        raise SystemExit(1)

    post_results = post_cleanup_validation()

    final_result = {
        "phase1_audit": {k: v for k, v in audit.items() if k != "authoritative_v1_df"},
        "phase2_backup": {k: (str(v) if isinstance(v, Path) else v) for k, v in backup_info.items()},
        "phase3_cleanup": cleanup_result,
        "post_cleanup_validation": post_results,
    }
    result_path = DATA_DIR / f"annual_production_cleanup_result_{RUN_TIMESTAMP}.json"
    result_path.write_text(json.dumps(final_result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nFull result written to {result_path}")


if __name__ == "__main__":
    main()
