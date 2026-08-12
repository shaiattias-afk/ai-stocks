"""
scoring/predictive_analysis_v1.py -- does Scoring Model V1's composite
score (or any individual factor) predict which company-years beat QQQ
(Nasdaq-100) by >= a stated margin over the next `horizon_months`?

Reuses stock_agent.scoring.backtest_v1's own point-in-time-safe forward-
return mechanism unchanged (adj_close, fails closed rather than
fabricating a return past available price data).

No p-values or significance testing is claimed -- with ~700 company-
years but heavy cross-sectional correlation within a fiscal year (the
same QQQ return applies to every company entering that year) and
survivorship gaps in price data (~10 delisted/acquired tickers with no
price history at all -- ATVI, SPLK, CERN, XLNX, ANSS, MXIM, ALXN, SGEN,
WBA, FI -- silently excluded from any return-based analysis here,
reintroducing a MILD survivorship bias this module does not correct),
this is descriptive, not inferential, analysis.
"""

from __future__ import annotations

import duckdb

from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return

FACTOR_NAMES = [
    "revenue_growth", "roic_level", "roic_trend", "operating_margin",
    "fcf_growth", "fcf_margin", "balance_sheet_strength_ratio",
    "capex_discipline_deviation", "distance_from_high",
]


def spearman_correlation(pairs: list[tuple[float, float]]) -> float | None:
    """Spearman rank correlation, ties averaged. No scipy dependency --
    this project's venv does not have it installed. Returns None for
    fewer than 3 pairs (not statistically meaningful) or zero variance
    in either series."""
    n = len(pairs)
    if n < 3:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        r = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg_rank
            i = j + 1
        return r

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    rx = ranks(xs)
    ry = ranks(ys)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    cov = sum((a - mean_rx) * (b - mean_ry) for a, b in zip(rx, ry))
    var_x = sum((a - mean_rx) ** 2 for a in rx)
    var_y = sum((b - mean_ry) ** 2 for b in ry)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x * var_y) ** 0.5


def build_predictive_dataset(
    connection: duckdb.DuckDBPyConnection, horizon_months: int = 12, min_weight_covered: float = 0.0
) -> list[dict[str, object]]:
    """One row per company-year with a resolvable composite_score AND a
    resolvable forward return (both the stock's and QQQ's, over the
    exact same entry/exit dates) at `horizon_months`."""

    composite_rows = connection.execute(
        "SELECT ticker, report_date, fiscal_year, composite_score, weight_covered, "
        "factors_used, factor_scores_json FROM scoring_composite_v1 "
        "WHERE composite_score IS NOT NULL AND weight_covered >= ?",
        [min_weight_covered],
    ).fetchall()

    filing_dates = dict(connection.execute(
        "SELECT ticker || '|' || report_date, filing_date FROM sec_filings"
    ).fetchall())

    import json as _json

    dataset = []
    for ticker, report_date, fiscal_year, composite_score, weight_covered, factors_used, factor_scores_json in composite_rows:
        report_date = str(report_date)
        filing_date = filing_dates.get(f"{ticker}|{report_date}")
        if filing_date is None:
            continue
        filing_date = str(filing_date)

        stock_fwd = _forward_return(connection, ticker, filing_date, horizon_months)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, filing_date, horizon_months)
        if stock_fwd is None or bench_fwd is None:
            continue

        excess_return = stock_fwd["return"] - bench_fwd["return"]
        # Annualized (CAGR) versions -- the correct basis for a "beats
        # by X% PER YEAR, on average" question over a multi-year
        # horizon; identical to the raw total return when horizon_months
        # == 12, so this is a strict generalization, not a different
        # metric for the 12-month case already used elsewhere.
        years = horizon_months / 12
        annualized_stock_return = (1 + stock_fwd["return"]) ** (1 / years) - 1
        annualized_qqq_return = (1 + bench_fwd["return"]) ** (1 / years) - 1
        annualized_excess_return = annualized_stock_return - annualized_qqq_return
        dataset.append({
            "ticker": ticker, "report_date": report_date, "fiscal_year": fiscal_year,
            "filing_date": filing_date,
            "composite_score": float(composite_score), "weight_covered": float(weight_covered),
            "factors_used": factors_used,
            "factor_scores": _json.loads(factor_scores_json) if factor_scores_json else {},
            "stock_return": stock_fwd["return"], "qqq_return": bench_fwd["return"],
            "excess_return": excess_return,
            "beats_by_5pct": excess_return >= 0.05,
            "annualized_stock_return": annualized_stock_return,
            "annualized_qqq_return": annualized_qqq_return,
            "annualized_excess_return": annualized_excess_return,
            "beats_by_5pct_annualized": annualized_excess_return >= 0.05,
        })
    return dataset


def decile_analysis(dataset: list[dict[str, object]], n_buckets: int = 5) -> list[dict[str, object]]:
    """Buckets company-years by composite_score into `n_buckets` equal-
    count groups, reports each bucket's mean/median excess return and
    beat-by-5%-rate. Buckets are labeled low->high score."""
    ordered = sorted(dataset, key=lambda r: r["composite_score"])
    n = len(ordered)
    bucket_size = n / n_buckets
    buckets = []
    for b in range(n_buckets):
        start = int(round(b * bucket_size))
        end = int(round((b + 1) * bucket_size)) if b < n_buckets - 1 else n
        group = ordered[start:end]
        if not group:
            continue
        excess_values = sorted(r["excess_return"] for r in group)
        buckets.append({
            "bucket": b + 1, "n": len(group),
            "score_range": [group[0]["composite_score"], group[-1]["composite_score"]],
            "mean_excess_return": sum(excess_values) / len(excess_values),
            "median_excess_return": excess_values[len(excess_values) // 2],
            "beat_5pct_rate": sum(1 for r in group if r["beats_by_5pct"]) / len(group),
        })
    return buckets


def factor_correlations(dataset: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    """Spearman correlation between each factor's own percentile score
    (0-100, already direction-normalized by composite_v1 -- higher is
    always better) and excess_return, computed only over company-years
    where that factor was actually available."""
    results = {}
    for factor in FACTOR_NAMES:
        pairs = [
            (r["factor_scores"][factor], r["excess_return"])
            for r in dataset if factor in r["factor_scores"]
        ]
        results[factor] = {
            "n": len(pairs),
            "spearman_correlation": spearman_correlation(pairs),
        }
    pairs = [(r["composite_score"], r["excess_return"]) for r in dataset]
    results["composite_score"] = {"n": len(pairs), "spearman_correlation": spearman_correlation(pairs)}
    return results
