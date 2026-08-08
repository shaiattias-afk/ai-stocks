"""
tests/helpers.py -- shared, synthetic-fixture-only test helpers for the
stock_agent policy/extraction test suite.

Nothing in this module touches a real file, a real DuckDB database on
disk, or the network. `make_warehouse_connection()` builds a minimal
in-memory DuckDB schema that mirrors just the columns the ported
functions actually SELECT from the real XBRL warehouse (see
src/stock_agent/extraction/core.py, policies/debt_current_long_term.py,
policies/debt_undrawn_revolver.py, policies/short_term_investments.py)
-- confirmed by reading each function's own SQL before writing this.
"""

from __future__ import annotations

import duckdb
import pandas as pd

# --- shared role fixtures ---------------------------------------------------

BS_ROLE_URI = "http://test.invalid/role/BalanceSheet"
BS_ROLE_DEF = "1000 - Statement - Consolidated Balance Sheets"

IS_ROLE_URI = "http://test.invalid/role/IncomeStatement"
IS_ROLE_DEF = "2000 - Statement - Consolidated Statements of Operations"

CF_ROLE_URI = "http://test.invalid/role/CashFlow"
CF_ROLE_DEF = "3000 - Statement - Consolidated Statements of Cash Flows"

DEBT_MATURITY_ROLE_URI = "http://test.invalid/role/DebtMaturitySchedule"
DEBT_MATURITY_ROLE_DEF = "9010 - Disclosure - Schedule of Maturities of Long-Term Debt"

INVESTMENT_MATURITY_ROLE_URI = "http://test.invalid/role/InvestmentMaturitySchedule"
INVESTMENT_MATURITY_ROLE_DEF = "9020 - Disclosure - Contractual Maturities of Marketable Debt Securities"

# --- presentation DataFrame builders ----------------------------------------
# Columns match exactly what
# extraction.core.reconstruct_presentation_dataframe() returns.

PRESENTATION_COLUMNS = [
    "role_uri", "role_definition", "depth", "parent_qname", "concept_qname",
    "label", "is_abstract", "period_type", "balance",
]


def pres_row(
    concept_qname: str,
    label: str,
    *,
    role_uri: str = BS_ROLE_URI,
    role_definition: str = BS_ROLE_DEF,
    parent_qname: str = "",
    depth: int = 1,
    is_abstract: bool = False,
    period_type: str = "instant",
    balance: str = "debit",
) -> dict:
    return {
        "role_uri": role_uri,
        "role_definition": role_definition,
        "depth": depth,
        "parent_qname": parent_qname,
        "concept_qname": concept_qname,
        "label": label,
        "is_abstract": is_abstract,
        "period_type": period_type,
        "balance": balance,
    }


def presentation_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=PRESENTATION_COLUMNS)


# --- in-memory warehouse (DuckDB) -------------------------------------------

def make_warehouse_connection() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE xbrl_facts (
            accession_number VARCHAR, concept_qname VARCHAR, value_numeric DOUBLE,
            context_id VARCHAR, unit_id VARCHAR, period_start VARCHAR, period_end VARCHAR,
            instant_date VARCHAR, dimensions_json VARCHAR, is_nil BOOLEAN,
            period_type VARCHAR, decimals VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE xbrl_units (
            accession_number VARCHAR, unit_id VARCHAR,
            numerator_measures VARCHAR, denominator_measures VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE xbrl_roles (
            accession_number VARCHAR, role_uri VARCHAR, role_definition VARCHAR,
            relationship_type VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE xbrl_presentation_relationships (
            accession_number VARCHAR, role_uri VARCHAR, parent_concept VARCHAR,
            child_concept VARCHAR, order_value DOUBLE, preferred_label VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE xbrl_calculation_relationships (
            accession_number VARCHAR, role_uri VARCHAR, parent_concept VARCHAR,
            child_concept VARCHAR
        )
        """
    )
    return conn


def insert_fact(
    conn: duckdb.DuckDBPyConnection,
    accession: str,
    concept: str,
    value,
    context_id: str,
    *,
    unit_id: str = "usd",
    period_start: str | None = None,
    period_end: str | None = None,
    instant_date: str | None = None,
    dims: str = "{}",
    is_nil: bool = False,
    period_type: str = "instant",
    decimals: str = "-6",
) -> None:
    conn.execute(
        "INSERT INTO xbrl_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            accession, concept, value, context_id, unit_id, period_start,
            period_end, instant_date, dims, is_nil, period_type, decimals,
        ],
    )


def insert_usd_unit(conn: duckdb.DuckDBPyConnection, accession: str, unit_id: str = "usd") -> None:
    conn.execute(
        "INSERT INTO xbrl_units VALUES (?,?,?,?)",
        [accession, unit_id, "iso4217:USD", None],
    )


def insert_role(
    conn: duckdb.DuckDBPyConnection,
    accession: str,
    role_uri: str,
    role_definition: str,
    relationship_type: str = "presentation",
) -> None:
    conn.execute(
        "INSERT INTO xbrl_roles VALUES (?,?,?,?)",
        [accession, role_uri, role_definition, relationship_type],
    )


def insert_presentation_edge(
    conn: duckdb.DuckDBPyConnection,
    accession: str,
    role_uri: str,
    parent_concept: str,
    child_concept: str,
    order_value: float,
    preferred_label: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO xbrl_presentation_relationships VALUES (?,?,?,?,?,?)",
        [accession, role_uri, parent_concept, child_concept, order_value, preferred_label],
    )


def insert_calculation_edge(
    conn: duckdb.DuckDBPyConnection,
    accession: str,
    role_uri: str,
    parent_concept: str,
    child_concept: str,
) -> None:
    conn.execute(
        "INSERT INTO xbrl_calculation_relationships VALUES (?,?,?,?)",
        [accession, role_uri, parent_concept, child_concept],
    )
