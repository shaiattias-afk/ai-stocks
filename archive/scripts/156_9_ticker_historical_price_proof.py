"""
Read-only historical-price proof, extended from NVDA (scripts/154,
scripts/155) to all 9 approved companies. Proves that reliable daily
historical prices and corporate actions (splits, dividends) can be
obtained and validated from Yahoo Finance's historical chart data for
the full company universe, before any full market-price database is
built.

Fetches once per ticker from Yahoo Finance's public chart API
(https://query1.finance.yahoo.com/v8/finance/chart/<TICKER>), saves the
exact raw HTTP response byte-for-byte per ticker under
data/market_data/raw/yahoo/<TICKER>/ (git-ignored -- raw market data is
never committed), then parses and validates each ticker independently,
plus a cross-company comparison.

Also re-runs, per ticker, the same split-semantics test proven for NVDA
in scripts/155 (does `close` behave as already retroactively
split-adjusted?) against every split actually found in that ticker's
own data -- never assuming another ticker matches NVDA's behavior.

Does NOT modify any existing database. Does NOT create a production
price table. Does NOT run a backtest. Does NOT decide which price
series (close vs. adjusted close) will be used for backtesting -- that
decision is explicitly deferred; both are preserved as separate fields.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DOCS_DIR = PROJECT_DIR / "docs"
PROOFS_DIR = DATA_DIR / "proofs"

TICKERS = ["ORCL", "MSFT", "META", "NVDA", "GOOGL", "AMZN", "MU", "CRWD", "PANW"]

FETCH_START_DATE = date(2020, 1, 1)
FETCH_END_DATE_EXCLUSIVE = date.today() + timedelta(days=1)  # "current available date"

CSV_OUTPUT_PATH = PROOFS_DIR / "9_ticker_historical_price_proof.csv"
JSON_OUTPUT_PATH = PROOFS_DIR / "9_ticker_historical_price_proof.json"

MAX_ATTEMPTS_PER_TICKER = 3
RETRY_BACKOFF_SECONDS = [2, 5]  # used between attempt 1->2 and 2->3
DELAY_BETWEEN_TICKERS_SECONDS = 1.0

# NVDA's documented splits, already proven in scripts/154/155 -- reused here
# only as a cross-check that this run reproduces the earlier finding, never
# assumed for any other ticker.
NVDA_KNOWN_SPLITS = [
    {"year": 2021, "numerator": 4.0, "denominator": 1.0},
    {"year": 2024, "numerator": 10.0, "denominator": 1.0},
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def fetch_raw_chart_data(ticker: str) -> tuple[dict | None, bytes | None, dict]:
    """Fetch with up to MAX_ATTEMPTS_PER_TICKER attempts. Never retries indefinitely."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    period1 = int(datetime(FETCH_START_DATE.year, FETCH_START_DATE.month, FETCH_START_DATE.day, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime(FETCH_END_DATE_EXCLUSIVE.year, FETCH_END_DATE_EXCLUSIVE.month, FETCH_END_DATE_EXCLUSIVE.day, tzinfo=timezone.utc).timestamp())
    params = {"period1": period1, "period2": period2, "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"}

    attempts_log = []
    for attempt in range(1, MAX_ATTEMPTS_PER_TICKER + 1):
        try:
            response = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            status_code = response.status_code
            if status_code == 429:
                attempts_log.append({"attempt": attempt, "outcome": "rate_limited", "status_code": 429})
            elif status_code != 200:
                attempts_log.append({"attempt": attempt, "outcome": "http_error", "status_code": status_code})
            else:
                data = response.json()
                if data.get("chart", {}).get("error"):
                    attempts_log.append({"attempt": attempt, "outcome": "chart_api_error", "detail": data["chart"]["error"]})
                else:
                    attempts_log.append({"attempt": attempt, "outcome": "success", "status_code": status_code})
                    request_meta = {
                        "url": response.url, "status_code": status_code, "period1": period1, "period2": period2,
                        "fetch_start_date": FETCH_START_DATE.isoformat(),
                        "fetch_end_date_exclusive": FETCH_END_DATE_EXCLUSIVE.isoformat(),
                        "fetched_at_utc": utc_now_iso(), "attempts": attempts_log,
                    }
                    return data, response.content, request_meta
        except requests.RequestException as exc:
            attempts_log.append({"attempt": attempt, "outcome": "exception", "detail": str(exc)})

        if attempt < MAX_ATTEMPTS_PER_TICKER:
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)])

    return None, None, {"attempts": attempts_log, "failed": True}


def save_raw_response(ticker: str, raw_bytes: bytes, request_meta: dict) -> Path:
    raw_dir = DATA_DIR / "market_data" / "raw" / "yahoo" / ticker
    raw_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = raw_dir / f"{ticker.lower()}_chart_raw_{timestamp}.json"
    raw_path.write_bytes(raw_bytes)
    reread = raw_path.read_bytes()
    if reread != raw_bytes:
        raise RuntimeError(f"[{ticker}] Raw response file re-read verification failed -- write did not persist exactly.")
    meta_path = raw_dir / f"{ticker.lower()}_chart_raw_{timestamp}_request_meta.json"
    meta_path.write_text(json.dumps(request_meta, indent=2), encoding="utf-8")
    return raw_path


def parse_chart_result(ticker: str, data: dict) -> dict:
    chart = data.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"[{ticker}] Yahoo chart API returned an error: {chart['error']}")
    results = chart.get("result") or []
    if len(results) != 1:
        raise RuntimeError(f"[{ticker}] Expected exactly 1 chart result, got {len(results)}")
    result = results[0]

    meta = result["meta"]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    adjclose = result["indicators"].get("adjclose", [{}])[0].get("adjclose")
    events = result.get("events", {})
    dividends_raw = events.get("dividends", {})
    splits_raw = events.get("splits", {})

    dividends_by_date = {}
    for _, entry in dividends_raw.items():
        d = datetime.fromtimestamp(entry["date"], tz=timezone.utc).date().isoformat()
        dividends_by_date[d] = entry["amount"]

    splits_by_date = {}
    all_splits = []
    for _, entry in splits_raw.items():
        d = datetime.fromtimestamp(entry["date"], tz=timezone.utc).date().isoformat()
        split_record = {"date": d, "numerator": entry["numerator"], "denominator": entry["denominator"], "split_ratio": entry.get("splitRatio")}
        splits_by_date[d] = split_record
        all_splits.append(split_record)
    all_splits.sort(key=lambda s: s["date"])

    rows = []
    for i, ts in enumerate(timestamps):
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        rows.append({
            "date": d, "open": quote["open"][i], "high": quote["high"][i], "low": quote["low"][i],
            "close": quote["close"][i], "volume": quote["volume"][i],
            "adj_close": adjclose[i] if adjclose is not None else None,
            "dividend": dividends_by_date.get(d), "split_ratio": splits_by_date.get(d, {}).get("split_ratio"),
        })

    return {
        "meta": {k: v for k, v in meta.items() if k not in ("validRanges", "tradingPeriods", "currentTradingPeriod")},
        "rows": rows, "dividends_by_date": dividends_by_date, "splits": all_splits,
    }


def validate_ticker(ticker: str, parsed: dict) -> dict:
    rows = parsed["rows"]
    dates = [r["date"] for r in rows]

    sorted_dates = sorted(dates)
    is_sorted = dates == sorted_dates
    duplicate_dates = sorted({d for d in dates if dates.count(d) > 1})
    is_unique = len(duplicate_dates) == 0

    negative_price_rows, negative_volume_rows = [], []
    for r in rows:
        for field in ("open", "high", "low", "close", "adj_close"):
            value = r[field]
            if value is not None and value < 0:
                negative_price_rows.append({"date": r["date"], "field": field, "value": value})
        if r["volume"] is not None and r["volume"] < 0:
            negative_volume_rows.append({"date": r["date"], "value": r["volume"]})

    missing_fields = []
    for r in rows:
        for field, key in (("open", "open"), ("high", "high"), ("low", "low"), ("close", "close"),
                            ("volume", "volume"), ("adjclose", "adj_close")):
            if r[key] is None:
                missing_fields.append({"date": r["date"], "field": field})

    ohlc_violations = []
    complete_ohlc_rows = 0
    for r in rows:
        if r["open"] is None or r["high"] is None or r["low"] is None or r["close"] is None:
            continue
        complete_ohlc_rows += 1
        if not (r["low"] <= r["open"] <= r["high"]):
            ohlc_violations.append({"date": r["date"], "reason": "low <= open <= high violated", "low": r["low"], "open": r["open"], "high": r["high"]})
        if not (r["low"] <= r["close"] <= r["high"]):
            ohlc_violations.append({"date": r["date"], "reason": "low <= close <= high violated", "low": r["low"], "close": r["close"], "high": r["high"]})

    splits = parsed["splits"]
    close_vs_adjclose_differ_count = sum(
        1 for r in rows if r["close"] is not None and r["adj_close"] is not None and round(r["close"], 6) != round(r["adj_close"], 6)
    )

    is_pass = (
        is_sorted and is_unique and not negative_price_rows and not negative_volume_rows
        and not missing_fields and not ohlc_violations
    )

    result = {
        "ticker": ticker, "status": "PASS" if is_pass else "FAIL",
        "total_observations": len(rows),
        "first_trading_date": dates[0] if dates else None,
        "last_trading_date": dates[-1] if dates else None,
        "dates_sorted": is_sorted, "dates_unique": is_unique, "duplicate_dates": duplicate_dates,
        "negative_price_rows": negative_price_rows, "negative_volume_rows": negative_volume_rows,
        "missing_fields": missing_fields, "missing_fields_count": len(missing_fields),
        "complete_ohlc_rows": complete_ohlc_rows, "ohlc_relationship_violations": ohlc_violations,
        "splits_found": splits, "splits_count": len(splits),
        "dividends_count": len(parsed["dividends_by_date"]), "dividend_dates": sorted(parsed["dividends_by_date"].keys()),
        "close_vs_adjclose_differ_count": close_vs_adjclose_differ_count, "close_and_adjclose_kept_separate": True,
    }

    if ticker == "NVDA":
        found_expected = []
        for expected in NVDA_KNOWN_SPLITS:
            match = next((s for s in splits if s["date"].startswith(str(expected["year"]))
                          and s["numerator"] == expected["numerator"] and s["denominator"] == expected["denominator"]), None)
            found_expected.append({**expected, "found": match is not None})
        result["nvda_known_splits_reproduced"] = all(f["found"] for f in found_expected)
        result["nvda_known_splits_check"] = found_expected

    return result


def analyze_split_semantics(ticker: str, by_date: dict, dates_sorted: list[str], split: dict) -> dict | None:
    split_date = split["date"]
    if split_date not in dates_sorted:
        return None
    idx = dates_sorted.index(split_date)
    if idx == 0 or idx == len(dates_sorted) - 1:
        return None  # split at series boundary -- no before/after pair available
    day_before, day_after = dates_sorted[idx - 1], dates_sorted[idx + 1]
    row_before, row_after = by_date[day_before], by_date[day_after]

    close_before, close_after = float(row_before["close"]), float(row_after["close"])
    adj_before, adj_after = float(row_before["adj_close"]), float(row_after["adj_close"])
    if adj_before == 0 or adj_after == 0:
        return None

    ratio_before = close_before / adj_before
    ratio_after = close_after / adj_after
    documented_multiple = split["numerator"] / split["denominator"] if split["denominator"] else None
    close_before_over_after = close_before / close_after if close_after else None

    close_pct_change = 100.0 * (close_after / close_before - 1.0) if close_before else None
    # If close were on the pre-split scale, close_before/close_after would be
    # near the split multiple (a large jump). If already split-adjusted, it
    # stays an ordinary small daily move instead.
    close_already_split_adjusted = (
        close_before_over_after is not None and documented_multiple
        and abs(close_before_over_after - documented_multiple) > (documented_multiple * 0.5)
    )

    return {
        "split_date": split_date, "split_ratio": split.get("split_ratio"),
        "numerator": split["numerator"], "denominator": split["denominator"],
        "day_before": day_before, "day_after": day_after,
        "close_before": close_before, "close_after": close_after,
        "adj_close_before": adj_before, "adj_close_after": adj_after,
        "close_to_adjclose_ratio_before": round(ratio_before, 6), "close_to_adjclose_ratio_after": round(ratio_after, 6),
        "close_pct_change_before_to_after": round(close_pct_change, 4) if close_pct_change is not None else None,
        "close_already_split_adjusted": bool(close_already_split_adjusted),
        "consistent_with_nvda_finding": bool(close_already_split_adjusted),
    }


def run_split_semantics_for_ticker(ticker: str, rows: list[dict], splits: list[dict]) -> dict:
    if not splits:
        return {"ticker": ticker, "splits_in_period": 0, "test_possible": False,
                "note": "No split-semantics test possible during available period.", "results": []}

    by_date = {r["date"]: r for r in rows}
    dates_sorted = sorted(by_date.keys())
    results = [r for r in (analyze_split_semantics(ticker, by_date, dates_sorted, s) for s in splits) if r is not None]

    return {
        "ticker": ticker, "splits_in_period": len(splits), "test_possible": len(results) > 0,
        "results": results,
        "all_tested_splits_show_close_already_adjusted": all(r["close_already_split_adjusted"] for r in results) if results else None,
    }


def cross_company_validation(all_results: list[dict]) -> dict:
    successful = [r for r in all_results if r["fetch_status"] == "success"]
    counts = {r["ticker"]: r["validation"]["total_observations"] for r in successful}
    first_dates = {r["ticker"]: r["validation"]["first_trading_date"] for r in successful}
    last_dates = {r["ticker"]: r["validation"]["last_trading_date"] for r in successful}

    flags = []
    fetch_failures = [r["ticker"] for r in all_results if r["fetch_status"] != "success"]
    if fetch_failures:
        flags.append(f"Fetch failed for: {fetch_failures}")

    if counts:
        median_count = sorted(counts.values())[len(counts) // 2]
        for ticker, count in counts.items():
            if count < median_count * 0.9:
                flags.append(f"{ticker} has materially fewer dates ({count}) than the group median ({median_count})")

    if first_dates:
        distinct_first = set(first_dates.values())
        if len(distinct_first) > 1:
            flags.append(f"First trading dates differ across tickers: {first_dates}")
        distinct_last = set(last_dates.values())
        if len(distinct_last) > 1:
            flags.append(f"Last trading dates differ across tickers: {last_dates}")

    for r in successful:
        v = r["validation"]
        if v["missing_fields_count"] > 0:
            flags.append(f"{r['ticker']} has {v['missing_fields_count']} missing field(s)")
        if v["ohlc_relationship_violations"]:
            flags.append(f"{r['ticker']} has {len(v['ohlc_relationship_violations'])} OHLC relationship violation(s)")
        if v["status"] != "PASS":
            flags.append(f"{r['ticker']} failed per-ticker validation")

    return {
        "tickers_attempted": len(all_results), "tickers_succeeded": len(successful),
        "observation_counts": counts, "first_trading_dates": first_dates, "last_trading_dates": last_dates,
        "flags": flags, "clean": len(flags) == 0,
    }


def write_outputs(all_results: list[dict], split_semantics_by_ticker: list[dict], cross_validation: dict, runtime: float) -> tuple[str, dict]:
    PROOFS_DIR.mkdir(parents=True, exist_ok=True)

    columns = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "dividend", "split_ratio"]
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for r in all_results:
            if r["fetch_status"] != "success":
                continue
            for row in r["rows"]:
                writer.writerow([r["ticker"]] + [row[c] for c in columns[1:]])

    all_ticker_status_pass = all(r["fetch_status"] == "success" and r["validation"]["status"] == "PASS" for r in all_results)
    overall_status = "PASS" if (all_ticker_status_pass and cross_validation["clean"]) else "FAIL"

    json_output = {
        "status": overall_status, "tickers": TICKERS, "source": "Yahoo Finance historical chart API",
        "fetch_start_date": FETCH_START_DATE.isoformat(), "fetch_end_date_exclusive": FETCH_END_DATE_EXCLUSIVE.isoformat(),
        "per_ticker": [
            {
                "ticker": r["ticker"], "fetch_status": r["fetch_status"], "attempts": r["request_meta"].get("attempts"),
                "raw_response_path": r.get("raw_response_path"),
                "validation": r["validation"],
            }
            for r in all_results
        ],
        "cross_company_validation": cross_validation,
        "split_semantics_by_ticker": split_semantics_by_ticker,
        "note_on_price_methodology": (
            "close and adjusted_close are preserved as separate fields for every ticker. "
            "No final production price-methodology decision has been made in this proof. "
            "Where close was found to be already split-adjusted at the source, it is suitable "
            "for return calculations across split boundaries but must not be treated as the "
            "literal historical dollar price known to an investor before a later split occurred "
            "(look-ahead risk for nominal-price use, not for percentage-return use)."
        ),
        "runtime_seconds": runtime, "generated_at_utc": utc_now_iso(),
    }
    JSON_OUTPUT_PATH.write_text(json.dumps(json_output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    return overall_status, json_output


def main() -> dict:
    start_time = time.perf_counter()
    print("=" * 100)
    print(f"9-TICKER HISTORICAL PRICE PROOF -- {FETCH_START_DATE} to {FETCH_END_DATE_EXCLUSIVE} (exclusive), daily")
    print("=" * 100)

    all_results = []
    split_semantics_by_ticker = []

    for i, ticker in enumerate(TICKERS):
        print(f"\n[{ticker}] fetching...")
        data, raw_bytes, request_meta = fetch_raw_chart_data(ticker)

        if data is None:
            print(f"[{ticker}] FETCH FAILED after {MAX_ATTEMPTS_PER_TICKER} attempts: {request_meta['attempts']}")
            all_results.append({"ticker": ticker, "fetch_status": "failed", "request_meta": request_meta,
                                 "validation": {"status": "FAIL", "total_observations": 0, "first_trading_date": None,
                                                "last_trading_date": None, "missing_fields_count": None,
                                                "ohlc_relationship_violations": []},
                                 "rows": [], "raw_response_path": None})
            split_semantics_by_ticker.append({"ticker": ticker, "splits_in_period": 0, "test_possible": False,
                                               "note": "Fetch failed -- no data to test.", "results": []})
            if i < len(TICKERS) - 1:
                time.sleep(DELAY_BETWEEN_TICKERS_SECONDS)
            continue

        raw_path = save_raw_response(ticker, raw_bytes, request_meta)
        parsed = parse_chart_result(ticker, data)
        validation = validate_ticker(ticker, parsed)
        print(f"[{ticker}] {validation['total_observations']} obs, {validation['first_trading_date']} -> {validation['last_trading_date']}, "
              f"splits={validation['splits_count']}, dividends={validation['dividends_count']}, status={validation['status']}")

        all_results.append({"ticker": ticker, "fetch_status": "success", "request_meta": request_meta,
                             "validation": validation, "rows": parsed["rows"], "raw_response_path": str(raw_path)})

        split_test = run_split_semantics_for_ticker(ticker, parsed["rows"], parsed["splits"])
        split_semantics_by_ticker.append(split_test)
        if split_test["test_possible"]:
            print(f"[{ticker}] split-semantics: {len(split_test['results'])} split(s) tested, "
                  f"all show close already split-adjusted = {split_test['all_tested_splits_show_close_already_adjusted']}")
        else:
            print(f"[{ticker}] {split_test['note']}")

        if i < len(TICKERS) - 1:
            time.sleep(DELAY_BETWEEN_TICKERS_SECONDS)

    cross_validation = cross_company_validation(all_results)
    runtime = round(time.perf_counter() - start_time, 2)
    status, json_output = write_outputs(all_results, split_semantics_by_ticker, cross_validation, runtime)

    print("\n" + "=" * 100)
    print("CROSS-COMPANY VALIDATION")
    print("=" * 100)
    print(f"Tickers attempted: {cross_validation['tickers_attempted']}  Succeeded: {cross_validation['tickers_succeeded']}")
    if cross_validation["flags"]:
        print("Flags:")
        for f in cross_validation["flags"]:
            print(f"  - {f}")
    else:
        print("No cross-company flags.")

    print(f"\nJSON written to {JSON_OUTPUT_PATH}")
    print(f"CSV written to {CSV_OUTPUT_PATH}")
    print(f"\nFINAL: {status}  (runtime {runtime}s)")
    print("=" * 100)

    if status != "PASS":
        raise RuntimeError(f"FAIL -- cross_validation: {cross_validation}")
    return json_output


if __name__ == "__main__":
    main()
