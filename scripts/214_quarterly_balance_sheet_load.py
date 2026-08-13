"""User-directed (2026-08-12 session, D-076/D-077): extend Quarterly
Data's coverage from 6 income-statement/cash-flow metrics to the full
9-factor composite's requirements, by extracting balance-sheet metrics
(current_debt, long_term_debt, total_debt, cash_and_equivalents,
short_term_investments, stockholders_equity, adjusted_net_debt,
invested_capital) plus the derived ROIC chain (effective_tax_rate, nopat,
average_invested_capital, roic) for every company-quarter Engine V5 has
already loaded.

Purely additive: writes new metric_name rows under the existing run_id
per (ticker, fiscal_year_end), tagged with a new engine_version
(quarterly_balance_sheet_v1) -- never touches Engine V5's own 6-metric
rows. Idempotent and resumable: already_loaded skips any (run_id,
fiscal_quarter) that already has this engine_version's rows.

**Redesigned 2026-08-13 after the first attempt was silently killed
overnight with zero visible progress and zero rows written.** The
original design built one giant in-memory cache across ALL ~1,400
company-quarters BEFORE writing anything -- an all-or-nothing step with
no checkpoint and, because stdout is fully buffered when piped, no
visible progress either; when the process was interrupted (machine
sleep / session end), the ~50+ minutes of compute were entirely lost.

This version processes ONE company-quarter at a time: compute, write,
commit, print (flush=True), repeat. The write-through cache in
compute_derived_metrics_from_cache still avoids recomputing an
accession already seen earlier in this same run (most commonly: a later
quarter's own "4 quarters ago" YoY comparison), but every quarter's
result is durable the moment it's written -- an interruption at any
point loses at most the ONE quarter in flight, and simply re-running
this script picks up exactly where it left off (already_loaded).

    .venv\\Scripts\\python.exe scripts\\214_quarterly_balance_sheet_load.py
"""

from __future__ import annotations

import json
import sys
import time

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH, WAREHOUSE_DB_PATH
from stock_agent.extraction.quarterly_balance_sheet import (
    already_loaded,
    compute_derived_metrics_from_cache,
    list_all_engine_v5_quarters,
    write_quarterly_balance_sheet_metrics,
)
from stock_agent.scoring.quarterly_trend_v1 import _quarters_before

RESULT_PATH = DATA_DIR / "quarterly_balance_sheet_load_result.json"


def main() -> None:
    overall_start = time.perf_counter()

    read_only = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    all_quarters = list_all_engine_v5_quarters(read_only)
    read_only.close()
    print(f"total Engine V5 company-quarters found: {len(all_quarters)}", flush=True)

    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    prod = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=False)

    cache: dict[str, dict] = {}
    status_counts: dict[str, int] = {}
    skipped_already_loaded = 0
    errors: list[dict] = []
    n_processed = 0
    t0 = time.perf_counter()

    for i, quarter in enumerate(all_quarters, start=1):
        if already_loaded(prod, quarter["run_id"], quarter["fiscal_quarter"]):
            skipped_already_loaded += 1
            continue
        try:
            metrics = compute_derived_metrics_from_cache(warehouse, prod, cache, quarter, _quarters_before)
        except Exception as error:  # noqa: BLE001
            errors.append({"ticker": quarter["ticker"], "fiscal_year_end": quarter["fiscal_year_end"],
                            "fiscal_quarter": quarter["fiscal_quarter"], "error": str(error)[:500]})
            print(f"  [{i}/{len(all_quarters)}] {quarter['ticker']} {quarter['fiscal_year_end']} "
                  f"{quarter['fiscal_quarter']}: ERROR {str(error)[:150]}", flush=True)
            continue

        write_quarterly_balance_sheet_metrics(
            prod, quarter["run_id"], quarter["ticker"], quarter["fiscal_year_end"], quarter["fiscal_quarter"],
            quarter["period_end"], quarter["accession_number"], metrics,
        )
        n_processed += 1
        for metric_result in metrics.values():
            status_counts[metric_result["status"]] = status_counts.get(metric_result["status"], 0) + 1

        roic_status = metrics["roic"]["status"]
        elapsed = time.perf_counter() - t0
        rate = n_processed / elapsed if elapsed > 0 else 0.0
        remaining = len(all_quarters) - skipped_already_loaded - n_processed - len(errors)
        eta_min = (remaining / rate / 60) if rate > 0 else float("nan")
        print(f"  [{i}/{len(all_quarters)}] {quarter['ticker']:<6} {quarter['fiscal_year_end']} "
              f"{quarter['fiscal_quarter']}  roic={roic_status:<16} cache={len(cache)} accessions  "
              f"{rate:.2f} q/s  ETA {eta_min:.1f}min", flush=True)

    warehouse.close()
    prod.close()
    elapsed = time.perf_counter() - t0

    print(f"\nwrite phase: {elapsed:.1f}s, {n_processed} written, "
          f"{skipped_already_loaded} already loaded (skipped), {len(errors)} errors", flush=True)
    print("status counts:", status_counts, flush=True)
    if errors:
        print("errors (first 10):", json.dumps(errors[:10], indent=2), flush=True)

    RESULT_PATH.write_text(json.dumps({
        "total_quarters": len(all_quarters), "distinct_accessions_cached": len(cache),
        "n_processed": n_processed, "skipped_already_loaded": skipped_already_loaded,
        "status_counts": status_counts, "errors": errors,
        "total_elapsed_seconds": time.perf_counter() - overall_start,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}", flush=True)


if __name__ == "__main__":
    main()
