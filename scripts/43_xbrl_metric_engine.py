from __future__ import annotations

import argparse
import json
import multiprocessing
import re
import time
import traceback
from dataclasses import dataclass
from datetime import timedelta, timezone, datetime
from pathlib import Path
from typing import Any

import pandas as pd


# =============================================================================
# Generic, shared, ticker-agnostic XBRL statement-first metric engine.
#
# Consolidates the logic already proven separately in scripts 37c, 37d,
# 38, 39, 40, and 41 into one reusable engine, organized into clearly
# separated concerns (per explicit instruction):
#   1. FILING LOCK LOADING       - locate the locked 10-K manifest
#   2. ARELLE SESSION LOADING    - bounded, child-process-safe model load
#   3. STATEMENT ROLE ID         - find the primary statement role
#   4. CANONICAL ROW ID          - find the target line item within it
#   5. FACT MATCHING             - filter facts by context/period/unit/
#                                  dimensions/entity
#   6. DEDUPLICATION + STATUS    - collapse technical duplicates, decide
#                                  PASS / REVIEW_REQUIRED
#   7. BOUNDED ORCHESTRATION     - child process + timeouts + TIMEOUT
#
# No ticker-specific rule exists anywhere in this file. No manual concept
# tag list is used as the primary selection mechanism — concepts are
# always derived from the filing's own presentation structure (D-007).
#
# v2 (this file, 42 unmodified): fixes operating_income's label rule,
# which only recognized "Operating income"/"Operating loss" (Oracle,
# Microsoft). Meta's 10-K labels the same us-gaap:OperatingIncomeLoss
# concept "Income (loss) from operations" — a different, equally common,
# ticker-agnostic SEC-filer convention, not a Meta-specific quirk. The
# fix broadens the pattern to recognize both phrasings; nothing else
# changed.
# =============================================================================


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "arelle_cache"

EXPECTED_FORM = "10-K"

# Same bounding values already verified as safe (non-hanging) across all
# prior proofs (scripts 37c through 42).
TOTAL_TIMEOUT_SECONDS = 240
INTERNET_TIMEOUT_SECONDS = 20
TERMINATE_GRACE_SECONDS = 5

# A duration context is accepted as "annual" only within this tolerance,
# instead of assuming any single company's specific fiscal-year length.
ANNUAL_DURATION_MIN_DAYS = 350
ANNUAL_DURATION_MAX_DAYS = 380


# =============================================================================
# Metric definitions — declarative, structural/label rules only. Adding a
# new metric means adding an entry here, never a ticker branch and never
# a hard-coded concept name.
# =============================================================================


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    # Statement-type role must match this pattern in its title...
    role_include_pattern: str
    # ...and must NOT match this pattern (None = no exclusion).
    role_exclude_pattern: str | None
    # A non-abstract row's label must contain this pattern to be a
    # candidate at all.
    mention_pattern: str
    # Candidate rows whose label matches this pattern are dropped
    # (derived figures such as per-share or weighted-average-share rows).
    exclude_label_pattern: str | None
    # Tier A: preferred when it resolves to exactly one row (e.g. the
    # amount explicitly attributable to common/parent shareholders).
    attributable_pattern: str | None
    # Tier B: fallback plain/unqualified line-item label, anchored.
    plain_pattern: str


BUILT_IN_METRICS: dict[str, MetricDefinition] = {
    "revenue": MetricDefinition(
        name="revenue",
        role_include_pattern=r"operations|income",
        role_exclude_pattern=r"comprehensive",
        mention_pattern=r"revenues?",
        exclude_label_pattern=None,
        attributable_pattern=None,
        plain_pattern=r"^\s*(?:total\s+)?revenues?\s*$",
    ),
    "net_income": MetricDefinition(
        name="net_income",
        role_include_pattern=r"operations|income",
        role_exclude_pattern=r"comprehensive",
        mention_pattern=r"net\s+(?:income|loss)",
        exclude_label_pattern=r"per\s+share|weighted\s+average",
        attributable_pattern=(
            r"attributable\s+to.*(?:common|stockholders|shareholders|"
            r"corporation|company|\binc\.?\b|\bcorp\.?\b)"
        ),
        plain_pattern=r"^\s*net\s+(?:income\s*\(loss\)|income|loss)\s*$",
    ),
    "operating_income": MetricDefinition(
        name="operating_income",
        role_include_pattern=r"operations|income",
        role_exclude_pattern=r"comprehensive",
        # Two equally common, ticker-agnostic SEC-filer phrasings for the
        # same us-gaap:OperatingIncomeLoss concept: "Operating income/
        # loss" (e.g. Oracle, Microsoft) and "Income/loss from
        # operations" (e.g. Meta).
        mention_pattern=(
            r"operating\s+(?:income|loss)|"
            r"(?:income|loss)\s*(?:\(loss\)|\(income\))?\s*from\s+operations"
        ),
        exclude_label_pattern=r"per\s+share|weighted\s+average|margin|percentage",
        attributable_pattern=None,
        plain_pattern=(
            r"^\s*(?:"
            r"operating\s+(?:income\s*\(loss\)|income|loss)"
            r"|(?:income|loss)\s*(?:\(loss\)|\(income\))?\s*from\s+operations"
            r")\s*$"
        ),
    ),
}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generic, ticker-agnostic statement-first XBRL metric "
            "engine. Extracts one or more built-in metrics from an "
            "already locked 10-K using Arelle presentation structure "
            "only — no per-company rule, no manual concept tag list as "
            "primary mechanism."
        )
    )

    parser.add_argument("--ticker", required=True, help="e.g. ORCL, MSFT.")
    parser.add_argument(
        "--report-date",
        required=True,
        help="Fiscal report date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--metrics",
        nargs="+",
        default=list(BUILT_IN_METRICS.keys()),
        choices=list(BUILT_IN_METRICS.keys()),
        help="Which built-in metrics to extract in this run.",
    )

    return parser.parse_args()


def output_paths(ticker: str, report_date: str) -> dict[str, Path]:
    prefix = f"{ticker.lower()}_{report_date.replace('-', '')}"

    return {
        "presentation_csv": (
            DATA_DIR / f"{prefix}_engine_v2_presentation.csv"
        ),
        "row_candidates_csv": (
            DATA_DIR / f"{prefix}_engine_v2_row_candidates.csv"
        ),
        "fact_candidates_csv": (
            DATA_DIR / f"{prefix}_engine_v2_fact_candidates.csv"
        ),
        "result_file": DATA_DIR / f"{prefix}_engine_v2_result.json",
        "arelle_log_file": (
            DATA_DIR / f"{prefix}_engine_v2_arelle_child.log"
        ),
        "orchestration_log_file": (
            DATA_DIR / f"{prefix}_engine_v2_orchestration.log"
        ),
    }


def log_line(orchestration_log_file: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"[{timestamp}] {message}"

    print(line)

    with orchestration_log_file.open(
        mode="a",
        encoding="utf-8",
    ) as log_file:
        log_file.write(line + "\n")


# =============================================================================
# 1. FILING LOCK LOADING
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
            f"{ticker} / {report_date}.\n"
            f"מספר התאמות: {len(matching_manifests)}\n"
            "יש לנעול את ההגשה תחילה עם "
            "36b_download_accession_locked_filing.py."
        )

    manifest_file, manifest = matching_manifests[0]

    primary_document_path = Path(
        manifest["primary_document_path"]
    ).resolve()

    if not primary_document_path.exists():
        raise FileNotFoundError(
            f"קובץ ה-10-K הראשי לא נמצא:\n{primary_document_path}"
        )

    sec_user_agent = str(manifest.get("sec_user_agent", "")).strip()

    if not sec_user_agent:
        raise RuntimeError("לא נמצא sec_user_agent ב-Manifest הנעול.")

    cik = manifest.get("cik")

    if not cik:
        raise RuntimeError("לא נמצא cik ב-Manifest הנעול.")

    return {
        "manifest_file": manifest_file,
        "primary_document_path": primary_document_path,
        "accession_number": manifest.get("accession_number"),
        "accession_compact": str(
            manifest.get("accession_number", "")
        ).replace("-", ""),
        "report_date": manifest.get("report_date"),
        "filing_date": manifest.get("filing_date"),
        "sec_user_agent": sec_user_agent,
        "cik": int(cik),
        "ticker": manifest.get("ticker", ticker.upper()),
        "company_name": manifest.get("company_name", ""),
        "primary_document_name": manifest.get("primary_document"),
    }


def find_qa_reference_value(
    ticker: str,
    report_date: str,
    metric_name: str,
) -> dict[str, object] | None:
    """
    Best-effort QA-only lookup of a figure already computed by an earlier,
    independent pipeline in this project, if one happens to exist for
    this exact ticker/period/metric. Never used to select or validate the
    extracted fact — only reported for human comparison.
    """

    candidate_file = DATA_DIR / f"{ticker.lower()}_{metric_name}_test.csv"

    if not candidate_file.exists():
        return {
            "source_file": None,
            "note": (
                f"לא נמצא קובץ QA עצמאי עבור {metric_name} של "
                f"{ticker.upper()} לתקופה זו — אין נתון להשוואה."
            ),
        }

    try:
        existing = pd.read_csv(candidate_file, dtype=str)
    except Exception:
        return None

    if "period_end" not in existing.columns:
        return None

    matching_rows = existing[existing["period_end"] == report_date]

    if len(matching_rows) != 1:
        return None

    row = matching_rows.iloc[0]

    return {
        "source_file": str(candidate_file),
        "row": row.to_dict(),
    }


# =============================================================================
# 2. ARELLE SESSION LOADING + full presentation walk (statement-agnostic)
# =============================================================================


def _safe_label(concept: Any, preferred_label: str | None = None) -> str:
    try:
        label = concept.label(
            preferredLabel=preferred_label,
            lang="en-US",
            fallbackToQname=True,
        )

        if label:
            return str(label)
    except Exception:
        pass

    try:
        label = concept.label(lang="en-US", fallbackToQname=True)

        if label:
            return str(label)
    except Exception:
        pass

    return str(getattr(concept, "qname", ""))


def _role_definition(model_xbrl: Any, role_uri: str) -> str:
    role_types = model_xbrl.roleTypes.get(role_uri, [])

    for role_type in role_types:
        definition = getattr(role_type, "definition", "")

        if definition:
            return str(definition)

    return ""


def _walk_tree(
    relationship_set: Any,
    role_uri: str,
    role_name: str,
    concept: Any,
    records: list[dict[str, object]],
    depth: int,
    parent_qname: str,
    preferred_label: str,
    visited: set[tuple[str, str, int]],
) -> None:
    concept_qname = str(getattr(concept, "qname", ""))
    visit_key = (parent_qname, concept_qname, depth)

    if visit_key in visited:
        return

    visited.add(visit_key)

    records.append(
        {
            "role_uri": role_uri,
            "role_definition": role_name,
            "depth": depth,
            "parent_qname": parent_qname,
            "concept_qname": concept_qname,
            "label": _safe_label(concept, preferred_label or None),
            "is_abstract": bool(getattr(concept, "isAbstract", False)),
            "period_type": str(getattr(concept, "periodType", "")),
            "balance": str(getattr(concept, "balance", "") or ""),
        }
    )

    relationships = relationship_set.fromModelObject(concept)

    relationships = sorted(
        relationships,
        key=lambda relationship: (
            float(getattr(relationship, "order", 0) or 0),
            str(getattr(relationship.toModelObject, "qname", "")),
        ),
    )

    for relationship in relationships:
        child = relationship.toModelObject

        if child is None:
            continue

        child_preferred_label = str(
            getattr(relationship, "preferredLabel", "") or ""
        )

        _walk_tree(
            relationship_set=relationship_set,
            role_uri=role_uri,
            role_name=role_name,
            concept=child,
            records=records,
            depth=depth + 1,
            parent_qname=concept_qname,
            preferred_label=child_preferred_label,
            visited=visited,
        )


def extract_presentation(model_xbrl: Any) -> pd.DataFrame:
    from arelle import XbrlConst

    records: list[dict[str, object]] = []

    global_relationship_set = model_xbrl.relationshipSet(
        XbrlConst.parentChild
    )

    for role_uri in sorted(global_relationship_set.linkRoleUris):
        relationship_set = model_xbrl.relationshipSet(
            XbrlConst.parentChild,
            role_uri,
        )

        definition = _role_definition(model_xbrl, role_uri)

        roots = sorted(
            relationship_set.rootConcepts,
            key=lambda concept: str(getattr(concept, "qname", "")),
        )

        for root in roots:
            _walk_tree(
                relationship_set=relationship_set,
                role_uri=role_uri,
                role_name=definition,
                concept=root,
                records=records,
                depth=0,
                parent_qname="",
                preferred_label="",
                visited=set(),
            )

    return pd.DataFrame(records)


# =============================================================================
# 3 + 4. STATEMENT ROLE IDENTIFICATION + CANONICAL ROW IDENTIFICATION
# =============================================================================


class TargetRowNotFound(Exception):
    """
    Raised when the presentation structure does not resolve to exactly
    one unambiguous row for a metric. Distinguished from other exceptions
    so the caller can report REVIEW_REQUIRED (insufficient evidence)
    instead of FAIL (execution error).
    """


def identify_canonical_row(
    presentation: pd.DataFrame,
    metric: MetricDefinition,
) -> tuple[dict[str, str], pd.DataFrame]:
    """
    Structure-first row selection, generic across metrics and tickers:

    1. STATEMENT ROLE IDENTIFICATION — restrict to a top-level
       Statement-type role whose title matches `role_include_pattern`
       and does not match `role_exclude_pattern` (e.g. excluding a
       "Comprehensive Income" statement, a distinct, universally
       recognized statement type across SEC filers, not specific to any
       one issuer — this is what previously caused a genuine two-
       candidate ambiguity for net income on Oracle, script 40).
    2. CANONICAL ROW IDENTIFICATION — within that role, collect
       non-abstract rows whose label matches `mention_pattern` and does
       not match `exclude_label_pattern`, then apply a two-tier decision:
       Tier A (`attributable_pattern`) — preferred when it resolves to
       exactly one row (correspondence to shareholders when a
       noncontrolling-interest breakout exists); Tier B (`plain_pattern`)
       — a single unqualified line-item label otherwise.

    Whichever concept the filer's own presentation attaches to the
    selected row is what gets used — never a concept chosen by this code.
    """

    is_statement_role = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-",
        na=False,
    )

    is_role_include = presentation["role_definition"].str.contains(
        metric.role_include_pattern,
        case=False,
        regex=True,
        na=False,
    )

    if metric.role_exclude_pattern:
        is_role_exclude = presentation["role_definition"].str.contains(
            metric.role_exclude_pattern,
            case=False,
            regex=True,
            na=False,
        )
    else:
        is_role_exclude = pd.Series(False, index=presentation.index)

    is_target_role = is_statement_role & is_role_include & ~is_role_exclude

    is_not_abstract = ~presentation["is_abstract"].astype(bool)

    mentions_metric = presentation["label"].str.contains(
        metric.mention_pattern,
        case=False,
        regex=True,
        na=False,
    )

    if metric.exclude_label_pattern:
        is_excluded_label = presentation["label"].str.contains(
            metric.exclude_label_pattern,
            case=False,
            regex=True,
            na=False,
        )
    else:
        is_excluded_label = pd.Series(False, index=presentation.index)

    base_candidates = presentation[
        is_target_role
        & is_not_abstract
        & mentions_metric
        & ~is_excluded_label
    ].copy()

    if base_candidates.empty:
        raise TargetRowNotFound(
            f"לא נמצאה אף שורת '{metric.name}' בתוך Statement role ראשי "
            "התואם לכללי ה-Role של המדד."
        )

    if metric.attributable_pattern:
        is_tier_a = base_candidates["label"].str.contains(
            metric.attributable_pattern,
            case=False,
            regex=True,
            na=False,
        )

        tier_a = base_candidates[is_tier_a]

        if len(tier_a) == 1:
            row = tier_a.iloc[0]

            return (
                {
                    "role_uri": str(row["role_uri"]),
                    "role_definition": str(row["role_definition"]),
                    "concept_qname": str(row["concept_qname"]),
                    "label": str(row["label"]),
                    "period_type": str(row["period_type"]),
                    "selection_tier": "attributable_to_shareholders",
                },
                base_candidates,
            )
    else:
        tier_a = base_candidates.iloc[0:0]

    is_tier_b = base_candidates["label"].str.match(
        metric.plain_pattern,
        case=False,
        na=False,
    )

    tier_b = base_candidates[is_tier_b]

    if len(tier_b) == 1:
        row = tier_b.iloc[0]

        return (
            {
                "role_uri": str(row["role_uri"]),
                "role_definition": str(row["role_definition"]),
                "concept_qname": str(row["concept_qname"]),
                "label": str(row["label"]),
                "period_type": str(row["period_type"]),
                "selection_tier": "plain",
            },
            base_candidates,
        )

    raise TargetRowNotFound(
        f"לא ניתן לזהות שורת '{metric.name}' יחידה וחד-משמעית.\n"
        f"מספר שורות מועמדות כוללות: {len(base_candidates)}, "
        f"מתוכן 'attributable': {len(tier_a)}, 'plain': {len(tier_b)}"
    )


# =============================================================================
# 5. FACT MATCHING — context / period / unit / dimensions / entity
# =============================================================================


def match_facts(
    model_xbrl: Any,
    target_concept_qname: str,
    expected_cik: int,
    report_date: str,
) -> pd.DataFrame:
    expected_report_end_date = datetime.strptime(
        report_date, "%Y-%m-%d"
    ).date()

    records: list[dict[str, object]] = []

    for fact_index, fact in enumerate(model_xbrl.facts):
        concept = getattr(fact, "concept", None)

        if concept is None:
            continue

        concept_qname_str = str(getattr(concept, "qname", ""))

        if concept_qname_str != target_concept_qname:
            continue

        context = fact.context
        unit = fact.unit

        if context is None:
            continue

        is_duration = bool(getattr(context, "isStartEndPeriod", False))
        is_instant = bool(getattr(context, "isInstantPeriod", False))

        period_start = None
        period_end = None
        duration_days = None

        if is_duration:
            start_dt = context.startDatetime
            end_dt = context.endDatetime

            if start_dt is not None:
                period_start = start_dt.date().isoformat()

            if end_dt is not None:
                # XBRL duration end dates are exclusive (a point in time
                # at the start of the following day), so the last
                # actually-reported day is end - 1 day.
                period_end = (
                    (end_dt - timedelta(days=1)).date().isoformat()
                )

            if start_dt is not None and end_dt is not None:
                duration_days = (end_dt - start_dt).days

        elif is_instant:
            instant_dt = context.instantDatetime

            if instant_dt is not None:
                period_end = instant_dt.date().isoformat()

        dims = getattr(context, "qnameDims", {}) or {}
        dimensions_count = len(dims)

        dimension_parts = []

        for dim_qname, dim_value in dims.items():
            member_repr = getattr(dim_value, "memberQname", None)

            if member_repr is None:
                member_repr = getattr(dim_value, "typedMember", None)

            dimension_parts.append(f"{dim_qname}={member_repr}")

        dimensions_repr = "; ".join(dimension_parts)

        entity_identifier = None
        entity_cik_ok = False

        entity_id_tuple = getattr(context, "entityIdentifier", None)

        if entity_id_tuple:
            entity_identifier = str(entity_id_tuple[1])

            try:
                entity_cik_ok = int(entity_identifier) == expected_cik
            except ValueError:
                entity_cik_ok = False

        unit_measures = ""
        unit_ok = False

        if unit is not None:
            measures = getattr(unit, "measures", None)

            if measures and measures[0]:
                unit_measures = ",".join(
                    str(measure) for measure in measures[0]
                )
                unit_ok = unit_measures == "iso4217:USD"

        no_dimensions_ok = dimensions_count == 0

        period_end_match_ok = (
            period_end is not None
            and period_end == expected_report_end_date.isoformat()
        )

        duration_annual_ok = (
            duration_days is not None
            and ANNUAL_DURATION_MIN_DAYS
            <= duration_days
            <= ANNUAL_DURATION_MAX_DAYS
        )

        all_filters_ok = (
            unit_ok
            and no_dimensions_ok
            and period_end_match_ok
            and duration_annual_ok
            and entity_cik_ok
            and not fact.isNil
        )

        value_raw = None if fact.isNil else fact.value

        value_numeric = None

        if not fact.isNil:
            try:
                value_numeric = float(fact.xValue)
            except (TypeError, ValueError):
                try:
                    value_numeric = float(fact.value)
                except (TypeError, ValueError):
                    value_numeric = None

        records.append(
            {
                "fact_index": fact_index,
                "concept_qname": concept_qname_str,
                "context_id": fact.contextID,
                "unit_id": fact.unitID,
                "unit_measures": unit_measures,
                "is_duration": is_duration,
                "is_instant": is_instant,
                "period_start": period_start,
                "period_end": period_end,
                "duration_days": duration_days,
                "entity_identifier": entity_identifier,
                "dimensions_count": dimensions_count,
                "dimensions": dimensions_repr,
                "decimals": fact.decimals,
                "is_nil": bool(fact.isNil),
                "value_raw": value_raw,
                "value_numeric": value_numeric,
                "unit_ok": unit_ok,
                "no_dimensions_ok": no_dimensions_ok,
                "period_end_match_ok": period_end_match_ok,
                "duration_annual_ok": duration_annual_ok,
                "entity_cik_ok": entity_cik_ok,
                "all_filters_ok": all_filters_ok,
            }
        )

    return pd.DataFrame(records)


# =============================================================================
# 6. DEDUPLICATION + STATUS DECISION
# =============================================================================


def deduplicate_and_decide(candidates: pd.DataFrame) -> dict[str, object]:
    """
    Collapses technical Inline XBRL duplicates (identical value reported
    under more than one fact instance — a known, previously observed
    pattern) and decides PASS vs REVIEW_REQUIRED. Never guesses between
    genuinely different values.
    """

    outcome: dict[str, object] = {
        "matched_fact_count": int(len(candidates)),
        "filtered_fact_count": 0,
        "distinct_value_count": 0,
        "status": "REVIEW_REQUIRED",
        "error": None,
        "selected_value": None,
        "selected_context_id": None,
        "selected_period_start": None,
        "selected_period_end": None,
        "selected_unit": None,
        "selected_decimals": None,
        "note": None,
    }

    if candidates.empty:
        outcome["error"] = (
            "לא נמצא אף Fact עם ה-concept שזוהה מה-Presentation."
        )
        return outcome

    filtered = candidates[candidates["all_filters_ok"]].copy()
    outcome["filtered_fact_count"] = int(len(filtered))

    if filtered.empty:
        outcome["error"] = (
            "נמצאו facts עם ה-concept הנדרש, אך אף אחד לא עמד בכל תנאי "
            "הסינון (unit / ללא dimensions / תאריך סיום תואם / משך "
            "שנתי / CIK תואם)."
        )
        return outcome

    distinct_values = sorted(set(filtered["value_numeric"].tolist()))
    outcome["distinct_value_count"] = len(distinct_values)

    if len(distinct_values) == 1:
        selected_row = filtered.iloc[0]

        outcome["status"] = "PASS"
        outcome["selected_value"] = distinct_values[0]
        outcome["selected_context_id"] = str(selected_row["context_id"])
        outcome["selected_period_start"] = selected_row["period_start"]
        outcome["selected_period_end"] = selected_row["period_end"]
        outcome["selected_unit"] = str(selected_row["unit_measures"])
        outcome["selected_decimals"] = str(selected_row["decimals"])

        if len(filtered) > 1:
            outcome["note"] = (
                f"{len(filtered)} facts עברו את הסינון אך כולם בעלי אותו "
                "ערך — טופלו ככפילות טכנית (תופעה מוכרת מ-Inline XBRL)."
            )
    else:
        outcome["error"] = (
            "יותר ממועמד אחד עבר את הסינון עם ערכים שונים "
            f"({distinct_values}) — אין בסיס לבחור אוטומטית."
        )

    return outcome


# =============================================================================
# 7. BOUNDED ORCHESTRATION — child process running steps 2-6 for every
#    requested metric against one loaded model, plus timeouts.
# =============================================================================


def engine_child_worker(
    primary_document: str,
    cache_directory: str,
    log_file: str,
    http_user_agent: str,
    internet_timeout_seconds: int,
    expected_cik: int,
    report_date: str,
    metric_names: list[str],
    presentation_csv: str,
    row_candidates_csv: str,
    fact_candidates_csv: str,
    result_file: str,
) -> None:
    per_metric_results: dict[str, dict[str, object]] = {
        name: {
            "status": "FAIL",
            "error": None,
            "target_concept_qname": None,
            "target_role_uri": None,
            "target_role_definition": None,
            "target_label": None,
            "selection_tier": None,
            "matched_fact_count": 0,
            "filtered_fact_count": 0,
            "distinct_value_count": 0,
            "selected_value": None,
            "selected_context_id": None,
            "selected_period_start": None,
            "selected_period_end": None,
            "selected_unit": None,
            "selected_decimals": None,
            "note": None,
        }
        for name in metric_names
    }

    presentation_row_count = 0
    all_row_candidates: list[pd.DataFrame] = []
    all_fact_candidates: list[pd.DataFrame] = []

    try:
        from arelle.RuntimeOptions import RuntimeOptions
        from arelle.api.Session import Session

        Path(cache_directory).mkdir(parents=True, exist_ok=True)

        options = RuntimeOptions(
            entrypointFile=primary_document,
            internetConnectivity="online",
            cacheDirectory=cache_directory,
            internetTimeout=internet_timeout_seconds,
            httpUserAgent=http_user_agent,
            keepOpen=True,
            logFile=log_file,
            logFormat=(
                "[%(levelname)s] [%(messageCode)s] "
                "%(message)s - %(file)s"
            ),
        )

        with Session() as session:
            session.run(options)

            models = session.get_models()

            if len(models) != 1:
                raise RuntimeError(
                    "Arelle לא החזיר מודל יחיד וברור.\n"
                    f"מספר מודלים: {len(models)}"
                )

            model_xbrl = models[0]

            if model_xbrl is None:
                raise RuntimeError("Arelle לא הצליח לטעון את מודל ה-XBRL.")

            # --- step 2: presentation, loaded once, shared by all metrics
            presentation = extract_presentation(model_xbrl)

            if presentation.empty:
                raise RuntimeError("לא נמצאו Presentation relationships.")

            presentation.to_csv(
                presentation_csv,
                index=False,
                encoding="utf-8-sig",
            )

            presentation_row_count = int(len(presentation))

            for metric_name in metric_names:
                metric = BUILT_IN_METRICS[metric_name]
                metric_result = per_metric_results[metric_name]

                # --- steps 3+4: statement role + canonical row
                try:
                    target_row, row_candidates = identify_canonical_row(
                        presentation, metric
                    )
                except TargetRowNotFound as exc:
                    metric_result["status"] = "REVIEW_REQUIRED"
                    metric_result["error"] = str(exc)
                    continue

                row_candidates = row_candidates.copy()
                row_candidates.insert(0, "metric", metric_name)
                all_row_candidates.append(row_candidates)

                metric_result["target_concept_qname"] = (
                    target_row["concept_qname"]
                )
                metric_result["target_role_uri"] = target_row["role_uri"]
                metric_result["target_role_definition"] = (
                    target_row["role_definition"]
                )
                metric_result["target_label"] = target_row["label"]
                metric_result["selection_tier"] = (
                    target_row["selection_tier"]
                )

                # --- step 5: fact matching
                fact_candidates = match_facts(
                    model_xbrl=model_xbrl,
                    target_concept_qname=target_row["concept_qname"],
                    expected_cik=expected_cik,
                    report_date=report_date,
                )

                fact_candidates_tagged = fact_candidates.copy()
                fact_candidates_tagged.insert(0, "metric", metric_name)
                all_fact_candidates.append(fact_candidates_tagged)

                # --- step 6: dedup + decision
                decision = deduplicate_and_decide(fact_candidates)

                metric_result.update(decision)

    except Exception as exc:
        error_text = f"{exc}\n{traceback.format_exc()}"

        for metric_result in per_metric_results.values():
            if metric_result["status"] not in ("PASS", "REVIEW_REQUIRED"):
                metric_result["status"] = "FAIL"
                metric_result["error"] = error_text

    if all_row_candidates:
        pd.concat(all_row_candidates, ignore_index=True).to_csv(
            row_candidates_csv,
            index=False,
            encoding="utf-8-sig",
        )

    if all_fact_candidates:
        pd.concat(all_fact_candidates, ignore_index=True).to_csv(
            fact_candidates_csv,
            index=False,
            encoding="utf-8-sig",
        )

    Path(result_file).write_text(
        json.dumps(
            {
                "presentation_row_count": presentation_row_count,
                "metrics": per_metric_results,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def run_engine(
    ticker: str,
    report_date: str,
    metric_names: list[str],
) -> dict[str, object]:
    paths = output_paths(ticker, report_date)

    locked_filing = load_locked_filing(ticker, report_date)

    log_line(paths["orchestration_log_file"], "=" * 100)
    log_line(
        paths["orchestration_log_file"],
        f"{ticker.upper()} {report_date} — GENERIC XBRL METRIC ENGINE v2 "
        f"[{', '.join(metric_names)}]",
    )
    log_line(paths["orchestration_log_file"], "=" * 100)
    log_line(
        paths["orchestration_log_file"],
        f"קובץ 10-K: {locked_filing['primary_document_path']}",
    )
    log_line(
        paths["orchestration_log_file"],
        f"Accession: {locked_filing['accession_compact']}",
    )
    log_line(
        paths["orchestration_log_file"],
        f"Total timeout: {TOTAL_TIMEOUT_SECONDS}s | "
        f"Per-connection timeout: {INTERNET_TIMEOUT_SECONDS}s",
    )

    if paths["result_file"].exists():
        paths["result_file"].unlink()

    process = multiprocessing.Process(
        target=engine_child_worker,
        kwargs={
            "primary_document": str(
                locked_filing["primary_document_path"]
            ),
            "cache_directory": str(CACHE_DIR),
            "log_file": str(paths["arelle_log_file"]),
            "http_user_agent": locked_filing["sec_user_agent"],
            "internet_timeout_seconds": INTERNET_TIMEOUT_SECONDS,
            "expected_cik": locked_filing["cik"],
            "report_date": locked_filing["report_date"],
            "metric_names": metric_names,
            "presentation_csv": str(paths["presentation_csv"]),
            "row_candidates_csv": str(paths["row_candidates_csv"]),
            "fact_candidates_csv": str(paths["fact_candidates_csv"]),
            "result_file": str(paths["result_file"]),
        },
    )

    run_started_at = datetime.now(timezone.utc)
    start_perf = time.perf_counter()

    log_line(paths["orchestration_log_file"], "מפעיל child process...")
    process.start()

    process.join(timeout=TOTAL_TIMEOUT_SECONDS)

    timed_out = False

    if process.is_alive():
        timed_out = True
        log_line(
            paths["orchestration_log_file"],
            f"חריגה מ-{TOTAL_TIMEOUT_SECONDS} שניות — "
            "שולח terminate() ל-child process.",
        )
        process.terminate()
        process.join(timeout=TERMINATE_GRACE_SECONDS)

        if process.is_alive():
            log_line(
                paths["orchestration_log_file"],
                "terminate() לא הספיק — שולח kill().",
            )
            process.kill()
            process.join(timeout=TERMINATE_GRACE_SECONDS)

    elapsed_seconds = time.perf_counter() - start_perf
    run_ended_at = datetime.now(timezone.utc)

    child_exit_code = process.exitcode
    log_line(
        paths["orchestration_log_file"],
        f"Child process הסתיים. exit_code={child_exit_code}",
    )

    child_result: dict[str, object] = {}

    if paths["result_file"].exists():
        child_result = json.loads(
            paths["result_file"].read_text(encoding="utf-8")
        )

    metrics_out: dict[str, object] = {}

    for metric_name in metric_names:
        if timed_out:
            metrics_out[metric_name] = {
                "status": "TIMEOUT",
                "error": (
                    f"ה-child process לא הסתיים תוך "
                    f"{TOTAL_TIMEOUT_SECONDS} שניות ונהרג באופן אוטומטי."
                ),
            }
            continue

        metric_child_result = (
            child_result.get("metrics", {}).get(metric_name)
        )

        if metric_child_result is None:
            metrics_out[metric_name] = {
                "status": "FAIL",
                "error": (
                    "ה-child process הסתיים אך לא נכתבה תוצאה למדד זה. "
                    f"exit_code={child_exit_code}"
                ),
            }
            continue

        qa_reference = find_qa_reference_value(
            ticker, report_date, metric_name
        )

        metrics_out[metric_name] = {
            **metric_child_result,
            "qa_reference_only_not_used_for_selection": qa_reference,
        }

    final_result = {
        "ticker": locked_filing["ticker"],
        "company_name": locked_filing["company_name"],
        "cik": locked_filing["cik"],
        "form": EXPECTED_FORM,
        "accession_number": locked_filing["accession_number"],
        "accession_compact": locked_filing["accession_compact"],
        "report_date": locked_filing["report_date"],
        "filing_date": locked_filing["filing_date"],
        "source_document": locked_filing["primary_document_name"],
        "primary_document_path": str(
            locked_filing["primary_document_path"]
        ),
        "manifest_file": str(locked_filing["manifest_file"]),
        "run_started_at_utc": run_started_at.isoformat(),
        "run_ended_at_utc": run_ended_at.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "total_timeout_seconds": TOTAL_TIMEOUT_SECONDS,
        "internet_timeout_seconds": INTERNET_TIMEOUT_SECONDS,
        "cache_directory": str(CACHE_DIR),
        "http_user_agent": locked_filing["sec_user_agent"],
        "child_exit_code": child_exit_code,
        "timed_out": timed_out,
        "presentation_row_count": child_result.get(
            "presentation_row_count", 0
        ),
        "metrics_requested": metric_names,
        "metrics": metrics_out,
        "all_pass": all(
            metrics_out[name].get("status") == "PASS"
            for name in metric_names
        ),
        "presentation_csv": (
            str(paths["presentation_csv"])
            if paths["presentation_csv"].exists()
            else None
        ),
        "row_candidates_csv": (
            str(paths["row_candidates_csv"])
            if paths["row_candidates_csv"].exists()
            else None
        ),
        "fact_candidates_csv": (
            str(paths["fact_candidates_csv"])
            if paths["fact_candidates_csv"].exists()
            else None
        ),
        "arelle_log_file": str(paths["arelle_log_file"]),
        "orchestration_log_file": str(paths["orchestration_log_file"]),
    }

    paths["result_file"].write_text(
        json.dumps(final_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for metric_name in metric_names:
        log_line(
            paths["orchestration_log_file"],
            f"{metric_name}: {metrics_out[metric_name].get('status')}",
        )

    log_line(
        paths["orchestration_log_file"],
        f"קובץ תוצאה: {paths['result_file']}",
    )

    return final_result


def main() -> None:
    arguments = parse_arguments()

    result = run_engine(
        ticker=arguments.ticker,
        report_date=arguments.report_date,
        metric_names=arguments.metrics,
    )

    print()
    print("=" * 100)
    print(
        f"תוצאת מנוע ה-XBRL הגנרי — {arguments.ticker.upper()} "
        f"{arguments.report_date}"
    )
    print("=" * 100)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
