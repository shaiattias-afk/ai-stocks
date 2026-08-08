"""
Bounded proof of a reusable, raw, structured XBRL warehouse — parse one
already accession-locked 10-K with Arelle exactly ONCE, and preserve the
complete parsed XBRL layer (facts, contexts, units, concepts, labels,
presentation/calculation/definition relationships, roles) in a
dedicated proof DuckDB database, so that downstream semantic analysis
(e.g. classifying current_debt from a debt-maturity schedule) can be
rerun entirely from stored data, without ever reopening Arelle.

Architecture distinction (binding for this proof, per the user's
explicit instruction):
  - The original SEC filing package on disk remains the immutable raw
    source — never modified, never touched by this script beyond a
    read-only open.
  - This warehouse stores the COMPLETE parsed XBRL facts and
    relationships — not a canonical-metric result, a raw structured
    layer one level below any accounting-policy interpretation.
  - Canonical metrics (current_debt, etc.) remain versioned outputs
    derived FROM this warehouse by a separate, later step — this proof
    does not compute or change any canonical metric.

Scope of this proof (per the user's explicit instruction): ONE already
locked filing (AMZN, report date 2024-12-31), written to a SEPARATE
proof database (data/database/xbrl_warehouse_proof.duckdb) — the
production database (data/database/ai_stock_agent.duckdb) is never
opened by this script. Not scaled to other filings; no ticker- or
year-specific mapping rule anywhere below (every extraction function
here is generic — it would run unchanged against any other 10-K).

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

SCRIPT_NAME = "73_build_xbrl_warehouse_proof.py"
EXPECTED_FORM = "10-K"

TARGET_TICKER = "AMZN"
TARGET_REPORT_DATE = "2024-12-31"

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
        CREATE OR REPLACE TABLE xbrl_facts (
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
        CREATE OR REPLACE TABLE xbrl_contexts (
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
        CREATE OR REPLACE TABLE xbrl_units (
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
        CREATE OR REPLACE TABLE xbrl_concepts (
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
        CREATE OR REPLACE TABLE xbrl_labels (
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
        CREATE OR REPLACE TABLE xbrl_presentation_relationships (
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
        CREATE OR REPLACE TABLE xbrl_calculation_relationships (
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
        CREATE OR REPLACE TABLE xbrl_definition_relationships (
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
        CREATE OR REPLACE TABLE xbrl_roles (
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
    connection: duckdb.DuckDBPyConnection, table_name: str, df: pd.DataFrame
) -> int:
    connection.register("df_tmp", df)
    connection.execute(f"DELETE FROM {table_name}")
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


def main() -> None:
    from arelle.RuntimeOptions import RuntimeOptions
    from arelle.api.Session import Session
    from arelle import Version as ArelleVersion

    total_start = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc).isoformat()

    print(f"טוען Manifest נעול עבור {TARGET_TICKER} / {TARGET_REPORT_DATE}...")
    locked_filing = load_locked_filing(TARGET_TICKER, TARGET_REPORT_DATE)
    accession_number = locked_filing["accession_number"]
    primary_document_path = locked_filing["primary_document_path"]
    source_document = locked_filing["primary_document_name"]

    print(f"Accession: {accession_number}")
    print(f"קובץ: {primary_document_path}")

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

        WAREHOUSE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH))
        create_schema(connection)

        row_counts["xbrl_facts"] = write_table(connection, "xbrl_facts", facts_df)
        row_counts["xbrl_contexts"] = write_table(
            connection, "xbrl_contexts", contexts_df
        )
        row_counts["xbrl_units"] = write_table(connection, "xbrl_units", units_df)
        row_counts["xbrl_concepts"] = write_table(
            connection, "xbrl_concepts", concepts_df
        )
        row_counts["xbrl_labels"] = write_table(connection, "xbrl_labels", labels_df)
        row_counts["xbrl_presentation_relationships"] = write_table(
            connection, "xbrl_presentation_relationships", presentation_df
        )
        row_counts["xbrl_calculation_relationships"] = write_table(
            connection, "xbrl_calculation_relationships", calculation_df
        )
        row_counts["xbrl_definition_relationships"] = write_table(
            connection, "xbrl_definition_relationships", definition_df
        )
        row_counts["xbrl_roles"] = write_table(connection, "xbrl_roles", roles_df)

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
    connection.execute(
        """
        INSERT INTO warehouse_runs VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            warehouse_run_id,
            accession_number,
            TARGET_TICKER,
            TARGET_REPORT_DATE,
            "generic-xbrl-warehouse-proof-v1",
            ArelleVersion.getVersion(),
            SCRIPT_NAME,
            started_at_utc,
            completed_at_utc,
            round(local_filing_load_seconds, 6),
            round(taxonomy_dts_and_parse_seconds, 6),
            round(fact_extraction_seconds, 6),
            round(relationship_extraction_seconds, 6),
            round(duckdb_write_seconds, 6),
            round(total_elapsed_seconds, 6),
            json.dumps(row_counts),
            status,
        ],
    )
    connection.close()

    print()
    print("=" * 100)
    print(f"WAREHOUSE PROOF COMPLETE — status={status}")
    print(f"DB: {WAREHOUSE_DB_PATH}")
    print(f"row_counts: {json.dumps(row_counts, indent=2)}")
    print(f"total_elapsed_seconds = {total_elapsed_seconds:.6f}")
    print("=" * 100)


if __name__ == "__main__":
    main()
