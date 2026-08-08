"""warehouse/extract.py — the pure Arelle-model -> pandas.DataFrame
extraction primitives, plus the warehouse schema and table writer.

Ported byte-exact (logic unchanged) from
`scripts/121_quarterly_batch_runner.py`, which until now was reached
through an `importlib.spec_from_file_location` bridge from inside the
package itself (`warehouse/loader.py`). That bridge is the last remaining
instance of the file-path import hack this package exists to remove; these
functions live here so every consumer can use a normal package import.

Nothing here touches the network, the filesystem, or any hardcoded
database path: each function takes an already-loaded Arelle `model_xbrl`
and an `accession_number`, and returns a DataFrame. The XBRL
exclusive-end-date convention (both duration end dates and instant dates
are stored as the filer-facing date, i.e. Arelle's value minus one day) is
applied here exactly as it always has been -- see `_context_period_fields`.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

import duckdb
import pandas as pd

STANDARD_NAMESPACE_PATTERN = r"fasb\.org|xbrl\.sec\.gov|xbrl\.org|w3\.org"

WAREHOUSE_TABLES = [
    "xbrl_facts", "xbrl_contexts", "xbrl_units", "xbrl_concepts", "xbrl_labels",
    "xbrl_presentation_relationships", "xbrl_calculation_relationships",
    "xbrl_definition_relationships", "xbrl_roles",
]


# =============================================================================
# Shared helpers
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
    return {"period_start": period_start, "period_end": period_end, "instant_date": instant_date}


# =============================================================================
# Facts, contexts, units
# =============================================================================

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


# =============================================================================
# Relationships (presentation / calculation / definition)
# =============================================================================

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


# =============================================================================
# Concepts, labels, roles
# =============================================================================

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


# =============================================================================
# Warehouse schema + writer
# =============================================================================

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


def extract_all(model_xbrl: Any, accession_number: str, source_document: str) -> dict[str, pd.DataFrame]:
    """Runs every extractor against one loaded model and returns the nine
    warehouse DataFrames keyed by table name. Concepts and labels are
    restricted to the qnames actually referenced by the facts and the
    relationship trees, exactly as the original loader did."""
    facts_df = extract_facts(model_xbrl, accession_number, source_document)
    contexts_df = extract_contexts(model_xbrl, accession_number)
    units_df = extract_units(model_xbrl, accession_number)
    presentation_df = extract_presentation_relationships(model_xbrl, accession_number)
    calculation_df = extract_calculation_relationships(model_xbrl, accession_number)
    definition_df = extract_definition_relationships(model_xbrl, accession_number)
    roles_df = extract_roles(model_xbrl, accession_number)
    qnames = referenced_concept_qnames(facts_df, presentation_df, calculation_df, definition_df)
    concepts_df = extract_concepts(model_xbrl, accession_number, qnames)
    labels_df = extract_labels(model_xbrl, accession_number, qnames)
    return {
        "xbrl_facts": facts_df, "xbrl_contexts": contexts_df, "xbrl_units": units_df,
        "xbrl_concepts": concepts_df, "xbrl_labels": labels_df,
        "xbrl_presentation_relationships": presentation_df,
        "xbrl_calculation_relationships": calculation_df,
        "xbrl_definition_relationships": definition_df, "xbrl_roles": roles_df,
    }
