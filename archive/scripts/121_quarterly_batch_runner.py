"""
Bounded 3-company x 3-fiscal-year quarterly production batch runner —
MSFT/AMZN/ORCL x FY2022/FY2023/FY2024 = 9 company-years. Not
company-specific: the 9 targets are a data list, not per-company code
branches; the same generic pipeline runs for all 9.

Reuses, unmodified:
  - scripts/107_download_accession_locked_filing_any_form.py — imported
    via importlib, its own functions called directly (find_company_record,
    load_all_filings, select_exact_filing, build_filing_base_url,
    load_filing_index, get_index_items, download_filing_files,
    save_manifests). Not copied, not reimplemented.
  - scripts/118_quarterly_extraction_engine.py — imported via importlib,
    run_quarterly_extraction_engine() called directly, unmodified.
  - scripts/120_quarterly_production_schema_load.py — imported via
    importlib; create_schema() and load_one_company() called directly
    (same transaction/read-back/rollback logic, unmodified).

The Arelle warehouse-loading internals (extract_facts/contexts/units/
concepts/labels/roles/presentation/calculation/definition relationships,
create_schema, write_table, already_loaded, load_one_filing, the
parent/worker subprocess pattern with a real 300s timeout) are the SAME
logic proven in scripts/108/110/112/115 — copied here (not imported,
since 115 hardcodes EXPECTED_FORM="10-K" at module scope) with the one
generalization this task requires: EXPECTED_FORM is now a parameter
("10-Q"), and the worker also downloads/locks a missing filing first via
scripts/107's functions before parsing it (115 only ever parsed
already-locked filings; 110/112 already-verified download+parse
happened in one script, which is what this task's STEP 3 requires).

STEP 1 (resolve filings): for each of the 9 target company-years, the FY
10-K is looked up directly from the production `sec_filings` table
(already warehoused per the completed 50/50 Annual XBRL Warehouse gate —
fails closed if not found there as `form=10-K` with a `warehouse_runs`
PASS row and nonzero fact/context/unit counts). Q1/Q2/Q3 10-Qs are
resolved from SEC's own submissions history (fetched once per company,
reused for every target fiscal year of that company): every `form=10-Q`
filing is kept, filtered to `prior_fiscal_year_end < reportDate <=
fiscal_year_end`, sorted ascending — must be EXACTLY 3, assigned to
Q1/Q2/Q3 in chronological order. Never assumed, never guessed; fails
closed if not exactly 3.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "arelle_cache"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
LOCKED_FILINGS_DIR = DATA_DIR / "sec_filings_locked"

SCRIPT_NAME = "121_quarterly_batch_runner.py"
CHILD_PROCESS_TIMEOUT_SECONDS = 300
INTERNET_TIMEOUT_SECONDS = 20
STANDARD_NAMESPACE_PATTERN = r"fasb\.org|xbrl\.sec\.gov|xbrl\.org|w3\.org"

BATCH_RESULT_PATH = DATA_DIR / "quarterly_batch_msft_amzn_orcl_fy2022_fy2024_result.json"

TARGET_COMPANY_YEARS: list[tuple[str, str]] = [
    ("MSFT", "2022-06-30"), ("MSFT", "2023-06-30"), ("MSFT", "2024-06-30"),
    ("AMZN", "2022-12-31"), ("AMZN", "2023-12-31"), ("AMZN", "2024-12-31"),
    ("ORCL", "2022-05-31"), ("ORCL", "2023-05-31"), ("ORCL", "2024-05-31"),
]

# ---------------------------------------------------------------------
# reuse existing verified modules via importlib (same pattern already
# used throughout this project, e.g. scripts/109/111/113/117/118/119
# importing scripts/89 this way)
# ---------------------------------------------------------------------

def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_DIR / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


s107 = _load_module("s107", "107_download_accession_locked_filing_any_form.py")
s118 = _load_module("s118", "118_quarterly_extraction_engine.py")
s120 = _load_module("s120", "120_quarterly_production_schema_load.py")


# =====================================================================
# FISCAL-YEAR WINDOW HELPERS
# =====================================================================

def compute_prior_fiscal_year_end(fiscal_year_end: str) -> str:
    d = date.fromisoformat(fiscal_year_end)
    try:
        prior = d.replace(year=d.year - 1)
    except ValueError:
        prior = d.replace(year=d.year - 1, day=28)  # Feb-29 fallback
    return prior.isoformat()


def resolve_quarters_for_fiscal_year(filings_df: pd.DataFrame, fiscal_year_end: str) -> dict:
    prior_fiscal_year_end = compute_prior_fiscal_year_end(fiscal_year_end)
    quarterly = filings_df[filings_df["form"] == "10-Q"].copy()
    quarterly = quarterly[
        (quarterly["reportDate"] > prior_fiscal_year_end) & (quarterly["reportDate"] <= fiscal_year_end)
    ].sort_values("reportDate")

    if len(quarterly) != 3:
        raise RuntimeError(
            f"Expected exactly 3 10-Q filings with reportDate in "
            f"({prior_fiscal_year_end}, {fiscal_year_end}], found {len(quarterly)}. "
            "Refusing to guess which are Q1/Q2/Q3."
        )

    labels = ["Q1", "Q2", "Q3"]
    result = {}
    for label, (_, row) in zip(labels, quarterly.iterrows()):
        result[label] = {
            "form": "10-Q", "report_date": str(row["reportDate"]),
            "filing_date": str(row["filingDate"]), "accession_number": str(row["accessionNumber"]),
            "primary_document": str(row["primaryDocument"]),
        }
    return result


# =====================================================================
# STEP 1a — FY 10-K must already be warehouse PASS (Annual XBRL
# Warehouse gate, completed previously). Read-only lookup, never loads
# Arelle for a 10-K in this task.
# =====================================================================

def resolve_fy_10k(prod_connection, warehouse_connection, ticker: str, fiscal_year_end: str) -> dict:
    row = prod_connection.execute(
        "SELECT accession_number, filing_date FROM sec_filings "
        "WHERE ticker = ? AND form = '10-K' AND report_date = ?",
        [ticker, fiscal_year_end],
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No 10-K found in sec_filings for {ticker} report_date={fiscal_year_end}.")
    accession_number, filing_date = row

    run = warehouse_connection.execute(
        "SELECT status FROM warehouse_runs WHERE accession_number = ? AND status = 'PASS'",
        [accession_number],
    ).fetchone()
    fact_count, context_count, unit_count = warehouse_connection.execute(
        "SELECT (SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?), "
        "(SELECT COUNT(*) FROM xbrl_contexts WHERE accession_number = ?), "
        "(SELECT COUNT(*) FROM xbrl_units WHERE accession_number = ?)",
        [accession_number, accession_number, accession_number],
    ).fetchone()

    if run is None or fact_count == 0 or context_count == 0 or unit_count == 0:
        raise RuntimeError(
            f"10-K {accession_number} for {ticker} {fiscal_year_end} is not warehouse PASS with "
            f"nonzero counts (run_found={run is not None}, facts={fact_count}, "
            f"contexts={context_count}, units={unit_count})."
        )

    return {"form": "10-K", "report_date": fiscal_year_end, "filing_date": str(filing_date),
            "accession_number": accession_number, "fact_count": fact_count,
            "context_count": context_count, "unit_count": unit_count}


# =====================================================================
# STEP 2/3 — reuse-or-lock, reuse-or-warehouse for each 10-Q
# =====================================================================

def is_already_locked(ticker: str, accession_number: str) -> bool:
    accession_compact = accession_number.replace("-", "")
    manifest_path = LOCKED_FILINGS_DIR / ticker / accession_compact / "locked_filing_manifest.json"
    return manifest_path.exists()


def lock_10q_if_missing(ticker: str, cik: int, company_name: str, quarter_info: dict) -> str:
    """Reuses scripts/107's own functions directly (not reimplemented).
    Returns 'REUSED' if already locked, 'NEWLY_LOCKED' otherwise."""

    accession_number = quarter_info["accession_number"]
    if is_already_locked(ticker, accession_number):
        return "REUSED"

    filing_base_url = s107.build_filing_base_url(cik=cik, accession_number=accession_number)
    accession_compact = accession_number.replace("-", "")
    output_directory = LOCKED_FILINGS_DIR / ticker / accession_compact

    filing_index = s107.load_filing_index(filing_base_url)
    index_items = s107.get_index_items(filing_index)
    downloaded_records = s107.download_filing_files(
        filing_base_url=filing_base_url, output_directory=output_directory, index_items=index_items
    )
    filing_series = pd.Series({
        "form": quarter_info["form"], "reportDate": quarter_info["report_date"],
        "filingDate": quarter_info["filing_date"], "accessionNumber": accession_number,
        "primaryDocument": quarter_info["primary_document"],
    })
    s107.save_manifests(
        ticker=ticker, company_name=company_name, cik=cik, filing=filing_series,
        filing_base_url=filing_base_url, output_directory=output_directory,
        downloaded_records=downloaded_records,
    )
    return "NEWLY_LOCKED"


def is_already_warehoused(accession_number: str) -> tuple[bool, dict]:
    connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    run = connection.execute(
        "SELECT status FROM warehouse_runs WHERE accession_number = ? AND status = 'PASS'",
        [accession_number],
    ).fetchone()
    fact_count, context_count, unit_count = connection.execute(
        "SELECT (SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?), "
        "(SELECT COUNT(*) FROM xbrl_contexts WHERE accession_number = ?), "
        "(SELECT COUNT(*) FROM xbrl_units WHERE accession_number = ?)",
        [accession_number, accession_number, accession_number],
    ).fetchone()
    connection.close()
    is_complete = run is not None and fact_count > 0 and context_count > 0 and unit_count > 0
    return is_complete, {"fact_count": fact_count, "context_count": context_count, "unit_count": unit_count}


# --- Arelle warehouse-loading internals (unchanged logic from
#     scripts/108/110/112/115; EXPECTED_FORM is now a parameter, and the
#     worker locks a missing filing first via scripts/107's functions) ---

def _role_definition(model_xbrl: Any, role_uri: str) -> str:
    role_types = model_xbrl.roleTypes.get(role_uri, [])
    for role_type in role_types:
        definition = getattr(role_type, "definition", "")
        if definition:
            return str(definition)
    return ""


def _dim_value_repr(dim_value: Any) -> str:
    member = getattr(dim_value, "memberQname", None)
    if member is not None:
        return str(member)
    typed_member = getattr(dim_value, "typedMember", None)
    if typed_member is not None:
        return str(getattr(typed_member, "text", typed_member))
    return str(dim_value)


def _context_period_fields(context: Any) -> dict[str, str | None]:
    is_instant = bool(getattr(context, "isInstantPeriod", False))
    is_duration = bool(getattr(context, "isStartEndPeriod", False))
    period_start = None
    period_end = None
    instant_date = None
    if is_duration:
        start_dt = context.startDatetime
        end_dt = context.endDatetime
        if start_dt is not None:
            period_start = start_dt.date().isoformat()
        if end_dt is not None:
            period_end = (end_dt - timedelta(days=1)).date().isoformat()
    elif is_instant:
        instant_dt = context.instantDatetime
        if instant_dt is not None:
            instant_date = (instant_dt - timedelta(days=1)).date().isoformat()
    return {"period_start": period_start, "period_end": period_end, "instant_date": instant_date}


def extract_facts(model_xbrl: Any, accession_number: str, source_document: str) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for fact_index, fact in enumerate(model_xbrl.facts):
        concept = getattr(fact, "concept", None)
        if concept is None:
            continue
        context = fact.context
        period_fields = _context_period_fields(context) if context is not None else {}
        dims = getattr(context, "qnameDims", {}) or {} if context is not None else {}
        dimensions_json = json.dumps({str(k): _dim_value_repr(v) for k, v in dims.items()}, ensure_ascii=False)
        is_nil = bool(getattr(fact, "isNil", False))
        value_raw = None if is_nil else fact.value
        value_numeric = None
        if not is_nil:
            try:
                value_numeric = float(fact.xValue)
            except (TypeError, ValueError):
                try:
                    value_numeric = float(fact.value)
                except (TypeError, ValueError):
                    value_numeric = None
        records.append({
            "accession_number": accession_number, "fact_index": fact_index,
            "concept_namespace": str(getattr(concept.qname, "namespaceURI", "")),
            "concept_local_name": str(getattr(concept.qname, "localName", "")),
            "concept_qname": str(concept.qname), "value_raw": value_raw, "value_numeric": value_numeric,
            "decimals": str(fact.decimals) if getattr(fact, "decimals", None) is not None else None,
            "precision": str(fact.precision) if getattr(fact, "precision", None) is not None else None,
            "unit_id": fact.unitID, "context_id": fact.contextID,
            "period_type": str(getattr(concept, "periodType", "")),
            "period_start": period_fields.get("period_start"), "period_end": period_fields.get("period_end"),
            "instant_date": period_fields.get("instant_date"), "dimensions_json": dimensions_json,
            "is_nil": is_nil, "source_document": source_document,
            "source_line": getattr(fact, "sourceline", None),
        })
    return pd.DataFrame(records)


def extract_contexts(model_xbrl: Any, accession_number: str) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for context_id, context in model_xbrl.contexts.items():
        period_fields = _context_period_fields(context)
        entity_id_tuple = getattr(context, "entityIdentifier", None)
        entity_identifier = str(entity_id_tuple[1]) if entity_id_tuple else None
        seg_dims = getattr(context, "segDimValues", {}) or {}
        scen_dims = getattr(context, "scenDimValues", {}) or {}
        segment_json = json.dumps({str(k): _dim_value_repr(v) for k, v in seg_dims.items()}, ensure_ascii=False)
        scenario_json = json.dumps({str(k): _dim_value_repr(v) for k, v in scen_dims.items()}, ensure_ascii=False)
        all_dims = getattr(context, "qnameDims", {}) or {}
        dimensions_json = json.dumps({str(k): _dim_value_repr(v) for k, v in all_dims.items()}, ensure_ascii=False)
        records.append({
            "accession_number": accession_number, "context_id": str(context_id),
            "entity_identifier": entity_identifier,
            "period_start": period_fields.get("period_start"), "period_end": period_fields.get("period_end"),
            "instant_date": period_fields.get("instant_date"), "dimensions_json": dimensions_json,
            "scenario_json": scenario_json, "segment_json": segment_json,
        })
    return pd.DataFrame(records)


def extract_units(model_xbrl: Any, accession_number: str) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for unit_id, unit in model_xbrl.units.items():
        measures = getattr(unit, "measures", None) or ((), ())
        numerator = measures[0] if len(measures) > 0 else ()
        denominator = measures[1] if len(measures) > 1 else ()
        records.append({
            "accession_number": accession_number, "unit_id": str(unit_id),
            "numerator_measures": ",".join(str(m) for m in numerator) or None,
            "denominator_measures": ",".join(str(m) for m in denominator) or None,
        })
    return pd.DataFrame(records)


def _walk_relationship_edges(relationship_set, role_uri, concept, edges, visited, extra_arc_attrs):
    concept_qname = str(getattr(concept, "qname", ""))
    relationships = relationship_set.fromModelObject(concept)
    for relationship in relationships:
        child = relationship.toModelObject
        if child is None:
            continue
        child_qname = str(getattr(child, "qname", ""))
        visit_key = (concept_qname, child_qname)
        edge = {"role_uri": role_uri, "parent_concept": concept_qname, "child_concept": child_qname,
                "order_value": float(getattr(relationship, "order", 0) or 0)}
        if extra_arc_attrs:
            edge["weight"] = float(getattr(relationship, "weight", 0) or 0)
        else:
            edge["preferred_label"] = str(getattr(relationship, "preferredLabel", "") or "")
        edges.append(edge)
        if visit_key in visited:
            continue
        visited.add(visit_key)
        _walk_relationship_edges(relationship_set, role_uri, child, edges, visited, extra_arc_attrs)


def extract_presentation_relationships(model_xbrl: Any, accession_number: str) -> pd.DataFrame:
    from arelle import XbrlConst
    records: list[dict[str, object]] = []
    global_relationship_set = model_xbrl.relationshipSet(XbrlConst.parentChild)
    for role_uri in sorted(global_relationship_set.linkRoleUris):
        relationship_set = model_xbrl.relationshipSet(XbrlConst.parentChild, role_uri)
        role_definition = _role_definition(model_xbrl, role_uri)
        roots = sorted(relationship_set.rootConcepts, key=lambda c: str(getattr(c, "qname", "")))
        for root in roots:
            edges: list[dict[str, object]] = []
            _walk_depth_first_presentation(relationship_set, role_uri, root, edges, set(), depth=0, parent_qname="")
            for edge in edges:
                edge["accession_number"] = accession_number
                edge["role_definition"] = role_definition
                records.append(edge)
    return pd.DataFrame(records)


def _walk_depth_first_presentation(relationship_set, role_uri, concept, records, visited, depth, parent_qname):
    concept_qname = str(getattr(concept, "qname", ""))
    visit_key = (parent_qname, concept_qname, depth)
    if visit_key in visited:
        return
    visited.add(visit_key)
    relationships = sorted(
        relationship_set.fromModelObject(concept),
        key=lambda r: (float(getattr(r, "order", 0) or 0), str(getattr(r.toModelObject, "qname", ""))),
    )
    for relationship in relationships:
        child = relationship.toModelObject
        if child is None:
            continue
        child_qname = str(getattr(child, "qname", ""))
        records.append({
            "role_uri": role_uri, "parent_concept": concept_qname, "child_concept": child_qname,
            "order_value": float(getattr(relationship, "order", 0) or 0),
            "preferred_label": str(getattr(relationship, "preferredLabel", "") or ""),
            "depth": depth + 1,
        })
        _walk_depth_first_presentation(relationship_set, role_uri, child, records, visited, depth + 1, concept_qname)


def extract_calculation_relationships(model_xbrl: Any, accession_number: str) -> pd.DataFrame:
    from arelle import XbrlConst
    records: list[dict[str, object]] = []
    global_relationship_set = model_xbrl.relationshipSet(XbrlConst.summationItem)
    for role_uri in sorted(global_relationship_set.linkRoleUris):
        relationship_set = model_xbrl.relationshipSet(XbrlConst.summationItem, role_uri)
        role_definition = _role_definition(model_xbrl, role_uri)
        roots = sorted(relationship_set.rootConcepts, key=lambda c: str(getattr(c, "qname", "")))
        for root in roots:
            edges: list[dict[str, object]] = []
            _walk_relationship_edges(relationship_set, role_uri, root, edges, set(), extra_arc_attrs=True)
            for edge in edges:
                edge["accession_number"] = accession_number
                edge["role_definition"] = role_definition
                records.append(edge)
    return pd.DataFrame(records)


def extract_definition_relationships(model_xbrl: Any, accession_number: str) -> pd.DataFrame:
    from arelle import XbrlConst
    arcroles = {
        "all": XbrlConst.all, "notAll": XbrlConst.notAll,
        "hypercubeDimension": XbrlConst.hypercubeDimension, "dimensionDomain": XbrlConst.dimensionDomain,
        "domainMember": XbrlConst.domainMember, "dimensionDefault": XbrlConst.dimensionDefault,
    }
    records: list[dict[str, object]] = []
    for arcrole_name, arcrole_uri in arcroles.items():
        global_relationship_set = model_xbrl.relationshipSet(arcrole_uri)
        for role_uri in sorted(global_relationship_set.linkRoleUris):
            relationship_set = model_xbrl.relationshipSet(arcrole_uri, role_uri)
            role_definition = _role_definition(model_xbrl, role_uri)
            roots = sorted(relationship_set.rootConcepts, key=lambda c: str(getattr(c, "qname", "")))
            for root in roots:
                _extract_definition_edges_recursive(
                    relationship_set, root, records, set(), accession_number, arcrole_name, role_uri, role_definition
                )
    return pd.DataFrame(records)


def _extract_definition_edges_recursive(relationship_set, concept, records, visited, accession_number, arcrole_name, role_uri, role_definition):
    concept_qname = str(getattr(concept, "qname", ""))
    relationships = relationship_set.fromModelObject(concept)
    for relationship in relationships:
        child = relationship.toModelObject
        if child is None:
            continue
        child_qname = str(getattr(child, "qname", ""))
        visit_key = (concept_qname, child_qname)
        records.append({
            "accession_number": accession_number, "arcrole": arcrole_name, "role_uri": role_uri,
            "role_definition": role_definition, "parent_concept": concept_qname, "child_concept": child_qname,
            "order_value": float(getattr(relationship, "order", 0) or 0),
            "closed_attr": bool(relationship.closed) if getattr(relationship, "closed", None) is not None else None,
            "context_element": getattr(relationship, "contextElement", None),
            "usable_attr": bool(relationship.usable) if getattr(relationship, "usable", None) is not None else None,
        })
        if visit_key in visited:
            continue
        visited.add(visit_key)
        _extract_definition_edges_recursive(relationship_set, child, records, visited, accession_number, arcrole_name, role_uri, role_definition)


def referenced_concept_qnames(facts_df, presentation_df, calculation_df, definition_df) -> set[str]:
    referenced: set[str] = set()
    if not facts_df.empty:
        referenced |= set(facts_df["concept_qname"].dropna().unique())
    for df in (presentation_df, calculation_df, definition_df):
        if df.empty:
            continue
        referenced |= set(df["parent_concept"].dropna().unique())
        referenced |= set(df["child_concept"].dropna().unique())
    referenced.discard("")
    return referenced


def extract_concepts(model_xbrl: Any, accession_number: str, qnames: set[str]) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    qname_concepts = model_xbrl.qnameConcepts
    by_str = {str(qname): concept for qname, concept in qname_concepts.items()}
    for qname_str in sorted(qnames):
        concept = by_str.get(qname_str)
        if concept is None:
            continue
        namespace = str(getattr(concept.qname, "namespaceURI", ""))
        is_extension = not bool(re.search(STANDARD_NAMESPACE_PATTERN, namespace, re.IGNORECASE))
        data_type = ""
        type_obj = getattr(concept, "type", None)
        if type_obj is not None:
            data_type = str(getattr(type_obj, "qname", "") or "")
        records.append({
            "accession_number": accession_number, "qname": qname_str, "namespace": namespace,
            "local_name": str(getattr(concept.qname, "localName", "")), "data_type": data_type,
            "balance_type": str(getattr(concept, "balance", "") or ""),
            "period_type": str(getattr(concept, "periodType", "")),
            "is_abstract": bool(getattr(concept, "isAbstract", False)), "is_extension": is_extension,
        })
    return pd.DataFrame(records)


def extract_labels(model_xbrl: Any, accession_number: str, qnames: set[str]) -> pd.DataFrame:
    from arelle import XbrlConst
    records: list[dict[str, object]] = []
    label_relationship_set = model_xbrl.relationshipSet(XbrlConst.conceptLabel)
    qname_concepts = model_xbrl.qnameConcepts
    by_str = {str(qname): concept for qname, concept in qname_concepts.items()}
    for qname_str in sorted(qnames):
        concept = by_str.get(qname_str)
        if concept is None:
            continue
        for relationship in label_relationship_set.fromModelObject(concept):
            label_resource = relationship.toModelObject
            if label_resource is None:
                continue
            text = getattr(label_resource, "textValue", None) or getattr(label_resource, "stringValue", None) or ""
            records.append({
                "accession_number": accession_number, "concept_qname": qname_str,
                "label_role": str(getattr(label_resource, "role", "") or ""),
                "language": str(getattr(label_resource, "xmlLang", "") or ""), "label_text": str(text),
            })
    return pd.DataFrame(records)


def extract_roles(model_xbrl: Any, accession_number: str) -> pd.DataFrame:
    from arelle import XbrlConst
    records: list[dict[str, object]] = []
    arcrole_by_type = {"presentation": XbrlConst.parentChild, "calculation": XbrlConst.summationItem}
    for relationship_type, arcrole in arcrole_by_type.items():
        relationship_set = model_xbrl.relationshipSet(arcrole)
        for role_uri in sorted(relationship_set.linkRoleUris):
            records.append({
                "accession_number": accession_number, "role_uri": role_uri,
                "role_definition": _role_definition(model_xbrl, role_uri), "relationship_type": relationship_type,
            })
    definition_arcroles = [
        XbrlConst.all, XbrlConst.notAll, XbrlConst.hypercubeDimension,
        XbrlConst.dimensionDomain, XbrlConst.domainMember, XbrlConst.dimensionDefault,
    ]
    definition_role_uris: set[str] = set()
    for arcrole in definition_arcroles:
        definition_role_uris |= set(model_xbrl.relationshipSet(arcrole).linkRoleUris)
    for role_uri in sorted(definition_role_uris):
        records.append({
            "accession_number": accession_number, "role_uri": role_uri,
            "role_definition": _role_definition(model_xbrl, role_uri), "relationship_type": "definition",
        })
    return pd.DataFrame(records)


def create_warehouse_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_facts (
        accession_number VARCHAR, fact_index INTEGER, concept_namespace VARCHAR,
        concept_local_name VARCHAR, concept_qname VARCHAR, value_raw VARCHAR,
        value_numeric DOUBLE, decimals VARCHAR, precision VARCHAR, unit_id VARCHAR,
        context_id VARCHAR, period_type VARCHAR, period_start VARCHAR, period_end VARCHAR,
        instant_date VARCHAR, dimensions_json VARCHAR, is_nil BOOLEAN, source_document VARCHAR,
        source_line INTEGER, PRIMARY KEY (accession_number, fact_index))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_contexts (
        accession_number VARCHAR, context_id VARCHAR, entity_identifier VARCHAR,
        period_start VARCHAR, period_end VARCHAR, instant_date VARCHAR, dimensions_json VARCHAR,
        scenario_json VARCHAR, segment_json VARCHAR, PRIMARY KEY (accession_number, context_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_units (
        accession_number VARCHAR, unit_id VARCHAR, numerator_measures VARCHAR,
        denominator_measures VARCHAR, PRIMARY KEY (accession_number, unit_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_concepts (
        accession_number VARCHAR, qname VARCHAR, namespace VARCHAR, local_name VARCHAR,
        data_type VARCHAR, balance_type VARCHAR, period_type VARCHAR, is_abstract BOOLEAN,
        is_extension BOOLEAN, PRIMARY KEY (accession_number, qname))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_labels (
        accession_number VARCHAR, concept_qname VARCHAR, label_role VARCHAR,
        language VARCHAR, label_text VARCHAR)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_presentation_relationships (
        accession_number VARCHAR, role_uri VARCHAR, role_definition VARCHAR,
        parent_concept VARCHAR, child_concept VARCHAR, order_value DOUBLE,
        preferred_label VARCHAR, depth INTEGER)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_calculation_relationships (
        accession_number VARCHAR, role_uri VARCHAR, role_definition VARCHAR,
        parent_concept VARCHAR, child_concept VARCHAR, weight DOUBLE, order_value DOUBLE)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_definition_relationships (
        accession_number VARCHAR, arcrole VARCHAR, role_uri VARCHAR, role_definition VARCHAR,
        parent_concept VARCHAR, child_concept VARCHAR, order_value DOUBLE, closed_attr BOOLEAN,
        context_element VARCHAR, usable_attr BOOLEAN)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS xbrl_roles (
        accession_number VARCHAR, role_uri VARCHAR, role_definition VARCHAR,
        relationship_type VARCHAR, PRIMARY KEY (accession_number, role_uri, relationship_type))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS warehouse_runs (
        warehouse_run_id VARCHAR PRIMARY KEY, accession_number VARCHAR, ticker VARCHAR,
        report_date VARCHAR, parser_version VARCHAR, arelle_version VARCHAR, script_name VARCHAR,
        started_at_utc VARCHAR, completed_at_utc VARCHAR, local_filing_load_seconds DOUBLE,
        taxonomy_dts_and_parse_seconds DOUBLE, fact_extraction_seconds DOUBLE,
        relationship_extraction_seconds DOUBLE, duckdb_write_seconds DOUBLE,
        total_elapsed_seconds DOUBLE, row_counts_json VARCHAR, status VARCHAR)""")


def write_table(connection, table_name, df, accession_number) -> int:
    connection.execute(f"DELETE FROM {table_name} WHERE accession_number = ?", [accession_number])
    if df.empty and len(df.columns) == 0:
        return 0
    connection.register("df_tmp", df)
    connection.execute(f"INSERT INTO {table_name} BY NAME SELECT * FROM df_tmp")
    connection.unregister("df_tmp")
    return int(len(df))


def load_and_warehouse_one_10q(ticker: str, report_date: str) -> dict[str, object]:
    """Worker-mode body: locates the already-locked package for this
    ticker/report_date (STEP 2/3's locking already happened in the
    parent before this subprocess was launched) and parses it with
    Arelle exactly once."""

    from arelle.RuntimeOptions import RuntimeOptions
    from arelle.api.Session import Session
    from arelle import Version as ArelleVersion

    total_start = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc).isoformat()

    locked_dir = LOCKED_FILINGS_DIR / ticker.upper()
    manifests = sorted(locked_dir.glob("*/locked_filing_manifest.json"))
    matching = [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in manifests
    ]
    matching = [
        (path, manifest) for path, manifest in matching
        if manifest.get("report_date") == report_date and manifest.get("form") == "10-Q"
    ]
    if len(matching) != 1:
        raise RuntimeError(f"Expected exactly 1 locked 10-Q manifest for {ticker}/{report_date}, found {len(matching)}.")
    _, manifest = matching[0]
    primary_document_path = Path(manifest["primary_document_path"]).resolve()
    if not primary_document_path.exists():
        raise FileNotFoundError(f"Primary document not found: {primary_document_path}")

    accession_number = manifest["accession_number"]
    source_document = manifest["primary_document"]
    sec_user_agent = str(manifest.get("sec_user_agent", "")).strip()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "database").mkdir(parents=True, exist_ok=True)

    options = RuntimeOptions(
        entrypointFile=str(primary_document_path), internetConnectivity="online",
        cacheDirectory=str(CACHE_DIR), internetTimeout=INTERNET_TIMEOUT_SECONDS,
        httpUserAgent=sec_user_agent, keepOpen=True,
        logFile=str(DATA_DIR / "database" / "xbrl_warehouse_proof_arelle.log"),
        logFormat="[%(levelname)s] [%(messageCode)s] %(message)s - %(file)s",
    )

    dts_parse_start = time.perf_counter()
    row_counts: dict[str, int] = {}
    status = "FAIL"

    with Session() as session:
        session.run(options)
        taxonomy_dts_and_parse_seconds = time.perf_counter() - dts_parse_start

        models = session.get_models()
        if len(models) != 1:
            raise RuntimeError(f"Arelle did not return a single model. Model count: {len(models)}")
        model_xbrl = models[0]
        if model_xbrl is None:
            raise RuntimeError("Arelle failed to load the XBRL model.")

        fact_extraction_start = time.perf_counter()
        facts_df = extract_facts(model_xbrl, accession_number, source_document)
        contexts_df = extract_contexts(model_xbrl, accession_number)
        units_df = extract_units(model_xbrl, accession_number)
        fact_extraction_seconds = time.perf_counter() - fact_extraction_start

        relationship_extraction_start = time.perf_counter()
        presentation_df = extract_presentation_relationships(model_xbrl, accession_number)
        calculation_df = extract_calculation_relationships(model_xbrl, accession_number)
        definition_df = extract_definition_relationships(model_xbrl, accession_number)
        roles_df = extract_roles(model_xbrl, accession_number)
        qnames = referenced_concept_qnames(facts_df, presentation_df, calculation_df, definition_df)
        concepts_df = extract_concepts(model_xbrl, accession_number, qnames)
        labels_df = extract_labels(model_xbrl, accession_number, qnames)
        relationship_extraction_seconds = time.perf_counter() - relationship_extraction_start

        duckdb_write_start = time.perf_counter()
        connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH))
        create_warehouse_schema(connection)
        row_counts["xbrl_facts"] = write_table(connection, "xbrl_facts", facts_df, accession_number)
        row_counts["xbrl_contexts"] = write_table(connection, "xbrl_contexts", contexts_df, accession_number)
        row_counts["xbrl_units"] = write_table(connection, "xbrl_units", units_df, accession_number)
        row_counts["xbrl_concepts"] = write_table(connection, "xbrl_concepts", concepts_df, accession_number)
        row_counts["xbrl_labels"] = write_table(connection, "xbrl_labels", labels_df, accession_number)
        row_counts["xbrl_presentation_relationships"] = write_table(connection, "xbrl_presentation_relationships", presentation_df, accession_number)
        row_counts["xbrl_calculation_relationships"] = write_table(connection, "xbrl_calculation_relationships", calculation_df, accession_number)
        row_counts["xbrl_definition_relationships"] = write_table(connection, "xbrl_definition_relationships", definition_df, accession_number)
        row_counts["xbrl_roles"] = write_table(connection, "xbrl_roles", roles_df, accession_number)
        duckdb_write_seconds = time.perf_counter() - duckdb_write_start
        status = "PASS"

    completed_at_utc = datetime.now(timezone.utc).isoformat()
    total_elapsed_seconds = time.perf_counter() - total_start
    warehouse_run_id = f"{accession_number}::{SCRIPT_NAME}::{started_at_utc}"
    connection.execute(
        "INSERT INTO warehouse_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [warehouse_run_id, accession_number, ticker, report_date, "generic-xbrl-warehouse-proof-v1",
         ArelleVersion.getVersion(), SCRIPT_NAME, started_at_utc, completed_at_utc,
         0.0, round(taxonomy_dts_and_parse_seconds, 6), round(fact_extraction_seconds, 6),
         round(relationship_extraction_seconds, 6), round(duckdb_write_seconds, 6),
         round(total_elapsed_seconds, 6), json.dumps(row_counts), status],
    )
    connection.close()

    return {"ticker": ticker, "report_date": report_date, "accession_number": accession_number,
            "status": status, "row_counts": row_counts, "total_elapsed_seconds": round(total_elapsed_seconds, 3)}


def run_worker(ticker: str, report_date: str) -> None:
    summary = load_and_warehouse_one_10q(ticker, report_date)
    print("WORKER_RESULT_JSON=" + json.dumps(summary))


def warehouse_10q_in_child_process(ticker: str, report_date: str) -> dict:
    filing_start = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--single-ticker", ticker, "--single-report-date", report_date],
            timeout=CHILD_PROCESS_TIMEOUT_SECONDS, capture_output=True, text=True, encoding="utf-8",
        )
        elapsed = time.perf_counter() - filing_start
        if result.returncode != 0:
            return {"status": "FAIL", "elapsed_seconds": round(elapsed, 3),
                    "error": f"child exited {result.returncode}", "stderr": result.stderr[-2000:]}
        worker_line = next((line for line in result.stdout.splitlines() if line.startswith("WORKER_RESULT_JSON=")), None)
        if worker_line is None:
            return {"status": "FAIL", "elapsed_seconds": round(elapsed, 3), "error": "no WORKER_RESULT_JSON line"}
        summary = json.loads(worker_line[len("WORKER_RESULT_JSON="):])
        summary["elapsed_seconds"] = round(elapsed, 3)
        return summary
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - filing_start
        return {"status": "TIMEOUT", "elapsed_seconds": round(elapsed, 3),
                "error": f"exceeded {CHILD_PROCESS_TIMEOUT_SECONDS}s"}


# =====================================================================
# EXISTING-COMPANY-YEAR VERIFICATION (skip path)
# =====================================================================

# =====================================================================
# sec_filings REGISTRATION — scripts/118's run_quarterly_extraction_engine
# looks up each accession's own report_date/filing_date/form/ticker from
# the production `sec_filings` table (the same authoritative metadata
# source used by the Annual Warehouse audit in scripts/114) rather than
# re-deriving it. The 9 FY2024 10-Q rows already there were registered by
# earlier, pre-this-session work; the 18 new FY2022/FY2023 10-Qs this
# task locks must be registered the same way — same schema, same natural
# key (accession_number), idempotent (checked before insert, never
# overwrites an existing row).
# =====================================================================

def ensure_sec_filings_row(connection, ticker: str, fiscal_year_end: str, quarter_info: dict) -> str:
    accession_number = quarter_info["accession_number"]
    existing = connection.execute(
        "SELECT accession_number FROM sec_filings WHERE accession_number = ?", [accession_number]
    ).fetchone()
    if existing is not None:
        return "ALREADY_REGISTERED"

    fiscal_year = int(fiscal_year_end[:4])
    connection.execute(
        "INSERT INTO sec_filings (accession_number, ticker, form, report_date, filing_date, "
        "fiscal_year, prior_report_date, source_document) VALUES (?,?,?,?,?,?,?,?)",
        [accession_number, ticker, quarter_info["form"], quarter_info["report_date"],
         quarter_info["filing_date"], fiscal_year, None, quarter_info["primary_document"]],
    )
    return "NEWLY_REGISTERED"


def verify_existing_company_year(prod_connection, ticker: str, fiscal_year_end: str) -> dict:
    run_row = prod_connection.execute(
        "SELECT run_id, run_status, q1_accession, q2_accession, q3_accession, fy_accession "
        "FROM quarterly_extraction_runs WHERE ticker = ? AND fiscal_year_end = ? "
        "AND engine_version = ?",
        [ticker, fiscal_year_end, s120.ENGINE_VERSION],
    ).fetchone()
    if run_row is None:
        return {"already_complete": False}

    run_id = run_row[0]
    rows = prod_connection.execute(
        "SELECT metric_name, fiscal_quarter, availability_date, accession_number, lineage_json, "
        "concept_qname FROM quarterly_metric_results WHERE run_id = ?", [run_id]
    ).fetchdf()

    checks = {
        "run_status_pass": run_row[1] == "PASS",
        "row_count_24": len(rows) == 24,
        "no_duplicate_keys": rows.groupby(["metric_name", "fiscal_quarter"]).size().max() == 1 if len(rows) else False,
        "lineage_complete": bool((rows["lineage_json"].notna() & rows["concept_qname"].notna() & rows["accession_number"].notna()).all()) if len(rows) else False,
        "availability_dates_present": bool(rows["availability_date"].notna().all()) if len(rows) else False,
    }
    return {"already_complete": all(checks.values()), "checks": checks, "run_id": run_id, "row_count": len(rows)}


def main() -> None:
    print("=" * 100)
    print("QUARTERLY BATCH RUNNER — MSFT/AMZN/ORCL x FY2022/FY2023/FY2024 (9 company-years)")
    print("=" * 100)

    batch_start = time.perf_counter()
    company_year_results: list[dict] = []
    filings_cache: dict[str, tuple[int, str, pd.DataFrame]] = {}

    # Schema creation happens once, in its own short-lived connection.
    # From here on, EVERY production-DB access opens its own fresh
    # connection and closes it immediately after use — never held open
    # across scripts/118's call, which opens its own internal connection
    # to the same file and would otherwise conflict (DuckDB refuses two
    # connections to the same file with different read-only/read-write
    # configurations open at once).
    _schema_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
    s120.create_schema(_schema_connection)
    _schema_connection.close()

    for ticker, fiscal_year_end in TARGET_COMPANY_YEARS:
        cy_start = time.perf_counter()
        print(f"\n>>> {ticker} FY(end={fiscal_year_end}) <<<")

        prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
        existing = verify_existing_company_year(prod_connection, ticker, fiscal_year_end)
        prod_connection.close()

        if existing["already_complete"]:
            elapsed = time.perf_counter() - cy_start
            print(f"  SKIPPED (already complete, verified 24/24 rows, no duplicates, full lineage) — {elapsed:.2f}s")
            company_year_results.append({
                "ticker": ticker, "fiscal_year_end": fiscal_year_end, "status": "SKIPPED_ALREADY_COMPLETE",
                "verification": existing, "elapsed_seconds": round(elapsed, 2),
            })
            continue

        try:
            # --- STEP 1: resolve filings ---
            warehouse_read_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
            prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
            fy_info = resolve_fy_10k(prod_connection, warehouse_read_connection, ticker, fiscal_year_end)
            prod_connection.close()
            warehouse_read_connection.close()

            if ticker not in filings_cache:
                company_record = s107.find_company_record(ticker)
                cik = int(company_record["cik_str"])
                company_name = str(company_record.get("title", ""))
                filings_df = s107.load_all_filings(cik)
                filings_cache[ticker] = (cik, company_name, filings_df)
            cik, company_name, filings_df = filings_cache[ticker]

            quarters = resolve_quarters_for_fiscal_year(filings_df, fiscal_year_end)
            print(f"  Resolved: Q1={quarters['Q1']['accession_number']} ({quarters['Q1']['report_date']})  "
                  f"Q2={quarters['Q2']['accession_number']} ({quarters['Q2']['report_date']})  "
                  f"Q3={quarters['Q3']['accession_number']} ({quarters['Q3']['report_date']})  "
                  f"FY={fy_info['accession_number']} ({fiscal_year_end})")

            # --- STEP 2/3: reuse-or-lock, reuse-or-warehouse each 10-Q ---
            quarter_status: dict[str, dict] = {}
            company_year_failed = False
            for label in ("Q1", "Q2", "Q3"):
                q = quarters[label]
                lock_result = lock_10q_if_missing(ticker, cik, company_name, q)
                already_warehoused, counts = is_already_warehoused(q["accession_number"])
                if already_warehoused:
                    quarter_status[label] = {"lock_result": lock_result, "warehouse_result": "REUSED", **counts}
                    print(f"  {label} {q['accession_number']}: lock={lock_result}, warehouse=REUSED "
                          f"(facts={counts['fact_count']}, contexts={counts['context_count']}, units={counts['unit_count']})")
                    continue

                warehouse_result = warehouse_10q_in_child_process(ticker, q["report_date"])
                quarter_status[label] = {"lock_result": lock_result, "warehouse_result": warehouse_result.get("status"),
                                          **warehouse_result.get("row_counts", {})}
                print(f"  {label} {q['accession_number']}: lock={lock_result}, warehouse={warehouse_result.get('status')} "
                      f"elapsed={warehouse_result.get('elapsed_seconds', warehouse_result.get('total_elapsed_seconds'))}s")

                if warehouse_result.get("status") not in ("PASS",):
                    company_year_failed = True
                    quarter_status[label]["error"] = warehouse_result.get("error")

            if company_year_failed:
                elapsed = time.perf_counter() - cy_start
                print(f"  COMPANY-YEAR FAIL/TIMEOUT — a 10-Q failed to warehouse. Skipping engine + load. {elapsed:.2f}s")
                company_year_results.append({
                    "ticker": ticker, "fiscal_year_end": fiscal_year_end, "status": "FAIL_WAREHOUSE",
                    "quarter_status": quarter_status, "elapsed_seconds": round(elapsed, 2),
                })
                continue

            # --- register the 3 newly-resolved 10-Qs in sec_filings so
            #     scripts/118's engine can look up their metadata, exactly
            #     as the 9 pre-existing FY2024 10-Q rows already are ---
            prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
            for label in ("Q1", "Q2", "Q3"):
                registration = ensure_sec_filings_row(prod_connection, ticker, fiscal_year_end, quarters[label])
                quarter_status[label]["sec_filings_registration"] = registration
            prod_connection.close()

            # --- STEP 4: run the consolidated engine ---
            # s118.run_quarterly_extraction_engine() opens its OWN
            # connection to the production DB internally (to look up
            # filing metadata) — no production connection of ours may be
            # open while it runs.
            engine_json_path = DATA_DIR / f"quarterly_engine_{ticker.lower()}_fy{fiscal_year_end[:4]}.json"
            engine_csv_path = DATA_DIR / f"quarterly_engine_{ticker.lower()}_fy{fiscal_year_end[:4]}.csv"
            engine_output = s118.run_quarterly_extraction_engine(
                ticker=ticker, fiscal_year_end=fiscal_year_end,
                q1_accession=quarters["Q1"]["accession_number"], q2_accession=quarters["Q2"]["accession_number"],
                q3_accession=quarters["Q3"]["accession_number"], fy_accession=fy_info["accession_number"],
                json_output_path=engine_json_path, csv_output_path=engine_csv_path,
            )
            metric_statuses = {m: engine_output["metrics"][m].get("status") for m in s118.METRICS}
            row_count = sum(len(engine_output["metrics"][m].get("quarters", {})) for m in s118.METRICS)
            print(f"  Engine: {row_count} rows, statuses={metric_statuses}")

            # --- STEP 5: load to production (reuse scripts/120's transaction logic) ---
            prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
            spec = {"fiscal_year_end": fiscal_year_end, "engine_json": engine_json_path}
            load_result = s120.load_one_company(prod_connection, ticker, spec, engine_output)
            prod_connection.close()
            print(f"  Production load: {load_result['status']}")

            elapsed = time.perf_counter() - cy_start
            company_year_results.append({
                "ticker": ticker, "fiscal_year_end": fiscal_year_end,
                "status": "COMMITTED" if load_result["status"] == "COMMITTED" else "LOAD_FAILED",
                "quarter_status": quarter_status, "metric_statuses": metric_statuses,
                "engine_row_count": row_count, "load_result": load_result,
                "elapsed_seconds": round(elapsed, 2),
            })
            print(f"  [{ticker} FY{fiscal_year_end[:4]}] filings=READY engine_rows={row_count} "
                  f"commit={load_result['status']} elapsed={elapsed:.2f}s")

        except Exception as exc:  # noqa: BLE001 - one company-year's failure must not stop the batch
            elapsed = time.perf_counter() - cy_start
            print(f"  EXCEPTION for {ticker} {fiscal_year_end}: {exc}")
            company_year_results.append({
                "ticker": ticker, "fiscal_year_end": fiscal_year_end, "status": "EXCEPTION",
                "error": str(exc), "elapsed_seconds": round(elapsed, 2),
            })

    # --- final aggregate validation (fresh connection) ---
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    total_runs = prod_connection.execute(
        "SELECT COUNT(*) FROM quarterly_extraction_runs WHERE engine_version = ?", [s120.ENGINE_VERSION]
    ).fetchone()[0]
    total_rows = prod_connection.execute(
        "SELECT COUNT(*) FROM quarterly_metric_results r JOIN quarterly_extraction_runs e "
        "ON e.run_id = r.run_id WHERE e.engine_version = ?", [s120.ENGINE_VERSION]
    ).fetchone()[0]
    basis_counts = dict(prod_connection.execute(
        "SELECT extraction_basis, COUNT(*) FROM quarterly_metric_results r JOIN quarterly_extraction_runs e "
        "ON e.run_id = r.run_id WHERE e.engine_version = ? GROUP BY extraction_basis", [s120.ENGINE_VERSION]
    ).fetchall())
    reconciliation_counts = dict(prod_connection.execute(
        "SELECT reconciliation_status, COUNT(*) FROM ("
        "  SELECT DISTINCT run_id, metric_name, reconciliation_status FROM quarterly_metric_results"
        ") x JOIN quarterly_extraction_runs e ON e.run_id = x.run_id WHERE e.engine_version = ? "
        "GROUP BY reconciliation_status", [s120.ENGINE_VERSION]
    ).fetchall())
    rows_per_company_year = prod_connection.execute(
        "SELECT r.ticker, r.fiscal_year_end, COUNT(*) FROM quarterly_metric_results r "
        "JOIN quarterly_extraction_runs e ON e.run_id = r.run_id WHERE e.engine_version = ? "
        "GROUP BY r.ticker, r.fiscal_year_end ORDER BY r.ticker, r.fiscal_year_end", [s120.ENGINE_VERSION]
    ).fetchall()
    duplicate_check = prod_connection.execute(
        "SELECT run_id, metric_name, fiscal_quarter, COUNT(*) c FROM quarterly_metric_results "
        "GROUP BY run_id, metric_name, fiscal_quarter HAVING COUNT(*) > 1"
    ).fetchall()
    review_required_rows = prod_connection.execute(
        "SELECT DISTINCT r.ticker, r.fiscal_year_end, r.metric_name FROM quarterly_metric_results r "
        "JOIN quarterly_extraction_runs e ON e.run_id = r.run_id "
        "WHERE e.engine_version = ? AND r.reconciliation_status = 'REVIEW_REQUIRED'", [s120.ENGINE_VERSION]
    ).fetchall()

    prod_connection.close()

    batch_result = {
        "target_company_years": len(TARGET_COMPANY_YEARS),
        "company_year_results": company_year_results,
        "total_extraction_runs": total_runs,
        "total_quarterly_metric_results": total_rows,
        "extraction_basis_counts": basis_counts,
        "reconciliation_status_counts_metric_year": reconciliation_counts,
        "rows_per_company_year": [{"ticker": t, "fiscal_year_end": f, "row_count": c} for t, f, c in rows_per_company_year],
        "duplicate_natural_keys": duplicate_check,
        "review_required_cases": review_required_rows,
        "batch_elapsed_seconds": round(time.perf_counter() - batch_start, 2),
    }

    print("\n" + "=" * 100)
    print("BATCH SUMMARY")
    print(f"  total_extraction_runs = {total_runs} (target 9)")
    print(f"  total_quarterly_metric_results = {total_rows} (target 216)")
    print(f"  extraction_basis_counts = {basis_counts}")
    print(f"  reconciliation_status_counts = {reconciliation_counts}")
    print(f"  duplicate_natural_keys = {duplicate_check}")
    print(f"  review_required_cases = {review_required_rows}")
    print(f"  batch_elapsed_seconds = {batch_result['batch_elapsed_seconds']}")
    print("=" * 100)

    with BATCH_RESULT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(batch_result, handle, indent=2, ensure_ascii=False, default=str)
    print(f"\nResult written to {BATCH_RESULT_PATH}")


def parse_worker_arguments():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--single-ticker", default=None)
    parser.add_argument("--single-report-date", default=None)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_worker_arguments()
    if arguments.single_report_date:
        run_worker(arguments.single_ticker, arguments.single_report_date)
    else:
        main()
