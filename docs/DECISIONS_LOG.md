# AI Stock Agent — Decisions Log

## D-001 — Official 10-K is the primary source
Primary fundamental data must come from the official company 10-K. SEC Company Facts and other feeds are validation/QA sources unless explicitly changed.

## D-002 — Filing selection is accession-first
Every filing must be locked using ticker → CIK, form = 10-K, exact report date, accession number, primary document, and filing date. Do not select by largest file, filename year, or guess.

## D-003 — Point-in-time fields are mandatory
Store filing date and accession so historical tests use only information available at the time.

## D-004 — HTML tables are not the primary extraction method
HTML may be used for human validation or diagnostics only.

## D-005 — Manual tag lists are not the universal engine
Manual `us-gaap` concept lists remain a QA baseline only.

## D-006 — Use the full XBRL DTS
Use extension schema, labels, presentation, calculation, definitions/dimensions, contexts, units, and statement roles. Arelle is the selected engine for the proof.

## D-007 — Statement-first mapping
Identify the primary statement and row meaning first, then map to a canonical metric.

## D-008 — Fail closed
When evidence is ambiguous, return `REVIEW_REQUIRED`. Never choose by size, intuition, or company-specific guess.

## D-009 — One small proof before a full engine
The next proof is Oracle 2024 total revenue.

## D-010 — No paid service before practical verification
Verify exact plan, endpoints, history, rate limits, account eligibility, cost, and exit strategy.

## D-011 — Code workflow
Until explicitly changed:
1. Create a new complete file under `scripts`.
2. Preserve the prior version.
3. Run from VS Code PowerShell.
4. Avoid partial manual patches.
5. Use observable outputs and timeouts.

## D-012 — Continue without unnecessary pauses
After a verified success or clearly diagnosed failure, continue to the next obvious step.

## D-013 — Backtesting discipline
Explicitly address look-ahead bias, survivorship bias, overfitting, out-of-sample performance, factor contribution, CapEx weight, thresholds, and benchmark excess return.

## D-014 — Council process
Five independent expert views, anonymous peer review with scores and averages, identities only at the end, and one chair decision.

## D-015 — NOPAT / ROIC accounting policy
Binding policy for the generic XBRL metric engine (`scripts\49_xbrl_metric_engine.py` and successors):
1. **Total Debt** includes only interest-bearing debt (current/short-term borrowings, current portion of long-term debt, long-term debt) — never accounts payable, operating liabilities, or operating lease liabilities. Prefer a single explicit "Total debt" row only if its Statement Role, context, and instant date validate structurally; otherwise sum current debt + long-term debt only when Presentation/Calculation relationships show them as separate, non-overlapping rows. Never infer current debt = 0 from a missing tag. Ambiguity or overlap → `REVIEW_REQUIRED`.
2. Extract Pretax Income, Income Tax Expense, and Stockholders' Equity generically (structure/label based, no ticker rules), for the current and prior fiscal year as needed.
3. **Reported Effective Tax Rate** = Income Tax Expense / Pretax Income. If Pretax Income is not positive, or the rate falls outside [0, 1] → `REVIEW_REQUIRED`, never a guess.
4. **NOPAT** = Operating Income × (1 − Reported Effective Tax Rate).
5. **Invested Capital** = Total Debt + Stockholders' Equity − Cash and Cash Equivalents − Short-Term Investments.
6. **Average Invested Capital** = average of Invested Capital at fiscal year start and fiscal year end. Never use only the year-end balance if the year-start balance is unavailable.
7. **ROIC** = NOPAT / Average Invested Capital.
8. Every computed metric is marked `is_derived_metric: true` and includes formula, components, full component lineage, current/prior periods, and status.

This is a binding accounting decision, not merely an implementation detail — changing it requires the user's explicit sign-off, the same as any other entry in this log.

## D-016 — current_debt as a sum of explicit components
Binding refinement to D-015's Total Debt rule (`scripts\52_xbrl_metric_engine.py` and successors): `current_debt` may be computed as the SUM of multiple explicit, interest-bearing current-debt components, not only a single row, resolved in this order (stop at the first tier that succeeds):
1. A single, reliable row explicitly labeled as a *total* of current debt (e.g. "Total current debt").
2. Otherwise, the filing's own Calculation linkbase (summation-item relationships) verifying a set of non-overlapping current-debt components.
3. Otherwise, presentation siblings (same immediate parent in the Presentation tree — the structural proof of non-overlap) whose labels match the allowed vocabulary: Short-Term Borrowings, Short-Term Debt, Commercial Paper, Current Portion of Long-Term Debt, Current Maturities of Long-Term Debt, or other explicitly-current interest-bearing debt.

Never included: Accounts Payable, Accrued Expenses, Operating Lease Liabilities, or other operating liabilities. Control rules: never count a component twice; never sum a Total Current Debt row together with its own sub-components; never infer `current_debt = 0` from a missing row; overlapping or unclear structure at any tier → `REVIEW_REQUIRED`. The rule is fully generic — no ticker or company name appears in its logic.

**Consequence discovered while implementing this policy on Microsoft:** its two current-debt components (`CommercialPaper`, `LongTermDebtCurrent`) were correctly identified as siblings by tier 3, but summing them initially still failed because of two separate, unrelated, generic dedup bugs in the fact-matching layer (not specific to this policy or to Microsoft) — see `docs/CURRENT_STATE.md` for the "decimals-aware duplicate reconciliation" and "unparseable-value fact exclusion" fixes. Both are now part of the engine's core `deduplicate_and_decide()` / `match_facts()` logic and benefit every metric, not just `current_debt`.

## D-017 — current_debt = 0 inference, 4-condition proof
Binding refinement to D-016 (`scripts\54_xbrl_metric_engine.py` and successors): `current_debt` may be inferred as exactly `0` — but only when D-016's tiers 1-3 already found zero explicit components (a precondition, not sufficient on its own) AND all four of the following conditions are structurally proven from the filing's own disclosures:
1. No Current Debt, Short-Term Borrowings, Commercial Paper, or Current Portion of Long-Term Debt row exists anywhere (D-016 tiers 1-3 finding zero components).
2. The debt maturity schedule / debt note shows no amount due for repayment within 12 months (the chronologically-earliest bucket in the filer's own debt-maturity disclosure is exactly `0`).
3. Total Debt reconciles fully with Long-Term Debt (the same disclosure's own "Total" row matches `long_term_debt` from the balance sheet, tolerance ≤$1).
4. No contradicting Fact, row, or note exists anywhere in the debt-related disclosures.

If any condition is not proven: return `REVIEW_REQUIRED`. The rule is fully generic — no ticker or company name appears in its logic; role/label pattern matching only.

**Consequence discovered while implementing this policy on Meta:** the first attempt (`scripts\53_xbrl_metric_engine.py`) found the maturity-schedule role lookup itself was ambiguous — Meta's own investment portfolio has a disclosure ("Contractual Maturities of Marketable Debt Securities") that also matches a naive "debt" + "maturity" role pattern, alongside Meta's actual borrowings disclosure ("Schedule of Maturities of Long-Term Debt"). Fixed generically in `scripts\54_xbrl_metric_engine.py` by excluding investment/marketable-securities-related roles (`marketable|available.for.sale|investment`) from the maturity-schedule role search — a real, ticker-agnostic gap (any filer disclosing both its own debt and its investment holdings' maturities could hit this), not specific to Meta.

**Result for Meta 2024, after the fix:** `current_debt` correctly remains `REVIEW_REQUIRED`, not `0` — condition 3 fails for the current year (the maturity schedule's Total, $29,000,000,000, does not reconcile with `long_term_debt`, $28,826,000,000 — a genuine $174,000,000 discrepancy), and condition 2 fails for the prior year (Meta's schedule merges its first two maturity years into one custom concept, so no single "due within 12 months" figure can be isolated). This is the policy working as intended: it proves zero only when the evidence genuinely supports it.

This is a binding accounting decision, not merely an implementation detail — changing it requires the user's explicit sign-off, the same as any other entry in this log.

## D-018 — Total Debt, "Aggregate-First"
Binding refinement to D-015/D-016/D-017 (`scripts\55_xbrl_metric_engine.py` and successors): `total_debt` may be taken directly from a reliable, filing-reported AGGREGATE "Total debt" row, without first requiring `current_debt` and `long_term_debt` to individually resolve, under these rules:
1. Keep `current_debt` itself `REVIEW_REQUIRED` unless it is independently, explicitly supported (D-016/D-017 unchanged) — this policy never overrides `current_debt`'s own status.
2. If a reliable, directly reported Total Debt row exists — its statement/disclosure role, context, instant date, unit, and reporting period all validated the same way as every other built-in metric — use it as `total_debt` with status `PASS_DIRECT_AGGREGATE`.
3. A validated direct Total Debt supports `adjusted_net_debt`, `invested_capital`, `average_invested_capital`, and `roic` exactly as a plain `PASS` would (see `SUCCESSFUL_METRIC_STATUSES`).
4. The current-vs-long-term debt allocation within that aggregate is explicitly recorded as unverified (`current_long_term_allocation` field) — never silently implied as split-verified.
5. If no reliable direct Total Debt exists, `total_debt` falls back to D-016/D-017's sum-or-proven-zero logic; if that also fails, `total_debt` and every dependent metric remain `REVIEW_REQUIRED`.
6. Fully generic: no ticker-specific logic, no company-specific exception, no assumed zero values. The row search is broadened to include Disclosure roles (not just the Balance Sheet), excluding any role whose own title scopes it to a single maturity class (e.g. "Components of Long-term Debt") or to an investment-asset disclosure — using the filer's own statement-role structure to validate scope, the same principle already used elsewhere in this engine.

**A real trap found and correctly avoided while implementing this on Microsoft, confirming the role-exclusion is necessary, not decorative:** Microsoft's 10-K has a row literally labeled "Total debt" (concept `us-gaap:LongTermDebt`) inside a role titled "Components of Long-term Debt (Detail)" — that row sums only the long-term instruments in that table, excluding Microsoft's separate Commercial Paper balance. Used unfiltered, it would have silently understated Microsoft's total debt. The role-title exclusion correctly rejects it; Microsoft's `total_debt` continues to resolve via the existing D-016 sibling-sum path, value unchanged.

**Result for Meta 2024:** verified by actually running the broadened search (not assumed) — no row anywhere in Meta's 10-K is a bare, unqualified, company-wide "Total debt" (the only candidates are its investment-portfolio note, already excluded, and two "Long-term Debt"-titled disclosure rows, correctly excluded and already known from D-017 not to reconcile with the balance sheet's net carrying value). `total_debt_explicit` finds zero candidates; `current_debt` remains `REVIEW_REQUIRED` (D-016/D-017); the sum-path fallback is unavailable; so `total_debt` and every dependent metric correctly remain `REVIEW_REQUIRED` — D-018's own rule 5 applied honestly, not forced.

This is a binding accounting decision, not merely an implementation detail — changing it requires the user's explicit sign-off, the same as any other entry in this log.

## D-019 — Debt Classification Resolver (bounded, council-approved)
Binding refinement to D-016 (`scripts\58_xbrl_metric_engine.py` and successors), addressing the Palo Alto Networks `current_debt`/`long_term_debt` finding from the multi-company batch test: a filer's sole debt instrument, "Convertible senior notes, net", was classified current-vs-non-current only by presentation POSITION, not by any current/non-current wording in its own label — neither `current_debt` nor `long_term_debt`'s label-text matching could safely tell which one it was.

**Architecture — a bounded, modular evidence hierarchy** (never a full engine rewrite, never weakened validation, never a delay to historical extraction):
1. Directly reported Total Debt (D-018, unchanged).
2. Explicit Current Debt / Long-Term Debt lines (the existing label search, unchanged).
3. Calculation relationships (D-016 tier 2, unchanged).
4. **Presentation parent and ancestor chains (new)** — for a debt-vocabulary row whose label carries no current/non-current qualifier, walk its presentation ancestry within the balance-sheet role: if the chain passes through a current-liabilities grouping (the standard, universal `us-gaap:LiabilitiesCurrentAbstract` structural concept, or any ancestor whose own label reads as a current-liabilities section header), the row is CURRENT; otherwise, if the chain reaches the general Liabilities section at all, it is NON-CURRENT (the convention for a filer, like Palo Alto Networks, that does not nest non-current liabilities under any matching "non-current" abstract).
5. Debt-note/maturity-schedule corroboration (new) — recorded as evidence for every ancestry-classified row; informational, not a second blocking gate (consistent with D-017 condition 4's "absence of contradiction, not proof of consistency" pattern).
6. `REVIEW_REQUIRED` whenever the ancestry chain does not resolve to a clear answer, or more than one row would satisfy the same classification — never a guess.

This is used ONLY as a fallback tier — for `current_debt`, tried after D-016 tiers 1-3 already found nothing (before falling to D-017's zero-inference); for `long_term_debt`, tried after its own direct label search already found nothing. It never overrides an existing successful match, and touches no other metric.

**Accounting policy (binding):**
1. Convertible notes are interest-bearing debt until converted, repaid, or extinguished — included in `current_debt`/`long_term_debt` like any other debt instrument, never excluded merely because they are convertible.
2. Use the GAAP CARRYING amount reported in the filing (whatever concept the filer's own presentation attaches to the row, typically net of unamortized discount/issuance costs), never principal or face value.
3. Respect the filing's own current/non-current classification — this resolver infers that classification from presentation structure only when the filer's label text doesn't state it directly; it never overrides an explicit label.
4. Exclude separately reported equity components, conversion-option equity, and derivative liabilities from debt, unless the filing's own structure includes them (implemented as a label-exclusion list: `equity component`, `conversion option`, `derivative liability`, `embedded derivative`, alongside the existing payables/accruals/lease exclusions).
5. Classification must be supported by general structural evidence together — statement role, presentation parent/ancestor chain, calculation relationships, labels, context, reporting date, unit, and accounting meaning — never a single signal alone.
6. No concept-name-only or manual-tag-list reliance: a row must match BOTH broadened debt-label vocabulary AND ancestry evidence to be classified; a bare concept-name lookup table is never used as the primary mechanism.
7. No ticker-specific or company-specific rule anywhere in this module — every pattern is a general SEC-filer/XBRL-taxonomy convention (e.g. the near-universal `LiabilitiesCurrentAbstract` grouping concept), not a company-specific exception.

**A related, general label-vocabulary gap found and fixed while building this (not part of the ancestry resolver itself, but surfaced by the same debt-classification review):** Micron's balance sheet reports a line literally titled bare **"Current debt"** (`us-gaap:DebtCurrent`), with no further qualifier ("short-term", "commercial paper", "portion of", "maturities of") that `current_debt`'s prior label pattern required. Without it, the row was invisible to every existing tier, incorrectly falling through to D-017's zero-inference attempt for a company that in fact reports a nonzero current-debt balance. Added `current\s+debt` to `current_debt`'s mention/plain patterns (guarded by the pattern's existing `non-?current` exclusion).

**Results:**
- **Palo Alto Networks**: `current_debt` now resolves via ancestry classification — $0 for FY2025 (the Convertible senior notes' carrying value genuinely dropped to zero by fiscal year-end, corroborated by the prior year's fact showing $963,900,000, i.e. the notes matured/were settled during the fiscal year — a real, structurally-verified value, not a bug), `PASS`. `long_term_debt` correctly remains `REVIEW_REQUIRED`, now with a precise reason (no long-term debt row exists on the balance sheet, and the ancestry search confirms no non-current debt-vocabulary candidate exists either) — Palo Alto Networks genuinely has no long-term debt this year, so `total_debt`/`roic` correctly stay `REVIEW_REQUIRED` rather than being forced to a value. No new policy was invented to force these to `PASS`.
- **Micron**: the bare-"Current debt" label fix alone resolved the entire chain — `current_debt`, `total_debt`, `adjusted_net_debt`, `invested_capital`, `average_invested_capital`, and `roic` all became `PASS` (Micron went from 14/20 to 20/20).
- **Google, Amazon, CrowdStrike**: unaffected — their `current_debt` `REVIEW_REQUIRED` reasons (proven nonzero footnote debt not on the balance sheet face; no maturity-schedule disclosure at all) are unrelated to ancestry/convertible-notes classification, and the resolver correctly found no new candidate for any of them.
- **Full regression** (Oracle, Microsoft, Meta, NVIDIA): zero differences in any status or value.

This is a binding accounting decision, not merely an implementation detail — changing it requires the user's explicit sign-off, the same as any other entry in this log.

## D-020 — Pretax Income structural fallback (bounded, evidence-confirmed)
Binding refinement to `pretax_income`'s row identification (`scripts\69_xbrl_metric_engine.py`, first applied on top of `scripts\60_xbrl_metric_engine.py`), addressing 9 REVIEW_REQUIRED company-years traced to one shared root cause: the income-statement row for pretax income used a label wording the existing label patterns did not cover (CRWD/PANW: "Loss before ..."; GOOGL: a bare "Total" row inside the pretax-income-scoped role; MU: a middle-clause variant) — confirmed, not assumed, by direct inspection of each filing's `presentation.csv`.

**Architecture — a bounded structural fallback, tried only after the existing label search fails:**
1. Locate the already-resolved `income_tax_expense` row (row identification runs in alphabetical metric-name order, so `income_tax_expense` is already resolved before `pretax_income` is attempted).
2. Within the SAME presentation role and the SAME immediate parent as that row, take its immediate presentation-order predecessor among non-abstract siblings.
3. Validate the candidate independently of its label: `duration` period type and `credit` balance type — the same evidence every other metric in this engine is validated against (statement role, presentation ancestry, period type, unit), never the label text alone.
4. If the candidate fails any check, or `income_tax_expense` itself is not uniquely resolved, `pretax_income` remains `REVIEW_REQUIRED` — no guess.

This is used ONLY as a fallback tier, after the existing label-pattern search already found nothing. It never overrides an existing successful label match, uses no ticker- or year-specific logic, and touches no other metric's row-identification logic.

**Confirmed shared pattern, not assumed:** all 9 affected company-years (CRWD 2022-01-31, 2023-01-31; GOOGL 2021-12-31, 2022-12-31, 2023-12-31; MU 2021-09-02, 2022-09-01; PANW 2021-07-31, 2022-07-31) were individually inspected in their presentation CSVs before implementation — every one is the immediate presentation-order predecessor of `income_tax_expense` within the same role and parent, regardless of label wording. Implementation proceeded only after this confirmation.

**Downstream interaction with D-015 (not a new policy, an observed consequence of an existing one):** for 5 of the 9 (GOOGL ×3, MU ×2), `pretax_income` resolving to a positive value also unblocks `effective_tax_rate`/`nopat`. For the other 4 (CRWD ×2, PANW ×2), `pretax_income` correctly resolves to PASS with a genuinely NEGATIVE value (real net losses in those fiscal years) — D-015's existing, unweakened "pretax income must be positive" rule correctly keeps `effective_tax_rate`/`nopat` (and therefore `roic`) `REVIEW_REQUIRED` for these 4. This is not a bug: it is D-015 correctly applying an already-binding rule to a value that only became visible once D-020 resolved the row-identification gap; the rule itself is unchanged.

This is a binding accounting decision, not merely an implementation detail — changing it requires the user's explicit sign-off, the same as any other entry in this log.

## D-021 — Reusable XBRL warehouse architecture + debt-basis policy (approved)
Two related, explicitly approved decisions, recorded together because the second was discovered and scoped while proving the first.

**1. Architecture — adopt the reusable raw structured XBRL warehouse going forward.** Proven in two stages: a bounded single-filing proof (AMZN, report date 2024-12-31 — `scripts\73_build_xbrl_warehouse_proof.py` + `scripts\74_query_xbrl_warehouse_debt_maturity.py`, PASS on every acceptance criterion), then a generalization proof across 3 more filings spanning different fiscal calendars, filing structures, extension concepts, and debt presentations (MSFT 2024-06-30, META 2024-12-31, NVDA 2024-01-28 — `scripts\75_build_xbrl_warehouse_multi_proof.py` + `scripts\76_query_xbrl_warehouse_canonical_candidates.py`/`scripts\77_query_xbrl_warehouse_canonical_candidates_v2.py`, PASS 39/39 canonical-metric candidates reconstructed from DuckDB alone, zero Arelle load for either comparison). Binding sub-decisions:
1. Parse each filing with Arelle exactly once; preserve the complete parsed XBRL layer (facts, contexts, units, concepts, labels, presentation/calculation/definition relationships, roles) in a dedicated DuckDB warehouse, partitioned by `accession_number` so it accumulates across filings without ever overwriting a prior one.
2. The original SEC filing package on disk remains the immutable raw source — never modified.
3. The warehouse stores the complete parsed XBRL facts and relationships — a raw structured layer, never itself a canonical-metric result.
4. Canonical metrics (current_debt, etc.) remain versioned outputs derived FROM the warehouse by a separate step — the warehouse and the canonical-metric outputs are kept architecturally distinct, never merged into one table/store.
5. Not yet scaled beyond the 4 filings already proven (AMZN, MSFT, META, NVDA) — extending to the remaining 41 already-locked company-years, and wiring canonical-metric computation to read from the warehouse instead of reopening Arelle each time, are separate, not-yet-approved next steps.

**2. Debt-basis policy — never use an undiscounted debt-maturity principal amount as canonical current_debt.** Directly informed by the D-017/D-021-proof-test finding (AMZN, GOOGL, and confirmed again on MSFT/META during the warehouse work) that a debt-maturity schedule's earliest ("next twelve months") bucket is, by the near-universal ASC 835/470 disclosure convention, an undiscounted principal cash-flow amount — never a GAAP carrying amount. Binding rules:
1. `current_debt` must be a GAAP carrying amount (net of unamortized discount/premium/issuance costs, whatever concept the filer's own presentation attaches to the row) — never a face value or undiscounted principal amount, regardless of source (balance sheet row, calculation-linkbase component, or debt-maturity-schedule bucket).
2. When only an undiscounted principal amount due within one year is available (no reliable carrying amount anywhere in the filing), that value is preserved as a separate structured fact / semantic candidate — visible for audit and future policy decisions — but is never used as GAAP `current_debt`.
3. Such a principal-only value is never used, directly or indirectly, in `total_debt`, `invested_capital`, `average_invested_capital`, or `roic`.
4. Canonical `current_debt` remains `REVIEW_REQUIRED` in this situation unless a reliable carrying amount becomes available through some other tier (explicit row, calculation-verified components, sibling components, ancestry classification, or D-017's zero-inference proof) — never forced to a value merely because a principal-only figure exists.
5. This policy does not change any already-computed canonical result: no filing's `current_debt` has ever been set from a principal-only maturity-schedule bucket, so recording this policy closes the door on an approach that was explored and correctly rejected (see the D-021 current_debt proof-test, AMZN/GOOGL, "STOPPED at proof test"), not a live change to any stored value.

Both decisions require the user's explicit sign-off to change, the same as any other entry in this log.

**Documentation defect status (updated):** the structural corruption noted here — D-019's own accounting-policy list (items 2-7, plus a Micron finding paragraph) having been misplaced after D-020's content instead of directly after D-019's own item 1 — has since been repaired in a dedicated, isolated edit. D-019 and D-020 are now each complete, self-contained entries; no decision text was lost, reworded, or reinterpreted in the repair.

## D-027 — Consolidated cleanup: four new approved policies (approved)
Four related, explicitly approved policies, applied together in one consolidated cleanup pass across PANW, AMZN, GOOGL, CRWD, META, and NVDA. Each supersedes a prior blocking rule only where noted — nothing else in D-007/D-016/D-019/D-020/D-021/D-022 is weakened.

**Policy A — debt maturity classification (extends D-022).** Principal contractually due within 12 months = `current_debt`; due after 12 months = `long_term_debt`; operating leases always excluded from financial debt; GAAP carrying value still preferred; maturity-principal fallback still uses `status=PASS_MATURITY_BASIS`, `basis=MATURITY_PRINCIPAL` (unchanged from D-022). **New item 7:** `average_invested_capital` may use the PREVIOUS fiscal year's own separately-locked filing and its latest-approved `invested_capital` result directly — does not require the prior period to appear as a comparative fact inside the current filing. This directly addresses the D-022 finding that a debt-maturity schedule never carries a prior-period comparative bucket within the same filing (structurally, not just practically, unavailable) — using the prior year's OWN already-approved, already-point-in-time-locked result is equally point-in-time-correct.

**Policy B — undrawn revolving credit facility (new).** An explicit, resolved (non-nil, numeric) XBRL fact showing a revolving-credit facility's own "amount outstanding" concept (`us-gaap:LineOfCredit`, dimensioned by a credit-facility member whose name contains "Revolving") equal to zero AT THE EXACT REPORT DATE proves zero debt for that facility — `status=PASS`, `basis=ZERO_EXPLICIT_UNDRAWN_FACILITY`. If, in addition, zero further unclaimed debt-vocabulary candidates exist anywhere on the balance sheet, both `current_debt` and `long_term_debt` may be zero (`basis=ZERO_PROVEN_STRUCTURAL_ABSENCE`, symmetric with the existing D-026 tier). The facility's credit limit/available capacity is never treated as evidence of an outstanding balance either way — only the facility's own outstanding-balance fact counts.

**Policy C — reported total-debt aggregate (new).** A filing's own reported "Total" row in its debt-maturity schedule is authoritative for `total_debt` (`status=PASS_DIRECT_AGGREGATE`, `basis=DIRECT_AGGREGATE_REPORTED_TOTAL`) even when it does not reconcile exactly with the balance-sheet `long_term_debt` carrying value (e.g. face value vs. carrying value net of unamortized discount/issuance costs). The reconciliation gap is preserved in lineage, never silently discarded, and never blocks `total_debt`, `adjusted_net_debt`, `invested_capital`, or `roic`. `current_debt`/`long_term_debt` individually may remain separately classified/`REVIEW_REQUIRED` — this policy does not force them. Operating leases and other non-debt liabilities are never included in the aggregate.

**Policy D — normalized tax rate (new, supersedes the ORCL FY2021 exception previously recorded as a standing REVIEW_REQUIRED case).** When `pretax_income<=0`, or the reported effective tax rate is `<0` or `>1`, but `pretax_income`/`income_tax_expense`/`operating_income` are all independently `PASS` (valid, unambiguous source facts), use a fixed 21% rate: `NOPAT = operating_income * (1 - 0.21)`, `status=PASS_NORMALIZED_TAX`, `basis=FIXED_NORMALIZED_TAX_RATE_21_PERCENT`. Negative `operating_income` is explicitly allowed to produce negative NOPAT/ROIC — never blocked on that basis alone. Still blocked (fails closed, no guess) if `operating_income` is missing/invalid, `average_invested_capital` is missing/zero, or any source fact is itself ambiguous/corrupted.

**Result:** REVIEW_REQUIRED (20 primary metrics × 45 company-years) went from 134 to 37. Full per-group breakdown, company-years, and values in `docs\LAST_CLAUDE_REPORT.md` (D-027 report, superseded in this file by the D-028 report below — see git/file history or the archived copy referenced in `docs\CURRENT_STATE.md` for the full D-027 text).

All four policies require the user's explicit sign-off to change, the same as any other entry in this log.

## D-028 — current_debt maturity-basis policy for AMZN/GOOGL/META (approved)
**Supersedes D-021 rule 3 for `current_debt` specifically** (D-021 rule 3: "a principal-only maturity value is never used, directly or indirectly, in total_debt, invested_capital, average_invested_capital, or roic" — `current_debt` itself was never explicitly named in rule 3's list, but D-021's overall intent and rule 1/4 clearly barred it too; this decision explicitly lifts that bar for `current_debt` alone, using the exact same `PASS_MATURITY_BASIS`/`MATURITY_PRINCIPAL` status/basis already approved for `total_debt` in D-022). All other D-021 rules (never use a principal-only value as a GAAP carrying amount claim, never conflate the two bases silently) remain unchanged — `PASS_MATURITY_BASIS` is a distinct, always-labeled status, never merged into plain `PASS`.

**Policy:** principal contractually due within 12 months (the filing's own debt-maturity schedule's earliest, chronologically-first non-abstract, non-total, non-lease bucket) = `current_debt`, when no reliable carrying-value detail is available through any earlier tier (explicit total, calculation-verified components, sibling components, ancestry classification) — `status=PASS_MATURITY_BASIS`, `basis=MATURITY_PRINCIPAL`, full bucket/role/report-date lineage preserved.

**Scope applied:** AMZN (5 years), GOOGL (5 years), META (2022/2023/2024) — 13 company-years, all previously `current_debt::REVIEW_REQUIRED` under the old D-021 bar. CRWD and NVDA explicitly excluded (not applicable/out of scope for this pass). Result: REVIEW_REQUIRED 37 → 24, zero downstream metrics required recalculation (total_debt/invested_capital for these 13 already resolved independently via the existing maturity-basis/direct-aggregate fallback). Full details in `docs\LAST_CLAUDE_REPORT.md`.

This decision requires the user's explicit sign-off to change, the same as any other entry in this log.

## D-029/D-030/D-031 — Summary (full detail in each milestone's own `docs\LAST_CLAUDE_REPORT.md` and the corresponding `docs\CURRENT_STATE.md` entries)
- **D-029:** Locked + warehoused one additional prior-fiscal-year 10-K each for AMZN (FY2020) and GOOGL (FY2020), and META (FY2019) — closing the first-year `average_invested_capital`/`roic` gap for AMZN 2021, GOOGL 2021, and META 2020 by computing each prior year's own `invested_capital` (reusing D-027's policy engine unchanged) and feeding it through the already-approved prior-fiscal-year-lookup mechanism (D-027 item 7). REVIEW_REQUIRED 24 → 18.
- **D-030:** Resolved CRWD's `short_term_investments::REVIEW_REQUIRED` (2022, 2026) via a general, ticker-agnostic proof: the filing's own `AssetsCurrent` calculation-linkbase children sum exactly to the reported total with no short-term-investment component present (compared directly against CRWD's own already-PASS years, whose calculation DOES include one) — `status=PASS`, `value=0`, `basis=ZERO_PROVEN_STRUCTURAL_ABSENCE`. REVIEW_REQUIRED 18 → 8 (also resolved CRWD 2023/2026 downstream cascades).
- **D-031:** Attempted to close CRWD's own remaining first-year gap (locked + warehoused CRWD FY2021) — found a genuine, verified blocker: the revolver's outstanding-balance fact in CRWD's FY2021 10-K has `value_raw="(ixTransformValueError)"` in Arelle's own output (Arelle could not decode the fact). No value was guessed; REVIEW_REQUIRED remained 8 (0 conversions), and the finding was escalated to the user as an explicit decision point rather than assumed.

## D-032 — Approved one-time manual SEC-HTML fact recovery for a specific Arelle ixTransformValueError (approved)
**Scope: this decision applies ONLY to the one specific fact identified in D-031** (CRWD FY2021, `us-gaap:LineOfCredit`, context `i36456875e4304ffba24e889b8aca8952_I20210131`) — it is NOT a general policy for handling every future `ixTransformValueError` case; each such case must be brought to the user individually, per the same "never guess" fail-closed principle that governs every other tier in this project.

**Approved rule:** when Arelle cannot decode an Inline XBRL fact (recorded as `value_raw="(ixTransformValueError)"`, `value_numeric=NULL`), the value may be read directly from the SAME locked, original SEC 10-K HTML already on disk (never an external source, never an estimate) — accepted ONLY if the value is fully deterministic from the HTML: the `<ix:nonFraction>` element's visible text, its declared `format`/transform attribute, and the surrounding prose must all independently agree on the same value. `status=PASS`, `basis=SEC_HTML_MANUAL_PARSE`. The original Arelle error is always preserved in lineage, alongside an explicit note that the value was manually recovered from the same locked source, not estimated.

**Applied once:** CRWD FY2021's revolver fact — visible text `"No"`, transform `ixt-sec:numwordsen` (the SEC's own standard number-words registry, built to map "no"→0), surrounding sentence "No amounts were outstanding under the A&R Credit Agreement as of January 31, 2021." — all agree on **0.0**. Result: CRWD FY2021 `current_debt`/`total_debt`/`adjusted_net_debt`/`invested_capital` resolved; CRWD FY2022 `average_invested_capital`/`roic` resolved via the unchanged D-027 prior-year-lookup mechanism. REVIEW_REQUIRED 8 → 6. Full HTML element and evidence in `docs\LAST_CLAUDE_REPORT.md`.

This decision (and any future extension of it to a different fact) requires the user's explicit sign-off, the same as any other entry in this log.

## D-035 — Approved XBRL-decimals-based rounding-tolerance policy for quarterly reconciliation (approved)
**Scope:** applies to the quarterly Q1+Q2+Q3+Q4-vs-Annual reconciliation step in the quarterly-proof line of work (MSFT/AMZN/ORCL proofs and any future company/period using the same method) — not to annual metric extraction, not to any other reconciliation, and not a general-purpose tolerance applied elsewhere in the project.

**Approved rule:** no reported value is ever altered, smoothed, or replaced. For every reconciliation equation:
1. Read the XBRL `decimals` value for every independently reported source fact participating in the equation.
2. `uncertainty_per_fact = (10 ** (-decimals)) / 2` (e.g. `decimals=-6` → rounding unit $1,000,000 → uncertainty $500,000).
3. `permitted_difference` = sum of the uncertainties of every independently reported source fact in the equation (a derived quarter, e.g. Q4 = Annual − Q3_9mYTD, is not itself an independently reported fact — its uncertainty is already carried by the facts it is built from, and is not counted a second time).
4. If `abs(actual_difference) <= permitted_difference`: `status = PASS_ROUNDING_TOLERANCE`. Full lineage preserved — exact reported values, exact difference, decimals/rounding-unit/uncertainty per source fact, calculated permitted difference, and the equation used.
5. If the difference exceeds the calculated tolerance: fail-closed `REVIEW_REQUIRED` (never a silent pass, never a fixed dollar or percentage tolerance).

**Applied once so far:** ORCL FY2024 quarterly proof (`scripts/117`) — `operating_income` and `pretax_income` each had a genuine $1,000,000 exact-equality gap, previously `FAIL`. All 4 contributing source facts (Q1, Q2, Q3, Annual) are reported at `decimals=-6`, giving a permitted difference of $2,000,000 — both gaps now correctly classified `PASS_ROUNDING_TOLERANCE`, with no value modified. Full detail in `docs\LAST_CLAUDE_REPORT.md`. The fail-closed `REVIEW_REQUIRED` branch (a gap that genuinely exceeds the calculated tolerance) has not yet been exercised on a real case.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-036 — Approved NULL representation for genuinely unresolved quarterly metric fields (approved)
**Scope:** applies to `quarterly_metric_results.concept_qname`,
`.reconciliation_difference`, and `.permitted_difference` only — not to
any annual table, not to any other quarterly column, and not a general
license to use NULL wherever convenient.

**Approved rule:** genuine unresolved quarterly metrics may use NULL for
`concept_qname`, `reconciliation_difference`, and `permitted_difference`
**only** when `lineage_json` documents the missing evidence (the exact
reason the value/concept/reconciliation could not be determined) **and**
the row's `result_status`/`reconciliation_status` is `REVIEW_REQUIRED`.
NULL must represent honestly missing evidence — never an artificial
sentinel value, never a fabricated concept or number. A row that resolved
a real concept/value (e.g. only Q1 of a metric succeeded) keeps that real
`concept_qname` even if the metric's overall reconciliation could not be
computed; only the specific field that genuinely lacks evidence is NULL.

**Applied once so far:** the `quarterly_metric_results` schema was
migrated (via `ALTER TABLE ... ALTER COLUMN ... DROP NOT NULL`,
`scripts/124`) from `NOT NULL` to nullable on exactly these three columns,
backed up and checksum-verified first. Proven on GOOGL FY2021
(`scripts/124`/`125`): 12 of 24 rows are `REVIEW_REQUIRED` with genuine,
documented causes (a missing annual statement row for `pretax_income`;
missing quarterly cash-flow facts for `operating_cash_flow`/`capex`) — 0
fabricated values, 100% of NULLs verified to carry a `REVIEW_REQUIRED`
status and a documented `lineage_json` reason. Full detail in
`docs/LAST_CLAUDE_REPORT.md`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-037 — Quarterly engine must anchor on the authoritative annual production result, not re-identify it (approved)
**Approved rule:** the quarterly engine uses the authoritative active
annual production result from the exact same 10-K accession and metric as
its annual anchor. It must not independently re-identify an annual row
already resolved by the annual pipeline.

**Rationale:** 54 of the 111 quarterly `REVIEW_REQUIRED` cases from the
`scripts/127` root-cause audit (category `ANNUAL_ROW_NOT_RESOLVED`) were
caused not by missing source data but by the quarterly engine
re-doing row-identification work the annual pipeline had already solved
for the identical filing/metric, and failing where the annual pipeline
had succeeded (e.g. via `s89`'s canonical-row identification, precision-
duplicate reconciliation, or other annual-only resolution logic). This is
a general, non-ticker-specific gap between the two pipelines, not a
per-filer data issue.

**Implementation:** `scripts/128_quarterly_extraction_engine_v2.py`
(`ENGINE_VERSION_V2 = "QUARTERLY_ENGINE_V2_ANNUAL_PRODUCTION_ANCHOR"`) —
`resolve_annual_anchor()` queries `financial_metric_results JOIN
extraction_runs` for the exact `(accession_number, metric_name)`,
fail-closed (`REVIEW_REQUIRED`) if the annual row is missing, ambiguous, or
not in an accepted PASS-family status. `lookup_annual_fact_decimals()`
reuses the existing `s89._reconcile_same_context_precision_duplicates_
from_warehouse()` (unchanged) rather than a new ad hoc decimals lookup.
Validated 54/54 (49 `PASS` + 5 `PASS_ROUNDING_TOLERANCE`, 0
`REVIEW_REQUIRED`) via `scripts/129`, then loaded into production for the
12 affected company-years via `scripts/130` (12/12 committed, backup +
archive verified, unique REVIEW_REQUIRED metric-years 111 → 57). Full
detail in `docs/LAST_CLAUDE_REPORT.md`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-038 — Deterministic DIRECT_QUARTER preferred over an identical-value DERIVED_FROM_YTD result (approved)
**Approved rule:** when a deterministic `DIRECT_QUARTER` fact from the
exact required filing has the identical financial value as an existing
`DERIVED_FROM_YTD` result, the direct fact is preferred and the
basis-only change is accepted as improved lineage, not a financial-result
change. This is approved only when all of the following hold: the direct
fact comes from the exact same required 10-Q accession; its `period_end`
matches the current fiscal quarter (never a prior-year comparative);
candidate selection is deterministic (a single distinct value, per the
engine's existing `pick_current_period_fact` logic); the financial value
is identical to the existing derived value; and complete direct-fact
lineage (concept, context, accession, duration) is preserved in place of
the derived-value lineage.

**Rationale / origin:** the quarterly duration-tolerance fix (widening
`QUARTER_DURATION_MIN_DAYS` 89→88 and `YTD_6M_MIN_DAYS` 181→180,
`scripts/132`, engine v3) was validated (`scripts/133`) against all 45
company-years and found, alongside its 36 intended resolutions, 10 PANW
Q3 rows (`operating_income`/`pretax_income`/`income_tax_expense` across
FY2021/2022/2023/2025) where the widened quarter bucket let the engine
find PANW's own direct 88-day Q3 fact instead of falling back to
YTD-derivation — landing on the exact same value both ways. Without this
rule such basis-only changes would fail a literal "no changes to
already-resolved rows" check even though no financial value is affected;
this decision makes that outcome an explicitly accepted, general (not
PANW-specific) policy.

**Applied:** `scripts/134_quarterly_engine_v3_production_load.py` loaded
the 15 affected company-years (11 from the 36 resolved duration cases +
4 PANW basis-only years) into production, re-verifying at commit time
that every one of the 10 approved PANW rows carries an identical value to
the row it replaced. 15/15 committed, 0 rollbacks; unique
REVIEW_REQUIRED metric-years 57 → 21. Full detail in
`docs/LAST_CLAUDE_REPORT.md`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-039 — Point-in-time-safe concept reuse for quarterly Q1/Q2/Q3 resolution (approved)
**Approved rule:** when the primary presentation-based quarterly concept
resolver (`s89.identify_canonical_row`, run independently per 10-Q) fails
for Q1, Q2, or Q3, the engine may reuse a concept **name** — never a
financial **value** — only from point-in-time-safe evidence, in priority
order: (1) an earlier 10-Q of the *same* fiscal year, same metric,
already resolved earlier in the same run; (2) the nearest resolved
prior-fiscal-year 10-K result for the same company and metric, walking
back through progressively older 10-Ks as needed. Every candidate source
is accepted only if `source filing_date <= blocking filing_date`.
**Forbidden, always**: the same fiscal year's own 10-K (it is always
filed after all 3 quarters), a later quarter, a later fiscal year, and
any filing dated after the blocking quarter's own `filing_date`. The
actual financial value is always re-selected fresh from the blocking
quarter's own exact accession via the engine's unchanged, independent
fact-selection safeguards (exact accession, exact period_end, correct
duration bucket, no dimensions, single deterministic value) — a reused
concept name is never assumed correct; it only earns a second attempt.

**Rationale / origin:** the remaining-21 root-cause audit (`scripts/135`)
found that 15 of 21 REVIEW_REQUIRED quarterly cases fail because `s89`'s
per-filing presentation walk cannot find a metric's row in a specific
quarter's own 10-Q, even though the exact same concept is already trusted
elsewhere for the same company/metric. Naively reusing the *current*
fiscal year's own annual result (as `scripts/135`'s read-only audit did,
for diagnostic purposes only) would introduce look-ahead bias — the
10-K is always filed after the quarter it would be "fixing." This
decision closes that loophole: only genuinely pre-existing, already-filed
evidence may ever be reused.

**Applied:** `scripts/136_quarterly_extraction_engine_v4_point_in_time_
concept_reuse.py` implements the rule (based on `scripts/132`, the only
change). Validated (`scripts/137`) against the 72-row MSFT/AMZN/ORCL
baseline (0 differences, fallback confirmed inactive) and all 15 target
cases (11 of 15 resolved — 9 `PASS` + 2 `PASS_ROUNDING_TOLERANCE`; the
other 4 correctly remain `REVIEW_REQUIRED` because each is its ticker's
earliest fiscal year in the database, with no earlier evidence to reuse
by construction — 0 regressions, 0 future-data violations). Loaded into
production for the 10 affected company-years via `scripts/138` (10/10
committed, backup + archive verified, unique REVIEW_REQUIRED metric-years
21 → 10). Full detail in `docs/LAST_CLAUDE_REPORT.md`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-040 — All future TASK_NNN work must use the shared task-marker utility (approved)
**Approved rule:** every future `TASK_NNN` operation in this project must
record its STARTED and RESULT evidence using
`scripts/142_task_marker_guard.py`'s `start_task()` / `finish_task()` /
`fail_task()` — never hand-written marker files. Manually typed
timestamps, local timestamps labeled as UTC, and `PASS` results without
validated mandatory outputs are forbidden.

**Rationale / origin:** TASK_142 proved TASK_141 had two process defects
— a hand-typed STARTED timestamp never derived from any clock, and a
RESULT completion timestamp taken from local time with a literal `Z`
(UTC designator) incorrectly appended. A written rule alone was judged
insufficient to prevent recurrence of this defect class; the invariant is
now enforced by reusable code instead. `scripts/142_task_marker_guard.py`
makes a caller-supplied timestamp structurally impossible (every
timestamp comes from exactly one internal call to
`datetime.now(timezone.utc)`, with no parameter anywhere through which a
different value could be supplied), makes every write atomic
(temp-file + `os.replace`, re-read-verified), and makes a `PASS` result
impossible when any declared mandatory output is missing, unreadable, or
hash-mismatched, or when `completed_at < started_at`.

**Applied and validated (TASK_143)**: `scripts/143_task_marker_guard_
validation.py` runs 12 required tests (duplicate STARTED rejected,
RESULT-without-STARTED rejected, missing/unreadable output blocks PASS,
tampered-output hash mismatch detected, task-ID mismatch detected,
non-UTC timestamp rejected, completion-before-start rejected via a
STARTED-file fixture — never by injecting a fake clock into production
code, existing RESULT never overwritten, atomic writes leave no temp
files, timestamps monotonic and end in `Z`) — **12/12 PASS**. TASK_143
then used the utility on itself to write and self-validate its own
STARTED/RESULT evidence (`validate_task_evidence` → `valid=True`, 0
failure categories), surfacing and fixing three real issues in the
process (documented in `docs/tasks/TASK_143_RESULT.md`). Full standard
documented in `docs/TASK_MARKER_STANDARD.md`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-041 — The authoritative warehouse loader must distinguish Inline XBRL from traditional XBRL, and PASS requires validated non-zero content (approved)
**Approved rule:** the authoritative warehouse loader must distinguish
Inline XBRL from traditional XBRL with a separate instance document.
`PASS` requires validated, non-zero warehouse content and complete
lineage; a successful Arelle return without extracted content is
insufficient.

**Rationale / origin:** `scripts/121`'s original loader always used the
SEC filing index's `primaryDocument` as the sole Arelle entry point and
set `status="PASS"` the instant Arelle's Session exited without a Python
exception — never checking whether anything was actually extracted. This
silently produced a false `PASS` with zero warehouse content for NVDA's
Q1 FY2020 10-Q (accession `0001045810-19-000079`), whose `primaryDocument`
is a plain pre-Inline-XBRL HTML page while the real facts live in a
separate `nvda-20190428.xml` instance document. `TASK_141`'s global
185-accession integrity audit confirmed this is the **only** such case
anywhere in the project's universe (184 valid, 1 defective) — a general,
provable, one-time defect, not a recurring pattern.

**Applied**: `scripts/144_warehouse_loader_v2_production.py` (general,
no ticker/accession-specific logic) implements the rule: check the
primary document for an Inline-XBRL namespace marker first; otherwise
require exactly one standalone `<xbrli:xbrl>` instance document in the
locked package (excluding schemas/linkbases), failing closed
(`ENTRY_POINT_NOT_RESOLVED` / `MULTIPLE_INSTANCE_CANDIDATES`) on zero or
multiple candidates. `PASS` is refused unless `xbrl_facts`/`xbrl_contexts`/
`xbrl_concepts` are all `> 0`, `xbrl_units > 0` whenever a monetary fact
exists, the physically-inserted counts (re-queried inside the same
transaction) exactly equal the computed counts
(`INSERTED_COUNT_MISMATCH` otherwise), and complete accession +
selected-entry-point lineage is present. Used via
`scripts/145_nvda_2019q1_production_warehouse_repair.py` to repair the
one defective accession in production (`data/database/xbrl_warehouse_
proof.duckdb`): full backup + checksum + archive of the two pre-existing
false-PASS `warehouse_runs` records before any write, one atomic
transaction, all 9 table counts matching the `scripts/139`/`140`
scratch-proof values exactly (654 facts, 134 contexts, 5 units, 711
concepts, etc.; total warehouse facts 225,126 → 225,780), both historical
false-PASS records preserved unchanged, `ai_stock_agent.duckdb`
completely untouched. 10/10 post-commit integrity checks passed. Full
detail in `docs/NVDA_2019Q1_PRODUCTION_WAREHOUSE_REPAIR.md`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-042 — Quarterly Data V1 is frozen; engine V5 is the authoritative quarterly extraction engine (approved)
**Approved rule:** Quarterly Data V1 (`data/database/ai_stock_agent.duckdb`,
tables `quarterly_extraction_runs` + `quarterly_metric_results`) is
**frozen**. `QUARTERLY_ENGINE_V5_STANDARD_GAAP_ALLOW_LIST`
(`scripts/148_quarterly_engine_v5_standard_gaap_fallback.py`) is the
authoritative quarterly extraction engine. Annual Data V1
(`data/database/ai_stock_agent_annual_v1.duckdb`) and Quarterly Data V1
are the approved inputs for the next project stage. **No further
data-engine changes (annual or quarterly) are permitted without a new
version and a full regression**, the same discipline already applied to
every engine version in this project (V1→V4 quarterly, the annual V1
freeze).

**Rationale / origin:** `TASK_147` proposed and read-only-proved a fixed,
versioned standard-US-GAAP-concept allow-list fallback tier
(`STANDARD_GAAP_ALLOW_LIST_V1`: 6 revenue concepts, 2 pretax_income
concepts), tried only after the primary presentation resolver and the
existing point-in-time concept-reuse fallback (D-039) both fail.
`TASK_148` regression-proved it clean across **all 45** authoritative
company-years (not a sample): the 4 remaining `REVIEW_REQUIRED`
metric-year cases (`CRWD 2022-01-31 pretax_income`, `MU 2021-09-02
pretax_income`, `PANW 2021-07-31 pretax_income`, `PANW 2021-07-31
revenue`) resolved cleanly with every safety check passing, and the
other 41 company-years (984 rows) were 100% byte-identical to existing
production — 0 regressions. `TASK_149` built the production-load script
(`scripts/151_v5_quarterly_production_load.py`, `--check-only` /
`--execute`), fixed two bugs found during real dry-run/execute attempts
(a self-PID-lock detection defect, and a schema mismatch where the
saved regression artifact didn't carry accession fields — both fixed
read-only-first, each independently re-verified before any write was
attempted), then executed the production load.

**Applied**: the 3 target company-years' old runs (`f5a8de2c...`,
`dae1c3f9...`, `f72f4da0...`) were replaced — full company-year unit (72
rows, not just the 16 changed), never patched in isolation — by 3 new
`QUARTERLY_ENGINE_V5_STANDARD_GAAP_ALLOW_LIST` runs in one atomic
transaction, with full backup (SHA-256-verified) + Parquet archive of
the 3 old runs + 72 old rows beforehand. **Independently re-verified
read-only, directly against the live databases (not merely the load
script's own report)**: `quarterly_extraction_runs`=45,
`quarterly_metric_results`=1,080, `financial_metric_results`=900,
**unique REVIEW_REQUIRED=0** (down from a peak of 21 several tasks ago),
every company-year has exactly 24 rows, 0 duplicate keys, 0 missing
lineage, 0 availability mismatches, 0 future-data violations (spot-
checked directly from the committed `lineage_json`: 4
`STANDARD_GAAP_ALLOW_LIST` activations — one per resolved family — and 8
`EARLIER_SAME_FISCAL_YEAR_QUARTER` reuse activations, 0 violations),
Annual V1 checksum unchanged, XBRL warehouse facts unchanged at 225,780.
Full detail in `docs/V5_FINAL_RELEASE_REGRESSION.md`,
`docs/V5_PRODUCTION_LOAD_BUILD.md`, `data/quarterly_data_v1_release_
manifest.json`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-043 — Derived Metrics V1 is frozen: 405 observations, exactly 2 approved metrics (approved)
**Approved rule:** Derived Metrics V1 (`data/database/ai_stock_agent.duckdb`,
table `derived_metric_results`) is **frozen**. Exactly **405** derived
observations across all 9 approved tickers (ORCL, MSFT, META, NVDA,
GOOGL, AMZN, MU, CRWD, PANW) are approved: 81 annual + 324 quarterly.
Exactly **2** approved derived metrics: `operating_margin` and
`revenue_yoy_growth`. **No future change to Derived Metrics V1 — a new
metric, a formula change, or a data reload — is permitted without a new
engine version and full validation**, the same discipline already
applied to Annual Data V1 (freeze) and Quarterly Data V1 (D-042).

**Rationale / origin:** built on top of the now-frozen Annual Data V1
and Quarterly Data V1 (D-042), generalizing the exact formulas and
point-in-time rules first proven single-ticker
(`scripts/152_msft_derived_metrics_proof.py`) to all 9 tickers
(`scripts/153_derived_metrics_v1_load.py`). Two real defects were found
and fixed before this load ran, each independently re-verified
read-only before any further write was attempted: (1) a
`fiscal_quarter`/`PRIMARY KEY` schema defect (a column inside a
`PRIMARY KEY` is implicitly `NOT NULL` in DuckDB, conflicting with the
required `NULL` semantics for annual rows) — fixed with a
`fiscal_quarter_key` surrogate column plus 4 `CHECK` constraints,
proven via a 13-case in-memory DuckDB test (which itself caught a
second defect: `BETWEEN` alone does not reject `NULL` under SQL's
three-valued logic); and (2) a documentation-only "90 annual / 315
quarterly" error that contradicted the correct, always-405 total —
corrected after independently re-deriving the true 81/324 split three
separate ways (direct CSV count, dataset-level checks, committed-table
checks).

**Applied**: `scripts/153 --execute` created `derived_metric_results`
(schema: `ticker, frequency, fiscal_year_end, fiscal_year,
fiscal_quarter, fiscal_quarter_key, derived_metric, value,
availability_date, formula, source_periods, source_run_ids,
source_accessions, reconciliation_status, engine_version, created_at`,
with 4 `CHECK` constraints and a composite primary key on
`fiscal_quarter_key` rather than the nullable `fiscal_quarter`) and
loaded all 405 validated rows in one atomic transaction, with full
backup (SHA-256-verified) beforehand. **Independently re-verified
read-only, directly against the live database**: load status `PASS`,
table exists, exactly 405 rows (81 annual, 324 quarterly), exactly 9
distinct tickers, 0 duplicate primary keys, 0 NULLs in any required
column, every annual row has `fiscal_quarter IS NULL`, every quarterly
row has `fiscal_quarter` 1–4, exact per-metric counts (`operating_margin`
45 annual + 180 quarterly, `revenue_yoy_growth` 36 annual + 144
quarterly), only the 2 approved `derived_metric` values present, Annual
V1 checksum unchanged, `quarterly_extraction_runs`=45,
`quarterly_metric_results`=1,080, `financial_metric_results`=900,
unique REVIEW_REQUIRED=0 — all unchanged by this load. Full detail in
`docs/DERIVED_METRICS_V1_BUILD.md`, `docs/LAST_CLAUDE_REPORT.md`,
`data/derived_metrics_v1_release_manifest.json`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-044 — Historical Price Policy V1 is binding (approved)
**Approved rule:** For all future historical-price work, exactly these
5 rules apply:

- **Rule A — Preserve source data.** Yahoo's original `open`, `high`,
  `low`, `close`, `adj_close`, `volume`, `dividend`, `split_ratio` are
  always kept as separate fields, never overwritten.
- **Rule B — Total-return calculations use `adj_close`.** Stock return,
  benchmark return, drawdown. Dividends are never separately added on
  top of an `adj_close`-based return (would double count).
- **Rule C — Historical nominal execution price is reconstructed.**
  Simulated buy/sell prices are recovered by multiplying Yahoo
  `open`/`high`/`low`/`close` by the product of every split ratio whose
  effective date is strictly AFTER the price date (a split effective ON
  the price date is not applied to that date). Dividends are never
  applied to nominal execution prices.
- **Rule D — Full portfolio backtest.** A simulation tracking cash,
  share count, dividends, and splits uses the Rule C nominal price for
  buy/sell execution, explicit split events for share-count changes,
  and explicit dividend events for cash — and must never also apply
  `adj_close` in that same simulation (would double count).
- **Rule E — Point-in-time safety.** A future split may be used only as
  the mechanical conversion factor in Rule C. It must never influence a
  historical score, buy/sell signal, valuation decision, position
  selection, or ranking. A backtest decision dated T may use only
  information available at T.

**No future price-related code — return calculations, execution-price
simulation, or portfolio backtesting — may deviate from these 5 rules
without a new decision superseding this one.**

**Rationale / origin:** built directly on the NVDA price-semantics
proof (`docs/NVDA_PRICE_SEMANTICS_PROOF.md`) and its 9-company
extension (`docs/9_TICKER_HISTORICAL_PRICE_PROOF.md`), both of which
established that Yahoo's `close` is already retroactively
split-adjusted at the source. That finding created an open question —
what price series is safe to use for which purpose — which this policy
resolves with 5 explicit, testable rules rather than an assumption.

**Applied**: proven read-only against the already-saved data for NVDA,
GOOGL, and PANW (`scripts/157_price_policy_v1_proof.py`, no new
download, no database access). All 5 known split events (NVDA 4:1/2021
and 10:1/2024, GOOGL 20:1/2022, PANW 3:1/2022 and 2:1/2024) validated
exactly. 7 determinations proven directly from data: Yahoo `close`
stays smooth through every split; the Rule C nominal reconstruction
shows the expected large mechanical jump at every split boundary
(e.g. NVDA 4:1 → 3.87×, GOOGL 20:1 → 19.64×); a naive return computed
from the nominal series is wildly distorted at every split (-48% to
-95%) while the same return computed from `adj_close` is not (-1.8% to
+3.4%) — the concrete justification for Rule B/D; the `adj_close`-based
return calculation was shown structurally to never reference the
`dividend` field, ruling out double counting; all reconstructed prices
positive and OHLC-valid across all 4,971 rows; reconstruction
deterministically reproducible. Full detail in
`docs/PRICE_POLICY_V1.md`, `data/proofs/price_policy_v1_proof.json`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-045 — Historical Prices V1 is frozen: 14,913 observations, 9 companies (approved)
**Approved rule:** Historical Prices V1 (`data/database/ai_stock_agent.duckdb`,
table `historical_prices_daily`) is **frozen**. Exactly **14,913**
daily price observations across all 9 approved tickers (ORCL, MSFT,
META, NVDA, GOOGL, AMZN, MU, CRWD, PANW) are approved: **1,657 rows per
ticker**, **2020-01-02 through 2026-08-06**. Yahoo Finance historical
chart data is the **approved V1 market-price source** for the current
9-company universe. **Historical Price Policy V1 / D-044 governs all
use of these prices** — no code may read or compute from
`historical_prices_daily` in a way that deviates from D-044's 5 rules.
**No future change to Historical Prices V1 — a data reload, a new
ticker, an extended date range, or a schema change — is permitted
without a new engine version and full validation**, the same
discipline already applied to Annual Data V1, Quarterly Data V1
(D-042), and Derived Metrics V1 (D-043).

**Rationale / origin:** built directly on top of the now-frozen
Historical Price Policy V1 (D-044) and the already-validated 9-company
proof (`docs/9_TICKER_HISTORICAL_PRICE_PROOF.md`), generalizing the
check-only-proven loader (`scripts/158_historical_prices_v1_load.py`)
into a single closed release task
(`scripts/159_historical_prices_v1_release.py`): preflight → exactly
one `--execute` → independent post-load re-verification (re-opening
production read-only, never trusting the loader's own report alone) →
freeze. The dataset was rebuilt entirely from scratch from the
already-saved raw Yahoo JSON responses for this release, not from any
prior proof's computed CSV, so the freeze is independent proof, not a
copy of earlier work.

**Applied**: `scripts/158 --execute` (run exactly once) created
`historical_prices_daily` (`ticker, price_date, open, high, low, close,
adj_close, nominal_open, nominal_high, nominal_low, nominal_close,
volume, dividend, split_ratio, source, source_raw_file,
source_raw_sha256, price_policy_version, created_at`, primary key
`(ticker, price_date)`, with `CHECK` constraints for positive prices,
non-negative volume, valid OHLC, valid reconstructed-nominal OHLC, and
correct policy-version tag) and loaded all 14,913 validated rows in one
atomic transaction, with full backup (SHA-256-verified) beforehand.
**Independently re-verified read-only, directly against the live
database** (not merely the loader's own report): table exists, exactly
14,913 rows, exactly 9 distinct tickers, exactly 1,657 rows per ticker,
correct date range, 0 duplicate keys, 0 missing/negative values, all
OHLC and reconstructed-nominal-OHLC relationships valid,
`price_policy_version` correct on every row, source lineage
(`source_raw_file`/`source_raw_sha256`) present on every row, all split
events and dividend counts match the approved 9-company proof exactly,
NVDA/GOOGL/PANW reconstructed prices match the Historical Price Policy
V1 proof exactly. Every pre-existing production table's row count and
content fingerprint confirmed unchanged: `financial_metric_results`=900,
`quarterly_extraction_runs`=45, `quarterly_metric_results`=1,080,
`derived_metric_results`=405, unique REVIEW_REQUIRED=0, Annual Data V1
checksum unchanged. Full detail in `docs/HISTORICAL_PRICES_V1_BUILD.md`,
`docs/LAST_CLAUDE_REPORT.md`, `data/historical_prices_v1_release_manifest.json`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-046 — Valuation V1 per-share input is frozen: reported diluted EPS, 45/45 company-years (approved)
**Approved rule:** Valuation V1 (`data/database/ai_stock_agent.duckdb`,
table `valuation_v1_per_share_inputs`) is **frozen**. The authoritative
per-share valuation input is **reported diluted EPS**
(`us-gaap:EarningsPerShareDiluted`), taken directly from each
company's own already-locked 10-K, using the single consolidated
(non-dimensional), full-fiscal-year fact matching the filing's own
`report_date`. **Fallback rule**: only if reported diluted EPS cannot
be resolved this way, `net_income / us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding`
may be used instead (not needed for any of the 45 currently-approved
company-years — reported diluted EPS resolved directly for all 45).
**Point-in-time availability rule**: `availability_date =
filing_date` — the diluted EPS value for a fiscal year is not usable
for any historical evaluation dated before that 10-K's own
`filing_date`. **Shares outstanding (diluted weighted-average or
period-end) is explicitly NOT stored in production** — it has no
required use once diluted EPS is available directly, since historical
P/E only needs a per-share figure, not a share count; it was used only
transiently as a cross-check during resolution. **No future change to
Valuation V1 — a new per-share input, a different resolution rule, or
a data reload — is permitted without a new version and full
validation**, the same discipline already applied to Annual Data V1,
Quarterly Data V1 (D-042), Derived Metrics V1 (D-043), and Historical
Prices V1 (D-045).

**Rationale / origin:** closes the valuation-data gap identified in
the Scoring Model V1 blueprint (`docs/SCORING_MODEL_V1_BLUEPRINT.md`),
which had flagged the complete absence of any per-share/EPS/shares
data as the single highest-leverage gap blocking Forward P/E, current
P/E, PEG, and both candidate Entry Price V1 methods. Resolved entirely
from data already on hand: the same 45 approved 10-K accessions behind
Annual Data V1, re-inspected via the already-built XBRL warehouse
(`data/database/xbrl_warehouse_proof.duckdb`) — no new filing was
downloaded, no external/analyst data was used. A real defect was found
and fixed before production load, independently by re-deriving the
result rather than assuming it: the first historical-P/E proof attempt
paired `close` (which Historical Price Policy V1 / D-044 Rule C
retroactively split-adjusts for later splits) with as-reported diluted
EPS (which is never split-adjusted), silently understating NVDA's
2024-02 P/E by exactly the pending 10:1 split factor (5.66 instead of
the correct 56.56); fixed by using `nominal_close` (the original-scale
reconstructed price) for any per-share calculation paired with
as-reported EPS.

**Applied**: `scripts/160_valuation_v1_per_share_inputs.py --execute`
created `valuation_v1_per_share_inputs` (`ticker, fiscal_year,
fiscal_year_end, diluted_eps, resolution_method, eps_source_concept,
accession_number, filing_date, availability_date,
cross_check_calculated_eps, cross_check_diff, valuation_version,
created_at`, primary key `(ticker, fiscal_year)`) and loaded all 45
validated rows in one atomic transaction, with full backup
(SHA-256-verified) beforehand. **Independently re-verified read-only,
directly against the live database**: table exists, exactly 45 rows,
exactly 9 distinct tickers, 0 duplicate keys, 0 missing lineage,
`availability_date = filing_date` on every row, the historical P/E
proof re-derived directly from the committed table for MSFT
(35.8407), NVDA (56.5566), and AMZN (29.3333) matches the pre-load
proof exactly. Every pre-existing production table confirmed
unchanged: `financial_metric_results`=900, `quarterly_extraction_runs`=45,
`quarterly_metric_results`=1,080, `derived_metric_results`=405,
`historical_prices_daily`=14,913, unique REVIEW_REQUIRED=0, Annual
Data V1 checksum unchanged. Full detail in
`docs/LAST_CLAUDE_REPORT.md`, `data/valuation_v1_release_manifest.json`.

This decision requires the user's explicit sign-off to change or extend to a new context, the same as any other entry in this log.

## D-047 — Table freeze replaced by code-enforced versioned append-only writes (approved, user pre-authorized)
**Supersedes, for these six tables only, the specific "no writes without
a new engine version and full regression" restriction stated in
D-042 (`quarterly_extraction_runs`, `quarterly_metric_results`), D-043
(`derived_metric_results`), D-045 (`historical_prices_daily`), and D-046
(`valuation_v1_per_share_inputs`)** — every other rule in those four
decisions (the approved engine versions, the approved data content as of
the freeze, the accounting/price/valuation policies governing what a
correct value IS) remains fully binding and unchanged. `financial_metric_results`
(Annual Data V1) is included in this decision even though its own
freeze was never given a separate D-number in this log.

**Rationale / origin:** the freeze existed only because rows carried no
version column, so a future engine change could silently rewrite a
previously verified number — a missing schema feature that had been
patched with an all-or-nothing policy instead. That policy was also
actively harmful for `historical_prices_daily` specifically: real
market data continues to accrue daily, but the table could not be kept
current without invoking the full "new engine version + regression"
ritual for a plain, mechanically safe daily append — despite the table
already having a primary key on `(ticker, price_date)` that would
reject a duplicate/overwrite attempt on its own.

**Approved replacement mechanism:**
1. **Schema** (`scripts/169_versioned_columns_migration.py`, run once):
   every one of the six tables now carries `engine_version`, `loaded_at`,
   and `is_active` on every row (where an equivalent per-row
   `engine_version` already existed — `quarterly_extraction_runs`,
   `derived_metric_results` — only `loaded_at`/`is_active` were added,
   to avoid a duplicate column with different semantics). Every
   backfilled value for the 2,933 pre-existing rows is a real, traceable
   fact (copied from `extraction_runs`/`quarterly_extraction_runs` via
   existing foreign keys, or from each table's own existing
   `created_at`, or — where no per-row engine label existed at all,
   `historical_prices_daily` and `valuation_v1_per_share_inputs` — the
   exact script that produced every one of those rows, per D-045/D-046)
   — nothing was invented. `is_active = TRUE` on every pre-existing row.
2. **Write guard** (`scripts/167_versioned_write_guard.py`, a shared
   module every future loader into any of these six tables must use):
   enforced INSIDE the write transaction, before COMMIT —
   - Append-only: DELETE/UPDATE/DROP/TRUNCATE/ALTER, and any INSERT
     variant that could silently overwrite (`ON CONFLICT`, `OR REPLACE`,
     `OR IGNORE`), are rejected by the module's only write chokepoint
     (`execute_write_statement`) before ever reaching the database —
     never merely "the API doesn't expose delete", but conducted by
     inspecting the literal SQL of every write attempt.
   - Row count per table can never decrease.
   - The checksum of every pre-existing row (identified by primary key,
     captured before the write, re-selected by that same key after the
     write) must be byte-identical after the write — catches silent
     modification of a prior row even if some future INSERT-shaped
     statement managed to alter one.
   - The actual post-write row-count delta must exactly equal the
     caller's own separately-declared intended delta — catches a
     caller's own counting bug even when every individual INSERT was
     itself legitimate.
   - Any violation raises before COMMIT and rolls back; DuckDB's own
     transaction semantics (verified empirically, not assumed —
     scripts/168 test 4) guarantee a connection closed or crashed
     without COMMIT leaves the on-disk database exactly as it was.
3. **Not provided, by design:** any mechanism to flip a row's
   `is_active` from TRUE to FALSE. That is reserved for a future,
   separately authorized supersession procedure — never an ordinary
   load through the write guard. Nothing in this codebase can currently
   change `is_active` on an existing row.
4. Every future load into any of these six tables must go through
   `scripts/167_versioned_write_guard.py`'s `guarded_versioned_append()`
   (or its `execute_write_statement()` chokepoint directly, for a
   caller with an unusual write shape) — a direct, un-guarded write is
   no longer the approved path for these six tables, the same way a
   direct HTML-scrape was never the approved path for fundamentals.

**Verified** (`scripts/168_versioned_write_guard_tests.py`, 5/5 PASS,
isolated scratch database only): attempted DELETE rejected; attempted
overwrite rejected (both a plain duplicate-primary-key INSERT, which
DuckDB's own constraint correctly raises on and the guard rolls back
after, and an explicit `ON CONFLICT ... DO UPDATE`, rejected by the
guard itself before reaching DuckDB); a load declaring 100 rows while
actually writing 101 rejected; a crash mid-load (connection closed with
2 of N inserts done, no COMMIT) leaves the database completely
unchanged; a legitimate append succeeds with every prior row
byte-identical (checksum equal before/after).

**Migration applied** (`scripts/169 --execute`, `--check-only` proof
first): PID lock → backup (SHA-256-verified,
`data/database/backups/ai_stock_agent_pre_d047_versioned_columns_migration_20260808T083442Z.duckdb`)
→ transaction 1 (ADD COLUMN + backfill, in-transaction validated: 0
NULLs in any new column, every table's row count unchanged, every
pre-existing column's content byte-identical before/after) → COMMIT →
transaction 2 (NOT NULL enforcement only, no data change — split into
its own transaction because DuckDB 1.5.5 cannot run `ALTER COLUMN ...
SET NOT NULL` in the same transaction as a preceding `UPDATE` on a
primary-keyed table: "Cannot create index with outstanding updates",
confirmed by direct reproduction before working around it) → COMMIT →
independent post-commit re-verification, reopening the database
read-only: all six tables' row counts unchanged
(`financial_metric_results`=900, `quarterly_extraction_runs`=45,
`quarterly_metric_results`=1,080, `derived_metric_results`=405,
`historical_prices_daily`=14,913, `valuation_v1_per_share_inputs`=45),
0 NULLs in any new column on any table, `is_active=TRUE` on all 2,933
pre-existing rows across all six tables, every pre-existing column's
content byte-identical, every other table in the database
(`companies`, `sec_filings`, `extraction_runs`,
`historical_review_items`) confirmed completely untouched.

**First real use** (immediately following, same task): appending
current prices to `historical_prices_daily` through this exact
mechanism, bringing it up to date from 2026-08-06 — see the
`scripts/170_historical_prices_append.py` entry below in
`docs/CURRENT_STATE.md` for the full result.

This decision was explicitly pre-authorized by the user as a superseding
entry for D-042/D-043/D-045/D-046; it still requires the user's explicit
sign-off to change or extend further.

