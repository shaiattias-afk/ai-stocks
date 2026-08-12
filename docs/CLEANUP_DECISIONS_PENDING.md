# Cleanup — decisions awaiting your sign-off

> **2026-08-11 update: D-P1, D-P2, and D-P3 are all resolved.** Formal
> decisions recorded as `docs/DECISIONS_LOG.md` D-051 (D-P3), D-052
> (D-P1), D-053 (two further defects found while verifying D-P1 — the
> `comprehensive` role-exclude bug and a `current_debt` exception-
> isolation gap), D-054 (D-P2, implemented as a component aggregator,
> not the literal label match originally asked about), D-055 (the
> resulting full-universe re-measurement: 74.36% → 79.86%, zero
> unintended regressions), and D-056 (the improved results LOADED into
> production — `financial_metric_results` 15,540 → 30,180, the 900
> frozen rows confirmed untouched; also found and worked around a
> second uncaught-exception site, same shape as D-053's, still present
> in the real engine and flagged for a future fix). The section-by-
> section history below is left as-is — it is the evidence trail these
> decisions were made from, not superseded text.

Collected during the extraction cleanup so the work could run without
interruption. **Nothing here has been treated as approved.** Each entry
states what was found, what was done (if anything), and what needs your
call.

Two categories:

* **IMPLEMENTED, NEEDS RATIFYING** — a fix was applied because it was
  needed to make progress and is, in my judgement, unambiguously correct.
  You should still confirm it, because it is an accounting policy and
  this project's rules say policy is yours, not mine.
* **PARKED, NOT IMPLEMENTED** — genuinely ambiguous. Left as
  `REVIEW_REQUIRED` rather than guessed.

---

## D-P1 — Combined filings: which entity's numbers are "the company's"?

**Status: RESOLVED — see DECISIONS_LOG.md D-052.** The "REVERTED"
finding below was itself a misdiagnosis (D-P3 already retracts it, see
that section's own update) — re-implemented and verified safe. Fixes
the `identify_canonical_row`-based metrics for the Constellation-style
combined-filing convention; does NOT fix Exelon's "every role qualified"
convention or the separate multi-instrument current_debt gap (D-052 has
the full evidence for both).

> **Update after testing.** The fix below was implemented and then
> **backed out**: it regressed the frozen baseline. PANW 2021-07-31's
> `average_invested_capital` went from `PASS` (698,750,000) to
> `REVIEW_REQUIRED`, because narrowing to a single statement role dropped
> a row that a metric legitimately needed from a different role.
>
> The frozen 900 rows must not change, so the change is out. The helper
> is retained unused in `extraction/core.py` with this evidence.
>
> The diagnosis stands and is valuable — the *fix* was too blunt. Any
> retry must narrow per-metric rather than globally, and must be proven
> against the golden regression before going near production.

### What was found

Utility holding companies file ONE 10-K covering the parent **and every
subsidiary registrant**. Exelon's filing contains **9 income statements
and 16 balance sheets** — separate sets for ComEd, PECO, BGE, Pepco, DPL
and ACE alongside Exelon itself. Constellation Energy's contains two of
each.

The engine found several equally valid candidate rows and refused to
choose, which is why **Constellation scored 0 of 100 metrics** and
**Exelon 8%** — not a wording problem, a genuine structural ambiguity.

Entity identity cannot resolve it: every context in Exelon's filing
carries the parent's own CIK (0001109357), including the subsidiaries'.
The only distinguishing signal is the statement role title.

### The decision

For a combined filing, the figures that represent "the company" are the
**top-level registrant's consolidated statements** — not any subsidiary's.

That is the right answer for equity analysis: you would be buying shares
in Exelon, not in Baltimore Gas & Electric. A subsidiary's revenue is not
Exelon's revenue.

### How it is implemented

Two filer conventions, both handled from the role title alone:

| Convention | Example | Rule |
|---|---|---|
| Consolidated role unqualified, subsidiaries suffixed | CEG: `Consolidated Statements of Operations` vs `..., Parent` | prefer the unqualified role |
| Every role suffixed, including the parent's | EXC: `... Cash Flows - Exelon` vs `... - ComEd` | prefer the role naming the registrant |

If neither rule leaves exactly one role, it still fails closed.

### What could be wrong with it

A holding company's *consolidated* figures include subsidiaries it does
not wholly own, so some of that revenue belongs to minority holders. The
existing `net_income` logic already prefers an "attributable to common
shareholders" line where one exists, so the most important metric is
already handled — but revenue and assets are not adjusted, and for a
utility group with significant minority interests that overstates what
shareholders own.

**Your call:** accept consolidated figures as-is (standard practice), or
require a separate treatment for groups with material minority interests.

---

## D-P2 — Utility capital spending labelled as acquisitions

**Status: RESOLVED — see DECISIONS_LOG.md D-054.** Implemented as a
GAAP-concept-based component aggregator, not the literal label match
this section originally asked about — evidence showed AEP's dominant
capex line ("Construction Expenditures") isn't reachable by any label
wording, and a label-only fix for "Generation Facilities" alone would
have produced a confidently-wrong, understated `PASS` rather than a
correct number.

American Electric Power labels its capital expenditure
`Acquisitions of Assets` and `Acquisitions of Generation Facilities` —
with no mention of "property".

Matching those would fold genuine **acquisitions of businesses** into
capital expenditure, which corrupts free cash flow and then the score.
Left as `REVIEW_REQUIRED`.

**Your call:** is "Acquisitions of Generation Facilities" capital
expenditure for your purposes? For a utility, buying a power station
arguably *is* capex in substance. But the same wording at another company
could mean an acquisition. A rule that reads "acquisitions of
<physical asset type>" as capex is defensible; I did not want to invent
it unilaterally.

---

## D-P3 — Loading new companies changed the result of an OLD computation

**Status: RESOLVED — see DECISIONS_LOG.md D-051.** Option 1 (below) was
taken: the golden regression now explicitly excludes PANW 2021-07-31's
`average_invested_capital`/`roic` as approved-not-reproducible values,
with the exclusion named and documented in the test itself rather than
silently comparing them against themselves.

### What happened

The golden regression started failing on PANW 2021-07-31's
`average_invested_capital`: recomputes as `REVIEW_REQUIRED`, but the
frozen row says `PASS` with 698,750,000.

I first assumed my combined-filing fix caused it and reverted that fix.
**The failure persisted.** The revert was not the answer, and the
diagnosis I gave at the time was wrong.

### The actual cause

`average_invested_capital` for one year needs the PRIOR year's
`invested_capital`, and `compute_full_company_year` **reads that from the
production database** rather than recomputing it.

PANW 2020-07-31 was never part of the frozen 900 rows. The universe
expansion loaded it for the first time — and it landed as
`REVIEW_REQUIRED`, because PANW 2020 is one of the company-years the
engine cannot fully resolve.

So the lookup that previously found nothing (and fell back to
recomputing) now finds a failed value and propagates the failure.

### Why this matters more than the coverage number

**The frozen 900 rows are intact.** Verified: unchanged, no duplicates,
every original engine version still present. Nothing was corrupted.

But the *computation* that reproduces them is no longer reproducible,
because it consults a database that has since grown. Adding unrelated
companies silently changed the output of an existing calculation.

That is a real architectural weakness, not a test artefact:

* the golden regression is not actually isolated — it depends on
  production contents that any future load can change
* the same coupling exists in the live engine, so a future load could
  change previously-verified figures the same way

### Your decision

1. **Pin the prior-year lookup to the frozen baseline** for the golden
   regression, so the test measures the engine rather than the database.
   Narrow, fast, but leaves the live coupling in place.
2. **Make `compute_full_company_year` recompute the prior year** instead
   of reading production. Removes the coupling entirely, at the cost of
   more work per company-year and a change to a core path.
3. **Accept it** and re-baseline the golden regression against current
   output. Cheapest, and the one I would argue against: it discards the
   guarantee that today's engine still reproduces the verified figures.

I did not choose, because option 3 would quietly weaken the protection
that caught this in the first place, and options 1 and 2 are structural
changes to how the engine sources prior-year data.

### Option B implemented — and it revealed the deeper problem

Option B was applied: the prior year is now ALWAYS recomputed from the
filings, never read from production. Production is still consulted for
which ACCESSION to read, never for a value.

**The golden regression still fails on PANW, identically.** That is the
useful result, because it rules out the explanation and exposes the real
one.

Tracing why the test used to pass:

* PANW 2020-07-31 was not in `sec_filings` before the universe expansion
* so `prior_report_date_for(...)` found **no prior year**
* that branch does not compute anything — it **reads the stored
  `average_invested_capital` straight out of production** and returns it
* the test then compared that stored value against itself

So for this metric the golden regression was **partly circular**: it was
reading the answer from the database rather than reproducing it. It only
looked like a passing independent check.

Now that PANW 2020 exists as a real filing, the honest computation runs
for the first time — and reports `REVIEW_REQUIRED`, because PANW 2020's
`invested_capital` genuinely cannot be resolved from that filing.

### What this means about the frozen value

`698,750,000` does not rest on a calculation today's engine can redo. It
rests on D-027 item 7, which permitted using the prior filing's
*previously approved* result. That approval was valid when made, but it
is not reproducible from the filings alone.

**This is not corruption.** The number may well be correct. But it is an
approved figure, not a derived one, and the distinction was invisible
until the circularity broke.

### Your decision (this supersedes the earlier three options)

1. **Accept that some frozen values are approvals, not derivations.**
   Mark them explicitly in the data so nobody later mistakes them for
   computed figures, and exclude them from the golden regression — which
   should only test what the engine can actually reproduce.
2. **Re-derive PANW 2020's invested capital properly**, which means
   resolving why its components fail — real extraction work, and it may
   end up genuinely unresolvable.
3. **Drop the value** and let PANW 2021's ROIC be `REVIEW_REQUIRED`,
   losing a figure that is probably right.

My recommendation is **1**: the honest description of what that number
is. It also fixes a real weakness — a regression test that reads its own
expected answer is not testing anything.

### Status of the coverage cleanup

**No longer halted, and no longer just measured — see DECISIONS_LOG.md
D-051 through D-056.** The coupling was settled (D-051), D-P1 and D-P2
were implemented and verified against the now-honest baseline
(D-052/D-054), two further defects were found and fixed along the way
(D-053), re-measuring the full universe moved coverage from 74.36% to
**79.86%** with zero unintended regressions (D-055), and that
improvement is now loaded into production, live (D-056) — not just
proven possible. The vocabulary loop continues from there, deliberately
paused for now at the user's request — D-055/D-056 list what's still
open (Exelon's "every role qualified" convention, the multi-instrument
current_debt gap, a second uncaught-exception site confirmed present in
the real engine, and the remaining ~20% of REVIEW_REQUIRED rows, not
yet diagnosed).

---

## 2026-08-12 cleanup session — new open items (D-071 through D-074)

This session picked up a large batch of work two concurrent prior sessions
had left uncommitted, verified it independently (found and fixed 2 more
bugs, D-071), wrote up the previously-undocumented parts as D-072/D-073/
D-074, and updated `CLAUDE.md`'s "Proven" section to state an honest new
caveat. Full detail: `docs/DECISIONS_LOG.md` D-071–D-074,
`docs/CURRENT_STATE.md`. What follows is the part that needs your call, not
mine.

## D-P4 — D-068's flagship P/E finding is regime-untested: what to do about it

**What was found (D-074).** The wide-universe validation of the entry-P/E
→ 5-year-return finding (D-068, previously called "the strongest
validation any finding in this project has received") turns out to rest
entirely on 2020-2021 entries — the only cohort old enough to have a full
5-year forward return yet. At shorter horizons where later entries ARE
eligible, the signal is absent, including for the same companies measured
over a shorter window. This is not proof the effect is false. It is proof
the effect has only ever been tested in one macro period, and the backtest
gate this project committed to (warn about regimes) was not actually
satisfiable until now, because no second cohort had reached 5 years yet.

**Your call — how to treat this finding going forward:**
1. **Treat it as the working thesis anyway**, with the caveat stated (as
   `CLAUDE.md` now does), and keep using "avoid P/E > ~80" as the
   practical rule until a second regime becomes testable naturally
   (~2027-2028, when 2022-2023 entries reach 5 years).
2. **Look for an earlier, independent regime test** — e.g. pre-2020
   history for the same tickers (if point-in-time price/filing data
   reaches back far enough) or a completely different universe/period, to
   get a second macro-regime read sooner than 2027-2028, accepting
   whatever extra work that costs.
3. **Deprioritize the P/E rule** as a practical output until it can
   actually be regime-tested, and redirect effort toward the two new
   unvalidated candidates (D-P5) or another line of work.

I lean toward (1) — it is still the only real signal this project has ever
found, the caveat is now honestly stated, and (2)'s "different universe"
approach would itself need the same regime scrutiny before being trusted
more than the original. But this changes how much weight the flagship
result should carry in any decision the user makes with it, so it is the
user's call, not mine.

## D-P5 — Two new unvalidated candidate factors: pursue now or park?

**What was found (D-074, `scripts/210`).** `dividend_yield` (n=137, corr
+0.226, CI [0.045, 0.396]) and `size_log_revenue` (n=104, corr +0.282, CI
[0.063, 0.477]) both clear a first-pass significance bar at the 60-month
horizon, wide universe — but neither has been through the robustness
gauntlet the P/E finding went through (D-063→D-064→D-074's own regime
check just showed why that gauntlet matters). `scripts/211` already found
both are ALSO regime-sensitive in the same way as P/E (significant
pre-2022, not in the shorter-horizon 2022+-eligible subset).

**Your call**: invest the next round of validation effort here (robustness
+ regime checks, same discipline as D-074), or treat this as a lower
priority than D-P4's open question about the existing flagship finding.

## D-P6 — Ratify the already-applied production changes and engine fixes

Two things happened in this batch of work without a specific up-front
approval step, both already verified safe by this session but both are
exactly the kind of accounting/production change `CLAUDE.md` says is the
user's call, not mine, to finalize:

1. **`extraction/quarterly.py`'s two fixes** (D-068): deduping active
   `financial_metric_results` rows to the latest `loaded_at` in
   `resolve_annual_anchor`, and a same-filing exact-value fallback for
   missing `context_id` in `lookup_annual_fact_decimals`. Both are
   additive fallback paths, both regression-verified to cause zero change
   for the original 9 tickers, both necessary for the wider-universe
   quarterly engine to work at all. I judge these unambiguously correct
   (same bar as the old D-P1/D-P2 "implemented, needs ratifying"
   category) — they fix real defects, not policy choices.
2. **`scripts/212`'s full production load** (D-072): 135-company quarterly
   extraction, already run and already verified not to have touched the
   frozen baseline. Not a policy change, but a substantial one-time
   production write that happened without this session's real-time
   supervision — flagging it here for visibility, not because I think it
   needs reverting.

My recommendation: ratify both as-is (same reasoning D-P1/D-P2 used) —
flagging here rather than treating as silently approved, per this
project's rule that policy sign-off is yours.
