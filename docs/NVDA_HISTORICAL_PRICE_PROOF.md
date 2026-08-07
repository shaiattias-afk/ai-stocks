# NVDA historical price proof — RESULT: PASS

Read-only proof that reliable daily historical prices and corporate
actions can be obtained and validated from Yahoo Finance's historical
chart data, before any full market-price database is built. **NVDA
only.** No database was modified, no production price table was
created, no other ticker was processed, no backtest was run.

## Source tested

Yahoo Finance historical chart API
(`https://query1.finance.yahoo.com/v8/finance/chart/NVDA`), requested
with `interval=1d`, `events=div,splits`, `includeAdjustedClose=true`,
for `2020-01-01` through `2026-08-07` (exclusive).

The **exact raw HTTP response** (187,591 bytes) was saved byte-for-byte
to `data/market_data/raw/yahoo/NVDA/nvda_chart_raw_20260807T083730Z.json`,
re-read and verified identical after writing. A sibling
`..._request_meta.json` records the exact request parameters and fetch
timestamp for reproducibility. **Raw market data is git-ignored**
(`data/market_data/raw/` added to `.gitignore`) — never committed.

## Validation results — all PASS

| # | Check | Result |
|---|---|---|
| 1 | Daily dates unique and sorted | ✓ |
| 2 | No duplicate trading dates | ✓ 0 duplicates |
| 3 | No negative prices | ✓ 0 |
| 4 | No negative volume | ✓ 0 |
| 5 | `low <= open <= high` and `low <= close <= high` for every complete row | ✓ 0 violations (checked across all 1,657 complete-OHLC rows) |
| 6 | Missing trading-day price fields | ✓ **0** — every row has open/high/low/close/volume/adj_close populated |
| 7 | Number of daily observations | **1,657** |
| 8 | First / last available trading dates | **2020-01-02** / **2026-08-06** |
| 9 | All split events extracted from the source | **2** splits found (see below) |
| 10 | Both known NVDA splits confirmed | ✓ **4-for-1 (2021)** and ✓ **10-for-1 (2024)**, both found exactly as documented |
| 11 | No split inferred/invented | ✓ — only what the source returned is reported; nothing added |
| 12 | Raw `close` and `adj_close` kept as separate fields | ✓ — both columns present in every row, never merged |
| 13 | Backtesting price methodology decision | **Not made** — explicitly deferred, see below |

## Split events found (exactly as returned by the source, not inferred)

| Date | Ratio | Numerator | Denominator |
|---|---|---:|---:|
| 2021-07-20 | 4:1 | 4.0 | 1.0 |
| 2024-06-10 | 10:1 | 10.0 | 1.0 |

Both match NVIDIA's independently documented corporate-action history
exactly (the 4-for-1 split effective 2021-07-20 and the 10-for-1 split
effective 2024-06-10).

## Dividends found (bonus corporate-action data, not required but preserved)

**26** dividend events were returned by the same request and preserved
in the `dividend` column on their respective trading dates (e.g.
`2020-02-27: 0.004`, `2020-06-04: 0.004`, ...).

## Raw close vs. adjusted close

They **differ** on **1,613 of 1,657** rows (97%) — expected, since
`adj_close` retroactively incorporates the two splits and 26 dividends
found above, while `close` is the raw, as-traded price on that date.
Both are preserved as separate CSV/JSON columns; **no decision has been
made about which one will be used for backtesting** — that is
explicitly out of scope for this proof and will be decided separately.

## Backtest safety note

This task only validated and stored source data. No point-in-time
signal was constructed, and no future corporate action was used to
retroactively adjust any historical decision point — there is no
decision logic in this proof at all, only data acquisition and
validation.

## Files created
- `scripts/154_nvda_historical_price_proof.py` (new)
- `data/proofs/nvda_historical_price_proof.csv` (1,657 rows: date, open, high, low, close, adj_close, volume, dividend, split_ratio)
- `data/proofs/nvda_historical_price_proof.json` (full detail: request metadata, all validation results, all split/dividend events)
- `data/market_data/raw/yahoo/NVDA/nvda_chart_raw_20260807T083730Z.json` (exact raw response — **not committed to Git**, `.gitignore`d)
- `docs/NVDA_HISTORICAL_PRICE_PROOF.md` (source of this report)
- `docs/LAST_CLAUDE_REPORT.md` — updated
- `.gitignore` — added `data/market_data/raw/`

No existing database was modified. No production price table was
created. Annual Data V1, Quarterly Data V1, and Derived Metrics V1 were
untouched.

## Result: PASS
Runtime **0.88s** (well under the 3-minute expectation). All validation
checks passed cleanly with zero data-quality problems found.

## Report — in simple terms

- **Did the source work?** Yes. Yahoo Finance's chart API returned a
  complete, valid daily dataset for NVDA on the first attempt.
- **How many daily prices were obtained?** **1,657** trading days,
  from 2020-01-02 to 2026-08-06.
- **Were both NVIDIA splits found?** Yes — the 4-for-1 split (July 2021)
  and the 10-for-1 split (June 2024) were both present in the source
  data exactly as expected, with no need to guess or invent them.
- **Was any data-quality problem found?** No. Zero missing fields, zero
  negative prices or volume, zero duplicate or out-of-order dates, zero
  high/low/open/close relationship violations, across all 1,657 rows.
- **Do raw and adjusted close differ?** Yes, substantially — on 97% of
  rows — because the adjusted series retroactively bakes in both stock
  splits and 26 dividend payments. Which series to use for backtesting
  is a separate decision, not made here.
- **Is this source suitable to continue testing on all 9 companies?**
  Based on this one clean run: yes, it looks suitable to extend the same
  read-only proof approach to the other 8 tickers next, to see whether
  this same reliability holds across the full universe. This is an
  observation from one successful test, not a recommendation of a final
  production data provider.
