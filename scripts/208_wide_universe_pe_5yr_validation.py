"""User-directed strategic pivot (2026-08-12 session): the quarterly
9-ticker composite work was too narrow to conclude anything about the
project's overall practical viability. The one signal in this whole
project that IS already bootstrap-validated as real is entry-date raw
P/E predicting 5-year excess return (D-063: -0.247, n=102; D-064:
block-bootstrap CI [-0.444, -0.032], survives cohort clustering) --
but it was only ever tested on the original 9-ticker universe. Scoring
Inputs V1, Valuation V1 (diluted EPS), and Historical Prices V1 were
all separately extended to a wide ~135-150-company survivorship-free
universe in earlier sessions (D-051-D-061) -- this script asks the
direct question: does the validated P/E finding get MORE robust (not
just re-confirmed on the same ~100 rows) when tested at that full scale?

READ-ONLY. Writes one result JSON.
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH
from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.cohort_robustness_v1 import block_bootstrap_correlation
from stock_agent.scoring.model_v2_candidate import _current_year_pe
from stock_agent.scoring.predictive_analysis_v1 import spearman_correlation

RESULT_PATH = DATA_DIR / "wide_universe_pe_5yr_validation_result.json"
ORIGINAL_9 = {"ORCL", "MSFT", "META", "NVDA", "GOOGL", "AMZN", "MU", "CRWD", "PANW"}


def _build_dataset(connection: duckdb.DuckDBPyConnection, horizon_months: int) -> list[dict]:
    rows = connection.execute(
        "SELECT ticker, report_date, fiscal_year, filing_date FROM scoring_inputs_v1"
    ).fetchall()
    dataset = []
    for ticker, report_date, fiscal_year, filing_date in rows:
        filing_date = str(filing_date)
        raw_pe = _current_year_pe(connection, ticker, fiscal_year)
        if raw_pe is None:
            continue
        stock_fwd = _forward_return(connection, ticker, filing_date, horizon_months)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, filing_date, horizon_months)
        if stock_fwd is None or bench_fwd is None:
            continue
        years = horizon_months / 12
        ann_stock = (1 + stock_fwd["return"]) ** (1 / years) - 1
        ann_qqq = (1 + bench_fwd["return"]) ** (1 / years) - 1
        dataset.append({
            "ticker": ticker, "report_date": str(report_date), "fiscal_year": fiscal_year,
            "filing_date": filing_date, "raw_pe": raw_pe,
            "excess_return": ann_stock - ann_qqq if horizon_months != 12 else (stock_fwd["return"] - bench_fwd["return"]),
        })
    return dataset


def _report(label: str, dataset: list[dict]) -> dict:
    n_groups = len({r["ticker"] for r in dataset})
    print(f"\n{'=' * 92}\n{label}: n={len(dataset)}  independent_tickers={n_groups}\n{'=' * 92}")
    if len(dataset) < 5:
        print("too few rows")
        return {"n": len(dataset), "n_groups": n_groups, "note": "too few rows"}
    plain = spearman_correlation([(r["raw_pe"], r["excess_return"]) for r in dataset])
    bootstrap = block_bootstrap_correlation(dataset, "raw_pe", "excess_return", group_key="ticker", n_resamples=5000, seed=23)
    print(f"plain spearman: {plain}")
    print(json.dumps(bootstrap, indent=2))
    flag = "CROSSES ZERO" if bootstrap["ci_95_crosses_zero"] else "does NOT cross zero"
    print(f"-> {flag}")
    return {"n": len(dataset), "n_groups": n_groups, "plain_spearman": plain, "bootstrap": bootstrap}


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)

    results = {}
    for horizon_months, label in ((60, "60-MONTH (ANNUALIZED)"), (12, "12-MONTH")):
        dataset = _build_dataset(connection, horizon_months)
        original_9_subset = [r for r in dataset if r["ticker"] in ORIGINAL_9]
        wide_only = [r for r in dataset if r["ticker"] not in ORIGINAL_9]

        results[f"{horizon_months}mo_full_universe"] = _report(f"{label} -- FULL UNIVERSE", dataset)
        results[f"{horizon_months}mo_original_9_only"] = _report(f"{label} -- ORIGINAL 9 ONLY (D-063/D-064 replication)", original_9_subset)
        results[f"{horizon_months}mo_wide_universe_new_tickers_only"] = _report(f"{label} -- NEW WIDE-UNIVERSE TICKERS ONLY (never tested before)", wide_only)

    connection.close()
    RESULT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
