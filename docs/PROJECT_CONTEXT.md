# AI Stock Agent — Project Context

## Executive summary
Build a personal system for analysing US stocks for long-term investment decisions. The system should identify high-quality companies after a meaningful correction in price, provided that the business, growth outlook, and competitive position remain strong.

The system is not intended for short-term trading. It should collect and validate data, score and compare companies, estimate attractive entry ranges, track changes, retain point-in-time snapshots, support reliable backtesting, issue focused Telegram alerts, and explain changes and risks in plain language.

Development must be gradual. Start with low-cost proofs. Consider paid data only after the model demonstrates value and the exact required endpoints and historical coverage are tested.

## Investment philosophy
The guiding idea is:

> A strong company that is still growing, has declined or corrected in price, but has not broken fundamentally.

The model must evaluate together:
1. Business quality
2. Future growth
3. Valuation
4. Timing and entry price

A decline from the all-time high is not automatically an opportunity.

## Initial universe
The working watchlist has included NVIDIA, Microsoft, Alphabet, Meta, Micron, Nebius, CrowdStrike, and Palo Alto Networks. Other companies discussed include Amazon, Broadcom, ServiceNow, Oracle, and major cruise companies.

The intended first backtest is approximately 15 companies over five years, with annual re-ranking. Later expansion may include the Nasdaq-100.

## Data requested per company
- current price
- percentage below all-time high
- current P/E
- forward P/E
- analyst rating and coverage count
- change in analyst ratings
- valuation and growth versus industry
- expected revenue growth
- expected earnings growth
- total score
- entry price or entry ranges

Additional metrics include FCF growth, ROIC, PEG, balance-sheet strength, estimate revisions, growth acceleration/deceleration, and material risks. Metrics must be sector-appropriate.

## Draft scoring model
Each parameter receives 0–100 and is multiplied by its weight. Early draft weights:

| Parameter | Draft weight |
|---|---:|
| Forward P/E | 15% |
| PEG | 15% |
| Revenue growth | 10% |
| EPS growth | 15% |
| Analyst trend | 10% |
| Target-price gap | 5% |
| ROIC | 10% |
| FCF growth | 10% |
| Balance sheet | 5% |
| Relative to industry | 5% |
| Distance from high | 5% |
| Entry price | 5% |

Weights are not final. Avoid double counting among Forward P/E, PEG, and EPS growth. Prefer continuous scores where possible. The initial CapEx anomaly factor was discussed at 5% and must be tested.

## Entry decision
A total score alone is insufficient. Require hard conditions:
- no material deterioration in forecasts or thesis;
- minimum business-quality threshold;
- acceptable balance-sheet risk;
- market price below or near the calculated entry range;
- no hidden red flag.

Final classifications may be: Not suitable, Watch, Entry candidate.

## Entry-price calculation
Combine:
- forward EPS × justified fair multiple;
- industry and company historical multiples;
- PEG or valuation-to-growth;
- analyst targets only as secondary reference;
- margin of safety adjusted for quality, volatility, cyclicality, and uncertainty.

Use a range, potentially initial entry, add-on entry, and exceptional-opportunity entry.

## Backtesting requirements
Use only information available at each historical date.
Prevent look-ahead bias, survivorship bias, and overfitting.
Retain filing and availability dates.
Use out-of-sample periods and compare against simpler models.
Benchmarks: S&P 500, Nasdaq-100, and relevant industry benchmarks.
Measure cumulative return, excess return, maximum drawdown, hit rate, volatility, and time to target.
Test 6, 12, 24, and 36 month horizons where practical.

The first target is 15 companies × 5 years with annual re-ranking and Excel output. Later expand to Nasdaq-100.

## Development philosophy
- Build a small proof before a full system.
- Verify each source on one company.
- Do not select a paid provider before testing exact endpoints, limits, history, cost, and account eligibility.
- Keep model logic independent of a single provider.
- Prefer a simple, measurable model over unnecessary complexity.
- Every claimed advantage must be supported by valid backtesting or labelled as a hypothesis.

## Intended final product
- watchlist and historical database
- quality, growth, valuation, and timing scores
- entry-price ranges
- peer and industry comparison
- forecast and analyst-revision monitoring
- focused Telegram alerts
- natural-language explanations
- backtesting reports
- decision history
