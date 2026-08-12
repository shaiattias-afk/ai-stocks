"""Council Proposal 2 (D-064), run against real data for the first time:
does the two-bucket value/growth model (value_growth_model_v1.py) --
profitable companies ranked by cheap P/E, unprofitable companies ranked
by fast revenue growth -- predict excess return over QQQ better than
the 9-factor quality composite (D-061/D-062: ~0), and does it capture
the winners a P/E-only rule would have missed (D-063: CRWD, PLTR, PANW,
RKLB)?

Tested at both horizons already established this session:
  - 12 months (larger sample, apples-to-apples with D-061/D-062)
  - 60 months / annualized (the horizon where D-063's raw-P/E finding,
    which this model is a structural version of, was actually found)

Same validation discipline as everywhere else this session: company-
grouped block bootstrap, not a naive pooled correlation, since repeat
tickers across fiscal years are not independent observations.

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
from stock_agent.scoring.value_growth_model_v1 import PROFITABLE, UNPROFITABLE, compute_value_growth_scores

RESULT_PATH = DATA_DIR / "value_growth_model_predictive_check_result.json"


def _build_rows(connection: duckdb.DuckDBPyConnection) -> list[dict[str, object]]:
    """One row per company-year: ticker, report_date, fiscal_year,
    filing_date, diluted_eps, raw_pe, revenue_growth -- everything
    compute_value_growth_scores needs, plus filing_date for return
    lookups."""
    base = connection.execute(
        "SELECT ticker, report_date, fiscal_year, filing_date, revenue_growth FROM scoring_inputs_v1"
    ).fetchall()
    eps_by_key = dict(connection.execute(
        "SELECT ticker || '|' || fiscal_year, diluted_eps FROM valuation_v1_per_share_inputs"
    ).fetchall())

    rows = []
    for ticker, report_date, fiscal_year, filing_date, revenue_growth in base:
        diluted_eps = eps_by_key.get(f"{ticker}|{fiscal_year}")
        raw_pe = _current_year_pe(connection, ticker, fiscal_year) if (diluted_eps and diluted_eps > 0) else None
        rows.append({
            "ticker": ticker, "report_date": str(report_date), "fiscal_year": fiscal_year,
            "filing_date": str(filing_date), "diluted_eps": diluted_eps,
            "raw_pe": raw_pe, "revenue_growth": revenue_growth,
        })
    return rows


def _attach_returns(connection: duckdb.DuckDBPyConnection, scored: list[dict], rows_by_key: dict, horizon_months: int) -> list[dict]:
    out = []
    for s in scored:
        if s["composite_score"] is None:
            continue
        key = f"{s['ticker']}|{s['fiscal_year']}"
        filing_date = rows_by_key[key]["filing_date"]
        stock_fwd = _forward_return(connection, s["ticker"], filing_date, horizon_months)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, filing_date, horizon_months)
        if stock_fwd is None or bench_fwd is None:
            continue
        years = horizon_months / 12
        ann_stock = (1 + stock_fwd["return"]) ** (1 / years) - 1
        ann_qqq = (1 + bench_fwd["return"]) ** (1 / years) - 1
        out.append({
            **s, "filing_date": filing_date,
            "excess_return": ann_stock - ann_qqq,
        })
    return out


def _report_horizon(connection, rows, rows_by_key, horizon_months: int) -> dict[str, object]:
    scored = compute_value_growth_scores(rows)
    dataset = _attach_returns(connection, scored, rows_by_key, horizon_months)

    label = "annualized" if horizon_months != 12 else "raw"
    print(f"\n{'=' * 92}\nHorizon = {horizon_months} months ({label} excess return), n = {len(dataset)}\n{'=' * 92}")

    for bucket in (PROFITABLE, UNPROFITABLE):
        bucket_rows = [r for r in dataset if r["bucket"] == bucket]
        pairs = [(r["composite_score"], r["excess_return"]) for r in bucket_rows]
        corr = spearman_correlation(pairs)
        print(f"  bucket={bucket:<13} n={len(bucket_rows):<4} plain Spearman={corr}")

    pooled_bootstrap = block_bootstrap_correlation(
        dataset, "composite_score", "excess_return", group_key="ticker", n_resamples=5000, seed=11
    )
    print(f"\n  POOLED (both buckets), block bootstrap by ticker:")
    print(f"  {json.dumps(pooled_bootstrap, indent=2)}")

    # Winners this model was specifically built to capture (D-063):
    # unprofitable-at-entry names that a raw-P/E-only rule would exclude.
    named_winners = {"CRWD", "PLTR", "PANW", "RKLB"}
    covered = sorted({r["ticker"] for r in dataset if r["ticker"] in named_winners and r["bucket"] == UNPROFITABLE})
    print(f"\n  D-063 unprofitable winners scored via UNPROFITABLE bucket (growth signal): {covered}")

    return {
        "horizon_months": horizon_months, "n": len(dataset),
        "pooled_block_bootstrap": pooled_bootstrap,
        "profitable_n": sum(1 for r in dataset if r["bucket"] == PROFITABLE),
        "unprofitable_n": sum(1 for r in dataset if r["bucket"] == UNPROFITABLE),
        "unprofitable_winners_covered": covered,
        "dataset": dataset,
    }


def main() -> None:
    connection = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    rows = _build_rows(connection)
    rows_by_key = {f"{r['ticker']}|{r['fiscal_year']}": r for r in rows}
    print(f"total company-years: {len(rows)}")

    result_12mo = _report_horizon(connection, rows, rows_by_key, 12)
    result_60mo = _report_horizon(connection, rows, rows_by_key, 60)
    connection.close()

    RESULT_PATH.write_text(
        json.dumps({"horizon_12mo": result_12mo, "horizon_60mo": result_60mo}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nwritten: {RESULT_PATH}")


if __name__ == "__main__":
    main()
