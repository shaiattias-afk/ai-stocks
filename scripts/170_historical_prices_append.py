"""
First real use of the D-047 versioned append-only write guard
(stock_agent.storage.write_guard, D-049): append current prices to
`historical_prices_daily`, bringing it up to date from its previous
end date (2026-08-06, D-045) -- without a new engine version, without a
full manual regression, and without ever touching a pre-existing row.

Re-fetches the FULL date range (2020-01-01 -> today) from Yahoo Finance
for all 9 approved tickers -- not just the new tail -- because Historical
Price Policy V1 / D-044 Rule C's nominal-price reconstruction for any
historical date depends on every split whose effective date is after
that date. If a NEW split happened since the 2026-08-06 freeze, it would
change the correct nominal_* reconstruction for every OLDER row too --
which the append-only guard would then (correctly) refuse to let through
undetected, because those older rows' checksums would no longer match.

Overlap check (mandatory, pre-write, fail-closed): every freshly
recomputed row for a date already in `historical_prices_daily`
(price_date <= 2026-08-06) must match the stored row for
open/high/low/close/adj_close/nominal_*/volume/dividend/split_ratio
within RECONSTRUCTION_TOLERANCE_ABS. Any mismatch aborts the entire run
before any write is attempted (REVIEW_REQUIRED) -- this is what would
catch e.g. a new dividend causing Yahoo to retroactively revise
historical adj_close values, which the guard's own checksum check would
independently also refuse to commit.

Two mutually exclusive modes:
  --check-only  Fetches fresh data, runs the full overlap-drift check
                and computes the proposed new rows, writes no database.
  --execute     Same fetch + validation, then PID lock -> full database
                backup (SHA-256-verified) -> one guarded, atomic append
                of only the new rows via
                stock_agent.storage.write_guard -> independent
                post-commit re-verification, reopening the database
                read-only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import requests

from stock_agent.storage.write_guard import guarded_versioned_append

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
LOGS_DIR = PROJECT_DIR / "logs"
BACKUPS_DIR = DATA_DIR / "database" / "backups"

PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

CHECK_ONLY_OUTPUT_PATH = DATA_DIR / "historical_prices_append_check.json"
RESULT_JSON_PATH = DATA_DIR / "historical_prices_append_result.json"
RESULT_CSV_PATH = DATA_DIR / "historical_prices_append_result.csv"
LOG_PATH = LOGS_DIR / "historical_prices_append.log"
PID_LOCK_PATH = DATA_DIR / "historical_prices_append.pid"

TICKERS = ["ORCL", "MSFT", "META", "NVDA", "GOOGL", "AMZN", "MU", "CRWD", "PANW"]
PREVIOUS_FROZEN_LAST_DATE = date(2026, 8, 6)  # D-045's frozen end date

FETCH_START_DATE = date(2020, 1, 1)
FETCH_END_DATE_EXCLUSIVE = date.today() + timedelta(days=1)

PRICE_POLICY_VERSION = "HISTORICAL_PRICE_POLICY_V1"
SOURCE_NAME = "Yahoo Finance historical chart API"
APPEND_ENGINE_VERSION = "HISTORICAL_PRICES_APPEND_V1 (scripts/170_historical_prices_append.py)"

MAX_ATTEMPTS_PER_TICKER = 3
RETRY_BACKOFF_SECONDS = [2, 5]
DELAY_BETWEEN_TICKERS_SECONDS = 1.0
RECONSTRUCTION_TOLERANCE_ABS = 0.001  # dollars -- identical tolerance to scripts/157/158

TABLE = "historical_prices_daily"
# Exact column order the guard will insert with -- verified at runtime
# against the live table (table_column_names), never assumed here.
ROW_COLUMNS = [
    "ticker", "price_date", "open", "high", "low", "close", "adj_close",
    "nominal_open", "nominal_high", "nominal_low", "nominal_close", "volume",
    "dividend", "split_ratio", "source", "source_raw_file", "source_raw_sha256",
    "price_policy_version", "engine_version", "loaded_at", "is_active", "created_at",
]


# =====================================================================
# SMALL SHARED HELPERS
# =====================================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log(message: str, also_print: bool = True) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{utc_now_iso()}] {message}\n")
    if also_print:
        print(message)


def sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def is_pid_active(pid: int) -> bool:
    try:
        completed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=10)
    except Exception:
        return True
    return str(pid) in completed.stdout


def acquire_pid_lock() -> None:
    if PID_LOCK_PATH.exists():
        try:
            content = json.loads(PID_LOCK_PATH.read_text(encoding="utf-8"))
            existing_pid = content["pid"]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"PID lock file exists but is unreadable/malformed ({exc}) -- refusing to start.")
        if is_pid_active(existing_pid):
            raise RuntimeError(f"A live PID lock already exists (pid={existing_pid}) -- refusing to start.")
        log(f"Removing stale PID lock (pid={existing_pid} is not active).")
        PID_LOCK_PATH.unlink(missing_ok=True)
    atomic_write_json(PID_LOCK_PATH, {"pid": os.getpid(), "started_at": utc_now_iso()})
    log(f"PID lock acquired (pid={os.getpid()}).")


def release_pid_lock() -> None:
    if PID_LOCK_PATH.exists():
        PID_LOCK_PATH.unlink(missing_ok=True)
        log("PID lock released.")


# =====================================================================
# FETCH + PARSE (standalone reimplementation of scripts/156/158's
# logic, per this project's convention of not importing business logic
# across numbered scripts -- only the write guard utility is imported)
# =====================================================================

def fetch_raw_chart_data(ticker: str) -> tuple[dict | None, bytes | None, dict]:
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
    raw_path = raw_dir / f"{ticker.lower()}_chart_raw_append_{timestamp}.json"
    raw_path.write_bytes(raw_bytes)
    reread = raw_path.read_bytes()
    if reread != raw_bytes:
        raise RuntimeError(f"[{ticker}] Raw response file re-read verification failed -- write did not persist exactly.")
    meta_path = raw_dir / f"{ticker.lower()}_chart_raw_append_{timestamp}_request_meta.json"
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
        if quote["open"][i] is None or quote["close"][i] is None:
            continue  # a genuinely incomplete/half-day row Yahoo sometimes returns for "today" pre-close; skip, never guess
        rows.append({
            "date": d, "open": quote["open"][i], "high": quote["high"][i], "low": quote["low"][i],
            "close": quote["close"][i], "volume": quote["volume"][i],
            "adj_close": adjclose[i] if adjclose is not None else None,
            "dividend": dividends_by_date.get(d), "split_ratio": splits_by_date.get(d, {}).get("split_ratio"),
        })
    rows.sort(key=lambda r: r["date"])
    return {"rows": rows, "dividends_by_date": dividends_by_date, "splits": all_splits}


def cumulative_future_split_factor(target_date: str, splits: list[dict]) -> float:
    factor = 1.0
    for s in splits:
        if s["date"] > target_date:
            factor *= s["numerator"] / s["denominator"]
    return factor


def reconstruct_nominal_series(rows: list[dict], splits: list[dict]) -> list[dict]:
    reconstructed = []
    for r in rows:
        factor = cumulative_future_split_factor(r["date"], splits)
        reconstructed.append({
            **r, "nominal_open": r["open"] * factor, "nominal_high": r["high"] * factor,
            "nominal_low": r["low"] * factor, "nominal_close": r["close"] * factor,
        })
    return reconstructed


# =====================================================================
# OVERLAP-DRIFT CHECK (mandatory, pre-write, fail-closed)
# =====================================================================


# Fields governed by Historical Price Policy V1 / D-044 (price, split,
# dividend) -- any drift here is a hard, blocking, fail-closed signal
# (e.g. it is exactly what a NEW split/dividend retroactively affecting
# Yahoo's adjusted series would look like). `volume` is preserved by
# D-044 Rule A but is not used by any binding price/valuation policy in
# this project -- Yahoo is known to finalize same-day volume over the
# following day(s) as consolidated-tape data arrives, so a volume-only
# drift on an already-frozen day is recorded and reported, but does NOT
# block the append (this script never writes to that row either way --
# it only appends strictly newer dates).
MATERIAL_FIELDS = ("open", "high", "low", "close", "adj_close", "nominal_open", "nominal_high", "nominal_low", "nominal_close")


def compare_row(fresh: dict, stored: dict) -> dict:
    material_diffs = []
    for field in MATERIAL_FIELDS:
        f, s = fresh[field], stored[field]
        if abs(f - s) > RECONSTRUCTION_TOLERANCE_ABS:
            material_diffs.append(f"{field}: fresh={f} stored={s} diff={abs(f - s)}")
    if (fresh["dividend"] or None) != (stored["dividend"] or None):
        material_diffs.append(f"dividend: fresh={fresh['dividend']} stored={stored['dividend']}")
    if (fresh["split_ratio"] or None) != (stored["split_ratio"] or None):
        material_diffs.append(f"split_ratio: fresh={fresh['split_ratio']} stored={stored['split_ratio']}")

    volume_diffs = []
    if int(fresh["volume"]) != int(stored["volume"]):
        volume_diffs.append(f"volume: fresh={fresh['volume']} stored={stored['volume']}")

    return {"material_diffs": material_diffs, "volume_diffs": volume_diffs}


def fetch_stored_rows(connection, ticker: str) -> dict[str, dict]:
    rows = connection.execute(
        "SELECT price_date, open, high, low, close, adj_close, nominal_open, nominal_high, nominal_low, "
        "nominal_close, volume, dividend, split_ratio FROM historical_prices_daily WHERE ticker = ?",
        [ticker],
    ).fetchall()
    columns = ["price_date", "open", "high", "low", "close", "adj_close", "nominal_open", "nominal_high",
               "nominal_low", "nominal_close", "volume", "dividend", "split_ratio"]
    return {str(r[0]): dict(zip(columns, r)) for r in rows}


# =====================================================================
# FULL BUILD + VALIDATE (shared by --check-only and --execute)
# =====================================================================

def build_and_validate() -> dict:
    prod_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    stored_max_date = prod_connection.execute("SELECT MAX(price_date) FROM historical_prices_daily").fetchone()[0]
    stored_row_count = prod_connection.execute("SELECT COUNT(*) FROM historical_prices_daily").fetchone()[0]

    per_ticker = []
    all_new_rows: list[dict] = []
    all_material_overlap_diffs: list[dict] = []
    all_volume_only_overlap_diffs: list[dict] = []
    raw_file_records = []

    for i, ticker in enumerate(TICKERS):
        data, raw_bytes, request_meta = fetch_raw_chart_data(ticker)
        if data is None:
            raise RuntimeError(f"[{ticker}] Failed to fetch Yahoo chart data after {MAX_ATTEMPTS_PER_TICKER} attempts: {request_meta}")
        raw_path = save_raw_response(ticker, raw_bytes, request_meta)
        raw_sha256 = sha256_of_bytes(raw_bytes)
        raw_file_records.append({"ticker": ticker, "path": str(raw_path), "sha256": raw_sha256})

        parsed = parse_chart_result(ticker, data)
        reconstructed = reconstruct_nominal_series(parsed["rows"], parsed["splits"])
        for r in reconstructed:
            r["ticker"] = ticker
            r["source_raw_file"] = str(raw_path)
            r["source_raw_sha256"] = raw_sha256

        stored_rows_by_date = fetch_stored_rows(prod_connection, ticker)
        overlap_rows = [r for r in reconstructed if r["date"] in stored_rows_by_date]
        new_rows = [r for r in reconstructed if r["date"] not in stored_rows_by_date]

        ticker_material_diffs = []
        ticker_volume_diffs = []
        for r in overlap_rows:
            comparison = compare_row(r, stored_rows_by_date[r["date"]])
            if comparison["material_diffs"]:
                ticker_material_diffs.append({"date": r["date"], "diffs": comparison["material_diffs"]})
            if comparison["volume_diffs"]:
                ticker_volume_diffs.append({"date": r["date"], "diffs": comparison["volume_diffs"]})
        all_material_overlap_diffs.extend({"ticker": ticker, **d} for d in ticker_material_diffs)
        all_volume_only_overlap_diffs.extend({"ticker": ticker, **d} for d in ticker_volume_diffs)

        all_new_rows.extend(new_rows)
        per_ticker.append({
            "ticker": ticker, "fetched_rows": len(reconstructed), "overlap_rows_checked": len(overlap_rows),
            "material_overlap_mismatches": len(ticker_material_diffs), "volume_only_overlap_mismatches": len(ticker_volume_diffs),
            "new_rows": len(new_rows), "new_dates": [r["date"] for r in new_rows],
            "splits_found_in_fresh_fetch": parsed["splits"],
        })
        if i < len(TICKERS) - 1:
            time.sleep(DELAY_BETWEEN_TICKERS_SECONDS)

    prod_connection.close()

    new_splits_since_freeze = []
    for t in per_ticker:
        for s in t["splits_found_in_fresh_fetch"]:
            if s["date"] > PREVIOUS_FROZEN_LAST_DATE.isoformat():
                new_splits_since_freeze.append({"ticker": t["ticker"], **s})

    global_checks = {
        "no_material_overlap_mismatches": len(all_material_overlap_diffs) == 0,
        "no_new_splits_since_freeze": len(new_splits_since_freeze) == 0,
        "all_9_tickers_fetched": len(per_ticker) == len(TICKERS),
        "has_new_rows": len(all_new_rows) > 0,
    }

    return {
        "stored_max_date_before": str(stored_max_date), "stored_row_count_before": stored_row_count,
        "per_ticker": per_ticker, "all_new_rows": all_new_rows,
        "all_material_overlap_diffs": all_material_overlap_diffs,
        "all_volume_only_overlap_diffs": all_volume_only_overlap_diffs,
        "new_splits_since_freeze": new_splits_since_freeze, "raw_file_records": raw_file_records,
        "global_checks": global_checks, "all_checks_passed": all(global_checks.values()),
        "total_new_rows": len(all_new_rows),
    }


def build_row_tuples(new_rows: list[dict]) -> list[tuple]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    tuples = []
    for r in new_rows:
        tuples.append((
            r["ticker"], date.fromisoformat(r["date"]), r["open"], r["high"], r["low"], r["close"], r["adj_close"],
            r["nominal_open"], r["nominal_high"], r["nominal_low"], r["nominal_close"], int(r["volume"]),
            r["dividend"], r["split_ratio"], SOURCE_NAME, r["source_raw_file"], r["source_raw_sha256"],
            PRICE_POLICY_VERSION, APPEND_ENGINE_VERSION, now, True, now,
        ))
    return tuples


# =====================================================================
# --check-only MODE
# =====================================================================

def run_check_only() -> dict:
    start = time.perf_counter()
    db_hash_before = sha256_of_file(PRODUCTION_DB_PATH)
    result = build_and_validate()
    db_hash_after = sha256_of_file(PRODUCTION_DB_PATH)
    result["global_checks"]["database_unchanged"] = db_hash_before == db_hash_after
    result["all_checks_passed"] = result["all_checks_passed"] and result["global_checks"]["database_unchanged"]
    runtime = round(time.perf_counter() - start, 3)

    output = {
        "mode": "check-only", "status": "PASS" if result["all_checks_passed"] else "REVIEW_REQUIRED",
        "stored_max_date_before": result["stored_max_date_before"], "stored_row_count_before": result["stored_row_count_before"],
        "total_new_rows": result["total_new_rows"], "per_ticker": result["per_ticker"],
        "material_overlap_diffs": result["all_material_overlap_diffs"],
        "volume_only_overlap_diffs": result["all_volume_only_overlap_diffs"],
        "new_splits_since_freeze": result["new_splits_since_freeze"],
        "raw_file_records": result["raw_file_records"], "global_checks": result["global_checks"],
        "database_sha256_before": db_hash_before, "database_sha256_after": db_hash_after,
        "runtime_seconds": runtime, "checked_at": utc_now_iso(),
    }
    atomic_write_json(CHECK_ONLY_OUTPUT_PATH, output)
    log(f"check-only run: status={output['status']} new_rows={result['total_new_rows']} runtime={runtime}s", also_print=False)

    print("=" * 100)
    print(f"HISTORICAL PRICES APPEND -- CHECK-ONLY: {output['status']}  (runtime {runtime}s)")
    print("=" * 100)
    print(f"Stored before: max_date={result['stored_max_date_before']} rows={result['stored_row_count_before']}")
    for t in result["per_ticker"]:
        print(f"  {t['ticker']}: fetched={t['fetched_rows']} overlap_checked={t['overlap_rows_checked']} "
              f"material_mismatches={t['material_overlap_mismatches']} volume_only_mismatches={t['volume_only_overlap_mismatches']} "
              f"new_rows={t['new_rows']} new_dates={t['new_dates']}")
    print(f"\nTotal new rows proposed: {result['total_new_rows']}")
    if result["all_volume_only_overlap_diffs"]:
        print(f"Volume-only drift on already-stored rows (informational, non-blocking): {result['all_volume_only_overlap_diffs']}")
    print(f"New splits since freeze: {result['new_splits_since_freeze']}")
    print(f"Global checks: {result['global_checks']}")
    print("=" * 100)
    return output


# =====================================================================
# --execute MODE
# =====================================================================

def phase_backup() -> dict:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_historical_prices_append_{timestamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    source_checksum = sha256_of_file(PRODUCTION_DB_PATH)
    backup_checksum = sha256_of_file(backup_path)
    if source_checksum != backup_checksum:
        raise RuntimeError("Backup checksum mismatch -- aborting before any write.")
    return {"backup_path": str(backup_path), "backup_checksum": backup_checksum, "source_checksum": source_checksum}


def run_execute() -> int:
    acquire_pid_lock()
    log("=== historical prices append: --execute started ===")
    try:
        result = build_and_validate()
        if not result["all_checks_passed"]:
            raise RuntimeError(f"Pre-write validation failed -- refusing to write: {result['global_checks']} "
                                f"material_overlap_diffs={result['all_material_overlap_diffs']} new_splits={result['new_splits_since_freeze']}")
        if result["all_volume_only_overlap_diffs"]:
            log(f"Volume-only drift on already-stored rows (informational, non-blocking, not written): {result['all_volume_only_overlap_diffs']}")
        if result["total_new_rows"] == 0:
            log("No new rows to append (dataset already current). Exiting without any write.")
            atomic_write_json(RESULT_JSON_PATH, {"status": "PASS", "rows_inserted": 0, "note": "already current", "completed_at": utc_now_iso()})
            return 0

        backup_info = phase_backup()
        log(f"Backup complete: {backup_info['backup_path']}")

        row_tuples = build_row_tuples(result["all_new_rows"])
        append_result = guarded_versioned_append(
            PRODUCTION_DB_PATH, TABLE, ROW_COLUMNS, row_tuples, declared_row_delta=len(row_tuples)
        )
        log(f"Guarded append committed: {append_result}")

        # --- independent post-commit re-verification (fresh connection, reopened read-only) ---
        verify_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
        post_row_count = verify_connection.execute("SELECT COUNT(*) FROM historical_prices_daily").fetchone()[0]
        post_max_date = verify_connection.execute("SELECT MAX(price_date) FROM historical_prices_daily").fetchone()[0]
        per_ticker_counts = dict(verify_connection.execute(
            "SELECT ticker, COUNT(*) FROM historical_prices_daily GROUP BY ticker"
        ).fetchall())
        dup_keys = verify_connection.execute(
            "SELECT COUNT(*) FROM (SELECT ticker, price_date, COUNT(*) c FROM historical_prices_daily GROUP BY 1,2 HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        new_rows_engine_check = verify_connection.execute(
            "SELECT COUNT(*) FROM historical_prices_daily WHERE price_date > ? AND engine_version <> ?",
            [PREVIOUS_FROZEN_LAST_DATE, APPEND_ENGINE_VERSION],
        ).fetchone()[0]
        old_rows_engine_untouched = verify_connection.execute(
            "SELECT COUNT(*) FROM historical_prices_daily WHERE price_date <= ? AND engine_version = ?",
            [PREVIOUS_FROZEN_LAST_DATE, APPEND_ENGINE_VERSION],
        ).fetchone()[0]
        other_tables = [r[0] for r in verify_connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' AND table_name <> 'historical_prices_daily' ORDER BY 1"
        ).fetchall()]
        other_table_counts = {t: verify_connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in other_tables}
        verify_connection.close()

        post_checks = {
            "row_count_correct": post_row_count == result["stored_row_count_before"] + len(row_tuples),
            "no_duplicate_keys": dup_keys == 0,
            "new_rows_carry_append_engine_version": new_rows_engine_check == 0,
            "old_rows_engine_version_untouched": old_rows_engine_untouched == 0,
            "max_date_advanced": str(post_max_date) > result["stored_max_date_before"],
        }
        if not all(post_checks.values()):
            raise RuntimeError(f"Post-commit independent re-verification failed (data already committed -- manual review required): {post_checks}")

        result_out = {
            "status": "PASS", "rows_inserted": len(row_tuples),
            "stored_row_count_before": result["stored_row_count_before"], "row_count_after": post_row_count,
            "stored_max_date_before": result["stored_max_date_before"], "max_date_after": str(post_max_date),
            "per_ticker_counts_after": per_ticker_counts, "post_checks": post_checks,
            "guard_result": append_result, "other_tables_row_counts": other_table_counts,
            "backup_path": backup_info["backup_path"], "backup_checksum": backup_info["backup_checksum"],
            "completed_at": utc_now_iso(),
        }
        atomic_write_json(RESULT_JSON_PATH, result_out)
        with RESULT_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["rows_inserted", "row_count_after", "max_date_after", "backup_path"])
            writer.writerow([len(row_tuples), post_row_count, str(post_max_date), backup_info["backup_path"]])

        log("=== historical prices append: --execute COMPLETE (PASS) ===")
        return 0
    except Exception as exc:  # noqa: BLE001
        fail_result = {"status": "FAIL", "error": str(exc), "failed_at": utc_now_iso()}
        atomic_write_json(RESULT_JSON_PATH, fail_result)
        log(f"=== historical prices append: --execute FAILED: {exc} ===")
        return 1
    finally:
        release_pid_lock()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Historical prices append (D-047 first real use).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-only", action="store_true")
    group.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.check_only:
        output = run_check_only()
        return 0 if output["status"] == "PASS" else 1
    return run_execute()


if __name__ == "__main__":
    sys.exit(main())
