from __future__ import annotations

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

LOCKED_FILINGS_DIR = DATA_DIR / "sec_filings_locked" / "ORCL"
CACHE_DIR = DATA_DIR / "arelle_cache"

EXPECTED_REPORT_DATE = "2024-05-31"
EXPECTED_FORM = "10-K"
EXPECTED_ACCESSION_COMPACT = "000095017024075605"
EXPECTED_PRIMARY_DOCUMENT = "orcl-20240531.htm"

# Input produced and verified by scripts/37c_run_arelle_bounded_test.py.
# This script only reads it; it is never modified or re-derived from
# scratch, so the already-verified presentation structure is reused
# rather than recomputed.
PRESENTATION_CSV = DATA_DIR / "orcl_2024_arelle_bounded_presentation.csv"

CANDIDATES_CSV = DATA_DIR / "orcl_2024_revenue_fact_candidates.csv"
RESULT_FILE = DATA_DIR / "orcl_2024_revenue_fact_result.json"
ARELLE_LOG_FILE = DATA_DIR / "orcl_2024_revenue_fact_child.log"
ORCHESTRATION_LOG_FILE = DATA_DIR / "orcl_2024_revenue_fact_orchestration.log"

# Same bounding values verified as safe (non-hanging) in 37c.
TOTAL_TIMEOUT_SECONDS = 240
INTERNET_TIMEOUT_SECONDS = 20
TERMINATE_GRACE_SECONDS = 5

# A duration context is accepted as "annual" only within this tolerance,
# instead of assuming Oracle's specific fiscal-year start date.
ANNUAL_DURATION_MIN_DAYS = 350
ANNUAL_DURATION_MAX_DAYS = 380


def log_line(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"[{timestamp}] {message}"

    print(line)

    with ORCHESTRATION_LOG_FILE.open(mode="a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")


def load_locked_filing() -> dict[str, Any]:
    manifests = sorted(
        LOCKED_FILINGS_DIR.glob("*/locked_filing_manifest.json")
    )

    matching_manifests = []

    for manifest_file in manifests:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

        if (
            manifest.get("report_date") == EXPECTED_REPORT_DATE
            and manifest.get("form") == EXPECTED_FORM
        ):
            matching_manifests.append((manifest_file, manifest))

    if len(matching_manifests) != 1:
        raise RuntimeError(
            "לא נמצא Manifest יחיד וברור של Oracle 2024.\n"
            f"מספר התאמות: {len(matching_manifests)}"
        )

    manifest_file, manifest = matching_manifests[0]

    accession_compact = str(manifest.get("accession_number", "")).replace(
        "-", ""
    )

    if accession_compact != EXPECTED_ACCESSION_COMPACT:
        raise RuntimeError(
            "מספר ה-accession אינו תואם לנעילה המצופה.\n"
            f"נמצא: {accession_compact}\n"
            f"מצופה: {EXPECTED_ACCESSION_COMPACT}"
        )

    if manifest.get("primary_document") != EXPECTED_PRIMARY_DOCUMENT:
        raise RuntimeError(
            "שם המסמך הראשי אינו תואם.\n"
            f"נמצא: {manifest.get('primary_document')}\n"
            f"מצופה: {EXPECTED_PRIMARY_DOCUMENT}"
        )

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
        "accession_compact": accession_compact,
        "accession_number": manifest.get("accession_number"),
        "report_date": manifest.get("report_date"),
        "filing_date": manifest.get("filing_date"),
        "sec_user_agent": sec_user_agent,
        "cik": int(cik),
        "ticker": manifest.get("ticker", "ORCL"),
    }


def select_target_presentation_row() -> dict[str, str]:
    """
    Chooses the single row inside the already-verified presentation output
    of 37c that represents "Total revenues" on the primary statement of
    operations. Selection is by structure (a top-level Statement role, not
    a Disclosure/Details/Tables/Policies role) and label text, never by a
    hard-coded Oracle-specific concept name. If this does not resolve to
    exactly one row, the caller must fail closed with REVIEW_REQUIRED.
    """

    if not PRESENTATION_CSV.exists():
        raise FileNotFoundError(
            f"קובץ ה-Presentation של 37c לא נמצא:\n{PRESENTATION_CSV}"
        )

    presentation = pd.read_csv(
        PRESENTATION_CSV,
        dtype=str,
        keep_default_na=False,
    )

    is_statement_role = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-",
        na=False,
    )

    is_operations_statement = presentation["role_definition"].str.contains(
        "OPERATIONS",
        case=False,
        na=False,
    )

    is_total_revenue_label = presentation["label"].str.contains(
        r"total\s+revenue",
        case=False,
        regex=True,
        na=False,
    )

    candidates = presentation[
        is_statement_role & is_operations_statement & is_total_revenue_label
    ]

    if len(candidates) != 1:
        raise RuntimeError(
            "לא נמצאה שורת Presentation יחידה וחד-משמעית של "
            "'Total revenues' בתוך Statement role של Operations.\n"
            f"מספר התאמות: {len(candidates)}"
        )

    row = candidates.iloc[0]

    return {
        "role_uri": str(row["role_uri"]),
        "role_definition": str(row["role_definition"]),
        "concept_qname": str(row["concept_qname"]),
        "label": str(row["label"]),
        "period_type": str(row["period_type"]),
    }


def arelle_child_worker(
    primary_document: str,
    cache_directory: str,
    log_file: str,
    http_user_agent: str,
    internet_timeout_seconds: int,
    target_concept_qname: str,
    target_role_uri: str,
    target_role_definition: str,
    target_label: str,
    expected_cik: int,
    report_date: str,
    candidates_csv: str,
    result_file: str,
) -> None:
    """
    Runs in a separate child process (same bounding pattern as 37c) so a
    hang cannot block the parent. Extracts only facts tagged with the
    already-identified concept, filters them to a single unambiguous
    reported value, and writes both the full candidate table and the
    final result to disk.
    """

    result: dict[str, object] = {
        "status": "FAIL",
        "error": None,
        "target_concept_qname": target_concept_qname,
        "target_role_uri": target_role_uri,
        "target_role_definition": target_role_definition,
        "target_label": target_label,
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
                        # XBRL duration end dates are exclusive (a point
                        # in time at the start of the following day), so
                        # the last actually-reported day is end - 1 day.
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
                "לא נמצא אף Fact עם ה-concept הנדרש בהגשה הנעולה."
            )
        else:
            filtered = candidates[candidates["all_filters_ok"]].copy()
            result["filtered_fact_count"] = int(len(filtered))

            if filtered.empty:
                result["status"] = "REVIEW_REQUIRED"
                result["error"] = (
                    "נמצאו facts עם ה-concept הנדרש, אך אף אחד לא עמד "
                    "בכל תנאי הסינון (unit / ללא dimensions / תאריך "
                    "סיום תואם / משך שנתי / CIK תואם)."
                )
            else:
                distinct_values = sorted(
                    set(filtered["value_numeric"].tolist())
                )
                result["distinct_value_count"] = len(distinct_values)

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
                            f"{len(filtered)} facts עברו את הסינון אך "
                            "כולם בעלי אותו ערך — טופלו ככפילות טכנית "
                            "(תופעה מוכרת מ-Inline XBRL)."
                        )
                else:
                    result["status"] = "REVIEW_REQUIRED"
                    result["error"] = (
                        "יותר ממועמד אחד עבר את הסינון עם ערכים שונים "
                        f"({distinct_values}) — אין בסיס לבחור אוטומטית."
                    )

    except Exception as exc:
        result["status"] = "FAIL"
        result["error"] = f"{exc}\n{traceback.format_exc()}"

    Path(result_file).write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def write_review_required_result(reason: str) -> dict[str, object]:
    result = {
        "status": "REVIEW_REQUIRED",
        "error": reason,
    }

    RESULT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return result


def run_extraction() -> dict[str, object]:
    locked_filing = load_locked_filing()

    log_line("=" * 100)
    log_line("ORACLE 2024 — REVENUE FACT EXTRACTION")
    log_line("=" * 100)

    try:
        target_row = select_target_presentation_row()
    except (FileNotFoundError, RuntimeError) as exc:
        log_line(f"בחירת שורת ה-Presentation נכשלה: {exc}")
        log_line("סטטוס סופי: REVIEW_REQUIRED (לפני הרצת Arelle)")
        return write_review_required_result(str(exc))

    log_line(f"קובץ 10-K: {locked_filing['primary_document_path']}")
    log_line(f"Accession: {locked_filing['accession_compact']}")
    log_line(
        f"Concept יעד: {target_row['concept_qname']} | "
        f"Role: {target_row['role_definition']}"
    )
    log_line(
        f"Total timeout: {TOTAL_TIMEOUT_SECONDS}s | "
        f"Per-connection timeout: {INTERNET_TIMEOUT_SECONDS}s"
    )

    if RESULT_FILE.exists():
        RESULT_FILE.unlink()

    process = multiprocessing.Process(
        target=arelle_child_worker,
        kwargs={
            "primary_document": str(
                locked_filing["primary_document_path"]
            ),
            "cache_directory": str(CACHE_DIR),
            "log_file": str(ARELLE_LOG_FILE),
            "http_user_agent": locked_filing["sec_user_agent"],
            "internet_timeout_seconds": INTERNET_TIMEOUT_SECONDS,
            "target_concept_qname": target_row["concept_qname"],
            "target_role_uri": target_row["role_uri"],
            "target_role_definition": target_row["role_definition"],
            "target_label": target_row["label"],
            "expected_cik": locked_filing["cik"],
            "report_date": locked_filing["report_date"],
            "candidates_csv": str(CANDIDATES_CSV),
            "result_file": str(RESULT_FILE),
        },
    )

    run_started_at = datetime.now(timezone.utc)
    start_perf = time.perf_counter()

    log_line("מפעיל child process...")
    process.start()

    process.join(timeout=TOTAL_TIMEOUT_SECONDS)

    timed_out = False

    if process.is_alive():
        timed_out = True
        log_line(
            f"חריגה מ-{TOTAL_TIMEOUT_SECONDS} שניות — "
            "שולח terminate() ל-child process."
        )
        process.terminate()
        process.join(timeout=TERMINATE_GRACE_SECONDS)

        if process.is_alive():
            log_line("terminate() לא הספיק — שולח kill().")
            process.kill()
            process.join(timeout=TERMINATE_GRACE_SECONDS)

    elapsed_seconds = time.perf_counter() - start_perf
    run_ended_at = datetime.now(timezone.utc)

    child_exit_code = process.exitcode
    log_line(f"Child process הסתיים. exit_code={child_exit_code}")

    child_result: dict[str, object] = {}

    if RESULT_FILE.exists():
        child_result = json.loads(RESULT_FILE.read_text(encoding="utf-8"))

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

    final_result = {
        "ticker": locked_filing["ticker"],
        "cik": locked_filing["cik"],
        "form": EXPECTED_FORM,
        "accession_number": locked_filing["accession_number"],
        "accession_compact": locked_filing["accession_compact"],
        "report_date": locked_filing["report_date"],
        "filing_date": locked_filing["filing_date"],
        "source_document": EXPECTED_PRIMARY_DOCUMENT,
        "primary_document_path": str(
            locked_filing["primary_document_path"]
        ),
        "manifest_file": str(locked_filing["manifest_file"]),
        "source_concept": target_row["concept_qname"],
        "statement_role_uri": target_row["role_uri"],
        "statement_role_definition": target_row["role_definition"],
        "label": target_row["label"],
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
        "candidates_csv": (
            str(CANDIDATES_CSV) if CANDIDATES_CSV.exists() else None
        ),
        "arelle_log_file": str(ARELLE_LOG_FILE),
        "orchestration_log_file": str(ORCHESTRATION_LOG_FILE),
    }

    RESULT_FILE.write_text(
        json.dumps(final_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log_line(f"סטטוס סופי: {status}")
    log_line(f"קובץ תוצאה: {RESULT_FILE}")

    return final_result


def main() -> None:
    result = run_extraction()

    print()
    print("=" * 100)
    print("תוצאת חילוץ Total Revenue — Oracle 2024")
    print("=" * 100)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
