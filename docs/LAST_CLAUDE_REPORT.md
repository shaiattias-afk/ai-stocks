# 9-company historical price proof — RESULT: PASS

Extended the NVDA-only historical-price proof to all 9 approved
companies (ORCL, MSFT, META, NVDA, GOOGL, AMZN, MU, CRWD, PANW), to
validate Yahoo Finance's historical chart API across the full company
universe before any market-price database is built. **Proof/validation
stage only — no production price table, no database changes, no
backtest, no paid service, nothing pushed to a remote.**

Full report: `docs/9_TICKER_HISTORICAL_PRICE_PROOF.md`.
Source code: `scripts/156_9_ticker_historical_price_proof.py`.
Machine-readable result: `data/proofs/9_ticker_historical_price_proof.json`.
Combined CSV: `data/proofs/9_ticker_historical_price_proof.csv` (14,913 rows).

## Result summary

All 9 companies: **1,657 daily observations each**, 2020-01-02 to
2026-08-06, zero missing fields, zero negative values, zero OHLC
relationship violations, zero duplicate/out-of-order dates. Zero fetch
failures, zero rate-limiting, zero cross-company flags. NVDA's two
previously-proven splits (4:1 2021-07-20, 10:1 2024-06-10) reproduced
exactly.

## Splits found per company

| Ticker | Splits | Dividends |
|---|---|---:|
| ORCL | none | 27 |
| MSFT | none | 26 |
| META | none | 10 |
| NVDA | 4:1 (2021-07-20), 10:1 (2024-06-10) | 26 |
| GOOGL | 20:1 (2022-07-18) | 9 |
| AMZN | 20:1 (2022-06-06) | 0 |
| MU | none | 20 |
| CRWD | 4:1 (2026-07-02) — newly discovered by this proof | 0 |
| PANW | 3:1 (2022-09-14), 2:1 (2024-12-16) | 0 |

## Split-semantics finding — extended and confirmed, not assumed

Ran the same before/after boundary test proven for NVDA
(`docs/NVDA_PRICE_SEMANTICS_PROOF.md`) against every split actually
found in each ticker's own data — **never assumed** any ticker would
behave like NVDA. Result: **all 7 split events across the 5 tickers
that had one (NVDA ×2, GOOGL, AMZN, CRWD, PANW ×2) show `close` already
retroactively split-adjusted at the source**, with no exceptions — the
close ÷ adj_close ratio never jumps by the split multiple at any
boundary, and every price move at a split is ordinary daily-volatility
size, not the large drop a genuinely unadjusted close would show.

## Backtest safety — distinguished per the task's requirement

- **Safe for return calculations**: `close` and `adj_close`, all 9
  tickers — confirmed no artificial jump at any split boundary.
- **Potentially unsafe as the literal nominal dollar price known to an
  investor at that historical date**: `close`/`adj_close` for dates
  before a split, for any of the 5 tickers with a split — same
  look-ahead characteristic proven for NVDA (the value encodes a split
  ratio that did not exist yet at that date). No future split
  information was used to construct any signal here — purely
  observational.

No final production price-methodology decision was made — deferred, as
with the NVDA-only proof.

## Files created
- `scripts/156_9_ticker_historical_price_proof.py` (new)
- `data/proofs/9_ticker_historical_price_proof.csv` (new)
- `data/proofs/9_ticker_historical_price_proof.json` (new)
- `data/market_data/raw/yahoo/<TICKER>/` for all 9 tickers (raw responses — **not committed to Git**, `.gitignore`d)
- `docs/9_TICKER_HISTORICAL_PRICE_PROOF.md` (new)
- `docs/LAST_CLAUDE_REPORT.md` — this file, updated

No existing database was modified. No production price table was
created. No backtest was run. Annual Data V1, Quarterly Data V1,
Derived Metrics V1, and both NVDA-only proofs were untouched.

## Result: PASS
Runtime **15.67s** (well under the 5-minute expectation).

## Report — in simple terms

- **PASS or FAIL?** PASS.
- **Did Yahoo work for all 9 companies?** Yes, all 9 on the first
  attempt, no rate-limiting.
- **Daily prices per company?** 1,657 for every company.
- **First/last date per company?** 2020-01-02 to 2026-08-06 — identical
  across all 9.
- **Splits found per company?** See table above.
- **Dividends found per company?** ORCL 27, MSFT 26, NVDA 26, MU 20,
  GOOGL 9, META 10, AMZN 0, CRWD 0, PANW 0.
- **Any missing or suspicious data?** None across all 9 companies and
  14,913 combined observations.
- **Which companies had split-semantics tests?** NVDA, GOOGL, AMZN,
  CRWD, PANW (7 split events). ORCL, MSFT, META, MU had no split to
  test.
- **Did all tested split cases behave consistently with NVDA?** Yes —
  all 7, no exceptions.
- **Does any company need separate investigation?** No.
- **Is Yahoo suitable to proceed to the next validation stage?** Yes,
  based on this clean 9-company run — still an observation from
  proof-stage testing, not a final production data-source decision.
