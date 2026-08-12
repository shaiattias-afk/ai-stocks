"""User-directed (2026-08-12 session, immediately after D-066): build a
QUARTERLY composite score -- not annual -- on the 5 factors that are
actually computable at quarterly cadence from Quarterly Data V1
(revenue_growth, operating_margin, fcf_margin, fcf_growth,
distance_from_high). Explicitly excludes roic_level/roic_trend/
balance_sheet_strength_ratio -- Quarterly Data V1 (D-042) has no
balance-sheet metrics at quarterly frequency, confirmed by direct query
before building anything (quarterly_metric_results has exactly 6 metric
families: revenue, operating_income, operating_cash_flow, capex,
pretax_income, income_tax_expense).

Run on the last 12 quarters (same cutoff convention as D-065's
quarterly_trend_v1 proof) across the 9 tickers with real Quarterly Data
V1 coverage (10-Q was never locked for the wider 143-company universe).
Horizon: 12 months forward excess return vs QQQ -- chosen, not the 60-
month horizon D-063/D-066 used, because a 5-year-back cap on this
project's backtest windows (docs/PROJECT_CONTEXT.md: "approximately 15
companies over five years") means a quarterly entry point from the last
12 quarters cannot honestly support a 60-month forward return yet (that
would need price data reaching 5 years past a quarter that itself is
already within the last 3 years) -- 12 months is the only horizon
consistent with both constraints, and matches D-065's own choice for
the same reason.

Same validation discipline as every other predictive check this
session: company-grouped block bootstrap (cohort_robustness_v1), not a
naive pooled correlation.

READ-ONLY. Writes one result JSON.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.predictive_analysis_v1 import spearman_correlation
from stock_agent.scoring.quarterly_composite_v1 import compute_quarterly_composite_scores, compute_quarterly_factors

RESULT_PATH = DATA_DIR / "quarterly_composite_predictive_check_result.json"
LOOKBACK_QUARTERS = 12
HORIZON_MONTHS = 12


def _candidate_quarters(connection: duckdb.DuckDBPyConnection, cutoff_date: str) -> list[tuple[str, str, str, str, str]]:
    return connection.execute(
        """
        SELECT DISTINCT qer.ticker, qmr.fiscal_year_end, qmr.fiscal_quarter, qmr.period_end, qmr.availability_date
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qmr.availability_date >= ? AND qmr.metric_name = 'revenue' AND qmr.period_end IS NOT NULL
        ORDER BY qer.ticker, qmr.availability_date
        """,
        [cutoff_date],
    ).fetchall()


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    cutoff_date = (date.today() - timedelta(days=int(LOOKBACK_QUARTERS * 365.25 / 4))).isoformat()
    print(f"lookback cutoff (approx {LOOKBACK_QUARTERS} quarters back): {cutoff_date}")

    quarters = _candidate_quarters(connection, cutoff_date)
    print(f"candidate quarterly entry points: {len(quarters)}")
    print(f"distinct tickers: {sorted({t for t, *_ in quarters})}")

    factor_rows = [
        compute_quarterly_factors(connection, ticker, fiscal_year_end, fiscal_quarter, period_end, availability_date)
        for ticker, fiscal_year_end, fiscal_quarter, period_end, availability_date in quarters
    ]

    print("\nCross-sectional group sizes by calendar quarter:")
    group_sizes: dict[str, int] = {}
    for row in factor_rows:
        group_sizes[row["calendar_quarter_key"]] = group_sizes.get(row["calendar_quarter_key"], 0) + 1
    for key in sorted(group_sizes):
        print(f"  {key}: {group_sizes[key]} companies")

    scored = compute_quarterly_composite_scores(factor_rows)

    dataset = []
    n_no_score = 0
    n_no_return = 0
    for row in scored:
        if row["composite_score"] is None:
            n_no_score += 1
            continue
        stock_fwd = _forward_return(connection, row["ticker"], row["availability_date"], HORIZON_MONTHS)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, row["availability_date"], HORIZON_MONTHS)
        if stock_fwd is None or bench_fwd is None:
            n_no_return += 1
            continue
        dataset.append({
            **row,
            "stock_return": stock_fwd["return"], "qqq_return": bench_fwd["return"],
            "excess_return": stock_fwd["return"] - bench_fwd["return"],
        })

    print(f"\nscored rows: {len(scored)}, no composite score (unrankable): {n_no_score}, "
          f"no forward return yet: {n_no_return}, usable dataset: {len(dataset)}")

    if len(dataset) < 5:
        print("\nToo few usable rows for any correlation to be meaningful.")
        RESULT_PATH.write_text(json.dumps({"dataset": dataset, "note": "too few rows"}, indent=2, default=str), encoding="utf-8")
        return

    print("\nPer-entry detail:")
    for r in sorted(dataset, key=lambda r: r["excess_return"]):
        print(f"  {r['ticker']:<6} {r['calendar_quarter_key']}  composite={r['composite_score']:.1f}  "
              f"weight_covered={r['weight_covered']:.0%}  12mo_excess_return={r['excess_return']:+.1%}")

    plain_corr = spearman_correlation([(r["composite_score"], r["excess_return"]) for r in dataset])
    print(f"\nPlain (unpooled) Spearman correlation: {plain_corr}")

    print("\n" + "=" * 92)
    print(f"Block bootstrap (grouped by ticker): quarterly composite_score vs {HORIZON_MONTHS}mo excess return")
    print("=" * 92)
    bootstrap_result = block_bootstrap_correlation(
        dataset, "composite_score", "excess_return", group_key="ticker", n_resamples=5000, seed=13
    )
    print(json.dumps(bootstrap_result, indent=2))
    if bootstrap_result["ci_95_crosses_zero"]:
        print("\n-> 95% interval CROSSES ZERO: not a validated signal.")
    else:
        print("\n-> 95% interval does NOT cross zero: real signal, even on this small proof.")

    # Robustness check (same discipline as D-065's MU-dominance concern):
    # MU has 3 extreme outliers here (+186%, +305%, +771% excess return,
    # the AI-memory demand spike) -- does the signal survive without it,
    # or is it one company's idiosyncratic run driving the whole result?
    print("\n" + "=" * 92)
    print("Robustness: same bootstrap EXCLUDING MU")
    print("=" * 92)
    dataset_ex_mu = [r for r in dataset if r["ticker"] != "MU"]
    bootstrap_ex_mu = block_bootstrap_correlation(
        dataset_ex_mu, "composite_score", "excess_return", group_key="ticker", n_resamples=5000, seed=13
    )
    print(f"n={len(dataset_ex_mu)} (was {len(dataset)})")
    print(json.dumps(bootstrap_ex_mu, indent=2))
    if bootstrap_ex_mu["ci_95_crosses_zero"]:
        print("\n-> WITHOUT MU, the 95% interval crosses zero: the full-sample result above is MU-dependent.")
    else:
        print("\n-> WITHOUT MU, the 95% interval still does not cross zero: the signal is not solely an MU artifact.")

    connection.close()

    payload = {
        "lookback_quarters": LOOKBACK_QUARTERS, "horizon_months": HORIZON_MONTHS,
        "cutoff_date": cutoff_date, "group_sizes": group_sizes,
        "n_scored": len(scored), "n_no_score": n_no_score, "n_no_return": n_no_return,
        "n_dataset": len(dataset), "plain_spearman_correlation": plain_corr,
        "block_bootstrap": bootstrap_result,
        "block_bootstrap_excluding_mu": bootstrap_ex_mu,
        "dataset": dataset,
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
