# TASK_141 — Global warehouse-ingestion integrity audit — RESULT: PASS

Read-only audit of every 10-K and 10-Q accession registered in
`sec_filings`. No warehouse loader was created or modified, no Arelle
process was run, no filing was re-warehoused, and no row in either
production database was written.

- **STARTED file**: `docs/tasks/TASK_141_STARTED.json`, timestamp `2026-08-05T09:25:00Z`.
- **Completion timestamp**: `2026-08-05T09:23:40Z`.
- **Script created**: `scripts/141_global_warehouse_integrity_audit.py` (new; no existing script modified).

## Total accessions audited: 185
Matches the authoritative `sec_filings` count exactly (135 10-Q + 50
10-K). Every accession was classified — 0 unresolved, 0 duplicates.

## Counts by form
| Form | Count |
|---|---:|
| 10-Q | 135 |
| 10-K | 50 |

## Counts by ticker
AMZN=21, CRWD=21, GOOGL=21, META=21, MSFT=20, MU=20, NVDA=21, ORCL=20, PANW=20.

## Counts by detected filing format
| Format | Count |
|---|---:|
| `INLINE_XBRL` | 183 |
| `TRADITIONAL_XBRL_SEPARATE_INSTANCE` | 2 |

## Counts by anomaly category
| Category | Count |
|---|---:|
| `VALID_INLINE_XBRL` | 183 |
| `VALID_TRADITIONAL_XBRL` | 1 |
| `FALSE_PASS_ZERO_CONTENT` | 1 |
| (all other categories) | 0 |

**Every other anomaly category checked for — `PARTIAL_CONTENT_
INCONSISTENCY`, `RUN_COUNT_DATABASE_MISMATCH`, `PHYSICAL_CONTENT_
WITHOUT_VALID_RUN`, `ENTRY_POINT_NOT_RESOLVED`, `MULTIPLE_INSTANCE_
CANDIDATES`, `LOCKED_PACKAGE_MISSING`, `MANIFEST_MISSING_OR_INVALID`,
`NO_VALID_WAREHOUSE_RUN`, `OTHER` — had exactly 0 matches across the
entire 185-accession universe.**

## Exact list of every affected accession
Only one accession has any anomaly at all:
- **`NVDA 0001045810-19-000079`** — category `FALSE_PASS_ZERO_CONTENT`.
  This is the same accession already root-caused and scratch-proven
  fixable in the prior task (`scripts/139`/`140`).

## Confirmation: is NVDA the only false PASS?
**Yes — confirmed.** `nvda_is_only_false_pass = true`. Exactly 1
`FALSE_PASS_ZERO_CONTENT` record exists in the entire universe, and its
ticker is NVDA.

## Exact list of every traditional-XBRL filing
Exactly 2, both NVDA, both from its pre-Inline-XBRL era:
- `NVDA 0001045810-19-000023` (10-K) — classified `VALID_TRADITIONAL_
  XBRL`: its warehouse content is present, internally consistent, and
  matches its recorded row counts. **Already correctly warehoused —
  no repair needed.**
- `NVDA 0001045810-19-000079` (10-Q) — classified `FALSE_PASS_ZERO_
  CONTENT`, the one broken accession.

## Exact list requiring re-warehouse
Exactly one: **`NVDA 0001045810-19-000079`**. No other accession in the
185-accession universe requires any repair or re-warehousing.

## Exact list whose state cannot be proven
**None.** `accessions_with_unproven_state = []`. Every accession was
definitively classified with supporting evidence; the task was not
downgraded to FAIL.

## Confirmation that no production row or schema changed
Both databases were opened `read_only=True` throughout; the script
issues zero `INSERT`/`UPDATE`/`DELETE`/`ALTER` statements. Re-verified
directly, before and after the audit ran:
- `ai_stock_agent.duckdb`: `quarterly_extraction_runs`=45,
  `quarterly_metric_results`=1,080, `financial_metric_results`=900 —
  identical before and after.
- `xbrl_warehouse_proof.duckdb`: total `xbrl_facts`=225,126 — identical
  before and after.

## Files created
- `scripts/141_global_warehouse_integrity_audit.py`
- `data/warehouse_global_integrity_audit.json` (one record per audited accession, 185 total, plus summary)
- `data/warehouse_global_integrity_audit.csv` (185 rows)
- `docs/tasks/TASK_141_STARTED.json`
- `docs/tasks/TASK_141_RESULT.json`
- `docs/tasks/TASK_141_RESULT.md` (this file)
- `docs/LAST_CLAUDE_REPORT.md` (convenience copy)
- `docs/CURRENT_STATE.md` (updated per instructions)

## Actual runtime
Local audit script execution: **0.26 seconds** (well within the
5–30 second expectation — no Arelle, no network, purely local file/DB
reads).

## PASS or FAIL: **PASS**
All fail-closed requirements met: every registered accession classified;
JSON record count (185) equals audited accession count (185); 0
duplicate records; every anomaly (the single NVDA case) has supporting
evidence; both databases confirmed read-only throughout; production
counts confirmed unchanged before/after.

## One exact next step
Build a production version of the corrected entry-point-detection loader
(generalizing `scripts/139`'s already-proven logic) and re-warehouse only
`NVDA 0001045810-19-000079` into the real `xbrl_warehouse_proof.duckdb`,
with a full backup/checksum/verify cycle — then re-run engine v4
(`scripts/136`) for NVDA FY2020-01-26 to determine whether its 6 metrics
now resolve, and load into production if they do. This is the **only**
remaining repair identified anywhere in the 185-accession universe.
