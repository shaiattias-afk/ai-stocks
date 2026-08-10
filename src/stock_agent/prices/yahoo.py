"""prices/yahoo.py — fetch and reconstruct daily prices from Yahoo's
historical chart API.

Ported from `scripts/170_historical_prices_append.py`, which was hard-wired
to the original 9 tickers. The logic is unchanged; it is here so any set of
tickers can use it.

Historical Price Policy V1 (D-044) governs everything below:

  Rule A  Yahoo's own open/high/low/close/adj_close/volume/dividend/
          split_ratio are preserved as separate fields, never overwritten.
  Rule C  A NOMINAL series is reconstructed for execution prices, by
          multiplying each day's OHLC by the product of every split ratio
          effective strictly AFTER that date. Yahoo's `close` is already
          retroactively split-adjusted, so it is NOT the price an investor
          could have paid.

Rule C is why the full date range must be re-fetched rather than just the
tail: a NEW split changes the correct nominal reconstruction for every
older row too.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from stock_agent import DATA_DIR

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
SOURCE_NAME = "Yahoo Finance historical chart API"
PRICE_POLICY_VERSION = "HISTORICAL_PRICE_POLICY_V1"

MAX_ATTEMPTS_PER_TICKER = 3
RETRY_BACKOFF_SECONDS = [2, 5, 10]
REQUEST_TIMEOUT_SECONDS = 30
DELAY_BETWEEN_TICKERS_SECONDS = 1.0

RAW_DIR = DATA_DIR / "market_data" / "raw" / "yahoo"


class YahooPriceError(Exception):
    pass


@dataclass
class TickerPrices:
    ticker: str
    rows: list[dict]
    splits: list[dict]
    dividends_by_date: dict[str, float]
    raw_path: Path | None
    raw_sha256: str | None
    request_meta: dict


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_raw_chart_data(
    ticker: str,
    start: date,
    end_exclusive: date,
) -> tuple[dict | None, bytes | None, dict]:
    """Fetches one ticker's chart, retrying on rate limits and transient
    errors. Returns (data, raw_bytes, request_meta); data is None when
    every attempt failed -- the caller decides what that means, since for
    a delisted company it may be a real fact rather than an error."""
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime(end_exclusive.year, end_exclusive.month, end_exclusive.day,
                           tzinfo=timezone.utc).timestamp())
    params = {"period1": period1, "period2": period2, "interval": "1d",
              "events": "div,splits", "includeAdjustedClose": "true"}

    attempts: list[dict] = []
    for attempt in range(1, MAX_ATTEMPTS_PER_TICKER + 1):
        try:
            response = requests.get(CHART_URL.format(ticker=ticker), params=params,
                                    headers={"User-Agent": "Mozilla/5.0"},
                                    timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code == 429:
                attempts.append({"attempt": attempt, "outcome": "rate_limited", "status_code": 429})
            elif response.status_code != 200:
                attempts.append({"attempt": attempt, "outcome": "http_error",
                                 "status_code": response.status_code})
            else:
                data = response.json()
                if data.get("chart", {}).get("error"):
                    attempts.append({"attempt": attempt, "outcome": "chart_api_error",
                                     "detail": data["chart"]["error"]})
                else:
                    attempts.append({"attempt": attempt, "outcome": "success"})
                    return data, response.content, {
                        "url": response.url, "status_code": response.status_code,
                        "period1": period1, "period2": period2,
                        "fetched_at_utc": _utc_now_iso(), "attempts": attempts,
                    }
        except requests.RequestException as exc:
            attempts.append({"attempt": attempt, "outcome": "exception", "detail": str(exc)})

        if attempt < MAX_ATTEMPTS_PER_TICKER:
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)])

    return None, None, {"attempts": attempts, "failed": True}


def save_raw_response(ticker: str, raw_bytes: bytes, request_meta: dict) -> Path:
    """Keeps the exact bytes Yahoo returned, re-read-verified. The stored
    rows carry this file's name and hash, so any price can be traced to
    the precise response it came from."""
    directory = RAW_DIR / ticker
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = directory / f"{ticker.lower()}_chart_raw_{stamp}.json"
    raw_path.write_bytes(raw_bytes)
    if raw_path.read_bytes() != raw_bytes:
        raise YahooPriceError(f"[{ticker}] raw response did not persist exactly")
    (directory / f"{ticker.lower()}_chart_raw_{stamp}_request_meta.json").write_text(
        json.dumps(request_meta, indent=2), encoding="utf-8")
    return raw_path


def parse_chart_result(ticker: str, data: dict) -> dict:
    chart = data.get("chart", {})
    if chart.get("error"):
        raise YahooPriceError(f"[{ticker}] Yahoo chart API error: {chart['error']}")
    results = chart.get("result") or []
    if len(results) != 1:
        raise YahooPriceError(f"[{ticker}] expected exactly 1 chart result, got {len(results)}")
    result = results[0]

    timestamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    adjclose = result["indicators"].get("adjclose", [{}])[0].get("adjclose")
    events = result.get("events", {})

    dividends_by_date = {
        datetime.fromtimestamp(entry["date"], tz=timezone.utc).date().isoformat(): entry["amount"]
        for entry in (events.get("dividends", {}) or {}).values()
    }

    splits_by_date, all_splits = {}, []
    for entry in (events.get("splits", {}) or {}).values():
        day = datetime.fromtimestamp(entry["date"], tz=timezone.utc).date().isoformat()
        record = {"date": day, "numerator": entry["numerator"],
                  "denominator": entry["denominator"], "split_ratio": entry.get("splitRatio")}
        splits_by_date[day] = record
        all_splits.append(record)
    all_splits.sort(key=lambda s: s["date"])

    rows = []
    for index, stamp in enumerate(timestamps):
        day = datetime.fromtimestamp(stamp, tz=timezone.utc).date().isoformat()
        if quote["open"][index] is None or quote["close"][index] is None:
            continue  # incomplete/half-day row Yahoo returns pre-close; skipped, never guessed
        rows.append({
            "date": day, "open": quote["open"][index], "high": quote["high"][index],
            "low": quote["low"][index], "close": quote["close"][index],
            "volume": quote["volume"][index],
            "adj_close": adjclose[index] if adjclose is not None else None,
            "dividend": dividends_by_date.get(day),
            "split_ratio": splits_by_date.get(day, {}).get("split_ratio"),
        })
    rows.sort(key=lambda r: r["date"])
    return {"rows": rows, "dividends_by_date": dividends_by_date, "splits": all_splits}


def cumulative_future_split_factor(target_date: str, splits: list[dict]) -> float:
    """Product of every split effective strictly AFTER `target_date`.
    A split effective ON the date is not applied to it (D-044 Rule C)."""
    factor = 1.0
    for split in splits:
        if split["date"] > target_date:
            factor *= split["numerator"] / split["denominator"]
    return factor


def reconstruct_nominal_series(rows: list[dict], splits: list[dict]) -> list[dict]:
    """Adds the nominal_* series: the price an investor could actually
    have paid on the day, before later splits rescaled the quoted series."""
    out = []
    for row in rows:
        factor = cumulative_future_split_factor(row["date"], splits)
        out.append({**row,
                    "nominal_open": row["open"] * factor, "nominal_high": row["high"] * factor,
                    "nominal_low": row["low"] * factor, "nominal_close": row["close"] * factor})
    return out


__all__ = [
    "DELAY_BETWEEN_TICKERS_SECONDS", "PRICE_POLICY_VERSION", "SOURCE_NAME",
    "TickerPrices", "YahooPriceError", "cumulative_future_split_factor",
    "fetch_raw_chart_data", "parse_chart_result", "reconstruct_nominal_series",
    "save_raw_response",
]
