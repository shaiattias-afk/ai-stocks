# AI Stock Agent — Claude Project Instructions

## Role
Practical working partner for a personal stock-analysis and investment-decision
system. Respond in clear, direct Hebrew; English technical terms where standard.

## Goal
Long-term investment decisions, not trading. The thesis under test: *a strong
company that is still growing, has corrected in price, but has not broken
fundamentally.* Primary benchmark: **Nasdaq-100 (QQQ)**.

---

## Binding rules

**Data**
- The official 10-K/10-Q is the source of truth. Lock every filing by `form`,
  exact `reportDate`, and `accessionNumber`. Record `filingDate` for
  point-in-time backtesting.
- Use the filing's Inline XBRL document set, extension taxonomy, and linkbases.
  Map statement-first: taxonomy, labels, presentation and calculation
  relationships, contexts, units, dimensions.
- Never return to HTML table scraping or a manual XBRL tag list as the primary
  method. SEC Company Facts is for validation/QA only.
- Ambiguous evidence ⇒ fail closed with `REVIEW_REQUIRED`. Never guess.
- Every extracted value retains: ticker, CIK, accession, form, report date,
  filing date, source document, source concept, statement role, label, context,
  unit, dimensions, value, validation status.
- Status labels: `PASS`, `REVIEW_REQUIRED`, `FAIL`, `TIMEOUT`.

**Code**
- Engine/library code lives in `src/stock_agent` and is **edited in place**. Git
  history is the version record — never create a numbered duplicate file (D-049
  retired that convention; it had produced 195 near-identical scripts).
- Tests live in `tests/` (pytest) and must pass before a change is done.
- `scripts/` is for thin, disposable one-off entry points that call into
  `src/stock_agent`. They may be deleted once their result is captured.
- Do not save project scripts under `.venv\Scripts`.
- Show the exact command before running it. Use timeouts for anything that may
  hang. Update `docs/CURRENT_STATE.md` after every verified milestone.

**Method**
- **Analysis horizon: quarterly cadence, 12 quarters (~3 years) lookback —
  not annual, no fixed 5-year requirement.** User-directed after D-064,
  restated 2026-08-12 after the project drifted back to an annual/5-year
  framing twice despite this rule already being in force (D-068's
  wide-universe P/E work, and this session's D-074 follow-up on it, both
  went back to the annual 5-year lens without a new explicit instruction
  to do so). Any proposal to analyze at annual cadence or a 5-year (or
  other non-quarterly) horizon needs the user's explicit go-ahead each
  time — it is not the default, even if an annual finding already exists
  and looks promising.
- One topic at a time. Practical steps over theory. After comparing
  alternatives, recommend one leading path.
- Never invent data, prices, API capabilities, costs, filing dates, accounting
  meanings, or technical results. Verify current or unstable facts against
  official sources. When material information is missing, say so.
- Separate facts, assumptions, estimates, and recommendations.
- Run a small proof before building a full system. Verify each source on one
  company. Do not recommend a paid service before testing its fit.
- Every model proposal must be measurable and reliably backtestable. Explicitly
  warn about look-ahead bias, survivorship bias, and overfitting.
- Backtest gate: point-in-time availability, predictive value, factor
  correlation, weight sensitivity, out-of-sample performance, regimes,
  benchmarks, drawdown, risk.
- Continue to the next obvious step after a verified result. Do not wait for
  "קדימה" unless information is missing.

**Security**
- Never put API keys, passwords, tokens, or secrets in source files.
- No API billing or paid services without explicit approval.
- No broad auto-approval or bypass permissions without explicit approval.

**Council** — when the user's entire message is exactly `מועצה`, reply only
`מה השאלה?`. After the question arrives, apply `.claude/skills/council/SKILL.md`.

---

## Project state

**Frozen releases** — changing any of these requires a new version and full
re-validation:

| Release | Decision | Content |
|---|---|---|
| Annual Data V1 | D-042 era | 45 company-years, 9 tickers, 0 unresolved `REVIEW_REQUIRED` |
| Quarterly Data V1 | D-042 | Engine V5 is authoritative |
| Derived Metrics V1 | D-043 | 405 rows, exactly 2 metrics: `operating_margin`, `revenue_yoy_growth` |
| Historical Prices V1 | D-044/D-045 | Yahoo daily prices |
| Valuation V1 | D-046 | Reported diluted EPS as the per-share input |

Also binding: production tables are versioned append-only, enforced in code
(D-047); `availability_date = filing_date` governs point-in-time use (D-046).

**Reference finding, NOT the active line of work (annual cadence — out of
current scope per the Method rule above)** — entry-date raw P/E predicts
5-year excess return vs. QQQ: correlation **-0.247**, company-grouped
block-bootstrap 95% CI **[-0.449, -0.025]**, 84 independent tickers. The
effect is **asymmetric**: the cheapest quintile barely beats QQQ (+0.1%/yr),
while P/E > ~80 loses **-15.7%/yr with an 85% loss rate**. (D-063 → D-064 →
D-068.) **Serious open caveat (D-074)**: every 5-year-eligible company-year
in this result is a 2020-2021 entry — one macro period, not tested against
a regime change, and not retestable at 5 years until ~2027-2028. Kept here
as background context only — do not resume building on this annual/5-year
finding without the user explicitly asking for annual-cadence work again.

**Proven at quarterly cadence, but asymmetric not linear (D-079 →
D-080) — and NOT yet shown to be sector-neutral (D-084 below)** — raw
YoY quarterly revenue growth rate (current quarter vs.
the same quarter a year earlier) correlates with 6-12 month forward
excess return vs. QQQ: correlation **+0.235** at the 12-quarter-
lookback/12-month-horizon baseline, 95% CI **[+0.059, +0.383]**, 86
independent tickers, **0 of 86 leave-one-out exclusions flip it** — the
most robust correlation this project has measured by that standard.
**But D-080's quintile breakdown shows this is NOT "more growth is
better."** Quintiles 1-4 (bottom 80% by growth, i.e. anywhere from -91%
to +20% YoY) all show a NEGATIVE median excess return. Only quintile 5
(growth above ~20%/yr) is positive. The tentative practical read is a
**threshold rule — "require growth above ~20%/yr," not a continuous
ranking** — same asymmetric shape as the P/E finding (D-068), just
pointing the opposite direction (require a high bar, vs. avoid a high
bar). Quintile 5's own average (+84%/yr) is itself skewed by a handful
of 2023-2025 AI/hardware-cycle names (`RKLB`, `MU`, `STX`, `APP`); its
median (+26%/yr) is the more honest number. **Serious open caveat**:
the regime check that would test this against a different macro period
(the way D-074 tested P/E) **could not run at all** — every one of the
464 entries is from 2023-2025; the 12-quarter lookback structurally
cannot reach earlier data yet. Not yet a usable rule — see quintile
table in `docs/DECISIONS_LOG.md` D-080 (also published as an
interactive artifact, company- and quintile-level, during that
session). This is a single raw factor, not a composite — the quarterly
composite itself (D-067) remains dead, see below.

**Placebo/permutation test result (D-083)**: the correlation itself is
NOT explained by pure statistical noise — 0 of 200 random permutations
of the growth values (real tickers, real returns, growth shuffled)
produced a correlation as strong as the real +0.234; the strongest
noise-driven correlation across 200 tries was 0.129. This clears one
specific concern (is the discovery pipeline just finding shapes in
noise) but does NOT touch the still-open regime caveat above, nor two
other flagged risks: the growth>20% threshold was chosen AFTER seeing
the data (an uncounted extra degree of freedom, not yet permutation-
tested itself), and the signal may partly reflect AI/semiconductor
sector concentration (`RKLB`/`MU`/`STX`/`APP`-heavy) rather than a
sector-neutral fundamental edge — tested next, see D-084 below.

**Sector-concentration test result (D-084) — leans negative.** The
AI/semiconductor concern just above WAS tested: excluding SEC-classified
(official SIC 3674) semiconductor names (14 of 86 tickers: `ADI`, `AMAT`,
`AMD`, `AVGO`, `ENPH`, `INTC`, `MCHP`, `MPWR`, `MRVL`, `MU`, `NVDA`,
`NXPI`, `ON`, `SWKS`) drops the correlation from +0.234 to +0.152 and
flips the 95% CI from excluding zero to **crossing zero**. Excluding
just the 4 names D-080 itself flagged (`RKLB`/`MU`/`STX`/`APP`) does the
same (+0.142, CI crosses zero). Excluding both together nearly erases
the correlation: **+0.044**, CI **[-0.140, +0.214]**. By this project's
own established standard (the CI-crosses-zero test that killed the
quarterly composite, D-073/D-078), **the signal is not robust to sector
concentration** — a meaningful share of what looked like a broad
fundamental effect is better explained by "owning the 2023-2025 AI/semi
rally" than by growth rate as a sector-neutral factor. Not a full kill
(point estimates stay positive in every cut, unlike the composite's sign
flip) and does not resolve the still-separate regime caveat or the
post-hoc threshold-selection concern — both remain open. **D-085**: the
14 companies currently passing the growth+pullback screen recovered to a
new high in their own last-12-quarters history 96% of the time at 12
months, 100% at 24 months — but most of the 14 are the same concentrated
names D-084 just flagged, so this is consistent with, not independent
evidence against, the concentration finding. **D-086**: across the FULL
universe (not just the 14, full history not just 12 quarters), only 1 of
72 accelerating-growth+pullback episodes (`META`, entered 2021-10-12)
failed to reclaim its high within 24 months — and it is also one of only
3 pre-2023 episodes that exist at all (the other 2, both `NVDA`,
recovered), so it is the thinnest available hint that the rule may not
be regime-independent, not a resolution of that caveat. **D-087
escalates this**: characterizing what separates 12-month recoveries from
failures across the full 136-episode Group A pool shows entry YEAR, not
growth rate or even acceleration, is the sharpest divider — **90%
recovery for 2023+ entries (94/104) vs. only 30% for 2021-2022 entries
(3/10)**. Growth and pullback depth barely differ between recovered and
failed episodes at the median. Thin sample (n=10 pre-2023) but the
strongest evidence yet that the rule's headline recovery rates describe
a strong-market period, not a regime-independent property.

**Proven, entry-timing signal (D-081)** — combining D-080's growth>20%
threshold with a price pullback (stock down >=15% from its own rolling
52-week high, entry evaluated at ANY trading day, not just filing
dates) predicts recovery to that high, clearly beating a pullback-only
control on the full ~92-company universe: 68.8%/85.1%/95.5% recovery
within 6/12/24 months (n=136, 32 tickers) vs. 50.6%/67.1%/80.6% for
pullback alone with no growth filter (n=707, 91 tickers) — a
consistent 15-18 point gap at every horizon. Operating margin does NOT
add further discrimination within the growth>20% group (D-081) and
does NOT explain why some growth<=20% companies still won anyway
(D-082 — if anything the opposite: winners' average margin is
NEGATIVE). Whether growth is accelerating or decelerating AT the same
>20% level shows a real edge for accelerating at 12-24 months but not
at 6 months (D-081) — directionally supportive of the user's own
hypothesis, not a clean confirmation. **Same unresolved regime caveat
as D-079/D-080, now with concrete (if thin) evidence behind it — see
D-087 above: 30% vs 90% recovery pre- vs post-2023.** No formal
significance test run yet on the Group A vs. Group B gap.

**Tested and not supported at quarterly cadence** — do not re-propose
without new evidence: the quarterly composite (D-067) is **conclusively
not supported, in both its 5-factor and full 8-factor forms** (D-078).
Both of D-067/D-073's own stated bottlenecks — a 9-ticker universe, and
missing balance-sheet factors — were fixed (full ~99-ticker universe,
D-072; balance-sheet/ROIC factors added, D-076/D-077) and re-tested
side by side: the 5-factor version is barely-significant and 58%
leave-one-out-fragile at its original best cell; the 8-factor version
is NOT significant there at all (100% leave-one-out-fragile). Adding
the balance-sheet factors made the composite worse, not better. The
quarterly growth-ACCELERATION factor (D-065) is also not supported, now
confirmed at full scale too (D-079): CI crosses zero, 85 of 86
leave-one-out exclusions flip it — worse than the small-sample result,
not just unconfirmed. (Growth acceleration and the raw growth rate above
are different factors — acceleration measures whether growth is
speeding up/slowing down; the validated finding is the plain growth
rate itself.) **Also not supported, annual cadence** (background only,
see above): Scoring Model V1 composite (D-061/D-062), the value/growth
two-bucket model (D-066), none of Scoring Inputs V1's 9 factors at 5
years (D-074). See also `docs/FAILED_APPROACHES.md`.

**Open next step (post-council, D-083 → D-087)** — the placebo test
(D-083) cleared one concern (the correlation is not pure noise); the
sector-concentration test (D-084) confirmed a different one (a
meaningful share of it IS concentration, not a sector-neutral edge); the
year-split characterization (D-087) turned the long-standing regime
caveat from a data gap into a concrete, if thin (n=10), warning sign
(30% vs 90% recovery pre- vs post-2023). This makes (a) below the
clearest priority now, not just first among equals. Remaining, in rough
priority order: (a) a genuine walk-forward test — freeze the exact rule
(growth>20% + pullback>=15%) and score it untouched against new quarters
as they arrive, the only test not contaminated by having already mined
all current data; (b) a proper regime split remains data-limited for a
real statistical test (not just the n=10 characterization above) for
either the growth-rate or P/E finding; (c) the post-hoc quintile-
threshold selection (D-080's ~20% cutoff was chosen after seeing the
data — an uncounted extra degree of freedom, still not permutation-
tested); (d) D-081's entry-timing signal has no formal significance test
on its Group A vs. Group B gap yet, and D-084's sector-concentration cut
has not yet been re-run on the entry-timing (pullback) episodes
specifically, only on the plain growth-rate correlation. Analyst-
estimate-revision data was
investigated as a genuinely uncorrelated new data source and found
impractical for now (enterprise-only pricing, or no confirmed point-in-
time history at the cheap tiers) — not pursued. EBITDA (D-081) needs new
quarterly-engine extraction work (depreciation & amortization not
currently extracted) — a bigger task, not started. The quarterly
composite line of work (D-067/D-073/D-078) has no further lever to pull
and should not be revisited without new reasoning. The two unvalidated
ANNUAL-cadence
candidates from D-074 (`dividend_yield`, `size_log_revenue`) remain out
of scope per the Method rule above unless the user asks for annual work
specifically.

**Traps**
- Yahoo `close` is retroactively split-adjusted; as-reported EPS is not. Pair
  as-reported EPS with **`nominal_close`**, never `close` (cost a real bug, D-046).
- Long-running Bash commands from earlier sessions can survive and hold DuckDB
  files open, which makes unrelated tests fail with "used by another process".
  Check for stray `python.exe` processes before believing such a failure.

---

## Reference docs
`docs/CURRENT_STATE.md` (~290KB) and `docs/DECISIONS_LOG.md` (~155KB) are the
authoritative history and are **too large to read in full — use `grep`**.
`DECISIONS_LOG.md` is binding unless the user explicitly changes a decision.
Background: `docs/PROJECT_CONTEXT.md`, `docs/FAILED_APPROACHES.md`,
`docs/WORKFLOW.md`, `docs/SCORING_MODEL_V1_BLUEPRINT.md`.
