"""
Proof for the proposed Historical Price Policy V1 (5 rules: preserve
source fields, adj_close for total-return math, nominal-price
reconstruction for simulated execution prices, explicit
split/dividend-driven portfolio accounting, point-in-time safety of
future splits). NVDA, GOOGL, PANW only -- the 3 companies with proven
splits in the already-saved data.

Uses ONLY the already-saved 9-ticker proof
(data/proofs/9_ticker_historical_price_proof.csv /.json, produced by
scripts/156) -- no new download, no database access, no backtest, no
production price table.

What this proves, directly from the data:
  - Yahoo `close` stays smooth through every split (already known from
    scripts/155/156, re-confirmed here for these 3 tickers)
  - reconstructing a "nominal" (pre-later-split) price series by
    multiplying Yahoo OHLC by the product of all LATER split ratios
    produces the expected large mechanical jump at each split boundary
    (proving the two series -- smooth adjusted vs. jumpy nominal --
    are doing genuinely different, well-understood things)
  - a naive percentage return computed from the nominal series across a
    split boundary WOULD be wildly distorted, while the same return
    computed from adj_close is not -- justifying Rule B
  - the adj_close-based return path never references the dividend
    column at all, structurally proving dividends are not double
    counted when adj_close is used for returns
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DOCS_DIR = PROJECT_DIR / "docs"
PROOFS_DIR = DATA_DIR / "proofs"

SOURCE_CSV_PATH = PROOFS_DIR / "9_ticker_historical_price_proof.csv"
CSV_OUTPUT_PATH = PROOFS_DIR / "price_policy_v1_proof.csv"
JSON_OUTPUT_PATH = PROOFS_DIR / "price_policy_v1_proof.json"

TICKERS = ["NVDA", "GOOGL", "PANW"]

# Already-proven split events this proof must reconcile against (from
# scripts/154 for NVDA, scripts/156 for GOOGL/PANW). Never invented --
# cross-checked against what the source data actually contains below.
EXPECTED_SPLITS = {
    "NVDA": [{"year": 2021, "ratio": "4:1"}, {"year": 2024, "ratio": "10:1"}],
    "GOOGL": [{"year": 2022, "ratio": "20:1"}],
    "PANW": [{"year": 2022, "ratio": "3:1"}, {"year": 2024, "ratio": "2:1"}],
}

WINDOW_DAYS = 3  # at least 3 trading days before and after each split


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_source_rows() -> dict[str, list[dict]]:
    if not SOURCE_CSV_PATH.exists():
        raise RuntimeError(f"Required source not found: {SOURCE_CSV_PATH}. Run scripts/156 first.")
    by_ticker: dict[str, list[dict]] = {t: [] for t in TICKERS}
    with SOURCE_CSV_PATH.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row["ticker"] in by_ticker:
                by_ticker[row["ticker"]].append(row)
    for t in TICKERS:
        by_ticker[t].sort(key=lambda r: r["date"])
        dates = [r["date"] for r in by_ticker[t]]
        if dates != sorted(dates):
            raise RuntimeError(f"[{t}] source rows not in sorted date order.")
    return by_ticker


def parse_split_ratio(split_ratio_str: str) -> tuple[float, float]:
    """'4:1' -> (numerator=4.0, denominator=1.0). Fails closed on anything else."""
    parts = split_ratio_str.split(":")
    if len(parts) != 2:
        raise RuntimeError(f"Ambiguous split ratio string: {split_ratio_str!r}")
    numerator, denominator = float(parts[0]), float(parts[1])
    if numerator <= 0 or denominator <= 0:
        raise RuntimeError(f"Ambiguous split ratio (non-positive component): {split_ratio_str!r}")
    return numerator, denominator


def extract_splits(rows: list[dict]) -> list[dict]:
    splits = []
    by_date_seen: dict[str, str] = {}
    for r in rows:
        if r["split_ratio"]:
            if r["date"] in by_date_seen and by_date_seen[r["date"]] != r["split_ratio"]:
                raise RuntimeError(f"Ambiguous split: date {r['date']} has conflicting ratios "
                                    f"{by_date_seen[r['date']]!r} and {r['split_ratio']!r}")
            by_date_seen[r["date"]] = r["split_ratio"]
            numerator, denominator = parse_split_ratio(r["split_ratio"])
            splits.append({"date": r["date"], "split_ratio": r["split_ratio"],
                            "numerator": numerator, "denominator": denominator,
                            "multiplier": numerator / denominator})
    splits.sort(key=lambda s: s["date"])
    return splits


def cumulative_future_split_factor(target_date: str, splits: list[dict]) -> float:
    """Product of multipliers for every split whose date is STRICTLY AFTER
    target_date. A split effective ON target_date is NOT included (Rule C)."""
    factor = 1.0
    for s in splits:
        if s["date"] > target_date:
            factor *= s["multiplier"]
    return factor


def reconstruct_nominal_series(rows: list[dict], splits: list[dict]) -> list[dict]:
    reconstructed = []
    for r in rows:
        factor = cumulative_future_split_factor(r["date"], splits)
        yahoo_open, yahoo_high, yahoo_low, yahoo_close = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
        adj_close = float(r["adj_close"])
        reconstructed.append({
            **r, "open": yahoo_open, "high": yahoo_high, "low": yahoo_low, "close": yahoo_close, "adj_close": adj_close,
            "cumulative_future_split_factor": factor,
            "nominal_open": yahoo_open * factor, "nominal_high": yahoo_high * factor,
            "nominal_low": yahoo_low * factor, "nominal_close": yahoo_close * factor,
        })
    return reconstructed


def validate_reconstruction(ticker: str, reconstructed: list[dict]) -> dict:
    non_positive = []
    ohlc_violations = []
    for r in reconstructed:
        for field in ("nominal_open", "nominal_high", "nominal_low", "nominal_close"):
            if r[field] <= 0:
                non_positive.append({"date": r["date"], "field": field, "value": r[field]})
        if not (r["nominal_low"] <= r["nominal_open"] <= r["nominal_high"]):
            ohlc_violations.append({"date": r["date"], "reason": "nominal_low <= nominal_open <= nominal_high violated"})
        if not (r["nominal_low"] <= r["nominal_close"] <= r["nominal_high"]):
            ohlc_violations.append({"date": r["date"], "reason": "nominal_low <= nominal_close <= nominal_high violated"})

    return {
        "ticker": ticker, "rows_checked": len(reconstructed),
        "non_positive_reconstructed_prices": non_positive,
        "ohlc_violations_after_reconstruction": ohlc_violations,
        "clean": len(non_positive) == 0 and len(ohlc_violations) == 0,
    }


def window_around(dates_sorted: list[str], center: str, before: int, after: int) -> list[str]:
    idx = dates_sorted.index(center)
    lo, hi = max(0, idx - before), min(len(dates_sorted), idx + after + 1)
    return dates_sorted[lo:hi]


def build_split_window_table(ticker: str, reconstructed: list[dict], split: dict) -> dict:
    by_date = {r["date"]: r for r in reconstructed}
    dates_sorted = sorted(by_date.keys())
    if split["date"] not in dates_sorted:
        raise RuntimeError(f"[{ticker}] split date {split['date']} not present in source data.")
    win_dates = window_around(dates_sorted, split["date"], WINDOW_DAYS, WINDOW_DAYS)

    table = []
    for d in win_dates:
        r = by_date[d]
        table.append({
            "date": d, "yahoo_close": r["close"], "reconstructed_nominal_close": round(r["nominal_close"], 6),
            "split_event": r["split_ratio"] or None, "cumulative_future_split_factor": r["cumulative_future_split_factor"],
            "is_split_day": d == split["date"],
        })

    idx = dates_sorted.index(split["date"])
    day_before, day_after = dates_sorted[idx - 1], dates_sorted[idx + 1]
    r_before, r_after = by_date[day_before], by_date[day_after]

    yahoo_close_pct_change = 100.0 * (r_after["close"] / r_before["close"] - 1.0)
    nominal_close_before_over_after = r_before["nominal_close"] / r_after["nominal_close"]

    return {
        "split_date": split["date"], "split_ratio": split["split_ratio"], "multiplier": split["multiplier"],
        "window_table": table,
        "day_before": day_before, "day_after": day_after,
        "yahoo_close_pct_change_before_to_after": round(yahoo_close_pct_change, 4),
        "yahoo_close_stays_smooth": abs(yahoo_close_pct_change) < 25,
        "nominal_close_before_over_after_ratio": round(nominal_close_before_over_after, 4),
        "nominal_shows_expected_mechanical_jump": abs(nominal_close_before_over_after - split["multiplier"]) < (split["multiplier"] * 0.25),
    }


def demonstrate_return_distortion(ticker: str, reconstructed: list[dict], split: dict) -> dict:
    """Show that a naive % return computed from the RECONSTRUCTED NOMINAL
    series across a split boundary is wildly distorted, while the same
    return computed from adj_close (Rule B) is not. Proves why nominal
    reconstruction must never be fed into return math."""
    by_date = {r["date"]: r for r in reconstructed}
    dates_sorted = sorted(by_date.keys())
    idx = dates_sorted.index(split["date"])
    day_before, day_after = dates_sorted[idx - 1], dates_sorted[idx + 1]
    r_before, r_after = by_date[day_before], by_date[day_after]

    nominal_return_pct = 100.0 * (r_after["nominal_close"] / r_before["nominal_close"] - 1.0)
    adjclose_return_pct = 100.0 * (r_after["adj_close"] / r_before["adj_close"] - 1.0)

    return {
        "split_date": split["date"],
        "naive_return_from_reconstructed_nominal_pct": round(nominal_return_pct, 4),
        "return_from_adj_close_pct": round(adjclose_return_pct, 4),
        "nominal_return_is_distorted": abs(nominal_return_pct) > 25,
        "adjclose_return_is_not_distorted": abs(adjclose_return_pct) < 25,
    }


def prove_no_double_dividend_counting(ticker: str, reconstructed: list[dict]) -> dict:
    """Structural proof: the adj_close-based return function never reads
    the dividend column. Total dividends are reported separately, purely
    informationally, never added on top of an adj_close-based return."""
    total_dividend_amount = sum(float(r["dividend"]) for r in reconstructed if r["dividend"])
    dividend_dates = sorted(r["date"] for r in reconstructed if r["dividend"])

    def adjclose_return(rows: list[dict]) -> float:
        # Deliberately takes only adj_close values -- no dividend field is
        # in scope for this function at all.
        first_adj, last_adj = rows[0]["adj_close"], rows[-1]["adj_close"]
        return 100.0 * (last_adj / first_adj - 1.0)

    full_period_return_pct = round(adjclose_return(reconstructed), 4)

    return {
        "ticker": ticker, "dividend_events_found": len(dividend_dates), "dividend_dates": dividend_dates,
        "total_dividend_amount_informational_only": round(total_dividend_amount, 6),
        "full_period_adjclose_return_pct": full_period_return_pct,
        "adjclose_return_function_reads_dividend_field": False,
        "note": "adj_close already embeds dividend reinvestment; the dividend column is preserved "
                "only for explicit portfolio-cash-accounting use (Rule D), never added to an adj_close return.",
    }


def validate_known_splits(ticker: str, found_splits: list[dict]) -> dict:
    expected = EXPECTED_SPLITS[ticker]
    checks = []
    for exp in expected:
        match = next((s for s in found_splits if s["date"].startswith(str(exp["year"])) and s["split_ratio"] == exp["ratio"]), None)
        checks.append({**exp, "found": match is not None, "matched_date": match["date"] if match else None})
    return {"ticker": ticker, "expected_count": len(expected), "checks": checks, "all_found": all(c["found"] for c in checks)}


def prove_deterministic_reproducibility(rows: list[dict], splits: list[dict]) -> bool:
    run1 = reconstruct_nominal_series(rows, splits)
    run2 = reconstruct_nominal_series(rows, splits)
    return all(r1["nominal_close"] == r2["nominal_close"] and r1["nominal_open"] == r2["nominal_open"]
               and r1["nominal_high"] == r2["nominal_high"] and r1["nominal_low"] == r2["nominal_low"]
               for r1, r2 in zip(run1, run2))


ROUNDED_COLUMNS = ("cumulative_future_split_factor", "nominal_open", "nominal_high", "nominal_low", "nominal_close")


def write_csv(all_reconstructed: dict[str, list[dict]]) -> None:
    PROOFS_DIR.mkdir(parents=True, exist_ok=True)
    columns = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "dividend", "split_ratio",
               "cumulative_future_split_factor", "nominal_open", "nominal_high", "nominal_low", "nominal_close"]
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for ticker in TICKERS:
            for r in all_reconstructed[ticker]:
                writer.writerow([round(r[c], 6) if c in ROUNDED_COLUMNS else r[c] for c in columns])


def main() -> dict:
    start_time = time.perf_counter()
    print("=" * 100)
    print("HISTORICAL PRICE POLICY V1 PROOF -- NVDA, GOOGL, PANW")
    print("=" * 100)

    by_ticker = load_source_rows()

    per_ticker_results = {}
    all_reconstructed = {}
    split_window_analyses = []
    return_distortion_demos = []
    double_dividend_proofs = []
    known_split_validations = []
    reconstruction_validations = []
    deterministic_checks = []

    for ticker in TICKERS:
        rows = by_ticker[ticker]
        splits = extract_splits(rows)
        print(f"\n[{ticker}] {len(rows)} rows, {len(splits)} split(s) found: {[s['split_ratio'] for s in splits]}")

        known = validate_known_splits(ticker, splits)
        known_split_validations.append(known)
        if not known["all_found"]:
            raise RuntimeError(f"[{ticker}] FAIL -- missing required known split event(s): {known}")

        reconstructed = reconstruct_nominal_series(rows, splits)
        all_reconstructed[ticker] = reconstructed

        recon_check = validate_reconstruction(ticker, reconstructed)
        reconstruction_validations.append(recon_check)
        if not recon_check["clean"]:
            raise RuntimeError(f"[{ticker}] FAIL -- reconstruction validation failed: {recon_check}")

        deterministic_ok = prove_deterministic_reproducibility(rows, splits)
        deterministic_checks.append({"ticker": ticker, "deterministic": deterministic_ok})
        if not deterministic_ok:
            raise RuntimeError(f"[{ticker}] FAIL -- reconstruction is not deterministically reproducible.")

        for split in splits:
            window = build_split_window_table(ticker, reconstructed, split)
            window["ticker"] = ticker
            split_window_analyses.append(window)
            print(f"  split {split['split_ratio']} on {split['date']}: yahoo_close_pct_change="
                  f"{window['yahoo_close_pct_change_before_to_after']}%  nominal_before/after_ratio="
                  f"{window['nominal_close_before_over_after_ratio']} (expect ~{split['multiplier']})")

            distortion = demonstrate_return_distortion(ticker, reconstructed, split)
            distortion["ticker"] = ticker
            return_distortion_demos.append(distortion)

        double_dividend_proofs.append(prove_no_double_dividend_counting(ticker, reconstructed))

        per_ticker_results[ticker] = {"splits_found": splits, "reconstruction_check": recon_check}

    all_yahoo_close_smooth = all(w["yahoo_close_stays_smooth"] for w in split_window_analyses)
    all_nominal_jumps_expected = all(w["nominal_shows_expected_mechanical_jump"] for w in split_window_analyses)
    all_nominal_returns_distorted = all(d["nominal_return_is_distorted"] for d in return_distortion_demos)
    all_adjclose_returns_clean = all(d["adjclose_return_is_not_distorted"] for d in return_distortion_demos)
    all_known_splits_found = all(k["all_found"] for k in known_split_validations)
    all_reconstructions_clean = all(r["clean"] for r in reconstruction_validations)
    all_deterministic = all(d["deterministic"] for d in deterministic_checks)

    determinations = {
        "yahoo_close_stays_smooth_through_every_split": all_yahoo_close_smooth,
        "nominal_reconstruction_shows_expected_mechanical_split_jump": all_nominal_jumps_expected,
        "naive_nominal_returns_are_distorted_across_splits": all_nominal_returns_distorted,
        "adjclose_returns_are_not_distorted_across_splits": all_adjclose_returns_clean,
        "all_known_split_events_validated": all_known_splits_found,
        "all_reconstructed_prices_positive_and_ohlc_valid": all_reconstructions_clean,
        "reconstruction_is_deterministically_reproducible": all_deterministic,
    }
    status = "PASS" if all(determinations.values()) else "FAIL"

    write_csv(all_reconstructed)

    runtime = round(time.perf_counter() - start_time, 2)
    output = {
        "status": status, "tickers": TICKERS, "source_file": str(SOURCE_CSV_PATH),
        "determinations": determinations,
        "known_split_validations": known_split_validations,
        "reconstruction_validations": reconstruction_validations,
        "deterministic_checks": deterministic_checks,
        "split_window_analyses": split_window_analyses,
        "return_distortion_demonstrations": return_distortion_demos,
        "no_double_dividend_counting_proofs": double_dividend_proofs,
        "runtime_seconds": runtime, "generated_at_utc": utc_now_iso(),
    }
    JSON_OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print(f"\nDeterminations: {determinations}")
    print(f"\nCSV written to {CSV_OUTPUT_PATH}")
    print(f"JSON written to {JSON_OUTPUT_PATH}")
    print(f"\nFINAL: {status}  (runtime {runtime}s)")
    print("=" * 100)

    if status != "PASS":
        raise RuntimeError(f"FAIL -- determinations: {determinations}")
    return output


if __name__ == "__main__":
    main()
