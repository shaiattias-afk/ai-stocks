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

**Proven, for 2020-2021 entries — regime-untested since** — entry-date raw
P/E predicts 5-year excess return vs. QQQ: correlation **-0.247**,
company-grouped block-bootstrap 95% CI **[-0.449, -0.025]**, 84 independent
tickers. The 94 company-years never in the original sample reproduce it
independently (-0.253). The effect is **asymmetric**: the cheapest quintile
barely beats QQQ (+0.1%/yr, 40% win rate), while P/E > ~80 loses **-15.7%/yr
with an 85% loss rate**. The supported rule is **"avoid richly-valued
entries," not "buy the cheapest."** (D-063 → D-064 → D-068.) **Caveat found
in D-074, not yet resolved**: every 5-year-eligible company-year in this
result is a 2020-2021 entry — a single macro period, not tested against a
regime change, because no later cohort has reached its 5-year mark yet. At
shorter horizons where 2022+ entries ARE eligible (24mo, 36mo) the signal
disappears entirely, including for the SAME pre-2022 companies at the
shorter horizon. This does not mean the effect is false — it means it is
unconfirmed outside the one period tested, and will not be retestable at 5
years for a second regime until roughly 2027-2028.

**Tested and not supported** — do not re-propose without new evidence: Scoring
Model V1 composite (D-061) and V2 candidate (D-062) show no predictive power;
the composite is *negatively* related to 5-year returns (D-063); the quarterly
5-factor composite (D-067) is **retracted, not just fragile** — D-073's
robustness check shows only 1 of 6 (lookback, horizon) cells tested is
significant, and excluding any ONE of 6 of the 9 tickers erases it; the
value/growth two-bucket model adds nothing at 12 months (D-066); none of
Scoring Inputs V1's 9 factors shows a 5-year wide-universe signal (D-074).
**Nothing tested so far shows signal at a 12-month horizon** — the one real
effect is multi-year. See also `docs/FAILED_APPROACHES.md`.

**Open next step** — two unvalidated candidate factors from D-074's wide-
universe search (`dividend_yield`: n=137, corr +0.226, CI [0.045, 0.396];
`size_log_revenue`: n=104, corr +0.282, CI [0.063, 0.477]) need the same
robustness/regime discipline D-074 applied to the P/E finding before either
is trusted. Quarterly Data now covers the full 135-company universe (D-072)
at the same 6-metric scope (98.3% usable) — the 3-ticker pilot this section
used to reference is superseded by that full load.

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
