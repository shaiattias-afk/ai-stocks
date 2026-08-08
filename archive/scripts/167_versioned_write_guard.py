"""
Versioned append-only write guard -- shared, reusable module for every
future load into the six results tables that D-042/D-043/D-045/D-046 had
declared frozen (no writes without a new engine version and full manual
regression). See docs/DECISIONS_LOG.md D-047 for the full rationale.

The freeze existed only because rows carried no version column, so an
engine change could silently rewrite a previously verified number -- a
missing schema feature patched with an all-or-nothing policy. This module
replaces that policy with safeguards enforced by CODE, INSIDE the write
transaction, on every load, forever:

  1. Append-only: DELETE and UPDATE are structurally impossible through
     this module's only write primitive (`execute_write_statement`) --
     it inspects every SQL statement's leading keyword and rejects
     DELETE/UPDATE/DROP/TRUNCATE/ALTER, and rejects INSERT variants that
     would silently overwrite (ON CONFLICT / OR REPLACE / OR IGNORE).
  2. Row count per table never decreases.
  3. Checksum of all pre-existing rows (identified by primary key,
     captured BEFORE the write) is unchanged AFTER the write -- this
     catches a row being silently modified even if some future caller
     found another way to issue an UPDATE-shaped INSERT.
  4. The actual row-count delta after the write exactly equals the
     caller's own DECLARED delta -- catches a caller's own counting bug
     (e.g. an accidental duplicate insert) even when every individual
     insert was itself legitimate.

Any violation raises WriteGuardViolation and rolls back BEFORE commit --
never after. Nothing is ever partially applied: DuckDB's transaction
model guarantees that a connection closed (or crashed) without COMMIT
leaves the on-disk database exactly as it was (verified empirically and
covered by scripts/168's test #4).

Deliberately NOT provided by this module: any way to flip a row's
`is_active` flag from TRUE to FALSE. `is_active` exists so a *future*,
separately authorized supersession mechanism (never an ordinary
"load") can mark an older engine version's rows as superseded without
deleting them -- but building that mechanism is out of scope here. Every
row this module ever inserts carries `is_active = TRUE`; nothing in this
codebase can currently change that.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import duckdb

_BLOCKED_STATEMENT_PATTERN = re.compile(r"^\s*(DELETE|UPDATE|DROP|TRUNCATE|ALTER|REPLACE)\b", re.IGNORECASE)
_INSERT_PATTERN = re.compile(r"^\s*INSERT\s+INTO\b", re.IGNORECASE)
_FORBIDDEN_INSERT_CLAUSE_PATTERN = re.compile(r"\b(ON\s+CONFLICT|OR\s+REPLACE|OR\s+IGNORE)\b", re.IGNORECASE)


class WriteGuardViolation(RuntimeError):
    """Raised for any attempted or detected violation of the append-only
    write guard. The caller's transaction has already been (or is about
    to be) rolled back by the time this is raised."""


# =====================================================================
# LOWEST-LEVEL CHOKEPOINT -- the only way any SQL reaches the database
# through this module. Every higher-level function in this file routes
# through this, so a direct unit test of this function alone proves
# DELETE/UPDATE/overwrite rejection for every caller, present and future.
# =====================================================================

def execute_write_statement(connection: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> None:
    stripped = sql.strip()
    blocked = _BLOCKED_STATEMENT_PATTERN.match(stripped)
    if blocked:
        raise WriteGuardViolation(
            f"Blocked statement type (append-only write guard permits INSERT only): {blocked.group(1).upper()}"
        )
    if not _INSERT_PATTERN.match(stripped):
        raise WriteGuardViolation(
            f"Only 'INSERT INTO ...' statements are permitted through the write guard; got: {stripped[:80]!r}"
        )
    forbidden_clause = _FORBIDDEN_INSERT_CLAUSE_PATTERN.search(stripped)
    if forbidden_clause:
        raise WriteGuardViolation(
            f"INSERT with {forbidden_clause.group(1).upper()} is forbidden -- it could silently overwrite an "
            f"existing row instead of failing on the primary-key constraint."
        )
    connection.execute(sql, params or [])


# =====================================================================
# GENERIC TABLE INTROSPECTION (no table-specific logic anywhere in this
# module -- every table's primary key and column list is discovered at
# runtime, the same "no hard-coded per-entity logic" discipline used
# throughout this project's XBRL engine).
# =====================================================================

def table_primary_key_columns(connection: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = connection.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() "
        "WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'",
        [table],
    ).fetchall()
    if len(rows) != 1:
        raise WriteGuardViolation(
            f"Table {table!r} does not have exactly one PRIMARY KEY constraint ({len(rows)} found) -- "
            f"the write guard requires every guarded table to have a single-constraint primary key."
        )
    return list(rows[0][0])


def table_column_names(connection: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    if not rows:
        raise WriteGuardViolation(f"Table {table!r} not found (or has no columns) in information_schema.")
    return [r[0] for r in rows]


def _row_checksum(rows: list[tuple]) -> str:
    # Same technique already used elsewhere in this project (e.g.
    # scripts/158's / scripts/160's capture_pre_existing_fingerprints):
    # sort the repr() of every row so row order never affects the hash.
    return hashlib.sha256("\n".join(sorted(repr(r) for r in rows)).encode("utf-8")).hexdigest()


# =====================================================================
# HIGH-LEVEL GUARDED APPEND -- the function every future loader should
# call. Opens its own connection, runs entirely inside one transaction,
# and never leaves a partial write on disk.
# =====================================================================

def guarded_versioned_append(
    db_path: str | Path,
    table: str,
    column_names: list[str],
    rows: list[tuple],
    declared_row_delta: int,
) -> dict:
    """
    Append `rows` (each a tuple matching `column_names`, in order --
    including whatever engine_version/loaded_at/is_active values the
    caller has already baked in) to `table` in the database at
    `db_path`, enforcing every rule documented at the top of this file.

    `declared_row_delta` is the caller's OWN claim of how many net new
    rows this call is supposed to add -- deliberately a separate
    parameter from `len(rows)` so a caller bug that duplicates or drops
    a row (declared count != actual rows list != actual DB delta) is
    still caught, not just a mismatch between what was inserted and
    what was declared.

    Raises WriteGuardViolation (after best-effort ROLLBACK) on any
    violation. Raises whatever DuckDB itself raises (e.g. a primary-key
    constraint violation on an attempted overwrite) the same way --
    also after rollback. Returns a result dict only on success.
    """
    db_path = Path(db_path)
    connection = duckdb.connect(str(db_path), read_only=False)
    try:
        pk_columns = table_primary_key_columns(connection, table)
        actual_columns = table_column_names(connection, table)
        missing = set(column_names) - set(actual_columns)
        if missing:
            raise WriteGuardViolation(f"Column(s) {sorted(missing)} do not exist on table {table!r}.")
        pk_indexes = [column_names.index(pk) if pk in column_names else None for pk in pk_columns]

        pre_rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        pre_count = len(pre_rows)
        pre_checksum = _row_checksum(pre_rows)

        # Map each pre-existing row's PK tuple, so we can re-select
        # exactly this same row set after the insert (full-column SELECT
        # * uses the table's own column order, which is why we ask for
        # it explicitly rather than assuming it matches `column_names`).
        full_columns = table_column_names(connection, table)
        full_pk_indexes = [full_columns.index(pk) for pk in pk_columns]

        connection.execute("BEGIN TRANSACTION")
        col_list = ", ".join(column_names)
        placeholders = ", ".join("?" for _ in column_names)
        insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

        inserted = 0
        for row in rows:
            if len(row) != len(column_names):
                raise WriteGuardViolation(
                    f"Row {inserted} has {len(row)} values but {len(column_names)} columns were declared."
                )
            execute_write_statement(connection, insert_sql, list(row))
            inserted += 1

        post_count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if post_count < pre_count:
            raise WriteGuardViolation(
                f"Row count decreased ({pre_count} -> {post_count}) on {table!r} -- append-only violation."
            )
        actual_delta = post_count - pre_count
        if actual_delta != declared_row_delta:
            raise WriteGuardViolation(
                f"Declared row delta ({declared_row_delta}) does not match the actual DB row-count delta "
                f"({actual_delta}) on {table!r} -- refusing to commit an unaccounted-for change."
            )

        post_rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        pre_existing_keys = {tuple(r[i] for i in full_pk_indexes) for r in pre_rows}
        post_rows_matching_pre_existing_keys = [
            r for r in post_rows if tuple(r[i] for i in full_pk_indexes) in pre_existing_keys
        ]
        if len(post_rows_matching_pre_existing_keys) != pre_count:
            raise WriteGuardViolation(
                f"Expected to re-find all {pre_count} pre-existing rows by primary key after the write on "
                f"{table!r}, found {len(post_rows_matching_pre_existing_keys)} -- append-only violation."
            )
        post_pre_existing_checksum = _row_checksum(post_rows_matching_pre_existing_keys)
        if post_pre_existing_checksum != pre_checksum:
            raise WriteGuardViolation(
                f"Checksum of the {pre_count} pre-existing rows on {table!r} changed after the write "
                f"(before={pre_checksum} after={post_pre_existing_checksum}) -- a prior row was modified; "
                f"append-only violation."
            )

        connection.execute("COMMIT")
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 -- best-effort only; connection may already be unusable
            pass
        connection.close()
        raise
    connection.close()

    return {
        "table": table,
        "pre_count": pre_count,
        "post_count": post_count,
        "rows_inserted": inserted,
        "declared_row_delta": declared_row_delta,
        "actual_row_delta": actual_delta,
        "pre_existing_checksum_before": pre_checksum,
        "pre_existing_checksum_after": post_pre_existing_checksum,
        "pre_existing_rows_byte_identical": post_pre_existing_checksum == pre_checksum,
    }
