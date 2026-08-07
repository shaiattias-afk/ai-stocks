# Historical Price Policy V1 — RESULT: PASS

Defined and proved the official Historical Price Policy V1 — 5 binding
rules for how historical prices, splits, and dividends are used in
return calculations, simulated execution prices, and future portfolio
backtests. Proved using the already-saved data for **NVDA, GOOGL, and
PANW** (the 3 companies with proven splits). **Proof/validation stage
only — no production price table, no database changes, no backtest, no
new market data downloaded.**

Full report: `docs/PRICE_POLICY_V1.md`.
Decision recorded: `docs/DECISIONS_LOG.md` — **D-044**.
Source code: `scripts/157_price_policy_v1_proof.py`.
Machine-readable result: `data/proofs/price_policy_v1_proof.json`.
Reconstructed series: `data/proofs/price_policy_v1_proof.csv` (4,971 rows).

## The 5 rules (now binding, D-044)

- **Rule A**: preserve Yahoo's original fields, never overwrite.
- **Rule B**: use `adj_close` for total-return calculations; never add
  dividends on top (double counting).
- **Rule C**: reconstruct nominal historical execution prices by
  multiplying Yahoo OHLC by the product of all *later* split ratios
  (a split on the price date itself doesn't apply to that date).
- **Rule D**: a full portfolio backtest uses Rule C's nominal price for
  execution plus explicit split/dividend events — never `adj_close` in
  the same simulation (double counting).
- **Rule E**: a future split may be used only as Rule C's mechanical
  conversion factor — never as input to a score, signal, valuation, or
  ranking decision (point-in-time safety).

## Proof results — all 7 determinations PASS

1. Yahoo `close` stays smooth through every split — largest move +3.36%.
2. Reconstructed nominal price shows the expected mechanical jump —
   e.g. NVDA 4:1 → 3.87×, GOOGL 20:1 → 19.64×, PANW 3:1 → 3.06×.
3. Naive returns from the reconstructed nominal series are wildly
   distorted at every split boundary (-48% to -95%).
4. Returns from `adj_close` are not distorted (-1.8% to +3.4%) — the
   concrete justification for Rule B/D.
5. All 5 known split events validated exactly (NVDA 4:1/2021,
   10:1/2024; GOOGL 20:1/2022; PANW 3:1/2022, 2:1/2024).
6. All reconstructed prices positive, all OHLC relationships valid
   across 4,971 rows.
7. Reconstruction deterministically reproducible (ran twice, identical).

## No double-counting — structurally proven

The `adj_close`-based return function for each ticker never reads the
`dividend` column — verified by construction, not just by inspection.
Dividends are preserved separately, purely for Rule D's explicit
portfolio-cash use.

## Files created
- `scripts/157_price_policy_v1_proof.py` (new)
- `data/proofs/price_policy_v1_proof.csv` (new)
- `data/proofs/price_policy_v1_proof.json` (new)
- `docs/PRICE_POLICY_V1.md` (new)
- `docs/DECISIONS_LOG.md` — D-044 added
- `docs/LAST_CLAUDE_REPORT.md` — this file, updated

No existing database was modified. No production price table was
created. No backtest was run. No new market data was downloaded — all
values came from the already-saved 9-ticker proof CSV. Annual Data V1,
Quarterly Data V1, Derived Metrics V1, and all prior price proofs were
untouched.

## Result: PASS
Runtime **0.14s** (well under the 5-minute expectation).

## Report — in simple terms

- **What price for returns?** `adj_close` — proven continuous and
  undistorted across every split tested.
- **What price for simulating historical buys/sells?** A reconstructed
  nominal price: Yahoo's price multiplied by every later split ratio,
  recovering the real dollar price actually quoted on that date.
- **How are splits handled?** As an explicit multiplier applied only to
  dates before the split, used solely to reconstruct nominal prices —
  never silently baked into any other number.
- **How are dividends handled?** Implicitly inside `adj_close` for
  return math; explicitly as cash events for a full portfolio
  simulation — never both at once.
- **Why no double counting?** `adj_close` already embeds dividends, so
  the dividend column is never separately added when `adj_close` is in
  use, and the nominal-price path never touches dividends at all.
- **Why no look-ahead bias?** A future split is used only as a
  mechanical price-scale multiplier (Rule C) — never as input to any
  scoring, signal, or selection decision (Rule E).
- **Did the 3-company proof pass?** Yes — all 7 checks passed for NVDA,
  GOOGL, and PANW, covering all 5 known split events.
- **Git commit hash?** See below.
