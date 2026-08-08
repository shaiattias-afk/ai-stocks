# AI Stock Agent — Current State

**Last updated:** 2026-08-08 (**PR1 structural extraction opened (not
merged): `refactor/extract-stock-agent-package`, PR "refactor: extract
stock_agent package".** Moves the annual/quarterly XBRL policy-engine
lineage out of the numbered `scripts/` prototypes into an installable
package, `src/stock_agent/` (`extraction/`, `policies/`, `metrics/`,
`warehouse/`, `storage/`, `filings/`), with `scripts/171_recompute_
annual_company_year.py` as the read-only verification script (no
database writes anywhere in this task; PR2, separately, will formalize
this into a permanent pytest suite). This is a **pure structural
move** — no formula, threshold, regex, or precedence order was changed;
`docs/DECISIONS_LOG.md` D-015 through D-047 remain the binding source
of truth for every accounting policy, unchanged.

**Verification result (read-only, against the live `data/database/
ai_stock_agent.duckdb` and `xbrl_warehouse_proof.duckdb`, never
written to):** recomputed all 20 primary annual metrics for the 45
approved company-years plus the 5 supplementary prior-fiscal-year
accessions (AMZN 2020-12-31, CRWD 2021-01-31, GOOGL 2020-12-31, META
2019-12-31, NVDA 2019-01-27 — identified read-only from `extraction_
runs`/`sec_filings`, confirmed to carry zero rows of their own in the
frozen `financial_metric_results` table by design) using ONLY the new
package. First pass: 854 of 900 pairs matched exactly, with 46 rows
(AMZN/GOOGL `total_debt`-family metrics) carrying an identical value
but a more specific status label (`PASS_DIRECT_AGGREGATE`, from the
newer D-027 Policy C direct-aggregate tier) than the live table's
`PASS_MATURITY_BASIS`. Root-caused, not accepted as a known gap: a
direct query of `extraction_runs.engine_version` for exactly those 46
rows confirmed they were written by `scripts/79, D-022` — `scripts/92`'s
own Policy C was only ever run against its bounded 12-filing
`TARGET_FILINGS` scope, which never included AMZN/GOOGL, so their rows
were always resolved by scripts/79's earlier, narrower 2-tier precedence
(GAAP carrying value, then bucket-sum — never the reported Total row
directly); for AMZN/GOOGL the reported Total happens to equal the
bucket sum, which is why the value matched but the status didn't.
Fixed by porting scripts/79's original narrower resolver into
`policies/debt_total_aggregate.py` alongside both scripts' exact
historical `TARGET_FILINGS`/`TARGET_FILINGS` scopes, and selecting the
resolver by accession scope in `metrics.annual.compute_company_year` —
reproducing exactly which historical script produced each row, not
"whichever policy tier happens to resolve." **Independently re-run
after the fix (by the orchestrating session, not just the implementing
agent): 900 of 900 (accession_number, metric_name) pairs match the live
table exactly, value and status, zero mismatches.**

Quarterly: `scripts/148_quarterly_engine_v5_standard_gaap_fallback.py`
was edited in place (same path/number) to import its logic from the new
`stock_agent.extraction.quarterly` module instead of importlib-loading
`scripts/89` (now archived); re-running `scripts/150_v5_final_release_
regression.py` (unmodified) against the refactored 148 confirmed **45/45
company-years PASS, 1,080/1,080 rows produced, 0 changed, databases
unchanged** — byte-identical to the pre-refactor baseline.

Archived (via `git mv`, never deleted): the confirmed-dead
`scripts/42`-`scripts/59` and `scripts/72` range, plus the now-
superseded, fully-ported lineage `scripts/79, 82, 84, 87, 89, 92, 93,
94, 95, 96, 98, 99, 101, 102, 103, 105` — all now under
`archive/scripts/`. `scripts/144` (warehouse loader) and `scripts/106`
onward not in that explicit list are untouched. See the PR description
for the full module map and the complete verification output.

This entry documents a task result only — no data, policy, or
production table changed. See the PR for full detail.)

**Previous update:** 2026-08-08 (**D-047: the table-freeze policy (D-042/
D-043/D-045/D-046) has been replaced, for its "no writes without a new
engine version" restriction only, by code-enforced versioned
append-only writes.** New shared module
`scripts/167_versioned_write_guard.py` enforces, INSIDE every future
write transaction, before COMMIT: append-only (DELETE/UPDATE/DROP/
TRUNCATE/ALTER and any overwrite-shaped INSERT are rejected by the
module's only write chokepoint), row count per table never decreases,
the checksum of every pre-existing row (by primary key) is
byte-identical after the write, and the actual row-count delta exactly
equals the caller's own declared delta. Verified by
`scripts/168_versioned_write_guard_tests.py`, 5/5 required tests PASS
against an isolated scratch database (DELETE rejected, overwrite
rejected, declared-100-actual-101 rejected, a crash mid-load — 2 of N
inserts executed, connection closed without COMMIT — leaves the
database completely unchanged, a legitimate append succeeds with every
prior row byte-identical). `scripts/169_versioned_columns_migration.py
--execute` added `engine_version`/`loaded_at`/`is_active` to all six
previously-frozen tables (`financial_metric_results`,
`quarterly_extraction_runs`, `quarterly_metric_results`,
`derived_metric_results`, `historical_prices_daily`,
`valuation_v1_per_share_inputs`), backfilling every pre-existing row
from real, traceable sources (existing `extraction_runs`/`quarterly_
extraction_runs` foreign keys, each table's own existing `created_at`,
or — where no per-row engine label existed at all — the exact script
that produced the rows, per D-045/D-046); independently re-verified
read-only: all six row counts unchanged (900/45/1,080/405/14,913/45),
0 NULLs in any new column, `is_active=TRUE` on all 2,933 pre-existing
rows, every pre-existing column's content byte-identical, every other
table in the database untouched. **First real use**:
`scripts/170_historical_prices_append.py --execute` appended 9 new
rows (one 2026-08-07 close per approved ticker) to
`historical_prices_daily` through the write guard — bringing it current
from the D-045 freeze date (2026-08-06) without a new engine version or
manual regression. A real, honest pre-write finding (not a bug): Yahoo
had revised `volume` for 2026-08-06 slightly upward for all 9 tickers
between the original 2026-08-06 load and this run (same-day volume
finalization, a known data-provider behavior) — every price/nominal/
dividend/split field matched exactly. Since `volume` carries no defined
role in any binding price/valuation policy (D-044) and the row was
never written to, this was treated as informational and reported, not
blocking; every material field remained a hard, fail-closed gate.
`historical_prices_daily`: 14,913 → **14,922** rows, 2020-01-02 →
**2026-08-07**, 1,658 rows per ticker. Independently re-verified: row
count matches exactly, 0 duplicate keys, all 9 new rows carry
`engine_version='HISTORICAL_PRICES_APPEND_V1 (scripts/170_...)'` and
`is_active=TRUE`, and — checked against the pre-append backup file
directly, not the load script's own report — the SHA-256 checksum of
all 14,913 pre-existing rows (original columns only) is byte-identical
before and after:
`1e2ad3d268c2369676aa8987172620e39e06384bbdc0e39a65279ce323c5da25`.
See `docs/DECISIONS_LOG.md` D-047 for full detail.)

**Previous update:** 2026-08-08 (**VALUATION V1 IS FROZEN.** `scripts/160_valuation_v1_per_share_inputs.py --execute` succeeded (run exactly once, after a full read-only proof: inventory → micro proof (MSFT/NVDA/AMZN) → 45-company-year proof → historical P/E proof → in-memory load proof): `valuation_v1_per_share_inputs` created and loaded for all 9 approved tickers, **45/45 company-years resolved** using **reported diluted EPS** (`us-gaap:EarningsPerShareDiluted`), extracted directly from the already-locked 10-K filings via the already-built XBRL warehouse — no new filing downloaded, no external/analyst data used. Shares outstanding was evaluated but deliberately **not** stored in production (no required use once diluted EPS is available directly). A real defect was found and fixed before load: pairing `close` (retroactively split-adjusted per D-044 Rule C) with as-reported (never split-adjusted) diluted EPS silently distorted P/E for company-years preceding a later split (NVDA 2024-02 understated 10x); fixed by using `nominal_close`. Independently re-verified read-only, directly from the live database: table exists, exactly 45 rows, exactly 9 distinct tickers, 0 duplicate keys, 0 missing lineage, `availability_date=filing_date` on every row, historical P/E re-derived from the committed table matches the pre-load proof exactly for MSFT/NVDA/AMZN. All pre-existing production data confirmed unchanged: `financial_metric_results`=900, `quarterly_extraction_runs`=45, `quarterly_metric_results`=1,080, `derived_metric_results`=405, `historical_prices_daily`=14,913, unique REVIEW_REQUIRED=0, Annual Data V1 checksum unchanged. **Valuation V1 is now frozen; reported diluted EPS is the approved V1 per-share valuation input; no future changes without a new version and full validation (D-046).** See `data/valuation_v1_release_manifest.json`, `docs/DECISIONS_LOG.md` D-046, `docs/LAST_CLAUDE_REPORT.md` for full detail.)

**Previous update:** 2026-08-07 (**HISTORICAL PRICES V1 IS FROZEN.** `scripts/158_historical_prices_v1_load.py --execute` succeeded (run exactly once, orchestrated by `scripts/159_historical_prices_v1_release.py`): `historical_prices_daily` created and loaded for all 9 approved tickers, **14,913 validated daily price observations** (1,657 per ticker, 2020-01-02 through 2026-08-06). Independently re-verified read-only. **Historical Prices V1 is now frozen (D-045).** See `docs/DECISIONS_LOG.md` D-045.)

**Project folder:** `C:\AI_Stock_Agent`
**Environment:** Windows, VS Code, Python virtual environment, PowerShell

## Approved target architecture

```text
Ticker + fiscal report date
→ SEC Submissions structure
→ exact 10-K accession lock
→ complete filing document set
→ Arelle loads the XBRL DTS
→ identify primary statements through presentation roles
→ map rows through labels, hierarchy, calculations, contexts, units, and dimensions
→ canonical metrics
→ SEC Company Facts / other sources for QA
→ PASS / REVIEW_REQUIRED / FAIL
```

Do not return to HTML table scraping or tag-name-only mapping as the primary approach.

## Verified environment
- VS Code project at `C:\AI_Stock_Agent`
- `.venv` active and working
- Arelle installed: `arelle-release 2.43.1`
- Import test returned `ARELLE_OK`
- Official Anthropic Claude Code extension installed and authenticated
- Broad auto-approval has not intentionally been enabled

## Verified milestones

### Microsoft prototype
Completed previously:
Revenue, revenue growth, cash, debt, short-term investments, adjusted net debt, operating cash flow, CapEx, FCF, FCF margin, NOPAT, ROIC, price returns, and a backtest dataset.

This remains a QA baseline because it relied substantially on SEC Company Facts.

### Meta
- Correct historical 10-K retrieval after combining `filings.recent` and historical `filings.files`
- Inline XBRL facts extracted
- Technical duplicates safely deduplicated
- Revenue, operating income, and net income for 2022–2024 passed uniqueness checks

### Microsoft Inline XBRL test
- Fact extraction succeeded
- Deduplication succeeded
- Core income metrics passed

### Oracle
- Inline XBRL extraction and deduplication succeeded
- `NetIncomeLoss` versus `ProfitLoss` conflict identified
- A provisional priority selected `NetIncomeLoss`
- Oracle 2024 revenue was missing from the manual approved-tag list, proving tag-name-only mapping is insufficient

## Exact Oracle 2024 filing lock — PASS
Verified:
- ticker: ORCL
- form: 10-K
- report date: 2024-05-31
- accession compact directory: `000095017024075605`
- primary document: `orcl-20240531.htm`

Local directory:

```text
C:\AI_Stock_Agent\data\sec_filings_locked\ORCL\000095017024075605
```

Files include:
- `locked_filing_manifest.json`
- `downloaded_files_manifest.csv`
- `orcl-20240531.htm`

The SEC downloader succeeded with User-Agent:
`Shai Attias shaiattias@gmail.com`

## Arelle tests

### Incorrect filing test
An early test loaded `orcl-20230531.htm` instead of fiscal 2024 and hung. This is why accession locking is mandatory.

### Locked Oracle 2024 offline test
Arelle loaded the correct local primary document and produced a presentation CSV, but the printed Revenue/Sales candidate list was empty.

The exact cause is not proven. Missing external taxonomy resources is only a hypothesis until verified.

### Locked Oracle 2024 online test
An online taxonomy/cache test hung, produced an empty log, and was force-terminated.

### Bounded test — VERIFIED (PASS)
`scripts\37b_run_arelle_bounded_test.py` was never actually created on disk.
The bounded test was implemented as `scripts\37c_run_arelle_bounded_test.py`,
verified against the locked Oracle 2024 accession
(`000095017024075605`, `orcl-20240531.htm`), with all originally intended
features:
- child process (`multiprocessing.Process`)
- per-connection timeout: 20 seconds (`internetTimeout`)
- total runtime limit: 240 seconds, enforced via `process.join(timeout=...)`
- automatic termination (`terminate()` then `kill()` if still alive)
- summary JSON (`data\orcl_2024_arelle_bounded_summary.json`)
- live logging (Arelle log + a separate orchestration log)
- configured cache directory (`data\arelle_cache`)
- identified User-Agent (read from the locked manifest: `Shai Attias shaiattias@gmail.com`)

**Run result:** `PASS`, elapsed 45.22 seconds, `child_exit_code=0`,
`timed_out=false`. No terminate/kill was needed. 1,257 presentation rows
across 87 unique roles; 82 Revenue/Sales candidates.

**Finding:** In the primary income-statement role
(`100030 - Statement - CONSOLIDATED STATEMENTS OF OPERATIONS`), a
"Total revenues" row was identified: concept
`us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`, parent
`us-gaap:RevenuesAbstract`, alongside the revenue line-item breakdown
(Cloud services and license support, Cloud license and on-premise license,
Hardware, Services). This is a standard `us-gaap` concept, not an
Oracle-specific tag, and it was located purely through presentation
structure (role → parent → label), not through a hard-coded concept name.

**Fact vs. hypothesis:** The earlier offline run (script 37) produced an
empty Revenue/Sales candidate list; this online run (with taxonomy loaded)
found the row. This observation is consistent with the earlier hypothesis
that missing external taxonomy resources caused the empty offline result,
but it is not a controlled comparison and does not formally prove that was
the only cause.

**Not yet done (at the time of this run):** only the presentation-level row
(structure/label) was identified. The actual numeric fact value had not
yet been extracted. See "Revenue fact extraction — VERIFIED (PASS)" below.

Output files (under `data\`):
- `orcl_2024_arelle_bounded_presentation.csv`
- `orcl_2024_arelle_bounded_summary.json`
- `orcl_2024_arelle_bounded_child_result.json`
- `orcl_2024_arelle_bounded_child.log`
- `orcl_2024_arelle_bounded_orchestration.log`

### Revenue fact extraction — VERIFIED (PASS)
New script `scripts\37d_extract_oracle_revenue_fact.py` (37b/37c untouched).
Reads the already-verified `orcl_2024_arelle_bounded_presentation.csv` from
37c to select, by structure and label only (Statement-type role containing
"OPERATIONS" + label matching "total revenue"), the single target row —
same concept and role identified above. Then loads the same locked filing
in a bounded child process (same 240s total / 20s per-connection timeout
pattern as 37c) and scans all facts for that exact concept, keeping only
those where: unit = `iso4217:USD`, zero XBRL dimensions (excludes
segment/geography disclosure breakdowns using the same concept), period end
date matches the locked `report_date` exactly (with XBRL's exclusive-end-date
convention accounted for), period length is 350–380 days (annual, without
assuming Oracle's specific fiscal-year start date), and entity CIK matches
the locked manifest.

**Run result:** `PASS`, elapsed 2.67 seconds (Arelle cache was already warm
from the 37c run), `child_exit_code=0`, `timed_out=false`.
- 45 facts total carried the target concept (across years, disclosures, and
  dimensional breakdowns).
- 2 facts passed every filter — both with identical context ID
  (`C_6b80b0ed-ad46-419f-9d80-36739c35988b`), same value: a known Inline
  XBRL technical duplicate (same pattern seen earlier with Meta/Microsoft),
  not a conflict.

**Extracted value:**
- Concept: `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`
- Period: 2023-06-01 to 2024-05-31 (366 days, fiscal year 2024)
- Unit: USD, decimals: -6
- **Value: $52,961,000,000**
- Dimensions: none (consolidated, non-dimensional context)
- No Oracle-specific concept was hard-coded anywhere in the selection or
  extraction logic.

Output files (under `data\`):
- `orcl_2024_revenue_fact_candidates.csv` — all 45 candidate facts with
  every filter column, for audit
- `orcl_2024_revenue_fact_result.json` — full result with lineage
  (ticker, CIK, accession, form, report/filing date, source document,
  source concept, statement role, label, context, period, unit,
  dimensions, value, validation status)
- `orcl_2024_revenue_fact_child.log`
- `orcl_2024_revenue_fact_orchestration.log`

### Generalization test — Microsoft 2024 — VERIFIED (PASS)
Goal: prove the statement-first method is not Oracle-specific, by locking
and running the same approach against a second company (Microsoft, fiscal
year ended 2024-06-30) without hard-coding any company-specific concept.

**Filing lock:** used the existing generic `36b_download_accession_locked_filing.py`
(unmodified, ticker/report-date driven) to lock Microsoft's FY2024 10-K:
CIK 789019, accession `0000950170-24-087843`, `msft-20240630.htm`, filed
2024-07-30. Directory: `data\sec_filings_locked\MSFT\000095017024087843`.

**First generic attempt — `scripts\38_extract_total_revenue_fact.py`
(new, ticker-agnostic, takes `--ticker`/`--report-date`) — result: FAIL.**
Reused Oracle's row-selection rule (label containing "total revenue")
against Microsoft: 0 matches. Root cause (confirmed by inspecting the
presentation CSV): Microsoft's income statement has a single revenue line
labeled simply **"Revenue"** (no "Total" prefix, since there is only one
line item), whereas Oracle sums several revenue line items into a **"Total
revenues"** subtotal. This is a genuine SEC-filer labeling convention
difference, not a bug — it showed the Oracle-tuned label rule did not
generalize. 38 is left unmodified as a historical record of this finding.

**Fixed generic version — `scripts\39_extract_total_revenue_fact.py`
(new file, 38 untouched) — result: PASS for both companies, same
unmodified code.** Broadened the label rule to an anchored pattern
matching "Revenue", "Revenues", "Total revenue", or "Total revenues"
(case-insensitive, non-abstract rows only) — still no company-specific
concept name anywhere. Also fixed status semantics: an ambiguous/no-match
row selection now correctly reports `REVIEW_REQUIRED` (insufficient
evidence) instead of `FAIL` (execution error), consistent with the
project's fail-closed rule.

- **Microsoft run:** `PASS`, 4.08s. Role `100010 - Statement - INCOME
  STATEMENTS`, concept `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`,
  label "Revenue". Period 2023-07-01 to 2024-06-30. **Value:
  $245,122,000,000.** 4 filtered facts, all identical value (technical
  duplicate). This exact value matches, to the dollar, an independently
  computed figure already in `data\msft_revenue_test.csv` (from an earlier
  Company-Facts-based pipeline) for the same accession — used only as a
  QA cross-check, never as a selection input.
- **Oracle regression run (same unmodified script):** `PASS`, 2.75s, same
  result as before ($52,961,000,000, role 100030, label "Total revenues")
  — confirms the broadened rule did not break the original proof.

**Conclusion:** the statement-first method (Statement-type role containing
"income"/"operations" + non-abstract revenue-labeled row + concept-matched
fact filtered by unit/dimensions/period/entity) now works, unmodified,
across two companies with different presentation conventions. This is
still two companies, one metric (revenue), one year each — not yet a
general-purpose universal engine.

Output files (under `data\`), Microsoft run: `msft_20240630_v2_arelle_presentation.csv`,
`msft_20240630_v2_revenue_fact_candidates.csv`, `msft_20240630_v2_revenue_fact_result.json`,
`msft_20240630_v2_arelle_child.log`, `msft_20240630_v2_orchestration.log`.
Oracle regression run: same filenames with `orcl_20240531_v2_` prefix.

### Generalization test — Net Income, Oracle + Microsoft 2024 — VERIFIED (PASS)
Goal: prove the statement-first method also works on a second metric (not
just revenue), on both already-locked companies, without a manual
NetIncomeLoss/ProfitLoss priority list.

**First attempt — `scripts\40_extract_net_income_fact.py` (new,
ticker-agnostic) — result on Oracle: `REVIEW_REQUIRED` (correct fail-closed
behavior, not a bug).** Row selection required a Statement-type role whose
title contains "income" or "operations" (same rule as revenue), then a
non-abstract "Net income"/"Net loss" label. On Oracle this matched **two**
rows: the real income statement (`100030 - CONSOLIDATED STATEMENTS OF
OPERATIONS`) and a separate `100040 - CONSOLIDATED STATEMENTS OF
COMPREHENSIVE INCOME` role, which also starts from a "Net income" line
before adding OCI items. Genuine ambiguity, correctly reported as
`REVIEW_REQUIRED` rather than guessed. 40 is left unmodified as a
historical record. In the same run, the presentation dump also confirmed
(independently of the ambiguity) that Oracle's *equity* statement
(`100050 - STOCKHOLDERS' EQUITY (DEFICIT)`) tags its "Net income" line
with `us-gaap:ProfitLoss` instead of `NetIncomeLoss` — the source of the
NetIncomeLoss-vs-ProfitLoss conflict noted earlier in this document — but
that role is not an income statement and was correctly excluded from
candidates by the role filter.

**Fixed version — `scripts\41_extract_net_income_fact.py` (new file, 40
untouched) — result: PASS for both companies, same unmodified code.**
Narrowed the statement-role rule to exclude any role whose title contains
"comprehensive" (a distinct, universally recognized statement type across
SEC filers, not specific to Oracle or Microsoft). Row selection is
tiered, using position + label + relationships, never a hard-coded
concept preference list, per the user's explicit instruction:
1. Prefer a row labeled "... attributable to [common/stockholders/
   shareholders/corporation/company/Inc./Corp.]" — used when a
   noncontrolling-interest breakout exists on the face of the statement.
2. Otherwise fall back to a single, unqualified "Net income"/"Net loss"/
   "Net income (loss)" row.
3. If neither tier resolves to exactly one row, fail closed with
   `REVIEW_REQUIRED`.
Whichever concept the filer's own presentation attaches to that row is
used as-is — no NetIncomeLoss/ProfitLoss preference is coded anywhere.

- **Oracle:** `PASS`, 2.74s. Role `100030 - Statement - CONSOLIDATED
  STATEMENTS OF OPERATIONS`, concept `us-gaap:NetIncomeLoss` (selected
  purely because that is what the primary statement's presentation
  linkbase uses for the "Net income" row — `ProfitLoss` was never even a
  candidate, since it only appears in the non-income-statement equity
  role), tier `plain_net_income`. Period 2023-06-01 to 2024-05-31.
  **Value: $10,467,000,000.** 4 filtered facts, identical value (technical
  duplicate). No independent FY2024 QA reference exists yet for Oracle net
  income (only FY2022/FY2023 figures exist in this project, computed via
  the earlier manual-priority pipeline, which separately also chose
  `NetIncomeLoss` over `ProfitLoss` for those years — consistent, though
  not the same fiscal year).
- **Microsoft:** `PASS`, 4.00s. Role `100010 - Statement - INCOME
  STATEMENTS`, concept `us-gaap:NetIncomeLoss`, tier `plain_net_income`.
  Period 2023-07-01 to 2024-06-30. **Value: $88,136,000,000.** 4 filtered
  facts, identical value (technical duplicate). No independent FY2024 QA
  reference exists yet for Microsoft net income either (same limitation).

**Conclusion:** the statement-first method, with the same unmodified
script, now correctly extracts two different metrics (revenue and net
income) for two different companies (Oracle and Microsoft), including
correctly navigating a genuine NetIncomeLoss-vs-ProfitLoss structural
ambiguity without any hard-coded per-company or per-concept priority list
— resolved instead by statement role, row label, and position. This is
still two companies, two metrics, one year each — not yet a
general-purpose universal engine, and no independent QA figure exists yet
for either company's FY2024 net income specifically.

Output files (under `data\`): `{ticker}_{reportdate}_net_income_v2_arelle_presentation.csv`,
`_net_income_v2_row_candidates.csv` (all row candidates considered, for
audit), `_net_income_v2_fact_candidates.csv`, `_net_income_v2_fact_result.json`,
`_net_income_v2_arelle_child.log`, `_net_income_v2_orchestration.log`, for
both `orcl_20240531_` and `msft_20240630_` prefixes. The first (REVIEW_REQUIRED)
attempt's files use the same names without the `_v2` segment.

## Current technical task
1. Read the current code and manifests. — DONE
2. Verify whether `37b_run_arelle_bounded_test.py` exists and matches the
   intended design. — DONE: it did not exist; implemented as
   `37c_run_arelle_bounded_test.py` instead (37b untouched).
3. Inspect the locked filing package, entrypoint, extension schema, and
   linkbase references. — DONE via the bounded run.
4. Determine why Oracle 2024 did not expose Revenue/Sales in presentation
   output. — PARTIALLY ADDRESSED: online (taxonomy-loaded) run found the
   Revenue row; the earlier offline empty result remains a supported but
   not formally proven hypothesis (see "Bounded test — VERIFIED" above).
5. Run one bounded, observable proof. — DONE: PASS, 45.22s, no hang.
6. Do not add an Oracle-specific revenue tag. — RESPECTED: identified row
   uses standard `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`.
7. Do not return to HTML table parsing. — RESPECTED.
8. Do not start a full engine until the proof identifies Oracle revenue
   from XBRL statement structure without a company-specific tag rule. —
   MET: structure-level identification (37c) and numeric fact extraction
   with full filter lineage (37d) are both done and verified PASS for one
   company, one year, one metric. A full universal engine (many companies,
   many metrics, canonical schema) has still not been started.

## Success criteria for the next proof
- exact locked Oracle 2024 accession used — MET
- Arelle loads or reports a precise bounded failure — MET (loaded, PASS)
- income-statement presentation role identified — MET
  (`100030 - Statement - CONSOLIDATED STATEMENTS OF OPERATIONS`)
- total revenue row identified through structure, label, or relationships — MET
- no Oracle-specific concept hard-coded — MET
- terminal cannot hang indefinitely — MET (bounded child process)
- results written to inspectable CSV/JSON — MET

## Generic XBRL metric engine — VERIFIED (PASS, 2 companies × 3 metrics)
Goal: stop writing one script per metric per company; consolidate the
statement-first logic already proven in scripts 37c–41 into one shared,
ticker-agnostic, metric-agnostic engine.

**New file — `scripts\42_xbrl_metric_engine.py`** (37c through 41 all left
untouched; this is the only new file for this milestone). Single module,
internally separated into the requested distinct concerns:
1. **Filing lock loading** — `load_locked_filing()`, unchanged pattern
   from 39/41, looks up the already-locked manifest by ticker/report_date.
2. **Arelle session loading** — inside the bounded child process, loads
   the entrypoint once (`internetConnectivity=online`, cache dir,
   per-connection timeout, identified User-Agent).
3. **Statement role identification** — `identify_canonical_row()`
   (first half): top-level Statement-type role matching a per-metric
   `role_include_pattern` (`operations|income`) while excluding
   `role_exclude_pattern` (`comprehensive`).
4. **Canonical row identification** — same function, second half: a
   two-tier label match (`attributable_pattern` then `plain_pattern`),
   generalized from the tiering already proven necessary for net income.
5. **Fact matching** — `match_facts()`: unit=USD, zero dimensions, period
   end matches locked `report_date` (exclusive-end-date corrected), 350–
   380 day annual duration, entity CIK matches manifest.
6. **Deduplication + status** — `deduplicate_and_decide()`: collapses
   identical-value Inline XBRL technical duplicates, returns `PASS` (one
   distinct value), or `REVIEW_REQUIRED` (zero, or more than one distinct
   value) — never guesses.
7. **Bounded orchestration** — `run_engine()` / `engine_child_worker()`:
   same `multiprocessing.Process` + 240s total / 20s per-connection
   timeout + terminate-then-kill pattern as 37c–41, now running all
   requested metrics against one Arelle load instead of one per script.

**Metric definitions are declarative data (`MetricDefinition` + a
`BUILT_IN_METRICS` dict), not ticker branches:** `revenue`, `net_income`,
`operating_income` are each a small set of regex rules over role titles
and row labels — no concept name is hard-coded as "the" tag for any
metric, and no ticker ever appears in the selection logic.

**Acceptance test (Operating Income, per instruction) — Oracle 2024:**
`PASS`, concept `us-gaap:OperatingIncomeLoss`, role `100030 - CONSOLIDATED
STATEMENTS OF OPERATIONS`, label "Operating income", tier `plain`.
**Value: $15,353,000,000.**

**Full run, both companies, all 3 metrics via one engine invocation each
(regression + acceptance combined) — all 6 results `PASS`, all values
identical to the previously, individually verified scripts 39/41:**

| Ticker | Revenue | Net Income | Operating Income |
|---|---:|---:|---:|
| ORCL (FY2024) | $52,961,000,000 | $10,467,000,000 | $15,353,000,000 |
| MSFT (FY2024) | $245,122,000,000 | $88,136,000,000 | $109,433,000,000 |

MSFT revenue again matches the independent `msft_revenue_test.csv` QA
figure exactly. No independent FY2024 QA reference exists yet for net
income or operating income for either company (as previously noted).

**Conclusion:** one script, zero ticker-specific rules, zero hard-coded
concept names, now extracts 3 metrics × 2 companies correctly, including
the two genuine structural edge cases discovered earlier (revenue label
convention differing by filer; net-income vs. comprehensive-income role
ambiguity). This is 2 companies and 3 metrics for one fiscal year each —
still not a full universal engine (no balance sheet or cash flow metrics
yet, no multi-year point-in-time backtesting integration, no canonical
output schema beyond this JSON).

Output files (under `data\`), per `{ticker}_{reportdate}_engine_*`:
`presentation.csv` (full presentation dump, shared across metrics),
`row_candidates.csv` (all row candidates per metric, for audit),
`fact_candidates.csv` (all fact candidates per metric, with every filter
column), `result.json` (per-metric PASS/REVIEW_REQUIRED/FAIL/TIMEOUT with
full lineage), `arelle_child.log`, `orchestration.log`.

## Generalization test — Meta 2024 (3rd company) — VERIFIED (PASS)
Goal: test the generic engine on a third company with a genuinely
different statement layout, per the user's own observation that Oracle
and Microsoft "happen to share very similar statement layouts."

**Filing lock:** used the existing generic `36b_download_accession_locked_filing.py`
(unmodified) to lock Meta's FY2024 10-K: CIK 1326801, accession
`0001326801-25-000017`, `meta-20241231.htm`, filed 2025-01-30. Directory:
`data\sec_filings_locked\META\000132680125000017`.

**First run — `scripts\42_xbrl_metric_engine.py` (unmodified) — result:
revenue `PASS`, net_income `PASS`, operating_income `REVIEW_REQUIRED`.**
Revenue ($164,501,000,000) and Net Income ($62,360,000,000) passed
immediately with zero changes — both use the same "Revenue"/"Net income"
label conventions already handled. Operating Income failed closed with 0
row candidates (not ambiguity — genuinely no match), confirmed by
inspecting the presentation CSV: Meta labels the
`us-gaap:OperatingIncomeLoss` line **"Income (loss) from operations"**,
not "Operating income" as Oracle and Microsoft do. Same concept, third
distinct SEC-filer labeling convention — a real, honest gap, correctly
reported as `REVIEW_REQUIRED` rather than guessed.

**Fix — `scripts\43_xbrl_metric_engine.py` (new file, 42 left
unmodified)** — broadened only the `operating_income` `MetricDefinition`'s
`mention_pattern`/`plain_pattern` to also recognize "[Income/Loss] (loss)
from operations", alongside the existing "Operating income/loss". This is
a general labeling-convention rule (also used by many other SEC filers),
not a Meta-specific concept or ticker branch. Nothing else in the engine
changed.

**Result — all 9 combinations `PASS` (3 companies × 3 metrics), same
run, all values unchanged from prior individually-verified runs:**

| Ticker | Revenue | Net Income | Operating Income |
|---|---:|---:|---:|
| ORCL (FY2024) | $52,961,000,000 | $10,467,000,000 | $15,353,000,000 |
| MSFT (FY2024) | $245,122,000,000 | $88,136,000,000 | $109,433,000,000 |
| META (FY2024) | $164,501,000,000 | $62,360,000,000 | **$69,380,000,000** |

Meta's Operating Income label was "Income (loss) from operations"; Oracle
and Microsoft's role/label structure and values were fully unchanged by
the fix (regression clean). No independent QA reference file exists yet
for any of these Meta figures.

**Conclusion:** the engine now handles three companies, three metrics,
with three distinct statement-role names (`CONSOLIDATED STATEMENTS OF
OPERATIONS`, `INCOME STATEMENTS`, `CONSOLIDATED STATEMENTS OF INCOME`)
and three label conventions for the same concept family, all through one
declarative metric registry with zero ticker-specific code. Still: 3
companies, 3 metrics, one fiscal year each — no balance sheet/cash flow
support, no multi-year coverage, no canonical accumulating dataset yet.

Output files (under `data\`), per `{ticker}_{reportdate}_engine_v2_*`:
`presentation.csv`, `row_candidates.csv`, `fact_candidates.csv`,
`result.json`, `arelle_child.log`, `orchestration.log`, for `meta_20241231_`,
`orcl_20240531_`, and `msft_20240630_` prefixes. Meta's first
(REVIEW_REQUIRED) attempt used the same names without `_v2`
(`meta_20241231_engine_*`).

## Local project permissions
`C:\AI_Stock_Agent\.claude\settings.local.json` was created (project-local
only; the user's global Claude settings were not touched). It allows
running `.venv\Scripts\python.exe` restricted to `*.py` files under
`scripts\`, plus eight read-only PowerShell cmdlets for inspecting
results (`Get-Content`, `Import-Csv`, `ConvertFrom-Json`, `Select-Object`,
`Where-Object`, `Format-List`, `Test-Path`, `Get-ChildItem`); denies
`Remove-Item`, `Format-Volume`, `Stop-Computer`, `Restart-Computer`; and
sets `permissions.defaultMode` to `"auto"` for this project only. No
wildcard allow rule, no package-install/delete/system-change permission.

## Engine extension — Operating Cash Flow, CapEx, Free Cash Flow — VERIFIED (PASS)
Goal: extend the generic engine to the Cash Flow statement family (a
different primary-statement role than income statement metrics) and add
a first derived (computed, not extracted) metric.

**New file — `scripts\44_xbrl_metric_engine.py`** (42/43 left unmodified).
Architecture additions, still fully declarative and ticker-agnostic:
- Two new `BUILT_IN_METRICS` entries, `operating_cash_flow` and `capex`,
  using `role_include_pattern=r"cash\s*flows?"` instead of the income-
  statement family's `operations|income` — the Cash Flow Statement is a
  distinct, universal SEC statement type, selected the same declarative
  way as every other metric.
- A new `DERIVED_METRICS` registry (`8. DERIVED METRICS` section) for
  metrics computed from already-extracted built-in metrics rather than
  searched for as their own presentation row. `free_cash_flow` is the
  first entry: `operating_cash_flow - capex`, computed only after both
  components independently reach `PASS` over the *same* reporting period
  (checked explicitly); otherwise the worse of their statuses
  (`TIMEOUT` > `FAIL` > `REVIEW_REQUIRED`) propagates rather than
  guessing. Every derived-metric result carries `"is_derived_metric":
  true`, the formula, and a `components` block with full lineage
  (concept, context, role, label, period, unit, status) back to each
  source fact — nothing is a bare number with no provenance.
- CapEx's `MetricDefinition` required three unrelated label phrasings
  ORed together — "Capital expenditures" (Oracle), "Additions to
  property and equipment" (Microsoft), "Purchases of property and
  equipment" (Meta) all tag the same
  `us-gaap:PaymentsToAcquirePropertyPlantAndEquipment` concept — plus an
  explicit `exclude_label_pattern` to reject a real same-statement,
  same-role trap: Oracle's "Unpaid capital expenditures" and Meta's
  "Property and equipment in accounts payable..." both report the
  different, non-cash `us-gaap:CapitalExpendituresIncurredButNotYetPaid`
  concept but would otherwise match a naive "capital expenditures"/
  "property and equipment" label search and create a false
  `REVIEW_REQUIRED` two-candidate ambiguity. Excluded by label
  (`unpaid|incurred but not yet paid|accounts payable|accrued`), not by
  ticker.

**QA reference lookup extended:** `find_qa_reference_value()` now also
checks the combined `{ticker}_fcf_test.csv` convention (already present
for Microsoft only, from earlier independent work) in addition to the
existing single-metric `{ticker}_{metric}_test.csv` convention.

**Results — all three companies, all three new metrics `PASS`:**

| Ticker | Operating Cash Flow | CapEx | Free Cash Flow (derived) |
|---|---:|---:|---:|
| ORCL (FY2024) | $18,673,000,000 | $6,866,000,000 | $11,807,000,000 |
| MSFT (FY2024) | $118,548,000,000 | $44,477,000,000 | $74,071,000,000 |
| META (FY2024) | $91,328,000,000 | $37,256,000,000 | $54,072,000,000 |

Microsoft's OCF, CapEx, and FCF all matched `data\msft_fcf_test.csv`
**exactly**, to the dollar — the strongest QA cross-check obtained so far
in this project, on values computed independently by an earlier,
different pipeline. No independent reference exists yet for Oracle or
Meta's cash-flow figures.

**Full regression — all three companies, all six metrics (revenue,
net_income, operating_income, operating_cash_flow, capex,
free_cash_flow) in one engine call each — 18/18 `PASS`, every value
identical to prior individually-verified runs.** No `REVIEW_REQUIRED`,
`FAIL`, or `TIMEOUT` occurred in this milestone; the CapEx exclusion
pattern worked correctly on the first attempt (no iteration needed).

**Conclusion:** the engine now spans two different primary-statement
families (income statement, cash flow statement) and one computed/
derived metric, across three companies, with zero ticker-specific code.
Still open: balance sheet metrics, additional companies/years, and a
canonical accumulating output schema (each run still only writes one
JSON per company per invocation).

Output files (under `data\`), per `{ticker}_{reportdate}_engine_v3_*`:
`presentation.csv`, `row_candidates.csv`, `fact_candidates.csv`,
`result.json`, `arelle_child.log`, `orchestration.log`.

## Engine extension — Balance Sheet metrics: Cash, Short-Term Investments, Current/Long-Term/Total Debt, Adjusted Net Debt — VERIFIED
Goal: extend the generic engine to the Balance Sheet statement family
(instant-context facts, unlike every metric so far) and support a
derived metric that itself depends on another derived metric.

**First attempt — `scripts\45_xbrl_metric_engine.py` (new file, 42-44
unmodified) — result: every balance-sheet metric with a structurally
unambiguous row still came back `REVIEW_REQUIRED`, with `matched_fact_count`
in the dozens but `filtered_fact_count: 0`.** Root cause (confirmed by
inspecting the fact-candidates CSV): an incorrect assumption that XBRL
instant dates are not exclusive. Empirically, Arelle returns a balance
sheet dated 2024-06-30 with `instantDatetime` = 2024-07-01T00:00:00 — the
exact same "midnight of the following day" convention already handled
for duration end dates, just not applied to instants. Every real fact
was off by one day and silently rejected. 45 is left unmodified as a
historical record of this finding.

**Fix — `scripts\46_xbrl_metric_engine.py` (new file, 45 untouched)** —
applies the same `-1 day` adjustment to instant dates as duration end
dates. `match_facts()` now takes an `expected_period_type` argument
(`"instant"` or `"duration"`, read directly from the selected concept's
own declared `periodType` — never hard-coded per metric) and branches
its date/duration checks accordingly.

**New `BUILT_IN_METRICS` (Balance Sheet role family —
`role_include_pattern=r"balance\s+sheets?|financial\s+position"`,
excluding "Parenthetical" sub-statements):**
- `cash_and_equivalents`, `short_term_investments`: same anchored-label
  approach as prior metrics — e.g. short-term investments needed to OR
  "Marketable securities" (Oracle, Meta — two *different* concepts,
  same label) with "Short-term investments" (Microsoft), while its
  anchored pattern naturally excludes Microsoft's rollup row "Total
  cash, cash equivalents, and short-term investments".
- `current_debt`, `long_term_debt`: each excludes the other's row from
  its own candidate set via `exclude_label_pattern` (e.g. Microsoft's
  "Current portion of long-term debt" literally contains the substring
  "long-term debt" and would otherwise falsely match `long_term_debt`'s
  search too).

**New `DERIVED_METRICS`, generalized beyond the old fixed 2-component
subtract-only design:** `DerivedMetricDefinition` now carries an
arbitrary `combine` function over N ordered components, and a new
`resolve_metric_dependencies()` topologically orders derived metrics so
each one's components (built-in or themselves derived) are ready first.
- `total_debt = current_debt + long_term_debt`
- `adjusted_net_debt = total_debt - cash_and_equivalents - short_term_investments`
  (depends on `total_debt`, itself derived — resolved automatically,
  `total_debt` gets computed first).

**Results:**

| Ticker | Cash | Short-Term Investments | Current Debt | Long-Term Debt | Total Debt | Adjusted Net Debt |
|---|---:|---:|---:|---:|---:|---:|
| ORCL | $10,454,000,000 | $207,000,000 | $10,605,000,000 | $76,264,000,000 | **$86,869,000,000** | **$76,208,000,000** |
| MSFT | $18,315,000,000 | $57,228,000,000 | `REVIEW_REQUIRED` | $42,688,000,000 | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` |
| META | $43,889,000,000 | $33,926,000,000 | `REVIEW_REQUIRED` | $28,826,000,000 | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` |

**Current Debt REVIEW_REQUIRED — genuine, expected, per explicit
instruction, not a bug:**
- **Microsoft:** two separate current-liability line items with no
  single "total current debt" row — "Short-term debt" (concept
  `CommercialPaper`) and "Current portion of long-term debt" (concept
  `LongTermDebtCurrent`). Notably, an earlier independent pipeline in
  this project (`data\msft_adjusted_net_debt_test.csv`) made its own
  judgment call here — it used only `LongTermDebtCurrent` and excluded
  commercial paper — confirming this is a real, disputed accounting
  judgment call across pipelines, not something this engine should
  silently pick a side on.
- **Meta:** zero current-debt line items on the face of the balance
  sheet at all (its 2024 bond issuance shows up only as `long_term_debt`,
  $28,826,000,000, no current portion broken out). Absence of a row is
  treated as "cannot confirm", not "assume zero" — consistent with every
  other REVIEW_REQUIRED in this engine.
- `total_debt` and `adjusted_net_debt` correctly propagate
  `REVIEW_REQUIRED` for both companies, since a required component did
  not PASS — computed only for Oracle, where all components resolved
  unambiguously.

**Full regression — all three companies, all twelve metrics (the prior
six plus these six) in one engine call each: Oracle 12/12 PASS; Microsoft
and Meta 9/12 PASS with the 3 expected `REVIEW_REQUIRED` above — the
prior six metrics were completely unaffected in all three companies.**

**Conclusion:** the engine now spans three primary-statement families
(income statement, cash flow statement, balance sheet), instant and
duration contexts, and a two-level derived-metric dependency chain —
across three companies, still with zero ticker-specific code. The
Current Debt findings are a genuine, useful discovery about real
cross-company heterogeneity in how "current debt" is reported, not a
limitation to "fix" by guessing.

Output files (under `data\`): 45's REVIEW_REQUIRED run used
`{ticker}_{reportdate}_engine_v4_*`; 46's fixed run uses
`{ticker}_{reportdate}_engine_v4b_*` (`presentation.csv`,
`row_candidates.csv`, `fact_candidates.csv`, `result.json`,
`arelle_child.log`, `orchestration.log`).

## Engine extension — NOPAT / ROIC (accounting policy D-015) — VERIFIED
Goal: implement the user's explicit, binding accounting policy (see
`docs/DECISIONS_LOG.md` D-015) for Total Debt, Effective Tax Rate,
NOPAT, Invested Capital (averaged), and ROIC.

**New files, in order (each preserved unmodified, per D-011):**
- `scripts\47_xbrl_metric_engine.py` — first attempt, crashed before any
  output: `resolve_metric_dependencies()` checked the generic `"_prior"`
  suffix-stripping fallback *before* checking whether a name (e.g.
  `"invested_capital_prior"`, a literal `DERIVED_METRICS` key whose own
  components already reference `"_prior"`-suffixed raw items) was a
  direct registry hit. A real code bug, not a data ambiguity.
- `scripts\48_xbrl_metric_engine.py` — fixed the ordering bug; first
  successful run (Oracle, `roic`: `PASS`) surfaced a lineage-labeling
  bug: the generic derived-metric combiner tagged every result's `unit`
  from its first component, so `ROIC` (a dimensionless ratio) was
  mislabeled `"iso4217:USD"`. The computed *value* was already correct;
  only the unit label was wrong.
- `scripts\49_xbrl_metric_engine.py` — added an optional `result_unit`
  override on `DerivedMetricDefinition`, set to `"ratio"` for `roic`
  (matching how `effective_tax_rate`, a custom metric, already labeled
  itself). This is the verified, working version.

**Architecture additions (v5), both required by the policy, neither
ticker-specific:**
- **Prior-fiscal-year-end extraction.** Row identification (which
  concept represents a metric) is period-independent — the same concept
  appears in both the current and comparative columns of the same
  statement — so it is resolved once per metric and then fact-matched
  twice: once at `report_date`, once at a prior date computed generically
  as `report_date` minus exactly one year (`compute_prior_report_date`,
  with a Feb-29 fallback), never a company-specific fiscal calendar
  assumption.
- **Custom derived metrics.** Alongside the existing generic
  N-component-combine `DERIVED_METRICS`, a small `CUSTOM_METRIC_RAW_REQUIREMENTS`
  registry plus dedicated functions (`compute_total_debt`,
  `compute_effective_tax_rate`) implement accounting-policy logic a
  plain combine cannot express: Total Debt's "prefer explicit row, else
  sum non-overlapping components" rule, and Effective Tax Rate's
  [0, 1]-range / positive-pretax-income validation.
- Four new `BUILT_IN_METRICS`: `total_debt_explicit` (structural search
  for a single "Total debt" row — absent in all three companies' 10-Ks,
  so every run falls through to the current+long-term-debt sum, exactly
  as the policy specifies), `stockholders_equity` (anchored to "Total
  stockholders'/shareholders' equity" — the entity-wide total; for
  Oracle this naturally excludes the parent-only "Total Oracle
  Corporation stockholders' equity" row, since "Oracle Corporation"
  breaks the anchor, without needing NCI-attribution tiering logic like
  net_income's), `pretax_income`, `income_tax_expense`.

**Results — all three companies:**

| Ticker | Effective Tax Rate | NOPAT | Invested Capital (current) | Invested Capital (prior) | Average Invested Capital | ROIC |
|---|---:|---:|---:|---:|---:|---:|
| ORCL | 10.85% | $13,687,066,774.55 | $85,447,000,000 | $81,850,000,000 | $83,648,500,000 | **16.36%** |
| MSFT | 18.23% | $89,481,912,364.20 | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` |
| META | 11.75% | $61,227,754,270.27 | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` |

Effective Tax Rate, NOPAT, Pretax Income, Income Tax Expense, and
Stockholders' Equity all resolved cleanly (`PASS`) for all three
companies — Microsoft's and Meta's `REVIEW_REQUIRED` chain (Total Debt →
Invested Capital → Average Invested Capital → ROIC) is caused entirely
by the same `current_debt` ambiguity/absence already documented in the
Balance Sheet milestone above, correctly propagated per policy — not a
new gap.

**Full regression — twenty metrics per company (the 12 prior plus
Pretax Income, Income Tax Expense, Stockholders' Equity, Effective Tax
Rate, NOPAT, Invested Capital, Average Invested Capital, ROIC) in one
engine call each: Oracle 20/20 `PASS`; Microsoft and Meta 16/20 `PASS`
with the 4 expected `REVIEW_REQUIRED` above. Every previously-verified
value was unchanged.**

**Conclusion:** the engine now spans three primary-statement families, a
two-level averaging (current + prior fiscal year-end), custom
accounting-policy logic distinct from plain combines, and a five-level
derived-metric dependency chain (Total Debt → Invested Capital →
Average Invested Capital → ROIC, plus Effective Tax Rate → NOPAT →
ROIC) — still with zero ticker-specific code and full lineage back to
every source fact for every computed value.

Output files (under `data\`), verified run: `{ticker}_{reportdate}_engine_v5c_*`
(`presentation.csv`, `row_candidates.csv`, `fact_candidates.csv`,
`result.json`, `arelle_child.log`, `orchestration.log`). 47 produced no
output (crashed before extraction); 48's output uses `_engine_v5b_`.

## Engine extension — current_debt as sum of components (accounting policy D-016) — VERIFIED
Goal: implement the user's explicit, binding refinement (D-016) allowing
`current_debt` to be a sum of multiple explicit interest-bearing
components, in tiered order, instead of only ever a single row —
specifically to resolve Microsoft's (and structurally similar filers')
current-debt `REVIEW_REQUIRED`.

**New files, in order (each preserved unmodified, per D-011):**
- `scripts\50_xbrl_metric_engine.py` — implements the 3-tier policy
  (explicit total row → Calculation-linkbase-verified components →
  presentation siblings sharing one parent, summed). Structurally this
  worked immediately and correctly: Microsoft's `current_debt` was
  identified as `"Short-term debt" (CommercialPaper) + "Current portion
  of long-term debt" (LongTermDebtCurrent)`, both siblings under the same
  presentation parent — but the run still came back `REVIEW_REQUIRED`
  because one of the two components individually failed fact-matching,
  surfacing two separate, pre-existing, genuinely ticker-agnostic bugs
  in the fact-matching/dedup layer (not caused by this policy, only
  exposed by it):
  1. **Decimals-precision false conflict.** The same Commercial Paper
     balance was tagged twice in the same context — once precisely in
     the statement table (`decimals=-6`, $6,693,000,000) and once
     rounded for narrative prose (`decimals=-8`, $6,700,000,000, which
     is exactly what $6,693,000,000 rounds to at that precision). The
     old `deduplicate_and_decide()` only collapsed *identical* values,
     so this was flagged as a false 2-value conflict.
  2. **Unparseable-value fact silently admitted.** Microsoft's prior
     fiscal year (2023-06-30) Commercial Paper had a second same-context
     fact where Arelle's Inline XBRL value transform failed
     (`fact.value` = `"(ixTransformValueError)"`), yet `match_facts()`'s
     `all_filters_ok` never checked whether the value actually parsed —
     the broken fact was still admitted as a "candidate" with a
     `None`/NaN value, polluting the ambiguity check.
- `scripts\51_xbrl_metric_engine.py` — fixes bug 1: reconciles
  same-context facts whose values are consistent under standard XBRL
  rounding (using each fact's own `decimals` attribute) before deciding
  ambiguity; a genuine conflict still fails closed exactly as before.
  Fixed Microsoft's *current-year* current_debt; the *prior-year* one
  (bug 2) still failed.
- `scripts\52_xbrl_metric_engine.py` — fixes bug 2: adds
  `value_ok = value_numeric is not None` to `match_facts()`'s
  `all_filters_ok`. **This is the verified, working version.**

**Results — all three companies:**

| Ticker | current_debt | Total Debt | Invested Capital (avg) | ROIC |
|---|---:|---:|---:|---:|
| ORCL | $10,605,000,000 (single row, unchanged) | $86,869,000,000 | $83,648,500,000 | 16.36% (unchanged) |
| MSFT | **$8,942,000,000** (= $6,693,000,000 CommercialPaper + $2,249,000,000 LongTermDebtCurrent) | **$51,630,000,000** | **$193,381,000,000** | **46.27%** |
| META | `REVIEW_REQUIRED` (0 components — see below) | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` |

**Meta's current_debt REVIEW_REQUIRED persists — correctly, not a gap to
close by more code.** Meta's balance sheet has zero current-debt line
items at all (its 2024 bond issuance is entirely `long_term_debt`,
$28,826,000,000, with no current portion broken out on the face of the
statement). Tier 3 correctly reports "no components found" and does not
infer `current_debt = 0`, per the explicit policy. This is a genuine,
company-specific disclosure fact (not something the engine can resolve
without either the user accepting an inferred zero or a human checking
Meta's debt-maturity footnote) — flagged here as the one remaining
material REVIEW_REQUIRED, per the instruction to stop and report it
rather than work around it.

**Full regression — twenty metrics per company: Oracle 20/20 `PASS`
(current_debt still a single-row PASS — untouched by the sibling-sum
path); Microsoft 20/20 `PASS` (up from 16/20); Meta 16/20 `PASS`, same 4
`REVIEW_REQUIRED` as before (now for a materially different, better-
understood reason: zero components, not blocked by the earlier
Total-Debt-only formulation). Every previously-verified value that
didn't depend on current_debt was unchanged.**

**Conclusion:** the sibling-sum structural logic (D-016) worked
correctly on the first attempt; the two bugs it surfaced were general
fact-matching/deduplication correctness issues that now benefit every
metric in the engine, not specific to current_debt or to Microsoft.

Output files (under `data\`), verified run: `{ticker}_{reportdate}_engine_v6c_*`.
50's run used `_engine_v6_`, 51's used `_engine_v6b_`.

## Engine extension — current_debt = 0 inference, 4-condition proof (accounting policy D-017) — VERIFIED
Goal: implement the user's explicit, binding refinement (D-017) allowing
`current_debt` to be inferred as exactly `0`, but only when four
structural conditions are all proven from the filing's own disclosures —
never merely from the absence of a row (D-016's tier-3 "zero components
found" outcome, e.g. Meta, is a *precondition* for attempting this, not
itself sufficient).

**New files, in order (each preserved unmodified, per D-011):**
- `scripts\53_xbrl_metric_engine.py` — first implementation. Changed
  `resolve_current_debt_components` to return a
  `("zero_inference_needed", [])` signal (instead of raising) when D-016's
  tiers 1-3 find zero components, and added a new "3c + 4c. CURRENT DEBT
  = 0 INFERENCE" section implementing conditions 2-4 in order (condition
  1 is D-016's existing zero-components finding, reused as-is):
  - **Condition 2** (`verify_condition_2_no_near_term_maturity`): locates
    the filer's own Debt Maturity Schedule disclosure role
    (`find_debt_maturity_schedule_role`, a Disclosure-type role whose
    title matches both a debt pattern and a maturity pattern), takes its
    chronologically-first non-"Total" row (presentation order), and
    confirms that value is exactly `0`.
  - **Condition 3** (`verify_condition_3_total_matches_long_term_debt`):
    the same disclosure's own "Total" row must reconcile (tolerance ≤$1)
    with `long_term_debt` from the balance sheet.
  - **Condition 4** (`find_condition_4_contradictions`): scans every
    debt-related disclosure role for any row whose label matches current-
    portion/short-term-debt/commercial-paper vocabulary with a nonzero
    `PASS` value; any hit blocks the inference.
  - Any unproven condition → `REVIEW_REQUIRED` naming the specific
    condition, never a guess. Every successful inference is recorded with
    `selection_tier: "zero_inference_proven"` and a full `components_detail`
    evidence trail (the condition-2 and condition-3 facts used), per the
    project's lineage requirement.
  - **Meta 2024 run result: `REVIEW_REQUIRED` at condition 2** —
    `find_debt_maturity_schedule_role` matched **two** roles instead of
    one: Meta's actual borrowings disclosure (`"9955554 - Disclosure -
    Long-term Debt - Schedule of Maturities of Long-Term Debt
    (Details)"`) and a false positive, Meta's own investment portfolio
    (`"9955538 - Disclosure - Financial Instruments - Contractual
    Maturities of Marketable Debt Securities (Details)"`) — an asset-side
    note (Meta's holdings of *others'* debt securities) that also
    contains the words "debt" and "maturities". A real, generic gap
    (any filer disclosing both its own debt and its investment
    portfolio's maturities could hit this), not a Meta-specific bug. 53
    is left unmodified as a historical record of this finding.
- `scripts\54_xbrl_metric_engine.py` — fixes the role false-positive:
  adds `DEBT_MATURITY_ROLE_EXCLUDE_PATTERN =
  r"marketable|available.for.sale|investment"`, applied as `& ~is_excluded_role`
  in `find_debt_maturity_schedule_role`'s candidate filter — a general
  "this is an investment-asset note, not a borrowings note" exclusion,
  not a ticker rule. **This is the verified, working version.**

**Meta 2024 re-run result (script 54): role lookup now correctly finds
exactly one role, but `current_debt` remains `REVIEW_REQUIRED` for a
different, genuine reason — the fix corrected the lookup bug, it did not
force a zero result:**
- **Current year (2024-12-31):** condition 2 passes (earliest bucket
  value is $0), but **condition 3 fails**: the maturity schedule's own
  "Total" row is $29,000,000,000, which does not reconcile with
  `long_term_debt` from the balance sheet ($28,826,000,000) — a genuine
  $174,000,000 discrepancy (consistent with unamortized discount/issuance
  costs or similar balance-sheet-vs-footnote presentation differences),
  correctly blocking the inference rather than guessing which figure is
  "right".
- **Prior year (2023-12-31):** condition 2 itself fails — no single
  reliable value could be extracted for the earliest maturity bucket
  (Meta's schedule merges the first two years into one custom concept,
  `meta:LongTermDebtMaturityYearOneToYearTwo`, labeled "2025 through
  2026", which does not cleanly isolate a "due within 12 months" figure).
- Both outcomes were observed from the actual re-run, not assumed in
  advance — this is D-017 working as designed: it proves zero only when
  the evidence genuinely supports it, and fails closed otherwise.

**Full regression — twenty metrics per company, same script (54):**
Oracle 20/20 `PASS` (current_debt still the single-row PASS from D-016,
untouched by this policy); Microsoft 20/20 `PASS` (current_debt still
the sibling-sum PASS from D-016, untouched); Meta 16/20 `PASS`, same
count as before D-017 (current_debt and its dependents — total_debt,
adjusted_net_debt, invested_capital, average_invested_capital, roic —
remain `REVIEW_REQUIRED`, now for the specific, evidenced reasons above
instead of a generic "zero components found"). No previously-verified
value in any company changed.

**Conclusion:** D-017's 4-condition proof chain is implemented generically
(no ticker or company name in the logic) and correctly distinguishes
"the disclosures prove zero" from "no row exists" — for Meta specifically,
the disclosures do not currently prove zero (a real footnote
reconciliation gap and a merged maturity bucket), so `current_debt`
correctly remains `REVIEW_REQUIRED` rather than being forced to `0`.

Output files (under `data\`): 53's run used `{ticker}_{reportdate}_engine_v7_*`;
54's verified run uses `{ticker}_{reportdate}_engine_v8_*` (`presentation.csv`,
`row_candidates.csv`, `fact_candidates.csv`, `result.json`,
`arelle_child.log`, `orchestration.log`).

## Engine extension — Total Debt "Aggregate-First" (accounting policy D-018) — VERIFIED
Goal: implement the user's explicit, binding refinement (D-018) allowing
`total_debt` to be taken directly from a reliable, filing-reported
AGGREGATE "Total debt" figure — without first requiring `current_debt`
and `long_term_debt` to individually resolve — while explicitly
recording that the current-vs-long-term allocation inside that aggregate
is unverified, and letting a validated direct aggregate support
`adjusted_net_debt`, `invested_capital`, `average_invested_capital`, and
`roic`. Meta's `current_debt` (and thus `total_debt`) staying
`REVIEW_REQUIRED` under D-016/D-017 was the direct motivation.

**New file — `scripts\55_xbrl_metric_engine.py`** (42-54 unmodified).
Two changes, both fully generic (no ticker or company name in the logic):
- **Broadened `total_debt_explicit` search.** Previously restricted to
  Balance Sheet roles; now also searches any Disclosure role (many
  filers report a "Total debt" summary line inside a debt footnote, not
  on the balance sheet face), while a new `role_exclude_pattern`
  rejects any role whose own title scopes it to a single maturity class
  ("...Long-term Debt...", "...Current Debt...", "...Commercial
  Paper...") or to an investment-asset disclosure ("Marketable...",
  "Available-for-sale...", "Investment..."). This exclusion is
  load-bearing, not defensive-only — see the Microsoft finding below.
  The pre-existing anchored label pattern (exact "Total debt", nothing
  else) was already correctly narrow and needed no change.
- **New status `PASS_DIRECT_AGGREGATE`** (`compute_total_debt`,
  rewritten): when the broadened search finds exactly one valid direct
  aggregate row, `total_debt` uses it as-is with this status, plus a new
  `current_long_term_allocation` field stating explicitly that the
  split is unverified, and a non-blocking `component_sum_cross_check`
  (when `current_debt`/`long_term_debt` also happen to resolve
  independently, for QA visibility only — never blocking). A new module
  constant, `SUCCESSFUL_METRIC_STATUSES = {"PASS",
  "PASS_DIRECT_AGGREGATE"}`, is used everywhere a derived metric's
  components are checked for success (`compute_derived_metric`'s status
  aggregation, and the top-level `all_pass` flag) — so
  `adjusted_net_debt`, `invested_capital`, `average_invested_capital`,
  and `roic` all correctly accept a `PASS_DIRECT_AGGREGATE` `total_debt`
  as a valid input. Each such derived metric's own result status is
  still plain `PASS` (its formula was fully and correctly applied); the
  unverified-allocation caveat remains visible through
  `components.total_debt.status` in its lineage, never silently
  dropped. If no direct aggregate is found, `total_debt` falls back
  unchanged to the existing D-016/D-017 sum-or-proven-zero logic.

**A real trap found and correctly avoided while implementing this on
Microsoft — proof the role-exclusion is necessary, not decorative:**
Microsoft's 10-K contains a row literally labeled **"Total debt"**
(concept `us-gaap:LongTermDebt`) inside a role titled `"Components of
Long-term Debt (Detail)"`. Taking this at face value would have
silently *understated* Microsoft's total debt — that row sums only the
long-term instruments listed in that specific table, excluding
Microsoft's separate Commercial Paper balance ($6,693,000,000). The new
`role_exclude_pattern` (`"long-?term\s+debt"` matching the role's own
title) correctly rejects it, so `total_debt_explicit` still returns "0
candidates" for Microsoft and `total_debt` still resolves via the
existing D-016 sibling-sum path — value unchanged.

**Results — all three companies, twenty metrics each, one run per
company:**

| Ticker | total_debt status | total_debt value | ROIC status | ROIC |
|---|---|---:|---|---:|
| ORCL | `PASS` (sum path, unchanged) | $86,869,000,000 | `PASS` | 16.36% |
| MSFT | `PASS` (sum path, unchanged) | $51,630,000,000 | `PASS` | 46.27% |
| META | `REVIEW_REQUIRED` | — | `REVIEW_REQUIRED` | — |

**Meta: no `PASS_DIRECT_AGGREGATE` was achieved — this was verified by
actually running the broadened search, not assumed.** Grepping Meta's
own presentation data confirms the only "total"+"debt" rows anywhere in
its 10-K are: its investment portfolio's maturity note (already
excluded), the debt-maturity schedule's "Total" row and a "Total face
amount of long-term debt" row (both inside roles titled "Long-term
Debt...", correctly excluded, and both already known from D-017 not to
reconcile with the balance-sheet net carrying value). No row anywhere is
a bare, unqualified, company-wide "Total debt". `total_debt_explicit`
therefore still finds zero candidates, `current_debt` remains
`REVIEW_REQUIRED` (D-016/D-017, unchanged), and the sum-path fallback is
unavailable — so `total_debt` and every dependent metric
(`adjusted_net_debt`, `invested_capital`, `average_invested_capital`,
`roic`) correctly remain `REVIEW_REQUIRED`, exactly per D-018's own
"no reliable direct aggregate → stay REVIEW_REQUIRED" rule. This is a
genuine finding about Meta's disclosures, not a limitation of this
implementation.

**Full regression — twenty metrics per company: Oracle `all_pass: true`,
Microsoft `all_pass: true`, both with every value identical to the prior
(v8, D-017) verified run. Meta: same 4 `REVIEW_REQUIRED` chain as
before D-018 (current_debt → total_debt → adjusted_net_debt/
invested_capital → average_invested_capital → roic), for the same
D-016/D-017 reasons — D-018 simply found no direct aggregate to invoke
for Meta specifically.**

**Conclusion:** the Aggregate-First policy is implemented and verified
working end-to-end (broadened search, exclusion logic, new status,
propagation through the derived-metric dependency chain) — it has not
yet been positively exercised (no company in this 3-company set actually
has a valid, reliable direct aggregate "Total debt" row), but its
guardrail (the Microsoft trap) was proven necessary and effective on the
first real run, and Meta's continued `REVIEW_REQUIRED` was confirmed by
an actual search, not an assumption.

Output files (under `data\`), verified run: `{ticker}_{reportdate}_engine_v9_*`
(`presentation.csv`, `row_candidates.csv`, `fact_candidates.csv`,
`result.json`, `arelle_child.log`, `orchestration.log`).

## Next open step (not started)
The generic engine (`55_xbrl_metric_engine.py`) now covers twenty
metrics across three statement families, current_debt as a policy-driven
sum-or-single-row-or-proven-zero (D-016 + D-017), total_debt with an
Aggregate-First direct-row preference (D-018), NOPAT/ROIC (D-015), and
two general fact-matching correctness fixes — Oracle and Microsoft are
20/20 `PASS`; Meta has one remaining material `REVIEW_REQUIRED` chain
(current_debt and its dependents), confirmed under three successive,
increasingly permissive policies (D-016, D-017, D-018) that no available
evidence in its 10-K currently supports resolving it automatically.
Open options for the next step (none started, no decision made yet):
- Decide whether Meta's specific footnote-reconciliation gap (D-017) and
  the absence of any direct aggregate row (D-018) warrant yet another
  accounting-policy refinement, or whether `REVIEW_REQUIRED` should
  stand as the final, human-reviewed answer for Meta going forward. This
  is an accounting-judgment decision, not a code fix.
- Extend to more companies and/or additional fiscal years, toward the
  15-company backtest universe in `docs/PROJECT_CONTEXT.md` — still the
  most realistic test of how well the label/role patterns generalize,
  and the most likely way to actually exercise `PASS_DIRECT_AGGREGATE`
  on a real filing.
- Design the canonical output schema / storage layer (per "Required
  lineage" in `CLAUDE.md`) that will sit on top of this engine's PASS
  results — increasingly pressing now that 20 metrics × 3 companies
  exist as separate per-run files rather than an accumulating dataset.
This decision should be made explicitly with the user before proceeding,
consistent with "one topic at a time."

## Generalization test — NVIDIA (4th company, FY2024) — VERIFIED (PASS, 20/20)
Goal: prove the engine generalizes to a 4th company without weakening
any validation rule or adding ticker-specific logic merely to force a
PASS, per the user's explicit instruction.

**Filing lock:** `scripts\36b_download_accession_locked_filing.py`
(unmodified, ticker/report-date driven) against SEC submissions data —
CIK 1045810, form 10-K, report date **2024-01-28**, accession
`0001045810-24-000029`, primary document `nvda-20240128.htm`, filed
2024-02-21. Directory:
`data\sec_filings_locked\NVDA\000104581024000029`.

**First run — `scripts\55_xbrl_metric_engine.py` (unmodified, per
instruction to test the latest engine as-is first) — result: 14/20
`PASS`, 6 `REVIEW_REQUIRED`.** Every genuine gap was traced to real,
inspectable data (presentation/fact-candidate CSVs), never guessed:
- **`capex`** — 0 row candidates. NVIDIA's cash-flow label is "Purchases
  **related to** property and equipment **and intangible assets**" — a
  fourth real SEC-filer phrasing for
  `us-gaap:PaymentsToAcquireProductiveAssets`, alongside the three
  already handled (Oracle/Microsoft/Meta).
- **`pretax_income`** — exactly 1 mention-level candidate, matching
  neither tier. NVIDIA's label is "Income before income tax" (singular
  "tax"); the engine only recognized the plural "...income taxes".
- **`income_tax_expense`** — same pattern. NVIDIA's label is "Income tax
  expense **(benefit)**" — a trailing parenthetical (marking the line
  can swing either direction) the anchored pattern didn't allow.
- **All six `_prior` built-in metrics** — facts existed for the correct
  concept but none passed every filter. Root cause (confirmed via the
  fact-candidates CSV): NVIDIA's actual FY2023 comparative instant date
  is **2023-01-29**, one day off from `compute_prior_report_date`'s
  naive "-1 year, same month/day" calculation (2023-01-28). NVIDIA uses
  a 52/53-week fiscal calendar ("last Sunday of January"), unlike
  Oracle/Microsoft/Meta's fixed calendar-date fiscal year ends — a
  genuine, common SEC-filer convention this project's 3-company test set
  had never exercised before.
- `effective_tax_rate`, `total_debt_prior`, `free_cash_flow`,
  `invested_capital_prior`, `nopat`, `average_invested_capital`, `roic`
  all cascaded from the above — not independent gaps.
- `total_debt` (current year) already resolved `PASS` via the existing
  D-016 sum path (`current_debt` $1,250,000,000 + `long_term_debt`
  $8,459,000,000 = $9,709,000,000); `total_debt_explicit` was already
  correctly `REVIEW_REQUIRED` (no direct aggregate row — confirmed
  below).

**Fix — `scripts\56_xbrl_metric_engine.py` (new file; 55 preserved
unmodified — an in-place edit was made to 55 by mistake mid-session and
fully reverted before this was noticed).** All three fixes are pure
label/rule broadenings, the same pattern used for every prior
generalization gap in this project (e.g. Meta's "Income (loss) from
operations" in script 43) — no ticker name, no NVDA-only branch, and
every previously-matching label in Oracle/Microsoft/Meta still matches
unchanged:
1. `capex`: added an optional `related` infix and an optional trailing
   `and intangible assets` to the existing pattern.
2. `pretax_income`: `income\s+taxes` → `income\s+tax(?:es)?`.
3. `income_tax_expense`: added an optional trailing
   `(expense)`/`(benefit)`/`(provision)` parenthetical.
4. **Prior-fiscal-year-end date tolerance** (the substantive
   architectural fix): `match_facts()` gained a
   `period_end_tolerance_days` parameter (default `0`, i.e. unchanged
   exact-match behavior). A new constant,
   `PRIOR_PERIOD_DATE_TOLERANCE_DAYS = 10`, is applied **only** to the
   prior-fiscal-year-end search (`fact_match_requests` now carries a
   per-request tolerance); the current, accession-locked `report_date`
   still requires an exact match, unchanged. Widening the match window
   does not weaken ambiguity detection — `deduplicate_and_decide()`
   still fails closed to `REVIEW_REQUIRED` if more than one distinct
   value falls inside the window; it only lets a single genuine match
   land within ±10 days of the naive one-year-earlier guess instead of
   requiring exact equality.

**Results — NVIDIA FY2024, full 20-metric run, one engine call:**

| Metric | Status | Value |
|---|---|---:|
| revenue | PASS | $60,922,000,000 |
| operating_income | PASS | $32,972,000,000 |
| net_income | PASS | $29,760,000,000 |
| operating_cash_flow | PASS | $28,090,000,000 |
| capex | PASS | $1,069,000,000 |
| free_cash_flow | PASS | $27,021,000,000 |
| cash_and_equivalents | PASS | $7,280,000,000 |
| short_term_investments | PASS | $18,704,000,000 |
| current_debt | PASS | $1,250,000,000 |
| long_term_debt | PASS | $8,459,000,000 |
| total_debt | PASS (sum path) | $9,709,000,000 |
| total_debt_explicit | REVIEW_REQUIRED | — (no direct aggregate row exists) |
| adjusted_net_debt | PASS | -$16,275,000,000 |
| pretax_income | PASS | $33,818,000,000 |
| income_tax_expense | PASS | $4,058,000,000 |
| effective_tax_rate | PASS | 12.00% |
| stockholders_equity | PASS | $42,978,000,000 |
| nopat | PASS | $29,015,515,997.40 |
| invested_capital | PASS | $26,703,000,000 |
| average_invested_capital | PASS | $23,230,500,000 |
| roic | PASS | 124.90% |

`all_pass: true` — 20/20 (the 21st requested item, `total_debt_explicit`,
is a diagnostic sub-metric, not part of the "20 supported metrics" list;
its `REVIEW_REQUIRED` is the expected, correct outcome, not a gap). ROIC
of ~125% is a direct, unforced consequence of NVIDIA's FY2024 results
(NOPAT ≈$29.0B against average invested capital ≈$23.2B) — extreme but
not implausible for a company mid-way through an unprecedented demand
spike, and not adjusted or sanity-capped by this engine.

**`PASS_DIRECT_AGGREGATE` (D-018) was NOT exercised for NVIDIA — verified
by an actual grep of NVIDIA's own presentation data, not assumed.** No
row anywhere in NVIDIA's 10-K is labeled a bare, unqualified "Total
debt" — the only "total"+"debt" matches are two unrelated investment-
portfolio "Total" rows (unrealized-loss aggregates on NVIDIA's own
marketable debt securities holdings, not its borrowings). `total_debt`
therefore resolved via the pre-existing D-016 sum path, exactly as for
Oracle and Microsoft.

**Full regression — Oracle, Microsoft, Meta, same 20-metric set, script
56 vs. the prior verified script 55/v9 run: zero differences in any
status or value for any of the three companies** (Oracle `all_pass:
true`, Microsoft `all_pass: true`, Meta `all_pass: false` with the same
4-metric `REVIEW_REQUIRED` chain as before — all unchanged).

**Conclusion:** the engine now generalizes to 4 companies, and every fix
made to reach this point was a genuine broadening of an existing
structural/label rule to a new, real SEC-filer convention — never a
ticker-specific branch, an assumed zero, or a weakened threshold. The
52/53-week fiscal-calendar finding is the most consequential: it is a
common convention (used by many technology and retail filers, not just
NVIDIA), and this project's original 3-company set (all fixed calendar-
date fiscal years) had never exercised it before. The engine has still
never positively exercised `PASS_DIRECT_AGGREGATE` on a real filing (0
for 4 companies) — extending to more companies remains the most likely
way to do so. No new binding accounting decision was made in this
milestone (all changes are generalization/correctness fixes, not policy
changes), so `docs/DECISIONS_LOG.md` was not updated.

Output files (under `data\`): `scripts\36b_download_accession_locked_filing.py`
run created `data\sec_filings_locked\NVDA\000104581024000029\` (manifest +
downloaded filing package). Engine runs: `nvda_20240128_engine_v9_*`
(first, unmodified-engine run, 55/v9), `nvda_20240128_engine_v10_*`,
`orcl_20240531_engine_v10_*`, `msft_20240630_engine_v10_*`,
`meta_20241231_engine_v10_*` (regression) — each with
`presentation.csv`, `row_candidates.csv`, `fact_candidates.csv`,
`result.json`, `arelle_child.log`, `orchestration.log`.

## Batch generalization test — GOOGL, AMZN, MU, CRWD, PANW (5 more companies) — VERIFIED
Goal: determine whether the engine generalizes across additional
industries, fiscal calendars, filing structures, and debt presentations,
in one batch, without ticker-specific rules, assumed-zero values,
weakened validation, or forced `PASS_DIRECT_AGGREGATE`.

**Filings locked** (`scripts\36b_download_accession_locked_filing.py`,
unmodified), each the latest completed annual 10-K available at the
time of this run:

| Ticker | Report date | Accession | Filed |
|---|---|---|---|
| GOOGL | 2025-12-31 | 0001652044-26-000018 | 2026-02-05 |
| AMZN | 2025-12-31 | 0001018724-26-000004 | 2026-02-06 |
| MU | 2025-08-28 | 0000723125-25-000028 | 2025-10-03 |
| CRWD | 2026-01-31 | 0001535527-26-000010 | 2026-03-05 |
| PANW | 2025-07-31 | 0001327567-25-000027 | 2025-08-29 |

**First run — `scripts\56_xbrl_metric_engine.py` (unmodified, v10) —
every genuine gap traced to real, inspectable presentation/fact-
candidate data, never guessed.** Notable findings:
- **`revenue`**: 0 candidates for Amazon — its income statement row is
  labeled **"Total net sales"**, not "Revenue(s)" — a real, common
  retailer convention never seen in the first 4 companies.
- **`operating_cash_flow`**: Amazon's label, "Net cash provided by
  (used in) operating activities", broke the anchored pattern via an
  infix parenthetical.
- **`capex`**: Micron ("Expenditures for property, plant, and
  equipment") and Palo Alto Networks ("Purchases of property, equipment,
  and other assets") each needed a tail phrase no single enumerated
  pattern would have covered.
- **`pretax_income`**: Micron's label continues past "income taxes" with
  "...and equity in net income (loss) of equity method investees" (a
  joint-venture/equity-method disclosure convention).
- **`income_tax_expense`**: Micron ("Income tax (provision) benefit")
  and Palo Alto Networks ("Provision for (benefit from) income taxes")
  each placed the direction-swing parenthetical in a different spot than
  NVIDIA's ("expense (benefit)") — a third placement, confirming
  enumerating placements per metric does not scale.
- **`stockholders_equity`**: 0 candidates for Micron — its balance-sheet
  total row is bare **"Total equity"** (the section header above it
  still says "Shareholders' equity", but the total row itself omits the
  word).
- **current_debt zero-inference condition 2** ("no maturity-schedule
  role found"): Google's and Amazon's debt-repayment role is titled
  "**Future Principal Payments** for Borrowings/Debt", not "Maturities
  of Long-Term Debt" — a real role-title convention gap, not a missing
  disclosure.

**Fix — `scripts\57_xbrl_metric_engine.py` (new file; 56 preserved
unmodified).** All fixes are broadenings of existing structural/label
rules to real, observed conventions, or a consistency extension of the
existing 52/53-week-fiscal-calendar tolerance — never ticker-specific:
1. `revenue`: added "net sales"/"total net sales".
2. `capex`: replaced the enumerated asset-bundle tail list with a
   structural rule — acquisition verb (additions/purchases/
   expenditures) + preposition + "property", then **any** trailing
   text — since the real tails observed (four so far, across five
   companies) show enumeration doesn't scale.
3. `pretax_income`: allowed an optional trailing "and ..." clause after
   "income tax(es)".
4. `stockholders_equity`: accept a bare "Total equity" row.
5. `DEBT_MATURITY_ROLE_PATTERN`: added "future principal payments"
   alongside "maturities".
6. **New generic mechanism** — `_strip_parenthetical_asides()`: rather
   than special-casing each metric's exact parenthetical placement,
   `identify_canonical_row()`'s plain-tier match now also tries the
   label with any `(...)` aside stripped out. This single, general
   mechanism resolved the income-tax-expense placement problem (3
   different placements across NVIDIA/Micron/Palo Alto Networks) *and*
   Amazon's operating-cash-flow infix parenthetical, without a
   dedicated regex for either.
7. **D-017 zero-inference date-tolerance consistency fix**: the
   condition-2/3/4 fact lookups inside the zero-inference proof chain
   (`_fetch_single_fact_value` and everything built on it) previously
   stayed exact-match even when evaluating a *prior* period for a
   52/53-week-fiscal-calendar filer — a real consistency gap against the
   v10 tolerance already applied to ordinary prior-year built-in
   metrics for the same filer. Now threaded through consistently. Still
   exact (tolerance 0) for the current, accession-locked date; a genuine
   multi-value ambiguity inside the window still fails closed exactly as
   before.

**Results — pass rate by company (of the 20 supported metrics):**

| Ticker | PASS | REVIEW_REQUIRED |
|---|---:|---:|
| GOOGL | 14/20 | 6 |
| AMZN | 14/20 | 6 |
| MU | 14/20 | 6 |
| CRWD | 11/20 | 9 |
| PANW | 13/20 | 7 |

**Pass rate by metric (across the 5 companies):** `revenue`,
`net_income`, `operating_income`, `operating_cash_flow`, `capex`,
`free_cash_flow`, `cash_and_equivalents`, `pretax_income`,
`income_tax_expense`, `stockholders_equity` — **5/5 every company.**
`long_term_debt` 4/5, `short_term_investments` 4/5, `effective_tax_rate`
4/5, `nopat` 4/5. `current_debt`, `total_debt`, `adjusted_net_debt`,
`invested_capital`, `average_invested_capital`, `roic` — **0/5**, every
remaining `REVIEW_REQUIRED` in the batch traces back to one of these six
(all downstream of `current_debt`/`long_term_debt`).

**Every remaining REVIEW_REQUIRED, verified as a genuine finding, not a
bug — none force-fixed:**
- **GOOGL, AMZN — `current_debt` condition 2 now correctly evaluates
  (the role-title fix worked) and finds the earliest maturity bucket is
  *nonzero*** (Google: $2,000,000,000 due in 2026; Amazon:
  $2,752,000,000) — i.e., both filers genuinely do have debt due within
  12 months, disclosed only in the debt footnote, not as a separate line
  on the face of the balance sheet. D-017 correctly does not infer zero.
  Whether to promote this footnote figure directly into `current_debt`
  when D-016 finds nothing on the balance sheet face is a **new
  accounting-policy question**, not something decided here.
- **MU — `current_debt` condition 3 fails**: the maturity schedule's
  total ($11,533,000,000) does not reconcile with balance-sheet
  `long_term_debt` ($14,017,000,000) — the same class of genuine
  footnote/balance-sheet gap already documented for Meta under D-017.
  `current_debt_prior` also fails (condition 2) — Micron's debt maturity
  table is not disclosed on a comparative (prior-year) basis at all, a
  structural limitation of the disclosure itself, not a matching bug.
- **CRWD — `current_debt` condition 2 fails**: CrowdStrike's only debt
  disclosure is a revolving **credit facility** (drawn amount, capacity,
  rate — a dimensional `Line of Credit Facility` table), which has no
  fixed maturity-schedule table by its nature. `short_term_investments`
  is genuinely absent from its balance sheet (cash, AR, and prepaid only
  — no marketable-securities line). `effective_tax_rate` is
  `REVIEW_REQUIRED` because pretax income is **negative**
  (-$126,989,000) this fiscal year — exactly D-015's existing rule
  ("if Pretax Income is not positive → REVIEW_REQUIRED"), correctly
  applied, not a gap.
- **PANW — `current_debt` AND `long_term_debt` both `REVIEW_REQUIRED`,
  flagged as unresolved, not fixed.** Palo Alto Networks' entire debt is
  a single balance-sheet line, **"Convertible senior notes, net"**
  (`us-gaap:ConvertibleDebtCurrent`), classified current vs. non-current
  purely by **presentation position** (nested under
  `LiabilitiesCurrentAbstract`) — the label itself contains neither
  "current" nor "non-current" wording, so neither `current_debt` nor
  `long_term_debt`'s label-text matching can safely tell which one it
  is. Broadening the label vocabulary to recognize "Convertible
  [Senior] Notes" would let the SAME row match both metrics'
  independent searches simultaneously (a real double-counting risk in
  `total_debt`), since nothing in the label text disambiguates. Fixing
  this safely needs either a new accounting-policy decision or a
  structural, parent-chain-aware architecture change (using
  `LiabilitiesCurrentAbstract`/`LiabilitiesNoncurrent`-style standard
  ancestry, not label text, to classify current vs. non-current) —
  **deliberately left as `REVIEW_REQUIRED`, per the instruction not to
  invent a policy.**

**`PASS_DIRECT_AGGREGATE` (D-018): still not exercised — 0 for 9
companies now tested.** None of the 5 new companies has a bare,
unqualified "Total debt" row anywhere in its filing.

**Full regression — Oracle, Microsoft, Meta, NVIDIA, same 20-metric
set, script 57 vs. the prior verified script 56/v10 run: zero
differences in any status or value for any of the four companies**
(Oracle `all_pass: true`, Microsoft `all_pass: true`, Meta `all_pass:
false` with the same 4-metric chain as before, NVIDIA `all_pass: true`
— all unchanged).

**Conclusion:** the engine now spans 9 companies across hardware
(Oracle, Micron, NVIDIA), software/cloud (Microsoft, CrowdStrike, Palo
Alto Networks), internet/retail (Alphabet, Amazon), and social media
(Meta), 4 distinct fiscal-calendar conventions (3 fixed month-end
variants + 1 confirmed 52/53-week convention shared by NVIDIA and
Micron), and a wide range of extension taxonomies — with zero ticker-
specific code anywhere. Every fix in this batch was a genuine
broadening of a structural or label rule to a real, observed SEC-filer
convention; the new parenthetical-stripping mechanism in particular
generalizes a pattern that would otherwise require one bespoke regex
per metric per placement. Two REVIEW_REQUIRED findings (Google/Amazon's
nonzero-footnote current debt; Palo Alto Networks' position-only
current/non-current convertible notes) are genuine, structurally-
grounded findings that point toward specific, well-defined next
accounting-policy or architecture decisions — not gaps papered over. No
new binding accounting decision was made in this milestone (all changes
are generalization/correctness fixes), so `docs/DECISIONS_LOG.md` was
not updated.

Output files (under `data\`): filings locked under
`data\sec_filings_locked\{GOOGL,AMZN,MU,CRWD,PANW}\`. Engine runs:
`{ticker}_{reportdate}_engine_v10_*` (first, unmodified-engine run, per
ticker) and `{ticker}_{reportdate}_engine_v11_*` (fixed, verified run),
for all 5 new companies; `orcl_20240531_engine_v11_*`,
`msft_20240630_engine_v11_*`, `meta_20241231_engine_v11_*`,
`nvda_20240128_engine_v11_*` (regression) — each with
`presentation.csv`, `row_candidates.csv`, `fact_candidates.csv`,
`result.json`, `arelle_child.log`, `orchestration.log`.

## Debt Classification Resolver (accounting policy D-019, bounded) — VERIFIED
Goal: resolve the Palo Alto Networks `current_debt`/`long_term_debt`
finding from the batch test — its sole debt instrument, "Convertible
senior notes, net", is classified current-vs-non-current only by
presentation POSITION, not label wording — via a bounded, MODULAR
addition, without rewriting the engine, weakening validation, or
delaying historical multi-year extraction. Approved by council review.

**New file — `scripts\58_xbrl_metric_engine.py`** (57 preserved
unmodified). New section "3d + 4d. DEBT CLASSIFICATION RESOLVER",
implementing D-019's evidence hierarchy tiers 4-5 (tiers 1-3 and 6 are
the existing D-016/D-017/`identify_canonical_row` machinery, reused
unchanged) — see `docs/DECISIONS_LOG.md` D-019 for the full accounting
policy. Key pieces:
- `build_ancestor_chain()` — walks the presentation `parent_qname`
  chain for one row, within one role, from itself to the role's root.
- `classify_current_or_noncurrent_by_ancestry()` — CURRENT if the chain
  passes through a current-liabilities grouping (the standard, near-
  universal `us-gaap:LiabilitiesCurrentAbstract` concept, or a label
  reading as a current-liabilities header); otherwise NON-CURRENT if
  the chain reaches the general Liabilities section at all (the
  convention for a filer, like Palo Alto Networks, that doesn't nest
  non-current liabilities under a matching "non-current" abstract).
- `find_debt_vocabulary_rows()` — a broadened, standalone debt-label
  vocabulary (adds "convertible/senior notes", "term loan",
  "borrowings" to current_debt/long_term_debt's own narrower patterns),
  with an exclusion list for equity components, conversion options, and
  derivative liabilities (D-019 policy #4).
- `resolve_debt_classification_by_ancestry()` — orchestrates the above
  per balance-sheet role, returning matches for the requested
  classification plus FULL evidence (selected concept, label, role,
  ancestor chain, classification reason, maturity-schedule
  corroboration note) for every candidate considered.
- Wired in as **tier 4 only**: for `current_debt`, tried after D-016
  tiers 1-3 already found nothing (before D-017's zero-inference); for
  `long_term_debt`, tried after its own direct label search already
  found nothing. Never overrides an existing successful match; touches
  no other metric. Every ancestry-resolved result's lineage carries a
  new `debt_classification_evidence` block in the output JSON.
- **Related label-vocabulary fix, surfaced by the same review (not part
  of the ancestry module itself):** `current_debt`'s mention/plain
  patterns broadened to recognize a bare **"Current debt"** label
  (Micron's balance sheet — `us-gaap:DebtCurrent`, no further
  qualifier), which every existing tier had been blind to.

**Results:**

| Ticker | current_debt | long_term_debt | total_debt | roic |
|---|---|---|---|---|
| PANW | `PASS` — $0 (FY2025; notes matured/settled — prior year $963,900,000, `PASS`) | `REVIEW_REQUIRED` (precise: no LT debt exists, ancestry-confirmed) | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` |
| MU | `PASS` — $560,000,000 | `PASS` (unchanged) | `PASS` — $14,577,000,000 | `PASS` — 15.86% |

**Palo Alto Networks — convertible notes classification:** the
"Convertible senior notes, net" row (`us-gaap:ConvertibleDebtCurrent`)
was classified **CURRENT** by ancestry (its presentation parent is
`us-gaap:LiabilitiesCurrentAbstract`, "Current liabilities:") — verified
via the fact data itself: the FY2025 (2025-07-31) carrying value is
genuinely `$0` (the notes matured/were settled during the fiscal year),
while the FY2024 comparative fact for the identical row is
`$963,900,000` — confirming the classification and the fact-matching
are both correct, not a bug. `long_term_debt` remains `REVIEW_REQUIRED`
— Palo Alto Networks has no long-term debt row on its balance sheet and
the ancestry search finds no non-current debt-vocabulary candidate
either, so `total_debt`/`adjusted_net_debt`/`invested_capital`/
`average_invested_capital`/`roic` correctly stay `REVIEW_REQUIRED` —
**no new policy was invented to force these to `PASS`**, consistent
with the explicit instruction not to.

**Micron — unrelated but significant side effect:** the bare-"Current
debt" label fix alone (not the ancestry resolver — this row already had
an explicit "current" qualifier-free label matched via the ordinary
D-016 tier-3 sibling path once the vocabulary gap was closed) resolved
Micron's entire remaining chain: `current_debt`, `total_debt`,
`adjusted_net_debt`, `invested_capital`, `average_invested_capital`,
and `roic` all became `PASS` — **Micron went from 14/20 to 20/20,
fully verified.**

**Google, Amazon, CrowdStrike — correctly unaffected**, confirming the
resolver changes nothing when it shouldn't: their `current_debt`
`REVIEW_REQUIRED` reasons (Google/Amazon: a debt-maturity footnote
proves a genuine nonzero balance not reported on the balance sheet face
— a different, footnote-only-disclosure question, not a classification
question; CrowdStrike: no maturity-schedule disclosure and no debt-
vocabulary row on the balance sheet at all — its only debt facility is
a revolving credit line) are unrelated to ancestry/convertible-notes
classification, and the resolver correctly found no new candidate for
any of the three.

**Full regression — Oracle, Microsoft, Meta, NVIDIA, script 58 vs. the
prior verified script 57/v11 run: zero differences in any status or
value for any of the four companies.** No FAIL or TIMEOUT anywhere
across all 9 companies tested in this milestone.

**Acceptance criteria — all met:**
- PANW debt classification improved (`current_debt` `REVIEW_REQUIRED` →
  `PASS`) and `long_term_debt`'s `REVIEW_REQUIRED` became more precisely
  justified — both halves of the "OR" criterion, in fact.
- No ticker-specific logic added — every pattern is a general SEC-filer/
  XBRL-taxonomy convention.
- No verified company regressed (confirmed above).
- Classification evidence fully recorded (`debt_classification_evidence`
  in the output JSON: concept, label, role, ancestor chain, calculation
  relationships reused from existing tiers, context/unit/date via the
  existing fact-matching lineage, classification reason, corroboration
  note).
- The resolver changed no unrelated extraction logic (confirmed by the
  clean regression and by Google/Amazon/CrowdStrike being unaffected).

Output files (under `data\`): `{ticker}_{reportdate}_engine_v12_*` for
PANW (tested first, alone, per instruction), then GOOGL/AMZN/MU/CRWD
(batch), then ORCL/MSFT/META/NVDA (regression) — each with
`presentation.csv`, `row_candidates.csv`, `fact_candidates.csv`,
`result.json`, `arelle_child.log`, `orchestration.log`.

## Historical multi-year point-in-time extraction — first proof (Oracle, 5 years) — VERIFIED
Goal: per the user's explicit instruction, begin **historical multi-year
point-in-time extraction for all reliable metrics** (the "15 companies
× 5 years" backtest universe target from `docs/PROJECT_CONTEXT.md`),
starting with a small proof on one company before scaling — Oracle, the
only company with zero `REVIEW_REQUIRED` at the time.

**Filings locked** (`scripts\36b_download_accession_locked_filing.py`,
unmodified) — Oracle's 4 additional fiscal years, alongside the
already-locked FY2024:

| Fiscal year | Report date | Accession |
|---|---|---|
| FY2020 | 2020-05-31 | 0001564590-20-030125 |
| FY2021 | 2021-05-31 | 0001564590-21-033616 |
| FY2022 | 2022-05-31 | 0001564590-22-023675 |
| FY2023 | 2023-05-31 | 0000950170-23-028914 |
| FY2024 | 2024-05-31 | 0000950170-24-075605 |

**First run — `scripts\58_xbrl_metric_engine.py` (unmodified) — result:
running the SAME company across YEARS (not companies) surfaced two
further genuine, general label-convention gaps** — the same kind of
drift already handled across companies, now also occurring within one
company over time, root-caused via the presentation CSVs as always:
- `identify_canonical_row`'s parenthetical-stripped-label fallback
  (from the earlier batch generalization work) only applied to the
  anchored plain-tier match, not to the earlier, looser
  `mention_pattern` candidate-pool filter — so FY2022's stockholders'-
  equity total, labeled **"Total stockholders' (deficit) equity"**
  (Oracle had an accumulated deficit that year), never even entered the
  candidate pool.
- `income_tax_expense`: FY2021-2023 used two further parenthetical
  placements — "Benefit from (provision for) income taxes" and
  "(Provision for) benefit from income taxes" — a "from" variant of the
  existing "for" pattern, once stripped.
- `pretax_income`: FY2021's label, "Income before benefit from
  (provision for) income taxes", needed the same "from"/"for" variant.

**Fixes — `scripts\59_xbrl_metric_engine.py`** (mention-tier stripped-
label fallback; `income_tax_expense`'s "from" variant) **and
`scripts\60_xbrl_metric_engine.py`** (`pretax_income`'s "from" variant,
found on a second pass) — both new files, 58/59 preserved unmodified.
Neither fix is ticker- or year-specific.

**Results — Oracle, 5 consecutive fiscal years, one engine call each:**

| Fiscal year | all_pass | Revenue | Net Income | ROIC |
|---|---|---:|---:|---:|
| FY2020 | `true` | $39,068,000,000 | $10,135,000,000 | 28.49% |
| FY2021 | `false` | $40,479,000,000 | $13,746,000,000 | `REVIEW_REQUIRED` |
| FY2022 | `true` | $42,440,000,000 | $6,717,000,000 | 20.90% |
| FY2023 | `true` | $49,954,000,000 | $8,503,000,000 | 18.76% |
| FY2024 | `true` | $52,961,000,000 | $10,467,000,000 | 16.36% |

**FY2021's `REVIEW_REQUIRED` is a genuine finding, not a bug — verified,
not assumed:** Oracle's FY2021 effective tax rate computes to **-5.75%**
(a small net tax *benefit* relative to positive pretax income of
$12,999,000,000), which correctly falls outside D-015's existing [0, 1]
plausible-range check and fails closed rather than being reported as a
misleadingly negative "effective tax rate". `pretax_income` and
`income_tax_expense` both resolve to `PASS` individually; only the
downstream ratio is flagged — exactly the policy working as intended.

**Full regression — all 8 other previously-verified companies (Oracle
FY2024, Microsoft, Meta, NVIDIA, Google, Amazon, Micron, CrowdStrike,
Palo Alto Networks), script 60 vs. the prior verified script 58/v12 run:
zero differences in any status or value for any company.** No FAIL or
TIMEOUT anywhere.

**Conclusion:** the engine generalizes across TIME the same way it
already generalizes across companies — new fiscal years periodically
surface new label-convention variants (here, two more parenthetical-
placement drifts and one more "for"/"from" wording drift), each fixed
the same way every prior gap in this project has been fixed: traced to
real presentation data, broadened generically, regression-verified. This
is the first slice of the "15 companies × 5 years" target; extending to
the other 8 already-tested companies (and beyond, toward Nebius,
Broadcom, ServiceNow, and the cruise companies named in
`docs/PROJECT_CONTEXT.md`, none locked yet) is the direct next step.

Output files (under `data\`): `data\sec_filings_locked\ORCL\` gained 4
new accession directories. Engine runs: `orcl_20200531_engine_v12_*`
through `orcl_20230531_engine_v12_*` (first, unmodified-engine runs),
`orcl_{reportdate}_engine_v13_*` (first fix), `orcl_{reportdate}_engine_v14_*`
(final, verified run, all 5 years) — each with `presentation.csv`,
`row_candidates.csv`, `fact_candidates.csv`, `result.json`,
`arelle_child.log`, `orchestration.log`. Regression outputs for the
other 8 companies use the `_v14_` suffix.

## Historical multi-year point-in-time extraction — full 9-company batch (45 company-years) — VERIFIED
Goal: extend the Oracle 5-year proof to all 9 validated companies
(Microsoft, Meta, NVIDIA, Google, Amazon, Micron, CrowdStrike, Palo Alto
Networks), using the unmodified, already-verified engine
(`scripts\60_xbrl_metric_engine.py`) — no accounting-policy change, no
ticker- or year-specific logic — and consolidate the result into a
queryable historical dataset, filing manifest, and quality report.

**Filings locked** (`scripts\36b_download_accession_locked_filing.py`,
unmodified): 30 new filings (4 additional prior fiscal years per
company; Google needed 4 new + confirming 2 already-locked years =
5 total). Each company's 5-year window is anchored on its
previously-verified "latest" filing at the time it was first tested
(not necessarily the single most-recent 10-K now on EDGAR — several
companies have since filed a newer one; see the coverage note in
`data\historical_missing_years_v1.json`). One transient output-capture
glitch during the parallel locking batch (MSFT FY2021 showed a false
`success=False`) was caught and resolved by re-running that one lock
step alone — confirmed correct on retry, no duplicate engine run
resulted.

**Engine runs:** all 45 company-years (9 companies × 5 fiscal years)
now have a verified `{ticker}_{reportdate}_engine_v14_result.json`.
Zero `FAIL`, zero `TIMEOUT`, anywhere. Per-filing engine runtime: ~3-10
seconds (`elapsed_seconds` field in each result file), consistent with
every single-company test run in this project.

**New file — `scripts\61_build_historical_dataset.py`** (read-only
consolidation — never calls Arelle, never re-derives a value; the
generic engine scripts 42-60 are all still preserved unmodified).
Produces, from the 45 result JSONs:
- `data\historical_dataset_v1.csv` / `.json` — flat, one row per
  (ticker, report_date, metric), full point-in-time lineage (reportDate,
  filingDate as the point-in-time availability date, accessionNumber,
  primaryDocument, engine version, concept, label, context, unit,
  value, status, validation reason, formula for derived metrics).
- `data\historical_dataset_full_lineage_v1.json` — same coverage, but
  preserving each metric's full nested lineage (components,
  components_detail, debt_classification_evidence, QA cross-references)
  exactly as the engine produced it.
- `data\historical_filing_manifest_v1.csv` — one row per company-year:
  form, reportDate, filingDate, accessionNumber, primaryDocument,
  engine version, `all_pass`, and an `is_anchor_year` flag.
- `data\historical_quality_report_v1.json` — PASS/REVIEW_REQUIRED/
  FAIL/TIMEOUT counts and rates: overall, by company, by fiscal year,
  by metric (computed over the 20 primary supported metrics ×
  45 company-years = 900 metric-results).
- `data\historical_missing_years_v1.json` — per-company target vs.
  actual year coverage (0 missing for all 9 — see below) plus the
  anchor-year coverage caveat.
- `data\historical_review_required_v1.json` — every `REVIEW_REQUIRED`
  case grouped by root-cause pattern (regex-matched against the
  engine's own error text).
- `data\historical_regression_note_v1.json` — confirms each of the 9
  anchor-year result files is intact (valid JSON, correct ticker/
  report_date identity, no FAIL/TIMEOUT) and was NOT regenerated during
  this session (file timestamps predate this session's batch runs) — by
  construction, there is no regression risk from this milestone's own
  work on those 9 files.

**Results — 900 primary-metric results (20 metrics × 45 company-years):**

| | Count | Rate |
|---|---:|---:|
| PASS | 678 | 75.3% |
| REVIEW_REQUIRED | 222 | 24.7% |
| FAIL | 0 | 0% |
| TIMEOUT | 0 | 0% |

**By company (PASS rate):** MSFT 100% (100/100, every year), ORCL 97%,
NVDA 91%, MU 72%, META 68%, AMZN 68%, PANW 63%, GOOGL 61%, CRWD 58%.

**By metric (PASS rate):** `revenue`, `net_income`, `operating_income`,
`operating_cash_flow`, `capex`, `free_cash_flow`, `income_tax_expense`,
`stockholders_equity` — **100% (45/45) every company-year.**
`short_term_investments` 96%, `cash_and_equivalents` 84%,
`long_term_debt` 89%, `pretax_income` 80%, `effective_tax_rate`/`nopat`
64%. Debt-dependent chain lowest, as expected from the debt-resolver
milestone's own findings: `current_debt` 53%, `total_debt` 47%,
`invested_capital`/`average_invested_capital` 33%, `roic` 29%.

**Root-cause breakdown of the 222 REVIEW_REQUIRED cases** (from
`historical_review_required_v1.json`; 162 of these are cascading —
a derived metric correctly blocked by an upstream component, not an
independent finding):
- **162 cascading** (derived metric blocked by an already-documented
  upstream `REVIEW_REQUIRED` component — `total_debt`,
  `adjusted_net_debt`, `invested_capital`, `average_invested_capital`,
  `nopat`, `roic`, and `effective_tax_rate` when blocked by
  `pretax_income`).
- **21 `current_debt` zero-inference findings** — 10 "earliest maturity
  bucket is nonzero" (genuine footnote debt not on the balance sheet
  face — the same Google/Amazon finding from the batch-generalization
  milestone, now also appearing in their prior years), 8 "no maturity-
  schedule role found" (CrowdStrike's revolving-credit-only years), 3
  "schedule total doesn't reconcile with long_term_debt" (Micron's
  historical years, same class as Meta's original D-017 finding).
- **16 `long_term_debt`/`pretax_income`/`cash_and_equivalents` row-not-
  found or ambiguous cases** — genuine label-convention variants in
  specific historical years not yet generalized (e.g. Google/CrowdStrike/
  Palo Alto Networks' `pretax_income` label changed in some years; Palo
  Alto Networks had two rows both matching the anchored
  `cash_and_equivalents` plain pattern in FY2021-2023; Micron's balance
  sheet did not present a row matching `cash_and_equivalents`'s pattern
  in FY2021-2024). **Correctly left unresolved per this task's explicit
  instruction not to modify the engine — recorded as findings for a
  future generalization pass, not fixed here.**
- **7 `effective_tax_rate` genuine range/sign findings** — 4 outside the
  [0, 1] plausible range (Oracle FY2021's already-documented tax-benefit
  year, plus 3 new ones: NVIDIA FY2023, CrowdStrike FY2025, Palo Alto
  Networks FY2024 — all real, unforced findings, D-015's existing rule
  applied correctly, never weakened), 3 with non-positive pretax income.
- **2 `short_term_investments` genuinely absent.**

**Coverage note:** all 9 companies reached their full 5-year target (0
missing years). Each window is anchored on the company's
previously-verified "latest" year, not necessarily the single most
recent 10-K now available — several companies (NVIDIA, Google, Amazon,
Micron, CrowdStrike, Palo Alto Networks) have since filed a newer 10-K
than their anchor; extending forward to the true latest year is a
separate, not-yet-done step.

**Regression:** all 9 previously-verified anchor-year result files
confirmed intact and untouched by this session (see
`historical_regression_note_v1.json`) — no regeneration, therefore no
regression risk introduced by this milestone.

**Conclusion:** the dataset is internally consistent (0 FAIL/TIMEOUT,
full lineage preserved, filingDate retained as the point-in-time
availability date, no amended/restated filing silently substituted for
an original), and every `REVIEW_REQUIRED` traces to an already-
understood or newly-observed-but-plausible root cause — none
unexplained. Per the task's explicit instruction, **no engine or policy
change was made in this milestone** — this is a pure extraction +
consolidation pass. No new binding accounting or architecture decision
was required, so `docs/DECISIONS_LOG.md` was not updated.

Output files (under `data\`): 30 new `sec_filings_locked\{TICKER}\`
accession directories; 40 new `{ticker}_{reportdate}_engine_v14_*`
run outputs (`presentation.csv`, `row_candidates.csv`,
`fact_candidates.csv`, `result.json`, `arelle_child.log`,
`orchestration.log`); the 7 consolidated dataset/manifest/report files
listed above, all versioned `_v1`.

## Persistent DuckDB (all 9 companies) and REVIEW_REQUIRED root-cause analysis — VERIFIED
`data\database\ai_stock_agent.duckdb` (5-table schema: companies,
sec_filings, extraction_runs, financial_metric_results,
historical_review_items) loaded for all 9 companies
(`scripts\66_build_persistent_duckdb_all_companies.py`, verified by
`scripts\67_validate_persistent_duckdb_all_companies.py`), idempotent
under a double-run. A read-only root-cause analysis of the 222
REVIEW_REQUIRED results (`scripts\68_review_required_root_cause_analysis.py`)
found 51 primary root causes across 17 distinct patterns (162 of the
222 were cascading/downstream, not independent findings).

## D-020 — Pretax Income structural fallback — 9 affected company-years re-extracted — VERIFIED
The root-cause analysis's largest single-metric bucket was `pretax_income`
row-identification failure: 9 REVIEW_REQUIRED results (7 `row_not_found`,
2 `row_ambiguous` — the "primary items: 7" figure from an earlier report
only reflected the `row_not_found` sub-bucket; both sub-buckets together
account for the full 9). All 9 were individually inspected in their
presentation CSVs and confirmed to share one general structural pattern:
the pretax-income row is the immediate presentation-order predecessor of
`income_tax_expense`, within the same statement role and parent, across 3
distinct label wordings (see D-020 in `docs/DECISIONS_LOG.md` for the
full accounting policy and per-company label variants).

**New engine — `scripts\69_xbrl_metric_engine.py`** (copied from
`scripts\60_xbrl_metric_engine.py`, which remains preserved unmodified
and is still the engine of record for the other 36 company-years):
adds `find_pretax_income_by_structural_position()` as a fallback tier
after the existing label search, plus per-run timing instrumentation
(`arelle_load_seconds`, `extraction_seconds`, `validation_seconds`,
`total_elapsed_seconds`) newly recorded in every result JSON.

**Reran only the 9 affected company-years, plus regression on all 9
previously-verified latest-year anchors** (ORCL 2024-05-31, MSFT
2024-06-30, META 2024-12-31, NVDA 2024-01-28, GOOGL 2025-12-31, AMZN
2025-12-31, MU 2025-08-28, CRWD 2026-01-31, PANW 2025-07-31) — zero
differences in any status or value on any anchor. No other company-year
was touched.

**Result — all 9 affected company-years:**

| Ticker | FY end | pretax_income | effective_tax_rate | nopat | roic | Arelle load (s) | Extraction (s) | Validation (s) | Total (s) |
|---|---|---|---|---|---|---:|---:|---:|---:|
| CRWD | 2022-01-31 | PASS | REVIEW_REQUIRED | REVIEW_REQUIRED | REVIEW_REQUIRED | 0.9465 | 0.2771 | 0.0007 | 2.3906 |
| CRWD | 2023-01-31 | PASS | REVIEW_REQUIRED | REVIEW_REQUIRED | REVIEW_REQUIRED | 0.9159 | 0.2740 | 0.0018 | 2.2393 |
| GOOGL | 2021-12-31 | PASS | PASS | PASS | REVIEW_REQUIRED | 1.0325 | 0.3153 | 0.0008 | 2.5595 |
| GOOGL | 2022-12-31 | PASS | PASS | PASS | REVIEW_REQUIRED | 1.0246 | 0.2911 | 0.0008 | 2.3625 |
| GOOGL | 2023-12-31 | PASS | PASS | PASS | REVIEW_REQUIRED | 1.0321 | 0.3069 | 0.0007 | 2.3937 |
| MU | 2021-09-02 | PASS | PASS | PASS | REVIEW_REQUIRED | 1.1670 | 0.3439 | 0.0011 | 2.5840 |
| MU | 2022-09-01 | PASS | PASS | PASS | REVIEW_REQUIRED | 1.0100 | 0.2702 | 0.0010 | 2.3323 |
| PANW | 2021-07-31 | PASS | REVIEW_REQUIRED | REVIEW_REQUIRED | REVIEW_REQUIRED | 1.1088 | 0.3127 | 0.0007 | 2.5327 |
| PANW | 2022-07-31 | PASS | REVIEW_REQUIRED | REVIEW_REQUIRED | REVIEW_REQUIRED | 1.0881 | 0.3022 | 0.0008 | 2.4511 |

`pretax_income` reached PASS in all 9/9 — the structural fix worked as
designed in every case. `effective_tax_rate`/`nopat` reached PASS in
only 5/9 (GOOGL ×3, MU ×2): CRWD/PANW's 4 company-years have genuinely
negative pretax income, correctly triggering D-015's pre-existing,
unweakened "pretax income must be positive" rule — verified via each
filing's own error text (`Pretax Income אינו חיובי`), not assumed.
`roic` changed in 0/9, per instruction — verified each of the 9 is
blocked purely by `average_invested_capital` (an independent,
already-known debt-chain root cause, D-016/D-017/D-019), never by
`nopat` alone once `nopat` itself passed (GOOGL/MU), and still by both
`nopat` and `average_invested_capital` where `nopat` remains blocked
(CRWD/PANW). No other metric changed for any of the 9 company-years
(directly diffed against each one's prior `_engine_v14_` result — zero
differences outside `pretax_income`/`effective_tax_rate`/`nopat`/`roic`).

**New file — `scripts\70_apply_pretax_income_fix_and_rebuild.py`**
(read-only with respect to the 36 unaffected company-years and every
prior output file): loads the 9 new v15 results into the existing
persistent DuckDB as 9 brand-new `extraction_run` rows (natural key
`accession_number::engine_version`, and `engine_version` differs from
the existing v14 runs for the same accessions — so the old v14 rows for
these 9 filings are untouched, still queryable, coexisting with the new
v15 rows), then rebuilds the REVIEW_REQUIRED root-cause ranking against
a merged "latest state" view (v15 for the 9 affected company-years, v14
unchanged for the other 36).

**REVIEW_REQUIRED count: 222 → 203** (19 converted to PASS: 9
`pretax_income` + 5 `effective_tax_rate` + 5 `nopat`; 0 newly
REVIEW_REQUIRED — confirmed zero regression). Remaining root causes are
unchanged in kind from the prior analysis (`current_debt`/
`long_term_debt` zero-inference and ancestry gaps, `cash_and_equivalents`/
`short_term_investments` row gaps, `effective_tax_rate` genuine
range/sign findings including the 4 CRWD/PANW non-positive-pretax-income
cases now directly visible) — none newly introduced by this milestone.

Output files (under `data\`): 18 new `{ticker}_{reportdate}_engine_v15_*`
run outputs (9 affected company-years + 9 regression-check anchors),
`data\database\ai_stock_agent.duckdb` updated in place (9 new
extraction_run rows, no prior row overwritten).

## Read-only REVIEW_REQUIRED reranking (203 confirmed) — VERIFIED
A read-only rerank (`scripts\71_review_required_rerank.py`) against the
merged latest state (v15 for the 9 D-020-affected company-years, v14
for the other 36) confirmed **203 REVIEW_REQUIRED** (46 primary root
items, 157 downstream/cascading). Five largest root causes ranked by
total potentially-resolved: `current_debt::zero_inference_earliest_
bucket_nonzero` (60, AMZN+GOOGL all 5 years each), `current_debt::
zero_inference_role_not_found` (48, CRWD all years + META 2020-2021 +
NVDA 2020), `long_term_debt::ancestry_confirmed_absent` (30, META
2020-2021 + PANW 2023-2025), `current_debt_prior::zero_inference_
prior_bucket_unreliable` (26, AMZN/GOOGL all years + META 2022-2024),
`effective_tax_rate::pretax_income_not_positive` (21, genuine negative-
income findings, not an extraction issue). Full ranking and per-cause
detail saved to `docs\LAST_CLAUDE_REPORT.md` (superseded by the D-021
report below — see git/file history if the pre-D-021 version is
needed).

## D-021 (attempted) — current_debt from maturity-schedule bucket — STOPPED at proof test
Targeted the largest remaining root cause,
`current_debt::zero_inference_earliest_bucket_nonzero` (AMZN and GOOGL,
all 5 years each, 60 potentially-resolved results), with a bounded
extension: instead of only using the debt-maturity schedule's earliest
("next twelve months") bucket to PROVE current_debt = 0 (D-017,
unchanged), attempt to use that bucket's own reported value directly AS
current_debt — but ONLY when evidenced as a GAAP CARRYING amount, never
an undiscounted face-value/principal repayment amount.

**New engine — `scripts\72_xbrl_metric_engine.py`** (v16, copied from
`scripts\69_xbrl_metric_engine.py`/v15, which remains unmodified and is
still the engine of record for every other metric). New function
`attempt_current_debt_from_maturity_bucket()`, tried only after D-017's
existing zero-inference proof has already failed — never overriding it.

**Proof test (AMZN 2021-12-31, GOOGL 2021-12-31), per the task's own
stop condition — both REJECTED, correction NOT rolled out:** both
filings' earliest maturity bucket (AMZN: $1,493,000,000; GOOGL:
$187,000,000, both concept
`us-gaap:LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths`)
is, by its own concept name, an undiscounted PRINCIPAL repayment
amount, not a GAAP carrying amount — correctly rejected. Neither filing
discloses a separate carrying amount for the current-year slice in
isolation. This reflects the near-universal ASC 835/470 debt-maturity-
schedule disclosure convention (undiscounted principal, always), not an
AMZN/GOOGL-specific gap — very likely blocks this same approach
industry-wide. `current_debt` (and its 6 dependent metrics) remains
REVIEW_REQUIRED for all 10 AMZN/GOOGL company-years, unchanged.

**Regression found and fixed before completion:** the first wiring
attempted the new tier on ANY zero-inference failure, not specifically
"earliest bucket nonzero" — Meta's zero-inference genuinely fails at
D-017 condition 3 (schedule total $29.0B vs. balance-sheet
`long_term_debt` $28.826B, a real ~$174M unresolved discrepancy) while
Meta's own earliest bucket is already zero; the first wiring wrongly
re-accepted that zero bucket, flipping `current_debt`/`total_debt`/
`adjusted_net_debt`/`invested_capital` to a false PASS. Fixed with an
explicit scope guard (reject whenever the fetched bucket value is
exactly 0). Re-verified clean on all 9 latest-year anchors after the
fix — zero differences in any metric.

**REVIEW_REQUIRED count: 203 → 203 (unchanged)** — no correction was
implemented; the 10-company rerun, DuckDB update, and re-ranking were
correctly not performed. Full report, including the recommendation for
how to proceed, in `docs\LAST_CLAUDE_REPORT.md`.

Output files (under `data\`): `scripts\72_xbrl_metric_engine.py` (new,
v16); `{ticker}_{reportdate}_engine_v16_*` for 2 proof-test filings +
9 regression-check anchors (11 filings total, presentation.csv,
row_candidates.csv, fact_candidates.csv, result.json, arelle_child.log,
orchestration.log each). No DuckDB changes.

## Reusable raw structured XBRL warehouse — bounded proof (AMZN, 2024-12-31) — PASS
Architecture correction requested before any further debt-normalization
work: prove a reusable raw structured XBRL warehouse — parse a filing
with Arelle ONCE, preserve the complete parsed XBRL layer (facts,
contexts, units, concepts, labels, presentation/calculation/definition
relationships, roles), and prove that debt-classification analysis can
rerun entirely from stored DuckDB data without ever reopening Arelle.
Purely architectural — does not touch or resolve the D-021
`current_debt` accounting question (still stopped, unchanged).

**New scripts:** `scripts\73_build_xbrl_warehouse_proof.py` (parses
AMZN's already-locked 2024-12-31 10-K, accession
0001018724-25-000004, with Arelle exactly once; writes to a SEPARATE
proof database) and `scripts\74_query_xbrl_warehouse_debt_maturity.py`
(DuckDB-only analysis, zero Arelle import — verified by inspection).
Production database (`data\database\ai_stock_agent.duckdb`) never
opened by either script.

**Database:** `data\database\xbrl_warehouse_proof.duckdb`, 9,973,760
bytes (9.51 MB). 10 tables, all populated: `xbrl_facts` (1,499),
`xbrl_contexts` (357), `xbrl_units` (7), `xbrl_concepts` (1,679,
scoped to this filing's own referenced concepts — not the full base
taxonomy, a documented bounding decision), `xbrl_labels` (1,987, same
scoping), `xbrl_presentation_relationships` (1,087),
`xbrl_calculation_relationships` (268), `xbrl_definition_relationships`
(1,549), `xbrl_roles` (177), `warehouse_runs` (1).

**Runtime:** Arelle parse + warehouse creation = 1.582s total (local
file I/O 0.0008s; taxonomy/DTS load + instance parse, combined —
Arelle's public Session API does not expose these as two separately
measurable phases — 1.255s; fact extraction 0.064s; relationship
extraction 0.132s; DuckDB write 0.120s). Subsequent DuckDB-only
semantic query (`scripts\74`, run twice) = 0.060s then 0.049s — **~26–
32× faster than the initial Arelle parse**, with zero XBRL model in
memory.

**Debt-maturity result — identical conclusion to D-021, reproduced via
a completely independent code path (SQL/pandas over stored tables, not
live Arelle traversal):** earliest bucket = concept
`us-gaap:LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths`,
$5,000,000,000, correctly classified `debt_principal_not_carrying_
value` — an undiscounted principal amount, not a GAAP carrying amount,
confirming (not resolving) D-021's finding on a second fiscal year
(2024, vs. 2021 previously). Non-debt exclusion logic verified against
79 real lease/finance-lease/purchase-obligation/commitment concepts
found elsewhere in this same warehouse — all correctly excluded.
Reproducibility confirmed: two independent runs of the full analysis
produced byte-identical JSON output.

**Result: PASS on every acceptance criterion** — all required facts/
relationships stored, every fact traceable to the filing via
`accession_number`, maturity-schedule candidates fully reconstructable
from DuckDB, the second query needs no Arelle load, no original file
or verified result overwritten, no ticker-specific rule introduced. No
missing structural evidence was found requiring a schema expansion.
Full report (including the "information lost" disclosure — concepts/
labels intentionally scoped to referenced-only, no formula/validation
results stored, no entity/CIK filtering on facts, no full HTML
rendering context) in `docs\LAST_CLAUDE_REPORT.md`.

Not scaled beyond this one filing; not yet decided whether to adopt
the warehouse as the architecture for canonical-metric computation
(would need its own `docs\DECISIONS_LOG.md` entry). Quarterly
extraction not started.

## Warehouse generalization proof (MSFT/META/NVDA, FY2024) — PASS (39/39)
Extended the AMZN single-filing warehouse proof to 3 more already-
locked filings spanning different fiscal calendars, filing structures,
extension concepts, and debt presentations: MSFT 2024-06-30 (June FYE),
META 2024-12-31 (Dec FYE), NVDA 2024-01-28 (52/53-week, Jan FYE).

**New script `scripts\75_build_xbrl_warehouse_multi_proof.py`**
(generalized from `scripts\73`, which remains preserved unmodified):
loops over the 3 filings, parses each with Arelle exactly once, closes
before the next, writes into the SAME warehouse
(`data\database\xbrl_warehouse_proof.duckdb`) alongside AMZN's existing
rows — idempotent per `accession_number` (`CREATE TABLE IF NOT EXISTS`
+ per-accession `DELETE` instead of `scripts\73`'s blanket table wipe,
the one deliberate change needed now that the warehouse is proven
reusable across multiple filings). All 3 loaded successfully (status
PASS); AMZN's prior rows confirmed untouched. Warehouse now holds 4
filings, 6,011 facts total, 48,508,928 bytes.

**New script `scripts\76_query_xbrl_warehouse_canonical_candidates.py`**
(DuckDB-only, zero Arelle import): reconstructs the same row-
identification and current_debt tiered-resolution algorithms already
verified live (`identify_canonical_row`, D-016 tiers 1-3, D-017 zero-
inference, D-019 ancestry) — copied unchanged from `scripts\72` — fed
from a warehouse-reconstructed presentation DataFrame and a warehouse-
based fact-matcher, for 13 canonical metrics × 3 filings (39 checks).

**Two general bugs found and fixed (in a new script,
`scripts\77_query_xbrl_warehouse_canonical_candidates_v2.py`; `76`
preserved unmodified), both confirmed by direct diagnostic evidence
before any code change:**
1. **Label resolution** (found first, in `76` directly): the live
   engine displays each presentation row's label using the arc's own
   `preferredLabel` role (e.g. "verboseLabel" → "Revenue"), not always
   the generic standard label role — MSFT's revenue concept's standard
   label is the full technical text "Revenue from Contract with
   Customer, Excluding Assessed Tax", breaking the anchored match. The
   warehouse already stored `preferred_label` per edge; `76`'s
   reconstruction query just wasn't using it. Fixed directly in `76`
   (this one predates the interrupted session).
2. **Unit filtering** (root cause of MSFT's 0/13, found this task): a
   fact's `unit_id` is an arbitrary filer-chosen XML `@id` (MSFT:
   `U_USD`; AMZN/META/NVDA: `usd`, coincidentally), never a standard —
   confirmed by inspecting `xbrl_units` across all 4 filings. `77`'s
   `usd_unit_ids_for_accession()` now matches on the unit's actual
   `iso4217:USD` measure instead.
3. **Same-context rounding-precision duplicates** (found immediately
   after, blocking MSFT's `current_debt`): MSFT's `CommercialPaper`
   fact is tagged twice at the same context — $6,693,000,000
   (decimals=-6) and $6,700,000,000 (decimals=-8, the same value
   rounded) — the same known pattern `scripts\72`'s
   `_reconcile_same_context_precision_duplicates` already handles live;
   `77` reproduces that reconciliation unchanged.

**Result: 39/39** — MSFT 13/13, META 13/13 (regression-clean,
`current_debt`/`total_debt` correctly still REVIEW_REQUIRED, matching
live), NVDA 13/13 (regression-clean). DuckDB-only query time: 0.16-
0.19s per filing, no Arelle load. Full diagnostic sequence and runtime
in `docs\LAST_CLAUDE_REPORT.md`.

**D-021 recorded** (`docs\DECISIONS_LOG.md`): adopts the warehouse
architecture going forward, and formally binds the debt-basis policy
already explored in the earlier (stopped) current_debt proof-test — an
undiscounted maturity-schedule principal amount may never become
canonical `current_debt`; it is preserved only as a separate
structured candidate.

**`docs\DECISIONS_LOG.md` D-019/D-020 structural corruption — REPAIRED**
(separate task): D-019's own accounting-policy items 2-7 (plus a Micron
finding paragraph and a Results block) were moved back to directly
follow D-019's own item 1, restoring both D-019 and D-020 as complete,
self-contained entries. No decision text lost, reworded, or
reinterpreted — verified by re-reading the full file after the repair.
D-021's own internal note about this corruption has been updated to
reflect the repair.

Not scaled beyond these 4 filings (of 45) as of the generalization
proof. Production database not modified by the generalization proof
itself (see the D-022 milestone below for the first production-
database write in this warehouse line of work).

## D-022 — Maturity-based debt classification (AMZN/GOOGL, FY2021-2025) — PASS
Approved policy: debt principal contractually due within 12 months is
current maturity, due after 12 months is long-term maturity;
`total_debt_maturity = current_debt_maturity + long_term_debt_maturity`;
basis always explicitly stored as `MATURITY_PRINCIPAL`, never described
as a GAAP carrying value; carrying-value debt preserved separately when
it exists; for `total_debt` (and metrics depending on it): prefer a
reliable carrying-value total, fall back to `total_debt_maturity` only
when no reliable carrying-value total exists, status
`PASS_MATURITY_BASIS`. `current_debt`/`long_term_debt` themselves are
UNCHANGED by this policy — D-021 still binding: never set from a
maturity-principal amount.

**Warehouse extended to all 10 AMZN/GOOGL company-years (2021-2025
each)** — `scripts\78_build_xbrl_warehouse_amzn_googl_debt.py`
(generalizes 75/73; already_loaded() now checks by accession_number
alone across ANY prior loading script, so AMZN 2024-12-31 — already
warehoused by `scripts\73` — was correctly skipped, not reparsed). 8
filings newly parsed with Arelle (each exactly once); 2 already
present. Warehouse now holds 12 filings (4 prior + these 8 new).

**Bounded proof (AMZN 2024-12-31 + GOOGL 2021-12-31) — PASS, zero
reconciliation gap on both** — every maturity bucket reconstructed,
classified (current = earliest bucket; long-term = every other
non-Total bucket; zero excluded non-debt rows present in either
schedule), summed, and cross-checked against the schedule's own
reported Total row: **exact match ($0.00 gap) on both.** Judged
structurally safe and general → rolled out to all 10.

**New script `scripts\79_maturity_based_debt_classification.py`**
(extends 77, DuckDB-only, zero Arelle): reuses every unchanged D-016/
D-017/D-019/D-021 carrying-value function; adds `classify_maturity_
buckets()` (new) and the total_debt fallback decision. **Result: 30
REVIEW_REQUIRED → PASS_MATURITY_BASIS conversions** — `total_debt`,
`adjusted_net_debt`, `invested_capital`, all 10/10 company-years.
`current_debt`/`long_term_debt` unchanged (10/10 REVIEW_REQUIRED /
10/10 PASS respectively, exactly as before). `average_invested_capital`/
`roic` remain REVIEW_REQUIRED (10/10 each) — blocked by
`total_debt_prior`, unaffected by this policy.

**Prior-period cause evaluated, not improved:** `current_debt_prior::
zero_inference_prior_bucket_unreliable` (26 downstream results) —
confirmed EMPIRICALLY (direct `xbrl_facts` query, not assumed) that the
debt-maturity schedule is a single, forward-looking disclosure with no
prior-year comparative bucket for the same concepts in the same filing
— structurally unresolvable by this policy, 0/10 company-years
improved on this specific cause.

**REVIEW_REQUIRED count: 203 → 173** (30 converted, 0 new). Regression:
7 of 9 anchors untouched by construction (different tickers entirely);
the other 2 (AMZN/GOOGL 2025-12-31) are within this task's own scope —
their intended 3-metric change verified, every other metric (including
`current_debt` itself) confirmed byte-identical to prior ground truth.
**Zero regressions.**

**Production database updated** (`data\database\ai_stock_agent.duckdb`,
via new `scripts\80_load_d022_maturity_basis_extraction_runs.py`): 10
new `extraction_run` rows (`engine_version = "v1-maturity-basis
(scripts/79, D-022)"`), 70 new `financial_metric_results` rows (7
metrics × 10 company-years) — additive only, every prior v14/v15 row
for these same 10 accessions confirmed still present and untouched.

Runtime ~14 minutes total (slightly under the 15-25 minute expected
window); no filing approached the per-filing stop threshold. Full
evidence, per-filing runtime table, and proof detail in
`docs\LAST_CLAUDE_REPORT.md`.

## Read-only rerank (post-D-022) — 173 confirmed, top-5 root causes identified
Latest-run-per-filing query (`ROW_NUMBER() ... ORDER BY loaded_at DESC`
against the production database) confirmed **173 REVIEW_REQUIRED** (66
primary, 107 downstream) — D-022's 30 conversions correctly excluded.
Top 5: `current_debt::zero_inference_role_not_found` (48, CRWD/META/
NVDA); a tie at 30 between `long_term_debt::ancestry_confirmed_absent`
(META/PANW) and the AMZN/GOOGL current_debt zero-inference cause
(post-D-022, English-message variant — found split into 3 mislabeled
entries by the automated classifier, manually consolidated); a tie at
20 between `effective_tax_rate::pretax_income_not_positive` (genuine
business losses) and **`cash_and_equivalents::row_not_found` (MU, 4
years, 20 total)** — selected as the next issue: not the largest, but
the safest and most bounded (single ticker, no policy ambiguity).

## D-023 — MU `cash_and_equivalents::row_not_found` fix — PASS (bounded)
**Root cause confirmed** (MU 2024-08-29 vs. MU 2025-08-28 control):
MU 2024's balance-sheet cash row is labeled **"Cash and equivalents"**
(missing the second "cash"); MU 2025's is "Cash and cash equivalents".
Same concept (`us-gaap:CashAndCashEquivalentsAtCarryingValue`), same
role, same structural position — pure label-text pattern gap, not a
data or structural issue (role matching, concept existence, position
all already correct in both years).

**Warehouse extended to all 5 MU filings** (`scripts\81_build_xbrl_
warehouse_mu_cash.py`, generalizes 78/75/73) — all 5 years loaded, each
parsed with Arelle exactly once (proof stage: 2024+2025 only; widened
to 2021-2023 after the proof passed).

**New script `scripts\82_mu_cash_label_fix.py`** (extends 79, preserves
72/77/79/80 unchanged): broadened `cash_and_equivalents`'s pattern from
requiring the literal "cash and **cash** equivalents" to also accept
"cash and equivalents" (second "cash" optional) — still fully anchored,
so longer labels like "Restricted cash and cash equivalents" still
correctly fall through to REVIEW_REQUIRED. No ticker-specific tag,
no year-specific rule, no assumed-zero fallback.

**Result: 12 REVIEW_REQUIRED → PASS** (`cash_and_equivalents`,
`adjusted_net_debt`, `invested_capital` × 4 affected years:
MU 2021-09-02, 2022-09-01, 2023-08-31, 2024-08-29). MU 2025-08-28
(control) confirmed byte-identical on all 3 metrics — no new
extraction_run even needed for it.

**`average_invested_capital`/`roic` deliberately NOT written** —
recalculating them exposed a SEPARATE, pre-existing bug: the warehouse
reconstruction's prior-period lookup doesn't yet apply
`PRIOR_PERIOD_DATE_TOLERANCE_DAYS` for MU's 52/53-week fiscal calendar,
discovered because even the MU 2025 control (already PASS, ROIC
0.1586) could not be reproduced through this same code path. Writing
over an existing PASS with a false REVIEW_REQUIRED would have been a
real regression, not a general evidence improvement — correctly left
untouched for all 5 MU years. Flagged as a new, separate, not-yet-
started root cause (likely also affects NVDA/CRWD, reconstructed the
same way).

**REVIEW_REQUIRED count: 173 → 161** (12 converted, 0 new). Regression:
MU 2025 control confirmed unchanged (verified directly against the
database — only the pre-existing v14 row exists for it); the other 8
of 9 anchors untouched by construction. **Zero regressions.**

Runtime ~8.5 minutes (within the 8-15 minute expected window). Full
evidence, proof table, and the date-tolerance finding in
`docs\LAST_CLAUDE_REPORT.md`.

## D-024 — Prior-period date-tolerance fix (warehouse reconstruction) — PASS
**Root cause confirmed via control-year proof (MU 2025-08-28):** the
warehouse-based prior-period lookup required an EXACT date match, with
no tolerance — unlike the live engine, which has always applied
`PRIOR_PERIOD_DATE_TOLERANCE_DAYS = 10` for 52/53-week fiscal
calendars. MU's actual prior fiscal-year-end date is 1-2 days off the
naive "-1 year, same month/day" guess every year (e.g. 2024-08-29 →
naive prior guess 2023-08-29, actual 2023-08-31). Control test:
requested 2024-08-28, matched 2024-08-29 (diff 1 day, within
tolerance) — `average_invested_capital`/`roic` reproduced the existing
verified PASS values exactly (0.15860246567294037 ROIC) before
proceeding.

**New script `scripts\84_prior_period_date_tolerance_fix.py`** (extends
82; every prior script preserved unchanged, warehouse untouched — no
Arelle, no reload, DuckDB-only throughout): `match_facts_from_warehouse`
gained a `date_tolerance_days` parameter (0 = exact, unchanged for the
current period); prior-period lookups now use 10, with an explicit
reject-if-more-than-one-candidate-date check (never triggered on this
data — every year resolved to exactly one date within tolerance). No
ticker- or year-specific logic; no context/unit/dimension validation
weakened.

**Result: 7 of 8 possible conversions** — MU 2021/2022/2024
`average_invested_capital`+`roic`, MU 2023 `average_invested_capital`
only. **MU 2023's `roic` correctly remains REVIEW_REQUIRED** — a
genuine operating loss (-$5,745,000,000) blocks `effective_tax_rate`/
`nopat` via D-015's existing rules, entirely independent of date
matching; not force-converted.

**REVIEW_REQUIRED count: 161 → 154** (7 converted, 0 new). MU 2025
control confirmed unchanged (only the pre-existing v14 row exists for
it — no new row even needed). Other 8 of 9 anchors untouched by
construction. **Zero regressions.**

Runtime ~10 minutes (within the 3-15 minute expected window — most
spent wiring the fix through the reconstruction call chain; actual
DuckDB computation under 2 seconds). Full evidence, date-difference
table, and control proof in `docs\LAST_CLAUDE_REPORT.md`.

## Read-only rerank (post-D-024) — 154 confirmed
Latest-run-per-filing query confirmed **154 REVIEW_REQUIRED** (63
primary, 91 downstream). Confirmed the D-024 date-tolerance fix does
**not** apply to NVDA/CRWD — both still run entirely on the live
engine (already tolerance-aware); their blocker is the unrelated
`current_debt::zero_inference_role_not_found` (no debt-maturity
schedule role found at all — likely genuine missing data for CRWD,
which relies on revolving credit only). Top 5: `current_debt::
zero_inference_role_not_found` (48, CRWD/META/NVDA), `long_term_debt::
ancestry_confirmed_absent` (30, META/PANW), `effective_tax_rate::
pretax_income_not_positive` (19, genuine losses), `current_debt::
zero_inference_total_mismatch_with_ltd` (18, META),
`current_debt_prior::zero_inference_role_not_found` (16). Recommended
next: investigate (not yet fix) `cash_and_equivalents::row_ambiguous`
(PANW, 15) — most bounded remaining cause.

## D-025 — PANW `cash_and_equivalents::row_ambiguous` fix — PASS
**Root cause confirmed (PANW 2023 vs. 2024 control):** both "candidate"
rows are the SAME concept and SAME value — never a genuine two-number
ambiguity. The second candidate is PANW's standard ASC 230 "Reconciliation
of Cash, Cash Equivalents, and Restricted Cash to the Consolidated
Balance Sheets" table (required of any filer holding restricted cash,
not PANW-specific) — its own title happens to contain "balance sheets,"
falsely matching the balance-sheet role pattern. Confirmed via sibling
rows: the reconciliation role has "Restricted cash" as a direct
sibling; the true balance-sheet role does not. PANW 2024's equivalent
role is titled without "balance sheets" wording, so it was never a
false candidate there.

**New script `scripts\87_panw_cash_reconciliation_role_fix.py`**
(extends 84; every prior script preserved unchanged, warehouse-only via
`scripts\86`, no live-engine rerun): `cash_and_equivalents`'s
`role_exclude_pattern` broadened from `"parenthetical"` to
`"parenthetical|reconciliation"` — general, since a true balance-sheet
role never itself contains "reconciliation" in its title. No
ticker-specific tag, no year-specific rule, no validation weakened.

**Result: 9 of 15 possible conversions** — PANW 2021/2022
`cash_and_equivalents`+`adjusted_net_debt`+`invested_capital`+
`average_invested_capital` (2021's/2022's `roic` correctly still
blocked by the already-known `pretax_income_not_positive`); PANW 2023
`cash_and_equivalents` only (its other 4 metrics correctly still
blocked by the already-known `long_term_debt::ancestry_confirmed_absent`
finding for PANW 2023-2025, unrelated to cash).

**REVIEW_REQUIRED count: 154 → 145** (9 converted, 0 new). PANW 2024
control confirmed unchanged (only the pre-existing v14 row exists for
it). Other 8 of 9 anchors untouched by construction. **Zero
regressions.**

Runtime ~9 minutes (within the 6-12 minute expected window). Full
candidate-evidence comparison and control proof in
`docs\LAST_CLAUDE_REPORT.md`.

## D-026 — PANW zero-long-term-debt policy applied (2023, 2024) — PASS
**Applied the previously-approved debt-maturity policy directly** (no
new investigation phase, per explicit instruction) — item 6: "if the
filing proves no financial debt remains outstanding, record zero with
explicit supporting lineage." Root cause for PANW's prior
`long_term_debt::ancestry_confirmed_absent` REVIEW_REQUIRED (2023, 2024):
PANW's sole debt instrument (`us-gaap:ConvertibleDebtCurrent`) is
classified entirely as CURRENT by the existing D-019 ancestry resolver
in both years — genuinely zero long-term debt, not a gap.

**New script `scripts\89_panw_zero_long_term_debt_policy.py`** (copied
from 87; every prior script preserved unchanged; warehouse-only, no
Arelle, no reload): adds a "prove zero" tier to `long_term_debt`,
firing only when BOTH the D-019 ancestry search AND the broader
unclaimed-debt-vocabulary search (same pattern `current_debt` itself
uses, across every balance-sheet role) independently return zero
candidates. Any unclaimed candidate would have kept the result
REVIEW_REQUIRED rather than guessing. New DB-writer
`scripts\90_load_d026_panw_zero_long_term_debt_extraction_runs.py`
follows the established idempotent, non-overwriting pattern.

**Result: 11 of 12 possible conversions** — PANW 2023 all 6 metrics
(`long_term_debt` $0 → `total_debt` $1,991,500,000 →
`adjusted_net_debt`/`invested_capital`/`average_invested_capital`/`roic`
37.54%); PANW 2024 5 of 6 (`roic` correctly still REVIEW_REQUIRED,
blocked by the independent, already-known
`effective_tax_rate::outside_plausible_range` finding — a tax-benefit
year, unrelated to debt). `current_debt`/`cash_and_equivalents`
independently re-verified unchanged in both years.

**PANW 2025-07-31 explicitly NOT processed** — not present in the XBRL
warehouse, and this task's implementation rules forbade loading Arelle
or reparsing any filing. Disclosed as a scope deviation rather than
silently dropped or silently violating the no-Arelle constraint; its 6
debt-dependent metrics remain REVIEW_REQUIRED, unchanged.

**REVIEW_REQUIRED count: 145 → 134** (11 converted, 0 new). All 9
latest-year anchors independently re-verified unchanged (full
20-metric status pulled per anchor); database writes confirmed scoped
to only the 2 processed PANW accessions (12 rows total). **Zero
regressions.**

Runtime: script 89 computation 0.78s; script 90 write sub-second; total
active task well within the 2-10 minute window. Full evidence, values,
and recommended next step in `docs\LAST_CLAUDE_REPORT.md`.

## D-027 — Consolidated cleanup: 4 new approved policies, all 5 groups — PASS
**Approved policies (supersede prior blocking rules where noted):**
- **Policy A (debt maturity, extends D-021/D-022):** current_debt = principal due ≤12mo, long_term_debt = principal due >12mo, leases excluded, carrying value preferred, `PASS_MATURITY_BASIS` fallback unchanged. **New item 7:** average_invested_capital may use the PREVIOUS FISCAL YEAR'S OWN locked filing's latest-approved invested_capital directly — does not require prior-period comparative facts to appear inside the current filing (this was the structural blocker in D-022 for AMZN/GOOGL's average_invested_capital/roic).
- **Policy B (undrawn revolving credit, NEW):** an explicit zero-balance revolver fact (`us-gaap:LineOfCredit`=0 at the exact report date, dimensioned by a "Revolving" credit-facility member) proves zero current_debt for that facility; if no other debt-vocabulary candidate exists anywhere, both current_debt and long_term_debt are zero. Credit availability/commitment size is never treated as debt.
- **Policy C (direct aggregate, NEW):** a filing's own reported debt-maturity-schedule "Total" row is authoritative for total_debt (`PASS_DIRECT_AGGREGATE`) even when it doesn't reconcile exactly with the balance-sheet long_term_debt carrying value (face value vs. carrying value net of unamortized issuance costs) — gap preserved in lineage, never blocks total_debt/adjusted_net_debt/invested_capital/roic. current_debt/long_term_debt individually may remain separately classified.
- **Policy D (normalized tax, NEW, supersedes the prior ORCL FY2021 exception):** when pretax_income≤0 or reported effective_tax_rate is outside [0,1] but pretax/tax/operating-income facts are all valid, use a fixed 21% rate: NOPAT = operating_income × 0.79, status `PASS_NORMALIZED_TAX`. Negative operating_income may produce negative NOPAT/ROIC — never blocked on that basis alone.

**New scripts** (91-95, each new/versioned, all prior scripts preserved unchanged): `scripts\91_build_xbrl_warehouse_groups_1_3_4.py` (Arelle warehouse load, 11 filings: PANW 2025; CRWD ×5; META 2020-2023; NVDA 2020 — zero further Arelle calls needed anywhere else in this task, since AMZN/GOOGL/META-2024/NVDA-2024 were already warehoused from D-022); `scripts\92_groups_1_3_4_debt_facility_aggregate_policy.py` (Policies A+B+C, warehouse-only); `scripts\93_average_invested_capital_prior_filing_lookup.py` (Policy A item 7, production-DB-only, ticker-agnostic across all 45 company-years); `scripts\94_normalized_tax_policy.py` (Policy D, production-DB-only); `scripts\95_final_roic_combination_pass.py` (final ROIC combine, one company-year at a time with immediate write-verification).

**Results by group:**
- **Group 1 (PANW 2025):** current_debt/long_term_debt both proven zero (convertible notes fully settled) — **fully resolved, 0 REVIEW_REQUIRED** for this filing.
- **Group 2 (AMZN+GOOGL, 10 company-years):** current_debt/total_debt/invested_capital already resolved since D-022; average_invested_capital/roic now resolved for 8 of 10 (2021 for both tickers has no prior locked filing in the dataset — genuinely unresolvable, not a bug).
- **Group 3 (CRWD, 5 filings):** current_debt=0 via Policy B (undrawn revolver) for all 5 years; long_term_debt was already resolving fine (real term-loan-like balance). 2022/2026 remain blocked on adjusted_net_debt/invested_capital/roic by an independent, pre-existing `short_term_investments::REVIEW_REQUIRED` — unrelated to this cleanup.
- **Group 4 (META ×5 + NVDA 2020):** META 2020/2021 proven zero debt entirely; META 2022/2023/2024 resolved via Policy C (`PASS_DIRECT_AGGREGATE`, ~$174M-$500M face-vs-carrying gap preserved in lineage, never blocking). NVDA 2020 remains genuinely unresolvable (no maturity schedule, revolver fact numerically unparseable at source, real long_term_debt exists so "no debt" can't be proven either).
- **Group 5 (normalized tax, 11 company-years):** ORCL 2021, AMZN 2022, CRWD 2022/2023/2025/2026, MU 2023, NVDA 2023, PANW 2021/2022/2024 all converted to `PASS_NORMALIZED_TAX`.

**REVIEW_REQUIRED count: 134 → 37** (97 net conversions across current_debt/long_term_debt/total_debt/adjusted_net_debt/invested_capital/average_invested_capital/effective_tax_rate/nopat/roic). Status breakdown (20 primary metrics × 45 company-years): PASS 773, PASS_MATURITY_BASIS 45, PASS_DIRECT_AGGREGATE 15, PASS_NORMALIZED_TAX 30, REVIEW_REQUIRED 37.

**Zero regressions** — all 9 anchors re-verified: ORCL/MSFT/NVDA/MU/PANW at 0 REVIEW_REQUIRED (PANW's anchor fully resolved this run); META/GOOGL/AMZN each at 1 (current_debt only, an explicit, deliberate D-021 policy boundary, not a gap); CRWD 2026 at 5 (short_term_investments, independent pre-existing cause).

Runtime: warehouse load (script 91) 44.7s in the prior turn; all 4 computation/write scripts combined under 4 seconds. Full per-group breakdown, remaining unresolved list, and recovery notes (this task was interrupted once mid-run with zero unsafe state — verified via zero orphaned extraction_runs/zero duplicate keys before resuming) in `docs\LAST_CLAUDE_REPORT.md`.

**Remaining genuinely unresolved (not fixable by any of the 4 policies):** NVDA 2020 (missing/ambiguous source data); CRWD 2022/2023/2026 (independent `short_term_investments` gap — recommended next step); AMZN/GOOGL 2021 + META 2020 average_invested_capital (permanent dataset-boundary limit, no prior filing exists); 13 company-years' current_debt under the (then-still-standing) maturity-basis policy boundary — **closed in D-028 below.**

## D-028 — current_debt maturity-basis policy for AMZN/GOOGL/META — PASS
**Lifts the D-021 current_debt bar, specifically and only for current_debt** (D-021 rule 3 barred any principal-only maturity value from `total_debt`/`invested_capital`/`average_invested_capital`/`roic`; `current_debt` itself was covered by the same overall intent — this decision explicitly approves `current_debt = PASS_MATURITY_BASIS` using the exact same status/basis already used for `total_debt` since D-022). Applied to the 13 `current_debt::REVIEW_REQUIRED` cases identified in D-027: AMZN ×5, GOOGL ×5, META 2022/2023/2024. CRWD and NVDA explicitly excluded (out of scope).

**New script `scripts\96_current_debt_maturity_basis_policy.py`** (warehouse-only — all 13 filings already warehoused from D-022/D-027, zero Arelle calls; every prior script preserved unchanged). For each filing, sums the earliest ("due within 12 months") non-abstract, non-total, non-lease bucket(s) of the filing's own debt-maturity schedule. AMZN/GOOGL: real nonzero current-year maturities ($187M-$8.5B range). META 2022/2023/2024: $0 (schedule's earliest bucket spans multiple years with zero due, consistent with META's debt being 100% long-term with no current portion).

**Result: 13 of 13 converted**, `status=PASS_MATURITY_BASIS`, `basis=MATURITY_PRINCIPAL`, full bucket/role/report-date lineage preserved per row. **Zero downstream metrics required recalculation** — `total_debt`/`invested_capital`/`average_invested_capital`/`roic` for all 13 company-years were already resolved independently via the existing maturity-basis (AMZN/GOOGL) or direct-aggregate (META) fallback, neither of which depends on `current_debt`'s own status; verified byte-identical before/after.

**REVIEW_REQUIRED count: 37 → 24** (13 converted, 0 new). **Zero regressions** — all 9 anchors re-verified; META/GOOGL/AMZN's anchor gap (the one remaining `current_debt` REVIEW_REQUIRED each) is now closed (0 REVIEW_REQUIRED for all 3), since those anchors were themselves inside this task's scope; CRWD 2026 unchanged at 5 (independent, out-of-scope `short_term_investments` gap). Zero orphaned extraction_runs, zero duplicate natural keys, verified before and after.

Runtime: 3.68 seconds (warehouse read + production-DB write for 13 filings, no Arelle). Full per-filing table, maturity-bucket lineage, and regression evidence in `docs\LAST_CLAUDE_REPORT.md`.

**Remaining genuinely unresolved (24 total):** AMZN 2021-12-31 + GOOGL 2021-12-31 average_invested_capital/roic (each company's first year in the dataset — no prior locked filing exists to average against; **recommended next step: lock + warehouse one additional FY2020 10-K per company**); CRWD 2022/2023/2026 (independent `short_term_investments` gap, out of scope for D-027/D-028); NVDA 2020/2023 (pre-existing, unrelated gaps, out of scope).

## D-029 — Prior-fiscal-year gap closure (AMZN, GOOGL, META) — PASS
**Closes the recommended next step from D-028.** Locked 3 new 10-K filings from SEC EDGAR (form=10-K, exact reportDate/accessionNumber/filingDate/primaryDocument, via unmodified `scripts\36b_download_accession_locked_filing.py`): AMZN FY2020 (accession 0001018724-21-000004), GOOGL FY2020 (accession 0001652044-21-000010), META FY2019 (accession 0001326801-20-000013). Saved to the existing `data\sec_filings_locked` location; 3 new `sec_filings` rows added.

**New script `scripts\97_build_xbrl_warehouse_prior_year_gap.py`** (Arelle warehouse loader, copied-unchanged extraction logic from scripts/73/75/78/86/91) — all 3 filings parsed exactly once, warehoused in 6.16s total. **New script `scripts\98_prior_year_gap_invested_capital.py`** computes current_debt/long_term_debt/total_debt/cash/short_term_investments/stockholders_equity/adjusted_net_debt/invested_capital for these 3 prior filings by directly reusing `scripts\92`'s already-approved policy engine (module reuse, not duplicated) — treating each prior fiscal year as a genuine new company-year in the historical dataset, not a special "_prior" metric.

**Prior invested_capital resolved:** AMZN FY2020 $42,182,000,000 (`PASS_DIRECT_AGGREGATE`); GOOGL FY2020 $101,169,000,000 (`PASS_DIRECT_AGGREGATE`); META FY2019 $46,199,000,000 (`PASS`, zero debt proven). Rerunning the existing, unchanged `scripts\93` (average_invested_capital prior-filing lookup) and `scripts\95` (final roic combination) — zero new code — automatically picked up these values.

**Result: 6 of 6 expected conversions** — average_invested_capital + roic for AMZN 2021-12-31 (67.47%→$67,465,500,000 avg IC, ROIC 32.25%), GOOGL 2021-12-31 ($114,297,500,000 avg IC, ROIC 57.71%), META 2020-12-31 ($56,267,500,000 avg IC, ROIC 51.00%).

**REVIEW_REQUIRED count: 24 → 18** (6 converted, 0 new). **Zero regressions** — all 9 anchors and the 3 targeted company-years re-verified; no previously-passing value changed. Zero orphaned extraction_runs, zero duplicate natural keys.

Runtime: warehouse load 6.16s + invested-capital computation 1.2s + average/roic reruns ~1.3s ≈ 9 seconds total. Full per-filing values and lineage in `docs\LAST_CLAUDE_REPORT.md`.

**Remaining genuinely unresolved (18 total, all pre-existing/out-of-scope):** CRWD 2022/2023/2026 (independent `short_term_investments` gap — recommended next step); NVDA 2020/2023 (pre-existing, unrelated gaps).

## D-030 — CRWD short_term_investments zero-proof — PASS
**Closes the recommended next step from D-029.** Root cause (compared directly against CRWD's own already-PASS years 2023/2024/2025): the filer's own `us-gaap:AssetsCurrent` (Total current assets) calculation-linkbase explicitly lists `us-gaap:ShortTermInvestments` as a child in those years (even when its value is $0), but 2022 and 2026's calculations have no such child at all — a presentation choice, not a value difference.

**New script `scripts\99_short_term_investments_zero_proof.py`** (DuckDB-only, zero Arelle, all 3 CRWD filings already warehoused): general, ticker-agnostic proof — if `AssetsCurrent`'s calculation children contain no short-term-investment-vocabulary concept, AND every child independently resolves to PASS with their sum reconciling EXACTLY (zero gap) to the reported `AssetsCurrent` total, then no short-term-investment asset class existed that period: `status=PASS`, `value=0.0`, `basis=ZERO_PROVEN_STRUCTURAL_ABSENCE` (same basis name already used for debt in D-026/D-027). Confirmed for both years: CRWD 2022 (4 children sum to $2,570,952,000, matching exactly) and CRWD 2026 (4 children sum to $7,419,119,000, matching exactly).

**Result:** `short_term_investments` → PASS ($0) for CRWD 2022 and 2026. Downstream `adjusted_net_debt`/`invested_capital` recalculated directly (CRWD 2022: -$1,257,116,000 / -$219,473,000; CRWD 2026: -$4,484,654,000 / -$12,049,000). Rerunning the existing, unchanged `scripts\93`/`scripts\95` (zero new code) then resolved `average_invested_capital`/`roic` for CRWD 2023 (-$348,201,500 / 43.13%, `PASS_NORMALIZED_TAX`) and CRWD 2026 (-$136,222,000 / 170.09%, `PASS_NORMALIZED_TAX`).

**REVIEW_REQUIRED count: 18 → 8** (10 converted, 0 new). **Zero regressions** — all 9 anchors now show 0 REVIEW_REQUIRED, including CRWD's own anchor (2026-01-31) for the first time in this project. Zero orphaned extraction_runs, zero duplicate natural keys.

Runtime: under 2 seconds total (short_term_investments zero-proof + downstream recalculation + scripts 93/95 reruns). Full lineage and reconciliation detail in `docs\LAST_CLAUDE_REPORT.md`.

**Remaining genuinely unresolved (8 total):** CRWD 2022-01-31 average_invested_capital/roic — CRWD's first year in the dataset, no prior locked filing exists (same permanent, out-of-scope boundary already documented for AMZN/GOOGL 2021 and META 2020 in D-028/D-029; **recommended next step: lock + warehouse one additional CRWD FY2021 10-K**, closing the last of the four "first fiscal year" gaps).

## D-031 — CRWD first-year gap — genuine data-quality blocker found (FAIL on objective / PASS on methodology)
**Attempted to close the last "first fiscal year in dataset" gap** (the recommended next step from D-030): locked + warehoused CRWD FY2021 (2021-01-31, accession 0001535527-21-000007) via `scripts\100_build_xbrl_warehouse_crwd_first_year_gap.py` (1.96s, PASS) and computed its metrics via `scripts\101_crwd_first_year_gap_invested_capital.py` (reusing scripts/92's unchanged policy engine, same pattern as D-029).

**Result: current_debt/total_debt/adjusted_net_debt/invested_capital could NOT be resolved for CRWD FY2021 — a genuine, verified data-quality limitation, not a bug or policy gap.** CRWD's revolving-facility "amount outstanding" fact at 2021-01-31 (the same concept/evidence pattern that proved the facility undrawn for CRWD 2022-2026 under D-030's Policy B) has `value_raw = "(ixTransformValueError)"` — **Arelle itself cannot resolve this fact's numeric value** from its declared Inline XBRL transform in this specific filing. Since real long-term debt also exists ($738,029,000, `LongTermDebtNoncurrent`), the alternate "no debt exists anywhere" zero-proof (used for META 2020/2021) doesn't apply either, and no debt-maturity-schedule role exists for Policy C's aggregate fallback. No value was guessed; the result correctly remains `REVIEW_REQUIRED`, fully explained.

**Consequence:** CRWD 2022's `average_invested_capital`/`roic` remain `REVIEW_REQUIRED` — 0 of the 2 expected conversions achieved. **REVIEW_REQUIRED count: 8 → 8** (unchanged). **Zero regressions** — all 9 anchors and CRWD 2022's own previously-passing metrics confirmed byte-identical.

Full root-cause detail, raw fact evidence, and the open decision point (whether to manually read the corrupted fact's raw HTML value, or accept this as a permanent documented boundary) in `docs\LAST_CLAUDE_REPORT.md`.

**Remaining genuinely unresolved (8 total, unchanged from D-030):** CRWD 2022-01-31 average_invested_capital/roic — now confirmed blocked by a genuine source-data parsing limitation in CRWD's FY2021 10-K (not merely "no prior filing exists," which was the original hypothesis — that part is now closed, but the filing itself doesn't yield a usable answer). **Decision needed from the user** before any further attempt: accept this as permanent, or approve a manual-read exception for `ixTransformValueError`-corrupted facts. **Resolved in D-032 below.**

## D-032 — CRWD FY2021 manual HTML fact recovery — PASS
**Closes the decision point raised in D-031** — the user approved a one-time manual read of the original locked SEC HTML for the exact Arelle-unparseable fact identified there. New script `scripts\102_crwd_fy2021_manual_html_parse.py`.

**The fact:** `us-gaap:LineOfCredit` (revolver outstanding balance), context `i36456875e4304ffba24e889b8aca8952_I20210131`, dimensioned `CreditFacilityAxis`=`RevolvingCreditFacilityMember`, instant 2021-01-31, in `data\sec_filings_locked\CRWD\000153552721000007\crwd-20210131.htm`. The `<ix:nonFraction>` element's visible text is `"No"`, tagged with `format="ixt-sec:numwordsen"` (the SEC's own standard number-words transform, built to map words like "no" to 0) — Arelle recorded `value_raw="(ixTransformValueError)"` for this exact fact (a documented Arelle transform-implementation gap, not a filing ambiguity). The surrounding sentence — "No amounts were outstanding under the A&R Credit Agreement as of January 31, 2021." — independently confirms the same deterministic value. Final parsed value: **0.0**. Not an external source, not an estimate.

**Result:** CRWD FY2021's `current_debt`→PASS ($0, `SEC_HTML_MANUAL_PARSE`), `total_debt`→PASS ($738,029,000), `adjusted_net_debt`→PASS (-$1,180,579,000), `invested_capital`→PASS (-$308,705,000). Rerunning the existing, unchanged `scripts\93`/`scripts\95` then resolved CRWD 2022's `average_invested_capital`→PASS (-$264,089,000) and `roic`→`PASS_NORMALIZED_TAX` (42.64%).

**REVIEW_REQUIRED count: 8 → 6** (2 converted, exactly as expected, 0 new). **Zero regressions** — all 9 anchors and every previously-passing CRWD metric confirmed byte-identical. Zero orphaned extraction_runs, zero duplicate natural keys.

This closes the last of the four "first fiscal year in dataset" gaps (AMZN/GOOGL 2021, META 2020, CRWD 2022 — D-029 through D-032) — all now fully resolved wherever a prior filing was locked and warehoused. Full HTML element, lineage, and evidence in `docs\LAST_CLAUDE_REPORT.md`.

**Remaining genuinely unresolved (6 total):** all pre-existing, out-of-scope, or genuine data limitations documented across D-027 through D-031 (NVDA 2020/2023 gaps) — none newly created, none touched by this task. **Confirmed and mostly closed in D-033 below.**

## D-033 — NVDA FY2020 manual HTML fact recovery — 4 of 6 resolved
**Confirmed all 6 remaining REVIEW_REQUIRED results belong to one company-year: NVDA 2020-01-26** (current_debt, total_debt, adjusted_net_debt, invested_capital, average_invested_capital, roic). Root cause for current_debt: the same class of Arelle transform failure as D-032 — NVDA's revolver outstanding-balance fact (`us-gaap:LineOfCredit`, context `FI2020Q4_..._RevolvingCreditFacilityMember`) has `value_raw="(ixTransformValueError)"`. New script `scripts\103_nvda_fy2020_manual_html_parse.py` applies the D-032 rule (extended, per explicit instruction, to any deterministic Arelle parsing failure): the `<ix:nonFraction>` element's visible text `"no"` (format `ixt-sec:numwordsen`) plus the surrounding sentence "...we had not borrowed any amounts under this agreement" both independently confirm **0.0** — manually read from `data\sec_filings_locked\NVDA\000104581020000010\nvda-2020x10k.htm`, not estimated, not external.

**Result:** current_debt→PASS ($0, `SEC_HTML_MANUAL_PARSE`), total_debt→PASS ($1,991,000,000), adjusted_net_debt→PASS (-$8,906,000,000), invested_capital→PASS ($3,298,000,000). **average_invested_capital/roic remain REVIEW_REQUIRED** — NVDA 2020 is NVDA's own first fiscal year in the dataset (no NVDA FY2019 locked), the same permanent boundary already closed for AMZN/GOOGL/META/CRWD in D-029/D-031 — closing it needs a new, explicitly-authorized task to lock NVDA FY2019 (not done here, not authorized by this task's scope).

**REVIEW_REQUIRED count: 6 → 2** (4 converted, 0 new). **Zero regressions** — all 9 anchors and all previously-passing NVDA metrics (2021-2024, all still 0 REVIEW_REQUIRED) confirmed unchanged. Zero orphaned extraction_runs, zero duplicate natural keys.

**Only 2 REVIEW_REQUIRED results remain in the entire 45-company-year dataset** (NVDA 2020's average_invested_capital/roic). Full lineage and HTML element in `docs\LAST_CLAUDE_REPORT.md`. **Closed in D-034 below.**

## D-034 — NVDA FY2019 gap closure — FINAL: 0 REVIEW_REQUIRED across the entire 45-company-year dataset — PASS
**Closes the last remaining gap.** Locked NVDA FY2019 (report date 2019-01-27, accession 0001045810-19-000023, filed 2019-02-21, primary document `nvda-2019x10k.htm`) via `scripts\36b` (unmodified).

**Discovered and corrected before use:** NVDA's FY2019 10-K predates NVDA's Inline XBRL adoption — the SEC-designated primary HTML document carries no embedded `ix:` facts at all (first load attempt via `scripts\104` correctly surfaced this as 0 facts/0 contexts, not a silent failure). The filing instead ships a separate, traditional XBRL instance document, `nvda-20190127.xml`, in the SAME already-locked accession — `scripts\104` was corrected to use this file as Arelle's entry point for this one accession, then reloaded successfully (1,211 facts, 267 contexts, 7 units, `status=PASS`). The empty first attempt was deleted before any computation used it.

**New script `scripts\105_nvda_fy2019_gap_invested_capital.py`** (reuses scripts/92's unchanged policy engine, same pattern as D-029/D-031): NVDA FY2019 `invested_capital` = **$3,908,000,000** (`PASS`, `GAAP_CARRYING_VALUE` — resolved cleanly, no manual HTML parse needed for this filing). Rerunning the existing, unchanged `scripts\93`/`scripts\95` then resolved NVDA FY2020's `average_invested_capital` = **$3,603,000,000** and `roic` = **74.36%** (both `PASS`).

**REVIEW_REQUIRED count: 2 → 0.** **Confirmed: all 900 primary-metric results (20 metrics × 45 company-years) are now resolved** — status breakdown PASS 790, PASS_MATURITY_BASIS 58, PASS_NORMALIZED_TAX 33, PASS_DIRECT_AGGREGATE 19 (790+58+33+19=900). **Zero regressions** across all 45 company-years and all 9 anchors, verified directly. Zero orphaned extraction_runs, zero duplicate natural keys.

**This closes the entire D-027 consolidated-cleanup effort** (starting count 134 → 0) and all five "first fiscal year in dataset" gaps (AMZN/GOOGL 2021 — D-029; META 2020 — D-029; CRWD 2022 — D-031/D-032; NVDA 2020 — D-033/D-034), across every approved policy: debt maturity classification (D-027/D-028), undrawn-facility zero-proof (D-027/D-030), direct-reported-aggregate (D-027), normalized 21% tax (D-027), and manual SEC-HTML fact recovery for individually-verified Arelle transform failures (D-032/D-033). Full detail in `docs\LAST_CLAUDE_REPORT.md`.

**Dataset status: fully resolved, 0 REVIEW_REQUIRED.** Next phase (scoring/backtesting, quarterly extraction, market-price ingestion) requires separate, explicit user authorization per this project's standing rules — none of that has been started.

## Annual V1 freeze + MSFT FY2024 quarterly proof — PASS
**Part 1 — Annual V1 frozen.** With the annual dataset fully resolved (D-034: 900/900, 0 REVIEW_REQUIRED), created a read-only snapshot: `data\database\ai_stock_agent_annual_v1.duckdb` (byte-for-byte copy, SHA-256 `e655671e...58e9f814`) + `data\database\ai_stock_agent_annual_v1_manifest.json` (creation timestamp, source/snapshot paths, checksum, 45 company-years, 900 results, full status breakdown, REVIEW_REQUIRED=0, the 5 supplementary prior-year filings explicitly excluded from the 45-count, latest milestone D-034). Snapshot independently re-opened and re-verified to match the source exactly. New script `scripts\106_freeze_annual_v1_snapshot.py`.

**Part 2 — MSFT FY2024 quarterly proof (bounded, single company, NOT the production quarterly schema).** Locked MSFT's 3 FY2024 10-Qs from SEC EDGAR (Q1 2023-09-30 filed 2023-10-24, accession 0000950170-23-054855; Q2 2023-12-31 filed 2024-01-30, accession 0000950170-24-008814; Q3 2024-03-31 filed 2024-04-25, accession 0000950170-24-048288) via new `scripts\107_download_accession_locked_filing_any_form.py` (generalizes `scripts\36b` with an explicit `--form` argument). Warehoused all 3 via new `scripts\108_build_xbrl_warehouse_msft_10q_fy2024.py` (copied-unchanged extraction logic). Extracted revenue/operating_income/pretax_income/income_tax_expense/operating_cash_flow/capex via new `scripts\109_msft_fy2024_quarterly_proof.py`.

**Result: 24/24 quarterly results (6 metrics × 4 quarters), all 6 annual reconciliations PASS (zero difference).** All Q1-Q3 values resolved via `DIRECT_QUARTER` (MSFT tags discrete 3-month durations for every metric, including both cash-flow items) — the `DERIVED_FROM_YTD` fallback was independently computed as a cross-check for every metric/quarter and matched exactly in all 12 cases. Q4 = 10-K annual − Q3 9-month YTD for all 6 metrics, `basis=DERIVED_Q4_FROM_10K_MINUS_9M`, availability date = 10-K filing date (2024-07-30) — no look-ahead, no amended/later-year facts read anywhere.

**Confirmed zero impact on the annual dataset:** Annual V1 checksum re-verified unchanged; MSFT FY2024's 6 annual production values byte-identical; zero new `financial_metric_results` rows for the 3 new 10-Q accessions (no production quarterly schema created, per instruction); only the pre-existing `v14` engine_version exists for MSFT's 10-K accession.

Outputs: `data\quarterly_proof_msft_fy2024.json`, `data\quarterly_proof_msft_fy2024.csv`. Full detail in `docs\LAST_CLAUDE_REPORT.md`.

**Recommendation:** the quarterly method is ready to generalize, with one caveat — MSFT happens to tag discrete quarters for cash-flow items too (many filers don't). Recommend one more single-company proof on a filer known to omit discrete-quarter cash-flow tags (forcing real use of `DERIVED_FROM_YTD`, not just cross-check) before committing to a general quarterly system.

## AMZN FY2024 quarterly proof — in progress (warehouse load complete, extraction not yet run)
**Locked** AMZN's 3 FY2024 10-Qs (Q1 2024-03-31 filed 2024-05-01, accession 0001018724-24-000083; Q2 2024-06-30 filed 2024-08-02, accession 0001018724-24-000130; Q3 2024-09-30 filed 2024-11-01, accession 0001018724-24-000161) via `scripts\107` (two transient SEC 503s on non-XBRL exhibit/rendering files, both resolved by sequential retry).

**Warehoused** all 3 via new `scripts\110_build_xbrl_warehouse_amzn_10q_fy2024.py` — copied from the verified `scripts\108` (MSFT loader) with only: ticker/dates changed, plus one added bounded control-flow change (parent process launches each filing load as a separate child process via `subprocess.run(..., timeout=300)`, worker mode via `--single-report-date`). **This was the second attempt** — the first attempt (same task) correctly stopped and reported FAIL rather than redesign, because the initially-requested "minimal rename-only copy" of scripts/108 had no timeout mechanism at all to satisfy the real-300s-timeout requirement; the user then explicitly authorized the subprocess wrapper, which was added as the one bounded change.

**Result: all 3 filings PASS**, verified directly in DuckDB (`warehouse_runs` row + status=PASS + fact/context/unit counts > 0 for all 3): Q1 884 facts/181 contexts/7 units; Q2 1,165/244/7; Q3 1,139/238/7. Zero timeouts, zero failures — every load completed in 1-3 seconds, far under the 300-second cap. Total runtime 7.05s.

**Confirmed zero side effects**: Annual V1 checksum unchanged; zero `financial_metric_results` rows for the 3 new accessions (no production quarterly schema touched); AMZN FY2024's 6 annual production values byte-identical.

Full detail in `docs\LAST_CLAUDE_REPORT.md`.

## AMZN FY2024 quarterly proof — complete — PASS
**New script `scripts\111_amzn_fy2024_quarterly_proof.py`** (copied from the verified `scripts\109` MSFT proof; only `FILINGS`/output labels changed, quarter logic byte-for-byte unchanged) extracted all 6 metrics from the already-warehoused AMZN filings — no Arelle, no downloads.

**One real discrepancy found and fixed:** AMZN's FY2024 10-K tags `income_tax_expense` twice at different rounding precisions in the same context ($9,265,000,000 at decimals=-6 vs $9,300,000,000 at decimals=-8 — same value, different rounding). Fixed by calling the project's own pre-existing, already-approved `_reconcile_same_context_precision_duplicates_from_warehouse` (already imported via `s89`, just not yet used in this proof script's fact-selection path) — not new logic, the same reconciliation `scripts\92` already applies everywhere else.

**Result: 24/24 quarterly results, all 6 annual reconciliations exact (diff=0.00).** Revenue $637,959M, operating_income $68,593M, pretax_income $68,614M, income_tax_expense $9,265M, operating_cash_flow $115,877M, capex $82,999M — all reconcile Q1+Q2+Q3+Q4=Annual exactly.

**YTD fallback status: still only cross-check-verified, not load-bearing.** Exactly like MSFT, AMZN tags discrete 3-month facts for every one of the 6 metrics (including both cash-flow items) — so every Q1/Q2/Q3 value used `DIRECT_QUARTER`, per "do not force derivation when a valid fact exists." `DERIVED_FROM_YTD` was computed as an independent cross-check for all 12 applicable cases (Q2+Q3 × 6 metrics) and matched the direct value exactly every time — confirming the derivation formula is correct, but two single-company proofs in a row have now both had direct facts available everywhere, so the fallback has never yet been the *sole* source of a reported value.

**Confirmed zero side effects**: Annual V1 checksum unchanged; production DB still 900/900 resolved, 0 REVIEW_REQUIRED; zero `financial_metric_results` rows for the 3 AMZN 10-Q accessions; AMZN's 6 annual values byte-identical.

Outputs: `data\quarterly_proof_amzn_fy2024.json`, `data\quarterly_proof_amzn_fy2024.csv` (24 rows). Full detail in `docs\LAST_CLAUDE_REPORT.md`.

**Recommendation:** not yet ready to consolidate into one production quarterly engine. Recommend one more bounded single-company proof on a filer specifically known to omit discrete-quarter tagging for at least one metric (commonly cash-flow items) — the only way to confirm `DERIVED_FROM_YTD` works correctly when it is the *only* available evidence, not just when it happens to agree with an already-available direct value.

## ORCL FY2024 10-Q lock + warehouse — complete (extraction not yet run) — PASS
**Locked and warehoused ORCL's 3 FY2024 10-Qs** (Q1 2023-08-31 filed 2023-09-12, accession 0000950170-23-047713; Q2 2023-11-30 filed 2023-12-12, accession 0000950170-23-069682; Q3 2024-02-29 filed 2024-03-12, accession 0000950170-24-029904) — all 4 identifiers (including the FY2024 10-K, accession 0000950170-24-075605) independently confirmed against SEC EDGAR submissions metadata before use, exact match.

**New script `scripts\112_build_xbrl_warehouse_orcl_10q_fy2024.py`** — copied from the verified, timeout-enabled `scripts\110` (AMZN loader); only ticker/dates/script-name changed, the subprocess/300s-timeout mechanism preserved unchanged. SEC rate-limited this session noticeably (4 transient 503/timeout errors across the 3 downloads, all on non-XBRL rendering-preview files never needed by Arelle, each resolved by retrying with increasing backoff) — no corruption, no partial file accepted.

**Result: all 3 filings PASS**, verified directly in DuckDB: Q1 624 facts/143 contexts/6 units; Q2 850/190/6; Q3 858/196/6. Zero timeouts, zero failures. Total runtime 11.61s. Note: Q3's locked package lacks separately-named `_cal/_def/_lab/_pre.xml` files, yet Arelle still resolved the full relationship set via its existing `internetConnectivity="online"` setting — flagged for transparency, not a defect.

**Confirmed zero side effects**: Annual V1 checksum unchanged; zero `financial_metric_results` rows for the 3 new accessions; ORCL FY2024's 6 annual production values byte-identical.

Full detail in `docs\LAST_CLAUDE_REPORT.md`.

## ORCL FY2024 quarterly extraction proof — FAIL (genuine blocker found, not a bug)
**New script `scripts\113_orcl_fy2024_quarterly_proof.py`** (copied from the verified `scripts\111` AMZN proof, only `FILINGS`/labels changed) — attempted but could not complete: **all 6 metrics failed at the annual (FY 10-K) row-resolution step**, confirmed by direct query that ORCL's FY2024 10-K (accession `0000950170-24-075605`) has **zero rows in every warehouse table** (facts, contexts, units, presentation/calculation/definition relationships, roles) and no `warehouse_runs` entry at all — it was never actually loaded into `xbrl_warehouse_proof.duckdb`.

**Root cause of the gap:** the prior task ("ORCL FY2024 10-Q lock + warehouse") verified the 10-K's accession/dates against SEC metadata and against the production database's already-computed values (from the *original* live-Arelle engine, `v14`/`scripts/60` — a separate data path) — but never checked whether the SAME filing was also present in the separate warehouse database used by the quarterly-proof scripts. It was not; only the 3 10-Qs got warehoused. This gap was invisible until this task actually tried to read from it.

**Result: 0 of 24 quarterly results.** All 6 metrics correctly left `REVIEW_REQUIRED` — Q1/Q2/Q3 concept resolution succeeded for every metric (confirmed: the failure only ever occurred on the last-checked filing, FY), so the quarter-derivation logic itself was never even reached; no value was invented. `data\quarterly_proof_orcl_fy2024.json` was written (6 REVIEW_REQUIRED entries with exact error messages); the CSV was correctly NOT created (zero successful rows to write).

**Confirmed zero side effects**: Annual V1 checksum unchanged; ORCL's 6 annual production values byte-identical; no other company touched.

**Recommendation:** unchanged from the AMZN proof — still not ready to consolidate into a production engine; the open question (does `DERIVED_FROM_YTD` work when it's the *only* evidence) remains unanswered after 3 attempts.

Full detail in `docs\LAST_CLAUDE_REPORT.md`. **Exact next step (not started, requires new authorization):** warehouse ORCL's FY2024 10-K (accession `0000950170-24-075605`) — one bounded Arelle load, same pattern as every other anchor 10-K already warehoused in this project. Once done, `scripts\113` can be rerun as-is (no code change needed) to complete the proof.

## Annual XBRL Warehouse completion gate — PASS (50/50)
**Objective:** verify/complete the ENTIRE annual XBRL warehouse (45
target company-years + 5 supplementary prior-year filings = 50 exact
10-K filings) before any further 10-Q/quarterly work — a standing gate
requested explicitly, following the ORCL FY2024 quarterly-proof FAIL
that first surfaced a warehouse gap.

**Step 1 — audit (`scripts\114_annual_warehouse_audit.py`, new).**
Derived the authoritative 50-filing expected universe purely from
existing metadata (Annual V1 manifest's `target_company_years`/
`supplementary_prior_year_filings` + `sec_filings`, filtered to
`form='10-K'` — a metadata-driven exclusion of 9 unrelated 10-Q rows
from the separate MSFT/AMZN/ORCL quarterly proofs, not a guess; this
filter was a real bug found and fixed mid-task, first run raised
`RuntimeError` at 54 vs. expected 45). First audit run:
**COMPLETE: 38, LOCKED_NOT_WAREHOUSED: 12, NOT_LOCKED: 0,
WAREHOUSE_FAILED: 0, METADATA_MISMATCH: 0.**

**The 12 missing (all already locked on disk, none supplementary):**
MSFT 2020/2021/2022/2023-06-30; NVDA 2021-01-31/2022-01-30/2023-01-29;
ORCL 2020/2021/2022/2023/2024-05-31 (the last is the exact filing the
ORCL quarterly proof had already found missing — now confirmed as part
of a systematic, not isolated, gap). **Root cause:** MSFT, NVDA, and
ORCL ran entirely on the original live-Arelle engine (`v14`/scripts/60)
throughout the project — none of the warehouse-based D-0xx fix
milestones ever needed to touch their non-anchor years, so only each
company's single anchor year (from the original 4-company
generalization proof) was ever warehoused. ORCL was never even part of
that original set, so none of its years were warehoused before this
gate found them.

**Step 2 — load (`scripts\115_annual_warehouse_load_missing.py`,
new).** Generalized the parent/worker subprocess pattern proven in
`scripts\110` (AMZN 10-Q loader) from a single fixed `TICKER` constant
to worker-mode CLI args `--single-ticker`/`--single-report-date`, since
the 12 missing filings span 3 companies — no other change to the
Arelle extraction logic, warehouse schema, or accounting policies.
Real `subprocess.run(..., timeout=300)` per filing, `TimeoutExpired` →
`TIMEOUT`/continue-no-retry (never triggered). **Result: 12/12 PASS,
0 TIMEOUT, 0 FAIL, total runtime 51.7s.** All 12 filings were Inline
XBRL (confirmed by nonzero extracted fact counts on first attempt —
no traditional/separate-instance-document handling was needed, unlike
NVDA FY2019 in D-034). Fact/context/unit counts ranged 1307–2020 /
280–514 / 5–9 across the 12. One informational note: ORCL 2024-05-31
extracted 0 calculation-linkbase relationships (all other counts
nonzero, fact/context/unit all present) — recorded as a warning in the
manifest, not a failure, since STEP 3's success criteria (fact/context/
unit > 0, one PASS run, no duplicates) do not depend on calculation
relationships.

**Step 3/4 — verify + manifest (`scripts\116_annual_warehouse_verify_
and_manifest.py`, new).** Audit rerun: **50/50 COMPLETE, 0 missing.**
Final verification confirmed 0 duplicate `warehouse_runs` rows and 0
empty successful loads across all 50. Manifest created at
`data\database\annual_xbrl_warehouse_v1_manifest.json`:
`expected_filing_count=50, pass_count=50, missing_filing_count=0,
timeout_count=0, fail_count=0, success_criteria_met=true`, one record
per filing with ticker/report date/filing date/accession/primary
document/Arelle entry point/locked path/fact/context/unit counts/
loader script/load timestamp/warning.

**Step 5 — no side effects, confirmed:**
- Annual V1 SHA-256 checksum unchanged
  (`e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814`).
- Scripts 115/116 never open the production database at all; script
  114 opens it `read_only=True` only — zero writes to
  `financial_metric_results` by this task. (The live production DB's
  `financial_metric_results` row count has grown to 1,882 rows with
  557 `REVIEW_REQUIRED` since Annual V1 was frozen at 900/0 — this
  predates this task entirely, from earlier D-0xx extraction re-runs,
  and is unrelated to this gate.)
- No 10-Q metric extraction was run; no quarterly production schema
  created; no market-price/backtesting work started; no new companies
  added; ORCL quarterly extraction was explicitly NOT rerun (out of
  scope for this task).

**Standing gate — now satisfied, recorded for future reference:**
Quarterly work may begin only when the Annual XBRL Warehouse manifest
shows every expected annual 10-K filing as PASS. **As of this
milestone, that condition is met (50/50 PASS).** Quarterly work
(resuming the ORCL FY2024 quarterly proof via `scripts\113`, unchanged)
may now proceed.

Full detail in `docs\LAST_CLAUDE_REPORT.md`.

## ORCL FY2024 quarterly proof rerun (post-warehouse-gate) — FAIL (4/6 PASS, genuine $1M gap)
**Ran `scripts/113_orcl_fy2024_quarterly_proof.py` unchanged**, now that all 4
required accessions (3 10-Qs + the FY2024 10-K) are warehoused per the
completed Annual XBRL Warehouse gate above. Pre-run validation confirmed all
4 PASS with nonzero facts before executing. Produced the full 24 results (6
metrics × 4 quarters) and all 6 reconciliations.

**4 of 6 metrics PASS** (revenue, income_tax_expense, operating_cash_flow,
capex) — exact $0.00 reconciliation. **2 of 6 FAIL** (operating_income,
pretax_income) — both off by exactly $1,000,000, traced to a genuine
inconsistency inside ORCL's own Q3 10-Q (accession `0000950170-24-029904`)
between its discrete-quarter fact and its YTD-derived cross-check value for
those two metrics only (`cross_check_matches_direct: false`) — not a script
defect, not an invented value.

**Key finding — closes the open question carried across MSFT → AMZN → ORCL:**
for `operating_cash_flow` and `capex`, ORCL's Q2/Q3 10-Qs tag **only** a YTD
cash-flow fact, no discrete 3-month fact — `DERIVED_FROM_YTD` was the sole
source for those 4 quarter-values (not merely a redundant cross-check as in
every prior proof), and both reconciled exactly to the annual 10-K figure.
**`DERIVED_FROM_YTD` is now confirmed to work correctly when it is the only
available evidence.**

**Confirmed zero side effects:** Annual V1 checksum unchanged; ORCL FY2024's
6 annual production values byte-identical (verified via latest-result-per-
filing-and-metric query, not raw row counts); zero `financial_metric_results`
rows for the 3 ORCL 10-Q accessions; `sec_filings` form counts unchanged
(50 `10-K`, 9 `10-Q`); no other company touched.

**Recommendation:** not yet ready to consolidate into a production quarterly
engine, but the central open question (does YTD-derivation work when it's
the only evidence) is now answered yes. Remaining blocker before
consolidation: an explicit user decision on a tolerance policy for
sub-rounding-unit ($1,000,000-scale, at the `decimals=-6` precision floor)
reconciliation gaps, rather than requiring exact-dollar equality.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## ORCL FY2024 quarterly proof — precision-aware reconciliation (D-035) — 6/6 RESOLVED
**New script `scripts/117_orcl_fy2024_quarterly_proof_precision_aware.py`**
(copy of the verified `scripts/113`, 113 left untouched) — adds only the
approved XBRL-decimals-based rounding-tolerance policy (see
`docs/DECISIONS_LOG.md` D-035) to the final reconciliation step. No change to
quarter-duration classification, `DIRECT_QUARTER` selection,
`DERIVED_FROM_YTD` logic, Q4 derivation, concept selection, or filing-date
availability logic. No reported value is ever altered.

**Policy:** `uncertainty_per_fact = (10 ** (-decimals)) / 2`;
`permitted_difference` = sum of the uncertainties of every independently
reported source fact in the `Q1+Q2+Q3+Q4 vs. Annual` equation (Q4 itself is
derived, not independently reported, so its uncertainty is already carried
by the Annual and Q3-9mYTD terms it's built from — not double-counted).
`abs(difference) <= permitted_difference` → `PASS_ROUNDING_TOLERANCE`;
otherwise fail-closed `REVIEW_REQUIRED` (this branch has not yet been
exercised on a real case). Full precision lineage preserved per metric
(`precision_calculation` block: decimals, rounding unit, uncertainty per
fact, permitted difference, actual difference).

**Result: 6 of 6 metrics resolved** (up from 4/6 PASS + 2/6 FAIL in the prior
exact-equality run). `revenue`, `income_tax_expense`, `operating_cash_flow`,
`capex` remain exact `PASS` ($0 difference). `operating_income` and
`pretax_income` — both previously `FAIL` on an exact $1,000,000 gap — are now
`PASS_ROUNDING_TOLERANCE`: all 4 contributing source facts (Q1, Q2, Q3,
Annual) are reported at `decimals=-6` ($1,000,000 rounding unit, $500,000
uncertainty each), giving a permitted difference of $2,000,000 — the actual
$1,000,000 gap sits inside it. All reported quarterly/annual values are
byte-identical to the pre-policy run; only the reconciliation `status`
changed, from an honest explanation of the gap, not from adjusting data.

**Confirmed:** exactly 24 quarterly results + 6 reconciliations;
`operating_cash_flow`/`capex` still use `DERIVED_FROM_YTD` for Q2/Q3 (still
genuinely load-bearing, still $0 diff); Annual V1 checksum unchanged; ORCL
FY2024's 6 annual production values byte-identical; zero
`financial_metric_results` rows for the 3 ORCL 10-Q accessions; no other
company touched.

**Recommendation:** closer to ready for consolidation into a production
quarterly engine — both open questions from prior proofs are now answered
(YTD-derivation works standalone; a precision-aware tolerance policy resolves
genuine rounding-level gaps without altering data). Not yet exercised: a real
case where the calculated tolerance is actually exceeded (the fail-closed
`REVIEW_REQUIRED` branch is implemented but unproven in practice).

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## Consolidated quarterly extraction engine — PASS (72/72 reproduced, 0 differences)
**New scripts `scripts/118_quarterly_extraction_engine.py`** (the engine)
**and `scripts/119_quarterly_extraction_engine_validation.py`** (generic
validation harness) — consolidate the verified per-company quarterly-proof
logic (scripts 109 MSFT, 111 AMZN, 117 ORCL precision-aware) into one
ticker-agnostic, fiscal-year-agnostic engine. Parameters: `ticker`,
`fiscal_year_end`, `q1_accession`, `q2_accession`, `q3_accession`,
`fy_accession`, `json_output_path`, `csv_output_path`. Filing metadata
(report_date/filing_date/form) is looked up from `sec_filings` (read-only)
and cross-validated against the supplied ticker/fiscal_year_end — fail-closed
if mismatched. No change to quarter-duration classification,
`DIRECT_QUARTER`/`DERIVED_FROM_YTD`/Q4-derivation logic, concept selection,
filing-date availability, same-context precision-duplicate reconciliation
(now applied unconditionally, a no-op where no duplicate exists), or the
D-035 rounding-tolerance policy (its calculation was extracted into a pure,
DB-free function, `compute_precision_aware_reconciliation()`, for
testability). 109/111/113/117 left untouched as historical baselines.

**Validation: ran the engine for MSFT/AMZN/ORCL FY2024 (24 results each,
72 total) against their already-warehoused accessions, writing to NEW
output paths (`data/quarterly_engine_{ticker}_fy2024.json/.csv`) so the
original verified files are preserved untouched.** Compared every
quarterly value, extraction_basis, and reconciliation status
programmatically against the original verified JSONs: **0 of 72 results
differ.** MSFT/AMZN remain all-`PASS`; ORCL's `operating_cash_flow`/`capex`
still use `DERIVED_FROM_YTD` for Q2/Q3 (genuinely load-bearing); ORCL's
`operating_income`/`pretax_income` still resolve `PASS_ROUNDING_TOLERANCE`.

**Fail-closed synthetic test (in `scripts/119`, no database access, no
company processed, nothing written to either database):** hand-built
Q1=Q2=Q3=Q4=$1,000,000,000 (decimals=-6 each), Annual=$4,005,000,000 — a
$5,000,000 gap against a $2,000,000 permitted difference. **Engine
correctly returned `REVIEW_REQUIRED`** — the fail-closed branch (present
but never exercised on a real case as of D-035) is now proven to actually
work, not just implemented.

**Confirmed zero side effects:** Annual V1 checksum unchanged; MSFT/AMZN/
ORCL FY2024's 18 annual production values (6 each) byte-identical; zero
`financial_metric_results` rows for any of the 9 10-Q accessions used;
`sec_filings` form counts unchanged (50/9); no other company processed; no
production quarterly schema created.

**Next step:** design the production quarterly database tables/schema to
persist per-company, per-fiscal-quarter engine results (point-in-time
integrity, lineage, and the 3-way status set already proven) — not yet
started.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## Minimal production quarterly schema + load — PASS (72/72 rows, 0 rollbacks)
**New script `scripts/120_quarterly_production_schema_load.py`** — adds
exactly 2 new tables to the existing production database
(`data/database/ai_stock_agent.duckdb`), additive only, no existing table
touched: `quarterly_extraction_runs` (one row per company/fiscal-year load,
natural key `ticker+fiscal_year_end+engine_version`) and
`quarterly_metric_results` (one row per company/fiscal-year/metric/quarter,
natural key `run_id+metric_name+fiscal_quarter`, full lineage as
`lineage_json`, plus row-level `result_status` distinct from metric-year-
level `reconciliation_status`).

**Safety model:** before writing anything, every engine JSON
(`data/quarterly_engine_{ticker}_fy2024.json`) is re-compared against its
original verified proof JSON — 0 differences found for MSFT/AMZN/ORCL, so
the load proceeded. Each company loads in its own transaction with a
read-back validation (row count=24, no null required fields, no duplicate
natural keys, values match source JSON) before commit; any failure would
roll back that company only. **All 3 companies committed cleanly, 0
rollbacks.**

**Result: 3 extraction runs, 72 quarterly_metric_results rows (24 per
company), 0 duplicate natural keys.** Extraction-basis counts: 50
`DIRECT_QUARTER`, 4 `DERIVED_FROM_YTD`, 18 `DERIVED_Q4_FROM_10K_MINUS_9M`
(verified correct — Q4 is structurally always derived, so 18 = 3
companies × 6 metrics is the only possible count; a task-stated expectation
of 16 was checked and found incorrect). Reconciliation-status counts
(metric-year level, 18 total): 16 `PASS`, 2 `PASS_ROUNDING_TOLERANCE`, 0
`REVIEW_REQUIRED` — matches expectation exactly.

**Confirmed:** availability_date equals the matching accession's own
`sec_filings.filing_date` for all 72 rows (0 mismatches); only legitimately-
optional fields (`period_start`, and `context_id` for ORCL's 4
`DERIVED_FROM_YTD` rows) are null, every other required field populated in
all 72 rows; Annual V1 checksum unchanged; annual `financial_metric_results`
status breakdown unchanged; `sec_filings` form counts unchanged (50/9); no
existing table modified.

**Next step:** run a bounded quarterly batch on the existing validated
companies (MSFT/AMZN/ORCL) for additional already-warehoused fiscal years,
using the same engine and schema, before extending to any new company.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## Bounded quarterly batch — MSFT/AMZN/ORCL x FY2022/FY2023/FY2024 — PASS (9/9, 216/216 rows)
**New script `scripts/121_quarterly_batch_runner.py`** (generic batch
runner; the 9 targets are a data list, not company-specific code). Reuses
unmodified: `scripts/107` (SEC lock functions, via importlib),
`scripts/118` (consolidated engine), `scripts/120` (`create_schema`/
`load_one_company`); the Arelle warehouse-loading internals are the same
logic as `scripts/108/110/112/115`, generalized to `EXPECTED_FORM="10-Q"`
with locking added first.

**Two real bugs found and fixed in `scripts/121` during this task** (107/
108/110/112/115/118/120 untouched): (1) DuckDB refuses a second
same-file connection with a different read-only/read-write config while
one is open — fixed by opening/closing a fresh short-lived connection
around each discrete DB operation instead of holding one open across
`scripts/118`'s own internal connection. (2) `scripts/118` looks up each
accession's metadata from `sec_filings`, but the 18 newly-locked
FY2022/FY2023 10-Qs weren't registered there (only the 9 pre-existing
FY2024 10-Qs were, from earlier work) — fixed by adding
`ensure_sec_filings_row()`, idempotent, same schema/natural key as the
existing rows.

**Completed company-years (9/9):** MSFT/AMZN/ORCL × FY2022/FY2023/FY2024.
3 were already complete (FY2024, verified 24/24 rows each, untouched,
not reloaded). 6 newly committed (FY2022 + FY2023 for all 3 companies),
each in its own transaction with read-back validation, **0 rollbacks**.

**18 new 10-Q filings locked + warehoused** (3 per new company-year), all
`PASS`, 0 TIMEOUT, 0 FAIL, 2.75s–4.31s each.

**Result: `quarterly_extraction_runs` 3→9, `quarterly_metric_results`
72→216 (144 new rows).** Extraction-basis (216 total): `DIRECT_QUARTER`=150,
`DERIVED_Q4_FROM_10K_MINUS_9M`=54 (=9×6, structurally always derived),
`DERIVED_FROM_YTD`=12 (all ORCL `operating_cash_flow`/`capex` Q2+Q3, now
confirmed a consistent 3-year filer pattern, not a one-off). Reconciliation
(54 metric-years): `PASS`=47, `PASS_ROUNDING_TOLERANCE`=7 (all ORCL, same
$1,000,000-vs-$2,000,000 D-035 pattern recurring across FY2022/FY2023/
FY2024), `REVIEW_REQUIRED`=0.

**Confirmed:** 0 duplicate natural keys; 0 missing required lineage fields
(only `context_id` legitimately null, 12 rows, all `DERIVED_FROM_YTD`); 0
availability-date mismatches; existing 72 FY2024 rows unchanged; Annual V1
checksum unchanged; annual `financial_metric_results` unchanged; only the 2
pre-existing quarterly tables used (no new tables); `sec_filings` 10-Q rows
grew 9→27 (the 18 newly-registered filings, same schema); no other
company/year processed.

**Files:** `data/quarterly_batch_msft_amzn_orcl_fy2022_fy2024_result.json`
(script's own output) and `data/quarterly_3companies_3years_batch_result.json`
(reconstructed one-record-per-company-year form) — both reflect the same
committed database state.

**Next step:** run one additional already-warehoused fiscal year (e.g.
FY2021) on the same 3 validated companies to further stress-test the
pipeline before considering a new company.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## Annual production data cleanup — PASS (financial_metric_results: 1,882 → 900 rows)
**New script `scripts/122_annual_production_data_cleanup.py`** — replaced
the active production `financial_metric_results` table's contents with
exactly the 900 authoritative Annual V1 rows (45 target company-years x 20
primary metrics, 0 REVIEW_REQUIRED), archiving every removed row first.

**Key finding:** Annual V1's own raw `financial_metric_results` table has
1,882 rows, not 900 — it's a full snapshot copy, not a pre-filtered table.
The correct "900 authoritative rows" identification is a specific reduction
query (fixed 20-metric-name list, excluding the 5 supplementary
prior-year accessions, latest-per-`(accession, metric)` by `loaded_at`),
copied verbatim from `scripts/106_freeze_annual_v1_snapshot.py`'s own
`gather_stats()` — the exact logic that originally froze and checksummed
Annual V1. Two other plausible reconstructions (`is_primary_metric=TRUE`
filter; latest-whole-run-per-accession) were tried first and correctly
rejected because neither reproduced the manifest's exact
900/45/20/0-REVIEW_REQUIRED claim.

**Phase 1 (read-only audit):** production's 1,882 raw rows, reduced with
the same query, matched Annual V1's 900 rows exactly (0 missing, 0
mismatches, 0 ambiguous/tied mappings, identical schema) — passed, proceeded.

**Phase 2 (backup + archive):** full DB backup
(`data/database/backups/ai_stock_agent_pre_annual_cleanup_20260804T065520Z.duckdb`,
checksum-verified identical to source) + 2 Parquet exports (pre-cleanup
full table, 1,882 rows; removed rows, 982 rows) + a manifest — all
verified before Phase 3 began. (A pyarrow/fastparquet import error on the
first attempt was fixed by switching to DuckDB's native `COPY ... FORMAT
PARQUET`, avoiding a new dependency.)

**Phase 3 (transactional cleanup):** one transaction — staging table
built + validated (900 rows, 45×20, 0 REVIEW_REQUIRED, 0 duplicates, exact
match to a freshly-read Annual V1) — then `financial_metric_results`
replaced (rename-old / rename-staging-in / drop-old, all inside the same
transaction) and committed. A post-commit null-aware re-verification
confirmed 0 real mismatches across all 900 rows (an initial naive
string-comparison check had shown 2,870 false positives from `None`-vs-
`NaN` stringification on legitimately-null columns — resolved).

**Post-cleanup, all confirmed:** `financial_metric_results`=900 (45×20, 0
REVIEW_REQUIRED, exact Annual V1 match, 0 duplicates); quarterly data
completely unchanged (`quarterly_extraction_runs`=9,
`quarterly_metric_results`=216, 0 duplicates/missing-lineage/availability-
mismatches); Annual V1 checksum unchanged; 50/50 annual 10-K warehouse
filings still `PASS`; `sec_filings` unchanged (50 `10-K`, 27 `10-Q`);
`companies`(9)/`extraction_runs`(172)/`historical_review_items`(222)
unchanged; no new/leftover tables.

**Next step:** no further annual cleanup needed. Consider extending the
quarterly batch pattern to another fiscal year, or begin designing the
scoring/backtesting layer now that both annual and quarterly production
tables are clean and verified.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## 45-company-year quarterly batch — COMPLETE, PARTIAL PASS (18/45 committed)
**New script `scripts/123_quarterly_batch_runner_45_company_years.py`** —
generalizes `scripts/121` from the 9-company-year MSFT/AMZN/ORCL target to
the full authoritative 45-company-year universe (9 tickers x 5 fiscal
years: AMZN, CRWD, GOOGL, META, MSFT, MU, NVDA, ORCL, PANW), derived
dynamically from Annual V1 (never hardcoded, verified exactly 45/9/5
before processing). Mandatory per-company-year checkpoint/progress-log
saves (`data/quarterly_9companies_5years_batch_result.json`,
`data/quarterly_9companies_5years_batch_progress.log`) for safe
restartability. Reuses `scripts/107/118/120` unmodified for the common
case; adds one new capability
(`load_company_year_allowing_review_required`) for company-years with a
genuinely unresolved metric, which `scripts/118`/`scripts/120` cannot
represent as-is.

**Final result after all 45/45 evaluated (8,638s ≈ 2h24m runtime): 18
committed, 27 not committed for two distinct, honestly-reported reasons —
PARTIAL PASS, not converted to PASS.**

- **18 committed** (432 rows, 0 duplicates, 0 missing lineage, 0
  availability mismatches, 0 REVIEW_REQUIRED): AMZN 2021-2025 (all 5),
  CRWD 2025, GOOGL 2024, META 2020, MSFT 2020-2024 (all 5), ORCL 2020-2024
  (all 5).
- **22 blocked by a real, identified bug** (not a data problem): the new
  fallback loader's honest NULL placeholders for genuinely-unresolved
  metrics violate `quarterly_metric_results`'s existing `NOT NULL`
  constraints on `concept_qname`/`reconciliation_difference`/
  `permitted_difference` (from `scripts/120`'s original schema) — every
  such INSERT is rejected and the transaction cleanly rolls back (0
  corrupted commits, just lost progress). Affects CRWD 2023/2024/2026,
  GOOGL 2021/2022/2023/2025, META 2021/2022/2023/2024, MU 2021-2025 (all
  5), NVDA 2020-2024 (all 5), PANW 2021. **Not fixed** — needs a schema
  nullability change (or a documented sentinel) in a new versioned script.
- **5 blocked by transient SEC rate-limiting** (HTTP 503 / read timeout on
  non-XBRL `R*.htm` rendering-preview files during locking — the same
  well-documented transient pattern seen throughout this project, never a
  data risk): CRWD 2022, PANW 2022/2023/2024/2025. Would likely succeed on
  a simple retry, none attempted.

**Locked/warehoused**: 124 10-Q packages locked on disk (9 pre-existing +
new); 120 registered in `sec_filings`; **120/120 registered 10-Q
accessions are `warehouse_runs` PASS** — Arelle parsing had a 100% success
rate; all failures occurred at the network-locking or production-load
step, never during XBRL parsing.

**Confirmed unchanged**: annual `financial_metric_results` still exactly
900 rows, 0 REVIEW_REQUIRED; Annual V1 checksum unchanged
(`e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814`); the
original 216 quarterly rows (9 original company-years) untouched; no new
production tables; `sec_filings` now 50 `10-K` / 120 `10-Q`.

**Next step (two independent, bounded follow-ups, each needing its own
authorization):** (1) fix the NOT NULL schema issue in a new script, then
re-run the 22 affected company-years; (2) simply retry the 5
transient-network failures, no code change needed.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## Quarterly schema nullable fix + GOOGL FY2021 proof — PASS (D-036)
**New scripts**: `scripts/124_quarterly_schema_nullable_fix_and_resume.py`
(backup, schema migration, GOOGL FY2021 proof, and — per that same task's
own explicit "resume only if proof passes" instruction — an auto-resume
of the remaining batch) and
`scripts/125_googl_fy2021_proof_verification_report.py` (read-only,
created in a later turn to verify the proof and report true state without
processing anything new).

**Root cause fixed**: `quarterly_metric_results.concept_qname`,
`.reconciliation_difference`, `.permitted_difference` were `NOT NULL`,
rejecting the honest NULL placeholders the REVIEW_REQUIRED-tolerant loader
(from `scripts/123`) writes for genuinely-unresolved metrics. Fixed via
`ALTER TABLE ... ALTER COLUMN ... DROP NOT NULL` (DuckDB 1.5.5 supports
this directly) inside one transaction — backup taken first
(`data/database/backups/ai_stock_agent_pre_quarterly_nullable_fix_
20260804T152035Z.duckdb`, checksum-verified identical to source,
pre-migration counts 900/18/432 confirmed both in source and backup).
A second, unrelated bug was found and fixed during the first proof
attempt: DuckDB NULL surfaces as pandas `NaN` (not `None`) in a `.fetchdf()`
DOUBLE column, so a `row["value"] is not None` check falsely rejected
every legitimate NULL — fixed with `pd.isna()`.

**GOOGL FY2021 proof — PASS**: 24 rows committed, 12 `PASS` + 12
`REVIEW_REQUIRED` (0 forced/fabricated). Genuine REVIEW_REQUIRED causes:
`pretax_income` (all 4 quarters — GOOGL's FY2021 10-K has no
deterministically identifiable pretax-income statement row at all) and
`operating_cash_flow`/`capex` (Q2-Q4 each — Q1 resolved normally with a
real concept and value, but Q2's 10-Q lacks any deterministic direct or
6-month-YTD fact for either concept). Every NULL confirmed to correspond
to a `REVIEW_REQUIRED` row with a documented reason in `lineage_json` —
0 exceptions.

**Sequencing note (fully disclosed, not hidden)**: because the schema-fix
task's own instructions said to auto-resume the remaining batch once the
proof passed, `scripts/124` did so and committed 20 company-years (not
just GOOGL FY2021) before a later, separate instruction asked to stop
immediately after the proof — the process was terminated the moment that
instruction arrived. The 20 extra commits were not undone (each
independently transactionally validated; this project's rules forbid
deleting committed data without authorization). **Current state: 38 of 45
company-years committed, 912 quarterly rows, 0 duplicates.** Remaining 7:
NVDA FY2024, PANW FY2021, and the 5 network-retry company-years (CRWD
FY2022, PANW FY2022/2023/2024/2025) — untouched.

**Confirmed unchanged**: the original 432 quarterly rows; `financial_metric_results`
still exactly 900; Annual V1 checksum still
`e655671e36298ce2297b8c86077a8f0559998b81245d1850004da36858e9f814`.

**Next step**: explicit decision needed — leave the batch at 38/45, or
authorize a new script to process the remaining 7 (2 reuse-only + 5
network-retry) to reach 45/45. No further code changes needed either way.

Full detail in `docs/LAST_CLAUDE_REPORT.md`. Decision recorded as D-036 in
`docs/DECISIONS_LOG.md`.

## Quarterly engine v2 — annual-anchor-from-production fix — PASS (54/54 proven, not yet loaded)
**New scripts `scripts/128_quarterly_extraction_engine_v2.py`** (based on
`scripts/118`, one logical change) **and `scripts/129_quarterly_engine_v2_
validation.py`** (validation harness). Both read-only; `scripts/118` and
all earlier scripts untouched.

**The one change**: the annual (FY 10-K) anchor value is now read directly
from the already-authoritative `financial_metric_results` row for the
exact same accession + metric (via `extraction_runs.accession_number`),
instead of re-identifying the annual statement row independently against
the warehouse. Fail-closed (`REVIEW_REQUIRED`, exact reason preserved) on
0/>1 rows, accession mismatch, unsupported status, or missing concept —
never a guess, never a different filing. Everything else (Q1/Q2/Q3
resolution, `DIRECT_QUARTER`/`DERIVED_FROM_YTD`, the Q4 equation,
`filing_date` availability, precision-duplicate reconciliation, the D-035
tolerance policy, output structure) is byte-for-byte unchanged from
`scripts/118`.

**One real bug found and fixed during validation**: AMZN FY2024
`income_tax_expense` initially regressed to `REVIEW_REQUIRED` — the new
decimals-lookup helper wasn't applying the same already-approved
same-context precision-duplicate reconciliation (`s89._reconcile_same_
context_precision_duplicates_from_warehouse`) every other fact lookup in
this engine already uses. Fixed by reusing that same unchanged function;
re-validated with 0 remaining differences.

**Validation 1 (baseline regression, MSFT/AMZN/ORCL FY2024): PASS — 72/72
rows, 0 differences** vs. the previously verified `scripts/118` output
(only the new `annual_anchor_*` lineage metadata was added, no financial
result changed).

**Validation 2 (all 54 ANNUAL_ROW_NOT_RESOLVED cases, read from
`data/quarterly_review_required_audit.json`, not hardcoded): 54/54
resolved** — 49 `PASS`, 5 `PASS_ROUNDING_TOLERANCE` (all NVDA, genuine
$1M-vs-$2M D-035 gaps, same honest pattern as ORCL), **0 remaining
`REVIEW_REQUIRED`**. Covers 12 unique company-years: GOOGL FY2021/2022/
2023; MU FY2021-2025 (all 5); NVDA FY2021/2022/2023/2024. Every anchor's
accession confirmed to exactly match the FY accession supplied (no later
filing ever used); every annual `status` was `PASS`; no value or concept
fabricated.

**This is a proof only — nothing was loaded to production.** The 111
REVIEW_REQUIRED count in `quarterly_metric_results` is unchanged; these 54
cases remain `REVIEW_REQUIRED` there until an explicitly authorized load
step runs.

**Confirmed unchanged**: all databases opened read-only throughout; no
row/schema/table changed anywhere.

**Next step**: authorize loading these 12 company-years' proven v2 output
into production (replacing exactly these 54 metric-years'
`REVIEW_REQUIRED` rows), which would reduce total REVIEW_REQUIRED from 111
to 57.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## Next open step
The historical dataset (45 company-years, 9 companies) is loaded into
the persistent point-in-time database. Before scoring/backtesting:
- **Fixed for MU in D-024** (was flagged in D-023): the prior-period
  date-tolerance gap. **Confirmed NOT applicable to NVDA/CRWD** (checked
  in the post-D-024 rerank) — both still run entirely on the live
  engine, which already has the tolerance built in; their blocker is
  the separate `current_debt::zero_inference_role_not_found` (no debt-
  maturity schedule role found — likely genuine missing data for CRWD).
- **Fixed for PANW (2021-2024) in D-025+D-026:** `cash_and_equivalents::
  row_ambiguous` (all 4 years) and `long_term_debt::ancestry_confirmed_
  absent` (2023, 2024) both closed. **PANW 2025-07-31 remains open**
  (warehouse gap — needs one Arelle load to close, see D-026 report).
  **META's `long_term_debt::ancestry_confirmed_absent` (2020-2021, ~5
  results) remains open, not yet started** — also needs Arelle (not
  warehoused for those years). `effective_tax_rate::pretax_income_not_
  positive`/`outside_plausible_range` (genuine losses/tax-benefit years,
  not fixable by extraction) remains open by design.
- **Updated, from D-022/D-023/D-025:** the warehouse now holds 21 of 45
  filings (AMZN/GOOGL full 5-year windows, all 5 MU years, all 5 PANW
  years, + the original 4-filing generalization set). Scaling to the
  remaining 24 company-years and wiring canonical-metric computation to
  read from the warehouse by default remain the next concrete
  architecture steps, still not started/approved.
- **Decision needed:** whether `total_debt`'s new `PASS_MATURITY_BASIS`
  results (AMZN/GOOGL, mixing bases with the rest of the dataset's
  GAAP-carrying-value results) are acceptable as-is for downstream
  scoring/backtesting, given the `basis` field distinguishes them but a
  consumer must be aware two measurement bases now coexist.
- **`average_invested_capital`/`roic` for AMZN/GOOGL remain
  REVIEW_REQUIRED** (20 of the 30 still-open D-022-related results) —
  blocked by `total_debt_prior`, which D-022 confirmed cannot be
  resolved via the maturity schedule (no prior-period comparative
  bucket exists in the same filing); would need a separate, explicit
  policy decision to address.
- **Not yet done, and explicitly out of scope for this milestone per
  the user's instruction:** scoring or backtesting on this dataset.
- A future generalization pass could address the remaining row-not-found/
  ambiguous label-convention gaps (Palo Alto Networks
  `cash_and_equivalents` ambiguity, Micron `cash_and_equivalents`
  absence, remaining `current_debt`/`long_term_debt` zero-inference and
  ancestry gaps) — the same kind of fix already applied repeatedly
  throughout this project. `pretax_income`'s gap is now resolved (D-020).
  **`current_debt::zero_inference_earliest_bucket_nonzero`** (the
  single largest remaining root cause, 60 potentially-resolved results)
  was attempted (D-021) and correctly stopped at the proof-test stage —
  see `docs\LAST_CLAUDE_REPORT.md`: the only available evidence (the
  debt-maturity schedule) is an undiscounted principal amount by GAAP
  convention, not a carrying amount, so this specific gap needs an
  explicit user/council decision on how to proceed (accept as
  permanently REVIEW_REQUIRED, or approve a principal-≈-carrying-value
  approximation policy), not a further generic extraction fix.
- Extend company coverage toward the full 15-company universe named in
  `docs/PROJECT_CONTEXT.md` (Nebius, Broadcom, ServiceNow, and the
  cruise companies remain unlocked).
- Consider extending each company's window forward to its true latest
  10-K (several now have one newer than their anchor year).
- `PASS_DIRECT_AGGREGATE` (D-018) still unexercised across all 45
  company-years now tested.
This decision should be made explicitly with the user before proceeding,
consistent with "one topic at a time."

## Immediate action after transfer
Claude performs a read-only audit:
- read all context files
- inspect repository tree
- identify relevant scripts and manifests
- summarise current state
- propose one bounded next step
- make no edits and run no commands until user approval


## Quarterly remaining-7 batch — background run result
Result: **PASS**. quarterly_extraction_runs=45/45, quarterly_metric_results=1080/1080, duplicates=0, missing_lineage=0, availability_mismatches=0, annual financial_metric_results=900/900, Annual V1 checksum unchanged=True. Full detail in docs/LAST_CLAUDE_REPORT.md.

**Quarterly V1 is now structurally complete: all 45 target company-years,
1,080 rows, 0 duplicates, 0 missing lineage, 0 availability mismatches.**
111 of the 1,080 rows' underlying metric-years (unique ticker/fiscal-year/
metric combinations) remain `REVIEW_REQUIRED` — see the root-cause audit
below for the full breakdown.

## Quarterly REVIEW_REQUIRED root-cause audit — PASS (111 cases classified, read-only)
**New script `scripts/127_quarterly_review_required_root_cause_audit.py`**
— read-only audit of all 111 unique REVIEW_REQUIRED metric-year cases
(confirmed exactly 111 via `DISTINCT ticker/fiscal_year_end/metric_name`).
No data, schema, or extraction logic touched.

**By root cause**: `ANNUAL_ROW_NOT_RESOLVED`=54 (49%, MU all 5 years + NVDA
4 of 5 years), `DIRECT_QUARTER_NOT_RESOLVED`=22 (100% CRWD),
`CONCEPT_NOT_RESOLVED`=21 (10-Q filings across CRWD/META/MU/NVDA/PANW,
mostly `revenue`/`pretax_income`), `YTD_FACT_NOT_RESOLVED`=14 (GOOGL/META
`capex`/`operating_cash_flow`, Q1 resolves but Q2 never does). 0 cases in
`CONTEXT_OR_DURATION_NOT_RESOLVED`, `RECONCILIATION_OUTSIDE_TOLERANCE`,
`CASCADING_DEPENDENCY_FAILURE`, or `OTHER`. **111 primary failures, 0
true cascading** (every case's shared placeholder-row error string already
identifies its own single earliest blocking point, by construction).

**Warehouse evidence** (FY-accession-only, keyword-based, directional not
precise): 107/111 cases (96%) show *some* plausibly-related fact present
in the filing; only 4 (all NVDA `capex`, FY2020–FY2023) show no plausible
fact at all — genuine source-data absence is rare in this dataset.

**Highest-value finding**: all 54 `ANNUAL_ROW_NOT_RESOLVED` cases' exact
accessions/metrics **already resolve successfully in the frozen Annual V1
dataset** (900/900, 0 REVIEW_REQUIRED) — proving the row/concept is
identifiable by logic already in this codebase. The quarterly engine's
own row-identification call just isn't achieving the same result on the
same filing — an implementation/parameterization gap between the annual
and quarterly call paths, not a new labeling convention. Assessed
`EXISTING_POLICY_ALREADY_COVERS_BUT_IMPLEMENTATION_MISSED` — the single
highest-leverage next fix, potentially closing ~49% of all open cases.

**Other groups**: `CONCEPT_NOT_RESOLVED` (21 cases) assessed
`GENERAL_RULE_FIX` (hypothesis: 10-Q "Condensed" statement-role titles may
not match a regex tuned on 10-K titles). `DIRECT_QUARTER_NOT_RESOLVED` (22,
all CRWD) and `YTD_FACT_NOT_RESOLVED` (14, GOOGL/META) both assessed
`AMBIGUOUS_REQUIRES_HUMAN_REVIEW` — plausible facts exist but don't match
the expected duration window; too few filers observed so far to generalize
confidently.

**Files**: `data/quarterly_review_required_audit.json` (all 111 cases,
full detail), `data/quarterly_review_required_audit.csv` (flat summary).
Runtime ~2 seconds.

**Next step**: investigate the annual-vs-quarterly row-identification
divergence for MU/NVDA (the 54-case group) — likely one general,
non-ticker-specific fix.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## Quarterly engine v2 production load — PASS (12/12 company-years committed)
**New script `scripts/130_quarterly_engine_v2_production_load.py`** —
loaded the already-validated engine-v2 (annual-anchor-from-production)
results into active production for exactly the 12 company-years covering
all 54 `ANNUAL_ROW_NOT_RESOLVED` cases: GOOGL FY2021/2022/2023, MU
FY2021/2022/2023/2024/2025, NVDA FY2021/2022/2023/2024.

**Backup**: `data/database/backups/ai_stock_agent_pre_quarterly_engine_v2_load_20260804T175744Z.duckdb`
(checksum-verified match against source). **Archived** (old v1 rows/runs,
fully preserved): `data/archive/quarterly_engine_v1_rows_replaced_20260804T175744Z.parquet`
(288 rows), `quarterly_engine_v1_runs_replaced_20260804T175744Z.parquet`
(12 runs), manifest at `quarterly_engine_v2_load_manifest_20260804T175744Z.json`.

**Result: all 12/12 COMMITTED**, one transaction per company-year (old
run+24 rows deleted, new run+24 rows inserted under
`engine_version=QUARTERLY_ENGINE_V2_ANNUAL_PRODUCTION_ANCHOR`), 0
rollbacks. Unique `REVIEW_REQUIRED` metric-years: **111 → 57** (all 54
target cases resolved: 49 `PASS` + 5 `PASS_ROUNDING_TOLERANCE`, matching
the validation proof exactly). `quarterly_extraction_runs`=45/45,
`quarterly_metric_results`=1080/1080, 0 duplicates, 0 missing lineage, 0
availability-date mismatches. `financial_metric_results`=900 (unchanged);
Annual V1 checksum unchanged. The 33 non-target company-years (incl.
MSFT/AMZN/ORCL baseline) confirmed untouched, still on
`engine_version=118_quarterly_extraction_engine_v1`.

**New approved production rule ([[D-037]])**: the quarterly engine uses
the authoritative active annual production result from the exact same
10-K accession and metric as its annual anchor. It must not independently
re-identify an annual row already resolved by the annual pipeline.

**Re-verified after an explicit mid-task stop/status-check request**: no
Python process running, no partial company-year, all counts above
reconfirmed live — task is COMPLETED, not stalled.

**Remaining out of scope for this task**: 57 REVIEW_REQUIRED cases —
`CONCEPT_NOT_RESOLVED` (21), `DIRECT_QUARTER_NOT_RESOLVED` (22, all CRWD),
`YTD_FACT_NOT_RESOLVED` (14, GOOGL/META) — not started.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## Remaining-57 quarterly REVIEW_REQUIRED root-cause re-audit — PASS (read-only, no fix implemented)
**New script `scripts/131_quarterly_remaining_57_review_required_audit.py`**
(baseline: `scripts/127`, not modified) re-classified all 57 unique
REVIEW_REQUIRED cases left after D-037's production load, checking
warehouse evidence against each case's **own blocking quarter's own 10-Q
accession** (not always the FY accession, unlike `scripts/127`) —
confirmed via `scripts/123` that `quarterly_metric_results.accession_number`
for a genuinely-unresolved row already stores that exact quarter's own
accession. Runtime 0.59s. Fail-closed 57/57 confirmed before analysis.

**By root-cause category**: `CONTEXT_OR_DURATION_NOT_RESOLVED`=38,
`AMBIGUOUS_MULTIPLE_FACTS`=8, `TRUE_SOURCE_DATA_ABSENCE`=6,
`CONCEPT_NOT_RESOLVED`=5.

**Top finding (highest-impact general-fix candidate, up to 38/57 =
67%)**: the fixed day-count duration buckets in `classify_duration()`
(`scripts/109`-`130`, unchanged) are exactly **one day too narrow** for
two very common, non-mid-year fiscal calendars — CRWD's Feb1-Apr30 Q1 is
88 days (needs `QUARTER_DURATION_MIN_DAYS`=89) in every non-leap year (24
cases, all 6 metrics × 4 years), and GOOGL's/META's Jan1-Jun30 six-month
YTD is 180 days (needs `YTD_6M_MIN_DAYS`=181) in every affected year (14
cases, capex+operating_cash_flow). In every one of the 38 cases a single,
unambiguous, correctly-tagged fact exists at exactly the expected
period_end — this is a calendar-boundary artifact, not missing or
ambiguous data, and not ticker-specific in principle.

**Other findings**: 5 cases (MU/PANW `pretax_income`) show a single
plausible standard concept existing but not found by this quarter's own
presentation-based row identification — the same shape of bug D-037 fixed
on the annual side, suggesting the same general remedy (reuse an
already-resolved concept) could apply here too. 8 `AMBIGUOUS_MULTIPLE_FACTS`
revenue cases (META/PANW) are flagged with an explicit caveat: 2 sampled
cases showed the *audit script's own* keyword list is over-broad (matches
`CostOfRevenue`, etc.), so the true ambiguity count may be lower — not
re-verified for all 8. 6 `TRUE_SOURCE_DATA_ABSENCE` cases are all NVDA's
oldest quarter (FY2020-01-26, all 6 metrics, one Q1 filing) — possibly an
older tagging convention, not yet distinguished from a keyword gap.

**No fix implemented** (task was explicitly read-only). Recommended next
step: implement and validate a generalized `classify_duration()` fix,
re-validated against the untouched 72-row MSFT/AMZN/ORCL baseline before
any production change, per the same discipline as D-037.

**Confirmed unchanged**: `quarterly_extraction_runs`=45,
`quarterly_metric_results`=1,080, `financial_metric_results`=900, unique
REVIEW_REQUIRED=57 — both databases opened read-only throughout.

**Files**: `data/quarterly_remaining_57_audit.json` (all 57 cases, full
detail), `data/quarterly_remaining_57_audit.csv`. Full detail in
`docs/LAST_CLAUDE_REPORT.md`.

## Quarterly engine V3 duration-tolerance validation — ACTIVE (in progress, not yet complete)
**New engine `scripts/132_quarterly_extraction_engine_v3_duration_tolerance.py`**
— identical to `scripts/128` (engine v2) except exactly two constants:
`QUARTER_DURATION_MIN_DAYS` 89→88, `YTD_6M_MIN_DAYS` 181→180 (no upper
boundary changed, no other logic changed — concept resolution, dimension
handling, DIRECT_QUARTER/DERIVED_FROM_YTD priority, Q4 derivation, the
D-037 annual anchor, and D-035 precision reconciliation are all copied
verbatim).

**New validation driver `scripts/133_quarterly_duration_v3_validation.py`**
— read-only, runs both engine v2 and engine v3 in-memory for all 45
company-years and classifies every difference (expected resolution of one
of the 38 audited duration cases vs. any unexpected value/basis/status
change), per the same discipline as D-037's validation (`scripts/129`).
Launched as a background process; **as of the last status check
(2026-08-05 06:50) it was ACTIVE with genuine, very recent progress —
34/45 company-years done** (confirmed via its own scratch working
directory, not yet cleaned up). Wall-clock so far ~8.4h but actual CPU
time only ~23.5 min (large gap consistent with the process having been
suspended/idle for most of that span, not stuck) — left running,
per instruction, since progress was seen within the last few seconds, far
inside the 10-minute no-progress termination threshold.

**No result yet** — `data/quarterly_duration_v3_validation.json/.csv` do
not exist yet; the 72-row baseline check, the full 1,080-row comparison,
and the count of the 38 cases resolved are all still pending. **No
database row or schema has been touched** — re-verified read-only:
`quarterly_extraction_runs`=45, `quarterly_metric_results`=1,080,
unchanged.

**Final result (background process completed on its own, exit code 0,
~8.45h wall-clock / ~24 min actual CPU time, 45/45 company-years)**:

- **Validation 1** (MSFT/AMZN/ORCL FY2024 baseline, 72 rows): **PASS**, 0 differences.
- **Validation 2** (all 45 company-years, 1,080 rows / 270 cases): row
  counts correct; **0 regressions** (no previously-resolved row broke, no
  previously-resolved value ever changed); **36 of the 38 audited
  duration cases now PASS** (0 `PASS_ROUNDING_TOLERANCE`), matching
  prediction almost exactly.
- **2 of the 38 remain REVIEW_REQUIRED**: CRWD FY2022/2023
  `pretax_income` — exact reason: `s89`'s presentation-based row
  identification fails to find a `pretax_income` row at all in that
  quarter's own 10-Q ("row not found"), a **concept-resolution** failure
  that occurs *before* duration is ever checked. This reveals these 2
  cases were mis-attributed to the "duration boundary" category by the
  prior audit (`scripts/131`) — a keyword-search false signal, not
  reached by the actual engine logic. Not corrected in that audit file
  (read-only task); documented here instead.
- **18 additional flagged differences, all individually reviewed and
  explained as benign, none a data-correctness issue**: 8 are legitimate
  same-case cascades (CRWD Q2 `operating_cash_flow`/`capex` resolving as
  a side effect of their own Q1 fix, mis-flagged by an overly narrow
  validation heuristic); 10 are PANW Q3 basis-only improvements
  (`DERIVED_FROM_YTD` → `DIRECT_QUARTER`) where the **value is bit-for-bit
  identical** in every case — these are outside the original 38-case
  scope (never REVIEW_REQUIRED) but literally violate the task's strict
  "no previously-resolved basis changes" requirement, which is why the
  overall run is reported **FAIL** despite zero incorrect data anywhere.
- **Confirmed unchanged**: `quarterly_extraction_runs`=45,
  `quarterly_metric_results`=1,080, `financial_metric_results`=900 — no
  production row or schema touched; this was a pure read-only proof.
- **No production load performed.** Next step: decide with the user
  whether a same-value basis-only change (the 10 PANW cases) is an
  acceptable side effect before loading the 36 resolved duration cases,
  or split the fix into two smaller increments (quarter-boundary only,
  then YTD-boundary separately).

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## Quarterly engine V3 duration-tolerance production load — PASS (15/15 company-years committed)
**New script `scripts/134_quarterly_engine_v3_production_load.py`** —
loaded the already-validated engine-v3 (duration-tolerance) results into
production for the 15 company-years whose results changed under v3,
derived (not hardcoded) from `data/quarterly_duration_v3_validation.json`
(scripts/133's saved output, not re-run): 11 from the 36 resolved
duration cases (CRWD FY2022/2023/2024/2026, GOOGL FY2021/2022/2023/2025,
META FY2021/2022/2023) plus PANW FY2021/2022/2023/2025 for the 10
approved basis-only improvements.

**Approved decision applied ([[D-038]])**: a deterministic `DIRECT_
QUARTER` fact with an identical value to an existing `DERIVED_FROM_YTD`
result is preferred; the basis-only change is accepted as improved
lineage, not a financial-result change. Re-verified per-case at commit
time (all 10 PANW Q3 rows carry an identical value to what they replaced).

**Result: all 15/15 COMMITTED**, 0 rollbacks. **36 of 38 audited duration
cases now `PASS`/`PASS_ROUNDING_TOLERANCE`** in production; **2 remain
`REVIEW_REQUIRED`** (CRWD FY2022/2023 `pretax_income` — a concept-
resolution failure unrelated to duration, out of scope). Unique
`REVIEW_REQUIRED` metric-years: **57 → 21**, confirmed by direct
re-query (not forced). `quarterly_extraction_runs`=45/45,
`quarterly_metric_results`=1,080/1,080, 0 duplicates, 0 missing lineage,
0 availability-date mismatches. `financial_metric_results`=900
(unchanged); Annual V1 checksum unchanged. `engine_version` breakdown:
`QUARTERLY_ENGINE_V3_DURATION_TOLERANCE`=15,
`118_quarterly_extraction_engine_v1`=21,
`QUARTERLY_ENGINE_V2_ANNUAL_PRODUCTION_ANCHOR`=9 (sums to 45).

**Backup**: `data/database/backups/ai_stock_agent_pre_quarterly_engine_v3_load_20260805T041620Z.duckdb`
(checksum-verified). **Archive**: `data/archive/quarterly_engine_v3_{rows,runs}_replaced_20260805T041620Z.parquet`
(360 rows / 15 runs) + manifest. Runtime 238.22s.

**Remaining out of scope**: 21 REVIEW_REQUIRED cases — 2 CRWD
`pretax_income` (concept-resolution), plus the previously-known
`CONCEPT_NOT_RESOLVED`/`AMBIGUOUS_MULTIPLE_FACTS`/`TRUE_SOURCE_DATA_
ABSENCE` cases from `scripts/131`'s audit not touched by this fix — not
started.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## Remaining-21 quarterly REVIEW_REQUIRED root-cause audit — PASS (read-only, no fix implemented)
**New script `scripts/135_quarterly_remaining_21_review_required_audit.py`**
(baseline: `scripts/131`, not modified) re-audited all 21 unique
REVIEW_REQUIRED cases left after the v3 duration-tolerance load. Runtime
0.76s. Fail-closed 21/21 confirmed before analysis; none of the 36
already-resolved duration cases reappeared.

**By ticker**: PANW=7, NVDA=6, META=3, MU=3, CRWD=2. **By metric**:
revenue=9, pretax_income=8, plus 1 each of capex/income_tax_expense/
operating_cash_flow/operating_income (all NVDA).

**Top finding — CONCEPT_REUSE_CANDIDATE, 15/21 (71%)**: for CRWD (2),
META (3), MU (3), and PANW (7) — the exact concept already resolved by
the authoritative annual production result for the same metric/fiscal
year (or a sibling/adjacent-year quarter) exists as a real, correctly-
valued fact in the blocking quarter's own 10-Q accession. `s89`'s
presentation-based row identification, run independently per-filing,
simply doesn't find it there. This mirrors the exact shape of bug D-037
already fixed on the annual side — a concept-reuse fallback for Q1/Q2/Q3
resolution (try the already-known-good annual/sibling/adjacent-year
concept before failing) is the single highest-impact next general fix
identified, resolving up to 15/21 cases.

**Tightened revenue-concept analysis** (exact allow-list, excluding
`CostOfRevenue`/`ContractWithCustomerLiabilityRevenueRecognized`/
`RevenueRemainingPerformanceObligation*`): 7 of 9 revenue cases narrow
from "apparently ambiguous" (broad `"Revenue"` substring search) to
exactly one standard candidate. The 1 remaining apparent conflict (META
FY2022 revenue) was traced to this audit script's own duration-bucket-
union check conflating a direct-quarter value with a 9-month-YTD value
for the *same* concept — not a real ambiguity; concept reuse independently
confirms a single concept.

**NVDA oldest-filing finding (6 cases, FY2020-01-26) — more specific than
"data absent"**: full inspection of accession `0001045810-19-000079`
found `xbrl_facts`/`xbrl_contexts`/`xbrl_units`/`xbrl_concepts` all
**literally 0** in the warehouse for this accession, across **both**
recorded load attempts, each completing in ~0.08s (far too fast for a
genuine Arelle parse) — yet `warehouse_runs.status='PASS'` both times.
This is a **warehouse-ingestion false-positive**, not confirmed absence
in the real SEC filing (which is a valid 10-Q per `sec_filings`).
Re-warehousing (out of scope here) would be needed to determine the true
content.

**No fix implemented** (task was explicitly read-only). **Confirmed
unchanged**: `quarterly_extraction_runs`=45, `quarterly_metric_results`=
1,080, `financial_metric_results`=900, unique REVIEW_REQUIRED=21 — both
databases opened read-only throughout.

**Files**: `data/quarterly_remaining_21_audit.json` (all 21 cases, full
detail), `data/quarterly_remaining_21_audit.csv`. Full detail in
`docs/LAST_CLAUDE_REPORT.md`.

## Quarterly engine V4 point-in-time concept-reuse validation — PASS (read-only proof, not loaded)
**New engine `scripts/136_quarterly_extraction_engine_v4_point_in_time_
concept_reuse.py`** — identical to `scripts/132` (engine v3) except one
addition: when the primary presentation-based resolver fails for Q1/Q2/Q3,
attempt a fallback using ONLY point-in-time-safe evidence (an earlier
same-fiscal-year quarter already resolved this run, or the nearest
resolved prior-fiscal-year 10-K, walking back as needed) — never the same
year's own future 10-K, never a later quarter/year, never any filing
dated after the blocking quarter's own filing_date. The concept NAME may
be reused; the VALUE always comes fresh from the blocking quarter's own
exact accession via the unchanged fact-selection safeguards.

**New validation driver `scripts/137_quarterly_concept_reuse_v4_
validation.py`** — read-only, runs v3 and v4 in-memory for the 3
MSFT/AMZN/ORCL FY2024 baselines plus the 13 unique company-years covering
all 15 CONCEPT_REUSE_CANDIDATE cases (derived from `scripts/135`'s saved
audit, not hardcoded; family counts cross-checked exactly). Runtime
238.33s.

**Result**: Validation 1 (72-row baseline) — 0 differences, fallback
confirmed inactive on all 72 rows. Validation 2 — **11 of 15 (73%)
resolved** (9 `PASS` + 2 `PASS_ROUNDING_TOLERANCE`); **4 correctly remain
`REVIEW_REQUIRED`**: CRWD FY2022 `pretax_income` (its only prior-year 10-K
is one of the 5 accessions excluded from the Annual V1 universe), MU
FY2021 `pretax_income` and PANW FY2021 `pretax_income`/`revenue` (each is
that ticker's earliest fiscal year in the database — no prior 10-K exists
at all). All 4 share the same structural cause: no earlier evidence
exists to reuse — the point-in-time rule working exactly as designed,
not a policy weakness. **0 regressions, 0 future-data violations, 0
unexpected findings** — every reuse independently verified
`source_filing_date <= blocking_filing_date`.

**Not loaded to production** — this was explicitly a read-only proof.
Next step: load the 11 resolved cases following the same transactional
discipline as `scripts/130`/`134` (D-037/D-038).

**Files**: `data/quarterly_concept_reuse_v4_validation.json` (full detail,
all 15 cases), `data/quarterly_concept_reuse_v4_validation.csv`. Full
detail in `docs/LAST_CLAUDE_REPORT.md`.

## Quarterly engine V4 production load — PASS (10/10 company-years committed)
**New script `scripts/138_quarterly_engine_v4_production_load.py`** —
loaded the already-validated engine-v4 (point-in-time-safe concept reuse)
results into production for the 10 company-years covering the 11
resolved cases, derived (not hardcoded) from
`data/quarterly_concept_reuse_v4_validation.json` (scripts/137's saved
output, not re-run): CRWD FY2023; META FY2022/2023/2024; MU FY2022/2023;
PANW FY2022/2023/2024/2025. Explicitly excluded and confirmed unchanged:
CRWD FY2022, MU FY2021, PANW FY2021 (2 metrics) `pretax_income`/`revenue`
— each ticker's earliest fiscal year with no point-in-time-safe concept
source, per D-039.

**New approved decision D-039**: the quarterly engine may reuse a
concept NAME (never a value) for Q1/Q2/Q3 resolution only from
point-in-time-safe evidence — an earlier same-fiscal-year quarter already
resolved, or the nearest resolved prior-fiscal-year 10-K — always
requiring source `filing_date <= blocking filing_date`; the same fiscal
year's own 10-K, a later quarter, a later fiscal year, and any
future-dated filing are always forbidden.

**Result: all 10/10 COMMITTED**, 0 rollbacks. **11 of 15 CONCEPT_REUSE_
CANDIDATE cases now `PASS`/`PASS_ROUNDING_TOLERANCE`** in production (9
PASS + 2 PASS_ROUNDING_TOLERANCE). **4 excluded cases confirmed still
REVIEW_REQUIRED, unchanged** (4/4 exact match); **6 NVDA cases confirmed
still REVIEW_REQUIRED, unchanged** (6/6 exact match, out of scope).
Unique REVIEW_REQUIRED metric-years: **21 → 10**, confirmed by direct
re-query. `quarterly_extraction_runs`=45/45, `quarterly_metric_results`=
1,080/1,080, 0 duplicates, 0 missing lineage, 0 availability-date
mismatches, **0 future-data violations** (re-verified twice: Phase 1 and
at each commit). `financial_metric_results`=900 (unchanged); Annual V1
checksum unchanged. `engine_version` breakdown:
`QUARTERLY_ENGINE_V4_POINT_IN_TIME_CONCEPT_REUSE`=10,
`QUARTERLY_ENGINE_V3_DURATION_TOLERANCE`=9,
`QUARTERLY_ENGINE_V2_ANNUAL_PRODUCTION_ANCHOR`=7,
`118_quarterly_extraction_engine_v1`=19 (sums to 45).

**Backup**: `data/database/backups/ai_stock_agent_pre_quarterly_engine_v4_load_20260805T055024Z.duckdb`
(checksum-verified). **Archive**: `data/archive/quarterly_engine_v4_{rows,runs}_replaced_20260805T055024Z.parquet`
(240 rows / 10 runs) + manifest. Runtime 88.03s.

**Remaining out of scope**: 10 REVIEW_REQUIRED cases — 4 earliest-year
cases with no possible point-in-time-safe fix, plus 6 NVDA
warehouse-ingestion cases (would need re-warehousing, not attempted) —
not started.

Full detail in `docs/LAST_CLAUDE_REPORT.md`.

## NVDA 2019 Q1 warehouse-ingestion bug — PASS (scratch-only proof, production untouched)
**New scripts `scripts/139_corrected_warehouse_loader_entry_point_
detection.py`** (reusable, general, not ticker-specific corrected
loader) **and `scripts/140_nvda_2019q1_rewarehouse_proof.py`**
(orchestration/proof, Phases 1/2/4). `scripts/121` not modified — Phase 2
reused its own unchanged `load_and_warehouse_one_10q()` with only its
`WAREHOUSE_DB_PATH` module attribute reassigned at runtime to redirect
writes to the scratch DB (`data/database/nvda_2019_q1_warehouse_proof.duckdb`),
then restored.

**Exact root cause, confirmed by direct code inspection**: `scripts/121`
(1) always uses the SEC filing index's `primaryDocument` as the sole
Arelle entry point, with no check for whether it's Inline XBRL, and (2)
sets `status="PASS"` the instant Arelle's Session exits without an
exception — **never checking whether any row was actually extracted**.
NVDA's Q1 FY2020 10-Q (accession `0001045810-19-000079`, filed
2019-05-16) predates NVDA's own transition to Inline XBRL: its
`primaryDocument` (`nvda2020q110q.htm`) is plain HTML with zero embedded
facts, while the real data sits in a separate, complete, valid
`nvda-20190428.xml` traditional-XBRL instance document the loader never
looks at. Confirmed reproducible: Phase 2 replayed the exact unchanged
existing path against the scratch DB and got the same false
`PASS`-with-all-zero-counts result. **A general format-transition bug,
not NVDA-specific or data-absence.**

**Corrected loader validated on two filings**:
- **Broken accession** (`0001045810-19-000079`): now extracts **654
  facts, 134 contexts, 5 units, 711 concepts** — `status=PASS`. **All 6
  target metrics have plausible, correctly-tagged facts** (`us-gaap:
  Revenues`, `OperatingIncomeLoss`, `IncomeLossFromContinuingOperations
  BeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethod
  Investments`, `IncomeTaxExpenseBenefit`, `NetCashProvidedByUsedIn
  OperatingActivities`, `PaymentsToAcquirePropertyPlantAndEquipment`) at
  the correct current-period context. No metric genuinely absent.
- **Known-good baseline** (derived from the database, not hardcoded:
  NVDA's next 10-Q, `0001045810-19-000144`, Q2 FY2020): corrected loader
  reproduces the production warehouse's existing counts **exactly**
  (920/186/5/875) — **0 regression**.

**Confirmed unchanged**: `xbrl_facts` for the broken accession in the
**real** `data/database/xbrl_warehouse_proof.duckdb` still 0;
`ai_stock_agent.duckdb` counts (45/1,080/900) unchanged. Zero network
calls. Runtime 6.22s.

**Not yet applied to production** — this was explicitly a scratch-only
proof. Next step: build a production version of the corrected loader,
re-warehouse only this one accession into the real warehouse with a full
backup/checksum cycle, then re-run engine v4 for NVDA FY2020-01-26 to see
if its 6 metrics now resolve.

**Files**: `data/nvda_2019q1_rewarehouse_proof.json` (full detail),
`data/nvda_2019q1_rewarehouse_proof.csv`. Full detail in
`docs/LAST_CLAUDE_REPORT.md`.

## Global warehouse-ingestion audit, task-marker foundation, warehouse repair, and NVDA FY2020 quarterly production load — PASS (TASK_141 through TASK_146)

Since the scratch proof above, six further tasks completed in sequence,
each preserved as its own versioned script and report:

- **TASK_141** (`scripts/141`) — read-only global integrity audit of all
  185 registered 10-K/10-Q accessions. Found exactly **one** defective
  accession project-wide: NVDA `0001045810-19-000079`
  (`FALSE_PASS_ZERO_CONTENT`) — the same bug root-caused above. All other
  184 accessions valid (183 `VALID_INLINE_XBRL`, 1
  `VALID_TRADITIONAL_XBRL`).
- **TASK_142** — reconciled a timestamp defect discovered in TASK_141's
  own saved evidence files (`completed_at` earlier than `started_at`).
  Root-caused via independent filesystem `CreationTime` metadata to a
  PowerShell `Get-Date -Format ...Z` local/UTC conversion bug, not a
  fabrication of the underlying audit work. Classified
  `VERIFIED_RESULTS_TIMESTAMP_DEFECT`; TASK_141's 185-accession findings
  independently re-derived and **accepted in full**.
- **TASK_143** (D-040) — built `scripts/142_task_marker_guard.py`, a
  reusable, fail-closed task-marker utility (`start_task`/`finish_task`/
  `fail_task`/`validate_task_evidence`) with UTC-only internally-sourced
  timestamps and atomic writes, mandatory for all future `TASK_NNN` work.
  Validated with 12/12 required tests (`scripts/143`); used on itself.
- **TASK_144** (D-041) — promoted the scratch-proven corrected loader to
  production (`scripts/144_warehouse_loader_v2_production.py`) and
  repaired the one defective NVDA accession in the real warehouse
  (`scripts/145`). `xbrl_facts` 225,126 → 225,780 (delta=654). Full
  backup/checksum/archive/atomic-transaction discipline; PASS.
- **TASK_145** — read-only quarterly engine V4 proof (`scripts/146`) for
  NVDA fiscal-year-end 2020-01-26 against the repaired warehouse content:
  all 6 previously-REVIEW_REQUIRED metrics resolved to exact PASS. Not
  loaded into production in this task.
- **TASK_146** — loaded that proof into quarterly production
  (`scripts/147`), see the "Last updated" line above for full detail.

**Current overall state**: warehouse-ingestion universe is 100% clean
(185/185 valid accessions); project-wide unique REVIEW_REQUIRED
metric-years is **4** (down from a peak of 10), all 4 with no
point-in-time-safe fix by construction (D-039) and not in scope for any
task so far. All future `TASK_NNN` work must use
`scripts/142_task_marker_guard.py` for STARTED/RESULT evidence (D-040).

## Quarterly engine V5 — standard GAAP concept allow-list fallback — read-only PROOF, PASS (TASK_147)

**New engine `scripts/148_quarterly_engine_v5_standard_gaap_fallback.py`**
— identical to `scripts/136` (engine V4) except one addition: a fixed,
versioned tier-3 concept-resolution fallback
(`STANDARD_GAAP_ALLOW_LIST_V1`), tried only when BOTH the primary
presentation-based resolver AND the point-in-time-safe concept-reuse
fallback (V4) fail for Q1/Q2/Q3. Allow-list: 6 revenue concepts
(`us-gaap:Revenues`, `RevenueFromContractWithCustomer{Excluding,
Including}AssessedTax`, `SalesRevenue{Net,GoodsNet,ServicesNet}`), 2
pretax_income concepts
(`IncomeLossFromContinuingOperationsBeforeIncomeTaxes{ExtraordinaryItems
NoncontrollingInterest, MinorityInterestAndIncomeLossFromEquityMethod
Investments}`). Only ever supplies a concept_qname candidate — the value
is always re-selected fresh from the blocking quarter's own exact
accession via the unchanged fact-selection pipeline. Fails closed
(REVIEW_REQUIRED) on zero or ambiguous (genuinely-different-value)
matches.

**New validation driver `scripts/149_standard_gaap_fallback_validation.py`**
— read-only, target/control company-years derived directly from
production (not hardcoded). **Validation A**: all 4 remaining
REVIEW_REQUIRED cases (CRWD FY2022 pretax_income, MU FY2021
pretax_income, PANW FY2021 pretax_income + revenue — 3 distinct
company-years) **resolved to exact PASS**, each via the new tier
activating only at Q1 (no earlier evidence existed at all — each
ticker's earliest fiscal year in the DB, or excluded-from-Annual-V1
prior 10-K), with Q2/Q3 cascading through the existing unchanged tier-2
mechanism. Re-running V4 alone against the same accessions confirms all
4 still REVIEW_REQUIRED, isolating the fix to the new tier specifically.
**Validation B**: 96-row regression control (MSFT/AMZN/ORCL FY2024 +
NVDA FY2020) fully identical between V4 and V5 (value, extraction_basis,
reconciliation_status, availability_date) — **0 regressions, 0 fallback
activations on any control row**. **Validation C**: all target-case
safety checks passed (value from blocking 10-Q only, no future filing,
no same-year 10-K, no comparative fact, exactly one allow-listed
`us-gaap:` concept selected, full-year quarterly sum reconciles to
annual with diff=0.00).

**Not loaded to production** — explicitly a read-only proof. **V5
appears safe for production adoption**: purely additive, 0 activations
on any already-resolved control row, all 4 targets resolved cleanly.
Both databases confirmed unchanged (`quarterly_extraction_runs`=45,
`quarterly_metric_results`=1,080, `financial_metric_results`=900,
unique REVIEW_REQUIRED=4 unchanged by this proof; warehouse
`xbrl_facts`=225,780 unchanged). TASK_147 task-marker evidence
self-validated `valid=True`, 0 failure categories.

**Exact next step (superseded by TASK_148 below, which extended this
96-row sample to the full 45-company-year universe)**: a production
load of these 4 proof rows into `quarterly_metric_results` — still not
started as of TASK_148's completion either.

**Files**: `data/standard_gaap_fallback_validation.json` (full detail:
allow-list, all 4 target cases with lineage/rejected candidates, all 96
control rows, point-in-time/reconciliation checks, database
before/after counts), `data/standard_gaap_fallback_validation.csv`. Full
detail in `docs/STANDARD_GAAP_FALLBACK_VALIDATION.md`.

## Quarterly engine V5 — final release regression, 45/45 company-years — read-only PROOF, PASS (TASK_148)

**New script `scripts/150_v5_final_release_regression.py`** — the one
final release regression for engine V5 (`scripts/148`) across **all 45**
authoritative company-years currently in quarterly production (not a
96-row sample). Each company-year's V5 run is a genuine OS subprocess
with a real, killable 45-second wall-clock timeout (`subprocess.run(...,
timeout=45)`), not an in-process soft timer — enforceable exactly as
required. Every resulting row is compared directly against the CURRENT
ACTIVE production row for that exact (ticker, fiscal_year_end,
metric_name, fiscal_quarter). Engine V4 (`scripts/136`) is never
invoked. The main JSON/CSV outputs are atomically rewritten (temp-file +
`os.replace`) after every single company-year as a running checkpoint,
with one progress line printed per company-year — both controls
independently re-confirmed live mid-run.

**Result: PASS, all 45/45 company-years, no early stop.** The same 4
target metric-year cases from TASK_147 (CRWD 2022-01-31 pretax_income,
MU 2021-09-02 pretax_income, PANW 2021-07-31 pretax_income + revenue —
16 rows total, 4 quarters × 4 metric-year cases) resolved cleanly, each
passing every required safety check (value from the exact blocking
10-Q, concept literally in `STANDARD_GAAP_ALLOW_LIST_V1`, no future
filing, no comparative fact, exact full-year reconciliation to annual,
complete lineage). **The other 41 company-years (984 rows) were 100%
byte-identical to current production** — 0 unexpected differences
anywhere in the entire 45-company-year universe, not just the earlier
96-row sample.

**Global checks, all true**: exactly 1,080 V5 rows produced (45×24),
every company-year has 24 rows, 0 duplicate keys, 0 missing lineage, 0
availability-date mismatches (vs. `sec_filings.filing_date`), 0
future-data violations, all 4 target cases resolve, no other production
row changed, expected REVIEW_REQUIRED after a future load = **0**, both
databases unchanged, exactly 45 engine invocations, V4 never rerun.

**Performance**: active execution 583.02s (~9.7 min, slightly over the
task's 3–8 min estimate — attributable to each company-year's fresh
subprocess cold-start cost, the deliberate price of a real killable
timeout rather than a soft one), wall-clock 584.18s. Slowest 5: MSFT
2024-06-30 (23.26s), GOOGL 2025-12-31 (18.75s), CRWD 2026-01-31
(18.32s), MSFT 2020-06-30 (18.01s), GOOGL 2023-12-31 (18.00s) — none
exceeded the 30s soft-warning threshold or the 45s hard limit.

**Not loaded to production** — explicitly a read-only proof. **V5 is
now regression-clean across the entire authoritative production
universe and appears fully ready for production adoption.**
TASK_148 task-marker evidence self-validated `valid=True`, 0 failure
categories.

**Exact next step (completed below)**: a production load of the 4
now-resolved target metric-year cases (16 rows total) into
`quarterly_metric_results` — executed and frozen, see the "Last
updated" line above and the new section below.

**Files**: `data/v5_final_release_regression.json` (full detail: all 45
company-year results, all 16 changed rows with full lineage, family
safety checks, performance report, database before/after counts),
`data/v5_final_release_regression.csv`. Full detail in
`docs/V5_FINAL_RELEASE_REGRESSION.md`.

## Quarterly Data V1 FROZEN — engine V5 production load executed and verified (D-042)

**New script `scripts/151_v5_quarterly_production_load.py`**
(`--check-only` / `--execute`) built the production load for the 3
remaining target company-years. Two bugs were found and fixed during
real dry-run/execute attempts before a successful load, each diagnosed
read-only-first and independently re-verified before any further write
was attempted:
- **Self-PID-lock detection defect**: `check_execution_preconditions()`
  re-checked the PID lock this same process had just acquired and
  mistook itself for another active writer. Fixed by adding an
  `exclude_pid` parameter to `check_pid_lock_status()`. A related
  malformed-lock-file fail-open defect (missing `"pid"` key silently
  treated as "free") was fixed at the same time — now fails closed.
- **`KeyError: 'q1_accession'`**: the saved TASK_148 regression
  artifact's `company_year_results` entries never carried
  `q1_accession`/`q2_accession`/`q3_accession`/`fy_accession` fields (only
  `ticker`/`fiscal_year_end`/`run_id`/comparison fields) — `scripts/150`
  used those fields locally to build its own subprocess command but
  never persisted them. Fixed by adding
  `enrich_target_company_years_with_accessions()`, which reads all four
  accession fields from `quarterly_extraction_runs` (the same table
  already used for `run_id`), cross-checks `run_id`, and fails closed on
  any missing field. Both bugs were caught **before** any database write
  occurred in both cases (confirmed via SHA-256 hash equality between
  production and each failed run's own pre-load backup).

**`--execute` result: PASS.** `f5a8de2c...` (CRWD 2022-01-31),
`dae1c3f9...` (MU 2021-09-02), and `f72f4da0...` (PANW 2021-07-31) were
replaced as complete company-year units (72 rows total, not just the 16
changed) by 3 new `QUARTERLY_ENGINE_V5_STANDARD_GAAP_ALLOW_LIST` runs
(`f610bc3a...`, `082ec4d1...`, `ab9b7e8e...`) in one atomic transaction.
Full backup (`ai_stock_agent_pre_v5_production_load_20260806T080759Z.duckdb`,
SHA-256-verified) and Parquet archive of the 3 old runs + 72 old rows
beforehand. Fresh engine re-verification per company-year: CRWD 8.41s,
MU 13.69s, PANW 12.62s (all well under the 45s hard timeout).

**Final freeze verification — independently re-derived directly from
the live databases** (not merely the load script's own self-report):

| Check | Result |
|---|---|
| `quarterly_extraction_runs` | 45 |
| `quarterly_metric_results` | 1,080 |
| `financial_metric_results` | 900 |
| unique REVIEW_REQUIRED | **0** |
| Every company-year has exactly 24 rows | ✓ (0 exceptions) |
| Duplicate quarterly keys | 0 |
| Missing lineage | 0 |
| Availability mismatches | 0 |
| Future-data violations | 0 (spot-checked directly from committed `lineage_json`: 4 `STANDARD_GAAP_ALLOW_LIST` activations, 8 point-in-time-reuse activations, 0 violations) |
| Annual V1 checksum | unchanged |
| XBRL warehouse facts | 225,780 (unchanged) |
| 3 target runs' `engine_version` | `QUARTERLY_ENGINE_V5_STANDARD_GAAP_ALLOW_LIST` (all 3) |

**Standing declarations (D-042, recorded in `docs/DECISIONS_LOG.md`)**:
Quarterly Data V1 is frozen. `QUARTERLY_ENGINE_V5_STANDARD_GAAP_ALLOW_LIST`
is the authoritative quarterly extraction engine. Annual Data V1 and
Quarterly Data V1 are the approved inputs for the next project stage.
No further data-engine changes are permitted without a new version and
a full regression.

**Files**: `data/quarterly_data_v1_release_manifest.json` (the freeze
record), `data/v5_production_load_result.json`,
`data/v5_production_load_checkpoint.json`,
`data/archive/v5_production_load_manifest.json`,
`logs/v5_production_load.log`. Full detail in
`docs/V5_PRODUCTION_LOAD_BUILD.md` / `docs/LAST_CLAUDE_REPORT.md`.

**Recommended next project stage**: with both Annual Data V1 and
Quarterly Data V1 frozen and verified, the natural next stage is
building the **derived-metrics / valuation layer** on top of this
locked fundamentals base (e.g., growth rates, margins, returns,
point-in-time-safe backtesting datasets) — per `CLAUDE.md`'s own
stated purpose ("personal stock-analysis and investment-decision
system") — rather than further extraction-engine work, which is now
explicitly frozen pending a new version + regression.

## Derived Metrics V1 FROZEN — production load executed and verified (D-043)

`scripts/153_derived_metrics_v1_load.py --execute` succeeded, creating
`derived_metric_results` in `data/database/ai_stock_agent.duckdb` and
loading it for all 9 approved tickers with exactly 2 approved metrics
(`operating_margin`, `revenue_yoy_growth`) at `annual` and `quarterly`
frequency — the exact same formulas, point-in-time rules, and lineage
logic proven single-ticker (`scripts/152`) and regression-validated
9-ticker (`scripts/153 --check-only`) in the two prior tasks, with the
`fiscal_quarter`/`PRIMARY KEY` schema defect and the 90/315 documentation
error both already found and fixed before this load ran.

**Final read-only verification — independently re-derived directly from
the live database** (not merely the load script's own self-report):

| Check | Result |
|---|---|
| Load status | `PASS` |
| `derived_metric_results` exists | ✓ |
| Total rows | **405** |
| Annual rows | **81** |
| Quarterly rows | **324** |
| Distinct tickers | **9** (AMZN, CRWD, GOOGL, META, MSFT, MU, NVDA, ORCL, PANW) |
| Duplicate primary keys | 0 |
| NULLs in any required (`NOT NULL`) column | 0 |
| Annual rows with a non-NULL `fiscal_quarter` | 0 (correctly always NULL) |
| Quarterly rows with `fiscal_quarter` outside 1–4 | 0 |
| `operating_margin` counts | annual=45, quarterly=180 (correct) |
| `revenue_yoy_growth` counts | annual=36, quarterly=144 (correct) |
| Distinct `derived_metric` values | exactly `{operating_margin, revenue_yoy_growth}` — no others |
| Annual Data V1 checksum | unchanged |
| `quarterly_extraction_runs` | 45 (unchanged) |
| `quarterly_metric_results` | 1,080 (unchanged) |
| `financial_metric_results` | 900 (unchanged) |
| unique REVIEW_REQUIRED | 0 (unchanged) |

**Standing declarations (D-043, recorded in `docs/DECISIONS_LOG.md`)**:
Derived Metrics V1 is frozen. 405 approved derived observations across
exactly 2 approved metrics (`operating_margin`, `revenue_yoy_growth`).
No future changes to Derived Metrics V1 (new metrics, formula changes,
or data reloads) are permitted without a new version and full
validation — the same discipline already applied to Annual Data V1 and
Quarterly Data V1.

**Files**: `data/derived_metrics_v1_load_result.json`,
`data/derived_metrics_v1_release_manifest.json` (the freeze record).
Full detail in `docs/LAST_CLAUDE_REPORT.md`.

**Current overall state (superseded — see 2026-08-07 Historical Prices
V1 section below)**: Annual Data V1, Quarterly Data V1, and Derived
Metrics V1 are all frozen. The fundamentals + derived-metrics base for
all 9 approved tickers is complete and locked.

## 2026-08-07 — Historical Prices V1 frozen (D-045)

`historical_prices_daily` was built and loaded for all 9 approved
tickers in a single closed release task: preflight → one `--execute` →
independent post-load verification → freeze. The production load was
run exactly once via
`.\.venv\Scripts\python.exe .\scripts\158_historical_prices_v1_load.py --execute`,
orchestrated end-to-end by `scripts/159_historical_prices_v1_release.py`.

**Result**: **14,913 rows** (9 tickers × 1,657 daily observations
each), **2020-01-02 through 2026-08-06**, rebuilt from scratch from the
already-saved raw Yahoo JSON responses (never from a prior proof's own
CSV output as a shortcut). Every row carries Yahoo's original
`open`/`high`/`low`/`close`/`adj_close`/`volume`/`dividend`/`split_ratio`
fields unmodified (Rule A, D-044) plus the reconstructed nominal
`open`/`high`/`low`/`close` (Rule C, D-044), full source lineage
(`source_raw_file`, `source_raw_sha256`), and
`price_policy_version='HISTORICAL_PRICE_POLICY_V1'` on every row.

| Check | Result |
|---|---|
| `historical_prices_daily` exists | ✓ |
| Total rows | **14,913** |
| Distinct tickers | **9** |
| Rows per ticker | **1,657** each |
| Date range | 2020-01-02 → 2026-08-06 |
| Duplicate `(ticker, price_date)` keys | 0 |
| Missing required price fields | 0 |
| Negative/non-positive prices | 0 |
| Negative volume | 0 |
| OHLC relationship violations | 0 |
| Reconstructed nominal OHLC violations | 0 |
| `price_policy_version` correct on every row | ✓ |
| Source lineage present on every row | ✓ |
| Split events match approved 9-company proof | ✓ |
| Dividend counts match approved 9-company proof | ✓ |
| NVDA/GOOGL/PANW reconstructed prices match Historical Price Policy V1 proof | ✓ |
| `financial_metric_results` | 900 (unchanged) |
| `quarterly_extraction_runs` | 45 (unchanged) |
| `quarterly_metric_results` | 1,080 (unchanged) |
| `derived_metric_results` | 405 (unchanged) |
| unique REVIEW_REQUIRED | 0 (unchanged) |
| Every pre-existing production-table fingerprint | unchanged |
| Annual Data V1 checksum | unchanged |

**Standing declarations (D-045, recorded in `docs/DECISIONS_LOG.md`)**:
Historical Prices V1 is frozen. 9 companies, 14,913 validated daily
observations, 2020-01-02 through 2026-08-06. Yahoo historical chart
data is the approved V1 market-price source for the current 9-company
universe. Historical Price Policy V1 / D-044 governs all use of these
prices. No changes to Historical Prices V1 without a new version and
full validation.

**Files**: `data/historical_prices_v1_release_manifest.json` (the
freeze record), `data/historical_prices_v1_build_validation.json`,
`data/historical_prices_v1_release_task_result.json`. Full detail in
`docs/HISTORICAL_PRICES_V1_BUILD.md`, `docs/LAST_CLAUDE_REPORT.md`.

**Current overall state (superseded — see 2026-08-08 Valuation V1
section below)**: Annual Data V1, Quarterly Data V1, Derived Metrics
V1, and Historical Prices V1 are all frozen.

## 2026-08-08 — Valuation V1 frozen (D-046)

`valuation_v1_per_share_inputs` was built and loaded for all 9
approved tickers, closing the valuation-data gap flagged in the
Scoring Model V1 blueprint (`docs/SCORING_MODEL_V1_BLUEPRINT.md`).
Full proof chain run in one closed task: inventory of filing-based
per-share XBRL concepts → micro proof (MSFT/NVDA/AMZN, 2 fiscal years
each) → full 45-company-year proof → historical P/E proof
(MSFT/NVDA/AMZN) → in-memory load proof → production load (run exactly
once, `scripts/160_valuation_v1_per_share_inputs.py --execute`) →
independent post-load verification.

**Chosen basis**: reported diluted EPS (`us-gaap:EarningsPerShareDiluted`),
resolved directly from the consolidated (non-dimensional),
full-fiscal-year fact in each company's own already-locked 10-K —
**no new filing downloaded, no external/analyst data used**. Shares
outstanding (diluted weighted-average or period-end) was evaluated per
the task's requirement but deliberately **not stored in production**:
historical P/E only needs a per-share earnings figure, not a share
count, so shares data has no required use once diluted EPS is
available directly. It was used only transiently, during resolution,
as a cross-check (`net_income / diluted weighted-average shares`
compared against reported diluted EPS — max observed difference across
all 45 company-years: $0.03).

**Real defect found and fixed before production load**: the first
historical-P/E proof attempt paired Yahoo `close` (retroactively
split-adjusted for later splits, per Historical Price Policy V1 / D-044
Rule C) with as-reported diluted EPS (never split-adjusted), silently
distorting P/E for any company-year preceding a later split — NVDA's
2024-02-21 filing (before the 2024-06-10 10:1 split) produced an
implausible P/E of 5.66 instead of the correct 56.56. Fixed by using
`nominal_close` (the reconstructed original-scale price) for any
calculation paired with as-reported EPS.

| Check | Result |
|---|---|
| `valuation_v1_per_share_inputs` exists | ✓ |
| Company-years resolved | **45 / 45** |
| Unavailable / Ambiguous / REVIEW_REQUIRED | 0 / 0 / 0 |
| Duplicate `(ticker, fiscal_year)` keys | 0 |
| Missing lineage (accession/filing_date/availability_date/source concept) | 0 |
| `availability_date = filing_date` on every row | ✓ |
| Micro proof (MSFT/NVDA/AMZN × 2 fiscal years) | PASS, max cross-check diff $0.00 |
| Historical P/E proof (MSFT/NVDA/AMZN) | PASS — 35.84 / 56.56 / 29.33, reproducible, re-derived identically post-load |
| `financial_metric_results` | 900 (unchanged) |
| `quarterly_extraction_runs` | 45 (unchanged) |
| `quarterly_metric_results` | 1,080 (unchanged) |
| `derived_metric_results` | 405 (unchanged) |
| `historical_prices_daily` | 14,913 (unchanged) |
| unique REVIEW_REQUIRED | 0 (unchanged) |
| Annual Data V1 checksum | unchanged |

**Standing declarations (D-046, recorded in `docs/DECISIONS_LOG.md`)**:
Valuation V1 is frozen. Reported diluted EPS is the authoritative
per-share valuation input, with a validated (but currently unused)
fallback to `net_income / diluted weighted-average shares`.
`availability_date = filing_date` governs point-in-time use. No
changes to Valuation V1 without a new version and full validation.

**Files**: `data/valuation_v1_release_manifest.json` (the freeze
record), `data/valuation_v1_build_validation.json`,
`data/valuation_v1_preview.csv`. Full detail in
`docs/LAST_CLAUDE_REPORT.md`.

**Current overall state**: Annual Data V1, Quarterly Data V1, Derived
Metrics V1, Historical Prices V1, and Valuation V1 are all frozen.
Historical P/E can now be calculated safely and reproducibly for any
of the 45 approved company-years. The next stage (not started) would
be building the full Scoring Model V1 (per
`docs/SCORING_MODEL_V1_BLUEPRINT.md`) and the point-in-time backtest
engine on top of these five frozen releases.
