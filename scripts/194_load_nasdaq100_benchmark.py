"""Loads QQQ (the Nasdaq-100 tracking ETF) into `historical_prices_daily`
as a benchmark series, for the excess-return-vs-Nasdaq-100 comparison
the user set as this project stage's goal.

Reuses stock_agent.prices.yahoo (the same, already-proven Historical
Price Policy V1 / D-044 fetch+reconstruct pipeline the 9 approved
tickers were loaded with) unchanged -- same source, same Rule A
(preserve Yahoo's own OHLCV/dividend/split fields), same Rule C
(nominal_* reconstruction for a price an investor could actually have
paid, correcting for splits effective after each date).

QQQ (the ETF), not ^NDX (the raw index), was chosen deliberately: an
index cannot itself be bought, has no dividend, and is not what "excess
return over Nasdaq-100" could concretely mean for this project's actual
buy/hold framing (docs/PROJECT_CONTEXT.md's backtesting section
compares against "S&P 500, Nasdaq-100" as investable benchmarks). QQQ's
own expense ratio and tracking difference are accepted as the honest
cost of a real, investable alternative -- not a reason to prefer an
un-investable index figure that would silently overstate what a real
comparison portfolio could have earned.

    --check-only   fetch + validate, write nothing
    --execute      back up, append through the guard, re-verify read-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import date, datetime, timedelta, timezone

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.prices.yahoo import (
    PRICE_POLICY_VERSION,
    SOURCE_NAME,
    fetch_raw_chart_data,
    parse_chart_result,
    reconstruct_nominal_series,
    save_raw_response,
)
from stock_agent.storage.write_guard import guarded_versioned_append

BENCHMARK_TICKER = "QQQ"
ENGINE_VERSION = "v1-nasdaq100-benchmark (scripts/194)"
FETCH_START_DATE = date(2020, 1, 1)
FETCH_END_DATE_EXCLUSIVE = date.today() + timedelta(days=1)

RESULT_PATH = DATA_DIR / "nasdaq100_benchmark_load_result.json"
BACKUP_DIR = DATA_DIR / "database" / "backups"

TABLE = "historical_prices_daily"
ROW_COLUMNS = [
    "ticker", "price_date", "open", "high", "low", "close", "adj_close",
    "nominal_open", "nominal_high", "nominal_low", "nominal_close", "volume",
    "dividend", "split_ratio", "source", "source_raw_file", "source_raw_sha256",
    "price_policy_version", "engine_version", "loaded_at", "is_active", "created_at",
]


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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

    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    already_present = production.execute(
        "SELECT COUNT(*) FROM historical_prices_daily WHERE ticker = ?", [BENCHMARK_TICKER]
    ).fetchone()[0]
    production.close()
    if already_present:
        raise SystemExit(f"{BENCHMARK_TICKER} already has {already_present} rows in historical_prices_daily -- "
                          f"this script only handles a first-time load, refusing to risk a duplicate.")

    print(f"fetching {BENCHMARK_TICKER} from Yahoo, {FETCH_START_DATE} -> {FETCH_END_DATE_EXCLUSIVE}")
    data, raw_bytes, request_meta = fetch_raw_chart_data(BENCHMARK_TICKER, FETCH_START_DATE, FETCH_END_DATE_EXCLUSIVE)
    if data is None:
        raise SystemExit(f"failed to fetch {BENCHMARK_TICKER}: {request_meta}")

    raw_path = save_raw_response(BENCHMARK_TICKER, raw_bytes, request_meta)
    raw_sha256 = sha256_of_bytes(raw_bytes)

    parsed = parse_chart_result(BENCHMARK_TICKER, data)
    reconstructed = reconstruct_nominal_series(parsed["rows"], parsed["splits"])

    print(f"rows fetched: {len(reconstructed)}")
    print(f"date range: {reconstructed[0]['date']} -> {reconstructed[-1]['date']}")
    print(f"splits found: {parsed['splits']}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        (
            BENCHMARK_TICKER, date.fromisoformat(r["date"]), r["open"], r["high"], r["low"], r["close"], r["adj_close"],
            r["nominal_open"], r["nominal_high"], r["nominal_low"], r["nominal_close"], int(r["volume"]),
            r["dividend"], r["split_ratio"], SOURCE_NAME, str(raw_path), raw_sha256,
            PRICE_POLICY_VERSION, ENGINE_VERSION, now, True, now,
        )
        for r in reconstructed
    ]

    payload = {
        "mode": "check-only" if args.check_only else "execute",
        "ticker": BENCHMARK_TICKER, "rows_fetched": len(rows),
        "date_range": [reconstructed[0]["date"], reconstructed[-1]["date"]],
        "splits_found": parsed["splits"], "raw_path": str(raw_path), "raw_sha256": raw_sha256,
    }

    if args.check_only:
        payload["note"] = "nothing was written"
        RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\nCHECK-ONLY: nothing written. {RESULT_PATH}")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUP_DIR / f"ai_stock_agent_pre_nasdaq100_benchmark_{stamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    if sha256_of_file(PRODUCTION_DB_PATH) != sha256_of_file(backup_path):
        raise SystemExit("backup checksum mismatch -- refusing to write")
    print(f"\nbackup verified: {backup_path.name}")

    result = guarded_versioned_append(PRODUCTION_DB_PATH, TABLE, ROW_COLUMNS, rows, len(rows))

    verify = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    qqq_count = verify.execute("SELECT COUNT(*) FROM historical_prices_daily WHERE ticker = ?", [BENCHMARK_TICKER]).fetchone()[0]
    dup_keys = verify.execute(
        "SELECT COUNT(*) FROM (SELECT ticker, price_date, COUNT(*) c FROM historical_prices_daily GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    verify.close()

    ok = qqq_count == len(rows) and dup_keys == 0
    payload.update({"rows_written": qqq_count, "duplicate_keys": dup_keys,
                    "backup_path": str(backup_path), "guard_result": result,
                    "status": "PASS" if ok else "FAIL"})
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    print(f"written: {RESULT_PATH}")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
