"""
Validation harness for scripts/142_task_marker_guard.py. Runs the 12
required tests, each inside its own isolated temporary test directory
(never touching docs/tasks/ or any production file), and writes
data/task_marker_guard_validation.json / .csv.

Does not modify scripts/142. Does not touch either production database.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
SCRATCH_ROOT = Path(r"C:\Users\shai\AppData\Local\Temp\claude\c--AI-Stock-Agent\bb0cbcf5-0be3-4efa-b2c0-fce0377906de\scratchpad") / "task143_tests"

JSON_OUTPUT_PATH = DATA_DIR / "task_marker_guard_validation.json"
CSV_OUTPUT_PATH = DATA_DIR / "task_marker_guard_validation.csv"

_spec = importlib.util.spec_from_file_location("s142", PROJECT_DIR / "scripts" / "142_task_marker_guard.py")
s142 = importlib.util.module_from_spec(_spec)
sys.modules["s142"] = s142
_spec.loader.exec_module(s142)


def fresh_dir(name: str) -> Path:
    d = SCRATCH_ROOT / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_output_file(dir_path: Path, name: str, content: str = "test content") -> Path:
    p = dir_path / name
    p.write_text(content, encoding="utf-8")
    return p


def run_test_1_normal_flow() -> dict:
    d = fresh_dir("test1_normal")
    task_id = "TEST_1_NORMAL"
    output = make_output_file(d, "output.txt")
    s142.start_task(task_id, "normal flow test", expected_outputs=[str(output)], tasks_dir=d)
    result = s142.finish_task(task_id, mandatory_outputs=[str(output)], status="PASS", tasks_dir=d)
    validation = s142.validate_task_evidence(task_id, tasks_dir=d)
    ok = result["status"] == "PASS" and validation["valid"] is True
    return {"test_name": "1_normal_started_outputs_pass", "expected_result": "PASS result written and self-validates",
            "actual_result": f"status={result['status']} validation_valid={validation['valid']}",
            "failure_category": None if ok else ";".join(validation["failure_categories"]),
            "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_2_duplicate_started() -> dict:
    d = fresh_dir("test2_dup_started")
    task_id = "TEST_2_DUP"
    s142.start_task(task_id, "dup test", expected_outputs=[], tasks_dir=d)
    try:
        s142.start_task(task_id, "dup test", expected_outputs=[], tasks_dir=d)
        return {"test_name": "2_duplicate_started_fails", "expected_result": "STARTED_ALREADY_EXISTS raised",
                "actual_result": "no exception raised", "failure_category": None, "status": "FAIL", "evidence_paths": [str(d)]}
    except s142.TaskMarkerError as exc:
        ok = exc.category == "STARTED_ALREADY_EXISTS"
        return {"test_name": "2_duplicate_started_fails", "expected_result": "STARTED_ALREADY_EXISTS raised",
                "actual_result": f"raised {exc.category}", "failure_category": exc.category,
                "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_3_result_without_started() -> dict:
    d = fresh_dir("test3_no_started")
    task_id = "TEST_3_NO_STARTED"
    try:
        s142.finish_task(task_id, mandatory_outputs=[], status="PASS", tasks_dir=d)
        return {"test_name": "3_result_without_started_fails", "expected_result": "STARTED_MISSING raised",
                "actual_result": "no exception raised", "failure_category": None, "status": "FAIL", "evidence_paths": [str(d)]}
    except s142.TaskMarkerError as exc:
        ok = exc.category == "STARTED_MISSING"
        return {"test_name": "3_result_without_started_fails", "expected_result": "STARTED_MISSING raised",
                "actual_result": f"raised {exc.category}", "failure_category": exc.category,
                "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_4_missing_output_prevents_pass() -> dict:
    d = fresh_dir("test4_missing_output")
    task_id = "TEST_4_MISSING_OUTPUT"
    missing = d / "does_not_exist.txt"
    s142.start_task(task_id, "missing output test", expected_outputs=[str(missing)], tasks_dir=d)
    try:
        s142.finish_task(task_id, mandatory_outputs=[str(missing)], status="PASS", tasks_dir=d)
        return {"test_name": "4_missing_output_prevents_pass", "expected_result": "REQUIRED_OUTPUT_MISSING raised, no RESULT file written",
                "actual_result": "no exception raised", "failure_category": None, "status": "FAIL", "evidence_paths": [str(d)]}
    except s142.TaskMarkerError as exc:
        result_written = (d / f"{task_id}_RESULT.json").exists()
        ok = exc.category == "REQUIRED_OUTPUT_MISSING" and not result_written
        return {"test_name": "4_missing_output_prevents_pass", "expected_result": "REQUIRED_OUTPUT_MISSING raised, no RESULT file written",
                "actual_result": f"raised {exc.category}; result_file_written={result_written}", "failure_category": exc.category,
                "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_5_unreadable_output_prevents_pass() -> dict:
    d = fresh_dir("test5_unreadable_output")
    task_id = "TEST_5_UNREADABLE"
    # a directory "exists" but cannot be opened/read as a file -> triggers the unreadable path
    fake_output_dir = d / "this_is_a_directory_not_a_file"
    fake_output_dir.mkdir()
    s142.start_task(task_id, "unreadable output test", expected_outputs=[str(fake_output_dir)], tasks_dir=d)
    try:
        s142.finish_task(task_id, mandatory_outputs=[str(fake_output_dir)], status="PASS", tasks_dir=d)
        return {"test_name": "5_unreadable_output_prevents_pass", "expected_result": "REQUIRED_OUTPUT_UNREADABLE raised",
                "actual_result": "no exception raised", "failure_category": None, "status": "FAIL", "evidence_paths": [str(d)]}
    except s142.TaskMarkerError as exc:
        ok = exc.category == "REQUIRED_OUTPUT_UNREADABLE"
        return {"test_name": "5_unreadable_output_prevents_pass", "expected_result": "REQUIRED_OUTPUT_UNREADABLE raised",
                "actual_result": f"raised {exc.category}", "failure_category": exc.category,
                "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_6_hash_mismatch_detected() -> dict:
    d = fresh_dir("test6_hash_mismatch")
    task_id = "TEST_6_HASH_MISMATCH"
    output = make_output_file(d, "output.txt", content="ORIGINAL CONTENT")
    s142.start_task(task_id, "hash mismatch test", expected_outputs=[str(output)], tasks_dir=d)
    s142.finish_task(task_id, mandatory_outputs=[str(output)], status="PASS", tasks_dir=d)
    # tamper the output AFTER the RESULT was written and hashed
    output.write_text("TAMPERED CONTENT", encoding="utf-8")
    validation = s142.validate_task_evidence(task_id, tasks_dir=d)
    ok = validation["valid"] is False and "OUTPUT_HASH_MISMATCH" in validation["failure_categories"]
    return {"test_name": "6_hash_mismatch_detected", "expected_result": "validate_task_evidence reports OUTPUT_HASH_MISMATCH",
            "actual_result": f"valid={validation['valid']} categories={validation['failure_categories']}",
            "failure_category": "OUTPUT_HASH_MISMATCH" if ok else ";".join(validation["failure_categories"]),
            "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_7_task_id_mismatch_detected() -> dict:
    d = fresh_dir("test7_task_id_mismatch")
    orig_id, alias_id = "TASK_7_ORIG", "TASK_7_ALIAS"
    s142.start_task(orig_id, "task id mismatch test", expected_outputs=[], tasks_dir=d)
    # copy the STARTED file under a different filename (simulating a foreign/misnamed
    # marker whose internal task_id does not match the name it's being looked up by)
    orig_started = d / f"{orig_id}_STARTED.json"
    alias_started = d / f"{alias_id}_STARTED.json"
    shutil.copyfile(orig_started, alias_started)
    try:
        s142.finish_task(alias_id, mandatory_outputs=[], status="PASS", tasks_dir=d)
        return {"test_name": "7_task_id_mismatch_detected", "expected_result": "TASK_ID_MISMATCH raised",
                "actual_result": "no exception raised", "failure_category": None, "status": "FAIL", "evidence_paths": [str(d)]}
    except s142.TaskMarkerError as exc:
        ok = exc.category == "TASK_ID_MISMATCH"
        return {"test_name": "7_task_id_mismatch_detected", "expected_result": "TASK_ID_MISMATCH raised",
                "actual_result": f"raised {exc.category}", "failure_category": exc.category,
                "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_8_non_utc_timestamp_rejected() -> dict:
    d = fresh_dir("test8_non_utc_timestamp")
    task_id = "TEST_8_NON_UTC"
    s142.start_task(task_id, "non-utc timestamp test", expected_outputs=[], tasks_dir=d)
    started_path = d / f"{task_id}_STARTED.json"
    marker = json.loads(started_path.read_text(encoding="utf-8"))
    marker["started_at"] = "2026-08-05T10:00:00+03:00"  # local-offset style, does NOT end in 'Z'
    started_path.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    try:
        s142.finish_task(task_id, mandatory_outputs=[], status="PASS", tasks_dir=d)
        return {"test_name": "8_non_utc_timestamp_rejected", "expected_result": "NON_UTC_TIMESTAMP raised",
                "actual_result": "no exception raised", "failure_category": None, "status": "FAIL", "evidence_paths": [str(d)]}
    except s142.TaskMarkerError as exc:
        ok = exc.category == "NON_UTC_TIMESTAMP"
        return {"test_name": "8_non_utc_timestamp_rejected", "expected_result": "NON_UTC_TIMESTAMP raised",
                "actual_result": f"raised {exc.category}", "failure_category": exc.category,
                "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_9_completion_before_start_rejected() -> dict:
    """Uses a test fixture (directly editing the STARTED file's
    started_at to an artificial future value) rather than any clock
    injection into production code -- finish_task still obtains
    completed_at exclusively from datetime.now(timezone.utc) and has no
    parameter through which a caller could supply a different clock."""
    d = fresh_dir("test9_completion_before_start")
    task_id = "TEST_9_COMPLETION_BEFORE_START"
    s142.start_task(task_id, "completion before start test", expected_outputs=[], tasks_dir=d)
    started_path = d / f"{task_id}_STARTED.json"
    marker = json.loads(started_path.read_text(encoding="utf-8"))
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    marker["started_at"] = future
    started_path.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    try:
        s142.finish_task(task_id, mandatory_outputs=[], status="PASS", tasks_dir=d)
        return {"test_name": "9_completion_before_start_rejected", "expected_result": "COMPLETION_BEFORE_START raised",
                "actual_result": "no exception raised", "failure_category": None, "status": "FAIL", "evidence_paths": [str(d)]}
    except s142.TaskMarkerError as exc:
        ok = exc.category == "COMPLETION_BEFORE_START"
        return {"test_name": "9_completion_before_start_rejected", "expected_result": "COMPLETION_BEFORE_START raised",
                "actual_result": f"raised {exc.category}", "failure_category": exc.category,
                "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_10_existing_result_not_overwritten() -> dict:
    d = fresh_dir("test10_no_overwrite")
    task_id = "TEST_10_NO_OVERWRITE"
    s142.start_task(task_id, "no overwrite test", expected_outputs=[], tasks_dir=d)
    first = s142.finish_task(task_id, mandatory_outputs=[], status="PASS", tasks_dir=d)
    original_hash_input = json.dumps(first, sort_keys=True)
    try:
        s142.finish_task(task_id, mandatory_outputs=[], status="FAIL", tasks_dir=d)
        return {"test_name": "10_existing_result_not_overwritten", "expected_result": "RESULT_ALREADY_EXISTS raised on second finish_task",
                "actual_result": "no exception raised", "failure_category": None, "status": "FAIL", "evidence_paths": [str(d)]}
    except s142.TaskMarkerError as exc:
        result_path = d / f"{task_id}_RESULT.json"
        unchanged = json.dumps(json.loads(result_path.read_text(encoding="utf-8")), sort_keys=True) == original_hash_input
        ok = exc.category == "RESULT_ALREADY_EXISTS" and unchanged
        return {"test_name": "10_existing_result_not_overwritten", "expected_result": "RESULT_ALREADY_EXISTS raised, file unchanged",
                "actual_result": f"raised {exc.category}; file_unchanged={unchanged}", "failure_category": exc.category,
                "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_11_atomic_writes_leave_no_temp_files() -> dict:
    d = fresh_dir("test11_atomic_no_temp")
    task_id = "TEST_11_ATOMIC"
    output = make_output_file(d, "output.txt")
    s142.start_task(task_id, "atomic test", expected_outputs=[str(output)], tasks_dir=d)
    s142.finish_task(task_id, mandatory_outputs=[str(output)], status="PASS", tasks_dir=d)
    leftover_temp_files = list(d.glob("*.tmp*"))
    ok = len(leftover_temp_files) == 0
    return {"test_name": "11_atomic_writes_no_temp_files_left", "expected_result": "0 leftover *.tmp* files",
            "actual_result": f"{len(leftover_temp_files)} leftover temp files: {[p.name for p in leftover_temp_files]}",
            "failure_category": None if ok else "ATOMIC_WRITE_FAILED", "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


def run_test_12_timestamps_monotonic_and_end_in_z() -> dict:
    d = fresh_dir("test12_monotonic_z")
    task_id = "TEST_12_MONOTONIC"
    s142.start_task(task_id, "monotonic test", expected_outputs=[], tasks_dir=d)
    result = s142.finish_task(task_id, mandatory_outputs=[], status="PASS", tasks_dir=d)
    started_at = result["started_at"]
    completed_at = result["completed_at"]
    ends_in_z = started_at.endswith("Z") and completed_at.endswith("Z")
    started_dt = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S.%fZ")
    completed_dt = datetime.strptime(completed_at, "%Y-%m-%dT%H:%M:%S.%fZ")
    monotonic = completed_dt >= started_dt
    ok = ends_in_z and monotonic
    return {"test_name": "12_timestamps_monotonic_and_end_in_z", "expected_result": "both end in Z and completed_at >= started_at",
            "actual_result": f"started_at={started_at} completed_at={completed_at} ends_in_z={ends_in_z} monotonic={monotonic}",
            "failure_category": None if ok else "INVALID_TIMESTAMP_FORMAT", "status": "PASS" if ok else "FAIL", "evidence_paths": [str(d)]}


TESTS = [
    run_test_1_normal_flow, run_test_2_duplicate_started, run_test_3_result_without_started,
    run_test_4_missing_output_prevents_pass, run_test_5_unreadable_output_prevents_pass,
    run_test_6_hash_mismatch_detected, run_test_7_task_id_mismatch_detected,
    run_test_8_non_utc_timestamp_rejected, run_test_9_completion_before_start_rejected,
    run_test_10_existing_result_not_overwritten, run_test_11_atomic_writes_leave_no_temp_files,
    run_test_12_timestamps_monotonic_and_end_in_z,
]


def main() -> dict:
    start_time = time.perf_counter()
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    print("=" * 100)
    print("TASK_143 — scripts/142_task_marker_guard.py VALIDATION (isolated temp directories only)")
    print("=" * 100)

    results = []
    for test_fn in TESTS:
        try:
            record = test_fn()
        except Exception as exc:  # noqa: BLE001
            record = {"test_name": test_fn.__name__, "expected_result": "test to complete without an unhandled exception",
                       "actual_result": f"unhandled exception: {exc}", "failure_category": "EVIDENCE_VALIDATION_FAILED",
                       "status": "FAIL", "evidence_paths": []}
        results.append(record)
        print(f"  {record['test_name']}: {record['status']} — {record['actual_result']}")

    all_pass = all(r["status"] == "PASS" for r in results)
    overall_status = "PASS" if all_pass else "FAIL"

    output = {
        "status": overall_status, "total_tests": len(results),
        "passed": sum(1 for r in results if r["status"] == "PASS"),
        "failed": sum(1 for r in results if r["status"] == "FAIL"),
        "results": results, "runtime_seconds": round(time.perf_counter() - start_time, 3),
    }

    print("\n" + "=" * 100)
    print(f"OVERALL: {overall_status} ({output['passed']}/{output['total_tests']} passed)")
    print("=" * 100)

    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"JSON written to {JSON_OUTPUT_PATH}")

    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["test_name", "expected_result", "actual_result", "failure_category", "status", "evidence_paths"])
        writer.writeheader()
        for r in results:
            writer.writerow({**r, "evidence_paths": ";".join(r["evidence_paths"])})
    print(f"CSV written to {CSV_OUTPUT_PATH}")

    return output


if __name__ == "__main__":
    main()
