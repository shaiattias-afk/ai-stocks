"""Thin entry point: load one archived filing into a warehouse DuckDB
via Arelle, sourcing bytes ONLY from the compressed archive.

All logic lives in `stock_agent.warehouse.archive_loader`. This file only
parses arguments and prints the result.

    .venv\\Scripts\\python.exe scripts\\165_filings_archive_arelle_loader.py --accession-number 0000950170-24-087843
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from stock_agent.filings import archive
from stock_agent.warehouse.archive_loader import (
    DEFAULT_PROOF_WAREHOUSE_DB_PATH,
    run_archive_warehouse_load,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load one filing from the compressed archive into a warehouse DuckDB via Arelle."
    )
    parser.add_argument("--accession-number", required=True)
    parser.add_argument("--warehouse-db-path", default=str(DEFAULT_PROOF_WAREHOUSE_DB_PATH))
    parser.add_argument("--internet-connectivity", default="online", choices=["online", "offline"])
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()

    print()
    print("=" * 110)
    print("ARCHIVE -> TEMP DIR -> ARELLE -> WAREHOUSE (single-accession proof)")
    print("=" * 110)
    print(f"Accession: {arguments.accession_number}")
    print(f"Warehouse DB: {arguments.warehouse_db_path}")
    print(f"Internet connectivity: {arguments.internet_connectivity}")

    archive_connection = duckdb.connect(database=str(archive.ARCHIVE_DB_PATH), read_only=True)
    try:
        result = run_archive_warehouse_load(
            archive_connection=archive_connection,
            accession_number=arguments.accession_number,
            warehouse_db_path=Path(arguments.warehouse_db_path),
            internet_connectivity=arguments.internet_connectivity,
        )
    finally:
        archive_connection.close()

    print()
    print(json.dumps(result, indent=2))
    print()
    print("RESULT: PASS")
    print("WORKER_RESULT_JSON=" + json.dumps(result))


if __name__ == "__main__":
    main()
