"""End-to-end pipeline test, 10 companies, all 3 concurrency improvements
combined (2026-08-12 session, user-directed): concurrent SEC download
(stock_agent.filings.download.download_filings_concurrently, tested on
48 real filings, 0 failures, full integrity-verified), concurrent Arelle
warehouse loading (stock_agent.warehouse.batch.load_many -- the
project's own existing, already-tested process-pool-sharding pattern,
re-verified here with a real append=True run against a full copy of the
production warehouse), and the quarterly engine's new presentation-cache
fix (5.2x measured speedup on the one real slow case found, 0
regressions across 10+18 sampled original-9 company-years).

4 of the 10 tickers (AXON, BIIB, CDNS, CDW) already have their 10-Q
filings discovered+downloaded+archived from an earlier experiment this
session -- included here specifically to also exercise the "already
archived, needs warehouse+engine only" resume path, not just the
from-scratch path the other 6 exercise.

Measures wall-clock time per phase. Writes results to production
tables -- this is real work (10 more tickers' worth of Quarterly Data
V1 coverage), not throwaway test data.
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

TEST_TICKERS = ["AXON", "BIIB", "CDNS", "CDW", "ALNY", "AMAT", "AMGN", "ANSS", "APP", "BKNG"]
RESULT_PATH = DATA_DIR / "e2e_pipeline_test_10_companies_result.json"


def main() -> None:
    print("=" * 100)
    print(f"E2E PIPELINE TEST -- {len(TEST_TICKERS)} companies: {TEST_TICKERS}")
    print("=" * 100)
    phase_timings: dict[str, float] = {}
    overall_start = time.perf_counter()

    # ---- Phase 1: discover + concurrent SEC download ----
    print("\n--- Phase 1: discover + concurrent SEC download ---")
    t0 = time.perf_counter()
    archive.ARCHIVE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    archive_connection = duckdb.connect(str(archive.ARCHIVE_DB_PATH))
    archive.create_archive_schema(archive_connection)
    prod_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=False)
    lock_results = discover_and_lock_10q_batch(prod_connection, archive_connection, TEST_TICKERS, max_workers=8)
    prod_connection.close()
    archive_connection.close()
    phase_timings["download"] = time.perf_counter() - t0

    total_downloaded = sum(len(r.get("downloaded", [])) for r in lock_results.values())
    total_already = sum(len(r.get("already_archived", [])) for r in lock_results.values())
    total_failed = sum(len(r.get("failed", [])) for r in lock_results.values())
    print(f"downloaded={total_downloaded} already_archived={total_already} failed={total_failed} "
          f"elapsed={phase_timings['download']:.1f}s")
    for ticker, r in lock_results.items():
        if r.get("failed"):
            print(f"  {ticker} FAILED: {r['failed']}")

    # ---- Phase 2: concurrent Arelle warehouse load (process-pool sharding) ----
    print("\n--- Phase 2: concurrent warehouse load (batch.load_many, append=True) ---")
    all_new_accessions = []
    for r in lock_results.values():
        all_new_accessions.extend(r.get("downloaded", []))
        all_new_accessions.extend(r.get("already_archived", []))
    # already-warehoused accessions would be redundant work for load_many
    # (it doesn't skip internally) -- filter them out first, same check
    # the old sequential warehouse_load_new_10q used.
    warehouse_connection = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    needs_warehouse = [
        acc for acc in all_new_accessions
        if warehouse_connection.execute("SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?", [acc]).fetchone()[0] == 0
    ]
    warehouse_connection.close()
    print(f"accessions needing warehouse load: {len(needs_warehouse)} (of {len(all_new_accessions)} total)")

    t0 = time.perf_counter()
    warehouse_report = {"status": "SKIPPED", "reason": "nothing to load"}
    if needs_warehouse:
        warehouse_report = warehouse_batch.load_many(
            needs_warehouse, WAREHOUSE_DB_PATH, workers=6, append=True, warm_cache=True, progress=True,
        )
    phase_timings["warehouse"] = time.perf_counter() - t0
    print(f"warehouse status={warehouse_report['status']} elapsed={phase_timings['warehouse']:.1f}s")
    if warehouse_report["status"] != "PASS" and needs_warehouse:
        print("WAREHOUSE LOAD FAILED:", json.dumps(warehouse_report.get("failed"), indent=2, default=str))

    # ---- Phase 3: group into fiscal years + concurrent engine (read-only) + sequential load ----
    print("\n--- Phase 3: group into fiscal years + concurrent engine + sequential load ---")
    t0 = time.perf_counter()
    prod_connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    all_targets = []
    for ticker in TEST_TICKERS:
        targets = group_into_fiscal_years(prod_connection, ticker)
        pending = [t for t in targets if not already_loaded(prod_connection, ticker, t["fiscal_year_end"])]
        all_targets.extend(pending)
    prod_connection.close()
    print(f"fiscal-year targets to process: {len(all_targets)}")

    engine_outputs: dict[tuple, object] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_target = {executor.submit(run_engine, target): target for target in all_targets}
        for future in as_completed(future_to_target):
            target = future_to_target[future]
            key = (target["ticker"], target["fiscal_year_end"])
            try:
                engine_outputs[key] = future.result()
            except Exception as error:  # noqa: BLE001
                engine_outputs[key] = error
    phase_timings["engine_concurrent"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    load_results = []
    for target in all_targets:
        key = (target["ticker"], target["fiscal_year_end"])
        output = engine_outputs[key]
        if isinstance(output, Exception):
            load_results.append({"ticker": target["ticker"], "fiscal_year_end": target["fiscal_year_end"],
                                  "status": "ENGINE_ERROR", "error": str(output)[:500]})
            continue
        result = load_engine_output(output, target)
        load_results.append(result)
        print(f"  {result['ticker']} {result['fiscal_year_end']}: n_rows={result['n_rows']} "
              f"status_counts={result['status_counts']}")
    phase_timings["load_sequential"] = time.perf_counter() - t0

    total_elapsed = time.perf_counter() - overall_start

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    for phase, seconds in phase_timings.items():
        print(f"  {phase:<20} {seconds:>7.1f}s")
    print(f"  {'TOTAL':<20} {total_elapsed:>7.1f}s")
    print(f"  {len(TEST_TICKERS)} tickers, {len(all_targets)} fiscal-years processed "
          f"({total_elapsed / max(len(TEST_TICKERS), 1):.1f}s/ticker average)")

    all_status_counts: dict[str, int] = {}
    for r in load_results:
        for status, count in r.get("status_counts", {}).items():
            all_status_counts[status] = all_status_counts.get(status, 0) + count
    print(f"  aggregate metric-quarter status counts: {all_status_counts}")

    RESULT_PATH.write_text(json.dumps({
        "tickers": TEST_TICKERS, "phase_timings_seconds": phase_timings, "total_elapsed_seconds": total_elapsed,
        "lock_results": lock_results, "warehouse_report_status": warehouse_report.get("status"),
        "load_results": load_results, "aggregate_status_counts": all_status_counts,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
