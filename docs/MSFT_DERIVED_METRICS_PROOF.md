# MSFT derived-metrics layer — read-only proof — RESULT: PASS

Read-only proof of a derived-metrics layer, MSFT only, built strictly on
top of the now-frozen Annual Data V1 and Quarterly Data V1 (D-042).
`scripts/152_msft_derived_metrics_proof.py` computes 4 derived metrics,
each **independently twice** — once via DuckDB SQL, once via Python
`decimal.Decimal` — and requires the two to match within a defined
tolerance before accepting any value. **No production table was
created. No database was written to.**

## Actual source schemas used (inspected before writing any code)

| Table | Columns used | Notes |
|---|---|---|
| `financial_metric_results` | `extraction_run_id` (PK), `metric_name` (PK), `value`, `status`, `is_primary_metric`, `is_derived_metric`, `period_start`, `period_end` | No ticker/accession/availability columns of its own |
| `extraction_runs` | `extraction_run_id` (PK), `accession_number` | Joined on `extraction_run_id` |
| `sec_filings` | `accession_number` (PK), `ticker`, `form`, `report_date`, `filing_date`, `fiscal_year` | Joined on `accession_number`; supplies `filing_date` as the annual availability date |
| `quarterly_extraction_runs` | `run_id` (PK), `ticker`, `fiscal_year_end`, `fy_accession` | Used only to map `fiscal_year_end → fiscal_year` via `sec_filings` |
| `quarterly_metric_results` | `run_id, fiscal_quarter, metric_name` (PK), `value`, `unit`, `availability_date`, `accession_number`, `reconciliation_status` | Carries `ticker`, `availability_date`, `accession_number` directly — no join needed |

Confirmed by direct inspection, not assumed: `financial_metric_results`'
primary key is `(extraction_run_id, metric_name)` and
`quarterly_metric_results`' primary key is `(run_id, fiscal_quarter,
metric_name)` — both structurally preclude duplicate source rows for a
given period/metric, independently re-verified anyway (see Validation).
For MSFT, `is_primary_metric=True` and `is_derived_metric=False` for
every `revenue`/`operating_income` row (confirmed, not assumed) and
`status`/`reconciliation_status` is `PASS` for every row used.

## Source periods found

| | Periods | Source rows |
|---|---:|---:|
| Annual (Annual Data V1) | **5** fiscal years (FY2020–FY2024) | 10 (revenue + operating_income × 5) |
| Quarterly (Quarterly Data V1) | **20** fiscal quarters (5 years × 4 quarters) | 40 (revenue + operating_income × 20) |

## Derived observations created, by metric

| Derived metric | Observations | Unresolved | Reason |
|---|---:|---:|---|
| `annual_operating_margin` | 5 | 0 | — |
| `annual_revenue_yoy_growth` | 4 | 1 | FY2020: no prior fiscal year revenue available (earliest frozen fiscal year) |
| `quarterly_operating_margin` | 20 | 0 | — |
| `quarterly_revenue_yoy_growth` | 16 | 4 | FY2020 Q1–Q4: no same fiscal quarter in the prior fiscal year available (earliest frozen fiscal year) |
| **Total** | **45** | **5** | |

No other period could not be calculated — the only unresolved cases are
the structurally-inevitable ones at the start of the frozen data window
(YoY growth needs a prior period that doesn't exist for the earliest
year). Zero missing-source-value cases, zero division-by-zero cases,
zero ambiguous-period-matching cases.

## Calculated MSFT results

**Annual** (values as ratios; percentages shown for readability):

| FY | Operating margin | Revenue YoY growth |
|---|---:|---:|
| 2020 | 37.03% | — (no prior year) |
| 2021 | 41.59% | 17.53% |
| 2022 | 42.06% | 17.96% |
| 2023 | 41.77% | 6.88% |
| 2024 | 44.64% | 15.67% |

**Quarterly**:

| FY | Q | Operating margin | Revenue YoY growth |
|---|---|---:|---:|
| 2020 | Q1 | 38.38% | — |
| 2020 | Q2 | 37.64% | — |
| 2020 | Q3 | 37.05% | — |
| 2020 | Q4 | 35.25% | — |
| 2021 | Q1 | 42.73% | 12.40% |
| 2021 | Q2 | 41.55% | 16.72% |
| 2021 | Q3 | 40.88% | 19.09% |
| 2021 | Q4 | 41.37% | 21.35% |
| 2022 | Q1 | 44.66% | 21.97% |
| 2022 | Q2 | 43.01% | 20.09% |
| 2022 | Q3 | 41.26% | 18.35% |
| 2022 | Q4 | 39.59% | 12.38% |
| 2023 | Q1 | 42.93% | 10.60% |
| 2023 | Q2 | 38.67% | 1.97% |
| 2023 | Q3 | 42.29% | 7.08% |
| 2023 | Q4 | 43.17% | 8.34% |
| 2024 | Q1 | 47.59% | 12.76% |
| 2024 | Q2 | 43.59% | 17.58% |
| 2024 | Q3 | 44.59% | 17.03% |
| 2024 | Q4 | 43.14% | 15.20% |

Full precision (28-digit `Decimal`) values are in
`data/proofs/msft_derived_metrics_proof.csv` / `.json`.

## Validation results — all PASS

| Check | Result |
|---|---|
| No duplicate derived keys | ✓ 0 |
| No missing lineage | ✓ 0 (every one of the 45 observations carries `source_periods`, `source_run_ids`, `source_accessions`) |
| No future-data use | ✓ (enforced at computation time using real `filing_date`/`availability_date` values — any YoY pair with an out-of-order prior/current date is routed to `unresolved`, never emitted; 0 such rejections occurred for MSFT's own data) |
| No duplicate source rows | ✓ 0 (re-verified beyond the schema's own PK guarantee) |
| Every formula uses the correct source periods | ✓ (verified structurally — margin uses exactly 1 period, YoY uses exactly `[prior, current]` in that order, fiscal-quarter identity used for the quarterly prior match, never calendar-quarter arithmetic) |
| Every source accession and run ID exists in Production | ✓ (independently re-queried against `sec_filings` / `extraction_runs` / `quarterly_extraction_runs`) |
| SQL and Python results match | ✓ **45/45**, tolerance `1e-9` absolute (DuckDB SQL division/`LAG()` window functions vs. Python `decimal.Decimal` division from the same retrieved source rows) |
| Production database SHA-256 unchanged | ✓ `2a37d47b2257a34545196a9b4435f493cb88611215afb3f35a766d21fa325773` (identical before and after) |

## Periods that could not be calculated (exact reasons)

| Derived metric | Period | Exact reason |
|---|---|---|
| `annual_revenue_yoy_growth` | FY2020-06-30 | No prior fiscal year revenue available in Annual Data V1 (earliest frozen fiscal year) |
| `quarterly_revenue_yoy_growth` | FY2020-06-30 Q1 | No same fiscal quarter in the prior fiscal year available in Quarterly Data V1 (earliest frozen fiscal year) |
| `quarterly_revenue_yoy_growth` | FY2020-06-30 Q2 | Same as above |
| `quarterly_revenue_yoy_growth` | FY2020-06-30 Q3 | Same as above |
| `quarterly_revenue_yoy_growth` | FY2020-06-30 Q4 | Same as above |

## Confirmation: no database was modified

`data/database/ai_stock_agent.duckdb` was opened `read_only=True`
throughout. SHA-256 before and after are identical
(`2a37d47b2257a34545196a9b4435f493cb88611215afb3f35a766d21fa325773`).
No other database was opened at all. No production table was created.

## Recommended schema for a future production derived-metrics table

```sql
CREATE TABLE derived_metric_results (
    ticker              VARCHAR NOT NULL,
    frequency           VARCHAR NOT NULL,   -- 'annual' | 'quarterly'
    fiscal_year_end     VARCHAR NOT NULL,
    fiscal_year         INTEGER,
    fiscal_quarter      VARCHAR,            -- NULL for annual rows
    derived_metric      VARCHAR NOT NULL,   -- e.g. 'annual_operating_margin'
    value               DOUBLE,
    availability_date   VARCHAR NOT NULL,   -- max() of all source availability/filing dates
    formula             VARCHAR NOT NULL,
    source_periods      VARCHAR NOT NULL,   -- JSON array, ordered [prior, current] for YoY
    source_run_ids      VARCHAR NOT NULL,   -- JSON array (extraction_run_id / quarterly run_id)
    source_accessions   VARCHAR NOT NULL,   -- JSON array
    reconciliation_status VARCHAR NOT NULL, -- 'PASS' | 'REVIEW_REQUIRED', mirrors the quarterly convention
    engine_version       VARCHAR NOT NULL,  -- e.g. 'DERIVED_METRICS_ENGINE_V1'
    created_at            VARCHAR NOT NULL,
    PRIMARY KEY (ticker, frequency, fiscal_year_end, fiscal_quarter, derived_metric)
);
```

Mirrors the existing `quarterly_metric_results` conventions
(string-array lineage columns, explicit `reconciliation_status`,
`availability_date` as the point-in-time gate) so it can reuse the same
loader/backup/archive/atomic-transaction discipline already established
for Annual/Quarterly Data V1 when this becomes a real production table
— not proposed for creation in this task.

## Files created
- `scripts/152_msft_derived_metrics_proof.py` (new)
- `data/proofs/msft_derived_metrics_proof.csv` (45 rows, long format)
- `data/proofs/msft_derived_metrics_proof.json` (full detail: schemas used, source counts, all 45 observations with full-precision values and lineage, all 5 unresolved periods with exact reasons, validation results, database hashes)
- `docs/MSFT_DERIVED_METRICS_PROOF.md` (source of this report)
- `docs/LAST_CLAUDE_REPORT.md` — updated

No database (`ai_stock_agent.duckdb` or otherwise) was modified.

## Result: PASS
Runtime **0.088s** (well under the 5-minute expectation). All 45
derived observations computed, cross-validated SQL-vs-Python within
`1e-9`, complete lineage, 0 future-data violations, 0 duplicate keys,
production database confirmed byte-identical before and after.

## One exact next step
Not requested by this task. If a production derived-metrics table is
wanted next, the recommended schema above, generalized beyond MSFT to
all 9 tickers already in Annual/Quarterly Data V1, with the same
backup/archive/atomic-transaction discipline used throughout this
project, would be the natural follow-up — out of scope here.
