"""
scoring/model_v2_candidate.py -- builds and honestly out-of-sample
validates a CANDIDATE alternative to Scoring Model V1's composite
(docs/DECISIONS_LOG.md D-061's recommended next step): add a valuation
factor (P/E vs. cross-section, not V1's own-history percentile -- see
below), and select/weight factors by their measured relationship with
forward excess return over QQQ -- but ONLY ever measured on a TRAIN
period, then checked on a untouched TEST period, never the reverse.

**This is the single most important discipline in this module: factor
selection and weighting happen ONLY on TRAIN. The TEST period's returns
are never looked at until the very last step, and never feed back into
selecting or weighting anything.** Skipping this and fitting weights on
the whole dataset (as a naive first pass would do) is in-sample curve-
fitting -- CLAUDE.md's own overfitting warning exists exactly to
prevent this, and it is why D-061 explicitly told the next session not
to reweight directly on that analysis's own dataset.

**The valuation factor here is deliberately NOT Entry Price V1's
`pe_percentile_within_own_history`** (docs/DECISIONS_LOG.md D-058):
that needs >=2 trailing years of the SAME company's own P/E history,
which cuts the usable sample sharply. This module instead uses the RAW
current-year P/E (price at filing_date / that year's own diluted EPS),
ranked CROSS-SECTIONALLY against the same fiscal year's other
companies -- the same percentile-rank mechanism composite_v1.py already
uses for the other 9 factors, applied consistently to a 10th. Lower P/E
ranks toward 100 (cheap, per value-investing convention) via composite_
v1's own `invert` flag -- no new ranking logic, just a new raw input.

**Train/test split is by filing_date, not company** -- deliberately: a
split by company (e.g. "train on half the tickers") would let macro/
sector conditions leak across the split in a different, harder-to-reason
-about way; a split by TIME is the one an actual investor building this
strategy in early 2023 could genuinely have respected (train on what
was already filed, test forward on what came after) -- the same point-
in-time discipline this whole project already applies everywhere else.
"""

from __future__ import annotations

import json

import duckdb

from stock_agent.scoring.backtest_v1 import BENCHMARK_TICKER, _forward_return
from stock_agent.scoring.composite_v1 import FACTOR_WEIGHTS as V1_FACTOR_WEIGHTS
from stock_agent.scoring.composite_v1 import compute_composite_scores_v1
from stock_agent.scoring.entry_price_v1 import _price_at_or_before
from stock_agent.scoring.predictive_analysis_v1 import spearman_correlation

# Fixed, theoretically-justified direction per factor -- the same 9
# directions docs/SCORING_MODEL_V1_BLUEPRINT.md's Stage 3 already
# committed to, plus valuation_pe (lower P/E = cheaper = conventionally
# better, standard value-investing prior). Directions are NOT chosen by
# searching for whichever sign fits the training data best -- that would
# let a "better fit" flip a well-justified economic direction into a
# data-mined, possibly spurious one on a modest sample. Only the
# STRENGTH of each factor's correlation in its OWN pre-committed
# direction, measured on train, decides selection and weight.
CANDIDATE_FACTOR_DIRECTIONS: dict[str, bool] = {
    **{factor: invert for factor, (_w, invert) in V1_FACTOR_WEIGHTS.items()},
    "valuation_pe": True,
}
ALL_CANDIDATE_FACTORS = list(CANDIDATE_FACTOR_DIRECTIONS)


def _current_year_pe(connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year: int) -> float | None:
    """Raw current-year P/E: filing-date nominal_close / that year's own
    as-reported diluted EPS. Same split-adjustment discipline as Entry
    Price V1 (D-058) -- nominal_close only, never close -- but no
    trailing-history requirement. None when EPS is missing or <= 0
    (undefined, never guessed)."""
    row = connection.execute(
        "SELECT diluted_eps, filing_date FROM valuation_v1_per_share_inputs "
        "WHERE ticker = ? AND fiscal_year = ?",
        [ticker, fiscal_year],
    ).fetchone()
    if row is None:
        return None
    diluted_eps, filing_date = float(row[0]), str(row[1])
    if diluted_eps <= 0:
        return None
    price, _price_date = _price_at_or_before(connection, ticker, filing_date)
    if price is None:
        return None
    return price / diluted_eps


def build_dataset_with_valuation(
    connection: duckdb.DuckDBPyConnection, horizon_months: int = 12
) -> list[dict[str, object]]:
    """One row per company-year in scoring_inputs_v1, with the 9 V1 raw
    factors, the new valuation_pe raw factor, filing_date (for the
    train/test split), and the horizon_months forward excess return vs
    QQQ (None where not resolvable -- never fabricated)."""

    rows = connection.execute(
        """
        SELECT ticker, report_date, fiscal_year, filing_date,
               revenue_growth, roic_level, roic_trend, operating_margin,
               fcf_margin, fcf_growth, balance_sheet_strength_ratio,
               capex_discipline_deviation, distance_from_high
        FROM scoring_inputs_v1
        """
    ).fetchall()

    dataset = []
    for (ticker, report_date, fiscal_year, filing_date, revenue_growth, roic_level, roic_trend,
         operating_margin, fcf_margin, fcf_growth, balance_sheet_strength_ratio,
         capex_discipline_deviation, distance_from_high) in rows:
        filing_date = str(filing_date)
        valuation_pe = _current_year_pe(connection, ticker, fiscal_year)

        stock_fwd = _forward_return(connection, ticker, filing_date, horizon_months)
        bench_fwd = _forward_return(connection, BENCHMARK_TICKER, filing_date, horizon_months)
        excess_return = (stock_fwd["return"] - bench_fwd["return"]) if (stock_fwd and bench_fwd) else None

        dataset.append({
            "ticker": ticker, "report_date": str(report_date), "fiscal_year": fiscal_year,
            "filing_date": filing_date,
            "revenue_growth": revenue_growth, "roic_level": roic_level, "roic_trend": roic_trend,
            "operating_margin": operating_margin, "fcf_margin": fcf_margin, "fcf_growth": fcf_growth,
            "balance_sheet_strength_ratio": balance_sheet_strength_ratio,
            "capex_discipline_deviation": capex_discipline_deviation,
            "distance_from_high": distance_from_high, "valuation_pe": valuation_pe,
            "excess_return": excess_return,
        })
    return dataset


def select_and_weight_factors_from_train(
    train_dataset: list[dict[str, object]],
    min_abs_correlation: float = 0.03,
) -> dict[str, tuple[float, bool]]:
    """TRAIN-ONLY factor selection and weighting. For each candidate
    factor, ranks it cross-sectionally per fiscal year (composite_v1's
    own mechanism, both directions tried since we don't yet know which
    way is "better" for valuation_pe going in) and measures Spearman
    correlation with excess_return, ON TRAIN ROWS ONLY. Keeps factors
    whose |correlation| clears `min_abs_correlation`, weighted
    proportionally to that correlation's magnitude. Returns a
    composite_v1-shaped {factor: (weight, invert)} dict, weights summing
    to 1.0 -- or, if nothing clears the bar, an empty dict (this is a
    valid, honestly-reported outcome, not an error)."""

    scored: dict[str, float] = {}
    for factor, invert in CANDIDATE_FACTOR_DIRECTIONS.items():
        weights = {factor: (1.0, invert)}
        train_inputs = [
            {"ticker": r["ticker"], "report_date": r["report_date"], "fiscal_year": r["fiscal_year"], factor: r[factor]}
            for r in train_dataset
        ]
        composites = compute_composite_scores_v1(train_inputs, factor_weights=weights)
        pairs = [
            (c["composite_score"], r["excess_return"])
            for c, r in zip(composites, train_dataset)
            if c["composite_score"] is not None and r["excess_return"] is not None
        ]
        corr = spearman_correlation(pairs)
        if corr is None:
            continue
        # Only a POSITIVE correlation in the factor's own pre-committed
        # direction counts as support -- a negative correlation here
        # means the factor pointed the wrong way on train (exactly
        # D-061's balance_sheet_strength_ratio finding), which is
        # evidence against including it, not a cue to flip its sign.
        scored[factor] = corr

    selected = {factor: corr for factor, corr in scored.items() if corr >= min_abs_correlation}
    if not selected:
        return {}

    total_corr = sum(selected.values())
    return {
        factor: (corr / total_corr, CANDIDATE_FACTOR_DIRECTIONS[factor])
        for factor, corr in selected.items()
    }
