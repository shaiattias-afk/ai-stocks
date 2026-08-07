"""
Read-only historical-price proof, NVDA only. Proves that reliable daily
historical prices and corporate actions (splits, dividends) can be
obtained and validated from Yahoo Finance's historical chart data,
before any full market-price database is built.

Fetches once from Yahoo Finance's public chart API
(https://query1.finance.yahoo.com/v8/finance/chart/NVDA), saves the
exact raw HTTP response byte-for-byte under
data/market_data/raw/yahoo/NVDA/ (git-ignored -- raw market data is
never committed), then parses and validates it.

Does NOT modify any existing database. Does NOT create a production
price table. Does NOT process any ticker other than NVDA. Does NOT run
a backtest. Does NOT decide which price series (raw close vs. adjusted
close) will be used for backtesting -- that decision is explicitly
deferred; both are preserved as separate fields.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DOCS_DIR = PROJECT_DIR / "docs"
RAW_DIR = DATA_DIR / "market_data" / "raw" / "yahoo" / "NVDA"
PROOFS_DIR = DATA_DIR / "proofs"

TICKER = "NVDA"
FETCH_START_DATE = date(2020, 1, 1)
FETCH_END_DATE = date(2026, 8, 7)  # exclusive
YAHOO_CHART_URL = f"https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}"

CSV_OUTPUT_PATH = PROOFS_DIR / "nvda_historical_price_proof.csv"
JSON_OUTPUT_PATH = PROOFS_DIR / "nvda_historical_price_proof.json"

# Known, independently-documented NVDA corporate actions this proof must
# find in the SOURCE data (never invented if the source doesn't return them).
EXPECTED_SPLITS = [
    {"year": 2021, "ratio": "4:1", "numerator": 4.0, "denominator": 1.0},
    {"year": 2024, "ratio": "10:1", "numerator": 10.0, "denominator": 1.0},
]

PRICE_FIELDS = ("open", "high", "low", "close", "adjclose", "volume")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def fetch_raw_chart_data() -> tuple[dict, bytes, dict]:
    period1 = int(datetime(FETCH_START_DATE.year, FETCH_START_DATE.month, FETCH_START_DATE.day, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime(FETCH_END_DATE.year, FETCH_END_DATE.month, FETCH_END_DATE.day, tzinfo=timezone.utc).timestamp())
    params = {
        "period1": period1, "period2": period2, "interval": "1d",
        "events": "div,splits", "includeAdjustedClose": "true",
    }
    response = requests.get(YAHOO_CHART_URL, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    response.raise_for_status()
    request_meta = {
        "url": response.url, "status_code": response.status_code,
        "period1": period1, "period2": period2,
        "fetch_start_date": FETCH_START_DATE.isoformat(), "fetch_end_date_exclusive": FETCH_END_DATE.isoformat(),
        "fetched_at_utc": utc_now_iso(),
    }
    return response.json(), response.content, request_meta


def save_raw_response(raw_bytes: bytes, request_meta: dict) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = RAW_DIR / f"nvda_chart_raw_{timestamp}.json"
    raw_path.write_bytes(raw_bytes)
    reread = raw_path.read_bytes()
    if reread != raw_bytes:
        raise RuntimeError("Raw response file re-read verification failed -- write did not persist exactly.")
    meta_path = RAW_DIR / f"nvda_chart_raw_{timestamp}_request_meta.json"
    meta_path.write_text(json.dumps(request_meta, indent=2), encoding="utf-8")
    return raw_path


def parse_chart_result(data: dict) -> dict:
    chart = data.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"Yahoo chart API returned an error: {chart['error']}")
    results = chart.get("result") or []
    if len(results) != 1:
        raise RuntimeError(f"Expected exactly 1 chart result, got {len(results)}")
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
        split_record = {
            "date": d, "numerator": entry["numerator"], "denominator": entry["denominator"],
            "split_ratio": entry.get("splitRatio"),
        }
        splits_by_date[d] = split_record
        all_splits.append(split_record)
    all_splits.sort(key=lambda s: s["date"])

    rows = []
    for i, ts in enumerate(timestamps):
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        row = {
            "date": d,
            "open": quote["open"][i], "high": quote["high"][i], "low": quote["low"][i],
            "close": quote["close"][i], "volume": quote["volume"][i],
            "adj_close": adjclose[i] if adjclose is not None else None,
            "dividend": dividends_by_date.get(d), "split_ratio": splits_by_date.get(d, {}).get("split_ratio"),
        }
        rows.append(row)

    return {
        "meta": {k: v for k, v in meta.items() if k not in ("validRanges", "tradingPeriods", "currentTradingPeriod")},
        "rows": rows, "dividends_by_date": dividends_by_date, "splits": all_splits,
    }


def validate(parsed: dict) -> dict:
    rows = parsed["rows"]
    dates = [r["date"] for r in rows]

    # 1 & 2. unique and sorted; no duplicate trading dates
    sorted_dates = sorted(dates)
    is_sorted = dates == sorted_dates
    duplicate_dates = sorted({d for d in dates if dates.count(d) > 1})
    is_unique = len(duplicate_dates) == 0

    # 3 & 4. no negative prices / volume (skip None values -- reported separately as missing)
    negative_price_rows = []
    negative_volume_rows = []
    for r in rows:
        for field in ("open", "high", "low", "close", "adj_close"):
            value = r[field]
            if value is not None and value < 0:
                negative_price_rows.append({"date": r["date"], "field": field, "value": value})
        if r["volume"] is not None and r["volume"] < 0:
            negative_volume_rows.append({"date": r["date"], "value": r["volume"]})

    # 6. missing trading-day price fields (any of open/high/low/close/volume/adjclose is None)
    missing_fields = []
    for r in rows:
        for field, key in (("open", "open"), ("high", "high"), ("low", "low"), ("close", "close"),
                            ("volume", "volume"), ("adjclose", "adj_close")):
            if r[key] is None:
                missing_fields.append({"date": r["date"], "field": field})

    # 5. low <= open <= high and low <= close <= high, only for rows with complete OHLC
    ohlc_violations = []
    complete_ohlc_rows = 0
    for r in rows:
        if r["open"] is None or r["high"] is None or r["low"] is None or r["close"] is None:
            continue
        complete_ohlc_rows += 1
        if not (r["low"] <= r["open"] <= r["high"]):
            ohlc_violations.append({"date": r["date"], "reason": "low <= open <= high violated",
                                     "low": r["low"], "open": r["open"], "high": r["high"]})
        if not (r["low"] <= r["close"] <= r["high"]):
            ohlc_violations.append({"date": r["date"], "reason": "low <= close <= high violated",
                                     "low": r["low"], "close": r["close"], "high": r["high"]})

    # 9 & 10. splits
    splits = parsed["splits"]
    found_expected_splits = []
    for expected in EXPECTED_SPLITS:
        match = next((s for s in splits if s["date"].startswith(str(expected["year"]))
                      and s["numerator"] == expected["numerator"] and s["denominator"] == expected["denominator"]), None)
        found_expected_splits.append({**expected, "found": match is not None, "matched_record": match})
    both_expected_splits_found = all(f["found"] for f in found_expected_splits)

    # raw close vs adjusted close difference (informational only -- no decision made)
    close_vs_adjclose_differ_count = sum(
        1 for r in rows if r["close"] is not None and r["adj_close"] is not None and round(r["close"], 6) != round(r["adj_close"], 6)
    )

    return {
        "total_observations": len(rows),
        "first_trading_date": dates[0] if dates else None,
        "last_trading_date": dates[-1] if dates else None,
        "dates_sorted": is_sorted,
        "dates_unique": is_unique,
        "duplicate_dates": duplicate_dates,
        "negative_price_rows": negative_price_rows,
        "negative_volume_rows": negative_volume_rows,
        "missing_fields": missing_fields,
        "missing_fields_count": len(missing_fields),
        "complete_ohlc_rows": complete_ohlc_rows,
        "ohlc_relationship_violations": ohlc_violations,
        "all_splits_found_in_source": splits,
        "expected_splits_check": found_expected_splits,
        "both_expected_splits_found": both_expected_splits_found,
        "dividends_count": len(parsed["dividends_by_date"]),
        "close_vs_adjclose_differ_count": close_vs_adjclose_differ_count,
        "close_and_adjclose_kept_separate": True,  # structural -- see CSV/JSON schema, never merged
    }


def write_outputs(rows: list[dict], validation: dict, request_meta: dict, raw_path: Path) -> tuple[str, dict]:
    PROOFS_DIR.mkdir(parents=True, exist_ok=True)

    columns = ["date", "open", "high", "low", "close", "adj_close", "volume", "dividend", "split_ratio"]
    with CSV_OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for r in rows:
            writer.writerow([r[c] for c in columns])

    overall_status = (
        "PASS" if (
            validation["dates_sorted"] and validation["dates_unique"]
            and not validation["negative_price_rows"] and not validation["negative_volume_rows"]
            and not validation["ohlc_relationship_violations"] and validation["both_expected_splits_found"]
        ) else "FAIL"
    )

    json_output = {
        "status": overall_status, "ticker": TICKER, "source": "Yahoo Finance historical chart API",
        "request": request_meta, "raw_response_path": str(raw_path),
        "fetch_start_date": FETCH_START_DATE.isoformat(), "fetch_end_date_exclusive": FETCH_END_DATE.isoformat(),
        "validation": validation,
        "note_on_price_methodology": (
            "Raw close and adjusted close are preserved as separate fields. "
            "No decision has been made about which will be used for backtesting -- "
            "that decision is explicitly deferred to a separate future task."
        ),
        "generated_at_utc": utc_now_iso(),
    }
    JSON_OUTPUT_PATH.write_text(json.dumps(json_output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    return overall_status, json_output


def main() -> dict:
    start_time = time.perf_counter()
    print("=" * 100)
    print(f"NVDA HISTORICAL PRICE PROOF -- fetching {FETCH_START_DATE} to {FETCH_END_DATE} (exclusive), daily")
    print("=" * 100)

    data, raw_bytes, request_meta = fetch_raw_chart_data()
    raw_path = save_raw_response(raw_bytes, request_meta)
    print(f"Raw response saved: {raw_path} ({len(raw_bytes):,} bytes)")

    parsed = parse_chart_result(data)
    validation = validate(parsed)
    status, json_output = write_outputs(parsed["rows"], validation, request_meta, raw_path)

    runtime = round(time.perf_counter() - start_time, 2)
    print(f"\nTotal daily observations: {validation['total_observations']}")
    print(f"First trading date: {validation['first_trading_date']}  Last trading date: {validation['last_trading_date']}")
    print(f"Dates sorted: {validation['dates_sorted']}  Dates unique: {validation['dates_unique']}")
    print(f"Negative price rows: {len(validation['negative_price_rows'])}  Negative volume rows: {len(validation['negative_volume_rows'])}")
    print(f"Missing fields: {validation['missing_fields_count']}  OHLC violations: {len(validation['ohlc_relationship_violations'])}")
    print(f"Splits found in source: {validation['all_splits_found_in_source']}")
    print(f"Both expected NVDA splits (2021 4:1, 2024 10:1) found: {validation['both_expected_splits_found']}")
    print(f"Dividends found: {validation['dividends_count']}")
    print(f"Rows where close != adj_close: {validation['close_vs_adjclose_differ_count']} of {validation['complete_ohlc_rows']}")
    print(f"\nJSON written to {JSON_OUTPUT_PATH}")
    print(f"CSV written to {CSV_OUTPUT_PATH}")
    print(f"\nFINAL: {status}  (runtime {runtime}s)")
    print("=" * 100)

    json_output["runtime_seconds"] = runtime
    JSON_OUTPUT_PATH.write_text(json.dumps(json_output, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    if status != "PASS":
        raise RuntimeError(f"FAIL -- validation: {validation}")
    return json_output


if __name__ == "__main__":
    main()
