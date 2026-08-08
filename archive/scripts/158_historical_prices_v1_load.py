"""
Historical Prices V1 -- production build/load for all 9 approved
tickers (ORCL, MSFT, META, NVDA, GOOGL, AMZN, MU, CRWD, PANW), applying
Historical Price Policy V1 / D-044 (docs/DECISIONS_LOG.md,
docs/PRICE_POLICY_V1.md) exactly:

  Rule A -- preserve Yahoo's original open/high/low/close/adj_close/
            volume/dividend/split_ratio fields separately, never
            overwritten.
  Rule C -- also compute the reconstructed historical nominal
            open/high/low/close (Yahoo OHLC x product of every split
            ratio whose effective date is strictly AFTER the price
            date; a split effective ON the date does not apply to that
            date).

Rebuilds the complete 9-company dataset FROM SCRATCH by re-parsing the
already-saved raw Yahoo JSON responses (the same files identified as
authoritative by data/proofs/9_ticker_historical_price_proof.json) --
never reads the previously-computed proof CSVs as a shortcut, and never
downloads anything new.

Two mutually exclusive modes:

  --check-only  Fully read-only. Rebuilds and validates the complete
                14,913-row dataset, cross-checks it against the
                existing 9-ticker proof (splits/dividends) and the
                Historical Price Policy V1 proof (NVDA/GOOGL/PANW
                reconstructed values), and proves the exact future
                production schema/insert/commit path against an
                in-memory (':memory:') DuckDB database only. Creates no
                table, backup, or PID lock; writes nothing to any file
                database.

  --execute     The real production load (built fully, NEVER invoked by
                this task): PID lock -> preconditions -> backup
                (SHA-256-verified) -> full dataset rebuilt and
                validated again -> one atomic transaction that
                (re)creates `historical_prices_daily` and inserts every
                validated row -> post-commit validation -> result
                written -> exit code 0 only if every post-commit check
                passes.
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
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DOCS_DIR = PROJECT_DIR / "docs"
LOGS_DIR = PROJECT_DIR / "logs"
BACKUPS_DIR = DATA_DIR / "database" / "backups"
PROOFS_DIR = DATA_DIR / "proofs"

PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
NINE_TICKER_PROOF_JSON_PATH = PROOFS_DIR / "9_ticker_historical_price_proof.json"
PRICE_POLICY_PROOF_JSON_PATH = PROOFS_DIR / "price_policy_v1_proof.json"

CHECK_ONLY_OUTPUT_PATH = DATA_DIR / "historical_prices_v1_build_validation.json"
PREVIEW_CSV_PATH = DATA_DIR / "historical_prices_v1_preview.csv"
RESULT_JSON_PATH = DATA_DIR / "historical_prices_v1_load_result.json"
RESULT_CSV_PATH = DATA_DIR / "historical_prices_v1_load_result.csv"
RELEASE_MANIFEST_PATH = DATA_DIR / "historical_prices_v1_release_manifest.json"
LOG_PATH = LOGS_DIR / "historical_prices_v1_load.log"
PID_LOCK_PATH = DATA_DIR / "historical_prices_v1_load.pid"

TICKERS = ["ORCL", "MSFT", "META", "NVDA", "GOOGL", "AMZN", "MU", "CRWD", "PANW"]
EXPECTED_TICKER_COUNT = 9
EXPECTED_ROWS_PER_TICKER = 1657
EXPECTED_TOTAL_ROWS = EXPECTED_TICKER_COUNT * EXPECTED_ROWS_PER_TICKER  # 14,913

PRICE_POLICY_VERSION = "HISTORICAL_PRICE_POLICY_V1"
SOURCE_NAME = "Yahoo Finance historical chart API"
POLICY_TICKERS_TO_CROSS_CHECK = ("NVDA", "GOOGL", "PANW")  # the 3 companies D-044 was proven against
RECONSTRUCTION_TOLERANCE_ABS = 0.001  # dollars -- tight, but allows for float rounding across two independent computations

HISTORICAL_PRICES_DAILY_DDL = """
CREATE TABLE historical_prices_daily (
    ticker                  VARCHAR NOT NULL,
    price_date              DATE NOT NULL,
    open                    DOUBLE NOT NULL,
    high                    DOUBLE NOT NULL,
    low                     DOUBLE NOT NULL,
    close                   DOUBLE NOT NULL,
    adj_close               DOUBLE NOT NULL,
    nominal_open            DOUBLE NOT NULL,
    nominal_high            DOUBLE NOT NULL,
    nominal_low             DOUBLE NOT NULL,
    nominal_close           DOUBLE NOT NULL,
    volume                  BIGINT NOT NULL,
    dividend                DOUBLE,
    split_ratio             VARCHAR,
    source                  VARCHAR NOT NULL,
    source_raw_file         VARCHAR NOT NULL,
    source_raw_sha256       VARCHAR NOT NULL,
    price_policy_version    VARCHAR NOT NULL,
    created_at              TIMESTAMP NOT NULL,
    CONSTRAINT chk_prices_positive CHECK (
        open > 0 AND high > 0 AND low > 0 AND close > 0 AND adj_close > 0
        AND nominal_open > 0 AND nominal_high > 0 AND nominal_low > 0 AND nominal_close > 0
    ),
    CONSTRAINT chk_volume_non_negative CHECK (volume >= 0),
    CONSTRAINT chk_ohlc_valid CHECK (low <= open AND open <= high AND low <= close AND close <= high),
    CONSTRAINT chk_nominal_ohlc_valid CHECK (
        nominal_low <= nominal_open AND nominal_open <= nominal_high
        AND nominal_low <= nominal_close AND nominal_close <= nominal_high
    ),
    CONSTRAINT chk_price_policy_version CHECK (price_policy_version = 'HISTORICAL_PRICE_POLICY_V1'),
    PRIMARY KEY (ticker, price_date)
)
"""


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


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


# =====================================================================
# PID LOCK (same discipline as scripts/151 / scripts/153, reimplemented
# here since this is a distinct domain -- not imported)
# =====================================================================

def is_pid_active(pid: int) -> bool:
    try:
        completed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=10)
    except Exception:
        return True
    return str(pid) in completed.stdout


def check_pid_lock_status(exclude_pid: int | None = None) -> dict:
    if not PID_LOCK_PATH.exists():
        return {"is_free": True, "existing_pid": None, "pid_active": None, "is_own_lock": False, "is_stale": False}
    try:
        content = json.loads(PID_LOCK_PATH.read_text(encoding="utf-8"))
        existing_pid = content["pid"]
        if not isinstance(existing_pid, int) or isinstance(existing_pid, bool):
            raise ValueError(f"'pid' field is not an integer: {existing_pid!r}")
    except Exception as exc:  # noqa: BLE001
        return {"is_free": False, "existing_pid": None, "pid_active": None, "is_own_lock": False, "is_stale": False,
                "reason": f"PID lock file unreadable/malformed: {exc}"}
    if exclude_pid is not None and existing_pid == exclude_pid:
        return {"is_free": True, "existing_pid": existing_pid, "pid_active": True, "is_own_lock": True, "is_stale": False}
    active = is_pid_active(existing_pid)
    return {"is_free": not active, "existing_pid": existing_pid, "pid_active": active, "is_own_lock": False, "is_stale": not active}


def acquire_pid_lock() -> None:
    status = check_pid_lock_status()
    if status["existing_pid"] is None and not status["is_free"]:
        raise RuntimeError(f"PID lock file exists but is malformed/unreadable ({status.get('reason')}) -- refusing to start.")
    if status["existing_pid"] is not None and status["pid_active"]:
        raise RuntimeError(f"A live PID lock already exists (pid={status['existing_pid']}) -- refusing to start.")
    if status["existing_pid"] is not None and status["is_stale"]:
        log(f"Removing stale PID lock (pid={status['existing_pid']} is not active).")
        PID_LOCK_PATH.unlink(missing_ok=True)
    atomic_write_json(PID_LOCK_PATH, {"pid": os.getpid(), "started_at": utc_now_iso()})
    log(f"PID lock acquired (pid={os.getpid()}).")


def release_pid_lock() -> None:
    if PID_LOCK_PATH.exists():
        PID_LOCK_PATH.unlink(missing_ok=True)
        log("PID lock released.")


# =====================================================================
# RAW SOURCE MANIFEST -- which raw Yahoo file is authoritative per
# ticker, taken from the already-verified 9-ticker proof (never
# re-downloaded, never guessed)
# =====================================================================

def load_raw_source_manifest() -> dict[str, dict]:
    if not NINE_TICKER_PROOF_JSON_PATH.exists():
        raise RuntimeError(f"Required prior proof not found: {NINE_TICKER_PROOF_JSON_PATH}. Run scripts/156 first.")
    proof = json.loads(NINE_TICKER_PROOF_JSON_PATH.read_text(encoding="utf-8"))
    manifest = {}
    for entry in proof["per_ticker"]:
        ticker = entry["ticker"]
        if ticker not in TICKERS:
            continue
        raw_path = Path(entry["raw_response_path"])
        if not raw_path.exists():
            raise RuntimeError(f"[{ticker}] raw source file recorded in prior proof does not exist: {raw_path}")
        manifest[ticker] = {
            "raw_response_path": raw_path,
            "prior_proof_validation": entry["validation"],
        }
    missing = set(TICKERS) - set(manifest.keys())
    if missing:
        raise RuntimeError(f"Prior proof is missing raw-file records for: {sorted(missing)}")
    return manifest


# =====================================================================
# PARSE RAW YAHOO JSON FROM SCRATCH (same logic as scripts/154/156,
# reimplemented standalone here -- this script does not import from
# other numbered scripts)
# =====================================================================

def parse_chart_result(ticker: str, raw_json_text: str) -> dict:
    data = json.loads(raw_json_text)
    chart = data.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"[{ticker}] Yahoo chart API raw response contains an error: {chart['error']}")
    results = chart.get("result") or []
    if len(results) != 1:
        raise RuntimeError(f"[{ticker}] Expected exactly 1 chart result in raw file, got {len(results)}")
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
        rows.append({
            "date": d, "open": quote["open"][i], "high": quote["high"][i], "low": quote["low"][i],
            "close": quote["close"][i], "volume": quote["volume"][i],
            "adj_close": adjclose[i] if adjclose is not None else None,
            "dividend": dividends_by_date.get(d), "split_ratio": splits_by_date.get(d, {}).get("split_ratio"),
        })
    rows.sort(key=lambda r: r["date"])

    return {"rows": rows, "dividends_by_date": dividends_by_date, "splits": all_splits}


# =====================================================================
# RULE C -- NOMINAL PRICE RECONSTRUCTION (identical formula to
# scripts/157, reimplemented standalone here)
# =====================================================================

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
            **r, "cumulative_future_split_factor": factor,
            "nominal_open": r["open"] * factor, "nominal_high": r["high"] * factor,
            "nominal_low": r["low"] * factor, "nominal_close": r["close"] * factor,
        })
    return reconstructed


# =====================================================================
# PER-TICKER VALIDATION (rebuilt from scratch)
# =====================================================================

def validate_ticker_dataset(ticker: str, reconstructed: list[dict]) -> dict:
    dates = [r["date"] for r in reconstructed]
    sorted_dates = sorted(dates)
    dates_sorted = dates == sorted_dates
    duplicate_dates = sorted({d for d in dates if dates.count(d) > 1})
    dates_unique = len(duplicate_dates) == 0

    missing_fields = []
    negative_price_rows = []
    negative_volume_rows = []
    ohlc_violations = []
    nominal_non_positive = []
    nominal_ohlc_violations = []

    for r in reconstructed:
        for field in ("open", "high", "low", "close", "adj_close"):
            if r[field] is None:
                missing_fields.append({"date": r["date"], "field": field})
            elif r[field] < 0:
                negative_price_rows.append({"date": r["date"], "field": field, "value": r[field]})
        if r["volume"] is None:
            missing_fields.append({"date": r["date"], "field": "volume"})
        elif r["volume"] < 0:
            negative_volume_rows.append({"date": r["date"], "value": r["volume"]})

        if r["open"] is not None and r["high"] is not None and r["low"] is not None and r["close"] is not None:
            if not (r["low"] <= r["open"] <= r["high"]):
                ohlc_violations.append({"date": r["date"], "reason": "low <= open <= high violated"})
            if not (r["low"] <= r["close"] <= r["high"]):
                ohlc_violations.append({"date": r["date"], "reason": "low <= close <= high violated"})

        for field in ("nominal_open", "nominal_high", "nominal_low", "nominal_close"):
            if r[field] <= 0:
                nominal_non_positive.append({"date": r["date"], "field": field, "value": r[field]})
        if not (r["nominal_low"] <= r["nominal_open"] <= r["nominal_high"]):
            nominal_ohlc_violations.append({"date": r["date"], "reason": "nominal_low <= nominal_open <= nominal_high violated"})
        if not (r["nominal_low"] <= r["nominal_close"] <= r["nominal_high"]):
            nominal_ohlc_violations.append({"date": r["date"], "reason": "nominal_low <= nominal_close <= nominal_high violated"})

    return {
        "ticker": ticker, "row_count": len(reconstructed),
        "row_count_matches_expected": len(reconstructed) == EXPECTED_ROWS_PER_TICKER,
        "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None,
        "dates_sorted": dates_sorted, "dates_unique": dates_unique, "duplicate_dates": duplicate_dates,
        "missing_fields": missing_fields, "negative_price_rows": negative_price_rows,
        "negative_volume_rows": negative_volume_rows, "ohlc_violations": ohlc_violations,
        "nominal_non_positive": nominal_non_positive, "nominal_ohlc_violations": nominal_ohlc_violations,
        "clean": (
            dates_sorted and dates_unique and not missing_fields and not negative_price_rows
            and not negative_volume_rows and not ohlc_violations and not nominal_non_positive
            and not nominal_ohlc_violations and len(reconstructed) == EXPECTED_ROWS_PER_TICKER
        ),
    }


def cross_check_against_9_ticker_proof(ticker: str, splits: list[dict], dividends_by_date: dict, prior_validation: dict) -> dict:
    prior_splits = prior_validation["splits_found"]
    splits_match = (
        sorted([(s["date"], s["numerator"], s["denominator"]) for s in splits])
        == sorted([(s["date"], s["numerator"], s["denominator"]) for s in prior_splits])
    )
    dividends_match = len(dividends_by_date) == prior_validation["dividends_count"]
    return {"ticker": ticker, "splits_match_prior_proof": splits_match, "dividends_count_match_prior_proof": dividends_match,
            "rebuilt_splits_count": len(splits), "prior_proof_splits_count": len(prior_splits),
            "rebuilt_dividends_count": len(dividends_by_date), "prior_proof_dividends_count": prior_validation["dividends_count"]}


def cross_check_against_price_policy_proof(all_reconstructed: dict[str, list[dict]]) -> dict:
    if not PRICE_POLICY_PROOF_JSON_PATH.exists():
        raise RuntimeError(f"Required prior proof not found: {PRICE_POLICY_PROOF_JSON_PATH}. Run scripts/157 first.")
    proof = json.loads(PRICE_POLICY_PROOF_JSON_PATH.read_text(encoding="utf-8"))

    mismatches = []
    checked = 0
    for window in proof["split_window_analyses"]:
        ticker = window["ticker"]
        if ticker not in POLICY_TICKERS_TO_CROSS_CHECK:
            continue
        by_date = {r["date"]: r for r in all_reconstructed[ticker]}
        for row in window["window_table"]:
            checked += 1
            rebuilt = by_date.get(row["date"])
            if rebuilt is None:
                mismatches.append({"ticker": ticker, "date": row["date"], "reason": "date not found in rebuilt dataset"})
                continue
            diff = abs(rebuilt["nominal_close"] - row["reconstructed_nominal_close"])
            if diff > RECONSTRUCTION_TOLERANCE_ABS:
                mismatches.append({"ticker": ticker, "date": row["date"], "rebuilt_nominal_close": rebuilt["nominal_close"],
                                    "proof_nominal_close": row["reconstructed_nominal_close"], "diff": diff})
    return {"tickers_checked": list(POLICY_TICKERS_TO_CROSS_CHECK), "dates_checked": checked,
            "mismatches": mismatches, "matches_price_policy_v1_proof": len(mismatches) == 0}


# =====================================================================
# FULL DATASET BUILD
# =====================================================================

def build_full_dataset() -> dict:
    manifest = load_raw_source_manifest()

    all_reconstructed: dict[str, list[dict]] = {}
    per_ticker_validation = []
    prior_proof_cross_checks = []
    raw_file_records = []

    for ticker in TICKERS:
        raw_path = manifest[ticker]["raw_response_path"]
        raw_text = raw_path.read_text(encoding="utf-8")
        raw_sha256 = sha256_of_file(raw_path)
        raw_file_records.append({"ticker": ticker, "path": str(raw_path), "sha256": raw_sha256, "exists": True})

        parsed = parse_chart_result(ticker, raw_text)
        reconstructed = reconstruct_nominal_series(parsed["rows"], parsed["splits"])
        for r in reconstructed:
            r["ticker"] = ticker
            r["source_raw_file"] = str(raw_path)
            r["source_raw_sha256"] = raw_sha256
        all_reconstructed[ticker] = reconstructed

        validation = validate_ticker_dataset(ticker, reconstructed)
        per_ticker_validation.append(validation)

        prior_proof_cross_checks.append(cross_check_against_9_ticker_proof(
            ticker, parsed["splits"], parsed["dividends_by_date"], manifest[ticker]["prior_proof_validation"]))

    policy_cross_check = cross_check_against_price_policy_proof(all_reconstructed)

    all_rows = [r for ticker in TICKERS for r in all_reconstructed[ticker]]
    total_rows = len(all_rows)
    distinct_tickers = sorted({r["ticker"] for r in all_rows})
    unexpected_tickers = sorted(set(distinct_tickers) - set(TICKERS))
    key_set = {(r["ticker"], r["date"]) for r in all_rows}
    duplicate_key_count = total_rows - len(key_set)

    global_checks = {
        "exactly_9_tickers": len(distinct_tickers) == EXPECTED_TICKER_COUNT,
        "no_unexpected_ticker": len(unexpected_tickers) == 0,
        "exactly_1657_rows_per_ticker": all(v["row_count_matches_expected"] for v in per_ticker_validation),
        "exactly_14913_total_rows": total_rows == EXPECTED_TOTAL_ROWS,
        "no_duplicate_ticker_date_rows": duplicate_key_count == 0,
        "all_per_ticker_validation_clean": all(v["clean"] for v in per_ticker_validation),
        "every_raw_source_file_exists": all(rec["exists"] for rec in raw_file_records),
        "every_raw_source_file_sha256_recorded": all(bool(rec["sha256"]) for rec in raw_file_records),
        "all_split_events_match_9_ticker_proof": all(c["splits_match_prior_proof"] for c in prior_proof_cross_checks),
        "all_dividend_counts_match_9_ticker_proof": all(c["dividends_count_match_prior_proof"] for c in prior_proof_cross_checks),
        "nvda_googl_panw_match_price_policy_v1_proof": policy_cross_check["matches_price_policy_v1_proof"],
    }

    return {
        "all_rows": all_rows, "all_reconstructed": all_reconstructed,
        "per_ticker_validation": per_ticker_validation, "prior_proof_cross_checks": prior_proof_cross_checks,
        "policy_cross_check": policy_cross_check, "raw_file_records": raw_file_records,
        "distinct_tickers": distinct_tickers, "total_rows": total_rows,
        "global_checks": global_checks,
    }


# =====================================================================
# IN-MEMORY LOAD PROOF (exact future production schema, ':memory:' only)
# =====================================================================

def run_in_memory_load_proof(all_rows: list[dict]) -> dict:
    connection = duckdb.connect(database=":memory:")
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.execute(HISTORICAL_PRICES_DAILY_DDL)
        for r in all_rows:
            connection.execute(
                "INSERT INTO historical_prices_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [r["ticker"], date.fromisoformat(r["date"]), r["open"], r["high"], r["low"], r["close"], r["adj_close"],
                 r["nominal_open"], r["nominal_high"], r["nominal_low"], r["nominal_close"], int(r["volume"]),
                 r["dividend"], r["split_ratio"], SOURCE_NAME, r["source_raw_file"], r["source_raw_sha256"],
                 PRICE_POLICY_VERSION, created_at],
            )
        connection.execute("COMMIT")
        commit_succeeded = True
    except Exception as exc:  # noqa: BLE001
        connection.execute("ROLLBACK")
        connection.close()
        return {"commit_succeeded": False, "error": str(exc)}

    row_count = connection.execute("SELECT COUNT(*) FROM historical_prices_daily").fetchone()[0]
    distinct_tickers = connection.execute("SELECT COUNT(DISTINCT ticker) FROM historical_prices_daily").fetchone()[0]
    per_ticker_counts = dict(connection.execute("SELECT ticker, COUNT(*) FROM historical_prices_daily GROUP BY ticker").fetchall())
    dup_keys = connection.execute(
        "SELECT COUNT(*) FROM (SELECT ticker, price_date, COUNT(*) c FROM historical_prices_daily GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    null_required_fields = connection.execute(
        "SELECT COUNT(*) FROM historical_prices_daily WHERE ticker IS NULL OR price_date IS NULL OR open IS NULL "
        "OR high IS NULL OR low IS NULL OR close IS NULL OR adj_close IS NULL OR nominal_open IS NULL "
        "OR nominal_high IS NULL OR nominal_low IS NULL OR nominal_close IS NULL OR volume IS NULL "
        "OR source IS NULL OR source_raw_file IS NULL OR source_raw_sha256 IS NULL "
        "OR price_policy_version IS NULL OR created_at IS NULL"
    ).fetchone()[0]
    wrong_policy_version = connection.execute(
        "SELECT COUNT(*) FROM historical_prices_daily WHERE price_policy_version <> ?", [PRICE_POLICY_VERSION]
    ).fetchone()[0]
    connection.close()

    all_1657 = all(per_ticker_counts.get(t) == EXPECTED_ROWS_PER_TICKER for t in TICKERS)

    return {
        "commit_succeeded": commit_succeeded, "rows_inserted": row_count,
        "distinct_tickers": distinct_tickers, "per_ticker_counts": per_ticker_counts,
        "all_tickers_have_1657_rows": all_1657, "duplicate_keys": dup_keys,
        "required_fields_null_count": null_required_fields, "wrong_policy_version_count": wrong_policy_version,
        "proof_passed": (
            commit_succeeded and row_count == EXPECTED_TOTAL_ROWS and distinct_tickers == EXPECTED_TICKER_COUNT
            and all_1657 and dup_keys == 0 and null_required_fields == 0 and wrong_policy_version == 0
        ),
        "used_file_database": False,
    }


# =====================================================================
# --check-only MODE
# =====================================================================

def write_preview_csv(all_rows: list[dict]) -> None:
    columns = ["ticker", "date", "open", "high", "low", "close", "adj_close", "nominal_open", "nominal_high",
               "nominal_low", "nominal_close", "volume", "dividend", "split_ratio", "source_raw_sha256"]
    with PREVIEW_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for r in sorted(all_rows, key=lambda x: (x["ticker"], x["date"])):
            writer.writerow([r.get(c) for c in columns])


def run_check_only() -> dict:
    start = time.perf_counter()
    db_hash_before = sha256_of_file(PRODUCTION_DB_PATH) if PRODUCTION_DB_PATH.exists() else None

    table_already_exists = False
    if PRODUCTION_DB_PATH.exists():
        connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
        table_already_exists = bool(connection.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'historical_prices_daily'"
        ).fetchone()[0])
        connection.close()

    dataset = build_full_dataset()
    in_memory_proof = run_in_memory_load_proof(dataset["all_rows"])
    dataset["global_checks"]["in_memory_load_proof_passed"] = in_memory_proof["proof_passed"]
    dataset["global_checks"]["production_table_does_not_yet_exist"] = not table_already_exists

    db_hash_after = sha256_of_file(PRODUCTION_DB_PATH) if PRODUCTION_DB_PATH.exists() else None
    dataset["global_checks"]["database_unchanged"] = db_hash_before == db_hash_after

    overall = all(dataset["global_checks"].values())
    runtime = round(time.perf_counter() - start, 3)

    output = {
        "mode": "check-only", "status": "PASS" if overall else "FAIL",
        "tickers_processed": dataset["distinct_tickers"], "total_rows": dataset["total_rows"],
        "per_ticker_validation": dataset["per_ticker_validation"],
        "prior_proof_cross_checks": dataset["prior_proof_cross_checks"],
        "policy_cross_check": dataset["policy_cross_check"],
        "raw_file_records": dataset["raw_file_records"],
        "global_checks": dataset["global_checks"],
        "in_memory_load_proof": in_memory_proof,
        "expected_production_table_ddl": HISTORICAL_PRICES_DAILY_DDL.strip(),
        "price_policy_version": PRICE_POLICY_VERSION,
        "database_sha256_before": db_hash_before, "database_sha256_after": db_hash_after,
        "table_created": False, "backup_performed": False, "pid_lock_acquired": False,
        "runtime_seconds": runtime, "checked_at": utc_now_iso(),
    }
    atomic_write_json(CHECK_ONLY_OUTPUT_PATH, output)
    write_preview_csv(dataset["all_rows"])
    log(f"check-only run: status={output['status']} runtime={runtime}s total_rows={dataset['total_rows']}", also_print=False)

    print("=" * 100)
    print(f"HISTORICAL PRICES V1 -- CHECK-ONLY: {output['status']}  (runtime {runtime}s)")
    print("=" * 100)
    for v in dataset["per_ticker_validation"]:
        print(f"  {v['ticker']}: rows={v['row_count']} {v['first_date']} -> {v['last_date']}  clean={v['clean']}")
    print(f"\nTotal rows: {dataset['total_rows']} (expected {EXPECTED_TOTAL_ROWS})")
    print(f"Global checks: {dataset['global_checks']}")
    print(f"In-memory load proof: {in_memory_proof}")
    print(f"Database SHA-256 unchanged: {db_hash_before == db_hash_after}")
    print("=" * 100)
    return output


# =====================================================================
# --execute MODE (built fully; never invoked by this task)
# =====================================================================

def phase_backup() -> dict:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = BACKUPS_DIR / f"ai_stock_agent_pre_historical_prices_v1_load_{timestamp}.duckdb"
    shutil.copy2(PRODUCTION_DB_PATH, backup_path)
    source_checksum = sha256_of_file(PRODUCTION_DB_PATH)
    backup_checksum = sha256_of_file(backup_path)
    if source_checksum != backup_checksum:
        raise RuntimeError("Backup checksum mismatch -- aborting before any write.")
    return {"backup_path": str(backup_path), "backup_checksum": backup_checksum, "source_checksum": source_checksum}


def validate_committed_table(connection, expected_row_count: int) -> None:
    errors = []
    committed_count = connection.execute("SELECT COUNT(*) FROM historical_prices_daily").fetchone()[0]
    if committed_count != expected_row_count:
        errors.append(f"committed row count {committed_count} != expected {expected_row_count}")
    dup = connection.execute(
        "SELECT COUNT(*) FROM (SELECT ticker, price_date, COUNT(*) c FROM historical_prices_daily GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if dup:
        errors.append(f"{dup} duplicate keys found in committed table")
    distinct_tickers = connection.execute("SELECT COUNT(DISTINCT ticker) FROM historical_prices_daily").fetchone()[0]
    if distinct_tickers != EXPECTED_TICKER_COUNT:
        errors.append(f"committed table has {distinct_tickers} distinct tickers, expected {EXPECTED_TICKER_COUNT}")
    for ticker in TICKERS:
        n = connection.execute("SELECT COUNT(*) FROM historical_prices_daily WHERE ticker = ?", [ticker]).fetchone()[0]
        if n != EXPECTED_ROWS_PER_TICKER:
            errors.append(f"{ticker} has {n} rows, expected {EXPECTED_ROWS_PER_TICKER}")
    wrong_policy = connection.execute(
        "SELECT COUNT(*) FROM historical_prices_daily WHERE price_policy_version <> ?", [PRICE_POLICY_VERSION]
    ).fetchone()[0]
    if wrong_policy:
        errors.append(f"{wrong_policy} row(s) have price_policy_version != {PRICE_POLICY_VERSION!r}")
    if errors:
        raise RuntimeError(f"Post-insert table validation failed ({len(errors)} issue(s)): {errors}")


def run_execute() -> int:
    acquire_pid_lock()
    log("=== historical prices v1 load: --execute started ===")
    try:
        db_hash_before = sha256_of_file(PRODUCTION_DB_PATH)

        dataset = build_full_dataset()
        if not all(dataset["global_checks"].values()):
            raise RuntimeError(f"Pre-write validation failed: {dataset['global_checks']}")

        backup_info = phase_backup()
        log(f"Backup complete: {backup_info['backup_path']}")

        connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=False)
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute("DROP TABLE IF EXISTS historical_prices_daily")
            connection.execute(HISTORICAL_PRICES_DAILY_DDL)
            created_at = datetime.now(timezone.utc).replace(tzinfo=None)
            for r in dataset["all_rows"]:
                connection.execute(
                    "INSERT INTO historical_prices_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [r["ticker"], date.fromisoformat(r["date"]), r["open"], r["high"], r["low"], r["close"], r["adj_close"],
                     r["nominal_open"], r["nominal_high"], r["nominal_low"], r["nominal_close"], int(r["volume"]),
                     r["dividend"], r["split_ratio"], SOURCE_NAME, r["source_raw_file"], r["source_raw_sha256"],
                     PRICE_POLICY_VERSION, created_at],
                )
            validate_committed_table(connection, len(dataset["all_rows"]))
            connection.execute("COMMIT")
            log("Atomic transaction committed.")
        except Exception:
            connection.execute("ROLLBACK")
            connection.close()
            raise
        connection.close()

        prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
        row_count = prod_connection.execute("SELECT COUNT(*) FROM historical_prices_daily").fetchone()[0]
        prod_connection.close()

        result = {
            "status": "PASS", "table_created": True, "rows_inserted": row_count,
            "tickers": dataset["distinct_tickers"], "backup_path": backup_info["backup_path"],
            "backup_checksum": backup_info["backup_checksum"], "completed_at": utc_now_iso(),
        }
        atomic_write_json(RESULT_JSON_PATH, result)
        with RESULT_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["tickers", "rows_inserted_total", "backup_path"])
            writer.writerow([",".join(dataset["distinct_tickers"]), row_count, backup_info["backup_path"]])

        manifest = {
            "release": "HISTORICAL_PRICES_V1", "price_policy_version": PRICE_POLICY_VERSION,
            "tickers": dataset["distinct_tickers"], "rows_inserted": row_count,
            "backup_path": backup_info["backup_path"], "backup_checksum": backup_info["backup_checksum"],
            "created_at_utc": utc_now_iso(),
        }
        atomic_write_json(RELEASE_MANIFEST_PATH, manifest)

        log("=== historical prices v1 load: --execute COMPLETE (PASS) ===")
        return 0
    except Exception as exc:  # noqa: BLE001
        fail_result = {"status": "FAIL", "error": str(exc), "failed_at": utc_now_iso()}
        atomic_write_json(RESULT_JSON_PATH, fail_result)
        log(f"=== historical prices v1 load: --execute FAILED: {exc} ===")
        return 1
    finally:
        release_pid_lock()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Historical Prices V1 build/load.")
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
