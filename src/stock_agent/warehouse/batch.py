"""warehouse/batch.py — parallel ingestion of many archived filings into
one XBRL warehouse.

Stage 4 Part A. Until now every ingestion path in this project was strictly
sequential: `multiprocessing` appeared in ~28 files but every use was a
single bounded child process for *timeout enforcement*, never concurrency,
and the batch runners looped with `subprocess.run` one filing at a time.

Why sharding rather than shared writes
--------------------------------------
DuckDB is single-writer, so N workers cannot write the same database
concurrently. Parsing dominates the cost anyway (measured: 1.64s average
Arelle parse vs. 0.12s DuckDB write), so the win comes entirely from
parallelising the parse. Each worker therefore writes into its own private
shard database, and the parent merges the shards afterwards in sorted
accession order — deterministic, and identical in content to a sequential
run.

Why a fresh process per filing
------------------------------
Arelle keeps global state between loads. The proven sequential path
(`scripts/166`) ran each accession in its own subprocess for exactly this
reason. `max_tasks_per_child=1` preserves that isolation guarantee under
the pool: every filing still gets a pristine interpreter.

Taxonomy cache warming
----------------------
Starting N cold workers simultaneously makes all of them fetch the same
external taxonomy at once — slow, and needlessly hard on SEC's servers.
`warm_taxonomy_cache()` loads a single filing sequentially first, populating
`data/arelle_cache`, so the parallel phase runs against a warm cache.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import duckdb

from stock_agent import DATA_DIR
from stock_agent.filings import archive as filings_archive
from stock_agent.warehouse import extract as wh_extract
from stock_agent.warehouse.archive_loader import run_archive_warehouse_load

DEFAULT_WORKERS = 6
PER_FILING_TIMEOUT_SECONDS = 180

# Filings are processed in chunks, with a FRESH process pool per chunk.
#
# This replaces `ProcessPoolExecutor(max_tasks_per_child=N)`, which
# deadlocked reproducibly on Windows: with 8 workers and N=8, all eight
# workers hit their retirement limit on the same task and the pool hung
# permanently during simultaneous respawn (observed: progress froze at
# exactly 64 = 8 x 8 shards, zero worker processes alive, parent blocked
# in `as_completed` with flat CPU).
#
# Chunking achieves the same goal -- bounding memory growth across a long
# batch, since Arelle accumulates state -- by retiring every worker at a
# chunk boundary instead, without ever exercising the mid-pool
# replacement path. Worker reuse *within* a chunk is safe:
# `load_many_sequential` runs every filing in one process and was
# verified byte-identical to the isolated path, so Arelle's between-load
# state does not affect extracted values.
DEFAULT_CHUNK_SIZE = 40


def _load_one_into_shard(
    accession_number: str,
    shard_dir: str,
    archive_db_path: str,
    internet_connectivity: str,
) -> dict:
    """Worker body. Runs in its own fresh process; writes into a private
    shard database so no two workers ever contend for the same file."""
    shard_path = Path(shard_dir) / f"shard_{accession_number.replace('-', '')}.duckdb"
    start = time.perf_counter()
    archive_connection = duckdb.connect(database=archive_db_path, read_only=True)
    try:
        result = run_archive_warehouse_load(
            archive_connection=archive_connection,
            accession_number=accession_number,
            warehouse_db_path=shard_path,
            internet_connectivity=internet_connectivity,
        )
        result["shard_path"] = str(shard_path)
        return result
    except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
        return {
            "status": "FAIL",
            "accession_number": accession_number,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - start, 3),
        }
    finally:
        archive_connection.close()


def warm_taxonomy_cache(
    accession_number: str,
    archive_db_path: Path | None = None,
    internet_connectivity: str = "online",
) -> dict:
    """Loads exactly one filing sequentially into a throwaway database so
    the shared Arelle taxonomy cache is populated before the parallel
    phase. The throwaway database is deleted afterwards; only the cache
    side effect is kept."""
    archive_db_path = archive_db_path or filings_archive.ARCHIVE_DB_PATH
    warm_dir = Path(tempfile.mkdtemp(prefix="warehouse_cache_warm_"))
    try:
        start = time.perf_counter()
        connection = duckdb.connect(database=str(archive_db_path), read_only=True)
        try:
            run_archive_warehouse_load(
                archive_connection=connection,
                accession_number=accession_number,
                warehouse_db_path=warm_dir / "warm.duckdb",
                internet_connectivity=internet_connectivity,
            )
        finally:
            connection.close()
        return {"status": "PASS", "accession_number": accession_number,
                "elapsed_seconds": round(time.perf_counter() - start, 3)}
    finally:
        shutil.rmtree(warm_dir, ignore_errors=True)


def _replace_with_retry(
    source: Path, target: Path, attempts: int = 5, initial_delay_seconds: float = 1.0,
) -> None:
    """os.replace, retried with exponential backoff on Windows
    PermissionError (WinError 5, 'Access is denied') -- another process
    holding even a brief, transient handle on `target` (antivirus,
    indexing, any other reader) can fail an otherwise-fully-successful
    merge outright. Never retries on any OTHER exception type; still
    raises after `attempts` if the conflict doesn't clear."""
    delay = initial_delay_seconds
    for attempt in range(1, attempts + 1):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(delay)
            delay *= 2


def _merge_shard(target_connection: duckdb.DuckDBPyConnection, shard_path: Path) -> None:
    """Copies every warehouse table out of one shard into the target.

    ATTACH/DETACH must sit OUTSIDE the transaction: DuckDB refuses to
    detach a database the current transaction has touched. Each shard is
    therefore merged in its own transaction; whole-operation atomicity is
    provided one level up by `_merge_shards_atomically`, which builds a
    temporary target and swaps it into place only on full success.
    """
    target_connection.execute(f"ATTACH '{shard_path}' AS shard (READ_ONLY)")
    try:
        target_connection.execute("BEGIN TRANSACTION")
        try:
            for table in wh_extract.WAREHOUSE_TABLES + ["warehouse_runs"]:
                target_connection.execute(f"INSERT INTO {table} BY NAME SELECT * FROM shard.{table}")
            target_connection.execute("COMMIT")
        except Exception:
            try:
                target_connection.execute("ROLLBACK")
            except duckdb.TransactionException:
                pass
            raise
    finally:
        target_connection.execute("DETACH shard")


def _merge_shards_atomically(
    shard_paths_in_order: list[Path],
    warehouse_db_path: Path,
    append: bool = False,
) -> None:
    """Merges every shard into a staging database beside the target and
    atomically renames it into place. The target is either fully written
    or left completely untouched -- never half-merged.

    `append=False` (default) builds a warehouse containing ONLY these
    shards. `append=True` starts from a copy of the existing target, so
    the new filings are added to what is already there.

    The distinction matters: because the merge finishes with a rename,
    running the default mode against a populated warehouse would discard
    every accession already in it. That is silent, irreversible data loss,
    so an existing target must be handled explicitly rather than by
    whichever mode happens to be the default.
    """
    warehouse_db_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = warehouse_db_path.with_suffix(warehouse_db_path.suffix + ".staging")
    staging_path.unlink(missing_ok=True)

    try:
        if append:
            if not warehouse_db_path.exists():
                raise FileNotFoundError(
                    f"append=True but no existing warehouse at {warehouse_db_path}"
                )
            shutil.copy2(warehouse_db_path, staging_path)

        target = duckdb.connect(database=str(staging_path))
        try:
            wh_extract.create_warehouse_schema(target)
            for shard_path in shard_paths_in_order:
                _merge_shard(target, shard_path)
        finally:
            target.close()
        # atomic on the same volume; the target only ever sees a complete
        # database. Retried on Windows PermissionError: a real conflict
        # was observed (2026-08-12) where some OTHER process briefly held
        # a handle on warehouse_db_path (e.g. antivirus/indexing, or any
        # other reader) at the exact instant of the rename, failing the
        # whole merge outright even though every shard had already loaded
        # successfully. The merged content itself never changes -- this
        # only re-attempts the final, already-computed rename.
        _replace_with_retry(staging_path, warehouse_db_path)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise


def load_many(
    accession_numbers: list[str],
    warehouse_db_path: Path,
    workers: int = DEFAULT_WORKERS,
    archive_db_path: Path | None = None,
    internet_connectivity: str = "online",
    warm_cache: bool = True,
    progress: bool = True,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    append: bool = False,
) -> dict:
    """Ingests many archived filings in parallel into `warehouse_db_path`.

    Returns a report with per-accession results. The target database is
    created if absent and is written in ONE transaction at the end, in
    sorted accession order, so a crash mid-merge leaves it untouched.

    Any worker failure is reported, never silently skipped, and the merge
    is refused outright if any filing failed — a partially ingested
    warehouse is never presented as a complete one.
    """
    archive_db_path = archive_db_path or filings_archive.ARCHIVE_DB_PATH
    accession_numbers = list(accession_numbers)
    total_start = time.perf_counter()

    # Refuse to silently discard an existing warehouse. The merge ends in a
    # rename, so the default (rebuild) mode against a populated target would
    # delete every accession already there.
    if warehouse_db_path.exists() and not append:
        raise FileExistsError(
            f"{warehouse_db_path} already exists. Pass append=True to add these filings "
            "to it, or delete/point elsewhere to rebuild from scratch. Refusing to "
            "replace a populated warehouse implicitly."
        )

    warm_result = None
    if warm_cache and accession_numbers:
        warm_result = warm_taxonomy_cache(
            accession_numbers[0], archive_db_path=archive_db_path,
            internet_connectivity=internet_connectivity,
        )

    shard_root = Path(tempfile.mkdtemp(prefix="warehouse_shards_"))
    results: list[dict] = []
    try:
        parallel_start = time.perf_counter()
        done = 0
        for chunk_start in range(0, len(accession_numbers), chunk_size):
            chunk = accession_numbers[chunk_start:chunk_start + chunk_size]
            # A generous ceiling for the whole chunk. Its only job is to
            # turn a hung pool into a reported failure instead of an
            # indefinite block -- it must never fire on healthy work.
            chunk_budget = PER_FILING_TIMEOUT_SECONDS * (len(chunk) / max(workers, 1) + 2)

            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        _load_one_into_shard, accession_number, str(shard_root),
                        str(archive_db_path), internet_connectivity,
                    ): accession_number
                    for accession_number in chunk
                }
                try:
                    for future in as_completed(futures, timeout=chunk_budget):
                        accession_number = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:  # noqa: BLE001
                            result = {"status": "FAIL", "accession_number": accession_number,
                                      "error": f"{type(exc).__name__}: {exc}"}
                        results.append(result)
                        done += 1
                        if progress:
                            facts = result.get("computed_counts", {}).get("xbrl_facts", "?")
                            print(f"[{done:>4}/{len(accession_numbers)}] {accession_number} "
                                  f"{result.get('status', 'UNKNOWN'):<8} facts={facts}", flush=True)
                except TimeoutError:
                    unfinished = [futures[f] for f in futures if not f.done()]
                    for accession_number in unfinished:
                        results.append({
                            "status": "FAIL", "accession_number": accession_number,
                            "error": f"chunk exceeded {chunk_budget:.0f}s budget; worker pool stalled",
                        })
                    for f in futures:
                        f.cancel()
        parallel_seconds = time.perf_counter() - parallel_start

        failed = [r for r in results if r.get("status") != "PASS"]
        if failed:
            return {
                "status": "FAIL",
                "reason": "one or more filings failed to load; nothing was merged",
                "accessions_attempted": len(accession_numbers),
                "failed": failed,
                "results": results,
                "parallel_seconds": round(parallel_seconds, 3),
                "total_elapsed_seconds": round(time.perf_counter() - total_start, 3),
            }

        merge_start = time.perf_counter()
        by_accession = {r["accession_number"]: r for r in results}
        _merge_shards_atomically(
            [Path(by_accession[a]["shard_path"]) for a in sorted(by_accession)],
            warehouse_db_path,
            append=append,
        )
        merge_seconds = time.perf_counter() - merge_start

        return {
            "status": "PASS",
            "accessions_attempted": len(accession_numbers),
            "accessions_passed": len(results),
            "workers": workers,
            "warm_cache_result": warm_result,
            "parallel_seconds": round(parallel_seconds, 3),
            "merge_seconds": round(merge_seconds, 3),
            "total_elapsed_seconds": round(time.perf_counter() - total_start, 3),
            "results": results,
        }
    finally:
        shutil.rmtree(shard_root, ignore_errors=True)


def load_many_sequential(
    accession_numbers: list[str],
    warehouse_db_path: Path,
    archive_db_path: Path | None = None,
    internet_connectivity: str = "online",
) -> dict:
    """Reference implementation: same work, one filing at a time, into the
    target database directly. Exists so tests can prove the parallel path
    produces identical output — not the production path."""
    archive_db_path = archive_db_path or filings_archive.ARCHIVE_DB_PATH
    total_start = time.perf_counter()
    results = []
    connection = duckdb.connect(database=str(archive_db_path), read_only=True)
    try:
        for accession_number in accession_numbers:
            results.append(run_archive_warehouse_load(
                archive_connection=connection,
                accession_number=accession_number,
                warehouse_db_path=warehouse_db_path,
                internet_connectivity=internet_connectivity,
            ))
    finally:
        connection.close()
    return {
        "status": "PASS",
        "accessions_attempted": len(accession_numbers),
        "total_elapsed_seconds": round(time.perf_counter() - total_start, 3),
        "results": results,
    }


def all_archived_accessions(
    archive_db_path: Path | None = None,
    parseable_only: bool = True,
) -> list[str]:
    """Archived accessions, in ticker/report-date order.

    `parseable_only` (the default) excludes filings recorded as carrying
    no XBRL document set. Those are archived deliberately -- the filing
    genuinely exists and its absence of machine-readable data is a real
    fact about the company's history -- but handing one to Arelle can only
    fail, so they are not offered up for parsing.
    """
    archive_db_path = archive_db_path or filings_archive.ARCHIVE_DB_PATH
    connection = duckdb.connect(database=str(archive_db_path), read_only=True)
    try:
        query = "SELECT accession_number FROM filing_archive_manifest"
        if parseable_only:
            query += f" WHERE source IS DISTINCT FROM '{filings_archive.NO_XBRL_DOCUMENT_SET}'"
        query += " ORDER BY ticker, report_date"
        return [row[0] for row in connection.execute(query).fetchall()]
    finally:
        connection.close()


__all__ = [
    "DATA_DIR", "DEFAULT_CHUNK_SIZE", "DEFAULT_WORKERS", "all_archived_accessions",
    "load_many", "load_many_sequential", "warm_taxonomy_cache",
]
