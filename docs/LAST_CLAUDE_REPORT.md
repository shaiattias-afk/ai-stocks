# NVDA price semantics proof — RESULT: PASS

Read-only analysis of the **already-saved** NVDA historical price proof
data (`data/proofs/nvda_historical_price_proof.csv`) to determine
exactly how Yahoo Finance's `close` and `adj_close` fields behave
around NVDA's two stock splits, so the correct price methodology for
future backtesting can be chosen. **NVDA only. No new data was
downloaded. No other ticker was processed. No database was modified.
No backtest was run. No production price table was created.**

Full report: `docs/NVDA_PRICE_SEMANTICS_PROOF.md`.
Source code: `scripts/155_nvda_price_semantics_proof.py`.
Machine-readable result: `data/proofs/nvda_price_semantics_proof.json`.

## What was checked

The trading days immediately before/after both known NVDA splits
(2021-07-20 4:1, 2024-06-10 10:1), plus the `close`/`adj_close` ratio
at 13 checkpoints spanning the whole 2020–2026 series, to test —
directly from the numbers, not by assumption — whether `close` is on
the original historical scale or already retroactively split-adjusted.

## Key finding (non-obvious, evidence-derived)

**`close` in this Yahoo dataset is already split-adjusted at the
source** — for both splits, including the 2024 split relative to 2021
dates that predate it by three years. Evidence: the close ÷ adj_close
ratio is identical immediately before and after each split (no ~4× or
~10× jump anywhere in the series), and it instead declines smoothly
and monotonically from ~1.0057 (2020) to exactly 1.0000 (2026-08-06,
today) — the signature of a dividend-only backward adjustment, not a
split adjustment. This differs from the "textbook" assumption (raw
close discontinuous at splits) that the task explicitly warned against
assuming without checking.

## Four required determinations — all confirmed by data

1. `close` is already split-adjusted retroactively, not on the
   original as-traded scale. **True.**
2. `adj_close` additionally reflects dividends (the entire remaining
   close/adj_close gap, since splits are already baked into `close`).
   **True.**
3. Raw-`close` returns show no artificial gain/loss across either
   split (+3.36% and +0.02% — ordinary daily moves, not ~-75%/-90%
   drops). **True.**
4. `adj_close` returns are continuous across both splits (+3.356% and
   +0.026%). **True.**

Both split dates/ratios match the previously recorded proof exactly
(4:1 on 2021-07-20, 10:1 on 2024-06-10).

## Look-ahead problem — real, and specific

`close` values for dates before a split already encode that split's
ratio — information that did not exist yet at that historical date.
Harmless for % return math (proportional rescaling doesn't change
returns), but a genuine hazard if `close`/`adj_close` from this
endpoint is ever treated as "the literal dollar price known to a
trader on that date" (e.g. dollar-based position sizing, share-count
reconstruction). Flagged explicitly so it isn't glossed over later.

## Correction to the prior proof's wording

`docs/NVDA_HISTORICAL_PRICE_PROOF.md` called `close` "the raw,
as-traded price" — based on this analysis, that's inaccurate. `close`
is split-adjusted (not dividend-adjusted); it is not the true original
nominal price for dates before a later split. Noted in the new report;
the prior file's validation results themselves are unaffected and not
retracted.

## Files created
- `scripts/155_nvda_price_semantics_proof.py` (new)
- `docs/NVDA_PRICE_SEMANTICS_PROOF.md` (new)
- `data/proofs/nvda_price_semantics_proof.json` (new)
- `docs/LAST_CLAUDE_REPORT.md` — this file, updated

No existing database was modified. No production price table was
created. Annual Data V1, Quarterly Data V1, and Derived Metrics V1
were untouched. Nothing was committed to Git yet (pending user
review, per standard freeze workflow).

## Result: PASS

## Report — in simple terms

- **What does CLOSE represent?** The daily closing price, already
  rescaled for every split that has happened since (even future ones
  relative to that date), but not adjusted for dividends.
- **What does ADJUSTED CLOSE represent?** The same, further adjusted
  backward for all dividends paid since — a total-return series.
- **Which for historical buy/sell prices?** `close` — the conventional
  split-normalized share price, without dividends mixed in.
- **Which for calculating investment returns?** `adj_close` — the
  continuous total-return series.
- **Why?** `close` omits dividends and understates total return;
  `adj_close` is built specifically so period-over-period % change
  reflects true investment performance.
- **Is there a look-ahead problem?** Yes — `close`/`adj_close` values
  before a split already encode that split's ratio, which is future
  information relative to that date. Safe for return math; unsafe if
  treated as "the price known at the time."
- **Recommended next step:** Run this same read-only semantics check
  on the other 8 approved tickers, then make and record one binding
  decision on which field is used for which purpose before the price
  database is built.
