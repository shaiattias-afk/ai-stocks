"""
Archive-sourced XBRL warehouse loader: pulls one filing's needed files
out of the compressed DuckDB archive (scripts/161), extracts them to a
fresh temp directory, hands that directory to Arelle exactly the way
scripts/144's disk-based loader does, writes the parsed XBRL layer into
a warehouse DuckDB, and always cleans up the temp directory afterward
(via scripts/161.extracted_filing()'s context manager, even on error).

Reuses, unmodified, via importlib (same pattern already used throughout
this project, e.g. scripts/144 importing scripts/121):
  - scripts/144_warehouse_loader_v2_production.py's detect_entry_point()
    -- general Inline-XBRL-vs-standalone-instance detection, works on
    any directory, not just data/sec_filings_locked/.
  - scripts/121_quarterly_batch_runner.py's pure extraction functions
    (extract_facts/contexts/units/concepts/labels/roles/presentation/
    calculation/definition relationships), create_warehouse_schema(),
    write_table().

Run standalone for a single bounded proof (one accession) before the
full 185-accession rebuild in scripts/166:

    .venv\\Scripts\\python.exe scripts\\165_filings_archive_arelle_loader.py --accession-number 0000950170-24-087843
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from importlib import util as _importlib_util
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "arelle_cache"
DEFAULT_PROOF_WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_from_archive_proof.duckdb"

USER_AGENT = "Shai Attias shaiattias@gmail.com"
INTERNET_TIMEOUT_SECONDS = 20

_spec161 = _importlib_util.spec_from_file_location("s161", PROJECT_DIR / "scripts" / "161_filings_archive_core.py")
s161 = _importlib_util.module_from_spec(_spec161)
sys.modules["s161"] = s161
_spec161.loader.exec_module(s161)

_spec144 = _importlib_util.spec_from_file_location("s144", PROJECT_DIR / "scripts" / "144_warehouse_loader_v2_production.py")
s144 = _importlib_util.module_from_spec(_spec144)
sys.modules["s144"] = s144
_spec144.loader.exec_module(s144)

s121 = s144.s121  # 144 already imported 121 unmodified; reuse the same loaded module, don't re-exec it

WAREHOUSE_TABLES = s144.WAREHOUSE_TABLES


class ArchiveWarehouseLoadError(Exception):
    pass


def run_archive_warehouse_load(
    archive_connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    warehouse_db_path: Path,
    internet_connectivity: str = "online",
) -> dict:
    """Loads exactly one archived filing into `warehouse_db_path`,
    sourcing bytes ONLY from the compressed archive (never from
    data/sec_filings_locked/). PASS requires the exact same checks as
    scripts/144: entry point resolved unambiguously, Arelle DTS load
    succeeds, xbrl_facts/xbrl_contexts/xbrl_concepts all > 0, xbrl_units
    > 0 whenever a monetary fact exists, physically-inserted counts
    (re-queried inside the same transaction) exactly match computed
    counts."""
    from arelle.RuntimeOptions import RuntimeOptions
    from arelle.api.Session import Session
    from arelle import Version as ArelleVersion

    total_start = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc).isoformat()

    with s161.extracted_filing(archive_connection, accession_number) as (temp_dir, manifest_row):
        primary_document_path = temp_dir / manifest_row["primary_document"]
        ticker = manifest_row["ticker"]
        form = manifest_row["form"]
        report_date = manifest_row["report_date"]
        source_document = manifest_row["primary_document"]

        detection = s144.detect_entry_point(temp_dir, primary_document_path)
        if not detection["resolved"]:
            raise ArchiveWarehouseLoadError(f"[{detection['category']}] {detection['reason']}")

        entry_point = Path(detection["selected_entry_point"])

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        warehouse_db_path.parent.mkdir(parents=True, exist_ok=True)

        options = RuntimeOptions(
            entrypointFile=str(entry_point), internetConnectivity=internet_connectivity,
            cacheDirectory=str(CACHE_DIR), internetTimeout=INTERNET_TIMEOUT_SECONDS,
            httpUserAgent=USER_AGENT, keepOpen=True,
            logFile=str(warehouse_db_path.parent / f"{warehouse_db_path.stem}_arelle.log"),
            logFormat="[%(levelname)s] [%(messageCode)s] %(message)s - %(file)s",
        )

        dts_start = time.perf_counter()
        try:
            with Session() as session:
                session.run(options)
                dts_seconds = time.perf_counter() - dts_start
                models = session.get_models()
                if len(models) != 1 or models[0] is None:
                    raise ArchiveWarehouseLoadError(f"[DTS_LOAD_FAILED] Arelle returned {len(models)} model(s), expected exactly 1")
                model_xbrl = models[0]

                facts_df = s121.extract_facts(model_xbrl, accession_number, source_document)
                contexts_df = s121.extract_contexts(model_xbrl, accession_number)
                units_df = s121.extract_units(model_xbrl, accession_number)
                presentation_df = s121.extract_presentation_relationships(model_xbrl, accession_number)
                calculation_df = s121.extract_calculation_relationships(model_xbrl, accession_number)
                definition_df = s121.extract_definition_relationships(model_xbrl, accession_number)
                roles_df = s121.extract_roles(model_xbrl, accession_number)
                qnames = s121.referenced_concept_qnames(facts_df, presentation_df, calculation_df, definition_df)
                concepts_df = s121.extract_concepts(model_xbrl, accession_number, qnames)
                labels_df = s121.extract_labels(model_xbrl, accession_number, qnames)
        except ArchiveWarehouseLoadError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ArchiveWarehouseLoadError(f"[DTS_LOAD_FAILED] {exc}") from exc

        computed_counts = {
            "xbrl_facts": len(facts_df), "xbrl_contexts": len(contexts_df), "xbrl_units": len(units_df),
            "xbrl_concepts": len(concepts_df), "xbrl_labels": len(labels_df),
            "xbrl_presentation_relationships": len(presentation_df), "xbrl_calculation_relationships": len(calculation_df),
            "xbrl_definition_relationships": len(definition_df), "xbrl_roles": len(roles_df),
        }

        if computed_counts["xbrl_facts"] == 0:
            raise ArchiveWarehouseLoadError("[ZERO_FACTS_EXTRACTED] Arelle loaded the DTS but extracted zero facts")
        if computed_counts["xbrl_contexts"] == 0:
            raise ArchiveWarehouseLoadError("[ZERO_CONTEXTS_EXTRACTED] zero contexts found")
        if computed_counts["xbrl_concepts"] == 0:
            raise ArchiveWarehouseLoadError("[ZERO_CONCEPTS_EXTRACTED] zero referenced concepts resolved")
        has_monetary_fact = bool(facts_df["unit_id"].astype(str).str.contains("usd", case=False, na=False).any()) if "unit_id" in facts_df.columns else False
        if has_monetary_fact and computed_counts["xbrl_units"] == 0:
            raise ArchiveWarehouseLoadError("[ZERO_UNITS_EXTRACTED_WITH_MONETARY_FACTS] monetary facts exist but zero units extracted")

        warehouse_connection = duckdb.connect(database=str(warehouse_db_path))
        try:
            warehouse_connection.execute("BEGIN TRANSACTION")
            s121.create_warehouse_schema(warehouse_connection)
            for table, df in (
                ("xbrl_facts", facts_df), ("xbrl_contexts", contexts_df), ("xbrl_units", units_df),
                ("xbrl_concepts", concepts_df), ("xbrl_labels", labels_df),
                ("xbrl_presentation_relationships", presentation_df), ("xbrl_calculation_relationships", calculation_df),
                ("xbrl_definition_relationships", definition_df), ("xbrl_roles", roles_df),
            ):
                s121.write_table(warehouse_connection, table, df, accession_number)

            inserted_counts = {
                t: warehouse_connection.execute(f"SELECT COUNT(*) FROM {t} WHERE accession_number = ?", [accession_number]).fetchone()[0]
                for t in WAREHOUSE_TABLES
            }
            mismatches = {t: (computed_counts[t], inserted_counts[t]) for t in WAREHOUSE_TABLES if computed_counts[t] != inserted_counts[t]}
            if mismatches:
                warehouse_connection.execute("ROLLBACK")
                raise ArchiveWarehouseLoadError(f"[INSERTED_COUNT_MISMATCH] {mismatches}")

            completed_at_utc = datetime.now(timezone.utc).isoformat()
            total_elapsed_seconds = time.perf_counter() - total_start
            warehouse_run_id = f"{accession_number}::165_filings_archive_arelle_loader.py::{started_at_utc}"
            warehouse_connection.execute(
                "INSERT INTO warehouse_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [warehouse_run_id, accession_number, ticker, report_date, "archive-warehouse-loader-v1",
                 ArelleVersion.getVersion(), "165_filings_archive_arelle_loader.py", started_at_utc, completed_at_utc,
                 0.0, round(dts_seconds, 6), 0.0, 0.0, 0.0, round(total_elapsed_seconds, 6),
                 json.dumps(inserted_counts), "PASS"],
            )
            warehouse_connection.execute("COMMIT")
        except ArchiveWarehouseLoadError:
            try:
                warehouse_connection.execute("ROLLBACK")
            except duckdb.TransactionException:
                pass
            raise
        finally:
            warehouse_connection.close()

    return {
        "status": "PASS", "accession_number": accession_number, "ticker": ticker,
        "form": form, "report_date": report_date, "detected_format": detection["detected_format"],
        "computed_counts": computed_counts, "inserted_counts": inserted_counts,
        "total_elapsed_seconds": round(total_elapsed_seconds, 3),
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load one filing from the compressed archive into a warehouse DuckDB via Arelle.")
    parser.add_argument("--accession-number", required=True)
    parser.add_argument("--warehouse-db-path", default=str(DEFAULT_PROOF_WAREHOUSE_DB_PATH))
    parser.add_argument("--internet-connectivity", default="online", choices=["online", "offline"])
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()

    print()
    print("=" * 110)
    print("ARCHIVE -> TEMP DIR -> ARELLE -> WAREHOUSE (single-accession proof)")
    print("=" * 110)
    print(f"Accession: {arguments.accession_number}")
    print(f"Warehouse DB: {arguments.warehouse_db_path}")
    print(f"Internet connectivity: {arguments.internet_connectivity}")

    archive_connection = duckdb.connect(database=str(s161.ARCHIVE_DB_PATH), read_only=True)
    try:
        result = run_archive_warehouse_load(
            archive_connection=archive_connection,
            accession_number=arguments.accession_number,
            warehouse_db_path=Path(arguments.warehouse_db_path),
            internet_connectivity=arguments.internet_connectivity,
        )
    finally:
        archive_connection.close()

    print()
    print(json.dumps(result, indent=2))
    print()
    print("RESULT: PASS")
    print("WORKER_RESULT_JSON=" + json.dumps(result))


if __name__ == "__main__":
    main()
