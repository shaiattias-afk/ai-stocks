"""
tests/test_task_marker_guard.py -- pytest port of the former
scripts/143_task_marker_guard_validation.py (D-040's required 12-test
validation harness for stock_agent.storage.task_marker_guard). Each test
runs inside pytest's own isolated tmp_path -- never docs/tasks/ or any
production file.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from stock_agent.storage.task_marker_guard import TaskMarkerError, finish_task, start_task, validate_task_evidence


def _make_output_file(dir_path, name: str, content: str = "test content"):
    p = dir_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_normal_flow_started_outputs_pass(tmp_path):
    task_id = "TEST_1_NORMAL"
    output = _make_output_file(tmp_path, "output.txt")
    start_task(task_id, "normal flow test", expected_outputs=[str(output)], tasks_dir=tmp_path)
    result = finish_task(task_id, mandatory_outputs=[str(output)], status="PASS", tasks_dir=tmp_path)
    validation = validate_task_evidence(task_id, tasks_dir=tmp_path)
    assert result["status"] == "PASS"
    assert validation["valid"] is True


def test_duplicate_started_fails(tmp_path):
    task_id = "TEST_2_DUP"
    start_task(task_id, "dup test", expected_outputs=[], tasks_dir=tmp_path)
    with pytest.raises(TaskMarkerError) as exc_info:
        start_task(task_id, "dup test", expected_outputs=[], tasks_dir=tmp_path)
    assert exc_info.value.category == "STARTED_ALREADY_EXISTS"


def test_result_without_started_fails(tmp_path):
    task_id = "TEST_3_NO_STARTED"
    with pytest.raises(TaskMarkerError) as exc_info:
        finish_task(task_id, mandatory_outputs=[], status="PASS", tasks_dir=tmp_path)
    assert exc_info.value.category == "STARTED_MISSING"


def test_missing_output_prevents_pass(tmp_path):
    task_id = "TEST_4_MISSING_OUTPUT"
    missing = tmp_path / "does_not_exist.txt"
    start_task(task_id, "missing output test", expected_outputs=[str(missing)], tasks_dir=tmp_path)
    with pytest.raises(TaskMarkerError) as exc_info:
        finish_task(task_id, mandatory_outputs=[str(missing)], status="PASS", tasks_dir=tmp_path)
    assert exc_info.value.category == "REQUIRED_OUTPUT_MISSING"
    assert not (tmp_path / f"{task_id}_RESULT.json").exists()


def test_unreadable_output_prevents_pass(tmp_path):
    task_id = "TEST_5_UNREADABLE"
    # a directory "exists" but cannot be opened/read as a file -> triggers the unreadable path
    fake_output_dir = tmp_path / "this_is_a_directory_not_a_file"
    fake_output_dir.mkdir()
    start_task(task_id, "unreadable output test", expected_outputs=[str(fake_output_dir)], tasks_dir=tmp_path)
    with pytest.raises(TaskMarkerError) as exc_info:
        finish_task(task_id, mandatory_outputs=[str(fake_output_dir)], status="PASS", tasks_dir=tmp_path)
    assert exc_info.value.category == "REQUIRED_OUTPUT_UNREADABLE"


def test_hash_mismatch_detected(tmp_path):
    task_id = "TEST_6_HASH_MISMATCH"
    output = _make_output_file(tmp_path, "output.txt", content="ORIGINAL CONTENT")
    start_task(task_id, "hash mismatch test", expected_outputs=[str(output)], tasks_dir=tmp_path)
    finish_task(task_id, mandatory_outputs=[str(output)], status="PASS", tasks_dir=tmp_path)
    # tamper the output AFTER the RESULT was written and hashed
    output.write_text("TAMPERED CONTENT", encoding="utf-8")
    validation = validate_task_evidence(task_id, tasks_dir=tmp_path)
    assert validation["valid"] is False
    assert "OUTPUT_HASH_MISMATCH" in validation["failure_categories"]


def test_task_id_mismatch_detected(tmp_path):
    orig_id, alias_id = "TASK_7_ORIG", "TASK_7_ALIAS"
    start_task(orig_id, "task id mismatch test", expected_outputs=[], tasks_dir=tmp_path)
    # copy the STARTED file under a different filename (simulating a foreign/misnamed
    # marker whose internal task_id does not match the name it's being looked up by)
    orig_started = tmp_path / f"{orig_id}_STARTED.json"
    alias_started = tmp_path / f"{alias_id}_STARTED.json"
    shutil.copyfile(orig_started, alias_started)
    with pytest.raises(TaskMarkerError) as exc_info:
        finish_task(alias_id, mandatory_outputs=[], status="PASS", tasks_dir=tmp_path)
    assert exc_info.value.category == "TASK_ID_MISMATCH"


def test_non_utc_timestamp_rejected(tmp_path):
    task_id = "TEST_8_NON_UTC"
    start_task(task_id, "non-utc timestamp test", expected_outputs=[], tasks_dir=tmp_path)
    started_path = tmp_path / f"{task_id}_STARTED.json"
    marker = json.loads(started_path.read_text(encoding="utf-8"))
    marker["started_at"] = "2026-08-05T10:00:00+03:00"  # local-offset style, does NOT end in 'Z'
    started_path.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    with pytest.raises(TaskMarkerError) as exc_info:
        finish_task(task_id, mandatory_outputs=[], status="PASS", tasks_dir=tmp_path)
    assert exc_info.value.category == "NON_UTC_TIMESTAMP"


def test_completion_before_start_rejected(tmp_path):
    """Uses a test fixture (directly editing the STARTED file's started_at
    to an artificial future value) rather than any clock injection into
    production code -- finish_task still obtains completed_at exclusively
    from datetime.now(timezone.utc) and has no parameter through which a
    caller could supply a different clock."""
    task_id = "TEST_9_COMPLETION_BEFORE_START"
    start_task(task_id, "completion before start test", expected_outputs=[], tasks_dir=tmp_path)
    started_path = tmp_path / f"{task_id}_STARTED.json"
    marker = json.loads(started_path.read_text(encoding="utf-8"))
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    marker["started_at"] = future
    started_path.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    with pytest.raises(TaskMarkerError) as exc_info:
        finish_task(task_id, mandatory_outputs=[], status="PASS", tasks_dir=tmp_path)
    assert exc_info.value.category == "COMPLETION_BEFORE_START"


def test_existing_result_not_overwritten(tmp_path):
    task_id = "TEST_10_NO_OVERWRITE"
    start_task(task_id, "no overwrite test", expected_outputs=[], tasks_dir=tmp_path)
    first = finish_task(task_id, mandatory_outputs=[], status="PASS", tasks_dir=tmp_path)
    original_hash_input = json.dumps(first, sort_keys=True)
    with pytest.raises(TaskMarkerError) as exc_info:
        finish_task(task_id, mandatory_outputs=[], status="FAIL", tasks_dir=tmp_path)
    assert exc_info.value.category == "RESULT_ALREADY_EXISTS"
    result_path = tmp_path / f"{task_id}_RESULT.json"
    unchanged = json.dumps(json.loads(result_path.read_text(encoding="utf-8")), sort_keys=True) == original_hash_input
    assert unchanged


def test_atomic_writes_leave_no_temp_files(tmp_path):
    task_id = "TEST_11_ATOMIC"
    output = _make_output_file(tmp_path, "output.txt")
    start_task(task_id, "atomic test", expected_outputs=[str(output)], tasks_dir=tmp_path)
    finish_task(task_id, mandatory_outputs=[str(output)], status="PASS", tasks_dir=tmp_path)
    leftover_temp_files = list(tmp_path.glob("*.tmp*"))
    assert leftover_temp_files == []


def test_timestamps_monotonic_and_end_in_z(tmp_path):
    task_id = "TEST_12_MONOTONIC"
    start_task(task_id, "monotonic test", expected_outputs=[], tasks_dir=tmp_path)
    result = finish_task(task_id, mandatory_outputs=[], status="PASS", tasks_dir=tmp_path)
    started_at = result["started_at"]
    completed_at = result["completed_at"]
    assert started_at.endswith("Z") and completed_at.endswith("Z")
    started_dt = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S.%fZ")
    completed_dt = datetime.strptime(completed_at, "%Y-%m-%dT%H:%M:%S.%fZ")
    assert completed_dt >= started_dt
