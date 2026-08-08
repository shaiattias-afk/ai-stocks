"""Parallel Arelle parsing + serialized DuckDB writes for the XBRL
warehouse (Part A of the ingestion-speed work — see docs/DECISIONS_LOG.md
and docs/CURRENT_STATE.md's parallel-ingestion entry).

Measured baseline before this module existed: 187 sequential warehouse
parses took 305.9s total (1.64s average, 10.11s worst case) — parsing
was not yet the bottleneck at 187 filings, but at the ~2,000-filing
scale the point-in-time universe expansion targets, sequential parsing
alone (~55 min) would consume nearly the entire 1-hour ingestion budget.
There was previously ZERO real concurrency anywhere in this project:
`multiprocessing`/`subprocess` appeared only as a single bounded child
process per filing for *timeout enforcement* (scripts/121's
`warehouse_10q_in_child_process`: `subprocess.run(..., timeout=...)`),
never to run N filings at once.

Architecture — this module runs N of exactly that same proven
subprocess-per-filing pattern CONCURRENTLY via a thread pool (the
threads just block on `subprocess.run`, so parsing genuinely runs on N
OS processes/cores; Python's GIL never applies to a blocked
`subprocess.run` call):
  - Each target filing is PARSED (Arelle DTS load + DataFrame
    extraction — CPU/IO-bound, no database access) in its own child
    process, invoked as `python -m stock_agent.warehouse.parallel_loader
    --worker ...`. The child calls `parse_locked_filing` (unchanged),
    pickles the full parse result (DataFrames included) to a temp file,
    and prints one `WORKER_RESULT_JSON=` status line — never the
    DataFrames themselves — to stdout. `subprocess.run(timeout=...)`
    gives a REAL hard kill on a hung child (matches this project's
    existing "external tools/processes that may hang need timeouts"
    rule); a `ProcessPoolExecutor` future cannot be forcibly killed once
    running, which is why this module does not use one.
  - The DuckDB WRITE for every filing happens serially in the calling
    (main) process/thread, one filing at a time under a lock, reusing
    `stock_agent.warehouse.loader.write_parsed_filing` UNCHANGED — the
    exact same atomic transaction + pre-commit count-verification logic
    already governing every non-parallel load (D-041). DuckDB only
    supports one read-write connection to a given file at a time, so
    this is a structural requirement, not a style choice.
  - A single failed/crashed/timed-out filing is caught and recorded — it
    never touches the database (parsing produced nothing to write, or
    the child was killed before it could) and never stops the rest of
    the batch. Because the write step commits or rolls back one filing
    at a time, a crash can never leave a partial filing or a corrupted
    warehouse: at any moment the database contains only fully-committed
    filings.
"""

from __future__ import annotations

import argparse
import json
import pickle
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

import duckdb

from stock_agent import PROJECT_DIR
from stock_agent.warehouse.loader import CACHE_DIR, WarehouseLoaderError, parse_locked_filing, write_parsed_filing

DEFAULT_PER_FILING_TIMEOUT_SECONDS = 180


class IngestTarget(NamedTuple):
    ticker: str
    report_date: str
    form: str = "10-Q"


def select_cache_warm_representatives(targets: list[IngestTarget]) -> list[IngestTarget]:
    """Picks one target per distinct (form, report-year) group — Arelle's
    taxonomy DTS is keyed mainly by fiscal year (dei/us-gaap taxonomy
    versions change annually), so warming the cache with one filing per
    year covers the taxonomy files the rest of that year's filings will
    also need, without re-parsing every filing sequentially first."""
    seen: set[tuple[str, str]] = set()
    representatives: list[IngestTarget] = []
    for target in targets:
        key = (target.form, target.report_date[:4])
        if key in seen:
            continue
        seen.add(key)
        representatives.append(target)
    return representatives


def warm_taxonomy_cache(targets: list[IngestTarget]) -> dict:
    """Sequentially parses one representative filing per (form, year)
    group, IN THIS PROCESS, before the parallel pool starts. This exists
    so N parallel children never simultaneously hit the network for the
    same missing taxonomy file (a thundering-herd risk that could also
    multiply outbound request volume past a safe rate) — by the time the
    pool starts, every taxonomy file the batch needs is already in
    CACHE_DIR, and every child can parse purely from the warm local
    cache exactly as parse_locked_filing already does. Cheap / a no-op
    in the common case where the cache is already fully warm."""
    representatives = select_cache_warm_representatives(targets)
    warmed: list[dict] = []
    failed: list[dict] = []
    start = time.perf_counter()
    for target in representatives:
        try:
            parsed = parse_locked_filing(target.ticker, target.report_date, target.form)
            warmed.append({"ticker": target.ticker, "report_date": target.report_date,
                            "form": target.form, "parse_elapsed_seconds": parsed["parse_elapsed_seconds"]})
        except WarehouseLoaderError as exc:
            failed.append({"ticker": target.ticker, "report_date": target.report_date,
                            "form": target.form, "category": exc.category, "error": str(exc)})
    return {
        "representative_count": len(representatives), "warmed": warmed, "failed": failed,
        "elapsed_seconds": round(time.perf_counter() - start, 3),
    }


def _worker_main(ticker: str, report_date: str, form: str, output_path: Path) -> None:
    """Child-process entry point (invoked via `-m ... --worker`). Parses
    exactly one filing and pickles the full parse result to
    `output_path`; prints a single-line JSON status to stdout. Never
    raises — a WarehouseLoaderError is reported as a FAIL status line,
    not a nonzero-exit crash, so the parent can distinguish "the filing
    genuinely failed validation" from "the child process crashed"."""
    try:
        parsed = parse_locked_filing(ticker, report_date, form)
    except WarehouseLoaderError as exc:
        print("WORKER_RESULT_JSON=" + json.dumps({"status": "FAIL", "category": exc.category, "error": str(exc)}))
        return
    with open(output_path, "wb") as handle:
        pickle.dump(parsed, handle)
    print("WORKER_RESULT_JSON=" + json.dumps({"status": "PARSED", "accession_number": parsed["accession_number"]}))


def _parse_one_in_subprocess(target: IngestTarget, timeout_seconds: float, tmp_dir: Path) -> dict:
    safe_name = f"{target.ticker}_{target.report_date}_{target.form}".replace("/", "_")
    output_path = tmp_dir / f"{safe_name}.pkl"
    cmd = [
        sys.executable, "-m", "stock_agent.warehouse.parallel_loader", "--worker",
        "--ticker", target.ticker, "--report-date", target.report_date, "--form", target.form,
        "--output", str(output_path),
    ]
    base = {"ticker": target.ticker, "report_date": target.report_date, "form": target.form}
    try:
        result = subprocess.run(
            cmd, timeout=timeout_seconds, capture_output=True, text=True, encoding="utf-8", cwd=str(PROJECT_DIR)
        )
    except subprocess.TimeoutExpired:
        return {**base, "status": "TIMEOUT", "error": f"parse exceeded {timeout_seconds}s (child killed)"}

    if result.returncode != 0:
        return {**base, "status": "FAIL", "category": "WORKER_CRASHED",
                "error": f"child exited {result.returncode}: {result.stderr[-2000:]}"}

    worker_line = next((line for line in result.stdout.splitlines() if line.startswith("WORKER_RESULT_JSON=")), None)
    if worker_line is None:
        return {**base, "status": "FAIL", "category": "WORKER_CRASHED", "error": "no WORKER_RESULT_JSON line in child stdout"}

    payload = json.loads(worker_line[len("WORKER_RESULT_JSON="):])
    if payload.get("status") != "PARSED":
        return {**base, "status": "FAIL", "category": payload.get("category", "UNKNOWN"), "error": payload.get("error", "")}

    if not output_path.exists():
        return {**base, "status": "FAIL", "category": "WORKER_CRASHED", "error": "worker reported PARSED but wrote no output file"}
    with open(output_path, "rb") as handle:
        parsed = pickle.load(handle)
    output_path.unlink(missing_ok=True)
    return {**base, "status": "PARSED", "parsed": parsed}


def run_parallel_warehouse_load(
    targets: list[IngestTarget],
    warehouse_db_path: Path,
    max_workers: int = 4,
    per_filing_timeout_seconds: float = DEFAULT_PER_FILING_TIMEOUT_SECONDS,
    warm_cache: bool = True,
) -> dict:
    """Parses `targets` across `max_workers` concurrent child processes
    and writes every successful parse into `warehouse_db_path` one at a
    time, in the order results complete (timing, not a correctness-
    relevant order — each filing's write is independent, keyed by its
    own accession_number). Never raises for an individual filing's
    failure; only propagates if `warehouse_db_path` itself cannot be
    opened."""
    warehouse_db_path.parent.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    warm_result = warm_taxonomy_cache(targets) if warm_cache else None

    batch_start = time.perf_counter()
    results: list[dict] = []
    write_lock = threading.Lock()

    with tempfile.TemporaryDirectory(prefix="parallel_warehouse_load_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        connection = duckdb.connect(database=str(warehouse_db_path))
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_target = {
                    executor.submit(_parse_one_in_subprocess, target, per_filing_timeout_seconds, tmp_dir): target
                    for target in targets
                }
                for future in as_completed(future_to_target):
                    target = future_to_target[future]
                    outcome = future.result()  # _parse_one_in_subprocess never raises
                    if outcome["status"] != "PARSED":
                        results.append(outcome)
                        continue

                    parsed = outcome.pop("parsed")
                    with write_lock:
                        try:
                            write_result = write_parsed_filing(connection, parsed, script_name="parallel_loader.py")
                            results.append({
                                "ticker": target.ticker, "report_date": target.report_date, "form": target.form,
                                "status": write_result["status"], "accession_number": write_result["accession_number"],
                                "total_elapsed_seconds": write_result["total_elapsed_seconds"],
                            })
                        except WarehouseLoaderError as exc:
                            results.append({
                                "ticker": target.ticker, "report_date": target.report_date, "form": target.form,
                                "status": "FAIL", "category": exc.category, "error": str(exc),
                            })
        finally:
            connection.close()

    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    return {
        "target_count": len(targets), "max_workers": max_workers,
        "warm_cache_result": warm_result, "results": results,
        "status_counts": status_counts, "batch_elapsed_seconds": round(time.perf_counter() - batch_start, 3),
    }


def _parse_worker_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--report-date", required=True)
    parser.add_argument("--form", default="10-Q")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    _arguments = _parse_worker_arguments()
    _worker_main(_arguments.ticker, _arguments.report_date, _arguments.form, Path(_arguments.output))
