# AI Stock Agent — Claude Project Instructions

## Role
Practical working partner for a personal stock-analysis and investment-decision
system. Respond in clear, direct Hebrew; English technical terms where standard.

## Goal
**Current active focus (set 2026-08-13): swing trading**, targeting ~30%
moves on the stock itself within **up to 6 months**, explicitly NOT
options (no options data exists in this project at all; would need a
new, likely paid, data source and explicit approval before ever being
considered) and explicitly **no stop-loss mechanism** (user-directed —
do not propose one without the user raising it again). Primary
benchmark: **Nasdaq-100 (QQQ)**. Council-directed first backtest
(D-095): 6-month hard cap, no stop-loss, on the 10-ticker
semiconductor/AI universe (D-092) — 80% hit +30% within the window
(median ~2 months), 17% end in a serious open loss (median -33%, worst
-44%) with no exit mechanism to have avoided it, blended expected value
positive (mean +22.7%, median +31.8%), excess return vs QQQ significant
(+56.5% mean, 70% beat-rate). Same caveats as the rest of this session
apply: 9 independent tickers, one favorable regime only (2023-2025 —
2020-2022 already shown unfavorable for this rule, D-094), and the
30%/6-month/no-stop-loss parameters were chosen after seeing earlier
results, not independently derived.

**Long-term investment track — set aside for now (2026-08-13), not
deleted.** User-stated holding horizon for this track is 3-5 years
minimum; the thesis under test is *a strong company that is still
growing, has corrected in price, but has not broken fundamentally*;
success is EXCESS RETURN vs QQQ over the hold, not absolute price
recovery. Every quarterly-cadence finding validated in that track
(D-079 through D-092) is evidence at a 6-24 month forward horizon only —
none of it was ever tested at the actual 3-5 year horizon that track was
meant to serve (D-088). Resume this framing only when the user explicitly
returns to long-term work — do not silently blend it back in.

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

**Entry-timing signal (D-081) — headline numbers below use an ABSOLUTE
metric, not excess return vs QQQ (see D-088), and D-091 shows its
excess-return edge does not survive removing semiconductor/AI names at
all — read this whole block with those two corrections, not as
"Proven."** — combining D-080's growth>20%
threshold with a price pullback (stock down >=15% from its own rolling
52-week high, entry evaluated at ANY trading day, not just filing
dates) predicts recovery to that high, clearly beating a pullback-only
control on the full ~92-company universe: 68.8%/85.1%/95.5% recovery
within 6/12/24 months (n=136, 32 tickers) vs. 50.6%/67.1%/80.6% for
pullback alone with no growth filter (n=707, 91 tickers) — a
consistent 15-18 point gap at every horizon. **"Recovery" here means the
stock reclaimed its OWN prior high — not that it beat QQQ.** In a
falling market a stock can underperform its own old high while still
beating a QQQ that fell further (or vice versa in a rising market) — see
D-088 for the corrected, excess-return version of these numbers, and why
it changes the D-087 regime read substantially. Operating margin does NOT
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

**D-088 — corrected to excess return vs QQQ (the user caught the metric
error directly: "if the market fell 15% and my stock did 0%, that's
fine").** Re-scored all 136 Group A episodes by forward EXCESS return vs
QQQ instead of "recovered to own high." Result is more modest but still
real at 6/12/24 months: beat-QQQ rate is only 53-60% (close to a coin
flip, NOT the dramatic 68.8-95.5% headline above), BUT the ticker-
grouped block-bootstrapped MEAN excess return is significantly positive
at all three horizons (6mo +23.4% CI[+6.9%,+38.7%]; 12mo +49.8%
CI[+1.3%,+105.5%]; 24mo +108.9% CI[+4.5%,+262.4%] — none cross zero) —
the edge comes from a skewed payoff (a few big winners), not a high hit
rate, the same shape as every other asymmetric finding this project has
measured. **Extending to the user's actual stated investment horizon
(3-5 years, not 6-24 months) finds almost no usable data yet**: at 36
months n=18 (13 ticker-groups) and the CI **crosses zero** — not
confirmed; at 60 months n=2, both `NVDA`, a single ticker-group — a
spectacular +1317% excess return but statistically meaningless (n=1
group). **This project currently has no validated signal at the 3-5
year horizon the user actually wants** — everything proven so far
(D-079 through D-088) is 6-24 month evidence. This is the same
structural problem as the P/E finding's 5-year regime lock (D-074): not
fixable by more analysis, only by more elapsed time.

**D-091 — decisive: the entry-timing signal's excess-return edge does
NOT survive excluding semiconductor/AI names, at all.** Combined D-084's
sector exclusion (10 tickers: `AMD, APP, AVGO, ENPH, MCHP, MPWR, MU,
NVDA, RKLB, STX`) with D-088's excess-return bootstrap, on the same 32-
ticker/136-episode Group A pool. Result is starker than D-084's version
of this test on the plain growth correlation: with those 10 tickers
removed, mean excess return vs QQQ is +0.6% at 6mo (CI crosses zero,
essentially flat) and **flips NEGATIVE** at 12mo (+49.8% → **-7.3%**) and
24mo (+108.9% → **-6.7%**) — none individually significant (n shrinks to
18-21 ticker-groups, wide CIs), but there is no remaining positive
signal to point to. **Plain statement of where this leaves the project**:
neither headline quarterly-cadence finding — the growth-rate correlation
(D-079/D-080) or the entry-timing signal (D-081/D-088) — has been shown
to work as a sector-neutral edge. Both are substantially or (for entry-
timing specifically) entirely explained by AI/semiconductor exposure
during the 2023-2025 rally. **D-092**: deliberately scoping TO that
10-ticker semiconductor/AI universe instead (the mirror image) shows the
growth+pullback rule strong there (12mo mean +145.6%, beat-QQQ 77%) and
adds real value over just timing sector dips generically (Group B alone
gets +56.9%/58% on the same 10 tickers) — but on only 9-10 distinct
tickers, an even MORE 2023-2025-concentrated sample than the rest of
this session, and as a concentrated sector bet, not a diversified
strategy. Combined with D-088's separate 3-5-year-
horizon gap, this is the honest current answer to "why hasn't a reliable
entry-timing tool been found yet": what has been validated so far is
closer to "own the AI/semiconductor rally with a growth+pullback timing
overlay" than a demonstrated general-purpose signal.

**D-093/D-094 — swing-trading track (stock only, no options) and a
4-part validation pass toward freezing the model.** D-093: exiting at
+30% instead of holding long-term, on the same trigger, hits eventually
93-98% of the time — but D-094 added a real stop-loss (-15%/-20%, 12mo
max hold) and the picture is far more sober: only 54-65% hit the target
before either a stop-out (35-45%) or timeout; still net-positive
expected value per trade (+9.8% to +14.4% mean realized return) in every
cut. Threshold sensitivity (15/20/25/30% growth cutoffs, semi/AI
universe): **reassuring, the 20% choice is not fragile** — all four
give ~+133-146% mean 12mo excess return, none cross zero. Formal Group A
vs Group B significance test, within the sector: the growth filter's
added value is **proven at 6 months** (gap CI excludes zero) but **not
yet provable at 12/24 months** (large point estimates, too little data,
9-5 ticker groups). **Most important: a genuine different-regime test,
the first this project has run with real data** — daily prices reach
back to 2020 even though quarterly growth doesn't, so the pullback-only
component (no growth filter) was tested on the same 10 tickers in
2020-2022 vs 2023-2025. **It did not work in 2020-2022**: mean excess
return -7.9% (CI crosses zero), beat-rate 42% (below a coin flip) —
sharply worse than 2023-2025's +96.7%/71%. This turns the standing
regime caveat from a data-availability gap into a directly confirmed
concern: the sector-timing component this strategy leans on hardest is
not regime-independent. **The walk-forward test remains the only way to
know if the current favorable period continues.**

**Tested and not supported for practical use, but the underlying
pattern is real (D-089 → D-090)** — user's own idea: use trading volume
to find the exact bottom within a decline, not just the 15% pullback
trigger. Across 86 full-universe Group A episodes, the actual price
trough's own volume averaged 1.18x its trailing-20-day average and
ranked in the top third by volume within its own decline window; both a
ticker-grouped bootstrap (CI [0.276, 0.388], n=22) and a trough-MONTH-
grouped bootstrap (CI [0.277, 0.390], n=30 — controlling directly for
shared macro events, since the April 2025 tariff crash alone produced 16
of the 86 troughs) exclude the no-effect value (0.5), so the retrospective
pattern is real, not just an artifact of a few shared crashes (D-089).
**But it does not translate into a usable entry improvement (D-090)**: a
genuine no-look-ahead test — buy on the first day volume crosses a fixed
1.5x or 2.0x trailing-20-day threshold, instead of on the plain pullback
trigger — got a BETTER entry price only 28% of the time at either
threshold; the average price change was actually worse (-1.1% / -3.5%);
the average remaining gap to the true trough stayed 16-19%; and
subsequent 12-month excess return vs QQQ was not improved either. Volume
spikes happen almost as often on the way down as exactly at the low, so
by the time one is confirmed, the best of the decline has often already
passed. Do not re-propose a volume-spike entry trigger without new
evidence or a materially different design (e.g. volume DECELERATION
instead of a spike, untested).

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

**Open next step (post-council, D-083 → D-091)** — D-091 is now the
dominant fact: neither headline finding has a demonstrated sector-
neutral edge. Two honest paths forward, not mutually exclusive: **(A)
find a genuinely sector-neutral version** — re-run the whole growth+
pullback discovery process (D-079-D-081) restricted to a non-tech/semi
universe from the start, rather than excluding after the fact, to see if
a smaller but real effect exists outside the names already tested; or
**(B) accept the AI/semiconductor concentration and stop pretending
otherwise** — if the only thing that has ever worked is "growth+pullback
timing on AI/semi names during a rally," say that plainly and decide
whether it's still useful knowing its actual scope. Underneath both:
**(0) close the 3-5 year horizon gap** (D-088) — nothing validated so
far has been tested at the horizon this project is actually meant to
serve; only elapsed time fixes this, or extending `scripts/227`'s
methodology to whatever 3+ year-old entries exist across the full
universe (not just Group A) to enlarge the n=18/n=2 samples somewhat;
(a) a genuine walk-forward test — freeze the exact rule and score it
untouched against new quarters as they arrive, scored on EXCESS return
(D-088) — the only test not contaminated by already-mined data, and
now the only way to know if path (A) or (B) is closer to true; (b) a
proper regime split remains data-limited for a real statistical test
(not just D-087's n=10 characterization) for either the growth-rate or
P/E finding; (c) the post-hoc quintile-threshold selection (D-080's
~20% cutoff was chosen after seeing the data — still not permutation-
tested); (d) D-081's entry-timing signal still has no formal
significance test on its Group A vs. Group B gap on excess-return terms.
Analyst-estimate-revision data was
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
