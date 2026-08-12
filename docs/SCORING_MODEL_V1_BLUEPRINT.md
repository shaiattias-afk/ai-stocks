# Scoring Model V1 — Blueprint (planning document, no code executed)

> **2026-08-11 update: this blueprint has now been EXECUTED.** Stages
> 3–6 are built, loaded, and backtested — see `docs/DECISIONS_LOG.md`
> D-057 (Stage 3, Scoring Model V1), D-058 (Stage 4 Method 1, Entry
> Price — supersedes this document's own Stage 6 step 1 assessment;
> see D-058 for why the shares-outstanding gap turned out to already be
> closed by D-046), D-059 (Stage 5 gap #4, QQQ benchmark), and D-060
> (Stage 6 step 5, the first backtest run — read its survivorship-bias
> caveat before trusting any number). This document is left exactly as
> originally written below, as the planning record; it is no longer
> current about what has and hasn't been built.

Defines the executable Scoring Model V1 blueprint from the actual
frozen project data (Annual Data V1, Quarterly Data V1, Derived
Metrics V1, Historical Prices V1) and the approved project context
(`docs/PROJECT_CONTEXT.md`). **This is a planning document only.** No
database was modified, no external data was downloaded, no paid
provider was evaluated or recommended, no backtest was built, and no
frozen release was changed.

Read as source of truth: `CLAUDE.md`, `docs/CURRENT_STATE.md`,
`docs/DECISIONS_LOG.md`, `docs/PROJECT_CONTEXT.md`,
`docs/FAILED_APPROACHES.md`, `docs/WORKFLOW.md`, and the live
production database (`data/database/ai_stock_agent.duckdb`), inspected
read-only.

---

## STAGE 1 — Inventory: what we really have

Inspected the actual production tables and columns (not assumed).
Production database contains: `companies`, `sec_filings`,
`extraction_runs`, `financial_metric_results` (Annual Data V1),
`quarterly_extraction_runs`, `quarterly_metric_results` (Quarterly Data
V1), `derived_metric_results` (Derived Metrics V1, frozen — D-043),
`historical_prices_daily` (Historical Prices V1, frozen — D-045),
`historical_review_items`.

**Universe**: 9 tickers (ORCL, MSFT, META, NVDA, GOOGL, AMZN, MU, CRWD,
PANW). Annual coverage: 45 company-fiscal-years (5 per ticker, one
ticker's window varies 2019–2026 by fiscal-year-end). Quarterly
coverage: 180 company-quarters (9 × 20). Daily prices: 14,913 rows
(2020-01-02 to 2026-08-06).

### Classification of every candidate factor

| Factor | Class | Notes |
|---|---|---|
| Revenue growth | **A** | Already computed |
| ROIC | **A** | Already computed |
| FCF (level) | **A** | Already computed |
| Balance-sheet components (debt, cash, equity) | **A** | Already computed |
| CapEx | **A** | Already computed, annual **and** quarterly |
| Operating margin | **A** | Already computed (Derived Metrics V1) |
| FCF growth | **B** | Calculable — same YoY pattern as `revenue_yoy_growth`, not yet computed |
| ROIC trend | **B** | Calculable — YoY change in existing `roic` |
| FCF margin | **B** | Calculable — `free_cash_flow / revenue` |
| Balance-sheet strength ratio | **B** | Calculable — e.g. `adjusted_net_debt / stockholders_equity` |
| CapEx discipline / anomaly | **B** | Calculable — `capex / revenue` vs. own trailing average |
| Distance from high | **B**, with a caveat | Calculable from `historical_prices_daily`, but only within the 2020-01-02+ window (see below) |
| EPS growth | **D** | No shares-outstanding or EPS field exists anywhere in the schema |
| Current P/E | **D** | Needs shares outstanding (market cap); not extracted |
| Forward P/E | **D** | Needs forward EPS estimate (external, not in SEC filings) |
| PEG | **D** | Depends on P/E and EPS growth, both **D** |
| Analyst trend (ratings, coverage, revisions) | **D** | External data, not in SEC filings, not point-in-time-safe without a specialized provider |
| Target-price gap | **D** | Same as analyst trend |
| Relative-to-industry valuation/growth | **D** | No sector/industry field exists in `companies` or anywhere else |
| Entry price | **N/A here** | Defined separately in Stage 4, not a weighted factor in the general score |

### Detail for every A/B factor

| Factor | Source table/fields | Formula | Frequency | Earliest knowable date | Backtest-safe? |
|---|---|---|---|---|---|
| Revenue growth | `derived_metric_results` (`derived_metric='revenue_yoy_growth'`) | current-period revenue ÷ matching prior-period revenue − 1 | annual + quarterly | `availability_date` column (= filing date of the later of the two filings) | Yes — already point-in-time gated (D-039/D-043) |
| Operating margin | `derived_metric_results` (`derived_metric='operating_margin'`) | `operating_income / revenue` | annual + quarterly | `availability_date` column | Yes |
| ROIC | `financial_metric_results` (`metric_name='roic'`) joined to `sec_filings.filing_date` | `nopat / average_invested_capital` | **annual only** | 10-K `filing_date` | Yes, once joined for the filing date (not stored redundantly on the row itself) |
| FCF (level) | `financial_metric_results` (`metric_name='free_cash_flow'`) | `operating_cash_flow − capex` | **annual only** | 10-K `filing_date` | Yes |
| Balance-sheet components | `financial_metric_results` (`total_debt`, `cash_and_equivalents`, `short_term_investments`, `stockholders_equity`, `adjusted_net_debt`) | as extracted / `adjusted_net_debt = total_debt − cash − short_term_investments` | **annual only** | 10-K `filing_date` | Yes |
| CapEx | `financial_metric_results` (annual) or `quarterly_metric_results` (`metric_name='capex'`) | as extracted | annual + quarterly | filing/`availability_date` | Yes |
| FCF growth (new) | same as FCF, new YoY formula | `current_fy_fcf / prior_fy_fcf − 1` | annual (FCF is annual-only) | max of the two filing dates | Yes, if built the same way as `revenue_yoy_growth` |
| ROIC trend (new) | same as ROIC, new YoY delta | `current_fy_roic − prior_fy_roic` | annual | max of the two filing dates | Yes |
| FCF margin (new) | FCF ÷ revenue, same fiscal year | `free_cash_flow / revenue` | annual | 10-K filing date | Yes |
| Balance-sheet strength ratio (new) | `adjusted_net_debt / stockholders_equity` (or `/ free_cash_flow`) | as stated | annual | 10-K filing date | Yes |
| CapEx discipline (new) | `capex / revenue`, compared to the same company's own trailing 3-year average | deviation from own history | annual (quarterly optional later) | 10-K filing date | Yes |
| Distance from high | `historical_prices_daily` (`close`/`nominal_close`) | `(rolling_high − price) / rolling_high`, rolling high computed only from dates **≤** the evaluation date | daily | any date in 2020-01-02+ | Yes, **only if** the "high" is computed strictly trailing (never a full-sample or centered window) — see Stage 2 |

**Important coverage caveat**: FCF, ROIC, and balance-sheet data exist
**only at annual frequency**. Quarterly Data V1 covers only `revenue`,
`operating_income`, `pretax_income`, `capex`, `income_tax_expense`. Any
scoring model that wants a quarterly-refreshed quality score is
currently limited to revenue/margin/CapEx-based signals only.

**Distance-from-high caveat**: the price database starts 2020-01-02.
For long-listed companies (MSFT, ORCL), the true all-time high may
predate this window, so "distance from high" computed today actually
means "distance from the highest close since 2020" — a
company-comparability caveat, not a look-ahead problem.

---

## STAGE 2 — Backtest safety

| Factor | Look-ahead bias | Survivorship bias | Revised/future estimates | Double counting |
|---|---|---|---|---|
| Forward P/E | High — a "forward" estimate is inherently forward-looking; any historical value must be the estimate *as it stood on that date*, which we do not have | — | High — consensus estimates are revised continuously; today's forward P/E is not what was known historically | With PEG, EPS growth |
| PEG | Inherits Forward P/E's and EPS growth's risk | — | High | With Forward P/E, EPS growth |
| EPS growth (forward/expected) | High, same reason as Forward P/E | — | High | With Forward P/E, PEG |
| EPS growth (trailing/historical) | Low, **if** built the same point-in-time way as `revenue_yoy_growth` — but requires shares-outstanding data we don't have yet | — | Low (uses only already-filed, filing-date-gated data) | With revenue growth (partial) |
| Analyst ratings / trend / target price | Very high — consensus figures are typically only available "as of today" from most providers, not as a historical point-in-time series; using today's rating history retroactively silently injects future information | — | Very high | With Forward P/E / PEG (both reflect market sentiment) |
| Industry-relative metrics | Moderate — a company's sector/peer set membership can itself change over time; must use the peer set as it existed at the evaluation date, not today's | Moderate — today's peer/industry universe reflects only currently-relevant companies | Low, once static classification exists | Low |
| Distance from high | Concrete and avoidable: the "high" must be computed using **only price data up to and including the evaluation date** (a strictly trailing rolling maximum); computing it against the full stored series (including future dates) would be a direct look-ahead violation | Low | None (raw price fact) | Low |
| Entry-price calculations | High if fundamentals/estimates aren't gated by `filing_date`/`availability_date`, or if the nominal historical execution price (Historical Price Policy V1 Rule C) isn't used correctly for the specific historical date | — | Depends on which method (see Stage 4) | Could double count with Forward P/E/PEG if both are used |

**1. Historical data we already have safely**: revenue, operating
income, operating margin, ROIC, FCF, CapEx, balance-sheet components,
and daily prices — all already point-in-time gated (`filing_date`
for annual, `availability_date` for quarterly, and Historical Price
Policy V1 for prices). These are safe to use in a real backtest today.

**2. Current-only data**: none of our existing tables are
"current-only" — everything already stored is a historical,
point-in-time-gated series. The current-only risk applies to data we
**don't** have yet: analyst ratings/targets and forward EPS estimates
are, in practice, usually only obtainable as today's snapshot from most
providers, not as a genuine historical as-of-date series.

**3. Data requiring a new historical source**: shares outstanding
(extractable from filings **already locked** — no new source needed,
just a wider extraction scope on data we already possess); sector/
industry classification (a one-time, largely static addition, not
financial data, low risk to add manually for 9 known companies); a
genuine point-in-time analyst-consensus history (a real new external
source, and the hardest of the three to get right for backtesting,
since most feeds only expose "current" consensus).

---

## STAGE 3 — Recommended Scoring Model V1

**Recommendation: build Scoring Model V1 now, using only what is
already available or calculable from frozen data, explicitly excluding
the valuation-multiple and analyst-dependent factors until shares
outstanding and analyst data exist.** This is not a case for stopping
development — every factor below is computable today with no new data
acquisition, only new calculation logic on data already frozen.

Scores use **continuous 0–100 percentile rank within the 9-company
universe, computed separately for each fiscal year** (i.e., a
company's score for FY2023 is its percentile rank among the other 8
companies' FY2023 values for that factor). This avoids needing an
external industry benchmark (which we don't have) while still giving a
relative, comparable, continuous 0–100 score. Once a sector taxonomy
exists, this can be refined to rank within-sector instead of
within-universe.

| # | Factor | Purpose | Weight | Exact calculation | 0–100 scoring rule | Required data | Exists today? |
|---|---|---|---:|---|---|---|---|
| 1 | Revenue growth | Top-line growth quality | 20% | `revenue_yoy_growth` (already computed) | Percentile rank within same-fiscal-year universe | `derived_metric_results` | Yes |
| 2 | ROIC (level) | Capital-efficiency quality | 15% | `roic` (already computed) | Percentile rank within same-fiscal-year universe | `financial_metric_results` | Yes |
| 3 | ROIC trend | Improving/deteriorating capital efficiency | 10% | current FY `roic` − prior FY `roic` | Percentile rank of the delta | `financial_metric_results` | Yes (new formula) |
| 4 | Operating margin | Core profitability quality | 10% | `operating_margin` (already computed) | Percentile rank within same-fiscal-year universe | `derived_metric_results` | Yes |
| 5 | FCF growth | Cash-generation growth quality | 15% | `free_cash_flow` YoY, same pattern as revenue growth | Percentile rank of the growth rate | `financial_metric_results` | Yes (new formula) |
| 6 | FCF margin | Cash conversion quality | 10% | `free_cash_flow / revenue` | Percentile rank within same-fiscal-year universe | `financial_metric_results` | Yes (new formula) |
| 7 | Balance-sheet strength | Financial risk / resilience | 10% | `adjusted_net_debt / stockholders_equity` (lower = stronger) | Percentile rank, inverted (lowest ratio → 100) | `financial_metric_results` | Yes (new formula) |
| 8 | CapEx discipline | Flags CapEx spikes/anomalies vs. the company's own history | 5% | `\|capex/revenue − own 3-yr trailing average capex/revenue\|` | Percentile rank, inverted (smallest deviation → 100) | `financial_metric_results` | Yes (new formula) |
| 9 | Distance from high | Timing signal (small weight, per project philosophy: a decline is not automatically an opportunity) | 5% | `(trailing_high − price) / trailing_high`, trailing high computed only from dates ≤ evaluation date | Percentile rank within same-fiscal-year universe | `historical_prices_daily` | Yes (new formula, strict trailing-only window) |
| | **Total** | | **100%** | | | | |

**Explicitly excluded from V1** (marked, not silently dropped):

| Factor | Why excluded | Recommendation |
|---|---|---|
| Forward P/E, current P/E, PEG, EPS growth, P/S, market cap-based anything | No shares-outstanding data exists anywhere in the schema — blocks every per-share/market-cap metric | **Temporarily exclude from V1.** This is a low-risk, fast fix (re-extract an existing XBRL tag from filings already locked, no new provider) — recommended as the very next development step (Stage 6), not a reason to stop. |
| Analyst trend, target-price gap | Requires a genuine external, point-in-time analyst-consensus history; most providers only expose "current" data, making historical backtesting unsafe without specific verification | **Temporarily exclude from V1.** Revisit only after a provider is found that can supply a true historical as-of-date consensus series — do not add this factor using only "current" data. |
| Relative-to-industry valuation/growth | No sector/industry classification exists in the schema | **Temporarily exclude as a standalone factor.** Partially mitigated already: the within-universe percentile-rank scoring mechanism above gives every factor a relative-to-peers flavor, just not a true sector-relative one. |

**Double-counting check**: revenue growth (top-line) and FCF growth
(cash-based) are economically distinct; ROIC (capital efficiency) and
operating margin (accounting profitability) and FCF margin (cash
profitability) each capture a different lens on profitability, kept at
modest individual weights (15%/10%/10%) rather than one dominant
factor; CapEx discipline is a specific risk flag, not a duplicate of
FCF; distance-from-high is intentionally isolated at low weight as the
sole timing signal, consistent with the project's explicit warning
that a price decline is not automatically an opportunity.

**Company quality vs. valuation vs. timing**: factors 1–8 are entirely
"company quality" (growth, profitability, capital efficiency, capital
discipline, balance-sheet risk) — **95% of the total weight**. Factor 9
(distance from high) is the sole "timing" signal — **5%**. **There is
currently no true "valuation" dimension** (no P/E, P/S, or multiple-based
factor) because shares outstanding does not exist in the schema. This
is stated plainly rather than glossed over: **Scoring Model V1, as
buildable today, is a quality-and-growth model with a small timing
overlay — not yet a quality+valuation+timing model** as the project
context intends. Closing the shares-outstanding gap (Stage 6, step 1)
is the single highest-leverage next step to complete the model.

---

## STAGE 4 — Entry Price (defined separately from the general score)

Per the project context, entry price is a **separate, secondary**
component (a gate: "market price below or near the calculated entry
range"), not one of the 9 weighted score factors above.

**Both candidate methods below share one hard prerequisite that
neither currently has: shares outstanding.** Without it, no market cap
and no per-share multiple (P/E, P/FCF, EV/anything) can be computed at
all. **No real entry price can be calculated yet — this is stated
explicitly rather than approximated.**

### Method 1 (test first): company-own historical multiple reversion
Compare the company's current implied multiple (e.g. Price/Earnings or
Price/FCF) against its **own** trailing 5-year historical range, using
only our own data — no external estimates.
- **Exact inputs**: daily close price (have — Historical Prices V1),
  diluted shares outstanding (**missing**), net income or FCF (have —
  Annual Data V1), filing dates for point-in-time gating (have).
- **Missing**: shares outstanding only.
- **How the historical price DB is used**: for each historical
  evaluation date, take the **nominal historical close** (Historical
  Price Policy V1 Rule C — the reconstructed, split-adjusted-for-splits-
  known-at-that-time price is *not* what's wanted here; the raw
  as-quoted `close`/`nominal_close` distinction must be re-examined once
  shares data exists, since a multiple needs the price an investor could
  actually have paid divided by the shares count at that date) as of
  that exact date only.
- **How we avoid future information**: fundamentals used are only those
  with `filing_date` (annual) or `availability_date` (quarterly) ≤ the
  evaluation date; the "historical range" is computed only from prior
  fiscal years relative to the evaluation date, never including the
  evaluation year itself or later years.

### Method 2 (test second, after a data source is found): forward-estimate multiple
`forward EPS × justified fair multiple`, roughly PEG-style, per the
project context.
- **Exact inputs**: forward EPS consensus estimate **as it stood at each
  historical date** (**missing — external, and must be a genuine
  point-in-time history, not today's consensus**), a "justified fair
  multiple" derived from quality/growth (could reuse Scoring Model V1's
  quality factors once built).
- **Missing**: the entire forward-estimate history. This method cannot
  be tested at all until a provider offering true historical as-of-date
  consensus is found and verified — explicitly deferred, not built on
  a guess.

**Recommendation**: build and test Method 1 first, immediately after
shares outstanding is added (Stage 6, step 1) — it needs no new
external data source. Method 2 stays blocked until a specialized
analyst-estimate history provider is evaluated (out of scope for this
task, per instruction not to investigate providers yet).

---

## STAGE 5 — Prioritized data gap list

See also the machine-readable version: `data/scoring_model_v1_data_gap.json`.

| Priority | Gap | Why needed | Essential for Scoring V1? | Point-in-time required? | Approx. required history | Can proceed without it temporarily? |
|---|---|---|---|---|---|---|
| 1 | Shares outstanding (diluted, weighted-average) | Blocks EPS, P/E, PEG, P/S, market cap, and both candidate Entry Price methods | **No** for the 9-factor quality/growth score above; **Yes** for any valuation factor and for Entry Price | Yes (per fiscal period, same as other fundamentals) | Same 5-year window already covered — **no new filings needed**, only re-extraction from filings already locked | Yes, for Scoring V1 as recommended. No, for a complete model matching the full project-context draft. |
| 2 | Analyst consensus data (ratings, coverage, revisions, price targets, forward EPS) | Needed for Analyst trend, Target-price gap, Forward P/E, PEG, Entry Price Method 2 | No | Yes, and this is the hard part — most providers expose only "current" data | Full 5-year backtest window, ideally with as-of-date snapshots | Yes — V1 excludes these factors entirely |
| 3 | Sector/industry classification | Needed for a true "Relative to industry" factor | No (within-universe ranking is a partial substitute) | No (classification is largely static) | None (current classification suffices) | Yes |
| 4 | Benchmark index historical prices (S&P 500, Nasdaq-100, relevant industry benchmarks) | Needed to measure excess return / drawdown vs. benchmark in the eventual backtest (`docs/PROJECT_CONTEXT.md` Backtesting requirements) | No — not needed to compute scores, only to evaluate a backtest | Yes, matching the backtest window | Same 5-year window | Yes — not needed until Stage 6 step 5 |

---

## STAGE 6 — Next development plan (shortest path to the first valid backtest)

1. **Extend XBRL extraction scope to add diluted weighted-average shares
   outstanding** for the same 9 tickers, using the already-locked
   filings and the same proven statement-first Arelle pipeline — no
   new filings, no new provider.
   **Success criterion**: shares outstanding validated `PASS` for all
   45 company-years (same coverage discipline as revenue).

2. **Build a new "Scoring Inputs V1" derived-metrics extension** (a new
   engine version — does not modify the frozen `derived_metric_results`
   table, D-043) computing the 9 factors in Stage 3's table, all from
   already-frozen Annual Data V1 and Historical Prices V1, independent
   of step 1.
   **Success criterion**: all 45 company-years produce a complete
   9-factor composite score with no missing factor.

3. **Build Entry Price Method 1** (company-own historical multiple
   reversion) once shares outstanding (step 1) exists.
   **Success criterion**: for each of the 45 company-years, a
   historical multiple band is computed and cross-checked for
   plausibility against at least one independently known public
   trailing-P/E figure.

4. **Run Scoring Model V1 across the full 9×5 window and sanity-check
   the rankings** manually (not yet a backtest — a plausibility check).
   **Success criterion**: rankings pass manual review with no
   unexplained anomaly.

5. **Build the point-in-time backtest engine** (annual re-ranking,
   score + Method 1 entry gate, using only `filing_date`/
   `availability_date`-gated fundamentals and Historical Price Policy V1
   nominal execution prices, measured against benchmark data once
   Stage-5 gap #4 is closed).
   **Success criterion**: one complete, reproducible backtest report
   for the 9-company/5-year window, spot-checked for zero look-ahead
   violations. **Not built in this task.**

No new infrastructure beyond what each step's success criterion
requires — steps 1–2 use only the existing extraction pipeline and
existing frozen data.

---

## Files produced
- `docs/SCORING_MODEL_V1_BLUEPRINT.md` (this file)
- `data/scoring_model_v1_data_gap.json`
- `docs/LAST_CLAUDE_REPORT.md` — updated

`docs/CURRENT_STATE.md` and `docs/DECISIONS_LOG.md` were **not**
modified — no scoring-model decision is frozen by this task, per
instruction.

## Result: PASS (planning task — no PASS/FAIL data validation applies; blueprint is complete and actionable)
