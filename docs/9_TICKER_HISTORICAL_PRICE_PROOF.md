# 9-company historical price proof — RESULT: PASS

Extends the NVDA-only historical-price proof
(`docs/NVDA_HISTORICAL_PRICE_PROOF.md`,
`docs/NVDA_PRICE_SEMANTICS_PROOF.md`) to all 9 approved companies, to
validate that Yahoo Finance's historical chart API is reliable across
the full company universe before any market-price database is built.
**Proof/validation stage only** — no production price table, no
database changes, no backtest.

Source code: `scripts/156_9_ticker_historical_price_proof.py`.
Outputs: `data/proofs/9_ticker_historical_price_proof.csv`,
`data/proofs/9_ticker_historical_price_proof.json`.

## Source and method

Same source proven for NVDA: Yahoo Finance historical chart API
(`https://query1.finance.yahoo.com/v8/finance/chart/<TICKER>`),
`interval=1d`, `events=div,splits`, `includeAdjustedClose=true`,
2020-01-01 through the current available date (2026-08-06, run on
2026-08-07). Each ticker's exact raw HTTP response was saved
byte-for-byte to `data/market_data/raw/yahoo/<TICKER>/` (git-ignored,
never committed). Fetched sequentially, 1-second spacing between
tickers, up to 3 attempts per ticker with backoff on failure/rate-limit
(none were needed — every ticker succeeded on attempt 1).

## Per-company results

| Ticker | Observations | First date | Last date | Splits found | Dividends found | Status |
|---|---:|---|---|---|---:|---|
| ORCL | 1,657 | 2020-01-02 | 2026-08-06 | none | 27 | PASS |
| MSFT | 1,657 | 2020-01-02 | 2026-08-06 | none | 26 | PASS |
| META | 1,657 | 2020-01-02 | 2026-08-06 | none | 10 | PASS |
| NVDA | 1,657 | 2020-01-02 | 2026-08-06 | 4:1 (2021-07-20), 10:1 (2024-06-10) | 26 | PASS |
| GOOGL | 1,657 | 2020-01-02 | 2026-08-06 | 20:1 (2022-07-18) | 9 | PASS |
| AMZN | 1,657 | 2020-01-02 | 2026-08-06 | 20:1 (2022-06-06) | 0 | PASS |
| MU | 1,657 | 2020-01-02 | 2026-08-06 | none | 20 | PASS |
| CRWD | 1,657 | 2020-01-02 | 2026-08-06 | 4:1 (2026-07-02) | 0 | PASS |
| PANW | 1,657 | 2020-01-02 | 2026-08-06 | 3:1 (2022-09-14), 2:1 (2024-12-16) | 0 | PASS |

All 9 companies returned **exactly the same** observation count (1,657)
and the same first/last trading dates. NVDA's two splits reproduce
exactly the previously proven values (4:1 on 2021-07-20, 10:1 on
2024-06-10) — cross-checked programmatically
(`nvda_known_splits_reproduced: true`).

CRWD's 4:1 split (2026-07-02) is a real, recent corporate action
returned by the source — not previously documented in this project,
newly discovered by this proof.

## Per-ticker validation (all 14 checks)

Every ticker passed every check with **zero exceptions**:
- Dates unique and sorted — ✓ all 9
- No duplicate trading dates — ✓ all 9
- No negative prices — ✓ all 9
- No negative volume — ✓ all 9
- No missing open/high/low/close/adjusted-close values — ✓ all 9 (0 missing fields)
- `low <= open <= high` — ✓ all 9 (0 violations)
- `low <= close <= high` — ✓ all 9 (0 violations)
- Splits extracted only as returned by Yahoo (none inferred) — ✓
- Dividends extracted only as returned by Yahoo (none inferred) — ✓
- `close` and `adj_close` kept as separate fields — ✓ all 9

## Cross-company validation

**No flags.** Zero fetch failures, zero rate-limiting, no ticker with
fewer dates than expected, no gaps, no missing values, no ticker
behaving differently from the others. All 9 tickers share an identical
1,657-row shape over the identical date range.

## Split-semantics test — extended from NVDA to every ticker with a split

For each ticker with at least one split, the trading days immediately
before/after every split were examined the same way as the NVDA proof
(`docs/NVDA_PRICE_SEMANTICS_PROOF.md`): checking whether the
close ÷ adj_close ratio jumps by the split multiple at the boundary
(which would mean `close` is on the original, unadjusted scale) or
stays flat (which would mean `close` is already retroactively
split-adjusted at the source).

| Ticker | Split | Ratio before | Ratio after | Close % change at boundary | Close already split-adjusted? |
|---|---|---:|---:|---:|---|
| NVDA | 4:1 (2021-07-20) | 1.003459 | 1.003459 | +3.356% | **Yes** |
| NVDA | 10:1 (2024-06-10) | 1.001730 | 1.001648 | +0.018% | **Yes** |
| GOOGL | 20:1 (2022-07-18) | 1.008867 | 1.008867 | +1.818% | **Yes** |
| AMZN | 20:1 (2022-06-06) | 1.000000 | 1.000000 | +0.531% | **Yes** |
| CRWD | 4:1 (2026-07-02) | 1.000000 | 1.000000 | +3.207% | **Yes** |
| PANW | 3:1 (2022-09-14) | 1.000000 | 1.000000 | -1.826% | **Yes** |
| PANW | 2:1 (2024-12-16) | 1.000000 | 1.000000 | +2.381% | **Yes** |

None of the split boundaries above show anything close to the
documented split multiple (20×, 4×, 3×, 2×) — every ratio and every
price move is ordinary daily-volatility size. **All 7 tested split
events, across 5 different companies, are consistent with the NVDA
finding**: `close` is already retroactively split-adjusted at the
source in this Yahoo endpoint, for every ticker tested, without
exception.

ORCL, MSFT, META, and MU had no split during 2020–2026, so per the task
instruction: **"No split-semantics test possible during available
period."** for each of these four.

## Backtest safety — return-safe vs. nominal-price-safe

Distinguishing, per the task's explicit requirement:

- **Suitable for return calculations**: `close` and `adj_close`, for
  all 9 tickers — confirmed, no ticker showed an artificial jump
  across a split boundary in either series.
- **Potentially unsafe as the literal historical dollar price known to
  an investor at that date**: `close` (and `adj_close`), for any
  ticker with a split, on dates *before* that split — because the
  stored value already incorporates a split ratio that did not exist
  yet at that historical date (same look-ahead characteristic proven
  for NVDA in `docs/NVDA_PRICE_SEMANTICS_PROOF.md`). This applies to
  NVDA, GOOGL, AMZN, CRWD, and PANW for dates before their respective
  split(s). No future split information was used to construct any
  signal in this proof — this is an observational finding only.

**No final production price-methodology decision has been made** —
that remains a separate future task, same as for the NVDA-only proof.

## Simple-language report

- **PASS or FAIL?** **PASS.**
- **Did Yahoo work for all 9 companies?** Yes — all 9 fetched
  successfully on the first attempt, with no rate-limiting.
- **Daily prices per company:** 1,657 for every one of the 9 companies.
- **First/last date per company:** 2020-01-02 to 2026-08-06 for every
  company — identical across all 9.
- **Splits found per company:** NVDA 2 (4:1, 10:1), GOOGL 1 (20:1),
  AMZN 1 (20:1), CRWD 1 (4:1, newly found — 2026-07-02), PANW 2 (3:1,
  2:1). ORCL, MSFT, META, MU had none.
- **Dividends found per company:** ORCL 27, MSFT 26, NVDA 26, MU 20,
  GOOGL 9, META 10, AMZN 0, CRWD 0, PANW 0.
- **Missing or suspicious data?** None. Zero missing fields, zero
  negative values, zero OHLC violations, zero duplicate/out-of-order
  dates, across all 9 companies and all 14,913 combined observations.
- **Which companies had split-semantics tests?** NVDA, GOOGL, AMZN,
  CRWD, PANW (7 split events total). ORCL, MSFT, META, MU had no split
  to test.
- **Did all tested split cases behave consistently with NVDA?** Yes —
  all 7 split events, across all 5 companies tested, showed `close`
  already split-adjusted at the source, exactly like NVDA. No
  exceptions found.
- **Does any company need separate investigation?** No — no
  cross-company flags were raised; every company matches the same
  clean pattern.
- **Is Yahoo suitable to proceed to the next validation stage?** Yes,
  based on this clean 9-company run — the source is consistent,
  complete, and behaves predictably across the full company universe.
  This is still an observation from proof-stage testing, not a final
  production data-source decision.

## Files produced
- `scripts/156_9_ticker_historical_price_proof.py` (new)
- `data/proofs/9_ticker_historical_price_proof.csv` (14,913 rows: ticker, date, open, high, low, close, adj_close, volume, dividend, split_ratio)
- `data/proofs/9_ticker_historical_price_proof.json` (full per-ticker validation, cross-company validation, split-semantics results)
- `data/market_data/raw/yahoo/<TICKER>/` for all 9 tickers (raw responses — **not committed to Git**, `.gitignore`d)
- `docs/9_TICKER_HISTORICAL_PRICE_PROOF.md` (this file)
- `docs/LAST_CLAUDE_REPORT.md` — updated

No existing database was modified. No production price table was
created. No backtest was run. Annual Data V1, Quarterly Data V1, and
Derived Metrics V1 were untouched. The NVDA-only proof files were not
changed.

## Result: PASS
Runtime **15.67s** (well under the 5-minute expectation). All 9
companies passed all validation checks with zero data-quality issues
and zero cross-company flags.
