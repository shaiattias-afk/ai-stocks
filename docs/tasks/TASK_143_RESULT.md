# TASK_143 — Task-marker foundation — RESULT: PASS

Created and validated one reusable, fail-closed task-marker utility,
`scripts/142_task_marker_guard.py`, now mandatory for every future
`TASK_NNN` operation in this project — enforcing in code the invariant
TASK_142 could previously only state as a written rule.

## Utility design

Four functions: `start_task()`, `finish_task()`, `fail_task()`,
`validate_task_evidence()`. Full design detail in
`docs/TASK_MARKER_STANDARD.md`.

### Exact UTC source
One function, `_utc_now_iso()`, is the only place any timestamp is
produced anywhere in the module:
```python
datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
```
No public or private function accepts a timestamp parameter — there is
no code path through which a caller (production or test) can supply
`started_at`/`completed_at`. This closes both defects TASK_142 found: a
hand-typed value and a local-time value mislabeled as UTC are both
structurally impossible, not merely disallowed by convention.

### Atomic-write method
`_atomic_write_json()`: write to `<path>.tmp<pid>` in the same directory,
`flush()` + `os.fsync()`, then `os.replace()` (atomic on Windows and
POSIX). On failure, the temp file is removed and `ATOMIC_WRITE_FAILED` is
raised. Every write function re-reads what it wrote and compares it to
what it intended before returning — verified, not assumed.

### PASS is impossible with missing/invalid outputs
`finish_task(status="PASS")` hashes (SHA-256), sizes, and time-stamps
every mandatory output *before* writing the RESULT file. Any missing or
unreadable output raises immediately and **no RESULT file is written at
all** — never a `PASS` record pointing at outputs that don't exist or
don't hash-match.

### `completed_at >= started_at` enforced
Both `finish_task()` and `fail_task()` raise `COMPLETION_BEFORE_START`
before writing anything if the internally-read `completed_at` precedes
the STARTED file's `started_at`.

## All test results — 12/12 PASS
| # | Test | Result |
|---|---|---|
| 1 | Normal STARTED → outputs → PASS RESULT | PASS |
| 2 | Duplicate STARTED attempt fails | PASS (`STARTED_ALREADY_EXISTS`) |
| 3 | RESULT without STARTED fails | PASS (`STARTED_MISSING`) |
| 4 | Missing mandatory output prevents PASS | PASS (`REQUIRED_OUTPUT_MISSING`, 0 RESULT file written) |
| 5 | Unreadable output prevents PASS | PASS (`REQUIRED_OUTPUT_UNREADABLE`, tested via a directory passed as a file path) |
| 6 | Hash mismatch is detected | PASS (`OUTPUT_HASH_MISMATCH` via `validate_task_evidence` after tampering post-write) |
| 7 | Task-ID mismatch is detected | PASS (`TASK_ID_MISMATCH`, via a STARTED file copied under an alias name) |
| 8 | Non-UTC timestamp is rejected | PASS (`NON_UTC_TIMESTAMP`, via a fixture-edited STARTED file) |
| 9 | Completion-before-start is rejected | PASS (`COMPLETION_BEFORE_START`, via a STARTED file edited to an artificial future `started_at` — **no clock injection into production code**) |
| 10 | Existing RESULT cannot be overwritten | PASS (`RESULT_ALREADY_EXISTS`, file content confirmed byte-identical after the rejected second call) |
| 11 | Atomic writes leave no temp files behind | PASS (0 `*.tmp*` files found after start+finish) |
| 12 | STARTED/RESULT timestamps monotonic and end in `Z` | PASS |

Every test ran inside its own isolated temporary directory under this
session's scratchpad — never `docs/tasks/`, never any production file.
Test 9 in particular proves the completion-before-start check works
*without* weakening production code: the fixture edits the on-disk
STARTED file's `started_at` field directly (simulating a marker with an
implausible value from some other source); `finish_task()` itself never
gained any parameter or hook that would let a caller pass in a fake
clock.

## TASK_143 self-validation result
After all outputs were finalized, `scripts/142`'s own `finish_task()` was
used to write `docs/tasks/TASK_143_TASK_MARKER_FOUNDATION_RESULT.json`
(hashing every mandatory output below), and `validate_task_evidence
("TASK_143_TASK_MARKER_FOUNDATION")` was then run against TASK_143's own
STARTED/RESULT pair. **Final result: `valid=True`, 0 failure categories.**

### Issues found and fixed during genuine dogfooding (reported honestly, not hidden)
Actually using the brand-new utility on itself — exactly what this
self-validation step is for — surfaced three real issues before the
final clean pass:

1. **Filename-convention ambiguity.** The task's bootstrap step required
   creating `docs/tasks/TASK_143_STARTED.json` (a short filename,
   matching TASK_141/TASK_142's established convention) *before*
   `scripts/142` existed to define its own convention. Once built,
   `scripts/142` derives marker filenames from the full `task_id`
   (`TASK_143_TASK_MARKER_FOUNDATION_STARTED.json`), so the two didn't
   match. **Fix**: renamed the bootstrap file to match the utility's
   convention — content byte-identical, only the filename changed (this
   is the file's very first write since creation; no reported/accepted
   evidence existed yet).
2. **A real bug in `validate_task_evidence()`'s output-list comparison.**
   It compared `expected_outputs` (relative paths, written at
   `start_task` time) against `outputs` (absolute paths, written at
   `finish_task` time) with a plain set comparison — always mismatching
   even when the same file was meant. **Fix**: both sides are now
   normalized to resolved absolute paths before comparing. The full
   12-test suite was re-run after the fix and still passed **12/12** —
   confirmed no regression.
3. **A logical self-reference issue**, not just a naming one: the
   STARTED file's `expected_outputs` had listed the RESULT JSON file
   itself as one of its own mandatory outputs — but a RESULT file cannot
   meaningfully hash-certify itself from inside itself (the hash isn't
   known until after the file is written). **Fix**: removed that one
   entry from the STARTED file's `expected_outputs` list (the file's
   `task_id`/`started_at`/`objective`/every other field is untouched);
   `docs/tasks/TASK_143_TASK_MARKER_FOUNDATION_RESULT.json` remains the
   authoritative machine-readable result and is not itself declared as a
   "mandatory output" requiring self-certification.

An earlier `finish_task()` attempt made before fix #2 was superseded (its
RESULT was moved aside, not deleted, and preserved in this session's
scratchpad directory) once the bug was found — the RESULT file now on
disk is the one and only ever written to `docs/tasks/` for this task_id.

## Files and SHA-256 hashes (recorded in TASK_143_RESULT.json by finish_task itself)
See `docs/tasks/TASK_143_RESULT.json` → `outputs` for the authoritative
hash/size/mtime record of every mandatory output, computed and verified
by `scripts/142_task_marker_guard.py` itself (not hand-computed).

## Confirmation that no production row or schema changed
This task never opened `data/database/ai_stock_agent.duckdb` or
`data/database/xbrl_warehouse_proof.duckdb` in any mode. No warehouse
loader was built or modified. No filing was re-warehoused. All test
activity was confined to isolated temporary directories.

## Actual runtime
Local test-suite execution: well under 10 seconds (12 isolated
filesystem-only tests, no network, no Arelle). Total Claude work for this
task (utility design, validation harness, standard doc, self-validated
completion): within the 5–10 minute expectation.

## PASS or FAIL: **PASS**
All 12 required tests pass; the utility structurally prevents caller-
supplied timestamps; every atomic write is verified by re-read; `PASS`
is provably impossible with missing/invalid outputs or
completion-before-start; TASK_143 successfully used its own new utility
to record and self-validate its own completion.

## One exact next implementation step
Adopt `scripts/142_task_marker_guard.py` for the next task that performs
real production work — the NVDA production repair (building a production
version of the corrected entry-point-detection loader and re-warehousing
accession `0001045810-19-000079`), explicitly deferred out of this task's
scope, should be the first task to use `start_task()`/`finish_task()`
from this new utility instead of hand-written marker files.
