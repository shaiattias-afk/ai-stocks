"""User-directed (2026-08-12 session): extend Quarterly Data V1 to all
135 companies with existing annual (10-K) coverage, last ~4 fiscal years
(12 quarters) each, all 6 quarterly-extractable metrics per quarter.

Full concurrent pipeline, validated end-to-end on a real 10-company run
(scripts/213, 336s total, 33.6s/ticker average, 820/840 PASS, 0 crashes)
before being wired in here:
  1. Concurrent SEC download across the whole batch
     (stock_agent.quarterly_extension.discover_and_lock_10q_batch --
     includes the company_name_hint fix for delisted/acquired tickers,
     verified across all 135 tickers before this script was written).
  2. Concurrent Arelle warehouse loading, the project's own existing
     process-pool-sharding pattern (stock_agent.warehouse.batch.
     load_many, append=True) -- re-verified here (not assumed) via the
     existing 6-test suite plus a real append=True test against a full
     copy of the production warehouse. Includes a retry-on-transient-
     Windows-PermissionError fix for the final atomic rename, found and
     fixed after a real conflict during testing.
  3. Concurrent engine execution (read-only phase, ThreadPoolExecutor --
     multiple simultaneous read-only DuckDB connections to the same file
     verified safe by direct test) using the quarterly engine's new
     presentation-dataframe cache (5.2x measured speedup on the one real
     slow case found; 0 regressions across 18+10 sampled original-9
     company-years plus the full test suite), then sequential database
     writes (load_engine_output) -- the only step that actually holds a
     write connection.

Processes tickers in BATCHES (BATCH_SIZE) purely for checkpoint
granularity -- unlike the old version, download/warehouse/engine are
each already batched internally across ALL remaining tickers by their
own concurrent implementations, not per-15-ticker groups.

Per-ticker fail-closed: one ticker's failure never stops the batch.
Checkpointed after every batch (atomic write) -- a killed/interrupted
run can simply be re-launched; every step is independently idempotent
and skips work already done.

    .venv\\Scripts\\python.exe scripts\\212_quarterly_universe_extension_batch.py
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH, WAREHOUSE_DB_PATH
from stock_agent.filings import archive
from stock_agent.quarterly_extension import (
    already_loaded,
    discover_and_lock_10q_batch,
    group_into_fiscal_years,
    load_engine_output,
    run_engine,
)
from stock_agent.warehouse import batch as warehouse_batch

CHECKPOINT_PATH = DATA_DIR / "quarterly_universe_extension_checkpoint.json"
RESULT_PATH = DATA_DIR / "quarterly_universe_extension_result.json"
BATCH_SIZE = 15
DOWNLOAD_WORKERS = 8
WAREHOUSE_WORKERS = 6
ENGINE_WORKERS = 6


def _all_tickers(prod_connection) -> list[str]:
    return [r[0] for r in prod_connection.execute(
        "SELECT DISTINCT ticker FROM sec_filings WHERE form = '10-K' ORDER BY ticker"
    ).fetchall()]


def _atomic_write(path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _process_batch(tickers: list[str]) -> tuple[dict, dict]:
    """Runs the full concurrent pipeline for one batch of tickers.
    Returns (per_ticker_results, phase_timings)."""
    timings: dict[str, float] = {}

    # ---- discover + concurrent download ----
    t0 = time.perf_counter()
    archive.ARCHIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    archive_connection = duckdb.connect(str(archive.ARCHIVE_DB_PATH))
    archive.create_archive_schema(archive_connection)
    prod_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=False)
    lock_results = discover_and_lock_10q_batch(prod_connection, archive_connection, tickers, max_workers=DOWNLOAD_WORKERS)
    prod_connection.close()
    timings["download"] = time.perf_counter() - t0

    # ---- concurrent warehouse load ----
    all_new_accessions = []
    for r in lock_results.values():
        all_new_accessions.extend(r.get("downloaded", []))
        all_new_accessions.extend(r.get("already_archived", []))
    # MUST close before load_many: it opens its OWN connections to this
    # same archive file (including a read-only one inside
    # warm_taxonomy_cache), and DuckDB refuses a second connection to the
    # same file under a different configuration while this one (opened
    # read-write, default) is still open -- a real crash hit exactly this
    # on the first production run of this script.
    archive_connection.close()

    t0 = time.perf_counter()
    warehouse_connection = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    needs_warehouse = [
        acc for acc in all_new_accessions
        if warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?", [acc]).fetchone()[0] == 0
    ]
    warehouse_connection.close()
    warehouse_report = {"status": "SKIPPED"}
    if needs_warehouse:
        warehouse_report = warehouse_batch.load_many(
            needs_warehouse, WAREHOUSE_DB_PATH, workers=WAREHOUSE_WORKERS, append=True, warm_cache=True, progress=False,
        )
    timings["warehouse"] = time.perf_counter() - t0

    # ---- group into fiscal years (read-only) ----
    prod_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    ticker_targets: dict[str, list] = {}
    all_targets = []
    for ticker in tickers:
        targets = group_into_fiscal_years(prod_connection, ticker)
        pending = [t for t in targets if not already_loaded(prod_connection, ticker, t["fiscal_year_end"])]
        ticker_targets[ticker] = targets
        all_targets.extend(pending)
    prod_connection.close()

    # ---- concurrent engine (read-only) + sequential load ----
    t0 = time.perf_counter()
    engine_outputs: dict[tuple, object] = {}
    if all_targets:
        with ThreadPoolExecutor(max_workers=ENGINE_WORKERS) as executor:
            future_to_target = {executor.submit(run_engine, target): target for target in all_targets}
            for future in as_completed(future_to_target):
                target = future_to_target[future]
                key = (target["ticker"], target["fiscal_year_end"])
                try:
                    engine_outputs[key] = future.result()
                except Exception as error:  # noqa: BLE001
                    engine_outputs[key] = error
    timings["engine_concurrent"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    load_results: dict[str, list] = {t: [] for t in tickers}
    for target in all_targets:
        key = (target["ticker"], target["fiscal_year_end"])
        output = engine_outputs[key]
        if isinstance(output, Exception):
            load_results[target["ticker"]].append({
                "fiscal_year_end": target["fiscal_year_end"], "status": "ENGINE_ERROR", "error": str(output)[:500],
            })
            continue
        result = load_engine_output(output, target)
        load_results[target["ticker"]].append(result)
    timings["load_sequential"] = time.perf_counter() - t0

    per_ticker = {}
    for ticker in tickers:
        per_ticker[ticker] = {
            "ticker": ticker, "lock_status": lock_results.get(ticker, {}).get("status"),
            "discovered": lock_results.get(ticker, {}).get("discovered", 0),
            "downloaded": len(lock_results.get(ticker, {}).get("downloaded", [])),
            "lock_failed": lock_results.get(ticker, {}).get("failed", []),
            "usable_fiscal_years": len(ticker_targets.get(ticker, [])),
            "newly_processed_fiscal_years": len(load_results.get(ticker, [])),
            "engine_results": load_results.get(ticker, []),
        }
    return per_ticker, timings


def main() -> None:
    prod_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    tickers = _all_tickers(prod_connection)
    prod_connection.close()

    checkpoint = {"completed_tickers": [], "results": {}}
    if CHECKPOINT_PATH.exists():
        checkpoint = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        print(f"resuming: {len(checkpoint['completed_tickers'])}/{len(tickers)} tickers already done")

    remaining = [t for t in tickers if t not in checkpoint["completed_tickers"]]
    print(f"total tickers: {len(tickers)}, remaining: {len(remaining)}, batch_size={BATCH_SIZE}")

    overall_start = time.perf_counter()
    for batch_index, batch_start in enumerate(range(0, len(remaining), BATCH_SIZE), start=1):
        batch = remaining[batch_start:batch_start + BATCH_SIZE]
        t0 = time.perf_counter()
        print(f"\n=== batch {batch_index} ({len(batch)} tickers): {batch} ===", flush=True)

        # Deliberately NOT caught here. _process_batch already fails
        # closed per-ticker for every recoverable error (a ticker's own
        # discovery/download/engine failure never raises out of it -- see
        # discover_and_lock_10q_batch and the engine loop's own per-
        # target try/except). Anything that DOES propagate up to here is
        # systemic (e.g. the database file itself cannot be opened) and
        # would fail EVERY remaining batch identically -- a real
        # production incident hit exactly this (another process holding
        # ai_stock_agent.duckdb open) and the old catch-and-continue
        # design silently marked all 126 remaining tickers "completed"
        # with zero real work done, corrupting the checkpoint. Letting
        # this crash the whole script instead is correct: the checkpoint
        # is only written AFTER a batch genuinely completes, so a crash
        # here leaves it exactly where it was -- safely re-launchable,
        # never falsely advanced.
        per_ticker, timings = _process_batch(batch)

        for ticker in batch:
            checkpoint["completed_tickers"].append(ticker)
            checkpoint["results"][ticker] = per_ticker.get(ticker, {"ticker": ticker, "status": "UNKNOWN"})
        _atomic_write(CHECKPOINT_PATH, checkpoint)

        elapsed = time.perf_counter() - t0
        n_processed = sum(len(checkpoint["completed_tickers"]) for _ in [0])
        remaining_after = len(remaining) - (batch_start + len(batch))
        avg_per_ticker = elapsed / len(batch)
        eta_min = remaining_after * avg_per_ticker / 60
        print(f"  timings: {timings}", flush=True)
        print(f"  batch done in {elapsed:.1f}s ({avg_per_ticker:.1f}s/ticker). "
              f"total done: {len(checkpoint['completed_tickers'])}/{len(tickers)}. "
              f"ETA remaining: {eta_min:.0f} min", flush=True)

    total_elapsed = time.perf_counter() - overall_start
    print(f"\nBATCH DONE. {len(checkpoint['completed_tickers'])}/{len(tickers)} tickers processed. "
          f"elapsed this run: {total_elapsed / 60:.1f} min")

    _atomic_write(RESULT_PATH, checkpoint)
    print(f"written: {RESULT_PATH}")


if __name__ == "__main__":
    main()
