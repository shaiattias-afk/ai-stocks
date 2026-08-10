"""universe/constituents.py — point-in-time index membership.

Stage 4 Part B. This is the module that fixes the project's single most
serious methodological problem: the 9-company universe consists entirely
of companies chosen in 2026 *because they are known to have done well*.
Any model validated on that sample measures its own selection, not
predictive power. Removing that bias requires knowing which companies
were in the index **at each historical date**, including the ones later
removed.

Data model
----------
Two tables, both carrying mandatory provenance (`source`, `source_url`,
`retrieved_at`) -- an index membership claim with no source is worthless
for a point-in-time backtest.

``index_membership_snapshot``
    Membership as observed on one specific date (typically "today").

``index_constituent_events``
    Every addition/removal, with its effective date and reason.

Reconstruction
--------------
Membership at any past date is derived by taking the snapshot and
*undoing* every event dated after the target date, walking backwards:

    for each event with event_date > as_of, newest first:
        if it added X    -> X was not a member before, so remove X
        if it removed Y  -> Y was a member before, so add Y back

This is exact rather than approximate, provided the event list is
complete. Completeness is not assumed -- `validate_membership_series`
checks the reconstructed size at many dates, because a correct
reconstruction of a fixed-size index must stay at its expected size at
*every* date. Drift is proof of a gap in the event data.

Nothing in this module fabricates membership. If the events are missing,
the validator says so; it never fills a hole with a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import duckdb

NASDAQ_100 = "NASDAQ-100"

# The Nasdaq-100 holds 100 *companies*, but the reconstruction counts
# *tickers*, and a company with two share classes in the index
# contributes two. The upper bound is therefore 100 + (number of
# dual-class listings), which is not a guess -- it was measured against
# the reconstructed series:
#
#   2020-03-31 -> 103 tickers: FOX/FOXA, GOOG/GOOGL, LBTYA/LBTYK
#   2020-12-31 -> 102 tickers: Liberty Global gone, Fox + Alphabet remain
#   2023-12-31 -> 101 tickers: Fox gone, Alphabet remains
#
# Every ticker at every one of those dates resolved to a known company
# name (zero unknowns), which is what distinguishes "extra share classes"
# from "the changelog is missing removals". The bound is set to 104 to
# leave one dual-class listing of headroom; anything beyond that is
# treated as a data gap and fails, which is the whole point of the check.
EXPECTED_SIZE_BY_INDEX = {NASDAQ_100: (100, 104)}


class MembershipReconstructionError(Exception):
    """Raised when the event data cannot support an exact reconstruction.
    Never resolved by guessing a membership list."""


@dataclass(frozen=True)
class ConstituentEvent:
    index_name: str
    event_date: date
    added_ticker: str | None
    added_company: str | None
    removed_ticker: str | None
    removed_company: str | None
    reason: str | None


def create_universe_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS index_membership_snapshot (
        index_name    VARCHAR   NOT NULL,
        snapshot_date DATE      NOT NULL,
        ticker        VARCHAR   NOT NULL,
        company_name  VARCHAR,
        source        VARCHAR   NOT NULL,
        source_url    VARCHAR   NOT NULL,
        retrieved_at  TIMESTAMP NOT NULL,
        PRIMARY KEY (index_name, snapshot_date, ticker)
    )""")

    connection.execute("""CREATE TABLE IF NOT EXISTS index_constituent_events (
        index_name      VARCHAR   NOT NULL,
        event_date      DATE      NOT NULL,
        added_ticker    VARCHAR,
        added_company   VARCHAR,
        removed_ticker  VARCHAR,
        removed_company VARCHAR,
        reason          VARCHAR,
        source          VARCHAR   NOT NULL,
        source_url      VARCHAR   NOT NULL,
        retrieved_at    TIMESTAMP NOT NULL
    )""")


def store_snapshot(
    connection: duckdb.DuckDBPyConnection,
    index_name: str,
    snapshot_date: date,
    members: list[tuple[str, str | None]],
    source: str,
    source_url: str,
) -> int:
    """Records membership observed on `snapshot_date`. `members` is a list
    of (ticker, company_name)."""
    if not members:
        raise ValueError("refusing to store an empty membership snapshot")
    retrieved_at = datetime.now(timezone.utc)
    connection.execute(
        "DELETE FROM index_membership_snapshot WHERE index_name = ? AND snapshot_date = ?",
        [index_name, snapshot_date],
    )
    for ticker, company_name in members:
        connection.execute(
            "INSERT INTO index_membership_snapshot VALUES (?,?,?,?,?,?,?)",
            [index_name, snapshot_date, ticker, company_name, source, source_url, retrieved_at],
        )
    return len(members)


def store_events(
    connection: duckdb.DuckDBPyConnection,
    events: list[ConstituentEvent],
    source: str,
    source_url: str,
) -> int:
    """Records constituent changes. An event that neither adds nor removes
    anything is rejected rather than stored as a no-op."""
    retrieved_at = datetime.now(timezone.utc)
    for event in events:
        if not event.added_ticker and not event.removed_ticker:
            raise ValueError(f"event on {event.event_date} neither adds nor removes a ticker")
        connection.execute(
            "INSERT INTO index_constituent_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            [event.index_name, event.event_date, event.added_ticker, event.added_company,
             event.removed_ticker, event.removed_company, event.reason,
             source, source_url, retrieved_at],
        )
    return len(events)


def load_snapshot(
    connection: duckdb.DuckDBPyConnection,
    index_name: str,
    snapshot_date: date | None = None,
) -> tuple[date, set[str]]:
    """Returns (snapshot_date, tickers). Defaults to the most recent
    snapshot stored for the index."""
    if snapshot_date is None:
        row = connection.execute(
            "SELECT MAX(snapshot_date) FROM index_membership_snapshot WHERE index_name = ?",
            [index_name],
        ).fetchone()
        if row is None or row[0] is None:
            raise MembershipReconstructionError(f"no membership snapshot stored for {index_name!r}")
        snapshot_date = row[0]

    tickers = {
        r[0] for r in connection.execute(
            "SELECT ticker FROM index_membership_snapshot WHERE index_name = ? AND snapshot_date = ?",
            [index_name, snapshot_date],
        ).fetchall()
    }
    if not tickers:
        raise MembershipReconstructionError(
            f"snapshot for {index_name!r} on {snapshot_date} is empty"
        )
    return snapshot_date, tickers


def load_events(connection: duckdb.DuckDBPyConnection, index_name: str) -> list[ConstituentEvent]:
    rows = connection.execute(
        "SELECT index_name, event_date, added_ticker, added_company, removed_ticker,"
        " removed_company, reason FROM index_constituent_events WHERE index_name = ?"
        " ORDER BY event_date",
        [index_name],
    ).fetchall()
    return [ConstituentEvent(*row) for row in rows]


def members_on(
    connection: duckdb.DuckDBPyConnection,
    index_name: str,
    as_of: date,
    snapshot_date: date | None = None,
) -> set[str]:
    """Reconstructs index membership as it stood on `as_of`.

    Walks backwards from the stored snapshot, undoing every event dated
    after `as_of`. Raises rather than guessing when an event contradicts
    the reconstruction (e.g. undoing an addition for a ticker that is not
    currently a member) -- that means the event data is inconsistent, and
    a silently wrong universe would poison every downstream backtest.
    """
    resolved_snapshot_date, members = load_snapshot(connection, index_name, snapshot_date)
    if as_of > resolved_snapshot_date:
        raise MembershipReconstructionError(
            f"as_of {as_of} is after the snapshot date {resolved_snapshot_date}; "
            "membership after the last observation is unknown, not extrapolated"
        )

    members = set(members)
    events = load_events(connection, index_name)

    for event in sorted(events, key=lambda e: e.event_date, reverse=True):
        if event.event_date <= as_of:
            break
        if event.event_date > resolved_snapshot_date:
            continue  # event postdates what the snapshot observed; not applicable

        if event.added_ticker:
            if event.added_ticker not in members:
                raise MembershipReconstructionError(
                    f"{index_name}: cannot undo the {event.event_date} addition of "
                    f"{event.added_ticker} -- it is not a member at that point in the walk. "
                    "The event list is inconsistent or incomplete."
                )
            members.discard(event.added_ticker)

        if event.removed_ticker:
            if event.removed_ticker in members:
                raise MembershipReconstructionError(
                    f"{index_name}: cannot undo the {event.event_date} removal of "
                    f"{event.removed_ticker} -- it is already a member at that point in the walk. "
                    "The event list is inconsistent or incomplete."
                )
            members.add(event.removed_ticker)

    return members


def validate_membership_series(
    connection: duckdb.DuckDBPyConnection,
    index_name: str,
    dates: list[date],
) -> dict:
    """Reconstructs membership at many dates and checks the size stayed
    within the index's expected range at every one.

    A fixed-size index must have its fixed size at every historical date.
    Size drift is objective proof that the event data has gaps -- this is
    the check that stops an incomplete changelog from silently producing
    a wrong universe.
    """
    low, high = EXPECTED_SIZE_BY_INDEX.get(index_name, (1, 10_000))
    observations, failures = [], []

    for as_of in sorted(dates):
        try:
            size = len(members_on(connection, index_name, as_of))
        except MembershipReconstructionError as error:
            failures.append({"as_of": as_of.isoformat(), "error": str(error)})
            continue
        observations.append({"as_of": as_of.isoformat(), "size": size})
        if not (low <= size <= high):
            failures.append({
                "as_of": as_of.isoformat(), "size": size,
                "error": f"membership size {size} outside expected range {low}-{high}",
            })

    return {
        "index_name": index_name,
        "dates_checked": len(dates),
        "expected_size_range": [low, high],
        "observations": observations,
        "failures": failures,
        "valid": not failures,
    }


def survivorship_report(
    connection: duckdb.DuckDBPyConnection,
    index_name: str,
    as_of: date,
) -> dict:
    """Quantifies the bias being removed: how many companies were in the
    index at `as_of` but are NOT in it today. Those are exactly the
    companies a survivor-only universe silently omits."""
    snapshot_date, current = load_snapshot(connection, index_name, None)
    historical = members_on(connection, index_name, as_of, snapshot_date)
    departed = sorted(historical - current)
    return {
        "index_name": index_name,
        "as_of": as_of.isoformat(),
        "snapshot_date": snapshot_date.isoformat(),
        "members_at_as_of": len(historical),
        "members_today": len(current),
        "departed_since": departed,
        "departed_count": len(departed),
        "survivorship_bias_pct": round(100.0 * len(departed) / len(historical), 1) if historical else 0.0,
    }


__all__ = [
    "NASDAQ_100", "ConstituentEvent", "MembershipReconstructionError",
    "create_universe_schema", "load_events", "load_snapshot", "members_on",
    "store_events", "store_snapshot", "survivorship_report",
    "validate_membership_series",
]
