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

## D-048 — Engine code restructured into an installable package, `src/stock_agent`; a permanent pytest suite replaces one-off verification scripts (approved, user pre-authorized)
**Supersedes, for stabilized engine code only, D-011's "create a new
complete file under `scripts`, preserve the prior version" workflow.**
D-011 remains binding for exploratory/analysis scripts as before; it no
longer governs the annual/quarterly XBRL extraction-and-policy engine,
which now lives in `src/stock_agent/` (an installable Python package,
`pip install -e .`) with a real `tests/` suite, instead of accumulating
further copy-paste-and-extend numbered scripts.

**Rationale / origin:** the annual engine had become a 15-script
copy-paste-and-extend lineage (`scripts/60` → `69` → `79` → `82` → `84`
→ `87` → `89` → `92` → five further one-off patch scripts `93`-`105`),
each a near-total duplicate of its predecessor with a small delta
appended — confirmed, not assumed, by a dedicated research pass before
any code was moved (every dependency edge, every `TARGET_FILINGS` scope,
and which specific script actually wrote each of the live 900
`financial_metric_results` rows was traced directly from
`extraction_runs.engine_version` and each script's own source, not
guessed). `scripts/42`-`59` and `scripts/72` (the pre-warehouse
live-Arelle iteration) were confirmed fully dead — unreferenced by any
`importlib`/`subprocess` call anywhere in the repository. No test of any
kind existed anywhere in the project before this decision.

**What changed:**
1. **`src/stock_agent/`** (`extraction/`, `policies/`, `metrics/`,
   `warehouse/`, `storage/`, `filings/`) — a pure structural extraction.
   No formula, threshold, regex, or precedence order was changed; every
   binding accounting policy D-015 through D-047 remains unchanged and
   binding. `scripts/148_quarterly_engine_v5_standard_gaap_fallback.py`
   was edited in place to import `stock_agent.extraction.quarterly`
   instead of its `importlib.util.spec_from_file_location` hack on
   `scripts/89`. `scripts/42`-`59`, `72`, and the fully-ported `79, 82,
   84, 87, 92, 93, 94, 95, 96, 98, 99, 101, 102, 103, 105` were archived
   to `archive/scripts/` via `git mv` — never deleted. `scripts/89` was
   restored to `scripts/` after archiving broke a live transitive
   `importlib` dependency (`scripts/136 ← scripts/149 ← scripts/150`,
   the quarterly regression harness) — out of this work's edit scope to
   change. `scripts/60`/`69` (9 of the 20 primary annual metrics — the
   simpler, always-resolved ones: revenue, net_income, operating_income,
   pretax_income, income_tax_expense, operating_cash_flow, capex,
   free_cash_flow, and untouched company-years of
   stockholders_equity/cash/short_term_investments) remain the source of
   those metrics, unported — an explicit, disclosed scope boundary, not
   a silent gap.
2. **`tests/`** — the project's first real test suite (pytest), 94
   tests: a permanent golden-regression test (formalizing the throwaway
   `scripts/171_recompute_annual_company_year.py`), synthetic-fixture
   unit tests per policy family (including D-017's 4-condition
   `current_debt=0` proof as an explicit pass/fail parametrized matrix),
   regression tests for 5 named historical incidents (XBRL instant-date
   off-by-one, decimals-precision duplicate reconciliation,
   `ixTransformValueError` handling, Meta's "Income (loss) from
   operations" label, Micron's bare "Current debt" label), and
   fail-closed tests (ambiguous evidence → `REVIEW_REQUIRED`, never a
   guess) across multiple policy modules.
3. **`derived_metric_results` (405 rows) and
   `valuation_v1_per_share_inputs` (45 rows) were explicitly left out of
   scope** — `scripts/153`/`scripts/160`, which produce them, were never
   ported, so there is no package code to regression-test against them.
   This is disclosed, not silent.

**Verification (read-only throughout; zero writes to any `.duckdb` file
in `data/database/` at any point in this work):**
- **Annual: 900/900** (`accession_number`, `metric_name`) pairs
  recomputed via `stock_agent` alone match the live
  `financial_metric_results` table exactly — value AND status — across
  all 45 approved company-years plus the 5 supplementary prior-fiscal-
  year accessions. A first pass found 854/900, with 46 rows (AMZN/GOOGL
  `total_debt`-family) carrying an identical value but a more advanced
  status label than production actually has; this was treated as a real
  defect to fix, not an acceptable disclosed gap, per this project's
  standing "never accept a difference" rule. Root cause, confirmed by
  directly querying `extraction_runs.engine_version` for exactly those
  46 rows: they were written by `scripts/79` (D-022), not `scripts/92` —
  AMZN/GOOGL were never inside `scripts/92`'s own bounded 12-filing
  `TARGET_FILINGS` scope for its newer Policy C tier. Fixed by porting
  `scripts/79`'s original, narrower 2-tier resolver alongside both
  scripts' exact historical `TARGET_FILINGS` scopes, and selecting the
  resolver by accession scope — reproducing which historical script
  actually produced each row, not "whichever policy tier happens to
  resolve." Re-verified: 900/900.
- **Quarterly: 1,080/1,080** `quarterly_metric_results` rows reproduced
  exactly, across all 45 company-years, re-running
  `scripts/150_v5_final_release_regression.py` (unmodified) against the
  refactored `scripts/148`.
- **A second real, latent bug was found and fixed by the test suite
  itself**, independently of the golden-regression work above:
  `extraction/core.py` defined `ANNUAL_DURATION_MIN_DAYS = 350` but
  never defined the `ANNUAL_DURATION_MAX_DAYS` constant referenced in
  the same duration filter, unlike every one of its ~20 source-script
  ancestors, which always defined both together as 350/380 — a
  `NameError` waiting to happen on the date-tolerance prior-period
  matching path, silent only because none of the 45+5 tested
  company-years happened to exercise that branch. Fixed by restoring the
  missing constant, exactly matching every source script. Re-verified
  after the fix: still 900/900 and 1,080/1,080.
- Every verification step (both the implementing agents' own runs and
  the orchestrating session's independent re-runs) confirmed
  `data/database/ai_stock_agent.duckdb` and
  `data/database/xbrl_warehouse_proof.duckdb` byte-identical (SHA-256)
  before and after. Final row counts confirmed unchanged:
  `financial_metric_results`=900, `quarterly_metric_results`=1,080,
  `derived_metric_results`=405, `valuation_v1_per_share_inputs`=45.

**Two PRs, both opened and merged**: "refactor: extract stock_agent
package" (PR #2) and "test: policy + golden regression suite" (PR #3),
both squash-merged into `main` after independent re-verification by the
orchestrating session (re-running the recomputation and the full pytest
suite itself, not merely trusting the implementing agent's report).

This decision was explicitly pre-authorized by the user (an autonomous,
non-interactive stage with pre-authorized D-011 supersession for
stabilized engine code); it still requires the user's explicit sign-off
to change or extend further.

## D-049 — D-011's numbered-script workflow fully retired; remaining storage-layer duplication integrated into `src/stock_agent` (approved, live user instruction)
**Fully supersedes D-011** (not just the "stabilized engine code" scope
D-048 carved out). The "create a new numbered file per change, never
patch in place, preserve every old version" convention no longer governs
any part of this project. `CLAUDE.md`'s "Code workflow — binding"
section was rewritten accordingly: engine/library code lives in
`src/stock_agent` and is edited in place (git history is the version
record); tests live in `tests/` (pytest); `scripts/` is for thin,
disposable one-off entry points and exploratory analysis only, and no
longer needs to be preserved forever once its result is captured
elsewhere in the repo.

**Rationale / origin:** direct user instruction, given after reviewing
D-048's result — the convention itself was identified as the root cause
of the 195-script sprawl (most of them full copy-paste duplicates), and
its scope-limited retirement in D-048 (engine code only) was judged
insufficient.

**Immediate follow-up work, same decision:** D-048's package port had
left a real, undone piece of duplication — `scripts/142_task_marker_
guard.py` and `scripts/167_versioned_write_guard.py` were ported into
`src/stock_agent/storage/` (per D-048) but the original standalone
files were never removed, and their remaining callers (`scripts/168`'s
5-test suite, `scripts/143`'s 12-test suite, `scripts/170`'s production
append) still `importlib`-hacked the old script paths instead of the
package. Fixed:
1. `scripts/168_versioned_write_guard_tests.py`'s 5 required tests
   ported verbatim (same assertions, same scenarios) to
   `tests/test_write_guard.py` (6 pytest tests — the overwrite case
   split into its two independently-asserted sub-checks).
2. `scripts/143_task_marker_guard_validation.py`'s 12 required tests
   ported verbatim to `tests/test_task_marker_guard.py`, using pytest's
   `tmp_path` in place of a hand-rolled scratch directory.
3. `scripts/170_historical_prices_append.py` (the still-live D-047
   append mechanism) rewired to `from stock_agent.storage.write_guard
   import guarded_versioned_append` — no behavior change, confirmed by
   `py_compile` and by the ported test suite passing unchanged.
4. `scripts/142`, `143`, `167`, `168` archived to `archive/scripts/`
   via `git mv` (never deleted) — their logic is now exercised only
   through `src/stock_agent/storage/` and the two new test modules.

**Explicitly left untouched, and why:** `scripts/144_warehouse_loader_
v2_production.py` was already ported to `src/stock_agent/warehouse/
loader.py` in D-048, but was NOT archived and NOT rewired here, because
an unrelated, uncommitted, in-progress piece of work already present in
the working tree before this session began (`scripts/161`-`166`, a
filings-archive pipeline, and specifically `scripts/165_filings_
archive_arelle_loader.py`) hardcodes an `importlib.util.spec_from_file_
location` path directly to `scripts/144_warehouse_loader_v2_production.
py`. Moving or rewriting `scripts/144` would silently break that
unrelated, not-yet-reviewed work without the user's knowledge. Left
exactly as-is until that separate work is addressed on its own.

**Verification:** 110 fast tests pass (94 from D-048 + 6 write-guard +
12 task-marker-guard, zero regressions), golden regression unaffected
(this change touches only the storage layer, not `extraction/`,
`policies/`, or `metrics/`, which the golden regression exercises).
`data/database/ai_stock_agent.duckdb` and `xbrl_warehouse_proof.duckdb`
confirmed byte-identical (SHA-256) before and after.

This decision was given directly by the user in a live conversation
(not the earlier autonomous batch instruction); it still requires the
user's explicit sign-off to change or extend further.

## D-050 — Full `scripts/` sweep: 136 one-time/historical scripts archived (approved, live user instruction)
**Direct follow-up to D-049**, at the user's explicit request to go
further than D-049's initial 4-file cleanup. `scripts/` went from 156
files to 20; the other 136 moved to `archive/scripts/` via `git mv`
(never deleted — full git history preserved for every one).

**Classification method:** every file in `scripts/` was placed into
exactly one bucket: (A) still `importlib`-loaded by another non-archived
script, (B) not imported by anything but a genuinely reusable, ongoing
tool per its own docstring, (C) a one-time proof/exploratory
script/single-company-year loader/one-time migration whose result is
already captured elsewhere (a database row, a `docs/*.md` entry, a
manifest JSON, an already-recorded decision in this file), or (D) the
unrelated, uncommitted, in-progress filings-archive work
(`scripts/161`-`166`) already sitting in the working tree before this
session began — left untouched, not evaluated further, not mine to
move.

**Kept in `scripts/` (20 files):** the 13 still-imported files
(`89_panw_zero_long_term_debt_policy.py`,
`107_download_accession_locked_filing_any_form.py`, the quarterly
engine version lineage `118`/`128`/`132`/`136`/`148`/`149`/`150`,
`120_quarterly_production_schema_load.py`,
`121_quarterly_batch_runner.py`,
`139_corrected_warehouse_loader_entry_point_detection.py`,
`144_warehouse_loader_v2_production.py`), the 1 live reusable tool
(`170_historical_prices_append.py` — the ongoing D-047 price-append
mechanism), and the 6 untouched WIP files.

**A documentation defect found and fixed while doing this:**
`docs/CURRENT_STATE.md`'s PR1 entry claimed `scripts/89` was "now
archived" — it was briefly archived during D-048's work, then
**restored** (as D-048's own text, further down the same file, already
correctly says) because it is a live transitive dependency of
`scripts/136`/`149`/`150`. The stale first mention was corrected in
place; `scripts/89` correctly remains in `scripts/`, confirmed by this
sweep's own mechanical "still imported" check.

**Historical citations not rewritten, by design:** dozens of entries in
this file (D-020 through D-047) cite one-time scripts by their
`scripts/NNN_name.py` path as historical evidence of what happened at
decision time. Those citations were deliberately left as-is rather than
mass-edited to `archive/scripts/NNN_name.py` — rewriting binding
historical decision text for a routine file relocation was judged
higher-risk (transcription error in binding text) than the alternative:
**if a script cited anywhere in this file by path is not found under
`scripts/`, check `archive/scripts/` — the filename itself never
changes, only its folder.**

**Verification:** 110 fast tests pass (0 regressions — this sweep moved
files only, touched no logic in `src/stock_agent` or any file that
stayed in `scripts/`). Every remaining `importlib` reference among the
20 kept files confirmed to resolve to an existing file (no broken
imports introduced). Both production databases confirmed byte-identical
(SHA-256) before and after.

This decision was given directly by the user in a live conversation; it
still requires the user's explicit sign-off to change or extend
further.

## D-051 — D-P3 ratified: some frozen values are approvals, not derivations; the golden regression must only test what the engine can reproduce (approved, user-directed)

**Ratifies D-P3's option 1** (docs/CLEANUP_DECISIONS_PENDING.md, the
recommended option). PANW 2021-07-31's `average_invested_capital`
(698,750,000, `PASS`) and the `roic` combined from it rest on D-027 item
7 — reuse of the prior filing's own previously-approved result — not on
a calculation today's engine (D-P3 option B: prior-year invested_capital
is always recomputed from the filings, never read from production) can
redo, because PANW 2020-07-31's own `invested_capital` is a real,
unresolved extraction gap (a D-017 zero-inference failure on an
unclaimed `us-gaap:ConvertibleDebtNoncurrent` candidate — confirmed by
direct recomputation, nothing to do with combined filings).

**Implemented in `tests/test_golden_regression.py`**: a named,
documented `APPROVED_NOT_REPRODUCIBLE` set — currently exactly
`{(PANW, 2021-07-31, average_invested_capital), (PANW, 2021-07-31,
roic)}` — is excluded from the annual comparison loop, and
`EXPECTED_ANNUAL_COMPARED_PAIRS` is derived from it (900 − 2 = 898)
rather than hard-coded, so the test's own arithmetic stays honest if the
set ever changes. No production data was modified — the frozen
`financial_metric_results` row for these two metrics is untouched,
still exactly 698,750,000 / `PASS`. Only the test's claim about what it
verifies changed: it no longer silently compares a passthrough value
against itself for these two cells.

**Verification**: `test_annual_golden_regression_900_rows_byte_identical`
passes — 898/898 pairs byte-identical, read-only, independently
re-run by the orchestrating session, not merely trusted on report.

## D-052 — D-P1 ratified and safely re-implemented: combined filings use the registrant's own consolidated statements (approved, user-directed)

**Ratifies D-P1's decision** (docs/CLEANUP_DECISIONS_PENDING.md): for a
combined filing (a utility holding company's 10-K covering the parent
and its subsidiary registrants), the figures that represent "the
company" are the registrant's own consolidated statements, never a
subsidiary's.

**The earlier "REVERTED" status was itself a misdiagnosis, now
corrected.** D-P1's own text records this fix being backed out after
appearing to regress PANW 2021-07-31's `average_invested_capital`
(`PASS` → `REVIEW_REQUIRED`) — but D-P3's later, more careful diagnosis
explicitly retracts that: "I first assumed my combined-filing fix caused
it and reverted that fix. The failure persisted... the diagnosis I gave
at the time was wrong." The real cause was the D-P3 prior-year-lookup
circularity (see D-051), unrelated to statement-role selection. This was
independently re-confirmed this session: PANW's own presentation tree
carries no role duplication for any metric this fix touches (checked
directly for `cash_and_equivalents`/`short_term_investments`/
`stockholders_equity` on both PANW 2020-07-31 and 2021-07-31 — each
resolves to exactly one role).

**Implemented**: `_narrow_to_registrant_statements` (already written,
previously retained unused) is now wired into
`extraction/core.py`'s `identify_canonical_row`, narrowing
`base_candidates` before the tier-A/tier-B row selection. It is a no-op
whenever candidates already span at most one distinct role (i.e. every
single-registrant filing, including all of the frozen 45), so it can
only ever change behavior for a genuine multi-role combined filing.

**What this fix does and does not reach, measured directly against
Exelon/Constellation** (not assumed): it resolves the
`identify_canonical_row`-based simple metrics (revenue, net_income,
operating_income, pretax_income, income_tax_expense, cash_and_
equivalents, short_term_investments, stockholders_equity) for any
combined filing whose registrant role is identifiable from the title
alone (the Constellation "unqualified vs. `, Parent`" convention). It
does **not** resolve two separately-diagnosed problems it was tempting
to conflate with it:
1. Exelon's filings (2023 onward) use the convention where **every**
   role is qualified, including the parent's own ("... - Exelon" vs.
   "... - ComEd") — `_narrow_to_registrant_statements`'s own docstring
   already documents this as unresolved by design (needs the
   registrant's legal name, not just role titles); confirmed unchanged
   by this fix.
2. The `current_debt`/`total_debt`/`cash`/`equity` family's "multiple
   ancestry-classified current-debt candidates" failure on both
   Constellation (single-registrant filing, no role duplication at all)
   and Exelon is a **different, genuine multi-instrument-debt
   classification gap** (measured: Constellation legitimately reports 3
   distinct current-debt line items — "Long-term debt due within one
   year", "Borrowings from money pool with Exelon", "Short-term
   borrowings" — with no shared calculation/sibling grouping to sum
   them by), not a combined-filing artifact. An attempt to extend the
   registrant-narrowing fix into this path
   (`resolve_debt_classification_by_ancestry_from_warehouse`) was
   prototyped and measured to make **zero difference** for either
   company (confirmed: identical candidate counts and identical error
   before/after), so it was reverted rather than kept as dead code.

**Verification**: fast suite unaffected (110→148 unrelated to this
change); `test_annual_golden_regression_900_rows_byte_identical` and
`test_quarterly_golden_regression_1080_rows_reproduced` both pass with
this fix live (both engines share `identify_canonical_row`/
`BUILT_IN_METRICS`).

## D-053 — Two further engine defects found and fixed while verifying D-P1 (approved, user-directed: "fix now")

Found during direct measurement of D-P1's actual effect on Exelon/
Constellation, not part of D-P1/D-P2/D-P3's original scope, but clearly
in-scope for "continue the vocabulary loop":

**1. The `comprehensive` role-exclude pattern rejected combined
income-statement titles wholesale.** `revenue`, `net_income`,
`operating_income`, `pretax_income`, and `income_tax_expense` all
excluded any statement role whose title contained "comprehensive" — added
to avoid picking up a filer's *separate* "Statement of Comprehensive
Income". Measured: Constellation and Exelon (and, per a direct count
against the wider universe, hundreds of other REVIEW_REQUIRED rows
project-wide) title their PRIMARY income statement "...Statements of
Operations **and** Comprehensive Income" — a single common combined
statement, not a separate one — so the exclusion left these five metrics
with **zero** candidate rows, independent of any combined-filing
ambiguity.

Fixed with `COMPREHENSIVE_STANDALONE_EXCLUDE_PATTERN =
r"^(?!.*operations).*comprehensive"` (`extraction/core.py`), applied to
all five metrics in place of the bare `r"comprehensive"`: excludes a role
only when it contains "comprehensive" **without** also containing
"operations" anywhere — i.e. only a genuinely standalone comprehensive-
income statement, never a combined one.

**2. `_resolve_current_debt` did not catch `TargetRowNotFound` from
`resolve_current_debt_components_from_warehouse`.** Every other raise
site in the same tier chain (e.g. the D-017 zero-inference attempt) was
already caught and converted to `REVIEW_REQUIRED` for `current_debt`
alone; this one, the first tier tried, was not — so a single ambiguous
debt candidate (see D-052's Constellation evidence) crashed the entire
`compute_company_year` call with an uncaught exception instead of
failing closed for `current_debt` alone. Fixed in `metrics/annual.py`'s
`_resolve_current_debt` with a `try/except TargetRowNotFound`, mirroring
the pattern already used at every other tier in the same function.

**Verification**: fast suite (154 passed, incl. new tests), annual golden
regression (898/898) and quarterly golden regression (1080/1080) all
pass with both fixes live — the frozen 45 never exercises either crash
site, so this could only ever add coverage, never regress it, and the
golden regression confirms exactly that.

## D-054 — D-P2 implemented as a capex component aggregator, not a label broaden (approved, user-directed: "build the aggregator")

**Supersedes the literal wording of D-P2's original question**
("is 'Acquisitions of Generation Facilities' capex?") with a more
robust implementation, after evidence showed the literal question was
the wrong shape for a correct fix.

**What a label-only fix would have gotten wrong, measured directly
against AEP's own filings (2020-2025):** AEP's real, dominant capex line
every single year is `us-gaap:PaymentsForConstructionInProcess`
("Construction Expenditures") — never matched by any wording in scope.
"Acquisitions of Generation/Renewable Facilities" is a real but
*smaller*, separate line reported *alongside* it, and its own label
drifted three times in three years for the identical underlying concept:
"Acquisition of Assets" (2020-2022) → "Acquisitions of Renewable Energy
Facilities" (2023) → "Acquisitions of Generation Facilities" (2025).
Recognizing only the generation-facilities wording would have resolved
`capex` to a single clean `PASS` that **silently omits the larger
construction-spend line** — understating capex and overstating free
cash flow. A confidently-wrong `PASS` is worse than the honest
`REVIEW_REQUIRED` it would have replaced.

**Implemented**: new module `policies/capex_components.py`,
`resolve_capex_by_component_aggregate`. Matches by GAAP **concept**, not
label wording — `PaymentsForConstructionInProcess`,
`PaymentsToAcquireProductiveAssets`, and
`PaymentsToAcquirePropertyPlantAndEquipment` are all taxonomy-defined
for a physical asset acquisition, never a business combination
("ProductiveAssets" is literally the taxonomy's term for physical
operating assets — D-P2's own "scoped to physical operating assets
only" framing, made structural instead of wording-dependent).
Deliberately excludes `PaymentsForNuclearFuel` (an operating/inventory
cost) and every `PaymentsToAcquireBusinesses*` concept (genuine M&A,
D-P2's explicit non-goal). Sums whichever of the three concepts are
present as distinct rows in the registrant's own cash-flow statement
(reusing D-052's registrant-narrowing for combined filings); a single
match behaves exactly like the existing label-based lookup; a component
identified but unresolvable fails closed to `REVIEW_REQUIRED` rather
than silently dropping out of the sum. Tried only as a fallback, after
the existing single-row `identify_canonical_row` lookup has already
failed to find exactly one candidate — never overrides an
already-working label match, and never touches the 45 frozen
company-years (their `capex` is a production passthrough in
`metrics/annual.py`, never routed through this module).

**Verification**: 6 new synthetic-fixture unit tests
(`tests/policies/test_capex_components.py`), covering no-match,
single-match, two-component sum, the nuclear-fuel/business-acquisition
exclusion, a missing-component fail-closed case, and combined-filing
narrowing. Golden regression unaffected by construction (capex is
passthrough for the frozen 45); confirmed unaffected by the same
annual/quarterly golden regression runs covering D-053.

## D-055 — Full-universe coverage re-measured after D-051 through D-054: 74.36% → 79.86%, zero unintended regressions (result, not a new policy)

Read-only re-measurement (`scripts/190_remeasure_full_universe_coverage.py`,
never writes production) of all 782 company-years currently in the
warehouse, using the engine as fixed by D-051-D-054. Compared against
the exact same 777 of those company-years' CURRENT stored production
coverage (5 have no stored baseline yet to compare against).

| | Pass rate |
|---|---:|
| Before (current production, same 777 company-years) | 74.36% (11,555/15,540) |
| After (this session's engine) | **79.86%** (12,411/15,540) |

**204 company-years improved, exactly 1 regressed** — PANW 2021-07-31,
which is D-051's own correct, intended outcome (an honestly-unreproducible
approved value now correctly reporting `REVIEW_REQUIRED` in a fresh
recomputation instead of a passthrough coincidence). No other
company-year regressed.

**A measurement-script pitfall found and fixed before trusting this
number**: the first two re-measurement passes showed additional false
"regressions" for GOOGL/MSFT/MU/ORCL — all among the frozen 9 — because
the script initially recomputed their 8 "out of scope" flow metrics (and,
separately, `average_invested_capital` for a ticker's true first fiscal
year) via `identify_canonical_row` directly, instead of reading the
already-approved production value the way `metrics.annual.compute_full_
company_year` correctly does for those specific cases. Fixed by adding
the same passthrough-for-the-frozen-9 special cases the real engine
already has; re-running then reproduced the golden regression's own
guarantee (byte-identical for the frozen 45) in this wider measurement
too, leaving only the one expected, correct regression.

**Largest single-company gains**: AEP (2/20 → 15/20, D-054's capex
aggregator plus D-053's comprehensive-statement fix), Constellation
Energy (0/20 → 8-11/20 depending on year, D-052's registrant narrowing
plus D-053), Walmart (8/20 → 17/20, D-053), DocuSign (12/20 → 20/20,
D-053).

**Not yet resolved, carried forward into the vocabulary loop**: Exelon's
"every role qualified" convention (D-052), the genuine multi-instrument
current-debt classification gap measured on Constellation (D-052), and
whatever the remaining ~20% of REVIEW_REQUIRED rows turn out to need —
none diagnosed yet as part of this session's work.

This is a result entry, not a new policy — D-051 through D-054 are the
decisions; this records what re-measuring them, honestly, produced.

## D-056 — D-055's improved results loaded into production as a new engine version (result, user-directed: "LOAD")

`scripts/191_reload_universe_metrics_v3.py --execute`, engine version
`v3-vocabulary-cleanup (scripts/191, D-051-D-054)`, appended through the
write guard — never an UPDATE, never touched an existing row. Scope:
exactly the 732 accessions `engine_version = 'v2-pilot-warehouse-native
(scripts/188)'` (scripts/188's original pilot/universe-expansion load).
The frozen 900 rows and every other pre-scripts/188 engine version are
untouched and unreachable by this script by construction — it never
queries or writes anything outside this one engine-version's accessions.

**Two further bugs found and fixed before this could run cleanly**
(both in the loader script itself, not the core engine — kept there
deliberately, since further engine changes were explicitly deferred by
the user this session):
1. The script's own prior-fiscal-year tracking only knew about a
   ticker's prior year if that year was ALSO in its own 732-accession
   target list. For a frozen ticker's newest fiscal year (whose earlier
   years are frozen and so never in this script's scope, e.g. META/MSFT/
   NVDA/ORCL's 2025), that made a genuinely resolvable prior year look
   missing, silently passing through a stale, pre-fix `REVIEW_REQUIRED`
   for `average_invested_capital`/`roic` instead of recomputing them
   correctly. Fixed by replicating `metrics.annual.compute_full_company_
   year`'s own prior-year mechanism exactly (`production_lookup.
   prior_report_date_for` across ALL of a ticker's known dates, then an
   always-fresh recompute of that prior year from the warehouse) instead
   of the script's own narrower self-tracking. Confirmed: all 4 affected
   company-years now correctly resolve (18/20 → 20/20 each).
2. A SECOND, previously-unknown uncaught-exception site of the same
   D-053 shape — `resolve_long_term_debt` (`policies/debt_current_long_
   term.py` ~line 726) also calls `resolve_current_debt_components_
   from_warehouse` internally, and this call site was not wrapped the
   way D-053 wrapped the one in `_resolve_current_debt`. Hit while
   recomputing a prior year for the fix above. Given the user deferred
   further engine changes this session, patched narrowly in the loader
   script only (fails closed to `REVIEW_REQUIRED` for that prior year
   instead of crashing the whole load) rather than in `metrics/annual.py`
   itself — **`compute_full_company_year` itself still has this exact
   latent gap** (it makes the identical unwrapped call for its own
   prior-year lookup) and should get the same D-053-style fix in a
   future session.

**Final result (`data/universe_metrics_v3_reload_result.json`):** 732
company-years, 14,640 result rows, coverage 11,513/14,640 = **78.6%**
for this exact scope (matches D-055's wider 79.86% measurement, which
also includes the frozen 45's own already-correct rows via passthrough).
`financial_metric_results`: 15,540 → 30,180 (+14,640, exactly the
declared count). `extraction_runs`: 904 → 1,636 (+732). **The 900 frozen
rows and all 14,640 non-scripts/188 rows confirmed present and
unmodified** (checksummed by the write guard during the transaction,
independently re-counted after: 900 before, 900 still present).
Read verified independently after load: `production_lookup.
latest_metric` (the standard read path every consumer uses) now returns
the new `v3` values for a spot-checked case (META 2025-12-31's
`average_invested_capital`: `REVIEW_REQUIRED` → `PASS_DIRECT_AGGREGATE`,
$164,236,500,000).

Backup taken before the write:
`data/database/backups/ai_stock_agent_pre_v3_reload_20260811T110148Z.duckdb`.

This is a load result, not a new policy or engine change.

## D-057 — Scoring Model V1 built and loaded per the existing blueprint (approved, user-directed: "enter option B, build work that runs unattended")

Implements `docs/SCORING_MODEL_V1_BLUEPRINT.md` Stage 3 exactly (no
weights or formulas invented fresh — the blueprint itself, an
already-existing planning document, specified all 9 factors and their
20/15/10/15/10/10/5/5/10... weights before this session began). New
package `src/stock_agent/scoring/`:

- `inputs_v1.py` — the 9 raw factors (revenue growth, ROIC level/trend,
  operating margin, FCF growth/margin, balance-sheet strength ratio,
  CapEx discipline, distance from high), each read only from
  already-frozen, point-in-time-gated data (Annual Data V1, Derived
  Metrics V1, Historical Prices V1) — no new extraction, no external
  data. A factor genuinely unavailable (e.g. a ticker's first fiscal
  year, no prior year to compute growth against) is `None`, never
  fabricated.
- `composite_v1.py` — combines the 9 factors into one 0–100 score via
  continuous percentile rank **within the same fiscal year** (never
  across years), weights renormalized over only the factors actually
  available for a given company-year rather than scoring a missing one
  as 0.

**A real bug found and fixed before scaling past the small proof**
(per CLAUDE.md's own discipline — proof on 3 companies before the full
45): `sec_filings.prior_report_date` is off by one calendar day for
every non-first fiscal year of MU and NVDA (8 of 45 rows) — a
pre-existing data quirk, not something this session introduced. An
exact-match lookup keyed on it silently found nothing and produced a
false `NO_PRIOR_YEAR`/`REVIEW_REQUIRED` for 4 of the 9 factors on those
company-years. Fixed by deriving the prior year the same way
`metrics.annual.compute_full_company_year` already does —
`production_lookup.prior_report_date_for` across the ticker's own known
dates — never the unreliable stored column.

**Manual plausibility review** (blueprint Stage 6 step 4's own success
criterion) across all 45 company-years found no unexplained anomaly:
NVDA scores highest in FY2024 (93.8/100 — the AI-boom year), Micron
lowest in FY2023 (4.4/100 — the memory-industry downturn year), both
matching public knowledge independently of this project's own data.

**Loaded** into two new production tables, `scoring_inputs_v1` and
`scoring_composite_v1` (45 rows each, 9 distinct tickers, 0 duplicate
keys), via the write guard, backed up first, independently re-verified
after. 12 new tests. Full detail: this commit's own message and
`data/scoring_model_v1_load_result.json`.

## D-058 — Entry Price Method 1 built and loaded; supersedes the blueprint's own "blocked on shares outstanding" assessment (approved, same directive)

Implements `docs/SCORING_MODEL_V1_BLUEPRINT.md` Stage 4 Method 1: each
fiscal year's own P/E (that year's own filing-date price ÷ that year's
own as-reported diluted EPS), positioned against the SAME company's own
trailing (up to 5-year) P/E history. No peer comparison, no external
estimate, no hard-coded buy/entry threshold — the percentile position
is reported as data, not a trading signal.

**The blueprint's own Stage 6 step 1 ("extend XBRL extraction to add
diluted weighted-average shares outstanding — the single highest-
leverage next step") turns out to be unnecessary for this method.**
D-046, a decision already on record before this session, resolved
reported diluted EPS directly for all 45 company-years from each
filing's own per-share fact — a share count was only ever a transient
cross-check during that resolution, never a required stored input.
P/E = price ÷ diluted_eps needs no shares count at all. This was not
recognized until this session re-read D-046 while re-reading the
blueprint it was itself written to support.

**Reuses D-046's own measured pitfall as a hard rule**: diluted EPS is
always as-reported, never retroactively split-adjusted, so this module
reads `nominal_close` only, never `close`. Verified by independently
reproducing D-046's own already-validated MSFT (35.84) and NVDA (56.56)
P/E figures exactly, and by correctly handling GOOGL's 2021 pre-split
diluted EPS (112.20, vs. 4.56–10.81 in adjacent post-split years)
without distortion.

**Loaded** into a new production table, `entry_price_v1` (45 rows, 37
with a resolved P/E — 8 correctly `UNDEFINED_NONPOSITIVE_EPS` for
loss-making fiscal years, never guessed). 5 new tests. Full detail in
`data/entry_price_v1_load_result.json`.

## D-059 — QQQ (Nasdaq-100 benchmark) loaded, closing blueprint Stage 5 gap #4 (approved, same directive)

Loads QQQ (the Nasdaq-100 tracking ETF) into the existing
`historical_prices_daily` table (a new ticker value, not a new table)
via the exact same Historical Price Policy V1 pipeline (D-044) already
proven on the 9 approved tickers — same free Yahoo source, same Rule
A/C reconstruction, no new provider, no signup. 1,659 rows,
2020-01-02 → 2026-08-10, no splits found.

QQQ (the ETF) was chosen over `^NDX` (the raw index) deliberately: an
index cannot itself be bought or held, and "excess return over
Nasdaq-100" (the user's stated goal for this project stage) can only
mean something concrete against an investable alternative — QQQ's own
expense ratio and tracking difference are the honest cost of that,
not a reason to prefer an un-investable figure that would silently
overstate what a real comparison portfolio could have earned.

Full detail in `data/nasdaq100_benchmark_load_result.json`.

## D-060 — Scoring Model V1's first backtest run: real result, with a prominent survivorship-bias caveat (result, not a new policy)

Implements `docs/SCORING_MODEL_V1_BLUEPRINT.md` Stage 6 step 5:
`stock_agent.scoring.backtest_v1` ranks each fiscal year's companies by
`scoring_composite_v1`'s composite score, "enters" the top-3 and
bottom-3 at their own filing_date, and measures forward return at
6/12/24/36-month horizons against QQQ over the exact same dates
(`adj_close` used consistently for entry and exit — the correct total-
return basis, distinct from Entry Price V1's `nominal_close`).

**Result** (`data/scoring_model_v1_backtest_result.json`): top-3 beat
QQQ on average at every horizon — mean excess return 9.1% (6mo) /
15.8% (12mo) / 42.5% (24mo) / 97.0% (36mo), hit rate 67%–100% across
4–6 independent annual entry points.

**The load-bearing caveat, stated in the backtest module's own
docstring before any number, not buried in a report**: **top-3 did NOT
reliably beat bottom-3 within the same universe** — the spread is
negative in most fiscal years (e.g. FY2023 36mo: top +257% vs. bottom
+729%; FY2022 12mo: top +19.6% vs. bottom +100.0%). Combined with the
9-company universe being a hand-picked watchlist of already-successful
companies (`docs/PROJECT_CONTEXT.md`), not a point-in-time-selected
one, **the positive excess-return numbers are better explained by
survivorship bias in which 9 companies were ever in this dataset than
by validated evidence that Scoring Model V1's factors themselves pick
winners.** This is exactly the CLAUDE.md-mandated warning against
survivorship bias, applied to a real result rather than left as a
generic caveat.

**Deliberately not built, and why**: no compounded multi-year equity
curve (each fiscal-year entry is reported as an independent
observation — overlapping holding periods were not given the careful
handling a true continuous curve requires); no max-drawdown or
volatility figure (both need a genuinely continuous daily-return path,
which 4–6 independent annual points cannot honestly provide).

**The obvious next step, not attempted this session**: rerun Scoring
Model V1 and this backtest against the survivorship-free ~150-company
universe already loaded in production from an earlier session
(D-051–D-056) — the only way to find out whether the top-minus-bottom
spread problem is a small-sample artifact of 9 hand-picked companies,
or a genuine sign the current 9 factors don't yet differentiate
winners from losers.

8 new tests. Read-only script (`scripts/195`) — no production table,
matching this project's existing pattern for exploratory result JSON.

## D-061 — Scoring Model V1 extended to the full universe; the composite score shows no measurable predictive power there (result, user-directed: "extend to 152 companies, find concrete conclusions")

**Two prerequisite fixes, before the extension could be trusted:**

1. `revenue_growth`/`operating_margin` (Scoring Inputs V1) previously
   read `derived_metric_results` (Derived Metrics V1, D-043), frozen at
   exactly the original 9 tickers. Recomputed directly from
   `financial_metric_results` instead (same formulas D-043 used) so
   the model works universe-wide. Verified before trusting: 88 of 90
   (factor, company-year) pairs for the original 9 tickers match
   D-043's stored values exactly; the 2 differences are a genuine
   improvement (MU/PANW's first frozen year now resolves `revenue_
   growth` from a supplementary accession D-043's narrower scope never
   reached), not a regression.
2. Extended `scoring_inputs_v1`/`scoring_composite_v1` to the 732
   company-years not already covered by the frozen 45 (`scripts/196`)
   — appended, the original 45 rows untouched. Data quality here is
   genuinely lower than the frozen 9 (mean `weight_covered` 60% vs.
   ~90%+), a real, expected consequence of the wider universe's own
   ~79% metric-level coverage (D-051–D-056), not a bug — every row
   still states exactly how much of the full weighting it rests on.

**The predictive-power question, answered directly** (`scripts/197`,
`stock_agent.scoring.predictive_analysis_v1`): does `composite_score`
predict beating QQQ by ≥5% over the next 12 months, tested on the full,
survivorship-free universe rather than the 9-company hand-picked one
D-060's backtest used?

**No, essentially not.** Spearman rank correlation between
`composite_score` and 12-month excess return: **-0.022** (all 613
resolvable company-years) / **-0.041** (262 company-years with
`weight_covered ≥ 70%`, the higher-confidence subset) — indistinguishable
from zero. Decile buckets are not monotonic: in the quality subset, the
**lowest**-scoring quintile had the **highest** mean excess return
(+11.8%), not the highest-scoring one. This directly confirms, with
measured evidence rather than a generic caveat, D-060's survivorship-
bias warning — the 9-company backtest's apparent outperformance is
attributable to which 9 companies were ever in that dataset, not to
the scoring methodology picking winners.

**No individual factor shows strong standalone signal either**
(strongest: `capex_discipline_deviation`, +0.11 in the quality subset —
still weak). One result is a genuine, explainable finding rather than
noise: `balance_sheet_strength_ratio` correlates **negatively** with
excess return (-0.12 to -0.15) — the OPPOSITE of the factor's own
assumption (lower leverage = better). In this specific 2020–2026
window (COVID recovery, 2022 rate-hike bear market, 2023–2025 AI boom),
more-leveraged, growth-oriented companies tended to outperform more
conservative ones. This may be a real regime effect (leverage/growth
factors are well documented to invert across macro regimes in the
broader factor-investing literature) rather than a flaw in the ratio
itself — but it is not evidence the ratio works as intended over this
window.

**Concrete conclusions:**

1. **Scoring Model V1, as currently weighted, should not be trusted to
   pick market-beating stocks yet.** The positive-looking 9-company
   backtest (D-060) was misleading on its own; this wider test is the
   one that matters, and it shows no measurable edge.
2. **The model has no valuation dimension at all** — all 9 factors are
   quality/growth/risk, zero are "is this cheap for what it is."
   Equity-factor research consistently finds valuation-at-a-reasonable-
   quality combinations outperform pure quality alone; Entry Price V1's
   P/E-vs-own-history percentile (D-058) already exists for the
   original 9 tickers and was never tested here as a candidate factor.
   **Recommended next step**: extend diluted-EPS resolution (D-046's
   method) to the wide universe, add a valuation factor to the
   composite, and rerun this same predictive-power analysis to see if
   it moves the correlation.
3. **The weights (20/15/10/15/10/10/10/5/5) came from the blueprint's
   own a priori reasoning, never calibrated against actual predictive
   power** — this analysis is the first time they were checked against
   real outcomes. Re-weighting toward the (weakly) positive factors
   and away from the negative/zero ones is tempting but **must not be
   done directly on this same dataset** — that would be in-sample
   curve-fitting on the exact data just used to diagnose the problem,
   precisely what CLAUDE.md's overfitting warning exists to prevent.
   Any reweighting needs a genuine out-of-sample split (fit on one
   period, test on another) before being trusted.
4. **No sector-neutrality and no regime control** — factors are ranked
   against the WHOLE universe regardless of industry, and the entire
   test window sits inside one unusual macro regime. Both are
   candidate explanations for the weak correlations that a longer
   history or a sector-relative ranking (already flagged as
   out-of-scope for V1 in the blueprint) could help distinguish from a
   genuinely broken model.
5. **A real, separate data gap surfaced along the way**: 10 tickers
   (ATVI, SPLK, CERN, XLNX, ANSS, MXIM, ALXN, SGEN, WBA, FI) have no
   `historical_prices_daily` rows at all — several are exactly the
   kind of acquired/delisted companies a survivorship-free backtest
   most needs (Activision, Splunk, Cerner, Xilinx, ANSYS, Maxim,
   Alexion, Seagen were all acquisition targets in this window). They
   are silently excluded from every return-based number above,
   reintroducing a mild survivorship bias this analysis does not
   correct. This is the same delisted-company price-history gap
   already tracked as pending (EODHD signup) — not solved here.

Nothing production-frozen was changed. `scoring_inputs_v1`/`scoring_
composite_v1` grew by 732 rows (appended, D-057's original 45
untouched); no new production table for the analysis itself (matches
this project's pattern for exploratory result JSON).

## D-062 — Scoring Model V2 candidate built and honestly out-of-sample tested: still no measurable predictive power (result, user-directed: "build weighted indicators that predict beating Nasdaq-100 by 5%/year")

**Direct answer to the user's request, done with the discipline D-061
demanded** (never select/weight factors on the same data used to
validate them): built a candidate model, `stock_agent.scoring.
model_v2_candidate`, that (1) adds a 10th factor — current-year P/E,
cross-sectionally ranked, using D-046's diluted-EPS resolution
extended to the wide universe this same day — and (2) selects and
weights factors using ONLY a **train period's** correlation with
12-month forward excess return over QQQ, validated **purely on a
held-out test period it never touched during selection**.

**Split**: train = filings before 2023-01-01 (270 company-years), test
= filings from 2023-01-01 onward (343 company-years) — a time split,
not a company split, so it respects the same point-in-time discipline
this project applies everywhere else (an investor building this
strategy in early 2023 could only have used what was already filed).

**Prerequisite, same session**: extended D-046's diluted EPS resolution
to the wide universe (`stock_agent.scoring.valuation_wide_v1`) — 681 of
732 accessions resolved cleanly, verified against D-046's original 45
values (0 mismatches) before trusting it further. Two tickers (CDNS,
ILMN) changed fiscal year-end mid-stream, colliding on `valuation_v1_
per_share_inputs`'s `(ticker, fiscal_year)` primary key — resolved by
keeping the later (completed) fiscal year, caught safely by the write
guard's transactional rollback on the first attempt (no partial write).

**Selected on train** (of 10 candidates, at a `min_abs_correlation` of
0.03): `revenue_growth` (50%), `fcf_margin` (33%), `operating_margin`
(17%). **The new valuation factor did not clear the bar** — it showed
no meaningful positive train-period relationship with returns, even
in-sample.

**Out-of-sample result on test (same-population comparison, n=270 —
both V1 and V2 scored the identical company-years)**:

| | Spearman correlation with 12mo excess return |
|---|---:|
| V1 (original 9-factor, fixed weights) | 0.031 |
| V2 (train-selected 3-factor + valuation attempt) | **0.002** |

**V2 is not better than V1 — if anything, marginally worse**, on data
it never saw during selection. Decile analysis on test is not
monotonic: bucket 2 outperforms bucket 1, and bucket 4 (the
second-highest-scoring quintile) is the single worst-performing
bucket. The train-period relationship these factors showed did not
generalize.

**This is the textbook overfitting signature, now demonstrated
empirically rather than left as a warning.** D-061 already showed the
full-dataset (in-sample) correlation was near zero; this result shows
that even disciplined, train-only factor selection — exactly the fix
D-061 recommended — does not produce a model that holds up out of
sample. That is meaningfully stronger and more credible evidence than
D-061's in-sample check alone.

**Concrete, direct answer to "build weights that predict beating
Nasdaq-100 by 5%/year"**: with the current 9 (now 10, including
valuation) factors, this project's current data window (2020–2026),
and a 12-month annual-rebalance horizon, **no set of weights this
session found — including one chosen with proper out-of-sample
discipline — achieves that.** This is not a methodology failure; it is
what a real, non-overfit test of this factor set looks like. Concrete
directions for a future attempt, not pursued this session:

1. **Try other horizons and rebalance frequencies** — 12 months annual
   was the only one tested here; the blueprint's own 6/24/36-month
   windows (D-060) were never run through this same train/test
   discipline, nor was quarterly rebalancing.
2. **Sector-neutral ranking** — flagged as out-of-scope for V1 from the
   start (`docs/SCORING_MODEL_V1_BLUEPRINT.md`); ranking within-sector
   rather than within the whole universe could surface a signal the
   current whole-universe ranking washes out.
3. **A genuinely different valuation formulation** — current-year raw
   P/E (this session) and own-history P/E percentile (D-058) both
   showed no signal; a cross-sectional P/E-to-growth or EV-based
   measure was not tried.
4. **More history** — the entire train+test window sits inside one
   unusual macro regime (COVID recovery → 2022 rate-hike bear market →
   2023–2025 AI boom); 2020–2026 may simply be too short and too
   regime-concentrated a window for annual-rebalance quality/growth
   factors to show their historically-documented edge.
5. **The 10-ticker delisted-company price gap** (D-061) is still open
   and still not corrected for here.

Nothing production-frozen was changed; this session's scoring/
valuation extensions (D-057, D-058-extension, D-061) are all additive.
No new production table for this analysis (model research, not a
lineage-tracked fact).

## D-063 — 5-year hold tested (user-requested); composite score is NEGATIVELY related to 5-year returns; entry P/E characterizes actual winners/losers better (result, user-directed: "test over 5 years back", then "characterize what worked" if still inconclusive)

**Two follow-up requests, both answered directly.**

**1. "Test over 5 years back"**: added CAGR-based (annualized)
excess-return support to `predictive_analysis_v1` (identical to the
raw 12-month figure at 12 months — verified by test — so this is a
strict generalization, not a different metric) and re-ran the
predictive check at a 60-month horizon.

**Sample-size reality, checked before trusting anything**: a 60-month
forward return needs 5 full years of price data past the entry date.
With price data ending 2026-08-10, only 133 company-years qualify at
all, and 109 of them are fiscal year 2020 (entered near the pandemic
bottom) — one overlapping entry window, not many independent 5-year
periods. Stated plainly rather than glossed over.

**Result**: composite_score's correlation with 5-year annualized
excess return is **-0.117 — negative**, and the highest-scoring
quintile was the single WORST-performing bucket (-14.0% mean
annualized excess, vs. -3% to -4.5% for the other four). The 12-month
test (D-061/D-062) found near-zero; this longer, out-of-sample-by-
construction window (the model's factors were never fit to this
specific outcome at all) finds the relationship actually runs
backward.

**2. "If still no conclusion, characterize what worked"**: per the
user's own fallback instruction, stopped trying to validate the
composite score and instead compared the 30 best vs. 30 worst
company-years by REALIZED 5-year annualized excess return — a
descriptive comparison, not a predictive claim.

**What characterized them**: the worst performers (LCID, PTON, ZM,
DOCU, MTCH, ENPH, ILMN, OKTA, TTD, MRNA...) are overwhelmingly 2020–
2021 pandemic-demand names — remote work, home fitness, EV hype,
vaccine — averaging **P/E ~162x** at entry with net-CASH-rich balance
sheets (`balance_sheet_strength_ratio` avg -0.68, i.e. more cash than
debt, typical of a company freshly flush with IPO/secondary-offering
proceeds). The best performers (NVDA, AVGO, KLAC, STX, MU, PANW,
CRWD, PLTR, RKLB...) averaged **P/E ~68x**, with more conventional,
more-leveraged balance sheets (avg +0.25).

**Formal check**: entry-date raw P/E alone correlates **-0.247** with
5-year annualized excess return (n=102) — negative (cheaper at entry →
better forward return, the classical value-investing direction) and
**clearly stronger than the composite score's own -0.117**. Raw P/E,
a single number the current model doesn't weight into the composite
at all in a way that helped, characterizes the actual winners/losers
better than the 9/10-factor quality-and-growth score this project has
built so far.

**A real, important limitation of this specific finding, stated
plainly**: several of the biggest winners — CRWD, PLTR, PANW, RKLB —
had **no computable P/E at entry** (not yet profitable, so diluted EPS
was ≤ 0). A strategy that simply bought the cheapest P/E would have
**excluded some of the best performers in the whole dataset entirely**.
Any future use of this valuation signal needs an explicit rule for
not-yet-profitable companies (e.g. a separate growth-stage bucket, or
a different valuation ratio such as EV/Sales that doesn't require
positive earnings) — not a reason to ignore the finding, but a reason
not to oversimplify it into "always buy the lowest P/E."

**Honest overall conclusion**: this session did not produce a working,
validated 5%/year-beats-Nasdaq predictor. It DID produce a real,
economically coherent, characterizable pattern — starting valuation,
not current quality/growth-factor scoring, appears to be the more
important variable in this dataset — worth pursuing directly (build a
proper valuation-tiered model, decide how to handle unprofitable
growth companies, and test on more independent time windows once more
price history exists) rather than continuing to tune the current
9/10-factor composite's weights.

Both scripts (`scripts/200`, `scripts/201`) are read-only; no
production table (model research, not a lineage-tracked fact). 2 new
tests for the annualization logic.

## D-064 — Council convened on the entry-P/E finding; cohort-clustering risk diagnosed and fixed with a company-grouped block bootstrap (result, user-directed: "מועצה" then 2 concrete proposals)

**Trigger**: per CLAUDE.md's binding council mechanism, the user typed
exactly `מועצה`, then asked for 2 concrete proposals to reliably
characterize what makes a company beat Nasdaq-100 by ~5%/year, given
the project's D-063 dead end.

**Diagnosis (stage 1-4, 5 independent advisor perspectives + peer
review + chair decision)**: the headline P/E finding (D-063: -0.247,
n=102) looked like 102 independent observations but wasn't — 109 of
133 5-year-eligible company-years are FY2020 entries, i.e. one
overlapping cohort dominates the sample. Nominal n overstates the
real, effective sample size.

**Chair decision, proposal 1 (statistical remedy)**: validate the
P/E finding with a block bootstrap resampled by TICKER (not by row),
so a company appearing multiple times can't be double-counted as
several independent data points. Built `cohort_robustness_v1.py`
(`block_bootstrap_correlation`, 5 tests, all pass) and ran it
(`scripts/202`): **the finding survives** — observed correlation
-0.247, 95% CI [-0.444, -0.032], does not cross zero. The FY2021-only
subset (n=20, excluding the FY2020 cohort entirely) shows the same
direction (-0.361), a useful but small independent sanity check, not
a second validated finding on its own.

**Chair decision, proposal 2 (data remedy)**: build a valuation
metric usable for NOT-YET-PROFITABLE companies (D-063's stated
limitation — CRWD/PLTR/PANW/RKLB had no computable P/E at entry).
Started `value_growth_model_v1.py` (two-bucket: profitable companies
ranked by low P/E, unprofitable companies ranked by high revenue
growth) — built and unit-tested (6 tests) in this session, but not
yet run against real production data or reported on; still open.

## D-065 — Quarterly revenue-growth-acceleration factor: built, tested, and run on real data; result is honest and NOT validated on this small 9-ticker proof (result, user-directed: "quarterly trends can reveal what annual reports have already priced in" + "12 quarters back, avoids COVID")

**Context**: after D-064, the user firmly pushed back on an earlier
dismissal of quarterly-cadence signals, arguing (correctly) that
quarterly data can reveal a trend before it's fully priced into an
annual report. Reframed scope per the user's own instruction: no
longer necessarily a 5-year hold; view the project in quarters; use a
12-quarter (~3 year) lookback, which conveniently also exits the
2020-2021 COVID-distorted window.

**Scope constraint found and disclosed before building anything**:
Quarterly Data V1 (D-042) covers ONLY the original 9 tickers — 10-Q
filings were never locked for the wider 143-company expansion
(confirmed via `sec_filings`: 135 10-Q rows / 9 tickers, vs 782 10-K
rows across the full universe). Proved the factor on these 9 first,
per the project's own small-proof-before-scaling principle, rather
than investing in extending 10-Q coverage on spec.

**Factor built**: `quarterly_trend_v1.py` —
`compute_revenue_growth_acceleration`: change in YEAR-OVER-YEAR
revenue growth rate, quarter to quarter (not sequential
quarter-over-quarter, which would confuse seasonality with trend).
4 tests, including a dedicated seasonality-safety test. Fails closed
(`NO_PRIOR_QUARTER` / `INSUFFICIENT_HISTORY`, never a fabricated 0)
when under 5 quarters of history exist.

**Entry-timing clarification from the user, applied**: real trade
entries can happen at any moment based on price, not only at a
filing's availability date. This proof uses `availability_date` as
the entry point — the earliest moment the factor is legitimately
knowable without look-ahead — as a first-pass proxy; a price-triggered
entry rule (e.g. enter only on a pullback) is a distinct, later
refinement, not built here.

**Run** (`scripts/203`, production DB, 12mo forward return vs QQQ,
company-grouped block bootstrap): 66 candidate quarterly entries
across the 9 tickers, 57 with both factor and forward return
resolved. **Growth acceleration**: correlation -0.183, 95% CI
[-0.554, +0.314] — crosses zero, NOT a validated signal.
**Reference check, raw YoY growth rate (not acceleration)**:
correlation +0.309, 95% CI [-0.087, +0.652] — also crosses zero, but
directionally positive and closer to significance.

**Why inconclusive rather than negative**: only **9 independent
groups** exist for the bootstrap (one per ticker) — the same
cohort-clustering concern D-064 diagnosed for the P/E finding, worse
here since the entire quarterly universe is 9 companies. MU's extreme
quarter-to-quarter outcome swings (-24% to +771% excess return across
quarters, driven by the AI-memory demand spike) dominate the pooled
correlation.

**Honest conclusion**: neither result justifies extending 10-Q
coverage to the wider universe yet — the raw-growth-rate direction is
worth another look once more companies have quarterly data, but this
proof, as run, does not clear the bar. Not a wasted result: it
correctly avoided an expensive extraction (locking + running the
quarterly engine on 143 more companies) that this small proof does not
support.

Files: `src/stock_agent/scoring/quarterly_trend_v1.py`,
`tests/scoring/test_quarterly_trend_v1.py` (4 tests),
`scripts/203_quarterly_trend_predictive_check.py` (read-only),
`data/quarterly_trend_predictive_check_result.json`.

## D-066 — Value/growth two-bucket model (D-064 proposal 2) run on real data: structurally reproduces D-063's P/E finding at the 5-year horizon, adds no new signal at 12 months, and the unprofitable bucket is too thin to judge (result)

**What it tests**: `value_growth_model_v1.py` (built, unit-tested, but
not yet run in D-064) — profitable-at-entry companies ranked by cheap
P/E, unprofitable-at-entry companies ranked by fast revenue growth,
both within the same (fiscal_year, bucket) group. Built specifically
to fix D-063's stated flaw: a raw-P/E-only rule excludes CRWD, PLTR,
PANW, RKLB entirely (no P/E when unprofitable).

**Run** (`scripts/204`, production DB, company-grouped block
bootstrap, both horizons already used this session):

- **12 months** (n=555 — the large, representative sample):
  correlation +0.009, 95% CI [-0.076, +0.094] — crosses zero, **no
  signal**. Matches D-061/D-062's already-established finding that
  nothing tested predicts a 12-month outcome in this dataset.
- **60 months / annualized** (n=106 — the same small,
  FY2020-cohort-heavy sample D-063 used): correlation **+0.222**, 95%
  CI **[0.004, 0.422] — does not cross zero** (barely). Positive is
  the right direction here (higher score = cheaper P/E or faster
  growth → better excess return).

**Why this is a structural replication, not a new independent
finding**: the profitable bucket alone (n=102) drives the pooled
result (its own plain correlation: 0.238) — essentially the same 102
company-years D-063's raw-P/E check already covered, now expressed as
a percentile score instead of a raw ratio. The unprofitable bucket at
60 months has only **n=4** (correlation -0.8, but meaningless at that
size) — too thin to say anything, and **none of the specific
unprofitable winners this model was built to rescue (CRWD, PLTR,
PANW, RKLB) have accumulated 5 years of forward price history yet**,
so the model's actual reason for existing is untested by this run.

**Honest conclusion**: this does not add new evidence beyond D-063 —
it confirms the P/E relationship survives being restructured into a
ranking model, at the same horizon, on largely the same companies. The
unprofitable-company fix is still unproven; it will only become
testable as CRWD/PLTR/PANW/RKLB-era entries age into a 5-year forward
window over the next several years, or if the model is checked at a
shorter horizon where more unprofitable-bucket rows already have
forward returns.

Files: `scripts/204_value_growth_model_predictive_check.py`
(read-only), `data/value_growth_model_predictive_check_result.json`.

## D-067 — Quarterly 5-factor composite score: positive, bootstrap-validated on the full sample, but MU-dependent (result, user-directed: "build a quarterly composite on the 5 params computable quarterly")

**What it tests**: a quarterly-cadence version of composite_v1's 9-factor
model, using only the 5 factors that CAN be computed from Quarterly Data
V1 (D-042) — confirmed by direct query before building anything that
`quarterly_metric_results` has exactly 6 metric families (revenue,
operating_income, operating_cash_flow, capex, pretax_income,
income_tax_expense) and no balance-sheet or share-count data at all.
`roic_level`, `roic_trend`, `balance_sheet_strength_ratio` (need
invested_capital / adjusted_net_debt / stockholders_equity) and raw
P/E (needs a share count) are therefore structurally out of scope, not
omitted by choice. `capex_discipline_deviation` was technically
computable but excluded per the user's own named 5-factor scope.

**Factors** (`quarterly_composite_v1.py`): `revenue_growth`,
`operating_margin`, `fcf_margin`, `fcf_growth` — same formulas as
inputs_v1.py's annual versions, `free_cash_flow = operating_cash_flow -
capex` verified byte-identical to the annual production metric (AMZN
FY2022: 46,752,000,000 - 63,645,000,000 = -16,893,000,000, exact match)
— evaluated at quarterly cadence (revenue_growth/fcf_growth compare the
current quarter to the SAME quarter 4 quarters earlier, YoY not
sequential, avoiding seasonality); and `distance_from_high`, anchored to
the quarter's own `availability_date` instead of a 10-K's `filing_date`.
Same weights composite_v1 already assigns these 5 factors, reused
unchanged and renormalized over whatever is actually available
(unchanged renormalization mechanism from composite_v1).

**Ranking group — calendar quarter, not fiscal_quarter label (new,
scoped to this module)**: the 9 tickers have different fiscal-year-ends
(NVDA/CRWD end late Jan, MU/PANW end Jul/Aug, MSFT/ORCL end May/Jun,
GOOGL/META/AMZN end Dec) — their "Q1" labels do not refer to the same 3
calendar months, so ranking by fiscal_quarter would silently compare
unrelated periods. Ranking is instead cross-sectional within the
CALENDAR quarter each row's `period_end` falls in — verified before
adoption (`scripts/205`'s own diagnostic output) to produce workable
cross-sections of 5-9 companies for most of the last 12 quarters
(2023Q3-2024Q2: 8-9 companies; degrading to 1-3 for the most recent 2
quarters as fewer tickers have a 10-Q locked that recently).

**Run** (`scripts/205`, production DB, last 12 quarters, cutoff
2023-08-13 — same convention as D-065 — 12-month forward excess return
vs QQQ, company-grouped block bootstrap): 66 candidate quarterly
entries, 1 unrankable (alone in its calendar quarter), 8 with no
12-month forward return yet (too recent), **57 usable company-quarters**.

- **Full sample**: plain Spearman +0.365; block bootstrap **95% CI
  [0.053, 0.613] — does NOT cross zero**. The most encouraging
  quarterly-cadence result of this session (D-065's growth-acceleration
  factor crossed zero at -0.183; its raw-growth-rate reference check
  crossed zero too, at +0.309).
- **Horizon choice**: 12 months, not 60 — a quarterly entry from the
  last 3 years cannot honestly support a 5-year-back-capped forward
  window that far out (would require price data reaching further than
  this project's own 5-year backtest-window convention,
  `docs/PROJECT_CONTEXT.md`), so 12 months is the only horizon
  consistent with both the 12-quarter lookback and the 5-year cap —
  same reasoning D-065 already applied.

**Robustness check (same discipline D-065 already applied to MU)**:
MU has 3 extreme outliers in this window (+186%, +305%, +771% 12-month
excess return — the AI-memory demand spike, the same one D-065 flagged
as dominating its pooled correlation). Re-running the identical
bootstrap with MU excluded: **n=49, correlation +0.318, 95% CI
[-0.073, 0.619] — CROSSES ZERO.** The full-sample result is MU-dependent,
not a broad signal across the other 8 tickers.

**Honest conclusion**: this is a real, reproducible, positive
association on the data as measured — not fabricated, not cherry-picked
— but it fails the exact same robustness bar D-065 already set for this
9-ticker quarterly universe. One company's idiosyncratic 2024-2025 run
is doing the work. Not yet a validated quarterly signal; a genuine test
needs either more tickers with quarterly coverage (10-Q was never
locked beyond these 9, D-042) or more independent time periods so no
single company's run can dominate a bootstrap with only 9 groups.

Files: `src/stock_agent/scoring/quarterly_composite_v1.py`,
`tests/scoring/test_quarterly_composite_v1.py` (9 tests),
`scripts/205_quarterly_composite_predictive_check.py` (read-only),
`data/quarterly_composite_predictive_check_result.json`.

## D-068 — The entry-P/E → 5-year-return finding validated at wide-universe scale: real, not a 9-ticker artifact (result, user-directed pivot: "you concluded no benefit from only 2 parameters — find real practical utility")

**Trigger**: after D-067's fragile robustness grid (only 1 of 6
lookback×horizon cells cleared significance), the user pushed back that
declaring the project's direction unproductive on that basis was
premature — one narrow model, one small 9-ticker universe. Redirected:
of everything tested this session, only one finding was ever properly
bootstrap-validated as real — entry-date raw P/E predicting 5-year
excess return (D-063: -0.247, n=102; D-064: block-bootstrap CI
[-0.444, -0.032]) — but it was only ever tested on the original
9-ticker universe (102 company-years, heavily FY2020-cohort-clustered
even after D-064's bootstrap fix). Scoring Inputs V1, Valuation V1, and
Historical Prices V1 were each separately extended to a wide
~135-150-company survivorship-free universe in earlier sessions
(D-051-D-061) — that infrastructure already existed and had simply
never been pointed at this specific question.

**Two real bugs found and fixed while building the wide-universe test**
(both in `stock_agent/extraction/quarterly.py`, discovered via a 3-ticker
quarterly-coverage pilot — COST/CSX/PYPL — run in parallel, not this
script itself):
1. `resolve_annual_anchor` queried ALL `financial_metric_results` rows
   for an accession+metric with no dedup, requiring exactly 1 — silently
   fine for the original 9 (each accession loaded exactly once, ever)
   but broken for any wider-universe ticker reloaded by a later engine
   version (D-051-D-054's "v3-vocabulary-cleanup" pass left 2
   `is_active=true` rows per accession+metric for those tickers).
   **Fix**: dedupe to the latest-`loaded_at` active row via the same
   `ROW_NUMBER() OVER (PARTITION BY accession, metric ORDER BY loaded_at
   DESC)` mechanism `production_lookup.latest_metric` already uses
   everywhere else in this project — not a new policy, closing a gap
   where one function never had it.
2. `lookup_annual_fact_decimals` required a non-null `context_id` to
   find its matching warehouse fact; some wider-universe
   `financial_metric_results` rows never recorded one (confirmed: COST
   FY2022 revenue had `context_id=NULL` despite the warehouse holding
   the real fact under its own context). **Fix**: added a fallback,
   used ONLY when `context_id` is missing, that recovers the fact by
   exact-value match against the already-resolved annual value —
   accepted only when it identifies exactly one context in the whole
   filing (never a guess; ambiguous matches stay blocked exactly as
   before).

**Both fixes independently regression-verified before being trusted**:
re-ran the engine against a random sample of company-years from all 9
original tickers (6 then 8 samples, two separate rounds) and
byte-compared every value/status against current production — **0
differences in either round**. Both changes are additive fallback tiers
only, tried after the existing path already fails; nothing about the
original 9's 45 company-years changed.

**Run** (raw P/E vs QQQ excess return, full `scoring_inputs_v1` universe
— 777 company-years / 135 tickers — company-grouped block bootstrap):

- **60 months (annualized), full universe**: n=102, but now **84
  independent tickers** (vs. D-063/D-064's small, FY2020-heavy cohort).
  Correlation **-0.247**, 95% CI **[-0.449, -0.025] — does not cross
  zero**.
- **The 94 company-years that were NEVER part of D-063/D-064** (wide-
  universe tickers only, 79 independent groups) independently reproduce
  almost the identical result on their own: **-0.253**, CI **[-0.454,
  -0.031]**. This is genuine out-of-sample confirmation on a
  near-disjoint sample, not a re-measurement of the same ~100 rows —
  the strongest validation any finding in this project has received.
  (The original-9-only subset at this horizon is now just n=8/5 groups
  — too small on its own, CI crosses zero — consistent with D-064's own
  diagnosis that the small universe was always underpowered here.)
- **12 months, every scale tested (full universe n=475, original-9
  n=37, wide-only n=438)**: correlation ≈ 0 every time, CI always
  crosses zero. The signal is specifically a multi-year effect — matches
  standard value-investing mean-reversion timing, not a project defect.

**Quintile breakdown — the practically important, previously-invisible
nuance**: sorting the 102 five-year-eligible company-years into P/E
quintiles shows the effect is **asymmetric, not "cheap wins"**:

| Quintile | P/E range | n | mean annualized excess | win rate |
|---|---|---|---|---|
| Q1 (cheapest) | 7.7–21.8 | 20 | +0.1% | 40% |
| Q2 | 21.9–28.8 | 21 | -2.2% | 48% |
| Q3 | 29.3–40.2 | 20 | -6.5% | 35% |
| Q4 | 40.7–81.1 | 21 | -1.1% | 38% |
| Q5 (most expensive) | 81.1–1696.9 | 20 | **-15.7%** | **15%** |

The cheapest quintile barely beats QQQ and only 40% of the time — not a
strong standalone buy signal. The most expensive quintile (names like
NFLX, ISRG, BKNG, MTCH at their entry P/E) underperforms by a mean
15.7%/year with an 85% LOSS rate against QQQ. **The rule this data
actually supports is "avoid richly-valued entries (P/E over ~80)," not
"buy the statistically cheapest names"** — a more nuanced, more
practically actionable conclusion than D-063's original framing.

**Honest limitation, unchanged from D-063**: this valuation signal still
has no rule for not-yet-profitable companies (no P/E at all) — D-064's
proposal 2 / D-066's value-growth model addresses that structurally but
remains unproven at the 5-year horizon for exactly the names it exists
to rescue (still too few years of forward history for CRWD/PLTR/PANW/
RKLB-era entries, unchanged from D-066).

**Status of the parallel quarterly-coverage pilot**: COST/CSX/PYPL 10-Q
filings are locked and warehouse-loaded (36/36 accessions, 0 failures)
but the quarterly metric load itself was not re-run after the two fixes
above — deprioritized in favor of this stronger annual result. Cleanly
resumable later (the two blocking bugs are already fixed and
regression-verified).

Files: `scripts/207_quarterly_extension_pilot.py` (the pilot that
surfaced both bugs), `scripts/208_wide_universe_pe_5yr_validation.py`
(read-only, the validation itself), `src/stock_agent/extraction/
quarterly.py` (the 2 fixes), `data/wide_universe_pe_5yr_validation_
result.json`, `data/quarterly_extension_pilot_result.json`.

This decision (the two engine fixes) requires the user's explicit
sign-off to change or extend further, the same as any other entry in
this log — they are narrow, regression-verified additive fallbacks, not
a new accounting or extraction policy.


## D-069 — `docs/PROJECT_MAP.md`: a single always-loaded, self-maintaining project-state file (approved, live user instruction: "I want you to have a project state text file ... always read ... always maintained automatically")

**Problem.** `docs/CURRENT_STATE.md` had grown to ~288KB and
`docs/DECISIONS_LOG.md` to ~152KB. Both are correct and valuable as
narrative history, but neither can be read at the start of a session —
`CURRENT_STATE.md` exceeds the assistant's single-file read limit
outright. The practical effect was that every new session re-discovered
the same structural facts (which modules exist, which tables are
loaded, what is frozen, what has already been disproven), and the
`CLAUDE.md` instruction to "read `docs/CURRENT_STATE.md` first" could
not actually be followed.

**Decision.** A new file, `docs/PROJECT_MAP.md`, is the session entry
point. It is imported into every session automatically via
`@docs/PROJECT_MAP.md` in `CLAUDE.md`. It is deliberately bounded in
size (currently ~24KB; a test fails above 60KB).

**The anti-drift mechanism** — the reason this file will not decay the
way a hand-maintained summary would. The map has two kinds of content:

1. **Managed blocks**, delimited by `<!-- MAP:BEGIN name -->` /
   `<!-- MAP:END name -->`, regenerated *from the repository itself* by
   `src/stock_agent/projectmap.py`: repository layout, every module in
   `src/stock_agent` with its purpose taken from its own docstring,
   live DuckDB table/row counts, the pytest suite, the docs index with
   sizes, the recent `D-NNN` headings, and git state. Nothing here is
   typed by hand, so nothing here can be wrong.
2. **Hand-written prose** — what is proven vs. disproven, the frozen
   releases, the binding rules, the current focus. A generator cannot
   know these. The generator provably never touches them (tested).

A `Stop` hook in `.claude/settings.json` runs the generator after every
turn, so the managed blocks refresh whenever anything changes; a
`SessionStart` hook covers a session that ended abnormally.
`--check` reports drift without writing (exit 1), deliberately ignoring
the volatile `git`/`stamp` blocks so an ordinary commit is never
reported as staleness.

**Verified, not assumed** (`tests/test_projectmap.py`, 24 tests, all
passing, plus explicit end-to-end runs):
- A new module added to `src/stock_agent` is detected by `--check`
  (exit 1, naming the `layout` and `modules` blocks), appears in the
  map with its docstring after regeneration, and disappears again when
  removed.
- A sentinel injected into a hand-written section survives regeneration
  byte-for-byte; prose outside the markers is provably untouched.
- The generator converges — two consecutive runs produce an identical
  file (an earlier version did not: the docs index listed the map's own
  size, making the block depend on its own output; fixed by excluding
  the map from its own index).
- Regex-special content (Windows paths, `\1`, `$&`) survives
  substitution intact.
- A deleted marker fails loudly (exit 2) rather than silently writing a
  damaged file.
- A DuckDB file locked by a writer is reported as `unavailable` and the
  hook still exits 0 — a Stop hook must never fail the session.
- A builder that raises is contained; the other blocks still generate.
- The hook command was verified to run under both `cmd.exe` and Git
  Bash before being committed, so it cannot fail on shell choice.

**`CLAUDE.md` is updated** to make the map the "read first" document
and to make it binding that the *prose* sections are updated in the
same turn as the work that changes them — the managed blocks need no
human attention, but "what is proven" and "current focus" do.

`docs/CURRENT_STATE.md` and `docs/DECISIONS_LOG.md` keep their existing
role and keep receiving full entries. The map indexes them; it does not
replace them.

**Not changed by this decision**: no engine logic, no accounting
policy, no production data. `projectmap.py` only ever opens DuckDB
files `read_only=True`.

Files: `src/stock_agent/projectmap.py`, `docs/PROJECT_MAP.md`,
`tests/test_projectmap.py`, `.claude/settings.json`, `CLAUDE.md`.

## D-070 — D-069 reverted: no separate project-map file; CLAUDE.md is the single place. Golden regression re-scoped to the frozen baseline (user-directed: "remove project map ... put it all in one place", "make the tests pass")

**D-069 is withdrawn.** `docs/PROJECT_MAP.md`, its generator
(`src/stock_agent/projectmap.py`), its tests
(`tests/test_projectmap.py`), and the `.claude/settings.json` Stop /
SessionStart hooks were all removed on the user's explicit instruction.
The mechanism worked and was verified, but the user judged the separate
always-loaded map file unnecessary and asked for one concise document
instead. Nothing was lost from git history — none of those four files
had ever been committed.

**`CLAUDE.md` is now the single place.** Rewritten to be as concise as
possible while keeping every binding rule, and it absorbed the parts of
the map that a generator could not have produced anyway: the frozen
releases, what is proven vs. what has been tested and NOT supported, the
open next step, and the two traps (the `nominal_close` vs `close` EPS
pairing, and stale background processes holding DuckDB files open). The
auto-generated inventories (module lists, live row counts, git state)
were dropped entirely — that content is discoverable on demand and was
the bulk of the file's size.

**Golden regression fixed — the baseline was wrong, not the engine.**
Both tests in `tests/test_golden_regression.py` had been failing because
they compared the frozen 9-ticker baseline against a production database
that now also holds the wide universe (D-051–D-068):

1. `test_annual_golden_regression_900_rows_byte_identical` found 777
   company-years instead of 45. Its existing filter excluded only
   `scripts/188`, but D-056 had reloaded rows under
   `v3-vocabulary-cleanup (scripts/191, D-051-D-054)`, so 10 later fiscal
   years of the original 9 tickers (MSFT 2026-06-30, NVDA 2026-01-25,
   ORCL 2025/2026, META 2025-12-31, and others) still matched. **Fix**:
   exclude `scripts/191` as well — the same principle the filter already
   encoded, applied to the second expansion engine. Yields exactly 45.
2. `test_quarterly_golden_regression_1080_rows_reproduced` found 78
   `quarterly_extraction_runs` instead of 45. Unlike the annual table
   there is no engine_version that separates them (the expansion reused
   `QUARTERLY_ENGINE_V5_*`), so the frozen set is identified by ticker
   via a new documented `FROZEN_BASELINE_TICKERS` constant. Those 9
   tickers have precisely the 45 frozen runs and nothing else.

Neither fix weakens the test: both still recompute every frozen
company-year from the filings and compare byte-for-byte. They now guard
the frozen baseline and ignore rows that never had a baseline to
reproduce. Both pass (7m22s).

**The annual query is ALSO scoped by ticker, not by engine_version
alone** -- added after the first fix, and it is what makes the test
stable going forward. engine_version can only exclude the expansion
engines that exist today; every future loader writes a version this
filter has never heard of. Pinning to the 9 frozen companies means new
tickers can never leak in, whatever loads them.

This was validated live rather than argued: while
`scripts/212_quarterly_universe_extension_batch.py` was running in a
concurrent session, `quarterly_extraction_runs` grew from 78 to 118 rows
within minutes, and the frozen-9 subset stayed at exactly 45 throughout.
Without the ticker scope the quarterly test would have broken again
immediately. The annual filter's before/after row sets were confirmed
byte-identical (45 = 45, symmetric difference empty), so adding it
provably cannot change the outcome of the run that already passed.

**The intermittent `filings_archive.duckdb` "used by another process"
failure was misdiagnosed at first.** It was initially attributed to
leaked `ProcessPoolExecutor` workers from `tests/test_warehouse_batch.py`.
That was wrong: the surviving processes were leftover
`_discover_10q_window` scans started by Bash-tool commands in earlier
sessions, not pool workers. `test_warehouse_batch.py` leaks nothing. With
those stale scans deliberately left running, `tests/test_filings_archive.py`
passes (10/10) — read-only DuckDB connections coexist fine — so the
original failure is not reproducible and no code change was made for it.
The operational lesson is recorded in `CLAUDE.md` under Traps.

Files: `CLAUDE.md` (rewritten), `tests/test_golden_regression.py`.
Removed: `docs/PROJECT_MAP.md`, `src/stock_agent/projectmap.py`,
`tests/test_projectmap.py`, `.claude/settings.json`.

## D-071 — Golden regression: two more bugs found re-verifying D-070's own fix, both in the exception/scope mechanisms D-070 just added

**Context.** This session picked up a large batch of uncommitted work left
by prior sessions (D-067–D-070, `quarterly.py`'s two engine fixes,
`download.py`'s concurrent-fetch addition, `batch.py`'s Windows retry) and
re-ran the full suite before committing any of it, per this project's rule
that tests must pass before a change is done. Two real failures turned up
— both inside the code D-070 itself had just written, not in the older
code D-070 was fixing.

**Bug 1 — the annual test's new exception list could never pass.**
D-070 added `APPROVED_NOT_DERIVED` (a 2-entry set for PANW 2021-07-31's
`average_invested_capital`/`roic`) with an assertion that both entries
must still mismatch. But the file already had `APPROVED_NOT_REPRODUCIBLE`
— the identical 2 entries, pre-dating D-070, defined at the top of the
loop where it `continue`s past those metrics before comparison ever
happens. D-070's new check could therefore never see a mismatch (0 found,
2 expected) — a guaranteed permanent failure, not a real regression.
**Fix**: removed the redundant `APPROVED_NOT_DERIVED` set, its dead
branch, and its assertion; kept the pre-existing `APPROVED_NOT_REPRODUCIBLE`
(D-051) as the single mechanism.

**Bug 2 — the quarterly test's ticker-only scope, exactly the gap D-070's
own text warned about.** D-070 documented that `scripts/212` was running
concurrently and that `quarterly_extraction_runs` grew 78→118 while the
frozen-9 ticker subset "stayed at exactly 45 throughout" — true at the
moment observed, but script 212 (see D-072) went on to completion and
added 7 MORE fiscal years for the same 9 frozen tickers (MSFT 2025-06-30/
2026-06-30, NVDA 2025-01-26/2026-01-25, ORCL 2025-05-31/2026-05-31, META
2025-12-31) — real new data, never part of the frozen 1,080-row baseline.
Ticker-only scope has no way to exclude a NEW fiscal year for an OLD
ticker, so the test found 52 rows, not 45. **Fix**: replaced the ticker
filter with an explicit 45-entry `FROZEN_BASELINE_QUARTERLY_RUNS` tuple of
exact (ticker, fiscal_year_end) pairs — the same precision the annual test
already uses via accession numbers. This is not expected to need
revisiting again: any future loader, whatever engine version it writes,
adds fiscal years or tickers that are by construction absent from this
fixed list.

Both fixes verified: full `pytest` (231 tests, ~6.5 min) green, 0
failures, 0 no stray `python.exe` processes before or after (checked per
`CLAUDE.md`'s Traps note).

Files: `tests/test_golden_regression.py`.

## D-072 — Quarterly Data engine extended to the full 135-company universe: 412 new fiscal-years, 7,180 metric rows (result of `scripts/212_quarterly_universe_extension_batch.py`, already run to completion, documented here for the first time)

**What happened.** A previous session ran `scripts/212` to completion
against production (`read_only=False`) before this session started; its
result file (`data/quarterly_universe_extension_result.json`) was present
but undocumented — D-070 only mentioned script 212 in passing, as the
concurrent process that caused file-lock contention while D-070's own
test fix was being validated. This entry records what script 212 actually
did, now that this session has independently confirmed it against
production.

**Result**: 132 of 135 tickers processed OK; 3 skipped for insufficient
10-K history (too few annual filings to anchor a fiscal year). 412 new
fiscal-years loaded, contributing 7,180 quarterly metric rows: 6,824
`PASS`, 260 `PASS_ROUNDING_TOLERANCE`, 96 `REVIEW_REQUIRED` — a 95.1%
clean-pass rate, consistent with D-070/D-071's confirmation that the 9
frozen-baseline tickers' original 45 fiscal-years are untouched (verified
via the golden regression, D-071). 11 tickers hit transient SEC-side
`503`/timeout errors on individual files during download (QCOM, REGN,
RIVN, SWKS, VRTX, WBA, WDAY, XEL, ZM, ZS, and one more) but still reached
`lock_status: OK` overall — the failures were on files not required to
complete the ticker's coverage, not a systemic problem; re-running
`scripts/212` (idempotent — it checks `already_loaded` per fiscal year
before doing any work) would pick up whatever those specific accessions
still need, if desired.

**Verification performed by this session** (not assumed from the result
file alone): confirmed via direct production query that the 9
frozen-baseline tickers' original 45 (ticker, fiscal_year_end) pairs are
present and unchanged, and that the golden regression (D-071) passes
byte-identical against them after this load. Confirmed
`quarterly_extraction_runs` is now 501 rows and `quarterly_metric_results`
is 9,188 rows in production, consistent with 45 (frozen) + 7 (new fiscal
years for the same 9 tickers) + 449 (the rest of the 135-ticker universe).

**What this means for the project.** Quarterly Data coverage across the
full universe was previously 6 metrics (`revenue`, `operating_income`,
`operating_cash_flow`, `capex`, `pretax_income`, `income_tax_expense`) at
98.3% usability for 130 companies (script 194, pre-dating this load). It
now covers 135 companies at the same 6-metric scope — this load adds
BREADTH (more tickers/years), not new metric families; the 14
balance-sheet/share-count metrics needed for the composite's other
factors (`docs/CLEANUP_DECISIONS_PENDING.md`-tracked open item) remain
annual-only, unchanged.

Files: `scripts/212_quarterly_universe_extension_batch.py` (already run),
`data/quarterly_universe_extension_result.json`,
`src/stock_agent/quarterly_extension.py` (the orchestration module it and
`scripts/213` both call — ported out of `scripts/207`, per this project's
own src-vs-scripts rule).

## D-073 — D-067's quarterly composite signal does not survive robustness checks: fragile to horizon, fragile to which ticker is excluded (result of `scripts/206_quarterly_composite_robustness_check.py`)

D-067 already flagged its own result as fragile (MU-dependent: excluding
MU alone crossed zero) and stopped short of calling it validated.
`scripts/206` extends the same robustness discipline D-065 applied to MU,
across every ticker and two more horizons, and the result is materially
worse than D-067's own caveat suggested.

**Horizon sensitivity**: only the exact cell D-067 reported (12-quarter
lookback, 12-month forward horizon: n=57, corr +0.365, 95% CI
[0.053, 0.605]) clears significance. 6-month and 24-month horizons at the
same lookback both cross zero (6mo: n=65, CI [-0.086, 0.424]; 24mo: n=35,
CI [-0.475, 0.609]), and EVERY horizon tested at "full history" lookback
(not just the last 12 quarters) also crosses zero (6/12/24mo: CI
[-0.082, 0.139] / [-0.154, 0.288] / [-0.090, 0.413]).

**Leave-one-ticker-out**: excluding any ONE of AMZN, CRWD, GOOGL, META,
MU, or NVDA — 6 of the 9 tickers, not just MU as D-067 disclosed — flips
the 95% CI to crossing zero. Only excluding MSFT, ORCL, or PANW leaves it
significant. A result that depends on which 6 of 9 possible single
tickers are included is not a broad cross-sectional signal.

**Per-factor breakdown**: none of the 5 individual factors
(`revenue_growth`, `operating_margin`, `fcf_margin`, `fcf_growth`,
`distance_from_high`) is independently significant — the composite's
apparent signal is not attributable to any one factor either.

**Honest conclusion, superseding D-067's own hedge**: this is not a
fragile-but-real signal, it is noise that happened to land significant in
one specific (lookback, horizon) cell out of six tested, on a 9-ticker
universe where a majority of leave-one-out subsets already erase it.
`quarterly_composite_v1.py` and its 9 tests remain committed (correctly
implemented, useful scaffolding for when quarterly balance-sheet coverage
allows the full 9-factor version, D-072), but the composite's predictive
claim from D-067 is retracted, not just caveated.

Files: `scripts/206_quarterly_composite_robustness_check.py` (read-only),
`data/quarterly_composite_robustness_check_result.json`.

## D-074 — D-068's flagship P/E finding is regime-dependent, not confirmed across market conditions; 9 existing factors show zero 5-year signal on the wide universe; 2 new factor candidates found (results of `scripts/209`, `210`, `211`, all read-only)

**`scripts/209` — the 9 existing Scoring Inputs V1 factors, wide universe,
60-month horizon**: zero factors show a significant signal. Every 95% CI
crosses zero, several from genuinely small n (`roic_trend` n=3,
`revenue_growth` n=18) rather than a real null result — a coverage gap,
not evidence the factors don't matter, but no basis for adding any of
them to a scoring model at this horizon either.

**`scripts/210` — 14 new candidate factors, wide universe, 60-month
horizon**: 12 of 14 cross zero. Two do not: `dividend_yield` (n=137, corr
+0.226, CI [0.045, 0.396]) and `size_log_revenue` (n=104, corr +0.282, CI
[0.063, 0.477]) — both unvalidated single-pass findings, not yet
robustness-checked the way D-063→D-064 checked the P/E finding.

**`scripts/211` — the critical result: multi-regime check on D-068's raw
P/E finding, and it does not hold up.** D-068 called the wide-universe
P/E validation "the strongest validation any finding in this project has
received," reproducing -0.247 (CI [-0.449,-0.025]) on 84 independent
tickers. `scripts/211` checked whether that holds across market regimes,
and found a structural confound D-068 did not test for: **the entire
60-month-eligible dataset is 100% pre-2022 entries** (year_spread: 2020=43,
2021=94, nothing later — a 5-year-forward window needs 5 years of
subsequent price history, so only entries old enough to have that history
by now are eligible, and none of them are from the 2022+ rate-hiking
regime). D-068's -0.247 was never tested against a different macro
period because, at 60 months, there is no other period to test yet.

At shorter horizons where 2022+ entries ARE eligible, **the signal
disappears**: 36-month pooled corr +0.033 (CI [-0.125, 0.199], crosses
zero); 24-month pooled corr +0.067 (CI [-0.057, 0.193], crosses zero).
Most tellingly, restricting to the SAME pre-2022 companies but a shorter
36-month forward window (instead of 60) still crosses zero (corr -0.068,
CI [-0.263, 0.133]) — so it is not simply "P/E predicts returns, just
only over long horizons": the effect is specific to that one 2020-2021
entry cohort's actual 5-year outcome (2020-2021 entries into the 2021-2022
sell-off then recovery), which script 211 cannot yet distinguish from "a
real long-horizon P/E effect that just hasn't had a second full market
cycle to re-test in yet." Both `size_log_revenue` and `dividend_yield`
show the same pattern — significant in the pre-2022 subset at 24/36mo,
not in the 2022-onward subset.

**This does not mean D-068 is wrong.** It means D-068's own claimed
"strongest validation" status overstated what was tested: 84 independent
TICKERS is real breadth, but it is still one macro PERIOD, and the
backtest gate this project committed to (`CLAUDE.md`, "regimes") was not
actually satisfied — it could not be, since no 2022+ cohort has reached
its 5-year mark yet. The honest state is: the P/E→5yr-return effect is
proven for entries made 2020-2021, unproven for entries made since, and
will not be testable at 5 years for a second regime until roughly 2027-
2028 (2022-2023 entries reaching their 5-year mark).

**`CLAUDE.md`'s "Proven" section is updated in this same change** to
state this caveat plainly, per this project's own binding rule to warn
about regime risk rather than let a single-period result read as settled.

Files: `scripts/209_wide_universe_5yr_factor_sweep.py`,
`scripts/210_wide_universe_5yr_new_factor_search.py`,
`scripts/211_multi_regime_factor_check.py` (all read-only),
`data/wide_universe_5yr_factor_sweep_result.json`,
`data/wide_universe_5yr_new_factor_search_result.json`,
`data/multi_regime_factor_check_result.json`.
