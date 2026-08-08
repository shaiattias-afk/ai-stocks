# Valuation V1 per-share inputs — RESULT: PASS

Completed the full closed release for Valuation V1 in one task,
recovering from the environment block resolved and confirmed in the
prior diagnostic task: environment precheck → determine the valuation
input → micro proof → full 45-company-year proof → historical P/E
proof → in-memory load proof → production load (run exactly once) →
independent post-load verification → documentation → Git.

Source code: `scripts/160_valuation_v1_per_share_inputs.py`
(`--check-only` for Stages 1–5, `--execute` for Stage 6, invoked once).
Freeze record: `data/valuation_v1_release_manifest.json`. Decision
recorded: `docs/DECISIONS_LOG.md` — **D-046**.

## Stage 0 — Environment precheck: PASS

`import duckdb`, `:memory:` open, production read-only open, and
`historical_prices_daily` row count (14,913) all confirmed working
before any project work began.

## Stage 1 — Determine the valuation input

Inspected the already-built XBRL warehouse (225,780 facts across all
45 already-locked 10-K accessions). Evaluated all 6 candidate items:
diluted EPS, basic EPS, diluted weighted-average shares, basic
weighted-average shares, period-end shares outstanding, net income
attributable to common — all 6 concepts exist for all 45 accessions at
the XBRL-tag level. **Chosen basis**: reported diluted EPS
(`us-gaap:EarningsPerShareDiluted`), resolved via a fully generic rule
(no ticker/year-specific logic): the single consolidated
(non-dimensional, `dimensions_json='{}'`) fact whose `period_end`
equals the filing's own `report_date` and whose duration is annual
(300–400 days). Shares outstanding is **not** required for P/E once
diluted EPS is available directly, and was therefore **not** stored in
production — used only as a transient cross-check.

## Stage 2 — Micro proof: PASS

MSFT, NVDA, AMZN, 2 fiscal years each — all 6 cases resolved with
reported diluted EPS matching the independently calculated
(`net_income / diluted weighted-average shares`) EPS exactly (diff
$0.00 in every micro-proof case; largest diff across all 45
company-years project-wide was $0.03, tolerance set at $0.05).

| Ticker | FY | Net income | Diluted EPS | Diluted shares | Calc. EPS | Diff | Filing date | Accession |
|---|---|---:|---:|---:|---:|---:|---|---|
| MSFT | 2024 | $88.136B | 11.80 | 7.469B | 11.80 | 0.00 | 2024-07-30 | 0000950170-24-087843 |
| MSFT | 2023 | $72.361B | 9.68 | 7.472B | 9.68 | 0.00 | 2023-07-27 | 0000950170-23-035122 |
| NVDA | 2024 | $29.760B | 11.93 | 2.494B | 11.93 | 0.00 | 2024-02-21 | 0001045810-24-000029 |
| NVDA | 2023 | $4.368B | 1.74 | 2.507B | 1.74 | 0.00 | 2023-02-24 | 0001045810-23-000017 |
| AMZN | 2025 | $77.670B | 7.17 | 10.827B | 7.17 | 0.00 | 2026-02-06 | 0001018724-26-000004 |
| AMZN | 2024 | $59.248B | 5.53 | 10.721B | 5.53 | 0.00 | 2025-02-07 | 0001018724-25-000004 |

## Stage 3 — Full 45-company-year proof: PASS

**45 / 45 resolved.** 0 unavailable, 0 ambiguous, 0 REVIEW_REQUIRED. 0
duplicate `(ticker, fiscal_year)` rows, complete lineage on every row,
`availability_date = filing_date`, no later filing ever used for an
earlier point (every resolution stays within its own single accession),
no ticker/year-specific code anywhere in the resolution logic.

## Stage 4 — Historical P/E proof: PASS

**A real defect was found and fixed here.** The first attempt used
Yahoo `close`, which Historical Price Policy V1 (D-044) Rule C
retroactively adjusts for later splits — but as-reported diluted EPS
is never split-adjusted. This silently distorted NVDA's 2024-02-21
P/E to 5.66 (should be ~56). Fixed by using `nominal_close` (the
reconstructed original-scale price), matching the EPS's unadjusted
scale. After the fix:

| Ticker | Diluted EPS | Availability date | Price date used | Nominal close | Historical P/E |
|---|---:|---|---|---:|---:|
| MSFT | 11.80 | 2024-07-30 | 2024-07-30 | 422.92 | **35.84** |
| NVDA | 11.93 | 2024-02-21 | 2024-02-21 | 674.72 | **56.56** |
| AMZN | 7.17 | 2026-02-06 | 2026-02-06 | 210.32 | **29.33** |

All three: price date on/after availability date (no look-ahead),
reproducible (recomputed independently, identical result), re-derived
directly from the committed production table post-load with identical
values.

## Stage 5 — In-memory load proof: PASS

Exact future production schema created in `:memory:`, all 45 rows
inserted in one transaction, commit succeeded: 9 tickers, 45 rows, 0
duplicate keys, 0 NULLs in required fields, all 3 P/E proof cases
re-validated against the in-memory table.

## Stage 6 — Production load: PASS

Ran exactly once:
```
.venv\Scripts\python.exe scripts\160_valuation_v1_per_share_inputs.py --execute
```
Verified backup created (SHA-256-matched) at
`data/database/backups/ai_stock_agent_pre_valuation_v1_load_20260808T045545Z.duckdb`,
one atomic transaction, `valuation_v1_per_share_inputs` created and 45
rows inserted.

## Stage 7 — Independent post-load verification: PASS

Re-opened production read-only: table exists, 45 rows, 9 distinct
tickers, 0 duplicate keys, 0 missing lineage, historical P/E re-derived
from the committed table matches the pre-load proof exactly. All
pre-existing data confirmed unchanged: `financial_metric_results`=900,
`quarterly_extraction_runs`=45, `quarterly_metric_results`=1,080,
`derived_metric_results`=405, `historical_prices_daily`=14,913, unique
REVIEW_REQUIRED=0, Annual Data V1 checksum unchanged
(`e655671e...58e9f814`).

## Stage 8 — Git

Committed with message `Add Valuation V1 per-share inputs` and tagged
`valuation-v1-frozen` (production load occurred and independently
passed).

## Files created/updated
- `scripts/160_valuation_v1_per_share_inputs.py` (new)
- `data/valuation_v1_build_validation.json` (new)
- `data/valuation_v1_preview.csv` (new)
- `data/valuation_v1_load_result.json` / `.csv` (new)
- `data/valuation_v1_release_manifest.json` (new)
- `docs/CURRENT_STATE.md` — updated
- `docs/DECISIONS_LOG.md` — D-046 added
- `docs/LAST_CLAUDE_REPORT.md` — this file, updated

Not committed (per policy): `data/database/ai_stock_agent.duckdb`, the
pre-load backup, the XBRL warehouse, logs, PID lock files.

## Result: PASS — Valuation V1 is now frozen

## Report — in simple terms

- **Is diluted EPS reliable for all 9 companies?** Yes — it resolved
  cleanly and directly for all 45 company-years, with no exceptions
  and no company-specific handling needed.
- **Is shares outstanding actually needed for V1?** No — historical
  P/E only needs the per-share earnings figure, which the filings
  already report directly. Shares outstanding was checked and used
  only to double-check the numbers, not stored.
- **What valuation input will we use?** The company's own reported
  diluted earnings-per-share figure, taken directly from its official
  annual filing.
- **How many of the 45 company-years were resolved?** All 45.
- **Can historical P/E be calculated safely now?** Yes — proven on
  Microsoft, NVIDIA, and Amazon, with one real bug caught and fixed
  along the way (a stock-split scaling mismatch that would have made
  NVIDIA's P/E look 10 times too low).
- **Was Production changed?** Yes — one new, minimal table was added
  with exactly the 45 validated numbers. Everything that existed
  before was independently re-checked afterward and confirmed
  unchanged.
- **Did all old frozen data remain unchanged?** Yes.
- **Git commit hash?** See below.
- **Recommended next step?** Build the Scoring Model V1 factors
  (already designed in the earlier blueprint) using this new valuation
  input together with the other frozen data, then build the
  point-in-time backtest.
