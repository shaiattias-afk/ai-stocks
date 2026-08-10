"""Loads Nasdaq-100 point-in-time membership into its own database, then
selects the 25-company pilot cohort FROM that membership data.

Why a separate database: the universe is new data with no bearing on the
frozen result tables, so it is kept out of the production database until
it has earned a place there.

Why derive the cohort from the data rather than typing a list: a
hand-typed ticker list is exactly the selection bias this whole exercise
removes. Every company below is chosen by a stated, mechanical rule
against membership the system reconstructed itself.

Cohort rules:
  1. The 9 companies already loaded -- they act as a control. If their
     numbers change, something broke.
  2. Companies that WERE in the index at the start of the backtest window
     but are NOT in it today -- the departed. These are the ones a
     survivor-only universe hides, and the ones most likely to break the
     pipeline (filings stop mid-window, foreign filers use different
     forms). Deliberately over-weighted in a pilot.
  3. Current members, in the source table's own order (alphabetical by
     company name), to fill the remainder. Not size-ranked -- the source
     carries no market-cap column, and inventing a ranking would be a
     selection choice dressed up as data. For a pipeline pilot the fill
     order is irrelevant; it matters only that it is stated and
     reproducible.

    .venv\\Scripts\\python.exe scripts\\180_load_universe_and_select_25.py
"""

from __future__ import annotations

import json
from datetime import date

import duckdb

from stock_agent import DATA_DIR
from stock_agent.universe import constituents as uni
from stock_agent.universe import wikipedia_source as wiki

UNIVERSE_DB_PATH = DATA_DIR / "database" / "universe.duckdb"
COHORT_PATH = DATA_DIR / "pilot_cohort_25.json"

BACKTEST_WINDOW_START = date(2021, 1, 1)

ALREADY_LOADED = ["ORCL", "MSFT", "META", "NVDA", "GOOGL", "AMZN", "MU", "CRWD", "PANW"]
COHORT_SIZE = 25
MIN_DEPARTED = 4


def main() -> None:
    print("=" * 78)
    print("LOAD POINT-IN-TIME NASDAQ-100 MEMBERSHIP + SELECT 25-COMPANY PILOT")
    print("=" * 78)

    page = wiki.fetch_page()
    members = wiki.parse_constituents(page.wikitext)
    events = wiki.parse_changes(page.wikitext)
    snapshot_date = date.fromisoformat(page.revision_timestamp[:10])

    print(f"source revision : {page.revision_id} ({page.revision_timestamp})")
    print(f"citation        : {page.citation_url}")
    print(f"constituents    : {len(members)}")
    print(f"change events   : {len(events)}  ({events[0].event_date} -> {events[-1].event_date})")

    UNIVERSE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=str(UNIVERSE_DB_PATH))
    try:
        uni.create_universe_schema(connection)
        connection.execute("DELETE FROM index_constituent_events WHERE index_name = ?", [uni.NASDAQ_100])
        uni.store_snapshot(connection, uni.NASDAQ_100, snapshot_date, members,
                           source="wikipedia", source_url=page.citation_url)
        uni.store_events(connection, events, source="wikipedia", source_url=page.citation_url)

        quarter_ends = [
            date(y, m, d)
            for y in range(2020, 2027)
            for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))
            if date(y, m, d) <= snapshot_date
        ]
        report = uni.validate_membership_series(connection, uni.NASDAQ_100, quarter_ends)
        print(f"\nvalidation      : {report['dates_checked']} quarter-ends, "
              f"valid={report['valid']}")
        if not report["valid"]:
            for failure in report["failures"][:10]:
                print("   FAIL:", failure)
            raise SystemExit("membership validation failed -- refusing to select a cohort from unvalidated data")

        # ---- cohort selection -------------------------------------------------
        members_then = uni.members_on(connection, uni.NASDAQ_100, BACKTEST_WINDOW_START)
        members_now = {ticker for ticker, _ in members}
        departed = sorted(members_then - members_now)

        company_names = {ticker: name for ticker, name in members}
        for event in events:
            if event.added_ticker and event.added_company:
                company_names.setdefault(event.added_ticker, event.added_company)
            if event.removed_ticker and event.removed_company:
                company_names.setdefault(event.removed_ticker, event.removed_company)

        survivorship = uni.survivorship_report(connection, uni.NASDAQ_100, BACKTEST_WINDOW_START)
        print(f"\nsurvivorship at {BACKTEST_WINDOW_START}: "
              f"{survivorship['departed_count']} of {survivorship['members_at_as_of']} "
              f"departed ({survivorship['survivorship_bias_pct']}%)")

        cohort: list[dict] = []
        for ticker in ALREADY_LOADED:
            cohort.append({"ticker": ticker, "company": company_names.get(ticker, ""),
                           "reason": "ALREADY_LOADED_CONTROL",
                           "in_index_2021": ticker in members_then,
                           "in_index_today": ticker in members_now})

        chosen = {c["ticker"] for c in cohort}
        for ticker in departed:
            if len(cohort) >= COHORT_SIZE:
                break
            if ticker in chosen:
                continue
            cohort.append({"ticker": ticker, "company": company_names.get(ticker, ""),
                           "reason": "DEPARTED_SINCE_2021",
                           "in_index_2021": True, "in_index_today": False})
            chosen.add(ticker)
            if sum(1 for c in cohort if c["reason"] == "DEPARTED_SINCE_2021") >= MIN_DEPARTED:
                break

        for ticker, _name in members:  # index-table order
            if len(cohort) >= COHORT_SIZE:
                break
            if ticker in chosen:
                continue
            cohort.append({"ticker": ticker, "company": company_names.get(ticker, ""),
                           "reason": "CURRENT_MEMBER",
                           "in_index_2021": ticker in members_then,
                           "in_index_today": True})
            chosen.add(ticker)

        print(f"\n{'ticker':<8}{'in 2021':<9}{'today':<7}{'reason':<24}company")
        print("-" * 78)
        for entry in cohort:
            print(f"{entry['ticker']:<8}{str(entry['in_index_2021']):<9}"
                  f"{str(entry['in_index_today']):<7}{entry['reason']:<24}{entry['company'][:32]}")

        by_reason: dict[str, int] = {}
        for entry in cohort:
            by_reason[entry["reason"]] = by_reason.get(entry["reason"], 0) + 1
        print(f"\ncohort size: {len(cohort)}   breakdown: {by_reason}")

        COHORT_PATH.write_text(json.dumps({
            "selected_at_window_start": BACKTEST_WINDOW_START.isoformat(),
            "source_revision_id": page.revision_id,
            "source_url": page.citation_url,
            "snapshot_date": snapshot_date.isoformat(),
            "members_in_2021": len(members_then),
            "members_today": len(members_now),
            "departed_since_2021": departed,
            "survivorship_bias_pct": survivorship["survivorship_bias_pct"],
            "cohort": cohort,
        }, indent=2), encoding="utf-8")
        print(f"\ncohort written: {COHORT_PATH}")
        print(f"universe database: {UNIVERSE_DB_PATH}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
