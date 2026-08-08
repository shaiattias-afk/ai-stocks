"""AI Stock Agent — structured Python package.

PR1 structural extraction: this package moves already-verified logic out of
the numbered ``scripts/`` prototypes into importable modules, without
changing any formula, threshold, regex, or precedence order. See
``docs/DECISIONS_LOG.md`` (D-015 through D-047) for the binding accounting
policies this package implements, and each module's docstring for the exact
source script(s) it was ported from.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_DIR / "data"
DATABASE_DIR = DATA_DIR / "database"
WAREHOUSE_DB_PATH = DATABASE_DIR / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATABASE_DIR / "ai_stock_agent.duckdb"

__all__ = [
    "PROJECT_DIR",
    "DATA_DIR",
    "DATABASE_DIR",
    "WAREHOUSE_DB_PATH",
    "PRODUCTION_DB_PATH",
]
