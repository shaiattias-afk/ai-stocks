# NVDA FY2020-01-26 quarterly production load — RESULT: PASS (4-phase load, 24/24 rows committed)

Loaded the already-validated 24-row NVDA FY2020 (fiscal-year-end
2020-01-26) quarterly V4 proof (`data/nvda_fy2020_quarterly_v4_proof.json`,
produced read-only by TASK_145/`scripts/146`) into quarterly production
(`data/database/ai_stock_agent.duckdb`) for exactly this one company-year.
The quarterly engine was **not** rerun — `scripts/147` reads only the
saved proof JSON. The XBRL warehouse and annual production table were
never opened for writing. No other company-year was touched.

## Wording-inconsistency resolution ("24 vs. 21") — required by this task

The prior report (`docs/NVDA_FY2020_QUARTERLY_V4_PROOF.md`, "Production
comparison" section) stated *"24 of 24 rows changed... (21
`DIRECT_QUARTER`/`DERIVED_FROM_YTD`/`DERIVED_Q4_FROM_10K_MINUS_9M` basis,
all reconciling to `PASS`)"* — a genuine miscount/typo on my part.

Phase 1 of `scripts/147` explicitly counted all 24 proof rows by
`extraction_basis`, directly from `data/nvda_fy2020_quarterly_v4_proof.json`:

| extraction_basis | count |
|---|---:|
| `DIRECT_QUARTER` | 14 |
| `DERIVED_FROM_YTD` | 4 |
| `DERIVED_Q4_FROM_10K_MINUS_9M` | 6 |
| **Total** | **24** |

**Verdict: the correct, verified total is 24, not 21.** Every row has a
non-null `extraction_basis`, and every value is one of the 3 expected
bases (`DIRECT_QUARTER`, `DERIVED_FROM_YTD`, `DERIVED_Q4_FROM_10K_MINUS_9M`)
— 0 unrecognized values, 0 missing values. No proof data was ever
actually missing; only the prose summary in the earlier markdown report
was imprecise. This reconciliation is corrected here and the "21" figure
should be considered superseded by this document.

## Phase 1 — proof validation (fail-closed) — all checks passed
| Check | Result |
|---|---|
| Proof `status` | `PASS` |
| Ticker / fiscal_year_end | `NVDA` / `2020-01-26` — matched |
| Row count | 24 — matched |
| Metrics × quarters | 6 × 4 = 24, 0 duplicate keys |
| `metric_outcomes` | all 6 = `PASS` |
| `still_review_required` | empty |
| Null values | 0 |
| `reconciliation_status` not `PASS` | 0 |
| Missing `lineage_json` | 0 |
| Missing `availability_date` | 0 |
| Point-in-time violations | 0 |
| Comparative-fact violations | 0 |
| Unexplained differences / regressions | 0 / 0 |
| Extraction-basis reconciliation | 24 = 14+4+6, all recognized (see above) |
| Q1 rows | all accession `0001045810-19-000079`, period `2019-01-28→2019-04-28`, availability `2019-05-16` |
| Q4 rows | all accession `0001045810-20-000010` |

## Phase 2 — production preconditions — all met
| Check | Result |
|---|---|
| Global counts before | `quarterly_extraction_runs`=45, `quarterly_metric_results`=1,080, `financial_metric_results`=900, unique REVIEW_REQUIRED=10 |
| Existing NVDA run | `run_id=59b524e2-4639-4af8-82c3-390b2363c40d`, `engine_version=118_quarterly_extraction_engine_v1`, `run_status=PASS_WITH_REVIEW_REQUIRED` |
| Existing V4 run for this company-year | none found (as required) |
| Existing rows | 24, all `REVIEW_REQUIRED`, covering exactly the 6 target metrics |

## Phase 3 — backup, archive, manifest — all re-read-verified
- Backup: `data/database/backups/ai_stock_agent_pre_nvda_fy2020_quarterly_load_20260805T161808Z.duckdb`
  — SHA-256 `1179b12aacd0a1edb9463c34593fc9f84236a71aaa962e81e167605e4090268b`,
  matches source exactly; reopened read-only, counts re-verified identical
  to pre-load state.
- Archived old run row (1 row) → `data/archive/nvda_fy2020_quarterly_run_replaced_20260805T161808Z.parquet`
  (re-read-verified: 1 row).
- Archived old 24 quarterly rows → `data/archive/nvda_fy2020_quarterly_rows_replaced_20260805T161808Z.parquet`
  (re-read-verified: 24 rows).
- Manifest: `data/archive/nvda_fy2020_quarterly_production_load_manifest.json`
  (written, re-read, byte-compared identical).

## Phase 4 — atomic transaction — COMMITTED
- Old run (`59b524e2-4639-4af8-82c3-390b2363c40d`) and its 24 rows deleted.
- New run inserted: `run_id=22c174be-d095-48da-8d68-8388805b9c7d`,
  `engine_version=QUARTERLY_ENGINE_V4_POINT_IN_TIME_CONCEPT_REUSE`,
  `q1_accession=0001045810-19-000079`, `q2_accession=0001045810-19-000144`,
  `q3_accession=0001045810-19-000170`, `fy_accession=0001045810-20-000010`,
  `run_status=PASS`.
- 24 new rows inserted, preserving every field from the proof
  (`value`, `unit`, `result_status`, `extraction_basis`, `period_start`,
  `period_end`, `availability_date`, `accession_number`, `concept_qname`,
  `context_id`, `lineage_json`, `reconciliation_status`,
  `reconciliation_difference`, `permitted_difference`).
- Pre-commit validation: 24 rows, 0 duplicate natural keys, 0 missing
  lineage, 0 null values, 0 unrecognized extraction bases, 0 availability
  mismatches vs. `sec_filings.filing_date`, all Q4 rows correctly anchored
  to the annual accession, all 24 rows byte/value-matched against the
  proof JSON (value, basis, status, accession, concept_qname) — 0
  mismatches found, so the transaction committed (not rolled back).
- `reconciliation_status_counts` after insert: `{'PASS': 24}`.

## Phase 5 — post-commit validation — ALL CHECKS PASSED
| Check | Result |
|---|---|
| `quarterly_extraction_runs` | 45 (unchanged — 1 replaced, not added) |
| `quarterly_metric_results` | 1,080 (unchanged — 24 replaced, not added) |
| `financial_metric_results` | 900 (untouched) |
| Every company-year has exactly 24 rows | yes, 0 exceptions |
| Duplicate natural keys | 0 |
| Missing lineage | 0 |
| Availability-date mismatches | 0 |
| Future-data violations (NVDA) | 0 |
| NVDA FY2020 rows with `PASS` | 24 / 24 |
| NVDA FY2020 rows `REVIEW_REQUIRED` | 0 |
| `ai_stock_agent_annual_v1.duckdb` checksum | unchanged |
| XBRL warehouse total `xbrl_facts` | 225,780 — unchanged (never opened for writing) |

**Project-wide unique REVIEW_REQUIRED metric-years: 10 → 4** (actual
re-queried value, not forced). The 4 remaining cases, confirmed to match
exactly the expected set:
- `CRWD 2022-01-31 pretax_income`
- `MU 2021-09-02 pretax_income`
- `PANW 2021-07-31 pretax_income`
- `PANW 2021-07-31 revenue`

Each of these has no point-in-time-safe fix by construction (D-039) and
remains explicitly out of scope for this task.

## Task-evidence validation (TASK_146, via `scripts/142_task_marker_guard.py`)
`start_task()` was called before any Phase 1 work began, obtaining
`started_at` internally from `datetime.now(timezone.utc)`. After all
mandatory outputs were finalized, `finish_task()` hashed every one of
them and wrote
`docs/tasks/TASK_146_NVDA_FY2020_QUARTERLY_PRODUCTION_LOAD_RESULT.json`;
`validate_task_evidence()` was then run against TASK_146's own
STARTED/RESULT pair. **Result: `valid=True`, 0 failure categories.**

## Files created
- `scripts/147_nvda_fy2020_quarterly_production_load.py` (new)
- `data/nvda_fy2020_quarterly_production_load_result.json` (full detail: phase-by-phase results, all 24 loaded rows, integrity checks)
- `data/nvda_fy2020_quarterly_production_load_result.csv` (flat summary)
- `data/archive/nvda_fy2020_quarterly_production_load_manifest.json`
- `data/archive/nvda_fy2020_quarterly_run_replaced_20260805T161808Z.parquet`
- `data/archive/nvda_fy2020_quarterly_rows_replaced_20260805T161808Z.parquet`
- `data/database/backups/ai_stock_agent_pre_nvda_fy2020_quarterly_load_20260805T161808Z.duckdb`
- `docs/NVDA_FY2020_QUARTERLY_PRODUCTION_LOAD.md` (source of this report)
- `docs/tasks/TASK_146_NVDA_FY2020_QUARTERLY_PRODUCTION_LOAD_STARTED.json` / `_RESULT.json` (task-marker evidence)
- `docs/LAST_CLAUDE_REPORT.md`, `docs/CURRENT_STATE.md` — updated

No warehouse file, annual production table, or non-NVDA-FY2020 quarterly
row was modified. `docs/DECISIONS_LOG.md` was **not** updated — no new
standing policy was adopted by this task, per explicit instruction.

## Result: PASS
All 5 phases completed; the old REVIEW_REQUIRED run for NVDA FY2020 was
replaced (not appended) by 24 fully-resolved `PASS` rows under a new
`QUARTERLY_ENGINE_V4_POINT_IN_TIME_CONCEPT_REUSE` run; every global and
per-company-year integrity check re-queried clean; project-wide unique
REVIEW_REQUIRED dropped from 10 to the expected 4, with the exact
expected 4 cases confirmed remaining.
