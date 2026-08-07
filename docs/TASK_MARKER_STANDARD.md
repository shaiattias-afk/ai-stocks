# Task-Marker Standard (mandatory for every future TASK_NNN operation)

## Why this exists

TASK_142 proved that TASK_141's task-marker evidence had two process
defects: (1) the STARTED timestamp was hand-typed rather than read from
any clock, and (2) the RESULT completion timestamp was local time with a
literal `Z` (UTC designator) incorrectly appended (`Get-Date -Format
"...Z"` in PowerShell returns local time, not UTC). A written rule alone
was judged insufficient to prevent recurrence — the invariant is now
enforced by reusable code: `scripts/142_task_marker_guard.py`.

## The rule, going forward

**Every future `TASK_NNN` operation in this project must use
`scripts/142_task_marker_guard.py`'s `start_task()` /
`finish_task()`/`fail_task()` to write its STARTED and RESULT evidence —
never hand-typed timestamps, never local time labeled as UTC, and never a
`PASS` result without every mandatory output validated (existence,
readability, SHA-256, size).**

## The utility: `scripts/142_task_marker_guard.py`

### Public API
```python
start_task(task_id, objective, expected_outputs, read_only=True, tasks_dir=None) -> dict
finish_task(task_id, mandatory_outputs, status="PASS", extra_fields=None, tasks_dir=None) -> dict
fail_task(task_id, error_message, tasks_dir=None) -> dict
validate_task_evidence(task_id, tasks_dir=None) -> dict
```

### Exact UTC source
Every timestamp is produced by exactly one function,
`_utc_now_iso()`, which calls `datetime.now(timezone.utc)` and formats it
as ISO-8601 with millisecond precision, always ending in `Z`:
```python
datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
```
**No function in this module accepts a timestamp parameter, anywhere.**
There is no code path — production or otherwise — through which a caller
can supply `started_at` or `completed_at`. This is what makes the defect
structurally impossible to repeat, not merely discouraged.

### Atomic-write method
Every write goes through `_atomic_write_json()`: write to a temp file
(`<path>.tmp<pid>`) in the *same directory* as the target, `flush()` +
`os.fsync()`, then `os.replace()` (an atomic rename on both Windows and
POSIX). On any failure the temp file is removed and a `TaskMarkerError`
with category `ATOMIC_WRITE_FAILED` is raised — nothing partially-written
is ever left as the "real" file. Every write function re-reads what it
just wrote and compares it to what it intended to write before returning.

### PASS is impossible with missing/invalid outputs
`finish_task(..., status="PASS", ...)` computes SHA-256, size, and
modified-time for every path in `mandatory_outputs` *before* writing
anything. If any is missing (`REQUIRED_OUTPUT_MISSING`) or unreadable
(`REQUIRED_OUTPUT_UNREADABLE`), the function raises immediately and
**writes no RESULT file at all** — there is no way to end up with a
`status: "PASS"` RESULT file whose declared outputs don't actually exist
and hash-match.

### `completed_at >= started_at` is enforced, not advisory
`finish_task()` and `fail_task()` both parse the STARTED file's
`started_at`, read `completed_at` from the same UTC clock, and raise
`TaskMarkerError("COMPLETION_BEFORE_START", ...)` if the comparison ever
fails — before any RESULT file is written.

### Failure categories
```
STARTED_ALREADY_EXISTS   RESULT_ALREADY_EXISTS   STARTED_MISSING
INVALID_TIMESTAMP_FORMAT NON_UTC_TIMESTAMP       COMPLETION_BEFORE_START
REQUIRED_OUTPUT_MISSING  REQUIRED_OUTPUT_UNREADABLE  OUTPUT_HASH_MISMATCH
TASK_ID_MISMATCH         INVALID_STATUS          ATOMIC_WRITE_FAILED
EVIDENCE_VALIDATION_FAILED
```
Plus three additional categories used only by `validate_task_evidence()`:
`DUPLICATE_EVIDENCE_FILES`, `RESULT_BEFORE_STARTED_ON_DISK`,
`REQUIRED_OUTPUT_LIST_MISMATCH`.

### `validate_task_evidence()`
Fully read-only, independent of any narrative report. Re-reads STARTED
and RESULT, checks: matching `task_id`; both timestamps parse as UTC and
end in `Z`; `completed_at >= started_at`; the STARTED file's filesystem
`mtime` predates the RESULT file's `mtime` (an independent, non-
fabricatable cross-check, the same technique TASK_142 used manually);
every output in the RESULT still exists on disk with a matching SHA-256
and size; `status` is one of `PASS`/`FAIL`/`INCOMPLETE`; and no duplicate
STARTED/RESULT files exist for the task. Returns
`{"valid": bool, "failure_categories": [...], "detail": {...}}` —
deterministic, no side effects.

## How to use it in a future task

```python
import importlib.util, sys
spec = importlib.util.spec_from_file_location("s142", "scripts/142_task_marker_guard.py")
s142 = importlib.util.module_from_spec(spec); sys.modules["s142"] = s142; spec.loader.exec_module(s142)

s142.start_task(
    task_id="TASK_144_SOMETHING",
    objective="...",
    expected_outputs=["scripts/144_something.py", "data/something.json"],
)

# ... do the actual work, produce the declared output files ...

s142.finish_task(
    task_id="TASK_144_SOMETHING",
    mandatory_outputs=["scripts/144_something.py", "data/something.json"],
    status="PASS",
)

validation = s142.validate_task_evidence("TASK_144_SOMETHING")
assert validation["valid"]
```

If the task fails partway through, call `s142.fail_task(task_id,
error_message)` instead of `finish_task` — it preserves the STARTED
evidence, records the exact error, and never silently overwrites an
existing RESULT.

## Validation

`scripts/143_task_marker_guard_validation.py` runs 12 required tests,
each inside its own isolated temporary directory (never `docs/tasks/`,
never any production file): normal STARTED→outputs→PASS flow; duplicate
STARTED rejected; RESULT-without-STARTED rejected; missing/unreadable
mandatory output blocks PASS; tampered-output hash mismatch detected;
task-ID mismatch detected; non-UTC timestamp rejected; completion-before-
start rejected (via a STARTED-file fixture, never by injecting a fake
clock into production code); existing RESULT never overwritten; atomic
writes leave no `*.tmp*` files behind; and STARTED/RESULT timestamps are
monotonic and end in `Z`. Results: `data/task_marker_guard_validation.json`
/ `.csv`. Last run: **12/12 PASS**.

## Standing decision (recorded in `docs/DECISIONS_LOG.md`)

All future `TASK_NNN` work must use this shared task-marker utility.
Manually typed timestamps, local timestamps labeled as UTC, and `PASS`
results without validated mandatory outputs are forbidden.
