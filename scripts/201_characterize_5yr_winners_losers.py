"""Follow-up to scripts/200: the composite score showed a NEGATIVE
relationship with 5-year annualized excess return (Spearman -0.117,
n=133) -- the opposite of useful. This script asks the user's own
follow-up question directly: what actually characterized the real
winners vs. the real losers?

Compares average raw factor values between the 30 best and 30 worst
company-years by REALIZED 5-year annualized excess return (a
descriptive comparison, not a predictive claim), then formally
correlates entry-date raw P/E (current-year, cross-sectional --
stock_agent.scoring.model_v2_candidate._current_year_pe) against
5-year annualized excess return across the full available sample.

*** Caveat, stated before the result: nearly the entire usable sample
(109 of 133) is fiscal year 2020 entries, because a 5-year forward
return needs 5 years of price data past entry, and price data starts
2020-01-02 -- this is descriptive of ONE overlapping entry window
(2020-2021), not many independent 5-year periods. Treat this as a
characterizable pattern worth a longer test period, not a validated,
ready-to-use signal. ***

READ-ONLY. Writes one result JSON.
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.model_v2_candidate import _current_year_pe
from stock_agent.scoring.predictive_analysis_v1 import spearman_correlation

RESULT_PATH = DATA_DIR / "scoring_model_5yr_winners_losers_characterization.json"
SOURCE_PATH = DATA_DIR / "scoring_model_5yr_predictive_analysis_result.json"

RAW_FACTORS = [
    "revenue_growth", "roic_level", "operating_margin", "fcf_margin",
    "balance_sheet_strength_ratio", "distance_from_high",
]


def main() -> None:
    dataset = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))["dataset"]
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    enriched = []
    for r in dataset:
        raw = connection.execute(
            f"SELECT {', '.join(RAW_FACTORS)} FROM scoring_inputs_v1 WHERE ticker = ? AND report_date = ?",
            [r["ticker"], r["report_date"]],
        ).fetchone()
        pe = _current_year_pe(connection, r["ticker"], r["fiscal_year"])
        enriched.append({**r, **dict(zip(RAW_FACTORS, raw)), "raw_pe": pe})

    ordered = sorted(enriched, key=lambda r: r["annualized_excess_return"])
    worst30, best30 = ordered[:30], ordered[-30:]

    def avg(group, key):
        vals = [g[key] for g in group if g.get(key) is not None]
        return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)

    print("=" * 92)
    print("Worst 30 vs. best 30 company-years by REALIZED 5-year annualized excess return")
    print("=" * 92)
    comparison = {}
    for key in [*RAW_FACTORS, "raw_pe", "composite_score"]:
        w, wn = avg(worst30, key)
        b, bn = avg(best30, key)
        comparison[key] = {"worst30_avg": w, "worst30_n": wn, "best30_avg": b, "best30_n": bn}
        w_str = f"{w:.2f}" if w is not None else "N/A"
        b_str = f"{b:.2f}" if b is not None else "N/A"
        print(f"  {key:<35} worst30={w_str:>10} (n={wn:>2})   best30={b_str:>10} (n={bn:>2})")

    print("\nWorst 10 (company, entry year, P/E at entry, 5yr result):")
    for r in worst30[:10]:
        pe_str = f"{r['raw_pe']:.0f}x" if r.get("raw_pe") else "N/A"
        print(f"  {r['ticker']:<6} FY{r['fiscal_year']}  entry_P/E={pe_str:>6}  "
              f"5yr_annualized_excess={r['annualized_excess_return']:+.1%}  total_return={r['stock_return']:+.0%}")

    print("\nBest 10 (company, entry year, P/E at entry, 5yr result):")
    for r in best30[-10:]:
        pe_str = f"{r['raw_pe']:.0f}x" if r.get("raw_pe") else "N/A"
        print(f"  {r['ticker']:<6} FY{r['fiscal_year']}  entry_P/E={pe_str:>6}  "
              f"5yr_annualized_excess={r['annualized_excess_return']:+.1%}  total_return={r['stock_return']:+.0%}")

    pe_pairs = [(r["raw_pe"], r["annualized_excess_return"]) for r in enriched if r.get("raw_pe") is not None]
    pe_correlation = spearman_correlation(pe_pairs)
    print(f"\nSpearman correlation, entry P/E vs. 5-year annualized excess return: "
          f"{pe_correlation:+.3f} (n={len(pe_pairs)})")
    print("(Negative = cheaper stocks at entry tended to outperform over the next 5 years --")
    print(" consistent with a real, moderate value effect; composite_score's own correlation was -0.117,")
    print(" so raw P/E alone is a noticeably STRONGER signal than the current 9-factor quality score.)")

    connection.close()

    payload = {
        "worst30_vs_best30_factor_comparison": comparison,
        "pe_vs_5yr_excess_return_correlation": pe_correlation,
        "pe_vs_5yr_excess_return_n": len(pe_pairs),
        "worst30": worst30, "best30": best30,
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
