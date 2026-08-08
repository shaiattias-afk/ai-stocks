from __future__ import annotations

import csv
import json
import multiprocessing
import time
import traceback
from datetime import datetime, timezone
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

OUTPUT_CSV = DATA_DIR / "orcl_2024_arelle_bounded_presentation.csv"
ARELLE_LOG_FILE = DATA_DIR / "orcl_2024_arelle_bounded_child.log"
CHILD_RESULT_FILE = DATA_DIR / "orcl_2024_arelle_bounded_child_result.json"
ORCHESTRATION_LOG_FILE = DATA_DIR / "orcl_2024_arelle_bounded_orchestration.log"
SUMMARY_FILE = DATA_DIR / "orcl_2024_arelle_bounded_summary.json"

# Bounding configuration. These are the only numbers that should be
# tuned between runs; nothing else in this script is Oracle-specific
# beyond locating the already-locked filing.
TOTAL_TIMEOUT_SECONDS = 240
INTERNET_TIMEOUT_SECONDS = 20
TERMINATE_GRACE_SECONDS = 5


def find_locked_manifest() -> Path:
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
            matching_manifests.append(manifest_file)

    if len(matching_manifests) != 1:
        raise RuntimeError(
            "לא נמצא Manifest יחיד וברור של Oracle 2024.\n"
            f"מספר התאמות: {len(matching_manifests)}"
        )

    return matching_manifests[0]


def load_locked_filing() -> dict[str, Any]:
    manifest_file = find_locked_manifest()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

    accession_compact = str(
        manifest.get("accession_number", "")
    ).replace("-", "")

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
            "קובץ ה-10-K הראשי לא נמצא:\n"
            f"{primary_document_path}"
        )

    sec_user_agent = str(manifest.get("sec_user_agent", "")).strip()

    if not sec_user_agent:
        raise RuntimeError(
            "לא נמצא sec_user_agent ב-Manifest הנעול."
        )

    return {
        "manifest_file": manifest_file,
        "primary_document_path": primary_document_path,
        "accession_compact": accession_compact,
        "report_date": manifest.get("report_date"),
        "sec_user_agent": sec_user_agent,
    }


def log_line(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"[{timestamp}] {message}"

    print(line)

    with ORCHESTRATION_LOG_FILE.open(
        mode="a",
        encoding="utf-8",
    ) as log_file:
        log_file.write(line + "\n")


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


def _find_revenue_candidates(presentation: pd.DataFrame) -> pd.DataFrame:
    combined_text = (
        presentation["role_definition"].fillna("")
        + " "
        + presentation["label"].fillna("")
        + " "
        + presentation["concept_qname"].fillna("")
    )

    return presentation[
        combined_text.str.contains(
            "revenue|revenues|sales",
            case=False,
            regex=True,
            na=False,
        )
    ].copy()


def arelle_child_worker(
    primary_document: str,
    cache_directory: str,
    log_file: str,
    output_csv: str,
    result_file: str,
    http_user_agent: str,
    internet_timeout_seconds: int,
) -> None:
    """
    Runs entirely inside a separate child process so the parent process
    can enforce a hard total-runtime limit and kill this process if it
    hangs, instead of the whole script hanging as happened previously
    with an unbounded online Arelle run.
    """

    result: dict[str, object] = {
        "status": "FAIL",
        "error": None,
        "presentation_row_count": 0,
        "revenue_candidate_count": 0,
        "unique_role_count": 0,
    }

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
                raise RuntimeError(
                    "Arelle לא הצליח לטעון את מודל ה-XBRL."
                )

            presentation = _extract_presentation(model_xbrl)

        if presentation.empty:
            raise RuntimeError(
                "לא נמצאו Presentation relationships."
            )

        presentation.to_csv(
            output_csv,
            index=False,
            encoding="utf-8-sig",
            quoting=csv.QUOTE_MINIMAL,
        )

        revenue_candidates = _find_revenue_candidates(presentation)

        result["presentation_row_count"] = int(len(presentation))
        result["unique_role_count"] = int(
            presentation["role_uri"].nunique()
        )
        result["revenue_candidate_count"] = int(len(revenue_candidates))
        result["output_csv"] = output_csv

        if revenue_candidates.empty:
            result["status"] = "REVIEW_REQUIRED"
            result["error"] = (
                "הדוח נטען ו-Presentation חולץ בהצלחה, אך לא נמצא "
                "אף concept עם Revenue/Sales בתווית, בשם ה-concept "
                "או בהגדרת ה-Role. אין בסיס לבחור שורת הכנסות."
            )
        else:
            result["status"] = "PASS"

    except Exception as exc:
        result["status"] = "FAIL"
        result["error"] = f"{exc}\n{traceback.format_exc()}"

    Path(result_file).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_bounded_test() -> dict[str, object]:
    locked_filing = load_locked_filing()

    if CHILD_RESULT_FILE.exists():
        CHILD_RESULT_FILE.unlink()

    log_line("=" * 100)
    log_line("ARELLE — LOCKED ORACLE 2024 BOUNDED ONLINE TEST")
    log_line("=" * 100)
    log_line(f"קובץ 10-K: {locked_filing['primary_document_path']}")
    log_line(f"Accession: {locked_filing['accession_compact']}")
    log_line(f"User-Agent: {locked_filing['sec_user_agent']}")
    log_line(f"Cache directory: {CACHE_DIR}")
    log_line(
        f"Total timeout: {TOTAL_TIMEOUT_SECONDS}s | "
        f"Per-connection timeout: {INTERNET_TIMEOUT_SECONDS}s"
    )

    process = multiprocessing.Process(
        target=arelle_child_worker,
        kwargs={
            "primary_document": str(
                locked_filing["primary_document_path"]
            ),
            "cache_directory": str(CACHE_DIR),
            "log_file": str(ARELLE_LOG_FILE),
            "output_csv": str(OUTPUT_CSV),
            "result_file": str(CHILD_RESULT_FILE),
            "http_user_agent": locked_filing["sec_user_agent"],
            "internet_timeout_seconds": INTERNET_TIMEOUT_SECONDS,
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

    if CHILD_RESULT_FILE.exists():
        child_result = json.loads(
            CHILD_RESULT_FILE.read_text(encoding="utf-8")
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

    summary = {
        "ticker": "ORCL",
        "report_date": locked_filing["report_date"],
        "accession_compact": locked_filing["accession_compact"],
        "primary_document": str(
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
        "status": status,
        "error": error,
        "presentation_row_count": child_result.get(
            "presentation_row_count", 0
        ),
        "unique_role_count": child_result.get("unique_role_count", 0),
        "revenue_candidate_count": child_result.get(
            "revenue_candidate_count", 0
        ),
        "output_csv": str(OUTPUT_CSV) if OUTPUT_CSV.exists() else None,
        "arelle_log_file": str(ARELLE_LOG_FILE),
        "orchestration_log_file": str(ORCHESTRATION_LOG_FILE),
    }

    SUMMARY_FILE.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log_line(f"סטטוס סופי: {status}")
    log_line(f"קובץ סיכום: {SUMMARY_FILE}")

    return summary


def main() -> None:
    summary = run_bounded_test()

    print()
    print("=" * 100)
    print("סיכום ההרצה המוגבלת")
    print("=" * 100)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if summary["status"] == "PASS" and summary["output_csv"]:
        presentation = pd.read_csv(
            summary["output_csv"],
            dtype=str,
            keep_default_na=False,
        )
        revenue_candidates = _find_revenue_candidates(presentation)

        print()
        print("=" * 100)
        print("Revenue / Sales candidates")
        print("=" * 100)
        print(
            revenue_candidates[
                [
                    "role_definition",
                    "depth",
                    "parent_qname",
                    "concept_qname",
                    "label",
                    "is_abstract",
                    "period_type",
                ]
            ].to_string(index=False)
        )


if __name__ == "__main__":
    main()
