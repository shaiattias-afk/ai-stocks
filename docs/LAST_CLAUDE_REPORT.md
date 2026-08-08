# Scoring Model V1 blueprint — RESULT: PASS

Defined the executable Scoring Model V1 blueprint from the actual
frozen project data (Annual Data V1, Quarterly Data V1, Derived
Metrics V1, Historical Prices V1) and the approved project context.
**Planning task only — no database was modified, no external data was
downloaded, no paid provider was recommended, no backtest was built, no
frozen release was changed.**

Full blueprint: `docs/SCORING_MODEL_V1_BLUEPRINT.md`.
Data gap list: `data/scoring_model_v1_data_gap.json`.
`docs/CURRENT_STATE.md` and `docs/DECISIONS_LOG.md` were **not**
modified — no scoring-model decision is frozen by this task.

## What we can already calculate

Directly available or trivially calculable from already-frozen data,
for all 45 company-years: revenue growth, operating margin, ROIC, FCF
(and calculable FCF growth/margin), CapEx (annual + quarterly),
balance-sheet components (debt, cash, equity — calculable into a
strength ratio), and distance from high (from Historical Prices V1,
with a strict trailing-window safeguard). None of these require any
new data acquisition — only new calculation logic on data already
frozen.

## What's still missing (important)

**Shares outstanding does not exist anywhere in the schema.** This
single gap blocks EPS, current P/E, forward P/E, PEG, P/S, market cap,
and both candidate Entry Price V1 methods — essentially the entire
"valuation" dimension of the draft model. It is a low-risk, fast fix:
extractable from filings **already locked**, using the same proven
XBRL pipeline, no new provider. Also missing: analyst consensus data
(ratings, targets, forward estimates — genuinely needs a new external,
point-in-time-safe source) and sector/industry classification (low
priority, partially substituted by within-universe ranking).

## Recommended Scoring Model V1

9 factors, 100% weight, all computable today: revenue growth (20%),
ROIC level (15%), ROIC trend (10%), operating margin (10%), FCF growth
(15%), FCF margin (10%), balance-sheet strength (10%), CapEx discipline
(5%), distance from high (5%). Scored as continuous 0–100 percentile
rank within the 9-company universe per fiscal year. Forward P/E, PEG,
EPS growth, analyst trend, target-price gap, and relative-to-industry
are explicitly excluded (not silently dropped) pending the
shares-outstanding and analyst-data gaps. Entry Price is defined
separately from the general score (a gate, not a weighted factor) —
two methods specified (company-own historical multiple reversion,
tested first; forward-estimate multiple, tested second) — neither can
be calculated yet since both need shares outstanding at minimum.

## Recommended next step

Extend the XBRL extraction scope to add diluted weighted-average shares
outstanding for the same 9 tickers, from filings already locked — no
new filings, no new provider. This unblocks the valuation dimension and
Entry Price Method 1.

## Files produced
- `docs/SCORING_MODEL_V1_BLUEPRINT.md` (new)
- `data/scoring_model_v1_data_gap.json` (new)
- `docs/LAST_CLAUDE_REPORT.md` — this file, updated

## Result: PASS

## Report — in simple terms

- **Which scoring factors can we already calculate?** Growth
  (revenue), profitability and capital efficiency (operating margin,
  ROIC and its trend), cash generation (free cash flow growth and
  margin), balance-sheet strength, capital-spending discipline (CapEx),
  and how far the stock price has fallen from its recent high — for
  all 9 companies across the full 5-year window, using only data
  already collected and frozen.
- **Which important factors are still missing?** Everything based on
  the stock's valuation multiple (P/E, PEG) and anything based on
  analyst opinions or price targets. The root cause for the valuation
  side is simple: we never extracted the number of shares each company
  has outstanding, so we cannot yet turn a stock price into a
  per-share earnings multiple. Analyst data is a separate, genuinely
  external gap.
- **Do we have enough data to build a meaningful first scoring model?**
  Yes — a company-quality-and-growth score with a light timing overlay
  can be built today, fully from data already on hand. It is not yet
  the complete quality+valuation+timing model the project aims for,
  but it is a real, usable, honest first version.
- **Do we have enough data to run a valid backtest yet?** Not yet.
  The scoring inputs exist, but the backtest itself, an entry-price
  calculation, and comparison against a market benchmark are not built
  in this task and need a few more small steps first.
- **Recommended next single project step?** Add the missing
  shares-outstanding number for the 9 companies, using the extraction
  process we already have — no new filings, no new outside data
  provider needed for this step.
- **Estimated work remaining before the first real backtest?** A small,
  well-defined sequence: add shares outstanding, build the new scoring
  calculations, build one entry-price method, sanity-check the
  rankings by hand, then build the backtest itself. Each step is small
  and uses tools already built in this project.
- **Git commit hash?** See below (recorded after commit).
