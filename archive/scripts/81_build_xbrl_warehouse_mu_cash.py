"""
D-022 warehouse loader — extends scripts/73/75 (which proved and then
generalized the warehouse architecture, approved in docs/DECISIONS_LOG.md
D-021) to the 10 AMZN/GOOGL company-years targeted by the maturity-based
debt classification: AMZN 2021-2025, GOOGL 2021-2025 (5 years each).
AMZN 2024-12-31 is already in the warehouse (loaded by scripts/73) —
already_loaded() below checks by accession_number alone across ANY
prior loading script, so it is correctly skipped here without
reopening Arelle. The other 9 filings are parsed exactly once each,
closed before the next is opened, and written into the SAME warehouse
(data/database/xbrl_warehouse_proof.duckdb) alongside every filing
already there — idempotent per accession_number (CREATE TABLE IF NOT
EXISTS + per-accession DELETE, unchanged from scripts/75).

Architecture distinction (binding, per docs/DECISIONS_LOG.md D-021):
  - The original SEC filing package on disk remains the immutable raw
    source — never modified, never touched by this script beyond a
    read-only open.
  - This warehouse stores the COMPLETE parsed XBRL facts and
    relationships — not a canonical-metric result, a raw structured
    layer one level below any accounting-policy interpretation.
  - Canonical metrics (current_debt, etc.) remain versioned outputs
    derived FROM this warehouse by a separate, later step — this
    script does not compute or change any canonical metric.

Written to the SAME SEPARATE proof database as scripts/73/75 — the
production database (data/database/ai_stock_agent.duckdb) is never
opened by this script. No ticker- or year-specific mapping rule
anywhere below (every extraction function here is the exact same,
unchanged, generic function as scripts/73/75).

Scoping decision (documented, not silent): concepts and labels are
preserved only for concepts actually REFERENCED by this filing — i.e.
appearing in its own reported facts, or as a parent/child anywhere in
its own presentation/calculation/definition relationship networks —
not the entire base us-gaap/dei/srt taxonomy (tens of thousands of
concepts/labels that were never used in this filing). This keeps the
proof bounded and directly traceable to this filing, while still
preserving every piece of structural evidence any of this project's
existing row-identification/classification logic has ever used
(role/label/presentation-ancestry/calculation-weight/context/unit/
dimension evidence — see docs/DECISIONS_LOG.md D-007/D-016/D-019/D-020).

Timing note (documented, not fabricated): Arelle's public Session API
loads the local instance document and resolves/loads its referenced
taxonomy (DTS) together, inside one call (`session.run()`) — for an
Inline XBRL 10-K, the primary document itself carries the schemaRef
that triggers DTS discovery, and Arelle does not expose "instance
parse" and "DTS load" as two independently-timeable sub-phases through
its public Session API. Splitting them further would require patching
Arelle internals, out of scope for a bounded proof. What IS measured
separately and honestly:
  - local_filing_load_seconds: raw disk I/O time to read the primary
    document's bytes (a genuine, separate, sub-millisecond measurement
    — a floor/proxy for "local filing load", since Arelle re-reads the
    file itself inside session.run() and does not expose its own
    internal file-open timing as a separate hook).
  - taxonomy_dts_and_parse_seconds: the single `session.run()` call —
    covers BOTH taxonomy/DTS loading and instance parsing together,
    honestly labeled as combined rather than falsely split.
  - fact_extraction_seconds: this script's own walk of
    model_xbrl.facts/contexts/units.
  - relationship_extraction_seconds: this script's own walk of
    concepts/labels/presentation/calculation/definition/roles.
  - duckdb_write_seconds: writing every extracted table to DuckDB.
  - total_elapsed_seconds: the full script, start to finish.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "arelle_cache"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"

SCRIPT_NAME = "81_build_xbrl_warehouse_mu_cash.py"
EXPECTED_FORM = "10-K"

# The 10 AMZN/GOOGL company-years targeted by the maturity-based debt
# classification (D-022). AMZN 2024-12-31 is already in the warehouse
# (loaded by scripts/73/75) — already_loaded() below skips it without
# reopening Arelle. The other 9 are missing and get parsed here, each
# exactly once.
# Widened to the full 10-company-year list after the bounded proof
# (AMZN 2024-12-31 + GOOGL 2021-12-31) passed cleanly — zero
# reconciliation gap on both, no ambiguous buckets, no ticker-specific
# handling needed. already_loaded() skips AMZN 2024-12-31 and GOOGL
# 2021-12-31 (already in the warehouse), so only the remaining 8
# filings are actually parsed by this run.
# Widened after the proof (MU 2024 vs 2025) confirmed one general,
# defensible label-wording pattern ("Cash and equivalents" vs "Cash and
# cash equivalents", same concept/role/position) — already_loaded()
# skips MU 2024/2025, already in the warehouse from the proof stage.
TARGET_FILINGS: list[tuple[str, str]] = [
    ("MU", "2021-09-02"),
    ("MU", "2022-09-01"),
    ("MU", "2023-08-31"),
    ("MU", "2024-08-29"),
    ("MU", "2025-08-28"),
]

INTERNET_TIMEOUT_SECONDS = 20

# Standard-taxonomy namespace signal (FASB us-gaap/srt, SEC dei/ecd/
# country/currency/exch/naics/sic/stpr/invest, W3C xbrli/xlink/xml) vs.
# a filer's OWN extension taxonomy (any other namespace). Generic
# across any filer — never a ticker-specific check.
STANDARD_NAMESPACE_PATTERN = r"fasb\.org|xbrl\.sec\.gov|xbrl\.org|w3\.org"


# =============================================================================
# Filing lock loading (identical logic to scripts/69+72's
# load_locked_filing — copied, not imported, per this project's
# established one-file-per-script convention).
# =============================================================================


def load_locked_filing(ticker: str, report_date: str) -> dict[str, Any]:
    locked_dir = DATA_DIR / "sec_filings_locked" / ticker.upper()
    manifests = sorted(locked_dir.glob("*/locked_filing_manifest.json"))
    matching_manifests = []

    for manifest_file in manifests:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

        if (
            manifest.get("report_date") == report_date
            and manifest.get("form") == EXPECTED_FORM
        ):
            matching_manifests.append((manifest_file, manifest))

    if len(matching_manifests) != 1:
        raise RuntimeError(
            "לא נמצא Manifest נעול יחיד וברור עבור "
            f"{ticker} / {report_date}. מספר התאמות: {len(matching_manifests)}"
        )

    _, manifest = matching_manifests[0]
    primary_document_path = Path(manifest["primary_document_path"]).resolve()

    if not primary_document_path.exists():
        raise FileNotFoundError(f"קובץ ה-10-K הראשי לא נמצא:\n{primary_document_path}")

    return {
        "primary_document_path": primary_document_path,
        "accession_number": manifest.get("accession_number"),
        "report_date": manifest.get("report_date"),
        "filing_date": manifest.get("filing_date"),
        "sec_user_agent": str(manifest.get("sec_user_agent", "")).strip(),
        "cik": int(manifest.get("cik")),
        "ticker": manifest.get("ticker", ticker.upper()),
        "company_name": manifest.get("company_name", ""),
        "primary_document_name": manifest.get("primary_document"),
    }


# =============================================================================
# Extraction helpers
# =============================================================================


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

    return {
        "period_start": period_start,
        "period_end": period_end,
        "instant_date": instant_date,
    }


def extract_facts(
    model_xbrl: Any, accession_number: str, source_document: str
) -> pd.DataFrame:
    records: list[dict[str, object]] = []

    for fact_index, fact in enumerate(model_xbrl.facts):
        concept = getattr(fact, "concept", None)

        if concept is None:
            continue

        context = fact.context
        unit = fact.unit
        period_fields = (
            _context_period_fields(context) if context is not None else {}
        )

        dims = (
            getattr(context, "qnameDims", {}) or {}
            if context is not None
            else {}
        )
        dimensions_json = json.dumps(
            {str(k): _dim_value_repr(v) for k, v in dims.items()},
            ensure_ascii=False,
        )

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

        records.append(
            {
                "accession_number": accession_number,
                "fact_index": fact_index,
                "concept_namespace": str(
                    getattr(concept.qname, "namespaceURI", "")
                ),
                "concept_local_name": str(
                    getattr(concept.qname, "localName", "")
                ),
                "concept_qname": str(concept.qname),
                "value_raw": value_raw,
                "value_numeric": value_numeric,
                "decimals": (
                    str(fact.decimals)
                    if getattr(fact, "decimals", None) is not None
                    else None
                ),
                "precision": (
                    str(fact.precision)
                    if getattr(fact, "precision", None) is not None
                    else None
                ),
                "unit_id": fact.unitID,
                "context_id": fact.contextID,
                "period_type": str(getattr(concept, "periodType", "")),
                "period_start": period_fields.get("period_start"),
                "period_end": period_fields.get("period_end"),
                "instant_date": period_fields.get("instant_date"),
                "dimensions_json": dimensions_json,
                "is_nil": is_nil,
                "source_document": source_document,
                "source_line": getattr(fact, "sourceline", None),
            }
        )

    return pd.DataFrame(records)


def extract_contexts(model_xbrl: Any, accession_number: str) -> pd.DataFrame:
    records: list[dict[str, object]] = []

    for context_id, context in model_xbrl.contexts.items():
        period_fields = _context_period_fields(context)

        entity_id_tuple = getattr(context, "entityIdentifier", None)
        entity_identifier = str(entity_id_tuple[1]) if entity_id_tuple else None

        seg_dims = getattr(context, "segDimValues", {}) or {}
        scen_dims = getattr(context, "scenDimValues", {}) or {}

        segment_json = json.dumps(
            {str(k): _dim_value_repr(v) for k, v in seg_dims.items()},
            ensure_ascii=False,
        )
        scenario_json = json.dumps(
            {str(k): _dim_value_repr(v) for k, v in scen_dims.items()},
            ensure_ascii=False,
        )

        all_dims = getattr(context, "qnameDims", {}) or {}
        dimensions_json = json.dumps(
            {str(k): _dim_value_repr(v) for k, v in all_dims.items()},
            ensure_ascii=False,
        )

        records.append(
            {
                "accession_number": accession_number,
                "context_id": str(context_id),
                "entity_identifier": entity_identifier,
                "period_start": period_fields.get("period_start"),
                "period_end": period_fields.get("period_end"),
                "instant_date": period_fields.get("instant_date"),
                "dimensions_json": dimensions_json,
                "scenario_json": scenario_json,
                "segment_json": segment_json,
            }
        )

    return pd.DataFrame(records)


def extract_units(model_xbrl: Any, accession_number: str) -> pd.DataFrame:
    records: list[dict[str, object]] = []

    for unit_id, unit in model_xbrl.units.items():
        measures = getattr(unit, "measures", None) or ((), ())
        numerator = measures[0] if len(measures) > 0 else ()
        denominator = measures[1] if len(measures) > 1 else ()

        records.append(
            {
                "accession_number": accession_number,
                "unit_id": str(unit_id),
                "numerator_measures": ",".join(str(m) for m in numerator)
                or None,
                "denominator_measures": ",".join(str(m) for m in denominator)
                or None,
            }
        )

    return pd.DataFrame(records)


def _walk_relationship_edges(
    relationship_set: Any,
    role_uri: str,
    concept: Any,
    edges: list[dict[str, object]],
    visited: set[tuple[str, str]],
    extra_arc_attrs: bool,
) -> None:
    concept_qname = str(getattr(concept, "qname", ""))
    relationships = relationship_set.fromModelObject(concept)

    for relationship in relationships:
        child = relationship.toModelObject

        if child is None:
            continue

        child_qname = str(getattr(child, "qname", ""))
        visit_key = (concept_qname, child_qname)

        edge: dict[str, object] = {
            "role_uri": role_uri,
            "parent_concept": concept_qname,
            "child_concept": child_qname,
            "order_value": float(getattr(relationship, "order", 0) or 0),
        }

        if extra_arc_attrs:
            edge["weight"] = float(getattr(relationship, "weight", 0) or 0)
        else:
            edge["preferred_label"] = str(
                getattr(relationship, "preferredLabel", "") or ""
            )

        edges.append(edge)

        if visit_key in visited:
            continue

        visited.add(visit_key)

        _walk_relationship_edges(
            relationship_set,
            role_uri,
            child,
            edges,
            visited,
            extra_arc_attrs,
        )


def extract_presentation_relationships(
    model_xbrl: Any, accession_number: str
) -> pd.DataFrame:
    from arelle import XbrlConst

    records: list[dict[str, object]] = []
    global_relationship_set = model_xbrl.relationshipSet(XbrlConst.parentChild)

    for role_uri in sorted(global_relationship_set.linkRoleUris):
        relationship_set = model_xbrl.relationshipSet(
            XbrlConst.parentChild, role_uri
        )
        role_definition = _role_definition(model_xbrl, role_uri)

        roots = sorted(
            relationship_set.rootConcepts,
            key=lambda concept: str(getattr(concept, "qname", "")),
        )

        for root in roots:
            edges: list[dict[str, object]] = []
            _walk_depth_first_presentation(
                relationship_set, role_uri, root, edges, set(), depth=0,
                parent_qname="",
            )

            for edge in edges:
                edge["accession_number"] = accession_number
                edge["role_definition"] = role_definition
                records.append(edge)

    return pd.DataFrame(records)


def _walk_depth_first_presentation(
    relationship_set: Any,
    role_uri: str,
    concept: Any,
    records: list[dict[str, object]],
    visited: set[tuple[str, str, int]],
    depth: int,
    parent_qname: str,
) -> None:
    concept_qname = str(getattr(concept, "qname", ""))
    visit_key = (parent_qname, concept_qname, depth)

    if visit_key in visited:
        return

    visited.add(visit_key)

    relationships = sorted(
        relationship_set.fromModelObject(concept),
        key=lambda relationship: (
            float(getattr(relationship, "order", 0) or 0),
            str(getattr(relationship.toModelObject, "qname", "")),
        ),
    )

    for relationship in relationships:
        child = relationship.toModelObject

        if child is None:
            continue

        child_qname = str(getattr(child, "qname", ""))

        records.append(
            {
                "role_uri": role_uri,
                "parent_concept": concept_qname,
                "child_concept": child_qname,
                "order_value": float(getattr(relationship, "order", 0) or 0),
                "preferred_label": str(
                    getattr(relationship, "preferredLabel", "") or ""
                ),
                "depth": depth + 1,
            }
        )

        _walk_depth_first_presentation(
            relationship_set,
            role_uri,
            child,
            records,
            visited,
            depth + 1,
            concept_qname,
        )


def extract_calculation_relationships(
    model_xbrl: Any, accession_number: str
) -> pd.DataFrame:
    from arelle import XbrlConst

    records: list[dict[str, object]] = []
    global_relationship_set = model_xbrl.relationshipSet(XbrlConst.summationItem)

    for role_uri in sorted(global_relationship_set.linkRoleUris):
        relationship_set = model_xbrl.relationshipSet(
            XbrlConst.summationItem, role_uri
        )
        role_definition = _role_definition(model_xbrl, role_uri)

        roots = sorted(
            relationship_set.rootConcepts,
            key=lambda concept: str(getattr(concept, "qname", "")),
        )

        for root in roots:
            edges: list[dict[str, object]] = []
            _walk_relationship_edges(
                relationship_set, role_uri, root, edges, set(),
                extra_arc_attrs=True,
            )

            for edge in edges:
                edge["accession_number"] = accession_number
                edge["role_definition"] = role_definition
                records.append(edge)

    return pd.DataFrame(records)


DEFINITION_ARCROLES: dict[str, str] = {}


def extract_definition_relationships(
    model_xbrl: Any, accession_number: str
) -> pd.DataFrame:
    from arelle import XbrlConst

    arcroles = {
        "all": XbrlConst.all,
        "notAll": XbrlConst.notAll,
        "hypercubeDimension": XbrlConst.hypercubeDimension,
        "dimensionDomain": XbrlConst.dimensionDomain,
        "domainMember": XbrlConst.domainMember,
        "dimensionDefault": XbrlConst.dimensionDefault,
    }

    records: list[dict[str, object]] = []

    for arcrole_name, arcrole_uri in arcroles.items():
        global_relationship_set = model_xbrl.relationshipSet(arcrole_uri)

        for role_uri in sorted(global_relationship_set.linkRoleUris):
            relationship_set = model_xbrl.relationshipSet(arcrole_uri, role_uri)
            role_definition = _role_definition(model_xbrl, role_uri)

            roots = sorted(
                relationship_set.rootConcepts,
                key=lambda concept: str(getattr(concept, "qname", "")),
            )

            for root in roots:
                relationships = relationship_set.fromModelObject(root)
                _extract_definition_edges_recursive(
                    relationship_set,
                    root,
                    records,
                    set(),
                    accession_number,
                    arcrole_name,
                    role_uri,
                    role_definition,
                )

    return pd.DataFrame(records)


def _extract_definition_edges_recursive(
    relationship_set: Any,
    concept: Any,
    records: list[dict[str, object]],
    visited: set[tuple[str, str]],
    accession_number: str,
    arcrole_name: str,
    role_uri: str,
    role_definition: str,
) -> None:
    concept_qname = str(getattr(concept, "qname", ""))
    relationships = relationship_set.fromModelObject(concept)

    for relationship in relationships:
        child = relationship.toModelObject

        if child is None:
            continue

        child_qname = str(getattr(child, "qname", ""))
        visit_key = (concept_qname, child_qname)

        records.append(
            {
                "accession_number": accession_number,
                "arcrole": arcrole_name,
                "role_uri": role_uri,
                "role_definition": role_definition,
                "parent_concept": concept_qname,
                "child_concept": child_qname,
                "order_value": float(getattr(relationship, "order", 0) or 0),
                "closed_attr": (
                    bool(relationship.closed)
                    if getattr(relationship, "closed", None) is not None
                    else None
                ),
                "context_element": getattr(relationship, "contextElement", None),
                "usable_attr": (
                    bool(relationship.usable)
                    if getattr(relationship, "usable", None) is not None
                    else None
                ),
            }
        )

        if visit_key in visited:
            continue

        visited.add(visit_key)

        _extract_definition_edges_recursive(
            relationship_set,
            child,
            records,
            visited,
            accession_number,
            arcrole_name,
            role_uri,
            role_definition,
        )


def referenced_concept_qnames(
    facts_df: pd.DataFrame,
    presentation_df: pd.DataFrame,
    calculation_df: pd.DataFrame,
    definition_df: pd.DataFrame,
) -> set[str]:
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


def extract_concepts(
    model_xbrl: Any, accession_number: str, qnames: set[str]
) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    qname_concepts = model_xbrl.qnameConcepts

    by_str = {str(qname): concept for qname, concept in qname_concepts.items()}

    for qname_str in sorted(qnames):
        concept = by_str.get(qname_str)

        if concept is None:
            continue

        namespace = str(getattr(concept.qname, "namespaceURI", ""))
        is_extension = not bool(
            re.search(STANDARD_NAMESPACE_PATTERN, namespace, re.IGNORECASE)
        )

        data_type = ""
        type_obj = getattr(concept, "type", None)

        if type_obj is not None:
            data_type = str(getattr(type_obj, "qname", "") or "")

        records.append(
            {
                "accession_number": accession_number,
                "qname": qname_str,
                "namespace": namespace,
                "local_name": str(getattr(concept.qname, "localName", "")),
                "data_type": data_type,
                "balance_type": str(getattr(concept, "balance", "") or ""),
                "period_type": str(getattr(concept, "periodType", "")),
                "is_abstract": bool(getattr(concept, "isAbstract", False)),
                "is_extension": is_extension,
            }
        )

    return pd.DataFrame(records)


def extract_labels(
    model_xbrl: Any, accession_number: str, qnames: set[str]
) -> pd.DataFrame:
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

            text = (
                getattr(label_resource, "textValue", None)
                or getattr(label_resource, "stringValue", None)
                or ""
            )

            records.append(
                {
                    "accession_number": accession_number,
                    "concept_qname": qname_str,
                    "label_role": str(getattr(label_resource, "role", "") or ""),
                    "language": str(
                        getattr(label_resource, "xmlLang", "") or ""
                    ),
                    "label_text": str(text),
                }
            )

    return pd.DataFrame(records)


def extract_roles(model_xbrl: Any, accession_number: str) -> pd.DataFrame:
    from arelle import XbrlConst

    records: list[dict[str, object]] = []

    arcrole_by_type = {
        "presentation": XbrlConst.parentChild,
        "calculation": XbrlConst.summationItem,
    }

    for relationship_type, arcrole in arcrole_by_type.items():
        relationship_set = model_xbrl.relationshipSet(arcrole)

        for role_uri in sorted(relationship_set.linkRoleUris):
            records.append(
                {
                    "accession_number": accession_number,
                    "role_uri": role_uri,
                    "role_definition": _role_definition(model_xbrl, role_uri),
                    "relationship_type": relationship_type,
                }
            )

    definition_arcroles = [
        XbrlConst.all,
        XbrlConst.notAll,
        XbrlConst.hypercubeDimension,
        XbrlConst.dimensionDomain,
        XbrlConst.domainMember,
        XbrlConst.dimensionDefault,
    ]
    definition_role_uris: set[str] = set()

    for arcrole in definition_arcroles:
        definition_role_uris |= set(
            model_xbrl.relationshipSet(arcrole).linkRoleUris
        )

    for role_uri in sorted(definition_role_uris):
        records.append(
            {
                "accession_number": accession_number,
                "role_uri": role_uri,
                "role_definition": _role_definition(model_xbrl, role_uri),
                "relationship_type": "definition",
            }
        )

    return pd.DataFrame(records)


# =============================================================================
# DuckDB schema + writing
# =============================================================================


def create_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS xbrl_facts (
            accession_number VARCHAR,
            fact_index INTEGER,
            concept_namespace VARCHAR,
            concept_local_name VARCHAR,
            concept_qname VARCHAR,
            value_raw VARCHAR,
            value_numeric DOUBLE,
            decimals VARCHAR,
            precision VARCHAR,
            unit_id VARCHAR,
            context_id VARCHAR,
            period_type VARCHAR,
            period_start VARCHAR,
            period_end VARCHAR,
            instant_date VARCHAR,
            dimensions_json VARCHAR,
            is_nil BOOLEAN,
            source_document VARCHAR,
            source_line INTEGER,
            PRIMARY KEY (accession_number, fact_index)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS xbrl_contexts (
            accession_number VARCHAR,
            context_id VARCHAR,
            entity_identifier VARCHAR,
            period_start VARCHAR,
            period_end VARCHAR,
            instant_date VARCHAR,
            dimensions_json VARCHAR,
            scenario_json VARCHAR,
            segment_json VARCHAR,
            PRIMARY KEY (accession_number, context_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS xbrl_units (
            accession_number VARCHAR,
            unit_id VARCHAR,
            numerator_measures VARCHAR,
            denominator_measures VARCHAR,
            PRIMARY KEY (accession_number, unit_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS xbrl_concepts (
            accession_number VARCHAR,
            qname VARCHAR,
            namespace VARCHAR,
            local_name VARCHAR,
            data_type VARCHAR,
            balance_type VARCHAR,
            period_type VARCHAR,
            is_abstract BOOLEAN,
            is_extension BOOLEAN,
            PRIMARY KEY (accession_number, qname)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS xbrl_labels (
            accession_number VARCHAR,
            concept_qname VARCHAR,
            label_role VARCHAR,
            language VARCHAR,
            label_text VARCHAR
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS xbrl_presentation_relationships (
            accession_number VARCHAR,
            role_uri VARCHAR,
            role_definition VARCHAR,
            parent_concept VARCHAR,
            child_concept VARCHAR,
            order_value DOUBLE,
            preferred_label VARCHAR,
            depth INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS xbrl_calculation_relationships (
            accession_number VARCHAR,
            role_uri VARCHAR,
            role_definition VARCHAR,
            parent_concept VARCHAR,
            child_concept VARCHAR,
            weight DOUBLE,
            order_value DOUBLE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS xbrl_definition_relationships (
            accession_number VARCHAR,
            arcrole VARCHAR,
            role_uri VARCHAR,
            role_definition VARCHAR,
            parent_concept VARCHAR,
            child_concept VARCHAR,
            order_value DOUBLE,
            closed_attr BOOLEAN,
            context_element VARCHAR,
            usable_attr BOOLEAN
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS xbrl_roles (
            accession_number VARCHAR,
            role_uri VARCHAR,
            role_definition VARCHAR,
            relationship_type VARCHAR,
            PRIMARY KEY (accession_number, role_uri, relationship_type)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS warehouse_runs (
            warehouse_run_id VARCHAR PRIMARY KEY,
            accession_number VARCHAR,
            ticker VARCHAR,
            report_date VARCHAR,
            parser_version VARCHAR,
            arelle_version VARCHAR,
            script_name VARCHAR,
            started_at_utc VARCHAR,
            completed_at_utc VARCHAR,
            local_filing_load_seconds DOUBLE,
            taxonomy_dts_and_parse_seconds DOUBLE,
            fact_extraction_seconds DOUBLE,
            relationship_extraction_seconds DOUBLE,
            duckdb_write_seconds DOUBLE,
            total_elapsed_seconds DOUBLE,
            row_counts_json VARCHAR,
            status VARCHAR
        )
        """
    )


def write_table(
    connection: duckdb.DuckDBPyConnection,
    table_name: str,
    df: pd.DataFrame,
    accession_number: str,
) -> int:
    """
    Idempotent per-accession write: deletes only THIS filing's own rows
    (scoped by accession_number) before inserting, never a blanket
    table wipe — the one deliberate change from scripts/73, required
    now that the warehouse is proven reusable across MULTIPLE filings
    (this script's whole purpose): AMZN's rows (and any other filing's)
    must survive re-running this script for MSFT/META/NVDA, and this
    script must itself be safely rerunnable for the same filing without
    duplicating rows.
    """

    connection.execute(
        f"DELETE FROM {table_name} WHERE accession_number = ?",
        [accession_number],
    )

    # A genuinely empty relationship set (e.g. MSFT's 2024 10-K has
    # ZERO calculation-linkbase edges anywhere — a real, filer-specific
    # fact, not an extraction bug) produces a DataFrame with zero
    # COLUMNS, not just zero rows (pd.DataFrame([]) has no columns at
    # all), which DuckDB's register() rejects outright. Nothing to
    # write in that case — the DELETE above already leaves this
    # filing's slice of the table empty, which is the correct, honest
    # representation of "this filing has no such relationships."
    if df.empty and len(df.columns) == 0:
        return 0

    connection.register("df_tmp", df)
    # BY NAME (not positional SELECT *): the DataFrame's column order
    # does not always match the table's declared column order (e.g.
    # presentation edges append accession_number/role_definition after
    # building the core edge dict) — matching by name is robust to that
    # regardless of construction order.
    connection.execute(
        f"INSERT INTO {table_name} BY NAME SELECT * FROM df_tmp"
    )
    connection.unregister("df_tmp")

    return int(len(df))


def already_loaded(
    connection: duckdb.DuckDBPyConnection, accession_number: str
) -> bool:
    """
    Resume-safety: never automatically re-parses a filing that already
    has a PASS warehouse_runs row from ANY prior warehouse-loading
    script (73, 75, or this one) — checked by accession_number alone,
    not script_name, since "do not reload filings already present in
    the warehouse" is about the WAREHOUSE's own state, not which
    script happened to populate it. AMZN 2024-12-31 was loaded by
    scripts/73; this check correctly skips it without reopening Arelle.
    """

    result = connection.execute(
        """
        SELECT count(*) FROM warehouse_runs
        WHERE accession_number = ? AND status = 'PASS'
        """,
        [accession_number],
    ).fetchone()

    return bool(result and result[0] > 0)


def load_one_filing(ticker: str, report_date: str) -> dict[str, object]:
    from arelle.RuntimeOptions import RuntimeOptions
    from arelle.api.Session import Session
    from arelle import Version as ArelleVersion

    total_start = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc).isoformat()

    print(f"טוען Manifest נעול עבור {ticker} / {report_date}...")
    locked_filing = load_locked_filing(ticker, report_date)
    accession_number = locked_filing["accession_number"]
    primary_document_path = locked_filing["primary_document_path"]
    source_document = locked_filing["primary_document_name"]

    print(f"Accession: {accession_number}")
    print(f"קובץ: {primary_document_path}")

    WAREHOUSE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    resume_check_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH))
    create_schema(resume_check_connection)

    if already_loaded(resume_check_connection, accession_number):
        print(
            f"כבר נטען בהצלחה בעבר (warehouse_runs status=PASS) — לא "
            f"נפתח Arelle מחדש עבור {ticker} / {report_date}."
        )
        resume_check_connection.close()

        return {
            "ticker": ticker,
            "report_date": report_date,
            "accession_number": accession_number,
            "status": "SKIPPED_ALREADY_LOADED",
        }

    resume_check_connection.close()

    # --- "local filing load": raw disk I/O of the primary document only
    # (a floor/proxy measurement — see module docstring for why this
    # can't be split further from DTS loading via Arelle's public API).
    local_load_start = time.perf_counter()
    _ = primary_document_path.read_bytes()
    local_filing_load_seconds = time.perf_counter() - local_load_start
    print(f"local_filing_load_seconds = {local_filing_load_seconds:.6f}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    options = RuntimeOptions(
        entrypointFile=str(primary_document_path),
        internetConnectivity="online",
        cacheDirectory=str(CACHE_DIR),
        internetTimeout=INTERNET_TIMEOUT_SECONDS,
        httpUserAgent=locked_filing["sec_user_agent"],
        keepOpen=True,
        logFile=str(
            DATA_DIR / "database" / "xbrl_warehouse_proof_arelle.log"
        ),
        logFormat=(
            "[%(levelname)s] [%(messageCode)s] %(message)s - %(file)s"
        ),
    )

    DATA_DIR.joinpath("database").mkdir(parents=True, exist_ok=True)

    dts_parse_start = time.perf_counter()

    row_counts: dict[str, int] = {}
    status = "FAIL"

    with Session() as session:
        session.run(options)
        taxonomy_dts_and_parse_seconds = time.perf_counter() - dts_parse_start
        print(
            "taxonomy_dts_and_parse_seconds (Arelle session.run, instance "
            f"parse + DTS load combined) = {taxonomy_dts_and_parse_seconds:.6f}"
        )

        models = session.get_models()

        if len(models) != 1:
            raise RuntimeError(f"Arelle לא החזיר מודל יחיד וברור. מספר מודלים: {len(models)}")

        model_xbrl = models[0]

        if model_xbrl is None:
            raise RuntimeError("Arelle לא הצליח לטעון את מודל ה-XBRL.")

        # --- fact extraction (facts + their contexts + units)
        fact_extraction_start = time.perf_counter()

        facts_df = extract_facts(model_xbrl, accession_number, source_document)
        contexts_df = extract_contexts(model_xbrl, accession_number)
        units_df = extract_units(model_xbrl, accession_number)

        fact_extraction_seconds = time.perf_counter() - fact_extraction_start
        print(
            f"fact_extraction_seconds = {fact_extraction_seconds:.6f} "
            f"({len(facts_df)} facts, {len(contexts_df)} contexts, "
            f"{len(units_df)} units)"
        )

        # --- relationship + structure extraction
        relationship_extraction_start = time.perf_counter()

        presentation_df = extract_presentation_relationships(
            model_xbrl, accession_number
        )
        calculation_df = extract_calculation_relationships(
            model_xbrl, accession_number
        )
        definition_df = extract_definition_relationships(
            model_xbrl, accession_number
        )
        roles_df = extract_roles(model_xbrl, accession_number)

        qnames = referenced_concept_qnames(
            facts_df, presentation_df, calculation_df, definition_df
        )
        concepts_df = extract_concepts(model_xbrl, accession_number, qnames)
        labels_df = extract_labels(model_xbrl, accession_number, qnames)

        relationship_extraction_seconds = (
            time.perf_counter() - relationship_extraction_start
        )
        print(
            f"relationship_extraction_seconds = "
            f"{relationship_extraction_seconds:.6f} "
            f"({len(presentation_df)} presentation edges, "
            f"{len(calculation_df)} calculation edges, "
            f"{len(definition_df)} definition edges, {len(roles_df)} roles, "
            f"{len(concepts_df)} referenced concepts, {len(labels_df)} labels)"
        )

        # --- DuckDB writing (per requirement 3: warehouse populated
        # BEFORE Arelle is closed — the `with Session()` block below
        # only exits, closing Arelle, once writing is complete).
        duckdb_write_start = time.perf_counter()

        connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH))
        create_schema(connection)

        row_counts["xbrl_facts"] = write_table(
            connection, "xbrl_facts", facts_df, accession_number
        )
        row_counts["xbrl_contexts"] = write_table(
            connection, "xbrl_contexts", contexts_df, accession_number
        )
        row_counts["xbrl_units"] = write_table(
            connection, "xbrl_units", units_df, accession_number
        )
        row_counts["xbrl_concepts"] = write_table(
            connection, "xbrl_concepts", concepts_df, accession_number
        )
        row_counts["xbrl_labels"] = write_table(
            connection, "xbrl_labels", labels_df, accession_number
        )
        row_counts["xbrl_presentation_relationships"] = write_table(
            connection,
            "xbrl_presentation_relationships",
            presentation_df,
            accession_number,
        )
        row_counts["xbrl_calculation_relationships"] = write_table(
            connection,
            "xbrl_calculation_relationships",
            calculation_df,
            accession_number,
        )
        row_counts["xbrl_definition_relationships"] = write_table(
            connection,
            "xbrl_definition_relationships",
            definition_df,
            accession_number,
        )
        row_counts["xbrl_roles"] = write_table(
            connection, "xbrl_roles", roles_df, accession_number
        )

        duckdb_write_seconds = time.perf_counter() - duckdb_write_start
        print(f"duckdb_write_seconds = {duckdb_write_seconds:.6f}")

        status = "PASS"

        # `with Session()` exits here -> Arelle model/session closed,
        # AFTER the warehouse has already been fully populated above.

    completed_at_utc = datetime.now(timezone.utc).isoformat()
    total_elapsed_seconds = time.perf_counter() - total_start

    warehouse_run_id = (
        f"{accession_number}::{SCRIPT_NAME}::{started_at_utc}"
    )
    timings = {
        "local_filing_load_seconds": round(local_filing_load_seconds, 6),
        "taxonomy_dts_and_parse_seconds": round(
            taxonomy_dts_and_parse_seconds, 6
        ),
        "fact_extraction_seconds": round(fact_extraction_seconds, 6),
        "relationship_extraction_seconds": round(
            relationship_extraction_seconds, 6
        ),
        "duckdb_write_seconds": round(duckdb_write_seconds, 6),
        "total_elapsed_seconds": round(total_elapsed_seconds, 6),
    }
    connection.execute(
        """
        INSERT INTO warehouse_runs VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            warehouse_run_id,
            accession_number,
            ticker,
            report_date,
            "generic-xbrl-warehouse-proof-v1",
            ArelleVersion.getVersion(),
            SCRIPT_NAME,
            started_at_utc,
            completed_at_utc,
            timings["local_filing_load_seconds"],
            timings["taxonomy_dts_and_parse_seconds"],
            timings["fact_extraction_seconds"],
            timings["relationship_extraction_seconds"],
            timings["duckdb_write_seconds"],
            timings["total_elapsed_seconds"],
            json.dumps(row_counts),
            status,
        ],
    )
    connection.close()

    print()
    print(f"--- {ticker} {report_date} WAREHOUSE LOAD COMPLETE — status={status} ---")
    print(f"row_counts: {json.dumps(row_counts)}")
    print(f"timings: {json.dumps(timings)}")

    return {
        "ticker": ticker,
        "report_date": report_date,
        "accession_number": accession_number,
        "status": status,
        "row_counts": row_counts,
        "timings": timings,
    }


def main() -> None:
    print(f"מסד נתונים (מצטבר, לא נמחק בין הרצות): {WAREHOUSE_DB_PATH}")
    print(f"קבצים למעבר: {TARGET_FILINGS}")
    print()

    summaries: list[dict[str, object]] = []

    for ticker, report_date in TARGET_FILINGS:
        summary = load_one_filing(ticker, report_date)
        summaries.append(summary)
        print()

    print("=" * 100)
    print("MULTI-FILING WAREHOUSE GENERALIZATION LOAD — SUMMARY")
    for summary in summaries:
        print(
            f"  {summary['ticker']:6s} {summary['report_date']:12s} "
            f"accession={summary['accession_number']} "
            f"status={summary['status']}"
        )
    print("=" * 100)


if __name__ == "__main__":
    main()
