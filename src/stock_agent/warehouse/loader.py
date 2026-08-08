"""
warehouse/loader.py — the authoritative Arelle -> DuckDB warehouse loader
(D-041): distinguishes Inline XBRL from traditional XBRL with a separate
instance document, and requires validated non-zero content for PASS (a
successful Arelle return with nothing actually extracted is insufficient).

Ported byte-exact from scripts/144_warehouse_loader_v2_production.py.

The pure DataFrame-extraction primitives (extract_facts, extract_contexts,
..., create_warehouse_schema, write_table) now live in
`stock_agent.warehouse.extract`, imported normally. They were previously
reached through an `importlib.spec_from_file_location` bridge into
`scripts/121_quarterly_batch_runner.py` — the last file-path import hack
inside this package, now removed. The extraction logic itself is
unchanged, so warehouse output is byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from stock_agent import PROJECT_DIR
from stock_agent.warehouse import extract as wh_extract

DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "arelle_cache"
LOCKED_FILINGS_DIR = DATA_DIR / "sec_filings_locked"

WAREHOUSE_TABLES = [
    "xbrl_facts", "xbrl_contexts", "xbrl_units", "xbrl_concepts", "xbrl_labels",
    "xbrl_presentation_relationships", "xbrl_calculation_relationships",
    "xbrl_definition_relationships", "xbrl_roles",
]

FAILURE_CATEGORIES = [
    "PACKAGE_INCOMPLETE", "ENTRY_POINT_NOT_RESOLVED", "MULTIPLE_INSTANCE_CANDIDATES",
    "DTS_LOAD_FAILED", "ZERO_FACTS_EXTRACTED", "ZERO_CONTEXTS_EXTRACTED",
    "ZERO_CONCEPTS_EXTRACTED", "ZERO_UNITS_EXTRACTED_WITH_MONETARY_FACTS",
    "INSERTED_COUNT_MISMATCH", "INCOMPLETE_LINEAGE",
]

class WarehouseLoaderError(Exception):
    def __init__(self, category: str, message: str) -> None:
        if category not in FAILURE_CATEGORIES:
            raise ValueError(f"Unknown failure category: {category!r}")
        self.category = category
        super().__init__(f"[{category}] {message}")


def _file_head(path: Path, n_bytes: int) -> str:
    with path.open("rb") as handle:
        return handle.read(n_bytes).decode("utf-8", errors="ignore")


def _sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def find_locked_manifest(ticker: str, report_date: str, form: str = "10-Q") -> dict:
    locked_dir = LOCKED_FILINGS_DIR / ticker.upper()
    manifests = sorted(locked_dir.glob("*/locked_filing_manifest.json"))
    matching = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in manifests]
    matching = [(p, m) for p, m in matching if m.get("report_date") == report_date and m.get("form") == form]
    if len(matching) != 1:
        raise WarehouseLoaderError("PACKAGE_INCOMPLETE", f"Expected exactly 1 locked {form} manifest for {ticker}/{report_date}, found {len(matching)}.")
    manifest_path, manifest = matching[0]
    manifest["_manifest_path"] = str(manifest_path)
    manifest["_locked_dir"] = str(manifest_path.parent)
    return manifest


def detect_entry_point(locked_dir: Path, primary_document_path: Path) -> dict:
    """General entry-point detection, identical logic to
    scripts/139_corrected_warehouse_loader_entry_point_detection.py's
    detect_entry_point (re-derived here, not imported, so this production
    module has no runtime dependency on the scratch-proof script):
      1. If the primary document declares the Inline XBRL namespace, use
         it directly.
      2. Otherwise, look for exactly one standalone <xbrli:xbrl> instance
         document in the same locked-filing directory (excluding schemas
         and linkbases). Fail closed on zero or multiple candidates.
    No ticker- or accession-specific logic anywhere in this function."""
    if not primary_document_path.exists():
        return {"resolved": False, "category": "PACKAGE_INCOMPLETE",
                "reason": f"primary_document_path does not exist: {primary_document_path}"}

    head = _file_head(primary_document_path, 200_000)
    if "inlinexbrl" in head.lower().replace(" ", ""):
        return {"resolved": True, "selected_entry_point": str(primary_document_path),
                "detected_format": "INLINE_XBRL", "reason": "primary document declares the Inline XBRL namespace"}

    candidates = []
    for p in sorted(locked_dir.glob("*.xml")):
        name_lower = p.name.lower()
        if name_lower.endswith(".xsd"):
            continue
        if any(name_lower.endswith(suf) for suf in ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml")):
            continue
        if name_lower == "filingsummary.xml":
            continue
        head2 = _file_head(p, 2000)
        if "<xbrli:xbrl" in head2 or "<xbrl " in head2.lower() or head2.lstrip().lower().startswith("<xbrl"):
            candidates.append(p)

    if len(candidates) == 1:
        return {"resolved": True, "selected_entry_point": str(candidates[0]),
                "detected_format": "TRADITIONAL_XBRL_SEPARATE_INSTANCE",
                "reason": "primary document has no Inline XBRL markup; found exactly one standalone "
                          "<xbrli:xbrl> instance document in the same package",
                "candidate_count": 1}
    if len(candidates) == 0:
        return {"resolved": False, "category": "ENTRY_POINT_NOT_RESOLVED",
                "reason": "primary document has no Inline XBRL markup, and no standalone XBRL instance "
                          "document was found in the locked package", "candidate_count": 0}
    return {"resolved": False, "category": "MULTIPLE_INSTANCE_CANDIDATES",
            "reason": f"primary document has no Inline XBRL markup, and {len(candidates)} ambiguous "
                      f"standalone instance-document candidates were found: {[c.name for c in candidates]}",
            "candidate_count": len(candidates), "candidates": [c.name for c in candidates]}


def run_production_warehouse_load(ticker: str, report_date: str, warehouse_db_path: Path, form: str = "10-Q") -> dict:
    """Loads exactly one filing into `warehouse_db_path` (the caller
    decides which database -- production or otherwise; this function has
    no hardcoded database path). PASS requires: locked package complete;
    entry point resolved unambiguously; Arelle DTS load succeeds;
    xbrl_facts/xbrl_contexts/xbrl_concepts all > 0; xbrl_units > 0
    whenever at least one extracted fact carries a monetary (iso4217)
    unit; the exact counts written match the counts computed in memory
    (verified by re-querying immediately after the write, inside the same
    transaction); complete accession + selected-entry-point lineage is
    present in the returned result. Arelle completing without a Python
    exception is NOT sufficient for PASS -- this function never sets
    status="PASS" without passing every one of these checks."""
    from arelle.RuntimeOptions import RuntimeOptions
    from arelle.api.Session import Session
    from arelle import Version as ArelleVersion

    total_start = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc).isoformat()

    manifest = find_locked_manifest(ticker, report_date, form)
    locked_dir = Path(manifest["_locked_dir"])
    primary_document_path = Path(manifest["primary_document_path"])
    accession_number = manifest["accession_number"]
    source_document = manifest["primary_document"]
    sec_user_agent = str(manifest.get("sec_user_agent", "")).strip()

    if not locked_dir.is_dir():
        raise WarehouseLoaderError("PACKAGE_INCOMPLETE", f"locked package directory does not exist: {locked_dir}")

    detection = detect_entry_point(locked_dir, primary_document_path)
    if not detection["resolved"]:
        raise WarehouseLoaderError(detection["category"], detection["reason"])

    entry_point = Path(detection["selected_entry_point"])
    entry_point_checksum = _sha256_of_file(entry_point)
    primary_document_checksum = _sha256_of_file(primary_document_path)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    warehouse_db_path.parent.mkdir(parents=True, exist_ok=True)

    options = RuntimeOptions(
        entrypointFile=str(entry_point), internetConnectivity="offline",
        cacheDirectory=str(CACHE_DIR), httpUserAgent=sec_user_agent, keepOpen=True,
        logFile=str(warehouse_db_path.parent / f"{warehouse_db_path.stem}_production_load_arelle.log"),
        logFormat="[%(levelname)s] [%(messageCode)s] %(message)s - %(file)s",
    )

    dts_start = time.perf_counter()
    try:
        with Session() as session:
            session.run(options)
            dts_seconds = time.perf_counter() - dts_start
            models = session.get_models()
            if len(models) != 1 or models[0] is None:
                raise WarehouseLoaderError("DTS_LOAD_FAILED", f"Arelle returned {len(models)} model(s), expected exactly 1")
            model_xbrl = models[0]

            facts_df = wh_extract.extract_facts(model_xbrl, accession_number, source_document)
            contexts_df = wh_extract.extract_contexts(model_xbrl, accession_number)
            units_df = wh_extract.extract_units(model_xbrl, accession_number)
            presentation_df = wh_extract.extract_presentation_relationships(model_xbrl, accession_number)
            calculation_df = wh_extract.extract_calculation_relationships(model_xbrl, accession_number)
            definition_df = wh_extract.extract_definition_relationships(model_xbrl, accession_number)
            roles_df = wh_extract.extract_roles(model_xbrl, accession_number)
            qnames = wh_extract.referenced_concept_qnames(facts_df, presentation_df, calculation_df, definition_df)
            concepts_df = wh_extract.extract_concepts(model_xbrl, accession_number, qnames)
            labels_df = wh_extract.extract_labels(model_xbrl, accession_number, qnames)
    except WarehouseLoaderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WarehouseLoaderError("DTS_LOAD_FAILED", str(exc)) from exc

    computed_counts = {
        "xbrl_facts": len(facts_df), "xbrl_contexts": len(contexts_df), "xbrl_units": len(units_df),
        "xbrl_concepts": len(concepts_df), "xbrl_labels": len(labels_df),
        "xbrl_presentation_relationships": len(presentation_df), "xbrl_calculation_relationships": len(calculation_df),
        "xbrl_definition_relationships": len(definition_df), "xbrl_roles": len(roles_df),
    }

    if computed_counts["xbrl_facts"] == 0:
        raise WarehouseLoaderError("ZERO_FACTS_EXTRACTED", "Arelle loaded the DTS but extracted zero facts from the selected entry point")
    if computed_counts["xbrl_contexts"] == 0:
        raise WarehouseLoaderError("ZERO_CONTEXTS_EXTRACTED", "facts were extracted but zero contexts were found")
    if computed_counts["xbrl_concepts"] == 0:
        raise WarehouseLoaderError("ZERO_CONCEPTS_EXTRACTED", "facts were extracted but zero referenced concepts resolved")
    has_monetary_fact = bool(facts_df["unit_id"].astype(str).str.contains("usd", case=False, na=False).any()) if "unit_id" in facts_df.columns else False
    if has_monetary_fact and computed_counts["xbrl_units"] == 0:
        raise WarehouseLoaderError("ZERO_UNITS_EXTRACTED_WITH_MONETARY_FACTS", "monetary facts exist but zero units were extracted")

    lineage = {
        "accession_number": accession_number, "ticker": ticker, "form": form, "report_date": report_date,
        "locked_dir": str(locked_dir), "primary_document": source_document,
        "primary_document_checksum": primary_document_checksum,
        "selected_entry_point": str(entry_point), "selected_entry_point_name": entry_point.name,
        "selected_entry_point_checksum": entry_point_checksum, "detected_format": detection["detected_format"],
    }
    if not all(lineage.get(k) for k in ("accession_number", "ticker", "form", "report_date", "selected_entry_point", "detected_format")):
        raise WarehouseLoaderError("INCOMPLETE_LINEAGE", f"lineage is missing required fields: {lineage}")

    # --- atomic write ---
    connection = duckdb.connect(database=str(warehouse_db_path))
    try:
        connection.execute("BEGIN TRANSACTION")
        wh_extract.create_warehouse_schema(connection)
        for table, df in (
            ("xbrl_facts", facts_df), ("xbrl_contexts", contexts_df), ("xbrl_units", units_df),
            ("xbrl_concepts", concepts_df), ("xbrl_labels", labels_df),
            ("xbrl_presentation_relationships", presentation_df), ("xbrl_calculation_relationships", calculation_df),
            ("xbrl_definition_relationships", definition_df), ("xbrl_roles", roles_df),
        ):
            wh_extract.write_table(connection, table, df, accession_number)

        # verify inserted physical counts equal computed counts BEFORE commit
        inserted_counts = {
            t: connection.execute(f"SELECT COUNT(*) FROM {t} WHERE accession_number = ?", [accession_number]).fetchone()[0]
            for t in WAREHOUSE_TABLES
        }
        mismatches = {t: (computed_counts[t], inserted_counts[t]) for t in WAREHOUSE_TABLES if computed_counts[t] != inserted_counts[t]}
        if mismatches:
            connection.execute("ROLLBACK")
            raise WarehouseLoaderError("INSERTED_COUNT_MISMATCH", f"computed vs. inserted count mismatch: {mismatches}")

        completed_at_utc = datetime.now(timezone.utc).isoformat()
        total_elapsed_seconds = time.perf_counter() - total_start
        warehouse_run_id = f"{accession_number}::144_warehouse_loader_v2_production.py::{started_at_utc}"
        connection.execute(
            "INSERT INTO warehouse_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [warehouse_run_id, accession_number, ticker, report_date, "warehouse-loader-v2-production",
             ArelleVersion.getVersion(), "144_warehouse_loader_v2_production.py", started_at_utc, completed_at_utc,
             0.0, round(dts_seconds, 6), 0.0, 0.0, 0.0, round(total_elapsed_seconds, 6),
             json.dumps(inserted_counts), "PASS"],
        )
        connection.execute("COMMIT")
    except WarehouseLoaderError:
        try:
            connection.execute("ROLLBACK")
        except duckdb.TransactionException:
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        try:
            connection.execute("ROLLBACK")
        except duckdb.TransactionException:
            pass
        raise WarehouseLoaderError("INSERTED_COUNT_MISMATCH", f"unexpected error during atomic write: {exc}") from exc
    finally:
        connection.close()

    return {
        "status": "PASS", "warehouse_run_id": warehouse_run_id, "accession_number": accession_number,
        "ticker": ticker, "report_date": report_date, "lineage": lineage,
        "computed_counts": computed_counts, "inserted_counts": inserted_counts,
        "started_at_utc": started_at_utc, "completed_at_utc": completed_at_utc,
        "total_elapsed_seconds": round(total_elapsed_seconds, 3),
    }
