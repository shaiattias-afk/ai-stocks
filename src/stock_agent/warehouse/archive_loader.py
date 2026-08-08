"""warehouse/archive_loader.py — load one filing into the XBRL warehouse
sourcing its bytes ONLY from the compressed archive, never from
`data/sec_filings_locked/`.

Ported from `scripts/165_filings_archive_arelle_loader.py`. That script
reached `scripts/161` (archive core), `scripts/144` (entry-point
detection) and `scripts/121` (extraction primitives) through three
separate `importlib.spec_from_file_location` bridges; all three are now
normal package imports:

  scripts/161 -> stock_agent.filings.archive
  scripts/144 -> stock_agent.warehouse.loader.detect_entry_point
  scripts/121 -> stock_agent.warehouse.extract

The load logic is unchanged, so warehouse output stays byte-identical --
proven by rebuilding all 185 accessions and matching production exactly
(`REPRODUCTION_VERIFIED`, 225,780 facts).

PASS requires the same checks as the disk-based loader: entry point
resolved unambiguously, Arelle DTS load succeeds, facts/contexts/concepts
all > 0, units > 0 whenever a monetary fact exists, and the physically
inserted counts (re-queried inside the same transaction) exactly match the
computed counts.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from stock_agent import PROJECT_DIR
from stock_agent.filings import archive as filings_archive
from stock_agent.warehouse import extract as wh_extract
from stock_agent.warehouse.loader import detect_entry_point

DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "arelle_cache"
DEFAULT_PROOF_WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_from_archive_proof.duckdb"

USER_AGENT = "Shai Attias shaiattias@gmail.com"
INTERNET_TIMEOUT_SECONDS = 20

WAREHOUSE_TABLES = wh_extract.WAREHOUSE_TABLES

PARSER_VERSION = "archive-warehouse-loader-v1"
LOADER_NAME = "stock_agent.warehouse.archive_loader"


class ArchiveWarehouseLoadError(Exception):
    pass


def run_archive_warehouse_load(
    archive_connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    warehouse_db_path: Path,
    internet_connectivity: str = "online",
) -> dict:
    """Loads exactly one archived filing into `warehouse_db_path`,
    sourcing bytes ONLY from the compressed archive."""
    from arelle.RuntimeOptions import RuntimeOptions
    from arelle.api.Session import Session
    from arelle import Version as ArelleVersion

    total_start = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc).isoformat()

    with filings_archive.extracted_filing(archive_connection, accession_number) as (temp_dir, manifest_row):
        primary_document_path = temp_dir / manifest_row["primary_document"]
        ticker = manifest_row["ticker"]
        form = manifest_row["form"]
        report_date = manifest_row["report_date"]
        source_document = manifest_row["primary_document"]

        detection = detect_entry_point(temp_dir, primary_document_path)
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

                frames = wh_extract.extract_all(model_xbrl, accession_number, source_document)
        except ArchiveWarehouseLoadError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ArchiveWarehouseLoadError(f"[DTS_LOAD_FAILED] {exc}") from exc

        computed_counts = {table: len(df) for table, df in frames.items()}

        if computed_counts["xbrl_facts"] == 0:
            raise ArchiveWarehouseLoadError("[ZERO_FACTS_EXTRACTED] Arelle loaded the DTS but extracted zero facts")
        if computed_counts["xbrl_contexts"] == 0:
            raise ArchiveWarehouseLoadError("[ZERO_CONTEXTS_EXTRACTED] zero contexts found")
        if computed_counts["xbrl_concepts"] == 0:
            raise ArchiveWarehouseLoadError("[ZERO_CONCEPTS_EXTRACTED] zero referenced concepts resolved")
        facts_df = frames["xbrl_facts"]
        has_monetary_fact = bool(facts_df["unit_id"].astype(str).str.contains("usd", case=False, na=False).any()) if "unit_id" in facts_df.columns else False
        if has_monetary_fact and computed_counts["xbrl_units"] == 0:
            raise ArchiveWarehouseLoadError("[ZERO_UNITS_EXTRACTED_WITH_MONETARY_FACTS] monetary facts exist but zero units extracted")

        warehouse_connection = duckdb.connect(database=str(warehouse_db_path))
        try:
            warehouse_connection.execute("BEGIN TRANSACTION")
            wh_extract.create_warehouse_schema(warehouse_connection)
            for table in WAREHOUSE_TABLES:
                wh_extract.write_table(warehouse_connection, table, frames[table], accession_number)

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
            warehouse_run_id = f"{accession_number}::{LOADER_NAME}::{started_at_utc}"
            warehouse_connection.execute(
                "INSERT INTO warehouse_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [warehouse_run_id, accession_number, ticker, report_date, PARSER_VERSION,
                 ArelleVersion.getVersion(), LOADER_NAME, started_at_utc, completed_at_utc,
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
