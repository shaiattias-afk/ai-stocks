from __future__ import annotations

import argparse
import json
import multiprocessing
import time
import traceback
from datetime import timedelta, timezone, datetime
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "arelle_cache"

EXPECTED_FORM = "10-K"

# Same bounding values already verified as safe (non-hanging) on the
# revenue extraction proofs (scripts 37c/37d/38/39).
TOTAL_TIMEOUT_SECONDS = 240
INTERNET_TIMEOUT_SECONDS = 20
TERMINATE_GRACE_SECONDS = 5

# A duration context is accepted as "annual" only within this tolerance,
# instead of assuming any single company's specific fiscal-year length.
ANNUAL_DURATION_MIN_DAYS = 350
ANNUAL_DURATION_MAX_DAYS = 380


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generic, ticker-agnostic extraction of the Net Income fact "
            "attributable to shareholders from a locked 10-K, using "
            "Arelle statement-first presentation structure only (no "
            "per-company concept, and no manual NetIncomeLoss/ProfitLoss "
            "priority list)."
        )
    )

    parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker, for example ORCL or MSFT.",
    )

    parser.add_argument(
        "--report-date",
        required=True,
        help="Fiscal report date in YYYY-MM-DD format.",
    )

    return parser.parse_args()


def output_paths(ticker: str, report_date: str) -> dict[str, Path]:
    prefix = f"{ticker.lower()}_{report_date.replace('-', '')}"

    return {
        "presentation_csv": (
            DATA_DIR / f"{prefix}_net_income_arelle_presentation.csv"
        ),
        "candidates_csv": (
            DATA_DIR / f"{prefix}_net_income_fact_candidates.csv"
        ),
        "row_candidates_csv": (
            DATA_DIR / f"{prefix}_net_income_row_candidates.csv"
        ),
        "result_file": (
            DATA_DIR / f"{prefix}_net_income_fact_result.json"
        ),
        "arelle_log_file": (
            DATA_DIR / f"{prefix}_net_income_arelle_child.log"
        ),
        "orchestration_log_file": (
            DATA_DIR / f"{prefix}_net_income_orchestration.log"
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
) -> dict[str, object] | None:
    """
    Best-effort QA-only lookup of a net income figure already computed by
    an earlier, independent pipeline in this project, if one happens to
    exist for this exact ticker and period. Never used to select or
    validate the extracted fact — only reported for human comparison.
    """

    candidate_file = DATA_DIR / f"{ticker.lower()}_net_income_test.csv"

    if not candidate_file.exists():
        return {
            "source_file": None,
            "note": (
                "לא נמצא קובץ QA עצמאי עבור net income של "
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
        "net_income_usd": row.get("net_income_usd"),
        "concept": row.get("concept"),
        "period_start": row.get("period_start"),
        "period_end": row.get("period_end"),
    }


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


def _extract_presentation(model_xbrl: Any) -> pd.DataFrame:
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


class TargetRowNotFound(Exception):
    """
    Raised when the presentation structure does not resolve to exactly
    one unambiguous net-income row. Distinguished from other exceptions
    so the caller can report REVIEW_REQUIRED (insufficient evidence)
    instead of FAIL (execution error).
    """


def select_target_row(
    presentation: pd.DataFrame,
) -> tuple[dict[str, str], pd.DataFrame]:
    """
    Selects the single row representing Net Income attributable to
    shareholders on the primary income statement, using position, label,
    and relationships only — never a hard-coded NetIncomeLoss/ProfitLoss
    priority list:

    1. Restrict to the primary Statement-type role (title containing
       "income" or "operations"), same rule already verified for revenue.
    2. Within it, collect non-abstract rows whose label mentions
       "net income"/"net loss", excluding per-share and weighted-average-
       share rows (those are derived figures, not the income line itself).
    3. Tier A — prefer a row explicitly "attributable to" the parent /
       common shareholders (this is what "correspondence to shareholders"
       means when a noncontrolling-interest breakout exists on the face
       of the statement).
    4. Tier B — if no such breakout exists, fall back to a plain,
       unqualified "Net income" / "Net loss" / "Net income (loss)" row —
       there is nothing to attribute away, so the single reported line
       already belongs to the shareholders.
    5. If neither tier resolves to exactly one row, fail closed.

    Whichever concept the filer's own presentation linkbase attached to
    the selected row (NetIncomeLoss, ProfitLoss, or anything else) is
    what gets used — the choice is made by the filing's own structure,
    not by a preference list maintained in this code.
    """

    is_statement_role = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-",
        na=False,
    )

    is_income_statement = presentation["role_definition"].str.contains(
        r"income|operations",
        case=False,
        regex=True,
        na=False,
    )

    is_not_abstract = ~presentation["is_abstract"].astype(bool)

    mentions_net_income = presentation["label"].str.contains(
        r"(?i)net\s+(income|loss)",
        regex=True,
        na=False,
    )

    is_per_share_or_shares = presentation["label"].str.contains(
        r"(?i)per\s+share|weighted\s+average",
        regex=True,
        na=False,
    )

    base_candidates = presentation[
        is_statement_role
        & is_income_statement
        & is_not_abstract
        & mentions_net_income
        & ~is_per_share_or_shares
    ].copy()

    if base_candidates.empty:
        raise TargetRowNotFound(
            "לא נמצאה אף שורת Net Income בתוך Statement role של "
            "Income/Operations."
        )

    is_attributable_to_shareholders = base_candidates["label"].str.contains(
        r"(?i)attributable\s+to.*("
        r"common|stockholders|shareholders|corporation|company|"
        r"\binc\.?\b|\bcorp\.?\b)",
        regex=True,
        na=False,
    )

    tier_a = base_candidates[is_attributable_to_shareholders]

    if len(tier_a) == 1:
        row = tier_a.iloc[0]

        return (
            {
                "role_uri": str(row["role_uri"]),
                "role_definition": str(row["role_definition"]),
                "concept_qname": str(row["concept_qname"]),
                "label": str(row["label"]),
                "period_type": str(row["period_type"]),
                "selection_tier": (
                    "attributable_to_shareholders"
                ),
            },
            base_candidates,
        )

    is_plain_net_income = base_candidates["label"].str.match(
        r"(?i)^\s*net\s+(income\s*\(loss\)|income|loss)\s*$",
        na=False,
    )

    tier_b = base_candidates[is_plain_net_income]

    if len(tier_b) == 1:
        row = tier_b.iloc[0]

        return (
            {
                "role_uri": str(row["role_uri"]),
                "role_definition": str(row["role_definition"]),
                "concept_qname": str(row["concept_qname"]),
                "label": str(row["label"]),
                "period_type": str(row["period_type"]),
                "selection_tier": "plain_net_income",
            },
            base_candidates,
        )

    raise TargetRowNotFound(
        "לא ניתן לזהות שורת Net Income יחידה וחד-משמעית: לא נמצאה "
        "בדיוק שורה אחת 'attributable to shareholders', וגם לא "
        "בדיוק שורת 'Net income' פשוטה יחידה.\n"
        f"מספר שורות מועמדות כוללות: {len(base_candidates)}, "
        f"מתוכן 'attributable': {len(tier_a)}, 'plain': {len(tier_b)}"
    )


def arelle_child_worker(
    primary_document: str,
    cache_directory: str,
    log_file: str,
    http_user_agent: str,
    internet_timeout_seconds: int,
    expected_cik: int,
    report_date: str,
    presentation_csv: str,
    candidates_csv: str,
    row_candidates_csv: str,
    result_file: str,
) -> None:
    """
    Runs in a separate child process so a hang cannot block the parent.
    Loads the locked filing once, walks the full presentation structure,
    selects the target Net Income row by structure/label only, then
    extracts and filters the matching numeric facts. Writes presentation,
    row candidates, fact candidates, and the final result to disk
    regardless of outcome.
    """

    result: dict[str, object] = {
        "status": "FAIL",
        "error": None,
        "target_concept_qname": None,
        "target_role_uri": None,
        "target_role_definition": None,
        "target_label": None,
        "selection_tier": None,
        "presentation_row_count": 0,
        "matched_fact_count": 0,
        "filtered_fact_count": 0,
        "distinct_value_count": 0,
        "selected_value": None,
        "selected_context_id": None,
        "selected_period_start": None,
        "selected_period_end": None,
        "selected_unit": None,
        "selected_decimals": None,
    }

    try:
        from arelle.RuntimeOptions import RuntimeOptions
        from arelle.api.Session import Session

        Path(cache_directory).mkdir(parents=True, exist_ok=True)

        expected_report_end_date = datetime.strptime(
            report_date, "%Y-%m-%d"
        ).date()

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

        target_row: dict[str, str] | None = None

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
                raise RuntimeError(
                    "Arelle לא הצליח לטעון את מודל ה-XBRL."
                )

            presentation = _extract_presentation(model_xbrl)

            if presentation.empty:
                raise RuntimeError(
                    "לא נמצאו Presentation relationships."
                )

            presentation.to_csv(
                presentation_csv,
                index=False,
                encoding="utf-8-sig",
            )

            result["presentation_row_count"] = int(len(presentation))

            try:
                target_row, row_candidates = select_target_row(
                    presentation
                )

                row_candidates.to_csv(
                    row_candidates_csv,
                    index=False,
                    encoding="utf-8-sig",
                )
            except TargetRowNotFound as exc:
                result["status"] = "REVIEW_REQUIRED"
                result["error"] = str(exc)

            if target_row is not None:
                result["target_concept_qname"] = (
                    target_row["concept_qname"]
                )
                result["target_role_uri"] = target_row["role_uri"]
                result["target_role_definition"] = (
                    target_row["role_definition"]
                )
                result["target_label"] = target_row["label"]
                result["selection_tier"] = target_row["selection_tier"]

                target_concept_qname = target_row["concept_qname"]

                records: list[dict[str, object]] = []

                for fact_index, fact in enumerate(model_xbrl.facts):
                    concept = getattr(fact, "concept", None)

                    if concept is None:
                        continue

                    concept_qname_str = str(
                        getattr(concept, "qname", "")
                    )

                    if concept_qname_str != target_concept_qname:
                        continue

                    context = fact.context
                    unit = fact.unit

                    if context is None:
                        continue

                    is_duration = bool(
                        getattr(context, "isStartEndPeriod", False)
                    )
                    is_instant = bool(
                        getattr(context, "isInstantPeriod", False)
                    )

                    period_start = None
                    period_end = None
                    duration_days = None

                    if is_duration:
                        start_dt = context.startDatetime
                        end_dt = context.endDatetime

                        if start_dt is not None:
                            period_start = start_dt.date().isoformat()

                        if end_dt is not None:
                            # XBRL duration end dates are exclusive (a
                            # point in time at the start of the following
                            # day), so the last actually-reported day is
                            # end - 1 day.
                            period_end = (
                                (end_dt - timedelta(days=1))
                                .date()
                                .isoformat()
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
                        member_repr = getattr(
                            dim_value, "memberQname", None
                        )

                        if member_repr is None:
                            member_repr = getattr(
                                dim_value, "typedMember", None
                            )

                        dimension_parts.append(
                            f"{dim_qname}={member_repr}"
                        )

                    dimensions_repr = "; ".join(dimension_parts)

                    entity_identifier = None
                    entity_cik_ok = False

                    entity_id_tuple = getattr(
                        context, "entityIdentifier", None
                    )

                    if entity_id_tuple:
                        entity_identifier = str(entity_id_tuple[1])

                        try:
                            entity_cik_ok = (
                                int(entity_identifier) == expected_cik
                            )
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
                        and period_end
                        == expected_report_end_date.isoformat()
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

                candidates = pd.DataFrame(records)

                candidates.to_csv(
                    candidates_csv,
                    index=False,
                    encoding="utf-8-sig",
                )

                result["matched_fact_count"] = int(len(candidates))

                if candidates.empty:
                    result["status"] = "REVIEW_REQUIRED"
                    result["error"] = (
                        "לא נמצא אף Fact עם ה-concept שזוהה "
                        "מה-Presentation."
                    )
                else:
                    filtered = candidates[
                        candidates["all_filters_ok"]
                    ].copy()
                    result["filtered_fact_count"] = int(len(filtered))

                    if filtered.empty:
                        result["status"] = "REVIEW_REQUIRED"
                        result["error"] = (
                            "נמצאו facts עם ה-concept הנדרש, אך אף אחד "
                            "לא עמד בכל תנאי הסינון (unit / ללא "
                            "dimensions / תאריך סיום תואם / משך שנתי / "
                            "CIK תואם)."
                        )
                    else:
                        distinct_values = sorted(
                            set(filtered["value_numeric"].tolist())
                        )
                        result["distinct_value_count"] = len(
                            distinct_values
                        )

                        if len(distinct_values) == 1:
                            selected_row = filtered.iloc[0]

                            result["status"] = "PASS"
                            result["selected_value"] = distinct_values[0]
                            result["selected_context_id"] = str(
                                selected_row["context_id"]
                            )
                            result["selected_period_start"] = (
                                selected_row["period_start"]
                            )
                            result["selected_period_end"] = (
                                selected_row["period_end"]
                            )
                            result["selected_unit"] = str(
                                selected_row["unit_measures"]
                            )
                            result["selected_decimals"] = str(
                                selected_row["decimals"]
                            )

                            if len(filtered) > 1:
                                result["note"] = (
                                    f"{len(filtered)} facts עברו את "
                                    "הסינון אך כולם בעלי אותו ערך — "
                                    "טופלו ככפילות טכנית (תופעה מוכרת "
                                    "מ-Inline XBRL)."
                                )
                        else:
                            result["status"] = "REVIEW_REQUIRED"
                            result["error"] = (
                                "יותר ממועמד אחד עבר את הסינון עם "
                                f"ערכים שונים ({distinct_values}) — "
                                "אין בסיס לבחור אוטומטית."
                            )

    except Exception as exc:
        result["status"] = "FAIL"
        result["error"] = f"{exc}\n{traceback.format_exc()}"

    Path(result_file).write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def run_extraction(ticker: str, report_date: str) -> dict[str, object]:
    paths = output_paths(ticker, report_date)

    locked_filing = load_locked_filing(ticker, report_date)

    log_line(paths["orchestration_log_file"], "=" * 100)
    log_line(
        paths["orchestration_log_file"],
        f"{ticker.upper()} {report_date} — NET INCOME FACT EXTRACTION "
        "(generic Statement-first method)",
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
        target=arelle_child_worker,
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
            "presentation_csv": str(paths["presentation_csv"]),
            "candidates_csv": str(paths["candidates_csv"]),
            "row_candidates_csv": str(paths["row_candidates_csv"]),
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

    if timed_out:
        status = "TIMEOUT"
        error = (
            f"ה-child process לא הסתיים תוך {TOTAL_TIMEOUT_SECONDS} "
            "שניות ונהרג באופן אוטומטי."
        )
    elif child_result:
        status = str(child_result.get("status", "FAIL"))
        error = child_result.get("error")
    else:
        status = "FAIL"
        error = (
            "ה-child process הסתיים אך לא נכתב קובץ תוצאה. "
            f"exit_code={child_exit_code}"
        )

    qa_reference = find_qa_reference_value(ticker, report_date)

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
        "source_concept": child_result.get("target_concept_qname"),
        "statement_role_uri": child_result.get("target_role_uri"),
        "statement_role_definition": child_result.get(
            "target_role_definition"
        ),
        "label": child_result.get("target_label"),
        "selection_tier": child_result.get("selection_tier"),
        "run_started_at_utc": run_started_at.isoformat(),
        "run_ended_at_utc": run_ended_at.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "total_timeout_seconds": TOTAL_TIMEOUT_SECONDS,
        "internet_timeout_seconds": INTERNET_TIMEOUT_SECONDS,
        "cache_directory": str(CACHE_DIR),
        "http_user_agent": locked_filing["sec_user_agent"],
        "child_exit_code": child_exit_code,
        "timed_out": timed_out,
        "validation_status": status,
        "error": error,
        "presentation_row_count": child_result.get(
            "presentation_row_count", 0
        ),
        "matched_fact_count": child_result.get("matched_fact_count", 0),
        "filtered_fact_count": child_result.get("filtered_fact_count", 0),
        "distinct_value_count": child_result.get(
            "distinct_value_count", 0
        ),
        "value": child_result.get("selected_value"),
        "context_id": child_result.get("selected_context_id"),
        "period_start": child_result.get("selected_period_start"),
        "period_end": child_result.get("selected_period_end"),
        "unit": child_result.get("selected_unit"),
        "decimals": child_result.get("selected_decimals"),
        "dimensions": "none (consolidated, non-dimensional context)",
        "note": child_result.get("note"),
        "qa_reference_only_not_used_for_selection": qa_reference,
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
        "candidates_csv": (
            str(paths["candidates_csv"])
            if paths["candidates_csv"].exists()
            else None
        ),
        "arelle_log_file": str(paths["arelle_log_file"]),
        "orchestration_log_file": str(paths["orchestration_log_file"]),
    }

    paths["result_file"].write_text(
        json.dumps(final_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log_line(paths["orchestration_log_file"], f"סטטוס סופי: {status}")
    log_line(
        paths["orchestration_log_file"],
        f"קובץ תוצאה: {paths['result_file']}",
    )

    return final_result


def main() -> None:
    arguments = parse_arguments()

    result = run_extraction(
        ticker=arguments.ticker,
        report_date=arguments.report_date,
    )

    print()
    print("=" * 100)
    print(
        f"תוצאת חילוץ Net Income — {arguments.ticker.upper()} "
        f"{arguments.report_date}"
    )
    print("=" * 100)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
