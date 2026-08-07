# Quarterly engine V5 — final release regression (45/45 company-years) — RESULT: PASS

The one final release regression for quarterly engine V5
(`scripts/148_quarterly_engine_v5_standard_gaap_fallback.py`) across all
**45** authoritative company-years currently in quarterly production.
V5 was run **exactly once per company-year** as a real subprocess (a
genuine, OS-enforced 45-second wall-clock kill switch — not a soft
Python-level timer), and every one of the resulting rows was compared
directly against the **current active production row**. Engine V4
(`scripts/136`) was never rerun. Nothing was loaded into production.

## Result: PASS — all 45/45 company-years, all global success conditions met

| Global check | Result |
|---|---|
| All 45 company-years complete | ✓ |
| Exactly 1,080 V5 rows produced | ✓ (45 × 24) |
| Every company-year has 24 rows | ✓ |
| Duplicate keys | 0 |
| Missing lineage | 0 |
| Availability-date mismatches (vs. `sec_filings.filing_date`) | 0 |
| Future-data violations | 0 |
| All 4 target cases resolve | ✓ |
| No other production row changed | ✓ |
| Expected REVIEW_REQUIRED after a future load | **0** |
| Both production databases unchanged | ✓ |
| Engine invocations | exactly 45 |
| V4 rerun | **No — never invoked** |

## The 4 target metric-year cases — all resolved, all safety checks passed

| Ticker | FY end | Metric | Rows changed | Allow-list activation quarter | Reconciliation |
|---|---|---|---:|---|---|
| CRWD | 2022-01-31 | pretax_income | 4 (Q1–Q4) | Q1 | PASS, diff=0.00 |
| MU | 2021-09-02 | pretax_income | 4 (Q1–Q4) | Q1 | PASS, diff=0.00 |
| PANW | 2021-07-31 | pretax_income | 4 (Q1–Q4) | Q1 | PASS, diff=0.00 |
| PANW | 2021-07-31 | revenue | 4 (Q1–Q4) | Q1 | PASS, diff=0.00 |

16 rows total changed (4 metric-year cases × 4 quarters each) — matches
the exact set predicted by TASK_147, no more, no fewer. For every one of
these 4 families, the required safety checks all passed:
- value came from the exact blocking 10-Q (`blocking_accession` == the quarter's own accession)
- the selected concept is literally in `STANDARD_GAAP_ALLOW_LIST_V1`
- no future filing was used (point-in-time-safety re-verified independently)
- no comparative fact was selected (`period_end` matches each quarter's own `report_date`)
- full-year quarterly sum reconciles exactly to the annual result (`difference=0.00` in all 4 cases)
- complete lineage present on every row

Every other row in these same 3 company-years (the other 5 metrics ×
4 quarters each) was **unchanged** — identical value, extraction_basis,
reconciliation_status, and availability_date to current production.

## The other 41 company-years — 100% identical to current production

All 984 rows (41 × 24) across AMZN (5), GOOGL (5), META (5), MSFT (5),
MU (4 non-target years), NVDA (5), ORCL (5), CRWD (4 non-target years),
PANW (4 non-target years) matched current production **exactly** — 0
changes, 0 unexpected differences anywhere. No company-year triggered
the new standard-GAAP-allow-list tier outside the 3 known target
company-years.

## Performance report

| Metric | Value |
|---|---:|
| Engine invocations | 45 (expected 45) |
| Active execution time (sum of per-company-year elapsed) | 583.02s (~9.7 min) |
| Wall-clock runtime | 584.18s |
| Any company-year over the 30s soft-warning threshold | **No** |
| Any company-year over the 45s hard timeout | **No** (none killed) |
| V4 rerun | **No** |

**Slowest 5 company-years** (all well under both the 30s soft warning
and the 45s hard limit):

| Ticker | FY end | Elapsed |
|---|---|---:|
| MSFT | 2024-06-30 | 23.26s |
| GOOGL | 2025-12-31 | 18.75s |
| CRWD | 2026-01-31 | 18.32s |
| MSFT | 2020-06-30 | 18.01s |
| GOOGL | 2023-12-31 | 18.00s |

Active execution (583.02s / ~9.7 minutes) ran somewhat longer than the
task's ~3–8 minute estimate (each subprocess pays a fresh Python/Arelle-
adjacent-module cold-start cost on top of the actual engine work, since
each company-year is a genuinely separate OS process — the intentional
cost of enforcing a *real*, killable per-company-year timeout rather
than an in-process soft timer). No company-year came close to either
the 30s or 45s threshold, so no correctness or reliability risk was
observed from this overhead.

## Checkpointing and progress reporting

`data/v5_final_release_regression.json` and
`data/v5_final_release_regression.csv` were atomically rewritten
(temp-file + `os.replace`) after every single company-year, so both
files reflected a fully consistent snapshot of progress at every point
during the run. One progress line was printed after every company-year
(`N/45 TICKER FY STATUS elapsed=Xs changed=Y`). Both controls were
independently confirmed still functioning mid-run before this task
completed.

## Database safety — confirmed unchanged

| Check | Before | After |
|---|---:|---:|
| `quarterly_extraction_runs` | 45 | 45 |
| `quarterly_metric_results` | 1,080 | 1,080 |
| `financial_metric_results` | 900 | 900 |
| unique REVIEW_REQUIRED | 4 | 4 |
| XBRL warehouse `xbrl_facts` | 225,780 | 225,780 |

Both `data/database/ai_stock_agent.duckdb` and
`data/database/xbrl_warehouse_proof.duckdb` were opened **read-only**
throughout `scripts/150` (and, per subprocess, throughout every
`scripts/148` invocation). No production row, no warehouse table, was
written to.

## Task-evidence validation (TASK_148, via `scripts/142_task_marker_guard.py`)
`start_task()` was called before any Phase 1 work began (`read_only:
true`). After all mandatory outputs were finalized, `finish_task()`
hashed every one of them and wrote
`docs/tasks/TASK_148_V5_FINAL_RELEASE_REGRESSION_RESULT.json`;
`validate_task_evidence()` was then run against TASK_148's own
STARTED/RESULT pair. **Result: `valid=True`, 0 failure categories.**

## Files created
- `scripts/150_v5_final_release_regression.py` (new)
- `data/v5_final_release_regression.json` (full detail: all 45 company-year results, all 16 changed rows with full lineage, all family safety checks, performance report, global checks, database before/after counts)
- `data/v5_final_release_regression.csv` (flat per-company-year summary)
- `docs/V5_FINAL_RELEASE_REGRESSION.md` (source of this report)
- `docs/tasks/TASK_148_V5_FINAL_RELEASE_REGRESSION_STARTED.json` / `_RESULT.json` (task-marker evidence, `valid=True`)
- `docs/LAST_CLAUDE_REPORT.md`, `docs/CURRENT_STATE.md` — updated

No production database or warehouse file was modified. `docs/DECISIONS_LOG.md`
was **not** updated — per explicit instruction (still a proof, no
production load performed yet).

## Result: PASS
All 45 company-years completed with no early stop; exactly 1,080 V5 rows
produced; 0 duplicate keys, 0 missing lineage, 0 availability mismatches,
0 future-data violations anywhere in the full 45-company-year universe;
all 4 target cases resolved with every required safety check passing; no
other production row changed anywhere; both production databases
confirmed unchanged.

## One exact next step
V5 is now regression-clean across the **entire** authoritative
production universe (not just the 96-row sample from TASK_147) and
appears fully ready for production adoption. The next step — **not
started, out of scope for this task** — would be a production load of
the 4 now-resolved target metric-year cases (16 rows total) into
`quarterly_metric_results`, following the same backup/archive/atomic-
transaction discipline as `scripts/130`/`134`/`138`/`147`, which would
bring the project-wide unique REVIEW_REQUIRED count from 4 to **0**, and
would be the point at which promoting V5 as the standing production
engine (a `docs/DECISIONS_LOG.md` entry) becomes appropriate.
