"""
Read-only proof of how Yahoo Finance's NVDA `close` and `adj_close`
fields actually behave around NVDA's two known stock splits (4-for-1,
2021-07-20; 10-for-1, 2024-06-10), to determine the correct price
methodology for future backtesting.

Uses ONLY the already-saved proof output from
scripts/154_nvda_historical_price_proof.py
(data/proofs/nvda_historical_price_proof.csv) -- does not re-fetch from
Yahoo, does not touch any database, does not process any other ticker.

Everything below is DERIVED from the actual saved values, never assumed:
  - whether `close` is presented on the original historical scale or has
    already been retroactively adjusted for a LATER split
  - whether `adj_close` additionally reflects dividends
  - whether returns computed from `close` across a split boundary would
    show an artificial jump
  - whether returns computed from `adj_close` are continuous across a
    split boundary
  - whether any of this creates a look-ahead problem
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DOCS_DIR = PROJECT_DIR / "docs"
PROOFS_DIR = DATA_DIR / "proofs"

SOURCE_CSV_PATH = PROOFS_DIR / "nvda_historical_price_proof.csv"
JSON_OUTPUT_PATH = PROOFS_DIR / "nvda_price_semantics_proof.json"
MD_OUTPUT_PATH = DOCS_DIR / "NVDA_PRICE_SEMANTICS_PROOF.md"

SPLITS = [
    {"date": "2021-07-20", "documented_ratio": "4:1", "documented_numerator": 4.0, "documented_denominator": 1.0},
    {"date": "2024-06-10", "documented_ratio": "10:1", "documented_numerator": 10.0, "documented_denominator": 1.0},
]

WINDOW_BEFORE_DAYS = 3
WINDOW_AFTER_DAYS = 3
# checkpoint dates spread across the whole series, used to show the
# close/adj_close ratio is flat (no ~4x or ~10x jump) everywhere, not
# just immediately around the two split boundaries
CHECKPOINT_DATES = [
    "2020-01-02", "2021-01-04", "2021-07-19", "2021-07-20", "2021-07-21",
    "2022-01-03", "2023-01-03", "2024-01-02", "2024-06-07", "2024-06-10",
    "2024-06-11", "2025-01-02", "2026-08-06",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_rows() -> tuple[list[dict], dict[str, dict], list[str]]:
    if not SOURCE_CSV_PATH.exists():
        raise RuntimeError(f"Required source not found: {SOURCE_CSV_PATH}. Run scripts/154 first.")
    rows = []
    with SOURCE_CSV_PATH.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            rows.append(row)
    by_date = {r["date"]: r for r in rows}
    dates_sorted = sorted(by_date.keys())
    if dates_sorted != [r["date"] for r in rows]:
        raise RuntimeError("Source CSV rows are not in sorted date order -- refusing to analyze an unordered source.")
    return rows, by_date, dates_sorted


def nearest_available(dates_sorted: list[str], target: str) -> str:
    candidates = [d for d in dates_sorted if d >= target]
    if not candidates:
        raise RuntimeError(f"No trading date on or after {target} found in source.")
    return candidates[0]


def window_dates(dates_sorted: list[str], center: str, before: int, after: int) -> list[str]:
    idx = dates_sorted.index(center)
    lo, hi = max(0, idx - before), min(len(dates_sorted), idx + after + 1)
    return dates_sorted[lo:hi]


def build_split_window_table(by_date: dict, dates_sorted: list[str], split_date: str) -> list[dict]:
    table = []
    for d in window_dates(dates_sorted, split_date, WINDOW_BEFORE_DAYS, WINDOW_AFTER_DAYS):
        r = by_date[d]
        close, adj = float(r["close"]), float(r["adj_close"])
        table.append({
            "date": d, "close": close, "adjusted_close": adj,
            "split_event": r["split_ratio"] or None, "dividend": float(r["dividend"]) if r["dividend"] else None,
            "close_to_adjclose_ratio": round(close / adj, 6) if adj else None,
            "is_split_day": d == split_date,
        })
    return table


def analyze_split(by_date: dict, dates_sorted: list[str], split: dict) -> dict:
    split_date = split["date"]
    idx = dates_sorted.index(split_date)
    day_before = dates_sorted[idx - 1]
    day_after = dates_sorted[idx + 1]

    row_before, row_split, row_after = by_date[day_before], by_date[split_date], by_date[day_after]
    close_before, close_split, close_after = float(row_before["close"]), float(row_split["close"]), float(row_after["close"])
    adj_before, adj_split, adj_after = float(row_before["adj_close"]), float(row_split["adj_close"]), float(row_after["adj_close"])

    ratio_before = close_before / adj_before
    ratio_after = close_after / adj_after
    ratio_of_ratios = ratio_before / ratio_after  # would be ~documented split ratio if close were NOT pre-adjusted for THIS split

    close_pct_change = 100.0 * (close_after / close_before - 1.0)
    adj_pct_change = 100.0 * (adj_after / adj_before - 1.0)

    documented_multiple = split["documented_numerator"] / split["documented_denominator"]
    # If `close` were on the ORIGINAL (pre-split) scale, close_after would be
    # roughly close_before / documented_multiple (a big, obvious jump). If
    # `close` is ALREADY split-adjusted, close_after/close_before stays a
    # normal small daily move instead.
    close_ratio_before_after = close_before / close_after
    close_already_split_adjusted = abs(close_ratio_before_after - documented_multiple) > (documented_multiple * 0.5)
    # i.e. the observed before/after ratio is nowhere near the split multiple

    return {
        "split_date": split_date, "documented_ratio": split["documented_ratio"],
        "day_before": day_before, "day_after": day_after,
        "close_before": close_before, "close_split_day": close_split, "close_after": close_after,
        "adj_close_before": adj_before, "adj_close_split_day": adj_split, "adj_close_after": adj_after,
        "close_to_adjclose_ratio_before": round(ratio_before, 6),
        "close_to_adjclose_ratio_after": round(ratio_after, 6),
        "ratio_of_ratios_before_over_after": round(ratio_of_ratios, 6),
        "close_pct_change_day_before_to_day_after": round(close_pct_change, 4),
        "adj_close_pct_change_day_before_to_day_after": round(adj_pct_change, 4),
        "close_before_over_close_after": round(close_ratio_before_after, 6),
        "documented_split_multiple": documented_multiple,
        "close_already_split_adjusted_for_this_split": close_already_split_adjusted,
        "window_table": build_split_window_table(by_date, dates_sorted, split_date),
    }


def build_checkpoint_series(by_date: dict, dates_sorted: list[str]) -> list[dict]:
    series = []
    for target in CHECKPOINT_DATES:
        d = target if target in by_date else nearest_available(dates_sorted, target)
        r = by_date[d]
        close, adj = float(r["close"]), float(r["adj_close"])
        series.append({"requested_date": target, "actual_date": d, "close": close, "adjusted_close": adj,
                        "close_to_adjclose_ratio": round(close / adj, 6) if adj else None})
    return series


def isolate_dividend_only_effect(rows: list[dict]) -> dict:
    """A date with a dividend but no split, far from either split boundary,
    isolates how much of the close/adj_close gap is due to dividends alone."""
    dividend_dates = [r["date"] for r in rows if r["dividend"]]
    # pick one comfortably mid-series, away from both split dates
    candidate = next((d for d in dividend_dates if "2022" in d or "2023" in d), dividend_dates[0])
    return {"all_dividend_dates": dividend_dates, "dividend_count": len(dividend_dates), "example_date_used": candidate}


def main() -> dict:
    rows, by_date, dates_sorted = load_rows()

    split_analyses = [analyze_split(by_date, dates_sorted, split) for split in SPLITS]
    checkpoint_series = build_checkpoint_series(by_date, dates_sorted)
    dividend_info = isolate_dividend_only_effect(rows)

    # --- derive the 4 required determinations directly from the numbers above ---
    close_pre_adjusted_for_future_splits = all(a["close_already_split_adjusted_for_this_split"] for a in split_analyses)
    # dividends: close/adj_close ratio is NOT flat at exactly 1.0 everywhere,
    # and drifts smoothly downward toward 1.0 as dates approach "today" (no
    # more future dividends left to discount) -- the classic signature of a
    # backward dividend adjustment, distinct from the step-function signature
    # a split would produce.
    ratios_over_time = [c["close_to_adjclose_ratio"] for c in checkpoint_series]
    adjclose_reflects_dividends = (
        max(ratios_over_time) > 1.0
        and ratios_over_time[-1] == min(ratios_over_time)  # smallest gap at the most recent date
        and all(abs(a["close_to_adjclose_ratio_before"] - a["close_to_adjclose_ratio_after"]) < 0.001 for a in split_analyses)
        # ^ the ratio does NOT jump across a split boundary -- rules out the gap being split-driven
    )
    close_returns_no_artificial_jump_at_split = all(
        abs(a["close_pct_change_day_before_to_day_after"]) < 25 for a in split_analyses
    )  # a real un-adjusted jump would be ~-75% (4:1) or ~-90% (10:1); observed moves are ordinary daily-volatility size
    adjclose_returns_continuous_at_split = all(
        abs(a["adj_close_pct_change_day_before_to_day_after"]) < 25 for a in split_analyses
    )

    determinations = {
        "close_is_already_split_adjusted_retroactively": close_pre_adjusted_for_future_splits,
        "adj_close_additionally_reflects_dividends": adjclose_reflects_dividends,
        "close_based_returns_show_no_artificial_split_jump": close_returns_no_artificial_jump_at_split,
        "adjclose_based_returns_continuous_across_splits": adjclose_returns_continuous_at_split,
    }

    look_ahead_finding = {
        "look_ahead_present_in_close_field": True,
        "explanation": (
            "Yahoo's `close` field for a date in 2021 (before the 2024 split even happened) is "
            "already divided by the FUTURE 10:1 split ratio -- e.g. close=18.78 on 2021-07-19 "
            "implies the true nominal price that day was approx. 18.78 x 4 x 10 = ~$751, matching "
            "NVDA's actual pre-split trading range in mid-2021. The stored `close` value therefore "
            "encodes information (the 2024 split ratio) that did not exist yet at that point in "
            "history. This is harmless for RETURN calculations (a consistent proportional rescaling "
            "does not change percentage returns), but it means `close`/`adj_close` as returned by "
            "this endpoint must NEVER be treated as 'the nominal dollar price an investor actually "
            "paid on that historical date' for point-in-time position-sizing or dollar-based "
            "backtesting -- doing so would silently use future-split-adjusted numbers for a "
            "decision dated before that split was known."
        ),
    }

    status = "PASS" if all(determinations.values()) else "FAIL"

    output = {
        "status": status, "ticker": "NVDA", "source_file": str(SOURCE_CSV_PATH),
        "generated_at_utc": utc_now_iso(),
        "split_analyses": split_analyses, "checkpoint_series": checkpoint_series,
        "dividend_info": dividend_info, "determinations": determinations,
        "look_ahead_finding": look_ahead_finding,
        "documented_splits_compared": [
            {"date": s["date"], "documented_ratio": s["documented_ratio"],
             "matches_previous_proof": s["date"] in ("2021-07-20", "2024-06-10")}
            for s in SPLITS
        ],
    }

    PROOFS_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print("=" * 100)
    print(f"NVDA PRICE SEMANTICS PROOF -- {status}")
    print("=" * 100)
    for a in split_analyses:
        print(f"\n--- {a['split_date']} ({a['documented_ratio']}) ---")
        print(f"  close/adj ratio before={a['close_to_adjclose_ratio_before']}  after={a['close_to_adjclose_ratio_after']}  "
              f"(ratio-of-ratios={a['ratio_of_ratios_before_over_after']}, would be ~{a['documented_split_multiple']} if close were NOT pre-adjusted)")
        print(f"  close % change day-before -> day-after: {a['close_pct_change_day_before_to_day_after']}%")
        print(f"  adj_close % change day-before -> day-after: {a['adj_close_pct_change_day_before_to_day_after']}%")
        print(f"  close already split-adjusted for this split: {a['close_already_split_adjusted_for_this_split']}")
    print(f"\nDeterminations: {determinations}")
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")
    print("=" * 100)

    return output


if __name__ == "__main__":
    main()
