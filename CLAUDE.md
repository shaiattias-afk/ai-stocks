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
D-080)** — raw YoY quarterly revenue growth rate (current quarter vs.
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
sector-neutral fundamental edge — neither tested yet.

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
as D-079/D-080** — not tested against a different macro period. No
formal significance test run yet on the Group A vs. Group B gap.

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

**Open next step (post-council, D-083)** — the placebo test cleared one
concern (the correlation is not pure noise); several remain, in rough
priority order per the council's backtesting advisor: (a) test whether
the growth>20% signal survives with AI/semiconductor names excluded
(sector-concentration confound, flagged by the accountant advisor) —
cheap, usable now; (b) a genuine walk-forward test — freeze the exact
rule (growth>20% + pullback>=15%) and score it untouched against new
quarters as they arrive, the only test not contaminated by having
already mined all current data; (c) the regime split itself remains
data-limited, not yet possible, for either the growth-rate or P/E
finding; (d) D-081's entry-timing signal has no formal significance
test on its Group A vs. Group B gap yet. Analyst-estimate-revision data
was investigated as a genuinely uncorrelated new data source and found
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
