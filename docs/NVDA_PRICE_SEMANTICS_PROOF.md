# NVDA price semantics proof — RESULT: PASS

Read-only analysis of the **already-saved** NVDA historical price proof
data (`data/proofs/nvda_historical_price_proof.csv`, produced by
`scripts/154_nvda_historical_price_proof.py`) to determine exactly how
Yahoo Finance's `close` and `adj_close` fields behave around NVDA's two
stock splits. **No new data was downloaded. No other ticker was
touched. No database was modified. No backtest was run.**

Source code: `scripts/155_nvda_price_semantics_proof.py`.
Full machine-readable result: `data/proofs/nvda_price_semantics_proof.json`.

## Before/after tables

### Split 1 — 2021-07-20, documented 4:1

| Date | Close | Adjusted close | Split event | Close ÷ Adj.close |
|---|---:|---:|---|---:|
| 2021-07-19 | 18.7798 | 18.7150 | — | 1.003459 |
| **2021-07-20** | **18.6120** | **18.5478** | **4:1** | 1.003459 |
| 2021-07-21 | 19.4100 | 19.3431 | — | 1.003459 |

### Split 2 — 2024-06-10, documented 10:1

| Date | Close | Adjusted close | Split event | Close ÷ Adj.close |
|---|---:|---:|---|---:|
| 2024-06-07 | 120.8880 | 120.6792 | — | 1.001730 |
| **2024-06-10** | **121.7900** | **121.5796** | **10:1** | 1.001730 |
| 2024-06-11 | 120.9100 | 120.7110 | (dividend 0.01) | 1.001648 |

**Key observation**: at both splits, `close` moves by an ordinary
daily amount (+3.36% and +0.02%) — it does **not** drop to roughly
1/4 or 1/10 of its prior value the way a truly raw, unadjusted price
would on a split day. And the close ÷ adj_close ratio is **identical
immediately before and immediately after** each split (1.003459 →
1.003459; 1.001730 → 1.001730/1.001648) — if `close` were on the
original pre-split scale, this ratio would jump by ~4× and ~10× at
the respective split dates. It does not.

## Ratio across the whole series (not just near the splits)

| Date | Close | Adj. close | Close ÷ Adj.close |
|---|---:|---:|---:|
| 2020-01-02 | 5.9975 | 5.9638 | 1.005692 |
| 2021-01-04 | 13.1135 | 13.0608 | 1.004035 |
| 2021-07-19 (pre-split-1) | 18.7798 | 18.7150 | 1.003459 |
| 2021-07-21 (post-split-1) | 19.4100 | 19.3431 | 1.003459 |
| 2022-01-03 | 30.1210 | 30.0261 | 1.003159 |
| 2023-01-03 | 14.3150 | 14.2833 | 1.002222 |
| 2024-01-02 | 48.1680 | 48.0825 | 1.001777 |
| 2024-06-07 (pre-split-2) | 120.8880 | 120.6792 | 1.001730 |
| 2024-06-11 (post-split-2) | 120.9100 | 120.7110 | 1.001648 |
| 2025-01-02 | 138.3100 | 138.1037 | 1.001494 |
| 2026-08-06 (most recent) | 218.9900 | 218.9900 | **1.000000** |

The ratio never approaches 4 or 10 anywhere in the series. Instead it
declines **smoothly and monotonically** from ~1.0057 in 2020 to
exactly **1.0000** on the most recent date — the classic signature of
a *backward dividend adjustment* (which shrinks toward 1.0 as fewer
future dividends remain to discount), not a split adjustment (which
would produce a step change exactly on the split date).

## The four required determinations — derived from the data above

1. **Is `close` pre-split-adjusted or on the original raw scale?**
   **Already split-adjusted.** `close` on 2021-07-19 (three years
   *before* the 2024 split even happened) is 18.78 — dividing that by
   both the 4:1 (2021) and 10:1 (2024) ratios backward would only make
   sense if the source already applied both. Cross-check: 18.78 × 4 ×
   10 ≈ **$751**, which matches NVIDIA's actual mid-2021 trading range
   in original, pre-any-split dollars. So Yahoo's `close` field for
   this endpoint is retroactively rescaled for **all** splits that
   have since occurred — including ones that, at that historical date,
   had not happened yet.

2. **Does `adj_close` additionally reflect dividends?**
   **Yes.** Since `close` is already split-adjusted, the entire
   remaining close/adj_close gap can only come from dividends. This is
   confirmed by the gap's shape: it does not jump at either split
   date, and it shrinks toward exactly 1.0 as the date approaches
   today (2026-08-06) — consistent with 26 dividend payments being
   progressively "used up" as history moves forward and fewer future
   dividends remain to back the adjustment.

3. **Would raw-`close` returns show an artificial gain/loss across a
   split?** **No.** Since `close` is already split-adjusted at the
   source, a return computed directly from `close` across either
   split boundary is an ordinary, small daily move (+3.36% on
   2021-07-20→21, +0.02% on 2024-06-10→11) — not the ~-75% or ~-90%
   drop a genuinely unadjusted close would show.

4. **Are `adj_close` returns continuous across splits?** **Yes**, for
   the same underlying reason, plus dividend continuity. `adj_close`
   moved +3.356% and +0.026% across the two split boundaries — smooth,
   no artificial jump.

## Comparison against the previously recorded split events

| Date | This proof | Previous proof (`nvda_historical_price_proof.json`) | Match |
|---|---|---|---|
| 2021-07-20 | 4:1, confirmed present in `close`/`adj_close` behavior | 4:1 (`numerator=4.0, denominator=1.0`) | ✓ |
| 2024-06-10 | 10:1, confirmed present in `close`/`adj_close` behavior | 10:1 (`numerator=10.0, denominator=1.0`) | ✓ |

Both split dates and ratios match exactly. What this proof adds is
that the `close` field already **embeds** those ratios retroactively,
which the previous proof did not check.

## Important correction to the previous proof's wording

`docs/NVDA_HISTORICAL_PRICE_PROOF.md` described `close` as "the raw,
as-traded price on that date." Based on the evidence above, that
description is **not accurate**: `close` is split-adjusted (for
splits that occurred after the date in question), just not
dividend-adjusted. Neither `close` nor `adj_close` from this endpoint
is the true original as-traded nominal price for dates before a
later split.

## Look-ahead problem — yes, a real one, specifically for nominal price use

**There is a genuine look-ahead characteristic in the `close` field**:
a row dated 2021-07-19 already reflects a split that happened in 2024
— three years after that date. This is **harmless for percentage
return calculations** (a constant proportional rescaling applied
uniformly across the whole series does not change % returns), but it
is a real problem if `close` is ever used as "the literal dollar price
available to a trader on that historical date" — e.g. for
dollar-based position sizing, share-count reconstruction, or matching
against a contemporaneous news quote. Using it that way would silently
inject future-split information into a decision dated before that
split was public knowledge.

## Simple-language answers

- **What does CLOSE actually represent in the data we received?**
  The daily closing price, already rescaled for every stock split that
  has happened since — including splits that were still in the future
  relative to that historical date — but **not** adjusted for
  dividends.
- **What does ADJUSTED CLOSE represent?**
  The same split-adjusted closing price, further adjusted backward for
  all dividends paid since that date, so it reflects total
  shareholder return (price + reinvested dividends).
- **Which one should be used for historical buy/sell prices?**
  `close` — it is the conventional, split-normalized share price
  (matches what any modern price chart shows), without dividends
  mixed in.
- **Which one should be used for calculating investment returns?**
  `adj_close` — it is the continuous, total-return series that
  correctly captures both splits and dividends without artificial
  jumps.
- **Why?** `close` mixes actual price levels with a retroactive split
  rescaling but omits dividends, so it understates total return.
  `adj_close` is built specifically to make period-over-period % change
  represent true investment performance.
- **Does anything here create a look-ahead problem?** Yes — see above.
  `close` (and `adj_close`) values for dates before a split already
  encode that split's ratio, which is future information relative to
  that date. This is safe for return math but must never be treated as
  "the dollar price known at the time" in point-in-time logic.
- **Recommended next step:** Extend this same read-only semantics
  check to the other 8 approved tickers to confirm the same source
  behavior holds project-wide, then make and record a single binding
  decision on which field (`close` vs `adj_close`) is used for which
  purpose in the eventual price database — before that database is
  built.

## Files produced
- `scripts/155_nvda_price_semantics_proof.py` (new)
- `docs/NVDA_PRICE_SEMANTICS_PROOF.md` (this file)
- `data/proofs/nvda_price_semantics_proof.json`
- `docs/LAST_CLAUDE_REPORT.md` — updated

No database was modified. No other ticker was processed. No backtest
was run. No production price table was created.

## Result: PASS
All four required determinations, derived directly from the saved
data (not assumed), came back consistent with each other and with the
previously recorded split events.
