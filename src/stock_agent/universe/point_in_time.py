"""Point-in-time Nasdaq-100 constituent-removal data -- the piece needed
to fix the project's current survivorship-bias gap (docs/PROJECT_CONTEXT.md:
the 9-company universe was picked in 2026 as known-current winners; none
of them ever left an index, so any model validated on that sample
measures its own selection, not predictive power).

## Provenance and confidence (disclosed, not asserted as fact)
This dataset was compiled by researching Wikipedia's "List of NASDAQ-100
companies" article, specifically its "Historical components" table
(Date / Added / Removed / Reason), which covers every index change since
1985. That table is CROWDSOURCED, not an official Nasdaq feed --
`source="wikipedia_historical_components_table"` on every row reflects
this honestly. A subset of rows were independently cross-checked against
a primary source (an investor-relations press release, dedicated news
coverage, or another data provider's own documentation naming the same
ticker/date) and are marked `cross_checked=True` with the corroborating
source recorded in `cross_check_source`; the rest are `cross_checked=False`
-- Wikipedia-only, not yet independently verified. Before using any row
here to gate a real financial decision, prefer confirming the exact
effective date against Nasdaq's own "Annual Changes to the Nasdaq-100
Index" / ad-hoc replacement press releases (ir.nasdaq.com, GlobeNewswire).

## What this data IS and IS NOT
This is a REMOVAL-event list (former Nasdaq-100 constituents and why/when
they left), not a full historical membership snapshot for the whole
index at every date -- no free source for that was found (see the
point-in-time-universe research this module is built from). It is
exactly the piece this project's stated goal needs: companies that
FAILED or were REMOVED, to add to the universe alongside still-successful
names, so a backtest stops only ever seeing survivors.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FormerConstituent:
    ticker: str
    company_name: str
    removed_date: str  # ISO date, Nasdaq-100 index removal date (not necessarily the delisting date)
    reason: str
    outcome: str  # "ACQUIRED", "TAKEN_PRIVATE", "INDEX_WEIGHT_DROP" (still trading), etc.
    source: str
    cross_checked: bool
    cross_check_source: str | None = None


# Confirmed former Nasdaq-100 constituents removed 2020-2026, researched
# from Wikipedia's "List of NASDAQ-100 companies" historical-components
# table. See module docstring for the confidence disclosure.
FORMER_NASDAQ100_CONSTITUENTS: list[FormerConstituent] = [
    FormerConstituent(
        ticker="CTXS", company_name="Citrix Systems Inc",
        removed_date="2020-12-21", reason="Index reconstitution (replaced by MRVL)",
        outcome="TAKEN_PRIVATE",  # went private via Vista/Evergreen ~Sept 2022, ~2 yrs after index removal
        source="wikipedia_historical_components_table", cross_checked=False,
    ),
    FormerConstituent(
        ticker="ALXN", company_name="Alexion Pharmaceuticals Inc",
        removed_date="2021-07-21", reason="Acquired by AstraZeneca",
        outcome="ACQUIRED", source="wikipedia_historical_components_table", cross_checked=False,
    ),
    FormerConstituent(
        ticker="MXIM", company_name="Maxim Integrated Products Inc",
        removed_date="2021-08-26", reason="Acquired by Analog Devices",
        outcome="ACQUIRED", source="wikipedia_historical_components_table",
        cross_checked=True, cross_check_source="investor.analog.com press release",
    ),
    FormerConstituent(
        ticker="CERN", company_name="Cerner Corp",
        removed_date="2021-12-20", reason="Ahead of Oracle acquisition (closed 2022-06)",
        outcome="ACQUIRED", source="wikipedia_historical_components_table", cross_checked=False,
    ),
    FormerConstituent(
        ticker="XLNX", company_name="Xilinx Inc",
        removed_date="2022-02-22", reason="Acquired by AMD",
        outcome="ACQUIRED", source="wikipedia_historical_components_table", cross_checked=False,
    ),
    FormerConstituent(
        ticker="RIVN", company_name="Rivian Automotive Inc",
        removed_date="2023-06-20", reason="Fell below minimum index weight (still trading)",
        outcome="INDEX_WEIGHT_DROP", source="wikipedia_historical_components_table", cross_checked=False,
    ),
    FormerConstituent(
        ticker="ATVI", company_name="Activision Blizzard Inc",
        removed_date="2023-07-17", reason="Ahead of Microsoft acquisition (closed 2023-10-13)",
        outcome="ACQUIRED", source="wikipedia_historical_components_table",
        cross_checked=True, cross_check_source="gaming press coverage + EODHD delisted-data docs (names ATVI.US)",
    ),
    FormerConstituent(
        ticker="SGEN", company_name="Seagen Inc",
        removed_date="2023-12-14", reason="Acquired by Pfizer",
        outcome="ACQUIRED", source="wikipedia_historical_components_table", cross_checked=False,
    ),
    FormerConstituent(
        ticker="SPLK", company_name="Splunk Inc",
        removed_date="2024-03-18", reason="Acquired by Cisco",
        outcome="ACQUIRED", source="wikipedia_historical_components_table", cross_checked=False,
    ),
    FormerConstituent(
        ticker="ANSS", company_name="Ansys Inc",
        removed_date="2025-07-17", reason="Acquired by Synopsys",
        outcome="ACQUIRED", source="wikipedia_historical_components_table", cross_checked=False,
    ),
    FormerConstituent(
        ticker="EA", company_name="Electronic Arts Inc",
        removed_date="2026-08-04", reason="Taken private ($55B PIF/Silver Lake/Affinity Partners buyout)",
        outcome="TAKEN_PRIVATE", source="wikipedia_historical_components_table",
        cross_checked=True, cross_check_source="ea.com press release; gameinformer.com; wccftech.com (Aug 2026)",
    ),
]


def constituents_removed_before(as_of_date: str) -> list[FormerConstituent]:
    """Former constituents already removed as of `as_of_date` (ISO date)
    -- i.e. names that would NOT appear in a naive "current Nasdaq-100"
    universe query run today, but WERE index members (and, for the
    ACQUIRED/TAKEN_PRIVATE ones, real trading companies with real price
    history) at some point in this project's 2020-2026 backtest window."""
    return [c for c in FORMER_NASDAQ100_CONSTITUENTS if c.removed_date <= as_of_date]


def cross_checked_only() -> list[FormerConstituent]:
    """The subset independently corroborated beyond the Wikipedia table
    alone -- the safer subset to prioritize for real data ingestion when
    provenance confidence matters more than sample size."""
    return [c for c in FORMER_NASDAQ100_CONSTITUENTS if c.cross_checked]
