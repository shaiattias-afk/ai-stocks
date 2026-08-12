"""Council Proposal 1, step 1 (2026-08-11/12 session): honest confidence
interval for the P/E-vs-5-year-excess-return finding (-0.247, n=102),
via a block bootstrap resampled by TICKER -- so a company that happens
to appear more than once in this small sample can't be double-counted
as if it were several independent observations.

Also reports the FY2021-only subset (24 company-years) as a direct,
non-bootstrapped sanity check per the user's own suggestion: what does
the correlation look like on JUST the one cohort that is not entangled
with the immediate 2020 pandemic-crash/recovery window (still a very
small, likely noisy sample -- reported honestly as such, not as a
second validated finding).

READ-ONLY. Writes one result JSON.
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.model_v2_candidate import _current_year_pe
from stock_agent.scoring.predictive_analysis_v1 import spearman_correlation

SOURCE_PATH = DATA_DIR / "scoring_model_5yr_predictive_analysis_result.json"
RESULT_PATH = DATA_DIR / "pe_finding_cohort_robustness_result.json"


def main() -> None:
    dataset = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))["dataset"]
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    enriched = []
    for r in dataset:
        pe = _current_year_pe(connection, r["ticker"], r["fiscal_year"])
        enriched.append({**r, "raw_pe": pe})
    connection.close()

    print("=" * 92)
    print("1) Block bootstrap (grouped by TICKER), full sample, raw P/E vs 5yr annualized excess return")
    print("=" * 92)
    bootstrap_result = block_bootstrap_correlation(
        enriched, "raw_pe", "annualized_excess_return", group_key="ticker", n_resamples=5000, seed=42
    )
    print(json.dumps(bootstrap_result, indent=2))
    if bootstrap_result["ci_95_crosses_zero"]:
        print("\n-> The 95% interval CROSSES ZERO: the correlation could plausibly be zero or even positive")
        print("   if we'd drawn a slightly different (but equally plausible) sample. Not yet a validated signal.")
    else:
        print("\n-> The 95% interval does NOT cross zero: the negative relationship holds up even")
        print("   after accounting for companies/rows not being fully independent.")

    print("\n" + "=" * 92)
    print("2) FY2021-only subset (24 company-years, excludes the FY2020 cohort) -- plain correlation, no bootstrap")
    print("=" * 92)
    fy2021_rows = [r for r in enriched if r["fiscal_year"] == 2021 and r.get("raw_pe") is not None]
    fy2021_pairs = [(r["raw_pe"], r["annualized_excess_return"]) for r in fy2021_rows]
    fy2021_correlation = spearman_correlation(fy2021_pairs)
    print(f"n = {len(fy2021_pairs)}")
    print(f"Spearman correlation = {fy2021_correlation}" if fy2021_correlation is not None else "N/A")
    for r in sorted(fy2021_rows, key=lambda r: r["annualized_excess_return"]):
        print(f"  {r['ticker']:<6} P/E={r['raw_pe']:.0f}x  annualized_excess={r['annualized_excess_return']:+.1%}")

    payload = {
        "block_bootstrap_full_sample": bootstrap_result,
        "fy2021_only_subset": {
            "n": len(fy2021_pairs), "spearman_correlation": fy2021_correlation,
            "rows": fy2021_rows,
        },
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
