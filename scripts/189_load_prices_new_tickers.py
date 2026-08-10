"""Fetches and loads daily prices for companies that have financial data
but no price history yet.

Existing rows are never touched: this only appends tickers absent from
`historical_prices_daily`, through the append-only write guard.

Delisted companies are the real test here. ATVI, ALXN and ANSS were
acquired, so Yahoo may hold a partial series or none at all. A missing or
short series is REPORTED, never padded -- a backtest must see the same
absence an investor would have seen when the company stopped trading.

    --check-only   fetch and validate, write nothing
    --execute      back up, append through the guard, re-verify read-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from datetime import date, datetime, timedelta, timezone

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.prices import yahoo
from stock_agent.storage.write_guard import guarded_versioned_append

ENGINE_VERSION = "v1-prices-new-tickers (scripts/189)"
RESULT_PATH = DATA_DIR / "prices_new_tickers_result.json"
BACKUP_DIR = DATA_DIR / "database" / "backups"

FETCH_START = date(2020, 1, 1)

PRICE_COLUMNS = [
    "ticker", "price_date", "open", "high", "low", "close", "adj_close",
    "nominal_open", "nominal_high", "nominal_low", "nominal_close",
    "volume", "dividend", "split_ratio", "source", "source_raw_file",
    "source_raw_sha256", "price_policy_version", "created_at",
    "engine_version", "loaded_at", "is_active",
]


def sha256_of_file(path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-only", action="store_true")
    group.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    with_financials = [r[0] for r in connection.execute(
        "SELECT DISTINCT ticker FROM companies ORDER BY ticker").fetchall()]
    with_prices = {r[0] for r in connection.execute(
        "SELECT DISTINCT ticker FROM historical_prices_daily").fetchall()}
    rows_before = connection.execute("SELECT COUNT(*) FROM historical_prices_daily").fetchone()[0]
    connection.close()

    todo = [t for t in with_financials if t not in with_prices]
    print("=" * 88)
    print(f"companies with financials : {len(with_financials)}")
    print(f"already have prices       : {len(with_prices)}")
    print(f"to fetch                  : {len(todo)}  {todo}")
    print("=" * 88)
    if not todo:
        print("nothing to do.")
        return

    end_exclusive = date.today() + timedelta(days=1)
    created_at = datetime.now(timezone.utc)

    price_rows: list[tuple] = []
    per_ticker: list[dict] = []

    for index, ticker in enumerate(todo):
        data, raw_bytes, meta = yahoo.fetch_raw_chart_data(ticker, FETCH_START, end_exclusive)
        if data is None:
            per_ticker.append({"ticker": ticker, "status": "NO_DATA", "rows": 0,
                               "reason": "Yahoo returned no usable chart after retries",
                               "attempts": meta.get("attempts")})
            print(f"  {ticker:<6} NO DATA  (Yahoo returned nothing usable)")
            continue

        try:
            parsed = yahoo.parse_chart_result(ticker, data)
        except yahoo.YahooPriceError as exc:
            per_ticker.append({"ticker": ticker, "status": "PARSE_FAILED", "rows": 0,
                               "reason": str(exc)})
            print(f"  {ticker:<6} PARSE FAILED: {exc}")
            continue

        rows = yahoo.reconstruct_nominal_series(parsed["rows"], parsed["splits"])
        if not rows:
            per_ticker.append({"ticker": ticker, "status": "EMPTY_SERIES", "rows": 0,
                               "reason": "chart parsed but contained no complete daily rows"})
            print(f"  {ticker:<6} EMPTY SERIES")
            continue

        raw_path = yahoo.save_raw_response(ticker, raw_bytes, meta) if args.execute else None
        raw_name = raw_path.name if raw_path else "(check-only, not saved)"
        raw_hash = sha256_of_file(raw_path) if raw_path else hashlib.sha256(raw_bytes).hexdigest()

        for row in rows:
            price_rows.append((
                ticker, date.fromisoformat(row["date"]),
                row["open"], row["high"], row["low"], row["close"], row["adj_close"],
                row["nominal_open"], row["nominal_high"], row["nominal_low"], row["nominal_close"],
                int(row["volume"]) if row["volume"] is not None else None,
                row["dividend"], row["split_ratio"],
                yahoo.SOURCE_NAME, raw_name, raw_hash, yahoo.PRICE_POLICY_VERSION,
                created_at, ENGINE_VERSION, created_at, True,
            ))

        per_ticker.append({"ticker": ticker, "status": "OK", "rows": len(rows),
                           "first_date": rows[0]["date"], "last_date": rows[-1]["date"],
                           "splits": len(parsed["splits"]),
                           "dividends": len(parsed["dividends_by_date"])})
        print(f"  {ticker:<6} {len(rows):>5} rows  {rows[0]['date']} -> {rows[-1]['date']}"
              f"   splits={len(parsed['splits'])} dividends={len(parsed['dividends_by_date'])}")

        if index < len(todo) - 1:
            time.sleep(yahoo.DELAY_BETWEEN_TICKERS_SECONDS)

    ok = [p for p in per_ticker if p["status"] == "OK"]
    problems = [p for p in per_ticker if p["status"] != "OK"]
    print()
    print(f"tickers with data : {len(ok)} / {len(todo)}")
    print(f"total price rows  : {len(price_rows)}")
    if problems:
        print("\nno usable price series:")
        for p in problems:
            print(f"   {p['ticker']:<6} {p['status']}: {p.get('reason')}")

    payload = {"mode": "check-only" if args.check_only else "execute",
               "tickers_requested": todo, "per_ticker": per_ticker,
               "price_rows": len(price_rows), "rows_before": rows_before,
               "engine_version": ENGINE_VERSION}

    if args.check_only:
        payload["note"] = "nothing written"
        RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\nCHECK-ONLY: nothing written. {RESULT_PATH}")
        return

    if not price_rows:
        print("no rows to load.")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"ai_stock_agent_pre_new_ticker_prices_{stamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    if sha256_of_file(PRODUCTION_DB_PATH) != sha256_of_file(backup_path):
        raise SystemExit("backup checksum mismatch -- refusing to write")
    print(f"\nbackup verified: {backup_path.name}")

    guard = guarded_versioned_append(
        PRODUCTION_DB_PATH, "historical_prices_daily", PRICE_COLUMNS,
        price_rows, len(price_rows))

    verify = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    rows_after = verify.execute("SELECT COUNT(*) FROM historical_prices_daily").fetchone()[0]
    tickers_after = verify.execute(
        "SELECT COUNT(DISTINCT ticker) FROM historical_prices_daily").fetchone()[0]
    # Count pre-existing rows by LOAD TIMESTAMP, not engine_version.
    # engine_version is constant across every run of this script, so a
    # second run counts the FIRST run's rows as its own and reports a
    # false failure on a perfectly good load. (Measured: after the first
    # run's 17,677 rows this read 32,599 - 17,677 = 14,922 and called it
    # FAIL.) The write guard is what actually proves pre-existing rows are
    # unchanged, by checksumming them before and after the insert.
    original_intact = verify.execute(
        "SELECT COUNT(*) FROM historical_prices_daily "
        "WHERE loaded_at IS NULL OR loaded_at < ?", [created_at]).fetchone()[0]
    verify.close()

    print(f"\nprice rows : {rows_before} -> {rows_after} (+{rows_after - rows_before})")
    print(f"tickers    : {len(with_prices)} -> {tickers_after}")
    print(f"pre-existing rows intact : {original_intact} (expected {rows_before})")

    passed = (rows_after - rows_before == len(price_rows) and original_intact == rows_before)
    payload.update({"rows_after": rows_after, "tickers_after": tickers_after,
                    "pre_existing_intact": original_intact == rows_before,
                    "backup_path": str(backup_path), "guard": guard,
                    "status": "PASS" if passed else "FAIL"})
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\nRESULT:", "PASS" if passed else "FAIL")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
