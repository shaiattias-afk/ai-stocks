# Quarterly engine V5 — standard GAAP concept allow-list fallback — RESULT: PASS (4/4 target cases resolved, 0 regressions)

Read-only proof of a new quarterly extraction engine V5
(`scripts/148_quarterly_engine_v5_standard_gaap_fallback.py`), which adds
exactly one new, fixed, versioned concept-resolution tier — a standard
US-GAAP concept allow-list — tried only after both the primary
presentation-based resolver and the existing point-in-time-safe
concept-reuse fallback (V4) have failed. **All 4 remaining
REVIEW_REQUIRED quarterly metric-year cases now resolve to exact
`PASS`. All 96 regression-control rows remain byte-identical to engine
V4. Neither production database was written to.**

## The allow-list (exact, versioned: `STANDARD_GAAP_ALLOW_LIST_V1`)

| Metric | Allowed concepts (in priority/selection order) |
|---|---|
| `revenue` | `us-gaap:Revenues`, `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`, `us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax`, `us-gaap:SalesRevenueNet`, `us-gaap:SalesRevenueGoodsNet`, `us-gaap:SalesRevenueServicesNet` |
| `pretax_income` | `us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest`, `us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments` |

No other metric has an allow-list — tier 3 is simply skipped for
`operating_income`, `income_tax_expense`, `operating_cash_flow`, and
`capex` (these were never REVIEW_REQUIRED for the target cases and are
out of scope). Rejected categories (`CostOfRevenue`, `DeferredRevenue*`,
`ContractWithCustomerLiability*`, `RevenueRemainingPerformanceObligation*`,
dimensioned/segment revenue, `IncomeTaxExpenseBenefit`,
`OperatingIncomeLoss`, `NetIncomeLoss`, non-`us-gaap:` extension
concepts) are never queried at all — structurally excluded by only ever
iterating the allow-list above, not by substring matching or a
blocklist check.

## Resolution priority (unchanged order, one tier added)
1. Primary presentation-based resolver.
2. Point-in-time-safe concept reuse (V4, unchanged).
3. **Standard US-GAAP allow-list fallback — NEW.**
4. REVIEW_REQUIRED.

The new tier only ever supplies a **concept_qname candidate**. The
actual financial value is always re-selected fresh from the blocking
quarter's own exact accession via the unchanged `facts_for_concept` +
`pick_current_period_fact` pipeline, which independently re-enforces
every existing safeguard (exact accession, exact `period_end`, correct
duration bucket, no dimensions, single deterministic value after
same-context precision-duplicate reconciliation).

## Validation A — the 4 target cases (derived from production, not hardcoded)

Target company-years were derived by querying
`quarterly_metric_results` for `reconciliation_status='REVIEW_REQUIRED'`
— 3 distinct company-years, family counts **pretax_income=3,
revenue=1**, matching the task's expectation exactly.

| Ticker | FY end | Metric | Old status | V5 status | V4-only rerun (isolation) | Fallback activated |
|---|---|---|---|---|---|---|
| CRWD | 2022-01-31 | pretax_income | REVIEW_REQUIRED | **PASS** | REVIEW_REQUIRED | Yes (Q1) |
| MU | 2021-09-02 | pretax_income | REVIEW_REQUIRED | **PASS** | REVIEW_REQUIRED | Yes (Q1) |
| PANW | 2021-07-31 | pretax_income | REVIEW_REQUIRED | **PASS** | REVIEW_REQUIRED | Yes (Q1) |
| PANW | 2021-07-31 | revenue | REVIEW_REQUIRED | **PASS** | REVIEW_REQUIRED | Yes (Q1) |

**Resolved: 4/4.** (The task explicitly allowed PASS with fewer than 4
resolved, provided every remaining case had an exact evidence-based
reason — that contingency was not needed.) Re-running engine V4 alone
(no tier 3) against the same accessions confirms all 4 still return
REVIEW_REQUIRED — isolating the improvement specifically to the new
tier, not to any other change.

### Selected concepts, exact blocking accessions, values, and rejected candidates

**CRWD FY2022 pretax_income** (blocking accession `0001535527-21-000013`, Q1 report_date 2021-04-30):
- Selected: `us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest` → **-$32,809,000** (Q1)
- Rejected: `...MinorityInterestAndIncomeLossFromEquityMethodInvestments` — no dimensionless, non-nil, numeric fact for this concept in the blocking accession
- Q2 (`0001535527-21-000022`) = -$53,080,000, Q3 (`0001535527-21-000028`) = -$45,977,000 — both `DIRECT_QUARTER`, concept reused from Q1 via tier 2 (`EARLIER_SAME_FISCAL_YEAR_QUARTER`)
- Q4 = -$28,157,000 (`DERIVED_Q4_FROM_10K_MINUS_9M`, annual accession `0001535527-22-000006`)
- **Reconciliation: sum=-$160,023,000, annual=-$160,023,000, diff=0.00 → PASS**

**MU FY2021 pretax_income** (blocking accession `0000723125-21-000012`, Q1 report_date 2020-12-03):
- Selected: `us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments` → **$841,000,000** (Q1)
- Rejected: `...ExtraordinaryItemsNoncontrollingInterest` — no dimensionless, non-nil, numeric fact for this concept in the blocking accession
- Q2 (`0000723125-21-000032`) = $635,000,000, Q3 (`0000723125-21-000052`) = $1,806,000,000 — both `DIRECT_QUARTER`, concept reused from Q1 via tier 2
- Q4 = $2,936,000,000 (`DERIVED_Q4_FROM_10K_MINUS_9M`, annual accession `0000723125-21-000065`)
- **Reconciliation: sum=$6,218,000,000, annual=$6,218,000,000, diff=0.00 → PASS**

**PANW FY2021 pretax_income** (blocking accession `0001327567-20-000041`, Q1 report_date 2020-10-31):
- Selected: `us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest` → **-$82,300,000** (Q1)
- Rejected: `...MinorityInterestAndIncomeLossFromEquityMethodInvestments` — no dimensionless, non-nil, numeric fact for this concept in the blocking accession
- Q2 (`0001327567-21-000004`) = -$130,000,000, Q3 (`0001327567-21-000014`) = -$150,400,000 — both `DIRECT_QUARTER`, concept reused from Q1 via tier 2
- Q4 = -$102,300,000 (`DERIVED_Q4_FROM_10K_MINUS_9M`, annual accession `0001327567-21-000029`)
- **Reconciliation: sum=-$465,000,000, annual=-$465,000,000, diff=0.00 → PASS**

**PANW FY2021 revenue** (blocking accession `0001327567-20-000041`, Q1 report_date 2020-10-31):
- Selected: `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax` → **$946,000,000** (Q1)
- Rejected (5): `us-gaap:Revenues`, `...IncludingAssessedTax`, `us-gaap:SalesRevenueNet`, `us-gaap:SalesRevenueGoodsNet`, `us-gaap:SalesRevenueServicesNet` — each: no dimensionless, non-nil, numeric fact for this concept in the blocking accession
- Q2 (`0001327567-21-000004`) = $1,016,900,000, Q3 (`0001327567-21-000014`) = $1,073,900,000 — both `DIRECT_QUARTER`, concept reused from Q1 via tier 2
- Q4 = $1,219,300,000 (`DERIVED_Q4_FROM_10K_MINUS_9M`, annual accession `0001327567-21-000029`)
- **Reconciliation: sum=$4,256,100,000, annual=$4,256,100,000, diff=0.00 → PASS**

In every one of the 4 families, the new tier activated **only at Q1**
(the earliest quarter, with no earlier same-fiscal-year quarter and no
prior-fiscal-year 10-K to reuse from — exactly the structural gap
identified by the earlier V4 validation, `scripts/137`). Q2 and Q3 then
resolved via the **existing, unchanged** tier-2 point-in-time-safe
concept-reuse mechanism, reusing the concept name Q1's tier-3 activation
established. Q4 never needed either tier — it uses the annual
production result's own already-resolved concept (D-037).

## Validation B — 96-row regression control (MSFT/AMZN/ORCL FY2024 + NVDA FY2020)

Control company-years were derived from `quarterly_extraction_runs`
(`ticker IN ('MSFT','AMZN','ORCL') AND fiscal_year_end LIKE '2024-%'`,
plus `NVDA AND fiscal_year_end LIKE '2020-%'`) — not hardcoded.

| Ticker | FY end | V4 rows | V5 rows | Identical | Fallback activations |
|---|---|---:|---:|---|---:|
| AMZN | 2024-12-31 | 24 | 24 | **Yes** | 0 |
| MSFT | 2024-06-30 | 24 | 24 | **Yes** | 0 |
| NVDA | 2020-01-26 | 24 | 24 | **Yes** | 0 |
| ORCL | 2024-05-31 | 24 | 24 | **Yes** | 0 |
| **Total** | | **96** | **96** | **Yes** | **0** |

All 96 rows identical across value, extraction_basis,
reconciliation_status, and availability_date (ORCL's operating_income
and pretax_income legitimately resolve to `PASS_ROUNDING_TOLERANCE` in
**both** V4 and V5, identically — pre-existing D-035 rounding-tolerance
behavior, unrelated to this task). **0 regressions. The standard-GAAP
allow-list tier never activated on any control row** — confirming it is
purely additive and inert wherever the existing tiers already succeed.

## Validation C — target safety checks (all passed)

| Check | Result |
|---|---|
| Value comes from the blocking 10-Q itself | ✓ all 4 cases (`blocking_accession` == the quarter's own accession) |
| No future filing referenced | ✓ (tier 3 only ever queries the blocking accession itself; tier 2's `source_filing_date <= blocking_filing_date` independently re-verified for every Q2/Q3 reuse) |
| No same-year later 10-K used | ✓ (structurally forbidden — `attempt_concept_reuse_fallback`'s own prior-10-K query filters `report_date < fiscal_year_end`) |
| No comparative value selected | ✓ every selected quarter's `period_end` matches that exact quarter's own `report_date`, re-verified independently from the saved JSON |
| Exactly one allowed standard concept selected | ✓ all 4 (`us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest` ×3, `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax` ×1 — each literally present in the allow-list) |
| No extension concept selected | ✓ all 4 selected concepts start with `us-gaap:` |
| Full-year quarterly sum reconciles to annual | ✓ all 4, exact `diff=0.00`, well inside permitted tolerance |

## Database safety — confirmed unchanged

| Check | Before | After |
|---|---:|---:|
| `quarterly_extraction_runs` | 45 | 45 |
| `quarterly_metric_results` | 1,080 | 1,080 |
| `financial_metric_results` | 900 | 900 |
| unique REVIEW_REQUIRED (production, unaffected by this read-only proof) | 4 | 4 |
| XBRL warehouse `xbrl_facts` | 225,780 | 225,780 |

Both `data/database/ai_stock_agent.duckdb` and
`data/database/xbrl_warehouse_proof.duckdb` were opened **read-only**
throughout `scripts/148` and `scripts/149`. No production row, no
warehouse table, was written to.

## Task-evidence validation (TASK_147, via `scripts/142_task_marker_guard.py`)
`start_task()` was called before any Phase 1 work began (`read_only:
true`), obtaining `started_at` internally from
`datetime.now(timezone.utc)`. After all mandatory outputs were
finalized, `finish_task()` hashed every one of them and wrote
`docs/tasks/TASK_147_STANDARD_GAAP_CONCEPT_FALLBACK_PROOF_RESULT.json`;
`validate_task_evidence()` was then run against TASK_147's own
STARTED/RESULT pair. **Result: `valid=True`, 0 failure categories.**

## Files created
- `scripts/148_quarterly_engine_v5_standard_gaap_fallback.py` (new engine)
- `scripts/149_standard_gaap_fallback_validation.py` (new validation driver)
- `data/standard_gaap_fallback_validation.json` (full detail: allow-list, all 4 target cases with full lineage/rejected candidates, all 96 control rows, old-vs-new comparisons, fallback activations, point-in-time checks, reconciliation checks, database before/after counts, runtime)
- `data/standard_gaap_fallback_validation.csv` (flat summary)
- `docs/STANDARD_GAAP_FALLBACK_VALIDATION.md` (source of this report)
- `docs/tasks/TASK_147_STANDARD_GAAP_CONCEPT_FALLBACK_PROOF_STARTED.json` / `_RESULT.json` (task-marker evidence, `valid=True`)
- `docs/LAST_CLAUDE_REPORT.md`, `docs/CURRENT_STATE.md` — updated

No production database or warehouse file was modified.
`docs/DECISIONS_LOG.md` was **not** updated — this is still a proof, per
explicit instruction.

## Actual runtime

The validation driver's wall-clock `time.perf_counter()` measurement
recorded **28,702.34 seconds**. This figure is **not** genuine
computation time — the host environment was suspended for an extended
idle period partway through the run (the system clock rolled from
2026-08-05 to 2026-08-06 mid-execution with no corresponding gap in the
script's own console output). The actual engine computation, visible
directly in the unbroken console log (7 company-years × up to 2 engine
runs each = 12 engine invocations, each completing in low single-digit
seconds exactly like the individual company-year proofs in TASK_145/146),
is consistent with the task's own **~1–3 minute** expectation for engine
execution. This is flagged explicitly rather than silently reported, per
the project's "do not fabricate or misrepresent technical results" rule.

## Result: PASS
All 4 target cases fully reported and resolved; all 96 control rows
remain byte-identical to engine V4 with 0 fallback activations; no
future-data violation occurred at any point; no ambiguous case was
silently accepted (every rejection is logged with an exact reason); both
production databases confirmed unchanged throughout.

## One exact next step
V5 (`scripts/148`) appears **safe for production adoption**: it strictly
extends V4 with one new, evidence-gated, fail-closed tier that never
activated on any of the 96 already-resolved control rows and resolved
all 4 remaining target cases with clean exact reconciliation. The
natural next step — **not started, out of scope for this proof** —
would be a TASK_148-style production load of these 4 now-resolved
metric-year rows into `quarterly_metric_results`, following the same
backup/archive/atomic-transaction discipline as
`scripts/130`/`134`/`138`/`147`, which would bring the project-wide
unique REVIEW_REQUIRED count from 4 to **0**.
