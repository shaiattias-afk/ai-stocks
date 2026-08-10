"""Selects the full survivorship-free cohort: every company that was in
the Nasdaq-100 at ANY point across the backtest window.

Not "today's 100". A backtest standing in 2022 must be able to consider
every company that was in the index in 2022, including the ones later
removed -- so the universe is the UNION of membership across the window,
not a snapshot of the survivors.

Then discovers which annual filings exist for each, reporting coverage
gaps (acquired mid-window, foreign filer using 20-F, no XBRL) rather
than silently dropping them.

    .venv\\Scripts\\python.exe scripts\\190_select_full_universe.py
"""

from __future__ import annotations

import json
from datetime import date

import duckdb

from stock_agent import DATA_DIR
from stock_agent.filings.download import SecDownloadError, discover_annual_filings
from stock_agent.universe import constituents as uni

UNIVERSE_DB_PATH = DATA_DIR / "database" / "universe.duckdb"
COHORT_PATH = DATA_DIR / "full_universe_cohort.json"
DISCOVERY_PATH = DATA_DIR / "full_universe_discovery.json"

WINDOW_START = date(2020, 1, 1)
WINDOW_END = date(2026, 12, 31)
# one membership observation per year-start across the window
OBSERVATION_DATES = [date(year, 1, 1) for year in range(2021, 2027)]


def main() -> None:
    connection = duckdb.connect(str(UNIVERSE_DB_PATH), read_only=True)
    try:
        snapshot_date, current = uni.load_snapshot(connection, uni.NASDAQ_100)
        union: set[str] = set(current)
        per_date: dict[str, int] = {}
        for as_of in OBSERVATION_DATES:
            members = uni.members_on(connection, uni.NASDAQ_100, as_of)
            per_date[as_of.isoformat()] = len(members)
            union |= members

        events = uni.load_events(connection, uni.NASDAQ_100)
    finally:
        connection.close()

    names = {}
    for event in events:
        if event.added_ticker and event.added_company:
            names.setdefault(event.added_ticker, event.added_company)
        if event.removed_ticker and event.removed_company:
            names.setdefault(event.removed_ticker, event.removed_company)

    connection = duckdb.connect(str(UNIVERSE_DB_PATH), read_only=True)
    for ticker, company in connection.execute(
        "SELECT ticker, company_name FROM index_membership_snapshot WHERE index_name = ?",
        [uni.NASDAQ_100],
    ).fetchall():
        names.setdefault(ticker, company)
    connection.close()

    print("=" * 92)
    print("FULL SURVIVORSHIP-FREE UNIVERSE")
    print("=" * 92)
    for as_of, count in per_date.items():
        print(f"   members on {as_of}: {count}")
    print(f"   current snapshot ({snapshot_date}): {len(current)}")
    print()
    print(f"UNION across the window: {len(union)} companies")
    print(f"   still in the index today : {len(union & current)}")
    print(f"   departed at some point   : {len(union - current)}")

    cohort = sorted(union)
    COHORT_PATH.write_text(json.dumps({
        "window": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
        "observation_dates": [d.isoformat() for d in OBSERVATION_DATES],
        "members_per_date": per_date,
        "snapshot_date": snapshot_date.isoformat(),
        "universe_size": len(union),
        "still_current": sorted(union & current),
        "departed": sorted(union - current),
        "cohort": [{"ticker": t, "company": names.get(t, ""),
                    "in_index_today": t in current} for t in cohort],
    }, indent=2), encoding="utf-8")
    print(f"\ncohort written: {COHORT_PATH}")

    print("\n" + "=" * 92)
    print("DISCOVERY (read-only): which annual filings exist for each")
    print("=" * 92)

    results, problems = [], []
    for index, ticker in enumerate(cohort, start=1):
        try:
            report = discover_annual_filings(
                ticker, WINDOW_START, WINDOW_END,
                company_name_hint=names.get(ticker))
        except SecDownloadError as error:
            problems.append({"ticker": ticker, "company": names.get(ticker, ""),
                             "error": str(error)[:220]})
            print(f"[{index:>3}/{len(cohort)}] {ticker:<6} LOOKUP FAILED")
            continue
        report["filings"] = [f.__dict__ for f in report["filings"]]
        report["in_index_today"] = ticker in current
        results.append(report)
        note = ""
        if report["filing_count"] == 0 and report["foreign_annual_forms_present"]:
            note = f"  files {report['foreign_annual_forms_present']} instead"
        elif report["filing_count"] < 5:
            note = f"  only {report['filing_count']}"
        print(f"[{index:>3}/{len(cohort)}] {ticker:<6} {report['filing_count']:>2} filings{note}")

    total = sum(r["filing_count"] for r in results)
    zero = [r["ticker"] for r in results if r["filing_count"] == 0]
    print()
    print(f"companies resolved : {len(results)} / {len(cohort)}")
    print(f"lookup failures    : {len(problems)}  {[p['ticker'] for p in problems]}")
    print(f"zero filings       : {len(zero)}  {zero}")
    print(f"TOTAL annual filings to fetch: {total}")

    DISCOVERY_PATH.write_text(json.dumps({
        "window": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
        "universe_size": len(cohort), "companies_resolved": len(results),
        "total_annual_filings": total, "lookup_failures": problems,
        "zero_filing_tickers": zero, "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nwritten: {DISCOVERY_PATH}")


if __name__ == "__main__":
    main()
