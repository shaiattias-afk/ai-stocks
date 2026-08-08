"""
TASK_143 — a reusable, fail-closed task-marker utility, mandatory going
forward for every TASK_NNN operation in this project.

WHY: TASK_142 proved TASK_141 had two process defects — (1) a hand-typed,
never-clock-derived STARTED timestamp, and (2) a RESULT completion
timestamp taken from local time with a literal 'Z' (UTC designator)
incorrectly appended (a PowerShell `Get-Date -Format "...Z"` pattern that
looks like UTC but isn't). A written rule was not enough to prevent this
class of defect; this module makes it structurally impossible for a
caller of `start_task`/`finish_task`/`fail_task` to ever supply a
timestamp, hand-typed or otherwise — every timestamp here is read
internally, exactly once, from `datetime.now(timezone.utc)`.

Public API:
  start_task(task_id, objective, expected_outputs, read_only=True, tasks_dir=None) -> dict
  finish_task(task_id, mandatory_outputs, status="PASS", extra_fields=None, tasks_dir=None) -> dict
  fail_task(task_id, error_message, tasks_dir=None) -> dict
  validate_task_evidence(task_id, tasks_dir=None) -> dict

All three write functions:
  - obtain every timestamp internally via `datetime.now(timezone.utc)` —
    no function in this module accepts a timestamp parameter, anywhere,
    including in private helpers used by the public API.
  - write ISO-8601 UTC, always ending in 'Z'.
  - write atomically: temp file in the same directory, then `os.replace`.
  - re-read and validate what was actually written before returning it.

No production database or warehouse file is ever touched by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TASKS_DIR = PROJECT_DIR / "docs" / "tasks"

UTILITY_VERSION = "142_task_marker_guard-1.0.0"

VALID_STATUSES = {"PASS", "FAIL", "INCOMPLETE"}

FAILURE_CATEGORIES = [
    "STARTED_ALREADY_EXISTS", "RESULT_ALREADY_EXISTS", "STARTED_MISSING",
    "INVALID_TIMESTAMP_FORMAT", "NON_UTC_TIMESTAMP", "COMPLETION_BEFORE_START",
    "REQUIRED_OUTPUT_MISSING", "REQUIRED_OUTPUT_UNREADABLE", "OUTPUT_HASH_MISMATCH",
    "TASK_ID_MISMATCH", "INVALID_STATUS", "ATOMIC_WRITE_FAILED", "EVIDENCE_VALIDATION_FAILED",
    # additional categories, beyond the required minimum, used only by validate_task_evidence
    "DUPLICATE_EVIDENCE_FILES", "RESULT_BEFORE_STARTED_ON_DISK", "REQUIRED_OUTPUT_LIST_MISMATCH",
]


class TaskMarkerError(Exception):
    """Raised by every write function in this module. Always carries an
    exact category from FAILURE_CATEGORIES."""

    def __init__(self, category: str, message: str) -> None:
        if category not in FAILURE_CATEGORIES:
            raise ValueError(f"Unknown TaskMarkerError category: {category!r}")
        self.category = category
        super().__init__(f"[{category}] {message}")


# =====================================================================
# INTERNAL — the ONLY place any timestamp is produced. No parameter of
# any public or private function in this module ever accepts a caller-
# supplied timestamp to use as started_at/completed_at.
# =====================================================================

def _utc_now_iso() -> str:
    """The single UTC clock source for this entire module:
    datetime.now(timezone.utc), formatted ISO-8601 with millisecond
    precision, always ending in 'Z'. Never local time; never a bare
    Get-Date/datetime.now() without an explicit UTC conversion."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_utc_iso(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TaskMarkerError("NON_UTC_TIMESTAMP", f"timestamp does not end in 'Z': {value!r}")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise TaskMarkerError("INVALID_TIMESTAMP_FORMAT", f"could not parse {value!r} as ISO-8601 UTC: {exc}") from exc


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception as exc:  # noqa: BLE001
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise TaskMarkerError("ATOMIC_WRITE_FAILED", f"failed to atomically write {path}: {exc}") from exc


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _started_path(task_id: str, tasks_dir: Path) -> Path:
    return tasks_dir / f"{task_id}_STARTED.json"


def _result_path(task_id: str, tasks_dir: Path) -> Path:
    return tasks_dir / f"{task_id}_RESULT.json"


# =====================================================================
# PUBLIC API
# =====================================================================

def start_task(task_id: str, objective: str, expected_outputs: list[str],
                read_only: bool = True, tasks_dir: Path | None = None) -> dict:
    tasks_dir = Path(tasks_dir) if tasks_dir else DEFAULT_TASKS_DIR
    started_path = _started_path(task_id, tasks_dir)
    result_path = _result_path(task_id, tasks_dir)

    if started_path.exists():
        raise TaskMarkerError("STARTED_ALREADY_EXISTS", f"{started_path} already exists")
    if result_path.exists():
        raise TaskMarkerError("RESULT_ALREADY_EXISTS", f"{result_path} already exists (cannot start over a completed task)")

    marker = {
        "task_id": task_id, "status": "STARTED", "started_at": _utc_now_iso(),
        "objective": objective, "read_only": bool(read_only), "expected_outputs": list(expected_outputs),
        "process_id": os.getpid(), "hostname": socket.gethostname(), "utility_version": UTILITY_VERSION,
    }
    _atomic_write_json(started_path, marker)

    reread = _read_json(started_path)
    if reread != marker:
        raise TaskMarkerError("ATOMIC_WRITE_FAILED", f"re-read STARTED file does not match what was written: {started_path}")
    return reread


def finish_task(task_id: str, mandatory_outputs: list[str], status: str = "PASS",
                 extra_fields: dict | None = None, tasks_dir: Path | None = None) -> dict:
    tasks_dir = Path(tasks_dir) if tasks_dir else DEFAULT_TASKS_DIR
    started_path = _started_path(task_id, tasks_dir)
    result_path = _result_path(task_id, tasks_dir)

    if not started_path.exists():
        raise TaskMarkerError("STARTED_MISSING", f"{started_path} does not exist — cannot finish a task that was never started")
    if result_path.exists():
        raise TaskMarkerError("RESULT_ALREADY_EXISTS", f"{result_path} already exists — finish_task never overwrites an existing RESULT")

    started = _read_json(started_path)
    if started.get("task_id") != task_id:
        raise TaskMarkerError("TASK_ID_MISMATCH", f"STARTED file task_id={started.get('task_id')!r} != requested {task_id!r}")

    started_at_str = started.get("started_at")
    started_at = _parse_utc_iso(started_at_str)
    completed_at_str = _utc_now_iso()
    completed_at = _parse_utc_iso(completed_at_str)

    if completed_at < started_at:
        raise TaskMarkerError("COMPLETION_BEFORE_START", f"completed_at {completed_at_str} < started_at {started_at_str}")

    if status not in VALID_STATUSES:
        raise TaskMarkerError("INVALID_STATUS", f"status {status!r} not in {sorted(VALID_STATUSES)}")

    outputs_evidence: dict[str, dict] = {}
    problems: list[tuple[str, str]] = []
    for output_path_str in mandatory_outputs:
        output_path = (PROJECT_DIR / output_path_str) if not Path(output_path_str).is_absolute() else Path(output_path_str)
        if not output_path.exists():
            problems.append(("REQUIRED_OUTPUT_MISSING", f"missing mandatory output: {output_path_str}"))
            continue
        try:
            sha256 = _sha256_of_file(output_path)
            size_bytes = output_path.stat().st_size
            modified_at = datetime.fromtimestamp(output_path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        except OSError as exc:
            problems.append(("REQUIRED_OUTPUT_UNREADABLE", f"could not read mandatory output {output_path_str}: {exc}"))
            continue
        outputs_evidence[output_path_str] = {"sha256": sha256, "size_bytes": size_bytes, "modified_at_utc": modified_at}

    # --- forbid PASS when any mandatory output is missing or invalid ---
    if status == "PASS" and problems:
        category, message = problems[0]
        raise TaskMarkerError(category, f"cannot write status=PASS: {message} (and {len(problems) - 1} more problem(s))" if len(problems) > 1 else message)

    result = {
        "task_id": task_id, "status": status,
        "started_at": started_at_str, "completed_at": completed_at_str,
        "elapsed_seconds": round((completed_at - started_at).total_seconds(), 3),
        "mandatory_outputs": list(mandatory_outputs), "outputs": outputs_evidence,
        "output_problems": [{"category": c, "message": m} for c, m in problems],
        "utility_version": UTILITY_VERSION,
    }
    if extra_fields:
        for key, value in extra_fields.items():
            if key not in result:
                result[key] = value

    _atomic_write_json(result_path, result)
    reread = _read_json(result_path)
    if reread != result:
        raise TaskMarkerError("ATOMIC_WRITE_FAILED", f"re-read RESULT file does not match what was written: {result_path}")
    return reread


def fail_task(task_id: str, error_message: str, tasks_dir: Path | None = None) -> dict:
    tasks_dir = Path(tasks_dir) if tasks_dir else DEFAULT_TASKS_DIR
    started_path = _started_path(task_id, tasks_dir)
    result_path = _result_path(task_id, tasks_dir)

    if not started_path.exists():
        raise TaskMarkerError("STARTED_MISSING", f"{started_path} does not exist — cannot fail a task that was never started")
    if result_path.exists():
        raise TaskMarkerError("RESULT_ALREADY_EXISTS", f"{result_path} already exists — fail_task never silently overwrites an existing RESULT")

    started = _read_json(started_path)
    if started.get("task_id") != task_id:
        raise TaskMarkerError("TASK_ID_MISMATCH", f"STARTED file task_id={started.get('task_id')!r} != requested {task_id!r}")

    started_at_str = started.get("started_at")
    started_at = _parse_utc_iso(started_at_str)
    completed_at_str = _utc_now_iso()
    completed_at = _parse_utc_iso(completed_at_str)
    if completed_at < started_at:
        raise TaskMarkerError("COMPLETION_BEFORE_START", f"completed_at {completed_at_str} < started_at {started_at_str}")

    result = {
        "task_id": task_id, "status": "FAIL",
        "started_at": started_at_str, "completed_at": completed_at_str,
        "elapsed_seconds": round((completed_at - started_at).total_seconds(), 3),
        "error": str(error_message), "utility_version": UTILITY_VERSION,
    }
    _atomic_write_json(result_path, result)
    reread = _read_json(result_path)
    if reread != result:
        raise TaskMarkerError("ATOMIC_WRITE_FAILED", f"re-read RESULT file does not match what was written: {result_path}")
    return reread


def validate_task_evidence(task_id: str, tasks_dir: Path | None = None) -> dict:
    """Deterministic, read-only re-validation of a task's saved STARTED +
    RESULT evidence. Returns {"valid": bool, "failure_categories": [...],
    "detail": {...}} — never raises for an invalid-but-inspectable state
    (only for I/O errors reading the marker files themselves, which are
    reported as EVIDENCE_VALIDATION_FAILED)."""
    tasks_dir = Path(tasks_dir) if tasks_dir else DEFAULT_TASKS_DIR
    failure_categories: list[str] = []
    detail: dict = {}

    started_path = _started_path(task_id, tasks_dir)
    result_path = _result_path(task_id, tasks_dir)

    # duplicate-evidence check: more than one file matching the naming pattern
    started_matches = sorted(tasks_dir.glob(f"{task_id}_STARTED*.json"))
    result_matches = sorted(tasks_dir.glob(f"{task_id}_RESULT*.json"))
    if len(started_matches) > 1 or len(result_matches) > 1:
        failure_categories.append("DUPLICATE_EVIDENCE_FILES")
    detail["started_matches"] = [str(p) for p in started_matches]
    detail["result_matches"] = [str(p) for p in result_matches]

    if not started_path.exists():
        failure_categories.append("STARTED_MISSING")
        return {"valid": False, "failure_categories": failure_categories, "detail": detail}

    try:
        started = _read_json(started_path)
    except (OSError, json.JSONDecodeError) as exc:
        failure_categories.append("EVIDENCE_VALIDATION_FAILED")
        detail["started_read_error"] = str(exc)
        return {"valid": False, "failure_categories": failure_categories, "detail": detail}

    if not result_path.exists():
        failure_categories.append("EVIDENCE_VALIDATION_FAILED")
        detail["reason"] = "RESULT file does not exist yet"
        return {"valid": False, "failure_categories": failure_categories, "detail": detail}

    try:
        result = _read_json(result_path)
    except (OSError, json.JSONDecodeError) as exc:
        failure_categories.append("EVIDENCE_VALIDATION_FAILED")
        detail["result_read_error"] = str(exc)
        return {"valid": False, "failure_categories": failure_categories, "detail": detail}

    # task_id match
    if started.get("task_id") != task_id or result.get("task_id") != task_id or started.get("task_id") != result.get("task_id"):
        failure_categories.append("TASK_ID_MISMATCH")
    detail["started_task_id"] = started.get("task_id")
    detail["result_task_id"] = result.get("task_id")

    # status
    status = result.get("status")
    if status not in VALID_STATUSES:
        failure_categories.append("INVALID_STATUS")
    detail["status"] = status

    # timestamp format + UTC + ordering
    started_at_str = started.get("started_at")
    completed_at_str = result.get("completed_at")
    started_at_dt = completed_at_dt = None
    try:
        started_at_dt = _parse_utc_iso(started_at_str) if started_at_str else None
    except TaskMarkerError as exc:
        failure_categories.append(exc.category)
    try:
        completed_at_dt = _parse_utc_iso(completed_at_str) if completed_at_str else None
    except TaskMarkerError as exc:
        failure_categories.append(exc.category)

    if started_at_dt and completed_at_dt and completed_at_dt < started_at_dt:
        failure_categories.append("COMPLETION_BEFORE_START")
    detail["started_at"] = started_at_str
    detail["completed_at"] = completed_at_str

    # STARTED existed before RESULT — filesystem mtime as independent evidence
    try:
        started_mtime = started_path.stat().st_mtime
        result_mtime = result_path.stat().st_mtime
        detail["started_file_mtime_utc"] = datetime.fromtimestamp(started_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        detail["result_file_mtime_utc"] = datetime.fromtimestamp(result_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if result_mtime < started_mtime:
            failure_categories.append("RESULT_BEFORE_STARTED_ON_DISK")
    except OSError as exc:
        failure_categories.append("EVIDENCE_VALIDATION_FAILED")
        detail["mtime_check_error"] = str(exc)

    # required output list + hash/size verification (only meaningful for PASS/FAIL with outputs).
    # Paths are normalized to resolved absolute strings before comparing, since
    # expected_outputs (written at start_task time, often relative) and the
    # RESULT's recorded outputs (written at finish_task time, often absolute)
    # may legitimately use different but equivalent path spellings for the
    # same file. The RESULT file's own path is exempt from the "must be
    # reported" check -- a RESULT cannot meaningfully certify its own hash
    # from inside itself (the value would not yet be known at write time).
    def _normalize(path_str: str) -> str:
        p = Path(path_str)
        p = p if p.is_absolute() else (PROJECT_DIR / p)
        return str(p.resolve())

    expected_outputs = {_normalize(p) for p in started.get("expected_outputs", [])}
    reported_outputs = {_normalize(p) for p in result.get("outputs", {}).keys()}
    expected_outputs_excluding_self = expected_outputs - {str(result_path.resolve())}
    if status == "PASS" and not expected_outputs_excluding_self.issubset(reported_outputs):
        failure_categories.append("REQUIRED_OUTPUT_LIST_MISMATCH")
    detail["expected_outputs"] = sorted(expected_outputs)
    detail["reported_outputs"] = sorted(reported_outputs)

    for output_path_str, evidence in result.get("outputs", {}).items():
        output_path = (PROJECT_DIR / output_path_str) if not Path(output_path_str).is_absolute() else Path(output_path_str)
        if not output_path.exists():
            failure_categories.append("REQUIRED_OUTPUT_MISSING")
            continue
        try:
            actual_hash = _sha256_of_file(output_path)
            actual_size = output_path.stat().st_size
        except OSError:
            failure_categories.append("REQUIRED_OUTPUT_UNREADABLE")
            continue
        if actual_hash != evidence.get("sha256") or actual_size != evidence.get("size_bytes"):
            failure_categories.append("OUTPUT_HASH_MISMATCH")

    failure_categories = sorted(set(failure_categories))
    valid = len(failure_categories) == 0
    return {"valid": valid, "failure_categories": failure_categories, "detail": detail}
