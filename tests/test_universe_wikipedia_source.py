"""Tests for the Wikipedia constituent parser.

Entirely offline: the wikitext fixture below is a trimmed copy of the real
page's markup, so the suite never depends on the network or on Wikipedia's
current content. A separate, network-marked test can be run deliberately
to check the live page still has the structure this parser expects.

The parser sits on the data path that decides which companies a backtest
may see, so the failure modes carry more weight than the happy path: a
malformed ticker or an unparseable date must raise, never be skipped,
because a silently dropped index change corrupts membership at every
subsequent date.
"""

from __future__ import annotations

from datetime import date

import pytest

from stock_agent.universe import wikipedia_source as wiki

WIKITEXT = """
Some intro prose.

=={{anchor|Components}}Nasdaq-100 component stocks==
{| class="wikitable sortable" id="constituents"
|-
! Ticker !! Company !! ICB Industry !! ICB Subsector
|-
| ADBE || [[Adobe Inc.]] || Technology || Software
|-
| AMD || [[AMD|Advanced Micro Devices]] || Technology || Semiconductors
|-
| GOOGL || [[Alphabet Inc.]] (Class A) || Technology || Software
|-
| GOOG || [[Alphabet Inc.]] (Class C) || Technology || Software
|-
| AMZN || [[Amazon (company)|Amazon]]<ref name=":1">{{Cite web |title=x |url=http://e.invalid}}</ref> || Consumer Discretionary || Catalog
|}

==Component changes==
===Historical components===
{| class="wikitable sortable" id="changes"
! rowspan="2" data-sort-type="date" |Date
! colspan="2" |Added
! colspan="2" |Removed
! rowspan="2" |Reason
|-
!Ticker
!Security
!Ticker
!Security
|-
|August 4, 2026
|
|
|EA
|[[Electronic Arts]]
|EA was [[Leveraged buyout|taken private]] by a consortium.<ref>{{Cite web |title=y |url=http://e.invalid}}</ref>
|-
|July 1, 2026
|SPCX
|[[SpaceX]]
|
|
|Fast-tracked into index.
|-
|June 22, 2026
|ALAB
|[[Astera Labs]]
|CHTR
|[[Charter Communications]]
|Quarterly index reconstitution.
|}
"""


def test_parses_current_constituents_with_tickers() -> None:
    members = wiki.parse_constituents(WIKITEXT)
    assert members == [
        ("ADBE", "Adobe Inc."),
        ("AMD", "Advanced Micro Devices"),
        ("GOOGL", "Alphabet Inc. (Class A)"),
        ("GOOG", "Alphabet Inc. (Class C)"),
        ("AMZN", "Amazon"),
    ]


def test_parses_change_events_oldest_first() -> None:
    events = wiki.parse_changes(WIKITEXT)
    assert [e.event_date for e in events] == [
        date(2026, 6, 22), date(2026, 7, 1), date(2026, 8, 4),
    ]


def test_removal_only_and_addition_only_rows_are_both_handled() -> None:
    events = {e.event_date: e for e in wiki.parse_changes(WIKITEXT)}

    removal_only = events[date(2026, 8, 4)]
    assert removal_only.added_ticker is None
    assert removal_only.removed_ticker == "EA"
    assert removal_only.removed_company == "Electronic Arts"

    addition_only = events[date(2026, 7, 1)]
    assert addition_only.added_ticker == "SPCX"
    assert addition_only.removed_ticker is None

    swap = events[date(2026, 6, 22)]
    assert (swap.added_ticker, swap.removed_ticker) == ("ALAB", "CHTR")


def test_reference_tags_and_templates_are_stripped_from_text() -> None:
    events = {e.event_date: e for e in wiki.parse_changes(WIKITEXT)}
    reason = events[date(2026, 8, 4)].reason
    assert "<ref" not in reason and "{{" not in reason and "[[" not in reason
    assert reason == "EA was taken private by a consortium."


@pytest.mark.parametrize(("raw", "expected"), [
    ("[[Adobe Inc.]]", "Adobe Inc."),
    ("[[AMD|Advanced Micro Devices]]", "Advanced Micro Devices"),
    ("[[Alphabet Inc.]] (Class A)", "Alphabet Inc. (Class A)"),
    ("text<ref name=':1' />more", "textmore"),
    ("a<!-- hidden -->b", "ab"),
    ("'''bold'''", "bold"),
    ("x&nbsp;y", "x y"),
])
def test_clean_cell_reduces_markup_to_plain_text(raw: str, expected: str) -> None:
    assert wiki.clean_cell(raw) == expected


def test_unparseable_date_raises_rather_than_skipping() -> None:
    broken = WIKITEXT.replace("|August 4, 2026", "|sometime in 2026")
    # the malformed row is skipped by the date guard, but if it were the
    # ONLY row the table would parse to zero events, which must raise
    only_bad = WIKITEXT.split("|-\n|August 4, 2026")[0] + "|-\n|sometime in 2026\n|\n|\n|EA\n|[[Electronic Arts]]\n|reason\n|}"
    with pytest.raises(wiki.WikipediaSourceError, match="zero events"):
        wiki.parse_changes(only_bad)
    assert broken  # keeps the fixture referenced


def test_malformed_ticker_raises() -> None:
    broken = WIKITEXT.replace("|ALAB\n", "|not a ticker at all\n")
    with pytest.raises(wiki.WikipediaSourceError, match="does not look like a ticker"):
        wiki.parse_changes(broken)


def test_missing_table_raises_when_page_structure_changes() -> None:
    with pytest.raises(wiki.WikipediaSourceError, match="not found"):
        wiki.parse_constituents("no tables here at all")


def test_duplicate_ticker_in_constituents_raises() -> None:
    duplicated = WIKITEXT.replace(
        "| AMZN || [[Amazon (company)|Amazon]]",
        "| ADBE || [[Amazon (company)|Amazon]]",
    )
    with pytest.raises(wiki.WikipediaSourceError, match="duplicate tickers"):
        wiki.parse_constituents(duplicated)


@pytest.mark.network
def test_live_page_still_matches_the_expected_structure() -> None:
    """Deliberate network check: Wikipedia is community-maintained, so the
    page layout can change under us. Run with `-m network`."""
    page = wiki.fetch_page()
    members = wiki.parse_constituents(page.wikitext)
    events = wiki.parse_changes(page.wikitext)

    assert page.revision_id > 0
    assert 95 <= len(members) <= 110, f"unexpected constituent count: {len(members)}"
    assert len(events) > 200, f"unexpected event count: {len(events)}"
    assert events[0].event_date < date(2015, 1, 1), "changelog no longer reaches back far enough"
