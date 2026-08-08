"""tests/test_universe_point_in_time.py -- data-integrity and query
tests for the point-in-time former-Nasdaq-100-constituent dataset."""

from __future__ import annotations

import datetime

from stock_agent.universe.point_in_time import (
    FORMER_NASDAQ100_CONSTITUENTS,
    constituents_removed_before,
    cross_checked_only,
)


def test_dataset_is_nonempty_and_covers_the_backtest_window() -> None:
    assert len(FORMER_NASDAQ100_CONSTITUENTS) >= 5
    for c in FORMER_NASDAQ100_CONSTITUENTS:
        removed = datetime.date.fromisoformat(c.removed_date)
        assert datetime.date(2020, 1, 1) <= removed <= datetime.date(2027, 1, 1)


def test_every_ticker_unique_and_uppercase() -> None:
    tickers = [c.ticker for c in FORMER_NASDAQ100_CONSTITUENTS]
    assert len(tickers) == len(set(tickers))
    assert all(t == t.upper() for t in tickers)


def test_every_row_has_disclosed_provenance() -> None:
    for c in FORMER_NASDAQ100_CONSTITUENTS:
        assert c.source, f"{c.ticker} missing a source"
        if c.cross_checked:
            assert c.cross_check_source, f"{c.ticker} marked cross_checked but has no cross_check_source"


def test_constituents_removed_before_is_a_date_filtered_subset() -> None:
    all_tickers = {c.ticker for c in FORMER_NASDAQ100_CONSTITUENTS}
    early = constituents_removed_before("2021-01-01")
    late = constituents_removed_before("2027-01-01")

    assert {c.ticker for c in early}.issubset(all_tickers)
    assert {c.ticker for c in late} == all_tickers
    assert len(early) < len(late)
    assert all(c.removed_date <= "2021-01-01" for c in early)


def test_cross_checked_only_is_a_strict_subset() -> None:
    cross_checked = cross_checked_only()
    assert cross_checked, "expected at least one independently cross-checked row"
    assert all(c.cross_checked for c in cross_checked)
    assert len(cross_checked) <= len(FORMER_NASDAQ100_CONSTITUENTS)
