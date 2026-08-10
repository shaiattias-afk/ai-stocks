"""Tests for point-in-time index membership reconstruction.

Pure logic, synthetic fixtures, no network and no project database. The
reconstruction walk is the thing that decides which companies a backtest
is allowed to see at each historical date, so its failure modes matter
more than its happy path:

  * a company removed from the index must reappear when looking at a date
    before its removal -- that is the entire survivorship-bias fix
  * an inconsistent or incomplete event list must raise, never silently
    produce a plausible-looking wrong universe
  * asking about a date after the snapshot must refuse to extrapolate
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from stock_agent.universe import constituents as uni

INDEX = "TEST-5"
SOURCE = "unit-test-fixture"
SOURCE_URL = "https://example.invalid/fixture"

SNAPSHOT_DATE = date(2026, 1, 1)
# Today's members. WINNER was added in 2023 replacing FAILCO; NEWCO was
# added in 2025 replacing OLDCO.
SNAPSHOT_MEMBERS = [
    ("AAA", "Alpha"),
    ("BBB", "Beta"),
    ("CCC", "Gamma"),
    ("WINNER", "Winner Corp"),
    ("NEWCO", "New Co"),
]

EVENTS = [
    uni.ConstituentEvent(INDEX, date(2023, 6, 1), "WINNER", "Winner Corp", "FAILCO", "Fail Co", "bankruptcy"),
    uni.ConstituentEvent(INDEX, date(2025, 3, 1), "NEWCO", "New Co", "OLDCO", "Old Co", "acquired"),
]


@pytest.fixture()
def connection():
    con = duckdb.connect(":memory:")
    uni.create_universe_schema(con)
    uni.store_snapshot(con, INDEX, SNAPSHOT_DATE, SNAPSHOT_MEMBERS, SOURCE, SOURCE_URL)
    uni.store_events(con, EVENTS, SOURCE, SOURCE_URL)
    # a 5-member test index
    uni.EXPECTED_SIZE_BY_INDEX[INDEX] = (5, 5)
    try:
        yield con
    finally:
        uni.EXPECTED_SIZE_BY_INDEX.pop(INDEX, None)
        con.close()


def test_membership_today_equals_the_snapshot(connection) -> None:
    assert uni.members_on(connection, INDEX, SNAPSHOT_DATE) == {
        "AAA", "BBB", "CCC", "WINNER", "NEWCO"
    }


def test_delisted_company_reappears_before_its_removal(connection) -> None:
    """The core survivorship-bias fix: FAILCO went bankrupt in 2023 and is
    absent from today's index, but a backtest standing in 2023-01 must
    still see it."""
    members = uni.members_on(connection, INDEX, date(2023, 1, 1))
    assert "FAILCO" in members, "a company removed later must be visible at an earlier date"
    assert "WINNER" not in members, "a company added later must NOT be visible at an earlier date"
    assert "OLDCO" in members
    assert "NEWCO" not in members


def test_membership_between_two_events(connection) -> None:
    members = uni.members_on(connection, INDEX, date(2024, 1, 1))
    assert "WINNER" in members     # added 2023-06, so present
    assert "FAILCO" not in members # removed 2023-06
    assert "OLDCO" in members      # not removed until 2025-03
    assert "NEWCO" not in members  # not added until 2025-03


def test_membership_size_is_constant_across_history(connection) -> None:
    """A fixed-size index must hold its size at every date; drift proves
    the event list has gaps."""
    for as_of in (date(2022, 6, 1), date(2023, 1, 1), date(2024, 1, 1),
                  date(2025, 6, 1), SNAPSHOT_DATE):
        assert len(uni.members_on(connection, INDEX, as_of)) == 5, f"size drifted at {as_of}"


def test_validator_passes_on_complete_event_data(connection) -> None:
    report = uni.validate_membership_series(
        connection, INDEX,
        [date(2022, 6, 1), date(2023, 1, 1), date(2024, 1, 1), date(2025, 6, 1)],
    )
    assert report["valid"], report["failures"]
    assert report["dates_checked"] == 4


def test_validator_detects_an_incomplete_event_list() -> None:
    """Delete one event and the reconstructed size must drift, and the
    validator must catch it rather than accepting a short universe."""
    con = duckdb.connect(":memory:")
    try:
        uni.create_universe_schema(con)
        uni.store_snapshot(con, INDEX, SNAPSHOT_DATE, SNAPSHOT_MEMBERS, SOURCE, SOURCE_URL)
        # only the addition half of the 2023 change is recorded -- the
        # matching removal of FAILCO is missing
        uni.store_events(con, [
            uni.ConstituentEvent(INDEX, date(2023, 6, 1), "WINNER", "Winner Corp", None, None, "partial record"),
            EVENTS[1],
        ], SOURCE, SOURCE_URL)
        uni.EXPECTED_SIZE_BY_INDEX[INDEX] = (5, 5)

        report = uni.validate_membership_series(con, INDEX, [date(2023, 1, 1)])
        assert not report["valid"], "validator accepted an index that shrank to 4 members"
        assert "outside expected range" in report["failures"][0]["error"]
    finally:
        uni.EXPECTED_SIZE_BY_INDEX.pop(INDEX, None)
        con.close()


def test_inconsistent_event_raises_rather_than_guessing(connection) -> None:
    """Undoing the addition of a ticker that is not a member means the
    data is wrong; that must raise, not produce a plausible universe."""
    uni.store_events(connection, [
        uni.ConstituentEvent(INDEX, date(2025, 8, 1), "GHOST", "Ghost Inc", None, None, "never actually present"),
    ], SOURCE, SOURCE_URL)

    with pytest.raises(uni.MembershipReconstructionError, match="cannot undo"):
        uni.members_on(connection, INDEX, date(2024, 1, 1))


def test_date_after_snapshot_refuses_to_extrapolate(connection) -> None:
    with pytest.raises(uni.MembershipReconstructionError, match="after the snapshot date"):
        uni.members_on(connection, INDEX, date(2026, 6, 1))


def test_empty_snapshot_is_rejected(connection) -> None:
    with pytest.raises(ValueError, match="empty membership snapshot"):
        uni.store_snapshot(connection, "EMPTY", SNAPSHOT_DATE, [], SOURCE, SOURCE_URL)


def test_event_that_does_nothing_is_rejected(connection) -> None:
    with pytest.raises(ValueError, match="neither adds nor removes"):
        uni.store_events(connection, [
            uni.ConstituentEvent(INDEX, date(2024, 5, 1), None, None, None, None, "no-op"),
        ], SOURCE, SOURCE_URL)


def test_survivorship_report_quantifies_the_bias(connection) -> None:
    report = uni.survivorship_report(connection, INDEX, date(2023, 1, 1))
    assert report["members_at_as_of"] == 5
    assert set(report["departed_since"]) == {"FAILCO", "OLDCO"}
    assert report["departed_count"] == 2
    assert report["survivorship_bias_pct"] == 40.0


def test_provenance_is_mandatory_on_every_row(connection) -> None:
    """An index-membership claim with no source is useless for a
    point-in-time backtest."""
    for table in ("index_membership_snapshot", "index_constituent_events"):
        nulls = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE source IS NULL OR source_url IS NULL OR retrieved_at IS NULL"
        ).fetchone()[0]
        assert nulls == 0, f"{table} has rows without provenance"
