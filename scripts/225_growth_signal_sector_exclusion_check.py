"""User-directed follow-up (2026-08-13 session) to the council review
(D-083): the accountant/valuation advisor flagged that the growth>20%
signal (D-079/D-080) might partly be re-expressing concentrated exposure
to the 2023-2025 AI/semiconductor rally rather than a sector-neutral
fundamental edge. This was the council's top-priority, cheapest next
step -- not yet run.

Two exclusion cuts, both applied to the exact same dataset and exact same
block-bootstrap procedure D-079/D-080/the placebo test (scripts/217,
223) already used, so the result is directly comparable:

1. **Official-SIC cut** (objective, mechanical): pulls each ticker's own
   SIC code from SEC's own submissions record (data.sec.gov -- source of
   truth, not a guess), excludes SIC 3674 "Semiconductors & Related
   Devices" -- the literal semiconductor-manufacturer classification.
2. **Named-flag cut** (matches the specific concern raised in council):
   excludes RKLB, MU, STX, APP -- the four names D-080 itself identified
   as skewing quintile 5's average, and that the accountant advisor named
   explicitly in D-083.

READ-ONLY relative to production. Makes read-only calls to SEC's public
submissions API (rate-limited, same client as scripts/filings/download.py)
to look up SIC codes -- no filing content downloaded, nothing written to
production. Writes one local result JSON.

    .venv\\Scripts\\python.exe scripts\\225_growth_signal_sector_exclusion_check.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.filings.download import SEC_DATA_URL, _sec_get_json
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation

SOURCE_PATH = DATA_DIR / "quarterly_growth_rate_regime_and_quintile_check_result.json"
RESULT_PATH = DATA_DIR / "growth_signal_sector_exclusion_check_result.json"

SEMICONDUCTOR_SIC = "3674"
NAMED_FLAGGED_TICKERS = {"RKLB", "MU", "STX", "APP"}


def _sic_lookup(connection: duckdb.DuckDBPyConnection, tickers: set[str]) -> dict[str, dict]:
    cik_by_ticker = dict(connection.execute(
        f"SELECT ticker, cik FROM companies WHERE ticker IN ({','.join('?' * len(tickers))})",
        list(tickers),
    ).fetchall())
    out = {}
    for ticker in sorted(tickers):
        cik = cik_by_ticker.get(ticker)
        if cik is None:
            out[ticker] = {"sic": None, "sic_description": None, "error": "no CIK on file"}
            continue
        try:
            submissions = _sec_get_json(f"{SEC_DATA_URL}/submissions/CIK{int(cik):010d}.json")
            out[ticker] = {"sic": submissions.get("sic"), "sic_description": submissions.get("sicDescription")}
        except Exception as error:  # noqa: BLE001
            out[ticker] = {"sic": None, "sic_description": None, "error": str(error)}
    return out


def _run_bootstrap(dataset: list[dict], label: str) -> dict:
    n_groups = len({r["ticker"] for r in dataset})
    result = block_bootstrap_correlation(
        dataset, "current_yoy_growth", "excess_return", group_key="ticker", n_resamples=5000, seed=23,
    )
    flag = "CROSSES ZERO" if result["ci_95_crosses_zero"] else "does NOT cross zero"
    print(f"{label:<32} n={len(dataset):<5} groups={n_groups:<4} "
          f"corr={result['observed_correlation']:+.3f}  "
          f"CI=[{result['ci_95_low']:+.3f}, {result['ci_95_high']:+.3f}]  {flag}")
    return {"n": len(dataset), "n_groups": n_groups, "bootstrap": result}


def main() -> None:
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    dataset = source["dataset"]
    all_tickers = {r["ticker"] for r in dataset}
    print(f"baseline dataset: {len(dataset)} rows, {len(all_tickers)} tickers "
          f"(reused unchanged from D-079/D-080, scripts/218)\n")

    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    print("looking up official SIC codes from SEC submissions records...")
    sic_by_ticker = _sic_lookup(connection, all_tickers)
    connection.close()

    semiconductor_tickers = {t for t, info in sic_by_ticker.items() if info.get("sic") == SEMICONDUCTOR_SIC}
    print(f"\nSIC {SEMICONDUCTOR_SIC} (Semiconductors & Related Devices) in this universe: "
          f"{sorted(semiconductor_tickers)}\n")

    print("=" * 100)
    print("Baseline vs two exclusion cuts (12-quarter lookback / 12-month horizon, D-079's own baseline cell)")
    print("=" * 100)
    baseline_result = _run_bootstrap(dataset, "BASELINE (all 86 tickers)")

    sic_excluded_dataset = [r for r in dataset if r["ticker"] not in semiconductor_tickers]
    sic_result = _run_bootstrap(sic_excluded_dataset, f"excl. SIC {SEMICONDUCTOR_SIC} semiconductors")

    named_excluded_dataset = [r for r in dataset if r["ticker"] not in NAMED_FLAGGED_TICKERS]
    named_result = _run_bootstrap(named_excluded_dataset, "excl. RKLB/MU/STX/APP (named)")

    both_excluded = {t for t in all_tickers if t in semiconductor_tickers or t in NAMED_FLAGGED_TICKERS}
    both_dataset = [r for r in dataset if r["ticker"] not in both_excluded]
    both_result = _run_bootstrap(both_dataset, "excl. SIC semiconductors + named")

    print(f"\nSIC-semiconductor exclusion removes {len(semiconductor_tickers)} tickers, "
          f"{len(dataset) - len(sic_excluded_dataset)} rows.")
    print(f"Named exclusion removes {len(NAMED_FLAGGED_TICKERS & all_tickers)} tickers, "
          f"{len(dataset) - len(named_excluded_dataset)} rows.")

    RESULT_PATH.write_text(json.dumps({
        "sic_by_ticker": sic_by_ticker,
        "semiconductor_sic_code": SEMICONDUCTOR_SIC,
        "semiconductor_tickers_found": sorted(semiconductor_tickers),
        "named_flagged_tickers": sorted(NAMED_FLAGGED_TICKERS),
        "baseline": baseline_result,
        "excl_sic_semiconductors": sic_result,
        "excl_named": named_result,
        "excl_sic_and_named": both_result,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
