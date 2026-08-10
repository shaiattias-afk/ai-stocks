"""Read-only discovery: for each company in the 25-company pilot cohort,
find which annual filings actually exist in the backtest window.

Downloads nothing. The point is to see the coverage gaps BEFORE spending
40 minutes fetching, because the gaps are the interesting part:
  - a company acquired mid-window stops filing part-way through
  - a foreign private issuer files 20-F, not 10-K
  - a delisted ticker may no longer appear in SEC's ticker index at all

Each of those is a real condition an honest backtest has to represent, so
they are reported explicitly rather than smoothed over.

    .venv\\Scripts\\python.exe scripts\\181_discover_pilot_filings.py
"""

from __future__ import annotations

import json
from datetime import date

from stock_agent import DATA_DIR
from stock_agent.filings.download import SecDownloadError, discover_annual_filings

COHORT_PATH = DATA_DIR / "pilot_cohort_25.json"
RESULT_PATH = DATA_DIR / "pilot_discovery_result.json"

WINDOW_START = date(2020, 1, 1)
WINDOW_END = date(2026, 12, 31)
EXPECTED_ANNUAL_FILINGS = 5


def main() -> None:
    cohort = json.loads(COHORT_PATH.read_text(encoding="utf-8"))["cohort"]
    tickers = [entry["ticker"] for entry in cohort]
    reason_by_ticker = {entry["ticker"]: entry["reason"] for entry in cohort}
    # The company name comes from the index membership data and is what
    # lets a delisted ticker be resolved at all -- SEC's ticker file lists
    # only current registrants.
    name_by_ticker = {entry["ticker"]: entry.get("company", "") for entry in cohort}

    print("=" * 100)
    print(f"DISCOVERY (read-only): {len(tickers)} companies, "
          f"annual filings {WINDOW_START} .. {WINDOW_END}")
    print("=" * 100)
    print(f"{'ticker':<8}{'n':<4}{'reason':<24}{'report dates':<46}notes")
    print("-" * 100)

    results, problems = [], []
    for ticker in tickers:
        try:
            report = discover_annual_filings(
                ticker, WINDOW_START, WINDOW_END,
                company_name_hint=name_by_ticker.get(ticker),
            )
        except SecDownloadError as error:
            problems.append({"ticker": ticker, "error": str(error)})
            print(f"{ticker:<8}{'-':<4}{reason_by_ticker[ticker]:<24}{'':<46}LOOKUP FAILED: {error}")
            continue

        report["filings"] = [f.__dict__ for f in report["filings"]]
        report["cohort_reason"] = reason_by_ticker[ticker]
        results.append(report)

        dates = ", ".join(report["report_dates"])
        notes = []
        if report["filing_count"] < EXPECTED_ANNUAL_FILINGS:
            notes.append(f"only {report['filing_count']} of {EXPECTED_ANNUAL_FILINGS}")
        if report["foreign_annual_forms_present"]:
            notes.append(f"also files {report['foreign_annual_forms_present']}")
        if report["duplicate_report_dates"]:
            notes.append(f"DUPLICATE dates {report['duplicate_report_dates']}")
        print(f"{ticker:<8}{report['filing_count']:<4}{reason_by_ticker[ticker]:<24}"
              f"{dates[:44]:<46}{'; '.join(notes)}")

    total = sum(r["filing_count"] for r in results)
    complete = [r for r in results if r["filing_count"] >= EXPECTED_ANNUAL_FILINGS]
    partial = [r for r in results if 0 < r["filing_count"] < EXPECTED_ANNUAL_FILINGS]
    empty = [r for r in results if r["filing_count"] == 0]

    print()
    print("=" * 100)
    print(f"companies resolved      : {len(results)} / {len(tickers)}")
    print(f"lookup failures         : {len(problems)}")
    print(f"full coverage (>={EXPECTED_ANNUAL_FILINGS})   : {len(complete)}")
    print(f"partial coverage        : {len(partial)}  {[r['ticker'] for r in partial]}")
    print(f"zero filings in window  : {len(empty)}  {[r['ticker'] for r in empty]}")
    print(f"total annual filings    : {total}")
    print("=" * 100)

    if problems:
        print("\nLOOKUP FAILURES (these companies cannot be fetched by ticker):")
        for problem in problems:
            print(f"  {problem['ticker']}: {problem['error']}")

    RESULT_PATH.write_text(json.dumps({
        "window": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
        "expected_annual_filings": EXPECTED_ANNUAL_FILINGS,
        "companies_requested": len(tickers),
        "companies_resolved": len(results),
        "total_annual_filings": total,
        "lookup_failures": problems,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
