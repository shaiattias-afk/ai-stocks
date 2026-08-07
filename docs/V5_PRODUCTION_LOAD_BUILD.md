# V5 quarterly production-load script — BUILD RESULT: PASS (check-only only; --execute never run)

Built `scripts/151_v5_quarterly_production_load.py`, the production-load
script for quarterly engine V5, supporting exactly two mutually
exclusive modes (`--check-only`, `--execute`). This task ran
**`--check-only` only** — `--execute` was never invoked, per explicit
instruction. No production data was changed.

## Bug fix: pre-transaction `KeyError: 'q1_accession'` (this update)

### Stage 1 — proof production remained unchanged

A real manual `--execute` attempt (`data/v5_production_load_result.json`,
`failed_at=2026-08-06T07:55:25.284Z`) acquired its PID lock, completed
backup+archive, then failed with `KeyError: 'q1_accession'` before the
production transaction began. Verified read-only, from first principles:

| Check | Result |
|---|---|
| DuckDB transaction (`BEGIN TRANSACTION`) ever started | **No** — that code is far later in `run_execute()`, past the point of failure |
| Any commit occurred | **No** |
| `data/v5_production_load_checkpoint.json` | `company_years: []` — the per-company-year checkpoint append never ran; the crash happened before the very first engine subprocess call |
| Current production SHA-256 | `79A57D28743E0D12DF82DFB41A0C041662ECE0D729E9414980F94E460604655F` |
| Failed-run backup SHA-256 (`ai_stock_agent_pre_v5_production_load_20260806T075525Z.duckdb`) | `79A57D28743E0D12DF82DFB41A0C041662ECE0D729E9414980F94E460604655F` — **identical** |
| Current `quarterly_extraction_runs` | 45 |
| Current `quarterly_metric_results` | 1,080 |
| Current `financial_metric_results` | 900 |
| Current unique REVIEW_REQUIRED | 4 (exactly `CRWD 2022-01-31 pretax_income`, `MU 2021-09-02 pretax_income`, `PANW 2021-07-31 pretax_income`, `PANW 2021-07-31 revenue`) |
| Existing V5-engine-version runs | 0 |

**Production is provably unchanged** — confirmed by hash equality with
the pre-load backup (not merely by trusting the script's own report),
by every count matching the expected pre-load state exactly, and by the
checkpoint recording zero completed company-years.

### Stage 2 — diagnosis (derived from the actual artifacts, not guessed)

**Exact failure line**: `scripts/151_v5_quarterly_production_load.py`,
inside `run_execute()`'s per-company-year loop —
`cy["q1_accession"]` when building the engine subprocess `cmd` list, at
the very first iteration (`index=1`, i.e. CRWD 2022-01-31 — the reason
`company_years: []` in the checkpoint: the crash happened before
`subprocess.run` was even called).

**Exact object being accessed**: `cy`, an element of `target_company_years`,
which `derive_and_cross_check_targets()` builds by filtering
`regression["company_year_results"]` (i.e. entries straight from
`data/v5_final_release_regression.json`, written by `scripts/150`) down
to the 3 target keys.

**Keys actually present** in a `company_year_results` entry (confirmed
by direct inspection of `CRWD 2022-01-31`'s entry):
`availability_violations, duplicate_keys, elapsed_seconds,
expected_changes, family_safety_checks, fiscal_year_end,
missing_lineage_count, row_count, run_id, status, ticker,
unexpected_differences`. **None of `q1_accession`, `q2_accession`,
`q3_accession`, `fy_accession` are present** — `scripts/150` never wrote
them into that structure (it used its own local `cy["q1_accession"]`
etc. only to build its *own* subprocess command, then discarded them
before appending the result dict to `company_year_results`).

**Scope of the mismatch**: confirmed **not** Q1-specific — all four of
`q1_accession`/`q2_accession`/`q3_accession`/`fy_accession` are equally
absent from every `company_year_results` entry. `q1_accession` merely
happened to be the first key referenced in the `cmd` list literal.

**Correct existing source**: `quarterly_extraction_runs`
(`q1_accession, q2_accession, q3_accession, fy_accession` columns),
keyed by `(ticker, fiscal_year_end)` — the exact same table
`check_execution_preconditions()` already queries for `run_id` per
target company-year, and the exact same table the atomic transaction
independently re-queries immediately before delete. This is the single
authoritative source for "what accession is this active run currently
pointing at," not a new trust boundary.

### Stage 3 — minimal fix

Added one function, `enrich_target_company_years_with_accessions()`,
called exactly once in `run_execute()` — immediately after the
execution-preconditions gate passes, before backup/archive — which
queries `quarterly_extraction_runs` for each target company-year and
merges `q1_accession`/`q2_accession`/`q3_accession`/`fy_accession` into
its `cy` dict:

- Validates **all four** accession fields consistently (not only Q1).
- Cross-checks the freshly-read `run_id` against the regression
  artifact's recorded `run_id` for that company-year — fails closed
  (`RuntimeError`) on any mismatch.
- Fails closed with a clear, specific error (naming the exact missing
  field(s)) if any accession is genuinely absent/empty — never
  silently substitutes or guesses.
- No ticker- or year-specific logic — a single generic query by
  `(ticker, fiscal_year_end)`.
- No database schema change. No change to `scripts/148`'s engine
  output. No weakening of `rows_differ_full()`'s accession comparison
  (`accession_number` remains a required-identical field for
  unchanged rows) — this fix only supplies the CLI invocation
  arguments; the row-level comparison logic that failed before was
  never reached and is untouched.

### Stage 4 — targeted validation (all read-only / no side effects)

**Part A — schema test, all 3 target company-years** (read-only against
production, no write): derived and cross-checked target company-years,
verified execution preconditions, then called
`enrich_target_company_years_with_accessions()` directly —

| Ticker | FY end | q1 | q2 | q3 | fy |
|---|---|---|---|---|---|
| CRWD | 2022-01-31 | 0001535527-21-000013 | 0001535527-21-000022 | 0001535527-21-000028 | 0001535527-22-000006 |
| MU | 2021-09-02 | 0000723125-21-000012 | 0000723125-21-000032 | 0000723125-21-000052 | 0000723125-21-000065 |
| PANW | 2021-07-31 | 0001327567-20-000041 | 0001327567-21-000004 | 0001327567-21-000014 | 0001327567-21-000029 |

**Result: PASS** — all 12 accession fields present and non-empty across
all 3 target company-years.

**Part B — one fresh targeted engine execution, CRWD 2022-01-31**, via
subprocess with a real OS timeout of 45s: **PASS, elapsed 8.1s**
(well under the limit), exit code 0.

**Part C — passed through the exact same normalization/comparison
functions `run_execute()` uses** (`get_production_rows_full`,
`compare_against_regression_and_production`): **PASS** — 24 rows
produced; 4 changed rows (CRWD's `pretax_income` × Q1–Q4, matching the
regression exactly) each carrying complete `value`/`unit`/
`concept_qname`/`extraction_basis`/`reconciliation_status`/
`availability_date`/`accession_number`, all `PASS`; 20 unchanged rows;
0 unexpected differences; 0 mismatches against the saved TASK_148
regression result. No database write, no backup, no archive performed
by this test (confirmed — it only ever opened both databases
`read_only=True`).

**Post-fix `--check-only`**: **PASS, runtime 0.045s.** Independently
re-verified afterward: production DB SHA-256 unchanged
(`79A57D28743E0D12...`, identical to before this fix), the pre-existing
failure-record checkpoint/result files (`failed_at=2026-08-06T07:55:25Z`)
untouched, no new backup created.

## Bug fix: self-PID detection (earlier update)

A real `--execute` attempt (`data/v5_production_load_result.json`,
`failed_at=2026-08-06T07:32:48.313Z`) stopped cleanly before any backup,
archive, engine run, or database write — every precondition passed
except `no_other_production_write_process_active = False`
(`existing_pid=22764`, `pid_active=True`).

**Exact defect**: `check_pid_lock_status()` (originally
`scripts/151_v5_quarterly_production_load.py:178`) took no way to
distinguish the calling process's own lock from a foreign one. In
`run_execute()`, `acquire_pid_lock()` runs first and writes
`data/v5_production_load.pid` containing the current process's own PID.
Immediately afterward, `check_execution_preconditions()` called
`check_pid_lock_status()` again — which re-read the *same* lock file,
found `existing_pid == os.getpid()`, correctly determined the PID was
"active" (it's the running process itself), and concluded another
writer was active. The process had, in effect, locked itself out.

A second, related defect in the same function: a syntactically valid
but malformed lock file (valid JSON, missing the `"pid"` key) was
silently treated as **free** (`existing_pid` fell through to `None`
via `.get("pid")`, then `active = False` because `None` is falsy) —
the opposite of the required fail-closed behavior.

**Exact correction**: `check_pid_lock_status()` now takes an
`exclude_pid: int | None = None` parameter. When the lock's PID equals
`exclude_pid`, it is classified as `is_own_lock=True, is_free=True` —
never "another writer." Any other PID is evaluated as before (active →
`is_free=False`; inactive → `is_free=True, is_stale=True`). The two
call sites that ask *"is some other process writing right now"*
(`check_execution_preconditions()` and `run_check_only()`'s own
`no_live_pid_lock` check) now pass `exclude_pid=os.getpid()`.
`acquire_pid_lock()` — which runs *before* this process holds any lock
— correctly still calls it with no `exclude_pid`. Separately, reading
the `"pid"` field now uses `content["pid"]` (not `.get`) with an
explicit `isinstance(..., int)` check, so a missing or non-integer
`pid` field raises and is caught, returning `is_free=False` — fail
closed, matching requirement 3 ("a stale lock may be removed only
after proving it is stale") and the malformed-file requirement.
`acquire_pid_lock()` was also updated to refuse to start outright (not
silently overwrite) when the lock file itself is malformed. Duplicate-
run protection is unchanged in every other respect: a genuinely live,
different-PID lock still fails closed exactly as before.

## Isolated PID-lock tests — 5/5 PASS

Run against a scratch lock-file path (never `data/v5_production_load.pid`,
never any database), via a temporary script in the session scratchpad
(not a new production script):

| Case | Result |
|---|---|
| Current process owns lock | **PASS** — `is_free=True, is_own_lock=True` |
| Different **active** PID owns lock | **PASS** — `is_free=False` (correctly blocks) |
| Different **inactive** PID owns lock | **PASS** — recognized as stale (`is_free=True, is_stale=True`) |
| Malformed lock file (missing `pid` field) | **PASS** — fails closed (`is_free=False`) |
| Malformed lock file (invalid JSON) | **PASS** — fails closed (`is_free=False`) |

## Check-only run (post-fix) — PASS

```
.\.venv\Scripts\python.exe .\scripts\151_v5_quarterly_production_load.py --check-only
```

**Result: PASS. Runtime: 0.044s** (well under the 10s expectation) —
`no_live_pid_lock: OK`, and critically `execution_preconditions_met: OK`
now correctly evaluates to true with no live lock present.

No backup, archive, engine invocation, or database write occurred: no
`data/v5_production_load.pid`, no new
`data/v5_production_load_checkpoint.json` (the one present on disk is
the **pre-fix failure record**, left untouched, timestamped
`2026-08-06T07:32:xx`), no new file in `data/database/backups/`; both
`ai_stock_agent.duckdb` and `xbrl_warehouse_proof.duckdb` file
modification timestamps are unchanged from before this session
(2026-08-05, from earlier tasks) — confirmed via direct filesystem
inspection, not merely the script's own self-report.

## Check-only run — PASS (original, pre-fix build)

**Result: PASS. Runtime: 0.061s** (well under the 10s expectation).

| Check | Result |
|---|---|
| Engine + regression scripts import successfully | ✓ |
| Regression artifacts (`data/v5_final_release_regression.{json,csv}`, `docs/V5_FINAL_RELEASE_REGRESSION.md`) valid — `status=PASS`, all 13 global checks true | ✓ |
| Target derivation matches the fail-closed cross-check | ✓ |
| Replacement scope verified (72 rows, 16 changed, 56 unchanged) | ✓ |
| Execution preconditions met | ✓ |
| Output/archive paths constructible | ✓ |
| No live PID lock | ✓ |

**Engine invoked: No. Backup performed: No. Database written: No.
Archive written: No.** — confirmed both by the script's own self-report
and independently verified afterward (no `data/v5_production_load.pid`,
no `data/v5_production_load_checkpoint.json`, no
`data/v5_production_load_result.json/.csv`, no
`data/archive/v5_production_load_manifest.json`, no new file in
`data/database/backups/` — only `data/v5_production_load_build_validation.json`
and one log line were written).

## Target derivation (not hardcoded)

Derived directly from `data/v5_final_release_regression.json`'s own
`target_metric_year_cases` field, then cross-checked (fail-closed) against
a fixed literal expectation — both matched exactly:

| Ticker | FY end |
|---|---|
| CRWD | 2022-01-31 |
| MU | 2021-09-02 |
| PANW | 2021-07-31 |

Metric-year cases (4): CRWD 2022-01-31 pretax_income, MU 2021-09-02
pretax_income, PANW 2021-07-31 pretax_income, PANW 2021-07-31 revenue.

## Replacement scope verified

| Check | Value |
|---|---:|
| Target company-years | 3 |
| Total replacement rows | 72 (24 × 3) |
| Rows expected to change | 16 |
| Rows expected to remain identical | 56 |
| Per-company-year row count | 24 (all 3) |
| Regression already confirmed 0 unexpected differences on these 3 | ✓ |

The future `--execute` mode replaces the **complete company-year** (all
24 rows per target), never just the 16 changed rows in isolation —
confirmed by the transaction logic in `scripts/151` (deletes all rows
for each old `run_id`, inserts all 24 fresh rows per company-year).

## Execution preconditions verified (read-only)

| Check | Result |
|---|---|
| `quarterly_extraction_runs` = 45 | ✓ |
| `quarterly_metric_results` = 1,080 | ✓ |
| `financial_metric_results` = 900 | ✓ |
| unique REVIEW_REQUIRED = 4 | ✓ |
| The exact 4 expected cases remain REVIEW_REQUIRED | ✓ |
| Exactly 1 active run per target company-year | ✓ (all 3) |
| Exactly 24 active rows per target company-year | ✓ (all 3) |
| No existing V5 run for any target company-year | ✓ |
| XBRL warehouse facts = 225,780 | ✓ |
| Regression artifacts valid | ✓ |
| No other production-write process active (PID lock free) | ✓ |

## What `--execute` will do (built, not run)

1. **Backup**: timestamped copy of `ai_stock_agent.duckdb`, SHA-256
   source/backup equality required, reopened read-only and re-verified.
2. **Archive**: the 3 existing target run rows + 72 existing target
   quarterly rows to Parquet, re-read and row-count-verified.
3. **Manifest**: task ID, target company-years, old run IDs, regression
   artifact paths + hashes, backup path + hash, archive paths + counts,
   pre-load counts, expected post-load counts.
4. **Fresh engine execution**: for each of the 3 target company-years,
   run `scripts/148` exactly once as a subprocess with a real, killable
   45-second OS timeout; require exactly 24 rows; compare the result
   against both the saved TASK_148 regression result and current active
   production (value, unit, concept_qname, extraction_basis,
   reconciliation_status, availability_date, accession_number); stop
   immediately on any mismatch. Never runs V4. Never processes a
   non-target company-year.
5. **One atomic transaction** over all 3 company-years: delete the 72
   old rows + 3 old runs, insert 3 new runs
   (`engine_version=QUARTERLY_ENGINE_V5_STANDARD_GAAP_ALLOW_LIST`) and
   72 new rows. Pre-commit validation checks run/row counts, 24-per-
   company-year, 0 duplicate keys, 0 missing lineage, 0 null values, 0
   availability mismatches, all 4 target cases PASS, exactly 16
   changed/56 unchanged. Rolls back entirely on any failure, preserving
   backup/archive/logs, returning a non-zero exit code.
6. **Post-commit validation**: re-queried global counts (45/1,080/900/0
   REVIEW_REQUIRED), every company-year 24 rows, 0 duplicate keys, 0
   missing lineage, 0 availability mismatches, Annual V1 checksum
   unchanged, XBRL warehouse facts unchanged (225,780, never opened for
   writing).
7. **Process controls**: refuses to start if a live PID lock
   (`data/v5_production_load.pid`) exists; removes a stale one only
   after proving the PID is not active (`tasklist`-based check); writes
   `logs/v5_production_load.log`; atomically updates
   `data/v5_production_load_checkpoint.json` after every target
   company-year's engine run; writes
   `data/v5_production_load_result.json`/`.csv` at the end; returns
   exit code 0 only after complete post-commit validation, non-zero on
   any failure; never auto-resumes a previous failed run; never
   launches a background process itself.

## Task-evidence validation (TASK_149, via `scripts/142_task_marker_guard.py`)
`start_task()` was called before any build work began (`read_only:
true`, since --check-only never writes to production and --execute was
never run). After all mandatory outputs were finalized, `finish_task()`
hashed every one of them and wrote
`docs/tasks/TASK_149_V5_PRODUCTION_LOAD_BUILD_RESULT.json`;
`validate_task_evidence()` was then run against TASK_149's own
STARTED/RESULT pair. **Result: `valid=True`, 0 failure categories.**

## Files changed / created (latest bug-fix update — `q1_accession`)
- `scripts/151_v5_quarterly_production_load.py` (**edited in place** — added `enrich_target_company_years_with_accessions()` and one call site in `run_execute()`; still the only production-load script, no new script created)
- `data/v5_production_load_build_validation.json` (re-written by the post-fix `--check-only` run)
- `docs/V5_PRODUCTION_LOAD_BUILD.md` (this report, updated)
- `docs/LAST_CLAUDE_REPORT.md` — updated
- Temporary schema-test + targeted-validation scripts: written to and
  run from the session scratchpad only (not part of the repository, not
  a new production script)

Pre-existing, untouched by this update: `data/v5_production_load_result.json`
and `data/v5_production_load_checkpoint.json` — both still hold the
**`q1_accession` failure record** from the real `--execute` attempt
(`failed_at=2026-08-06T07:55:25Z`), left exactly as `--execute` wrote
them (this bug-fix task never ran `--execute`, so it never touched
these files). `data/database/backups/ai_stock_agent_pre_v5_production_load_20260806T075525Z.duckdb`
(the failed run's own backup) is likewise untouched and remains the
hash-verified proof of an unchanged production database.

No production database was modified by this bug-fix work. No new
backup, archive, PID lock, checkpoint, or result file was created —
confirmed both by the post-fix `--check-only` run's own self-report and
independent filesystem verification (production DB SHA-256 identical to
the failed run's own pre-load backup; both database file modification
timestamps unchanged from before this session). `docs/DECISIONS_LOG.md`
was **not** updated. `--execute` was **not** run.

## Result: BUILD PASS (both bugs fixed and independently verified)
The self-PID detection defect, the malformed-lock-file fail-open
defect, and the `q1_accession` schema-mismatch defect are all fixed.
Stage 1 proved production was already unchanged before any fix was
applied (hash-identical to the failed run's own backup). Stage 4's
targeted validation (schema test on all 3 targets + one fresh CRWD
engine run through the exact comparison path that previously crashed)
passed cleanly with no side effects. The real `--check-only` path now
passes (0.045s), with `execution_preconditions_met` correctly true.
`--execute` remains fully built but intentionally not invoked.

## The exact manual command to run the production load

```powershell
.\.venv\Scripts\python.exe .\scripts\151_v5_quarterly_production_load.py --execute
```

Not run by Claude, per explicit instruction.
