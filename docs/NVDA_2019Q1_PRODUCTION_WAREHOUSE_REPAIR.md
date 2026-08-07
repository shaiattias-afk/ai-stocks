# NVDA 2019 Q1 production warehouse repair — RESULT: PASS

Promoted the scratch-proven entry-point-detection and fail-closed
loading logic into a production warehouse loader
(`scripts/144_warehouse_loader_v2_production.py`), then used it via a
bounded, one-accession repair runner
(`scripts/145_nvda_2019q1_production_warehouse_repair.py`) to load NVDA
accession `0001045810-19-000079` into the **real** production warehouse
(`data/database/xbrl_warehouse_proof.duckdb`) for the first time with
real content. Only this one accession was touched.

## Production-loader design
`scripts/144_warehouse_loader_v2_production.py` is general — no ticker
or accession name appears anywhere in its logic, only as data flowing
through parameters. It re-derives (not imports) `scripts/139`'s
entry-point detection: check the primary document for an Inline-XBRL
namespace marker first; if absent, require exactly one standalone
`<xbrli:xbrl>` instance document in the locked package (excluding
schemas and linkbases) — fail closed (`ENTRY_POINT_NOT_RESOLVED` /
`MULTIPLE_INSTANCE_CANDIDATES`) on zero or multiple candidates, never a
guess. `run_production_warehouse_load()` never sets `status="PASS"`
unless: the locked package and manifest are complete; the entry point
resolved unambiguously; Arelle's DTS load returns exactly one model;
`xbrl_facts`/`xbrl_contexts`/`xbrl_concepts` are all `> 0`; `xbrl_units`
is `> 0` whenever at least one extracted fact carries a monetary
(`usd`) unit; the counts physically re-queried immediately after
insertion, inside the same open transaction, exactly equal the counts
computed in memory (`INSERTED_COUNT_MISMATCH` otherwise); and complete
accession + selected-entry-point lineage is present. Arelle returning
without a Python exception is explicitly **not** sufficient — this is
the exact check the original `scripts/121` loader was missing. Uses
`internetConnectivity="offline"` — zero network access, resolving
entirely from this project's own pre-populated Arelle taxonomy cache.

## Backup and archive
- **Backup**: `data/database/backups/xbrl_warehouse_proof_pre_nvda_2019q1_repair_20260805T150426Z.duckdb`
- **Source checksum**: `2211db4304944289b20723b491059c3b1fcfda848ef5d1289772c4d825c38101`
- **Backup checksum**: `2211db4304944289b20723b491059c3b1fcfda848ef5d1289772c4d825c38101` — **match confirmed**; backup opened read-only and its `xbrl_facts` total independently re-verified (225,126, matching the pre-repair state exactly).
- **Archived** (all pre-load, before any write): all 9 content tables (0 rows each — the accession's true pre-repair state) + `warehouse_runs` (2 rows, the two historical false-PASS records) as Parquet, each re-read and count-verified after writing.
- **Manifest**: `data/archive/nvda_2019q1_production_warehouse_repair_manifest.json` — written, then re-read and byte-compared against what was intended before Phase 2 completed.

## Preconditions — all verified before any write was attempted
Locked manifest resolved and matched the target accession; entry point
resolved to exactly `nvda-20190428.xml`
(`TRADITIONAL_XBRL_SEPARATE_INSTANCE`); its checksum and the primary
document's checksum matched the scratch proof's recorded values exactly
(confirming the locked package is unchanged since `scripts/139`/`140`
proved it fixable); pre-load physical counts were all zero across all 9
tables; exactly 2 historical `PASS` runs existed (both false-PASS,
pre-corrected-loader); no corrected-loader run already existed; total
production `xbrl_facts` = 225,126; `ai_stock_agent.duckdb` counts
(45 / 1,080 / 900 / 10 unique REVIEW_REQUIRED) all matched expected. No
active Python/Arelle process was found immediately before execution.

## Exact pre-load and post-load counts
| Table | Pre-load | Post-load | Expected |
|---|---:|---:|---:|
| `xbrl_facts` | 0 | **654** | 654 ✓ |
| `xbrl_contexts` | 0 | **134** | 134 ✓ |
| `xbrl_units` | 0 | **5** | 5 ✓ |
| `xbrl_concepts` | 0 | **711** | 711 ✓ |
| `xbrl_labels` | 0 | **942** | 942 ✓ |
| `xbrl_presentation_relationships` | 0 | **498** | 498 ✓ |
| `xbrl_calculation_relationships` | 0 | **134** | 134 ✓ |
| `xbrl_definition_relationships` | 0 | **517** | 517 ✓ |
| `xbrl_roles` | 0 | **92** | 92 ✓ |

**Every count matches the scratch-proof value exactly.** Total production
`xbrl_facts`: **225,126 → 225,780** (delta = 654, exactly the new
accession's fact count — no other accession's facts changed).

## Corrected run and preserved historical runs
- **Corrected `warehouse_runs` record**: `0001045810-19-000079::144_warehouse_loader_v2_production.py::2026-08-05T15:04:28.319063+00:00` — `status=PASS`, `row_counts_json` matches the physical counts above exactly.
- **Both historical false-PASS records preserved unchanged**, byte-for-byte:
  - `0001045810-19-000079::123_quarterly_batch_runner_45_company_years.py::2026-08-04T09:31:49.458225+00:00`
  - `0001045810-19-000079::124_quarterly_schema_nullable_fix_and_resume.py::2026-08-04T15:28:11.929007+00:00`
- **Exactly one corrected `PASS` run exists** for this accession — confirmed by direct query after commit.

## All six target metrics confirmed present, current-period, non-dimensioned
| Metric | Concept | Value | Period | Context |
|---|---|---:|---|---|
| revenue | `us-gaap:Revenues` | $2,220,000,000 | 2019-01-28 → 2019-04-28 | `FD2020Q1YTD` |
| operating_income | `us-gaap:OperatingIncomeLoss` | $358,000,000 | 2019-01-28 → 2019-04-28 | `FD2020Q1YTD` |
| pretax_income | `us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments` | $389,000,000 | 2019-01-28 → 2019-04-28 | `FD2020Q1YTD` |
| income_tax_expense | `us-gaap:IncomeTaxExpenseBenefit` | −$5,000,000 | 2019-01-28 → 2019-04-28 | `FD2020Q1YTD` |
| operating_cash_flow | `us-gaap:NetCashProvidedByUsedInOperatingActivities` | $720,000,000 | 2019-01-28 → 2019-04-28 | `FD2020Q1YTD` |
| capex | `us-gaap:PaymentsToAcquirePropertyPlantAndEquipment` | $128,000,000 | 2019-01-28 → 2019-04-28 | `FD2020Q1YTD` |

Current Q1 period confirmed exactly `2019-01-28` through `2019-04-28`.
Comparative-period facts (e.g. context `FD2019Q1QTD`, `2018-01-29` →
`2018-04-29`) are present under distinct context IDs, confirmed disjoint
from the current-period context set — comparative and current data are
correctly distinguishable, not merged.

## Proof that only one accession changed
- `COUNT(DISTINCT accession_number) FROM xbrl_facts` = **185** after the
  repair (184 + this one newly-populated accession — every other
  accession in the 185-accession universe already had facts, per
  TASK_141's audit).
- The known-good Inline-XBRL baseline accession (`0001045810-19-000144`,
  used in the scratch proof) re-checked at **920 facts**, byte-identical
  to before.
- Database structure (table list) unchanged: the same 9 content tables +
  `warehouse_runs`, nothing added or removed.

## Proof that annual and quarterly production were untouched
`ai_stock_agent.duckdb` was never opened for writing by either
`scripts/144` or `scripts/145`. Re-verified read-only after commit:
`quarterly_extraction_runs`=45, `quarterly_metric_results`=1,080,
`financial_metric_results`=900, unique REVIEW_REQUIRED=10 — all
identical to the pre-task state. The quarterly extraction engine was not
run; no quarterly or annual result was modified, per this task's explicit
scope exclusion.

## Task-evidence validation (TASK_144, via `scripts/142_task_marker_guard.py`)
`start_task()` was called before any work began, obtaining `started_at`
internally from `datetime.now(timezone.utc)` (no hand-typed timestamp).
After all mandatory outputs were finalized, `finish_task()` hashed every
one of them and wrote `docs/tasks/TASK_144_WAREHOUSE_LOADER_V2_NVDA_
PRODUCTION_REPAIR_RESULT.json`; `validate_task_evidence()` was then run
against TASK_144's own STARTED/RESULT pair. See the accompanying
`docs/tasks/TASK_144_...RESULT.md` / `.json` for the exact validation
outcome recorded at completion time.

## Files created or modified
- `scripts/144_warehouse_loader_v2_production.py` (new)
- `scripts/145_nvda_2019q1_production_warehouse_repair.py` (new)
- `data/nvda_2019q1_production_warehouse_repair_result.json` (new)
- `data/nvda_2019q1_production_warehouse_repair_result.csv` (new)
- `data/archive/nvda_2019q1_production_warehouse_repair_manifest.json` (new)
- `data/archive/nvda_2019q1_repair_pre_*_20260805T150426Z.parquet` (new, 10 files — 9 content tables + `warehouse_runs`, pre-load archive)
- `data/database/backups/xbrl_warehouse_proof_pre_nvda_2019q1_repair_20260805T150426Z.duckdb` (new)
- `data/database/xbrl_warehouse_proof.duckdb` — **modified**: exactly one accession's rows inserted across 9 tables + one new `warehouse_runs` row; nothing else changed.
- `docs/NVDA_2019Q1_PRODUCTION_WAREHOUSE_REPAIR.md` (this file)
- `docs/tasks/TASK_144_WAREHOUSE_LOADER_V2_NVDA_PRODUCTION_REPAIR_STARTED.json` / `_RESULT.json` / (task-marker evidence)
- `docs/LAST_CLAUDE_REPORT.md`, `docs/CURRENT_STATE.md`, `docs/DECISIONS_LOG.md` — updated

`data/database/ai_stock_agent.duckdb` — **not modified**.

## Actual runtime
**3.27 seconds** (script-reported, all 4 phases: preconditions, backup/
archive, atomic Arelle load + write, post-commit validation) — within the
2–15 second expectation.

## Result: PASS
All 10 post-commit integrity checks passed: exact 9-table counts, correct
total-facts delta, all 6 target metrics present, correct current-period
dates, comparative/current context separation, both historical runs
preserved unchanged, exactly one corrected PASS run, known-good baseline
accession unchanged, database structure unchanged, `ai_stock_agent.duckdb`
completely unchanged.

## One exact next step
Run the quarterly extraction engine (`scripts/136`, point-in-time-safe
concept reuse) for NVDA fiscal-year-end 2020-01-26 to determine whether
its 6 quarterly metrics now resolve against the newly-populated warehouse
content, and — if they do — load the result into quarterly production
following the same transactional backup/archive/validate discipline as
`scripts/134`/`138` (D-037/D-038/D-039). Explicitly out of scope for this
task; not started.
