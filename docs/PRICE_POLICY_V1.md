# Historical Price Policy V1 — RESULT: PASS

Defines and proves, using the already-saved data for NVDA, GOOGL, and
PANW (the 3 approved companies with proven splits), the official
project-wide rule for how historical prices, splits, and dividends are
used in return calculations, simulated execution prices, and future
portfolio backtests. **Proof/validation stage only — no production
price table, no database changes, no backtest run.**

Source code: `scripts/157_price_policy_v1_proof.py`.
Source data: `data/proofs/9_ticker_historical_price_proof.csv` (already
saved — no new download).
Outputs: `data/proofs/price_policy_v1_proof.csv`,
`data/proofs/price_policy_v1_proof.json`.

## The 5 binding rules

**Rule A — Preserve source data.** Always keep Yahoo's original
returned fields (`open`, `high`, `low`, `close`, `adj_close`, `volume`,
`dividend`, `split_ratio`) as separate columns. Never overwritten.

**Rule B — Total-return calculations use `adj_close`.** Stock return,
benchmark return, drawdown — all computed from `adj_close`. Dividends
are never separately added on top of an `adj_close`-based return
(would double count).

**Rule C — Historical nominal execution price is reconstructed.**
Yahoo OHLC is already retroactively split-adjusted (proven for NVDA in
`docs/NVDA_PRICE_SEMANTICS_PROOF.md`, extended to 9 companies in
`docs/9_TICKER_HISTORICAL_PRICE_PROOF.md`). To recover the true nominal
dollar price quoted at a historical date, multiply Yahoo `open`/`high`/
`low`/`close` by the product of every split ratio whose effective date
is **strictly after** that price date. A split effective *on* the
price date is not applied to that date. Dividends are never applied to
nominal execution prices.

**Rule D — Full portfolio backtest.** A simulation that tracks cash,
share count, dividends, and splits explicitly must use the Rule C
nominal price for buy/sell execution, explicit split events to change
share counts, and explicit dividend events to add cash — and must
**never** also apply `adj_close` inside that same simulation (would
double count corporate actions).

**Rule E — Point-in-time safety.** A future split may be used **only**
as the mechanical conversion factor in Rule C's nominal reconstruction.
It must never influence a historical score, buy/sell signal, valuation
decision, position selection, or ranking. A backtest decision dated T
may use only information available at T.

## Proof method

For NVDA, GOOGL, PANW: reconstructed nominal `open`/`high`/`low`/`close`
for all 1,657 available dates per ticker (4,971 rows total) using Rule
C's formula, then examined the ≥3 trading days immediately before/after
every known split.

## Split boundary tables

### NVDA — 4:1 (2021-07-20)

| Date | Yahoo close | Reconstructed nominal close | Split event | Cumulative future factor |
|---|---:|---:|---|---:|
| 2021-07-15 | 18.9662 | 758.65 | — | 40.0 |
| 2021-07-16 | 18.1610 | 726.44 | — | 40.0 |
| 2021-07-19 | 18.7798 | 751.19 | — | 40.0 |
| **2021-07-20** | **18.6120** | **186.12** | **4:1** | **10.0** |
| 2021-07-21 | 19.4100 | 194.10 | — | 10.0 |
| 2021-07-22 | 19.5940 | 195.94 | — | 10.0 |
| 2021-07-23 | 19.5580 | 195.58 | — | 10.0 |

(cumulative factor is 40 before this date because NVDA's later 10:1
split is also still in the future relative to July 2021; it drops to
10 once the 4:1 split has occurred, and would reach 1 only after the
2024 split too.)

### NVDA — 10:1 (2024-06-10)

| Date | Yahoo close | Reconstructed nominal close | Split event | Cumulative future factor |
|---|---:|---:|---|---:|
| 2024-06-05 | 122.44 | 1224.40 | — | 10.0 |
| 2024-06-06 | 120.998 | 1209.98 | — | 10.0 |
| 2024-06-07 | 120.888 | 1208.88 | — | 10.0 |
| **2024-06-10** | **121.79** | **121.79** | **10:1** | **1.0** |
| 2024-06-11 | 120.91 | 120.91 | — | 1.0 |
| 2024-06-12 | 125.20 | 125.20 | — | 1.0 |
| 2024-06-13 | 129.61 | 129.61 | — | 1.0 |

### GOOGL — 20:1 (2022-07-18)

| Date | Yahoo close | Reconstructed nominal close | Split event | Cumulative future factor |
|---|---:|---:|---|---:|
| 2022-07-13 | 111.3535 | 2227.07 | — | 20.0 |
| 2022-07-14 | 110.3675 | 2207.35 | — | 20.0 |
| 2022-07-15 | 111.7775 | 2235.55 | — | 20.0 |
| **2022-07-18** | **109.03** | **109.03** | **20:1** | **1.0** |
| 2022-07-19 | 113.81 | 113.81 | — | 1.0 |
| 2022-07-20 | 113.90 | 113.90 | — | 1.0 |
| 2022-07-21 | 114.34 | 114.34 | — | 1.0 |

### PANW — 3:1 (2022-09-14)

| Date | Yahoo close | Reconstructed nominal close | Split event | Cumulative future factor |
|---|---:|---:|---|---:|
| 2022-09-09 | 94.1283 | 564.77 | — | 6.0 |
| 2022-09-12 | 94.68 | 568.08 | — | 6.0 |
| 2022-09-13 | 91.48 | 548.88 | — | 6.0 |
| **2022-09-14** | **91.03** | **182.06** | **3:1** | **2.0** |
| 2022-09-15 | 89.81 | 179.62 | — | 2.0 |
| 2022-09-16 | 87.045 | 174.09 | — | 2.0 |
| 2022-09-19 | 87.675 | 175.35 | — | 2.0 |

### PANW — 2:1 (2024-12-16)

| Date | Yahoo close | Reconstructed nominal close | Split event | Cumulative future factor |
|---|---:|---:|---|---:|
| 2024-12-11 | 199.21 | 398.42 | — | 2.0 |
| 2024-12-12 | 200.105 | 400.21 | — | 2.0 |
| 2024-12-13 | 196.56 | 393.12 | — | 2.0 |
| **2024-12-16** | **202.50** | **202.50** | **2:1** | **1.0** |
| 2024-12-17 | 201.24 | 201.24 | — | 1.0 |
| 2024-12-18 | 188.76 | 188.76 | — | 1.0 |
| 2024-12-19 | 189.36 | 189.36 | — | 1.0 |

## Proof results — all determinations PASS

| # | Determination | Result |
|---|---|---|
| 1 | Yahoo `close` stays smooth through every split (no artificial jump) | ✓ largest boundary move was +3.36% (NVDA), all under 25% |
| 2 | Reconstructed nominal price shows the expected mechanical split change | ✓ all 5 splits: before/after ratio matched the documented multiple within 25% tolerance (e.g. NVDA 4:1 → 3.87×, GOOGL 20:1 → 19.64×, PANW 3:1 → 3.06×) |
| 3 | Naive returns from the reconstructed nominal series are distorted at splits | ✓ all 5: -48% to -95% artificial "loss" if nominal price were fed into return math |
| 4 | Returns from `adj_close` are not distorted at splits | ✓ all 5: ordinary moves, -1.83% to +3.36% |
| 5 | All known split events validated | ✓ NVDA 4:1(2021)/10:1(2024), GOOGL 20:1(2022), PANW 3:1(2022)/2:1(2024) — all 5 found exactly |
| 6 | Reconstructed prices positive and OHLC-valid | ✓ 0 non-positive values, 0 OHLC violations across all 4,971 rows |
| 7 | Reconstruction is deterministically reproducible | ✓ ran twice per ticker, byte-identical results |

## Proving the return-distortion point directly

| Ticker | Split | Naive % return from reconstructed nominal | % return from `adj_close` |
|---|---|---:|---:|
| NVDA | 4:1 (2021) | **-74.16%** | +3.36% |
| NVDA | 10:1 (2024) | **-90.00%** | +0.03% |
| GOOGL | 20:1 (2022) | **-94.91%** | +1.82% |
| PANW | 3:1 (2022) | **-67.28%** | -1.83% |
| PANW | 2:1 (2024) | **-48.81%** | +2.38% |

This is the concrete demonstration of why Rule C's nominal price must
never be fed into return math (Rule B/D): every single split boundary
shows a large fake "loss" from the nominal series, while `adj_close`
shows an ordinary daily move every time.

## No double-counting of dividends — structural proof

For each ticker, the `adj_close`-based full-period return was computed
by a function that reads only the `adj_close` field — the `dividend`
column is never referenced inside it. Dividends are preserved
separately, purely for Rule D's explicit portfolio-cash-accounting use.

| Ticker | Dividend events found | Total dividend amount (informational only) | Full-period `adj_close` return |
|---|---:|---:|---:|
| NVDA | 26 | $0.398 | +3,571.99% |
| GOOGL | 9 | $1.86 | +427.40% |
| PANW | 0 | $0.00 | +816.25% |

## Point-in-time safety (Rule E)

The nominal reconstruction in Rule C is used **only** as a mechanical
price-scale conversion in this proof — it produces no score, signal,
valuation, selection, or ranking of any kind. Nothing in this proof
uses a future split to alter a decision dated before that split. This
matches the look-ahead finding already established in
`docs/NVDA_PRICE_SEMANTICS_PROOF.md`: future-split information is safe
to use as a unit-conversion factor, never as decision input.

## Simple-language summary

- **What price for returns?** `adj_close` — already proven continuous
  and undistorted across every split tested.
- **What price for simulating historical buys/sells?** The
  reconstructed nominal price (Yahoo OHLC × product of all later split
  ratios) — this recovers the real dollar price that would actually
  have been quoted on that historical date.
- **How are splits handled?** As an explicit multiplicative factor
  applied only to dates before the split, for nominal-price
  reconstruction — never baked silently into any other number.
- **How are dividends handled?** Implicitly inside `adj_close` for
  return math; explicitly as cash events for a full portfolio
  simulation (Rule D) — never both at once.
- **Why no double counting?** `adj_close` already embeds dividend
  reinvestment, so Rule B/D never separately add the `dividend` column
  when `adj_close` is in use, and vice versa for the nominal-price path.
- **Why no look-ahead bias?** The only place a future split is ever
  used is as a mechanical price-scale multiplier for historical
  execution-price reconstruction (Rule C) — never as an input to any
  scoring, signal, or selection decision (Rule E).
- **Did the 3-company proof pass?** Yes — all 7 proof checks passed for
  NVDA, GOOGL, and PANW, covering all 5 known split events.

## Files produced
- `scripts/157_price_policy_v1_proof.py` (new)
- `data/proofs/price_policy_v1_proof.csv` (4,971 rows: ticker, date, original Yahoo fields, cumulative_future_split_factor, nominal_open/high/low/close)
- `data/proofs/price_policy_v1_proof.json` (full validation detail)
- `docs/PRICE_POLICY_V1.md` (this file)
- `docs/DECISIONS_LOG.md` — new decision D-044
- `docs/LAST_CLAUDE_REPORT.md` — updated

No existing database was modified. No production price table was
created. No backtest was run. No new market data was downloaded (all
values come from the already-saved `data/proofs/9_ticker_historical_
price_proof.csv`).

## Result: PASS
Runtime **0.14s** (well under the 5-minute expectation). All 7 proof
determinations passed cleanly for all 3 tickers and all 5 known split
events.
