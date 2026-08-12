"""User-directed (2026-08-12 session, immediately after D-068): the raw
P/E finding only showed up at a 5-YEAR horizon -- it was invisible at 12
months (D-061/D-062, tested at 12mo only). This raises the obvious
question D-061/D-062 never asked: do the 9 existing Scoring Inputs V1
factors (revenue_growth, roic_level, roic_trend, operating_margin,
fcf_margin, fcf_growth, balance_sheet_strength_ratio,
capex_discipline_deviation, distance_from_high) ALSO carry a real signal
that was simply invisible at the wrong horizon?

Every factor is already computed for the full wide (~135-company)
universe in `scoring_inputs_v1` -- no new extraction, no new pipeline,
just the same horizon-corrected question D-068 just answered for P/E,
asked once per factor, with the same company-grouped block bootstrap
validation discipline used everywhere else this session.

READ-ONLY. Writes one result JSON.
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.predictive_analysis_v1 import spearman_correlation

RESULT_PATH = DATA_DIR / "wide_universe_5yr_factor_sweep_result.json"
HORIZON_MONTHS = 60

FACTOR_NAMES = [
    "revenue_growth", "roic_level", "roic_trend", "operating_margin",
    "fcf_margin", "fcf_growth", "balance_sheet_strength_ratio",
    "capex_discipline_deviation", "distance_from_high",
]


def _build_dataset(connection: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = connection.execute(
        f"SELECT ticker, report_date, fiscal_year, filing_date, "
        f"{', '.join(FACTOR_NAMES)} FROM scoring_inputs_v1"
    ).fetchall()
    columns = ["ticker", "report_date", "fiscal_year", "filing_date"] + FACTOR_NAMES

    dataset = []
    for row in rows:
        record = dict(zip(columns, row))
        ticker, filing_date = record["ticker"], str(record["filing_date"])
        stock_fwd = _forward_return(connection, ticker, filing_date, HORIZON_MONTHS)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, filing_date, HORIZON_MONTHS)
        if stock_fwd is None or bench_fwd is None:
            continue
        years = HORIZON_MONTHS / 12
        ann_stock = (1 + stock_fwd["return"]) ** (1 / years) - 1
        ann_qqq = (1 + bench_fwd["return"]) ** (1 / years) - 1
        record["excess_return"] = ann_stock - ann_qqq
        dataset.append(record)
    return dataset


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    dataset = _build_dataset(connection)
    connection.close()
    print(f"5-year-eligible company-years (any factor may still be missing per-row): {len(dataset)}")

    results = {}
    print(f"\n{'factor':<32}{'n':>5}{'groups':>8}{'corr':>9}{'ci_low':>9}{'ci_high':>9}  signal?")
    print("-" * 90)
    for factor in FACTOR_NAMES:
        rows = [r for r in dataset if r.get(factor) is not None]
        n_groups = len({r["ticker"] for r in rows})
        if len(rows) < 10:
            print(f"{factor:<32}{len(rows):>5}{n_groups:>8}   too few rows")
            results[factor] = {"n": len(rows), "n_groups": n_groups, "note": "too few rows"}
            continue
        plain = spearman_correlation([(r[factor], r["excess_return"]) for r in rows])
        bootstrap = block_bootstrap_correlation(rows, factor, "excess_return", group_key="ticker", n_resamples=5000, seed=29)
        signal = "REAL (CI excludes 0)" if not bootstrap["ci_95_crosses_zero"] else ""
        print(f"{factor:<32}{len(rows):>5}{n_groups:>8}{bootstrap['observed_correlation']:>+9.3f}"
              f"{bootstrap['ci_95_low']:>+9.3f}{bootstrap['ci_95_high']:>+9.3f}  {signal}")
        results[factor] = {"n": len(rows), "n_groups": n_groups, "plain_spearman": plain, "bootstrap": bootstrap}

    RESULT_PATH.write_text(json.dumps({"horizon_months": HORIZON_MONTHS, "factors": results}, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
