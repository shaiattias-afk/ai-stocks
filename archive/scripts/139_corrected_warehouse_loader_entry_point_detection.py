"""
A corrected, fail-closed, single-filing warehouse loader.

WHY: the remaining-21 audit (scripts/135) found that NVDA accession
0001045810-19-000079 has zero rows in every warehouse table, yet its two
recorded `warehouse_runs` rows (from scripts/123 and scripts/124) both
say status='PASS'. Reading scripts/121_quarterly_batch_runner.py's
`load_and_warehouse_one_10q()` (unchanged, not modified by this script)
shows exactly why, and it is a GENERAL bug, not NVDA-specific:

  1. Line ~643: `primary_document_path = manifest["primary_document_path"]`
     — the loader ALWAYS uses the SEC filing index's primaryDocument as
     the sole Arelle entry point, unconditionally.
  2. Line ~655: `entrypointFile=str(primary_document_path)` is passed to
     Arelle with no format check.
  3. Line ~706: `status = "PASS"` is set unconditionally once the Arelle
     Session context manager exits without raising an exception — NOTHING
     checks whether any row was actually extracted first.

This works for the common case (a modern Inline XBRL filing, where the
primary .htm document itself carries the `ix:` tagged facts). It silently
produces a technically-successful-but-empty load for the less common but
entirely valid case of a TRADITIONAL (non-inline) XBRL filing, where the
primary document is a plain HTML page and the actual machine-readable
facts live in a SEPARATE `.xml` instance document referenced only via the
filing's own `<link:schemaRef>` — exactly NVDA's Q1 FY2020 10-Q
(0001045810-19-000079, filed 2019-05-16), confirmed by direct inspection:
`nvda2020q110q.htm` (the primary document) contains no `ix:`/Inline-XBRL
markup at all, while `nvda-20190428.xml` in the SAME locked package is a
complete, valid `<xbrli:xbrl>` instance document (1.4 MB, real facts).
NVDA's very next 10-Q (0001045810-19-000144, filed ~3 months later)
already uses Inline XBRL — this is a real-world format transition, not a
data-absence problem.

THIS SCRIPT fixes both root causes, generally (no ticker-specific logic):
  1. `detect_entry_point()` — reads only a small prefix of the primary
     document to check for an Inline-XBRL namespace declaration. If
     absent, it searches the SAME locked-filing directory for a
     standalone `<xbrli:xbrl>` instance document (excluding linkbases,
     which have a different root element) and uses that instead. Neither
     branch is keyed to any specific ticker or filer.
  2. `run_corrected_warehouse_load()` never sets status="PASS" unless
     `xbrl_facts`, `xbrl_contexts`, and `xbrl_concepts` are all non-zero,
     and `xbrl_units` is non-zero whenever at least one extracted fact
     carries a monetary (iso4217) unit — with explicit, distinct failure
     categories otherwise.

Read-only against the locked filing package and the Arelle taxonomy
cache; writes ONLY to whatever `warehouse_db_path` the caller supplies
(a scratch database in this task — never
data/database/xbrl_warehouse_proof.duckdb, never
data/database/ai_stock_agent.duckdb). No network access
(`internetConnectivity="offline"`).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "arelle_cache"
LOCKED_FILINGS_DIR = DATA_DIR / "sec_filings_locked"

FAILURE_CATEGORIES = [
    "PACKAGE_INCOMPLETE", "ENTRY_POINT_NOT_RESOLVED", "DTS_LOAD_FAILED",
    "ZERO_FACTS_EXTRACTED", "ZERO_CONTEXTS_EXTRACTED", "ZERO_CONCEPTS_EXTRACTED",
    "ZERO_UNITS_EXTRACTED_WITH_MONETARY_FACTS",
]

# reuse (not copy) scripts/121's pure extraction functions and warehouse
# schema/writer — this script does not modify scripts/121 in any way
_spec = importlib.util.spec_from_file_location("s121", PROJECT_DIR / "scripts" / "121_quarterly_batch_runner.py")
s121 = importlib.util.module_from_spec(_spec)
sys.modules["s121"] = s121
_spec.loader.exec_module(s121)


def find_locked_manifest(ticker: str, report_date: str, form: str = "10-Q") -> dict:
    locked_dir = LOCKED_FILINGS_DIR / ticker.upper()
    manifests = sorted(locked_dir.glob("*/locked_filing_manifest.json"))
    matching = [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in manifests
    ]
    matching = [(p, m) for p, m in matching if m.get("report_date") == report_date and m.get("form") == form]
    if len(matching) != 1:
        raise RuntimeError(f"Expected exactly 1 locked {form} manifest for {ticker}/{report_date}, found {len(matching)}.")
    manifest_path, manifest = matching[0]
    manifest["_manifest_path"] = str(manifest_path)
    manifest["_locked_dir"] = str(manifest_path.parent)
    return manifest


def inspect_locked_package(manifest: dict) -> dict:
    """Phase 1 — list and classify every file in the locked package."""
    import hashlib

    locked_dir = Path(manifest["_locked_dir"])
    files = []
    for p in sorted(locked_dir.iterdir()):
        if not p.is_file():
            continue
        size = p.stat().st_size
        checksum = None
        if size < 5_000_000:  # cap hashing cost for very large files; still hash everything relevant here
            hasher = hashlib.sha256()
            with p.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            checksum = hasher.hexdigest()
        files.append({"name": p.name, "size_bytes": size, "sha256": checksum})

    primary_document_path = Path(manifest["primary_document_path"])
    entry_point_detection = detect_entry_point(locked_dir, primary_document_path)

    schema_files = [f["name"] for f in files if f["name"].lower().endswith(".xsd")]
    linkbase_files = [f["name"] for f in files if any(f["name"].lower().endswith(suf) for suf in ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml"))]
    candidate_instance_files = [f["name"] for f in files if f["name"].lower().endswith(".xml") and f["name"] not in linkbase_files
                                 and f["name"].lower() not in ("filingsummary.xml",)]
    htm_files = [f["name"] for f in files if f["name"].lower().endswith((".htm", ".html")) and not f["name"].lower().startswith("r")]

    package_complete = bool(schema_files) and (entry_point_detection["resolved"])

    return {
        "locked_dir": str(locked_dir), "file_count": len(files), "files": files,
        "primary_document": manifest["primary_document"],
        "html_documents_excluding_rendering_preview": htm_files,
        "candidate_traditional_instance_files": candidate_instance_files,
        "schema_files": schema_files, "linkbase_files": linkbase_files,
        "entry_point_detection": entry_point_detection,
        "package_complete_enough_to_attempt_parsing": package_complete,
    }


def _file_head(path: Path, n_bytes: int) -> str:
    with path.open("rb") as handle:
        return handle.read(n_bytes).decode("utf-8", errors="ignore")


def detect_entry_point(locked_dir: Path, primary_document_path: Path) -> dict:
    """General, non-ticker-specific entry-point detection:
      1. If the primary document itself declares the Inline XBRL
         namespace, use it directly (the common, modern case).
      2. Otherwise, look for a standalone <xbrli:xbrl> instance document
         in the same directory (excluding linkbases, whose root element
         is <link:linkbase>, not <xbrli:xbrl>) and use that instead.
    Never guesses when zero or multiple candidates are found."""
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
                          "<xbrli:xbrl> instance document in the same package"}
    if len(candidates) == 0:
        return {"resolved": False, "category": "ENTRY_POINT_NOT_RESOLVED",
                "reason": "primary document has no Inline XBRL markup, and no standalone XBRL instance "
                          "document was found in the locked package"}
    return {"resolved": False, "category": "ENTRY_POINT_NOT_RESOLVED",
            "reason": f"primary document has no Inline XBRL markup, and {len(candidates)} ambiguous "
                      f"standalone instance-document candidates were found: {[c.name for c in candidates]}"}


def run_corrected_warehouse_load(ticker: str, report_date: str, warehouse_db_path: Path, form: str = "10-Q") -> dict:
    """Phase 3/4 — the corrected, fail-closed, atomic single-filing load."""
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

    detection = detect_entry_point(locked_dir, primary_document_path)
    if not detection["resolved"]:
        return {
            "ticker": ticker, "report_date": report_date, "accession_number": accession_number,
            "status": "FAIL", "failure_category": detection["category"], "failure_reason": detection["reason"],
            "entry_point_detection": detection, "row_counts": {}, "elapsed_seconds": round(time.perf_counter() - total_start, 3),
        }

    entry_point = detection["selected_entry_point"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    warehouse_db_path.parent.mkdir(parents=True, exist_ok=True)

    options = RuntimeOptions(
        entrypointFile=entry_point, internetConnectivity="offline",
        cacheDirectory=str(CACHE_DIR), httpUserAgent=sec_user_agent, keepOpen=True,
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
                return {
                    "ticker": ticker, "report_date": report_date, "accession_number": accession_number,
                    "status": "FAIL", "failure_category": "DTS_LOAD_FAILED",
                    "failure_reason": f"Arelle returned {len(models)} model(s), expected exactly 1",
                    "entry_point_detection": detection, "row_counts": {},
                    "elapsed_seconds": round(time.perf_counter() - total_start, 3),
                }
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
    except Exception as exc:  # noqa: BLE001
        return {
            "ticker": ticker, "report_date": report_date, "accession_number": accession_number,
            "status": "FAIL", "failure_category": "DTS_LOAD_FAILED", "failure_reason": str(exc),
            "entry_point_detection": detection, "row_counts": {},
            "elapsed_seconds": round(time.perf_counter() - total_start, 3),
        }

    row_counts = {
        "xbrl_facts": len(facts_df), "xbrl_contexts": len(contexts_df), "xbrl_units": len(units_df),
        "xbrl_concepts": len(concepts_df), "xbrl_labels": len(labels_df),
        "xbrl_presentation_relationships": len(presentation_df), "xbrl_calculation_relationships": len(calculation_df),
        "xbrl_definition_relationships": len(definition_df), "xbrl_roles": len(roles_df),
    }

    # --- THE FAIL-CLOSED CHECK scripts/121 never had ---
    if row_counts["xbrl_facts"] == 0:
        failure_category, failure_reason = "ZERO_FACTS_EXTRACTED", "Arelle loaded the DTS but extracted zero facts from the selected entry point"
    elif row_counts["xbrl_contexts"] == 0:
        failure_category, failure_reason = "ZERO_CONTEXTS_EXTRACTED", "facts were extracted but zero contexts were found"
    elif row_counts["xbrl_concepts"] == 0:
        failure_category, failure_reason = "ZERO_CONCEPTS_EXTRACTED", "facts were extracted but zero referenced concepts resolved"
    elif not units_df.empty:
        failure_category, failure_reason = None, None
    else:
        has_monetary_fact = bool((facts_df["unit_id"].astype(str).str.contains("usd", case=False, na=False)).any()) if "unit_id" in facts_df.columns else False
        if has_monetary_fact and row_counts["xbrl_units"] == 0:
            failure_category, failure_reason = "ZERO_UNITS_EXTRACTED_WITH_MONETARY_FACTS", "monetary facts exist but zero units were extracted"
        else:
            failure_category, failure_reason = None, None

    if failure_category is not None:
        return {
            "ticker": ticker, "report_date": report_date, "accession_number": accession_number,
            "status": "FAIL", "failure_category": failure_category, "failure_reason": failure_reason,
            "entry_point_detection": detection, "row_counts": row_counts,
            "elapsed_seconds": round(time.perf_counter() - total_start, 3),
        }

    # --- atomic write to the SCRATCH warehouse database only ---
    connection = duckdb.connect(database=str(warehouse_db_path))
    try:
        connection.execute("BEGIN TRANSACTION")
        s121.create_warehouse_schema(connection)
        s121.write_table(connection, "xbrl_facts", facts_df, accession_number)
        s121.write_table(connection, "xbrl_contexts", contexts_df, accession_number)
        s121.write_table(connection, "xbrl_units", units_df, accession_number)
        s121.write_table(connection, "xbrl_concepts", concepts_df, accession_number)
        s121.write_table(connection, "xbrl_labels", labels_df, accession_number)
        s121.write_table(connection, "xbrl_presentation_relationships", presentation_df, accession_number)
        s121.write_table(connection, "xbrl_calculation_relationships", calculation_df, accession_number)
        s121.write_table(connection, "xbrl_definition_relationships", definition_df, accession_number)
        s121.write_table(connection, "xbrl_roles", roles_df, accession_number)

        completed_at_utc = datetime.now(timezone.utc).isoformat()
        warehouse_run_id = f"{accession_number}::139_corrected_warehouse_loader::{started_at_utc}"
        connection.execute(
            "INSERT INTO warehouse_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [warehouse_run_id, accession_number, ticker, report_date, "corrected-entry-point-detection-v1",
             ArelleVersion.getVersion(), "139_corrected_warehouse_loader_entry_point_detection.py",
             started_at_utc, completed_at_utc, 0.0, round(dts_seconds, 6), 0.0, 0.0, 0.0,
             round(time.perf_counter() - total_start, 6), json.dumps(row_counts), "PASS"],
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

    return {
        "ticker": ticker, "report_date": report_date, "accession_number": accession_number,
        "status": "PASS", "failure_category": None, "failure_reason": None,
        "entry_point_detection": detection, "row_counts": row_counts,
        "elapsed_seconds": round(time.perf_counter() - total_start, 3),
    }
