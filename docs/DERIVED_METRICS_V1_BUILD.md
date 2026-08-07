# Derived Metrics V1 — build for all 9 approved tickers — BUILD RESULT: PASS (check-only only; --execute never run)

Built `scripts/153_derived_metrics_v1_load.py`, generalizing the exact
formulas, point-in-time rules, and lineage logic proven single-ticker in
`scripts/152_msft_derived_metrics_proof.py` to all 9 approved tickers,
for exactly 2 metrics (`operating_margin`, `revenue_yoy_growth`) at
`annual` and `quarterly` frequency. This task ran **`--check-only`
only** — `--execute` was never invoked, per explicit instruction. No
production data was changed, no table was created.

## Schema fix: `fiscal_quarter` / `PRIMARY KEY` defect (this update)

The first real `--execute` attempt failed with `Constraint Error: NOT
NULL constraint failed: derived_metric_results.fiscal_quarter` (see
`docs/LAST_CLAUDE_REPORT.md`'s prior read-only verification — production
was proven unchanged, transaction rolled back cleanly). Root cause: a
column that is part of a `PRIMARY KEY` is implicitly `NOT NULL` in
DuckDB regardless of its own column-level declaration, but the schema
requires `fiscal_quarter IS NULL` for every annual row.

**Exact schema change** (`scripts/153_derived_metrics_v1_load.py`,
`DERIVED_METRIC_RESULTS_DDL`):

```sql
CREATE TABLE derived_metric_results (
    ticker                  VARCHAR NOT NULL,
    frequency               VARCHAR NOT NULL,
    fiscal_year_end         DATE NOT NULL,
    fiscal_year             INTEGER NOT NULL,
    fiscal_quarter          TINYINT,
    fiscal_quarter_key      TINYINT NOT NULL,
    derived_metric          VARCHAR NOT NULL,
    value                   DECIMAL(38,18) NOT NULL,
    availability_date       DATE NOT NULL,
    formula                 VARCHAR NOT NULL,
    source_periods          JSON NOT NULL,
    source_run_ids          JSON NOT NULL,
    source_accessions       JSON NOT NULL,
    reconciliation_status   VARCHAR NOT NULL,
    engine_version          VARCHAR NOT NULL,
    created_at              TIMESTAMP NOT NULL,
    CONSTRAINT chk_frequency_values
        CHECK (frequency IN ('annual', 'quarterly')),
    CONSTRAINT chk_annual_quarter_shape
        CHECK (frequency <> 'annual' OR (fiscal_quarter IS NULL AND fiscal_quarter_key = 0)),
    CONSTRAINT chk_quarterly_quarter_shape
        CHECK (frequency <> 'quarterly' OR (
            fiscal_quarter IS NOT NULL AND fiscal_quarter BETWEEN 1 AND 4 AND fiscal_quarter_key = fiscal_quarter
        )),
    CONSTRAINT chk_derived_metric_values
        CHECK (derived_metric IN ('operating_margin', 'revenue_yoy_growth')),
    PRIMARY KEY (ticker, frequency, fiscal_year_end, fiscal_quarter_key, derived_metric)
)
```

`fiscal_quarter_key` is a new `NOT NULL` surrogate column used only
inside the `PRIMARY KEY`: `0` for annual rows, `1`–`4` (mirroring
`fiscal_quarter`) for quarterly rows. `fiscal_quarter` itself keeps its
original semantics (`NULL` for annual, `1`–`4` for quarterly) and
remains the column callers should read. No new derived metric, no
calculation-formula change, no ticker- or year-specific logic.

**In-memory DuckDB schema test — 13/13 PASS** (`:memory:` only; no file
on disk, no production database opened at all):

| Case | Expected | Result |
|---|---|---|
| Annual row, `fiscal_quarter=NULL`, `fiscal_quarter_key=0` | inserts | ✓ PASS |
| Q1 / Q2 / Q3 / Q4 rows | insert | ✓ PASS (4/4) |
| Duplicate derived key | rejected | ✓ PASS (`PRIMARY KEY` violation) |
| Annual row with `fiscal_quarter_key != 0` | rejected | ✓ PASS (`chk_annual_quarter_shape`) |
| Quarterly row with `fiscal_quarter = NULL` | rejected | ✓ PASS (`chk_quarterly_quarter_shape`) — **caught a real defect in the first draft of this same constraint**, see below |
| Quarterly row, `fiscal_quarter_key != fiscal_quarter` | rejected | ✓ PASS (`chk_quarterly_quarter_shape`) |
| Quarterly row, quarter = 5 (outside 1–4) | rejected | ✓ PASS (`chk_quarterly_quarter_shape`) |
| Quarterly row, quarter = 0 (outside 1–4) | rejected | ✓ PASS (`chk_quarterly_quarter_shape`) |
| Invalid `frequency` value | rejected | ✓ PASS (`chk_frequency_values`) |
| Invalid `derived_metric` value | rejected | ✓ PASS (`chk_derived_metric_values`) |

**One additional defect found and fixed during this same in-memory
test, before `--check-only` was ever run**: the first draft of
`chk_quarterly_quarter_shape` used `fiscal_quarter BETWEEN 1 AND 4`
without an explicit `IS NOT NULL` guard. In SQL's three-valued logic,
`NULL BETWEEN 1 AND 4` evaluates to `NULL` (not `FALSE`), and a `CHECK`
constraint only **rejects** a row when its expression evaluates to
`FALSE` — `NULL` is treated as satisfied. A quarterly row with
`fiscal_quarter = NULL` therefore silently passed the first draft's
constraint. The in-memory test's "quarterly row with NULL fiscal_quarter
is rejected" case caught this immediately (`FAIL` on the first run);
the constraint was corrected to
`fiscal_quarter IS NOT NULL AND fiscal_quarter BETWEEN 1 AND 4 AND ...`,
and the full 13-case suite was re-run and passed cleanly.

**Post-fix `--check-only`**: **PASS, runtime 0.56s** — see updated
results below. `data/derived_metrics_v1_preview.csv` now includes the
new `fiscal_quarter_key` column for transparency (0 for every annual
row, 1–4 for every quarterly row, matching the schema exactly).

## Check-only run (post-fix) — PASS

```
.\.venv\Scripts\python.exe .\scripts\153_derived_metrics_v1_load.py --check-only
```

**Result: PASS. Runtime: 0.56s** (well under the 2-minute expectation).

## Source periods found, per ticker

| Ticker | Annual periods | Quarterly periods | Observations | Unresolved |
|---|---:|---:|---:|---:|
| ORCL | 5 | 20 | 45 | 5 |
| MSFT | 5 | 20 | 45 | 5 |
| META | 5 | 20 | 45 | 5 |
| NVDA | 5 | 20 | 45 | 5 |
| GOOGL | 5 | 20 | 45 | 5 |
| AMZN | 5 | 20 | 45 | 5 |
| MU | 5 | 20 | 45 | 5 |
| CRWD | 5 | 20 | 45 | 5 |
| PANW | 5 | 20 | 45 | 5 |
| **Total** | **45** | **180** | **405** | **45** |

Every one of the 9 tickers has an identical, clean shape (5 frozen
fiscal years, 4 quarters each) — a direct consequence of Quarterly Data
V1's freeze (D-042): project-wide unique REVIEW_REQUIRED is 0, so every
ticker's quarterly data is fully resolved with no gaps.

## Emitted observations by ticker, frequency, and metric

Per ticker (identical pattern across all 9, confirmed individually, not
assumed):

| Derived metric | Frequency | Observations | Unresolved |
|---|---|---:|---:|
| `operating_margin` | annual | 5 | 0 |
| `revenue_yoy_growth` | annual | 4 | 1 |
| `operating_margin` | quarterly | 20 | 0 |
| `revenue_yoy_growth` | quarterly | 16 | 4 |
| **Total per ticker** | | **45** | **5** |

**9 tickers × 45 = 405 total expected production rows.**

## Unresolved observations — exact reasons (45 total, 5 per ticker, uniform)

Every one of the 45 unresolved cases across all 9 tickers falls into
exactly one of these two categories — both are the structurally
inevitable boundary effect of a frozen 5-year data window (a YoY metric
needs a prior period that doesn't exist for the earliest year), **not**
a data-quality problem:

| Reason | Count |
|---|---:|
| `annual_revenue_yoy_growth`, earliest fiscal year: "no prior fiscal year revenue available in Annual Data V1 (earliest frozen fiscal year)" | 9 (1 per ticker) |
| `quarterly_revenue_yoy_growth`, earliest fiscal year's 4 quarters: "no same fiscal quarter in the prior fiscal year available in Quarterly Data V1 (earliest frozen fiscal year)" | 36 (4 per ticker) |

Zero missing-source-value cases, zero division-by-zero cases, zero
ambiguous-period-matching cases, zero non-PASS source rows, anywhere
across all 9 tickers.

## Validation results — all PASS

| Check | Result |
|---|---|
| Exactly 9 tickers processed | ✓ |
| No duplicate source rows | ✓ 0 |
| No duplicate derived keys | ✓ 0 |
| No missing lineage | ✓ 0 (all 405 observations carry complete `source_periods`/`source_run_ids`/`source_accessions`) |
| No future-data violations | ✓ (enforced at computation time using real `filing_date`/`availability_date` values, per ticker — never emitted if out of order) |
| No ambiguous prior-period matching | ✓ (fiscal-period identity throughout — never calendar-quarter assumptions; ambiguous cases route to `unresolved`, never guessed) |
| Every source run ID exists in Production | ✓ (re-queried against `extraction_runs`/`quarterly_extraction_runs`) |
| Every source accession exists in Production | ✓ (re-queried against `sec_filings`) |
| SQL and Python results match, every emitted row | ✓ **405/405**, tolerance `1e-9` absolute |
| **MSFT results match the existing single-ticker proof exactly** | ✓ **45/45** observations, 0 mismatches (`data/proofs/msft_derived_metrics_proof.json`) |
| Annual Data V1 checksum unchanged | ✓ |
| Quarterly Data V1 counts unchanged | ✓ (`quarterly_extraction_runs`/`quarterly_metric_results` re-queried before and after, identical) |
| Production database SHA-256 unchanged | ✓ `2a37d47b2257a34545196a9b4435f493cb88611215afb3f35a766d21fa325773` (identical before and after) |

## Point-in-time validation

Enforced identically to the MSFT proof, independently per ticker: every
YoY observation's `availability_date` is the `MAX()` of its two source
periods' own dates; any pair where the prior period's date would be
*after* the current period's date is routed to `unresolved` (reason
contains "future-data violation") and never emitted. **0 such rejections
occurred across all 9 tickers** — every ticker's frozen data is already
chronologically well-ordered.

## Exact database schema (corrected; created only during `--execute`, not run here)

```sql
CREATE TABLE derived_metric_results (
    ticker                  VARCHAR NOT NULL,
    frequency               VARCHAR NOT NULL,
    fiscal_year_end         DATE NOT NULL,
    fiscal_year             INTEGER NOT NULL,
    fiscal_quarter          TINYINT,
    fiscal_quarter_key      TINYINT NOT NULL,
    derived_metric          VARCHAR NOT NULL,
    value                   DECIMAL(38,18) NOT NULL,
    availability_date       DATE NOT NULL,
    formula                 VARCHAR NOT NULL,
    source_periods          JSON NOT NULL,
    source_run_ids          JSON NOT NULL,
    source_accessions       JSON NOT NULL,
    reconciliation_status   VARCHAR NOT NULL,
    engine_version          VARCHAR NOT NULL,
    created_at              TIMESTAMP NOT NULL,
    CONSTRAINT chk_frequency_values
        CHECK (frequency IN ('annual', 'quarterly')),
    CONSTRAINT chk_annual_quarter_shape
        CHECK (frequency <> 'annual' OR (fiscal_quarter IS NULL AND fiscal_quarter_key = 0)),
    CONSTRAINT chk_quarterly_quarter_shape
        CHECK (frequency <> 'quarterly' OR (
            fiscal_quarter IS NOT NULL AND fiscal_quarter BETWEEN 1 AND 4 AND fiscal_quarter_key = fiscal_quarter
        )),
    CONSTRAINT chk_derived_metric_values
        CHECK (derived_metric IN ('operating_margin', 'revenue_yoy_growth')),
    PRIMARY KEY (ticker, frequency, fiscal_year_end, fiscal_quarter_key, derived_metric)
);
```

- `frequency` is exactly `'annual'` or `'quarterly'` for every row (`CHECK`-enforced).
- `fiscal_quarter` is `NULL` for all 90 annual rows, `1`/`2`/`3`/`4` for all 315 quarterly rows — this is the column callers should read; its semantics are unchanged from the original design.
- `fiscal_quarter_key` (new) is a `NOT NULL` surrogate used only inside the `PRIMARY KEY`: `0` for annual rows, `1`–`4` (always equal to `fiscal_quarter`) for quarterly rows — `CHECK`-enforced to always stay in lockstep with `fiscal_quarter`, so it carries no independent information.
- `derived_metric` is exactly `'operating_margin'` or `'revenue_yoy_growth'` for every row (`CHECK`-enforced).
- `engine_version` is exactly `'DERIVED_METRICS_ENGINE_V1'` for every row.
- `value` is quantized to `DECIMAL(38,18)` (rounded from full-precision `Decimal` at insert time).
- Because `value` is `NOT NULL`, genuinely unresolved combinations are
  never inserted as placeholder rows at all (unlike `quarterly_metric_
  results`' NULL-for-REVIEW_REQUIRED convention) — "cannot calculate"
  means "no row", by construction of this schema.

## What `--execute` will do (built, not run)

1. Acquire a PID lock (`data/derived_metrics_v1_load.pid`), refusing to start on a live foreign lock, removing only a proven-stale one.
2. Re-verify all preconditions and **recalculate and revalidate the complete 9-ticker dataset from scratch** (never trusts a cached/prior result).
3. Create a timestamped backup of `ai_stock_agent.duckdb`, requiring SHA-256 equality with the source.
4. One atomic transaction: `DROP TABLE IF EXISTS` + `CREATE TABLE derived_metric_results` (exact DDL above) + insert every validated row.
5. Pre-commit checks inside the same transaction: row count matches expected, 0 duplicate keys, 0 NULL values, exactly 9 distinct tickers.
6. Roll back entirely on any failure, preserving the backup and log, returning a non-zero exit code.
7. Post-commit validation: `quarterly_extraction_runs`/`quarterly_metric_results` counts unchanged, inserted row count matches, Annual V1 checksum unchanged.
8. Write `data/derived_metrics_v1_load_result.json`/`.csv`, `data/derived_metrics_v1_release_manifest.json`, and append to `logs/derived_metrics_v1_load.log`.
9. Exit code 0 only after every post-commit check passes.

## Confirmation: no database was modified

`data/database/ai_stock_agent.duckdb` was opened `read_only=True`
throughout `--check-only`. SHA-256 before and after are identical
(`2a37d47b2257a34545196a9b4435f493cb88611215afb3f35a766d21fa325773`).
No PID lock, backup, archive, or table was created — confirmed both by
the script's own self-report and independent filesystem verification
(no `data/derived_metrics_v1_load.pid`, no
`data/derived_metrics_v1_load_result.json`, no
`data/derived_metrics_v1_release_manifest.json`, no new file in
`data/database/backups/`).

## Files created / modified (this schema-fix update)
- `scripts/153_derived_metrics_v1_load.py` (**edited in place** — `fiscal_quarter_key` column + 4 `CHECK` constraints + corrected `PRIMARY KEY`; still the only production-load script)
- `data/derived_metrics_v1_build_validation.json` (re-written by the post-fix `--check-only` run)
- `data/derived_metrics_v1_preview.csv` (405 rows, now includes `fiscal_quarter_key`)
- `docs/DERIVED_METRICS_V1_BUILD.md` (this report, updated)
- `docs/LAST_CLAUDE_REPORT.md` — updated
- Temporary in-memory schema test: written to and run from the session scratchpad only (`:memory:` DuckDB, no file, no production database — not part of the repository)

No production database was modified. `--execute` was **not** run.

## Result: BUILD PASS (schema fixed and verified)
The `fiscal_quarter`/`PRIMARY KEY` defect is fixed. All 13 in-memory
schema tests pass (including one the fix itself needed — see above).
Post-fix `--check-only` passed cleanly (0.56s), confirming: all 9
tickers still process identically and cleanly, exactly 405 rows would
be produced (45 per ticker), the MSFT subset still matches the existing
single-ticker proof exactly (45/45, 0 mismatches), SQL and Python
independently agree on every one of 405 rows, and the production
database remains untouched (SHA-256 identical before/after). `--execute`
remains fully built but intentionally not invoked.

## The exact manual command to run the production load

```powershell
.\.venv\Scripts\python.exe .\scripts\153_derived_metrics_v1_load.py --execute
```

Not run by Claude, per explicit instruction.
