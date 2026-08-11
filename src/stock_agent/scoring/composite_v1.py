"""
scoring/composite_v1.py -- combines Scoring Inputs V1's 9 raw factors
(inputs_v1.py) into a single 0-100 composite score per company-fiscal-
year, per docs/SCORING_MODEL_V1_BLUEPRINT.md Stage 3: continuous
percentile rank WITHIN THE SAME FISCAL YEAR across whatever companies
have a value for that factor that year (never across years, never
against a company's own history for THIS step -- that would silently
change what "100" means from one year to the next).

A company-year missing a factor (a genuine data gap, e.g. a ticker's
first fiscal year in the dataset has no revenue_growth) is EXCLUDED
from that factor's ranking and from the composite for that company-year
-- weights are renormalized over only the factors actually available,
never faked as 0 or a neutral 50. `weight_covered` on every output row
states exactly what fraction of the full weighting the composite
actually rests on, so a thin composite is never silently indistinguishable
from a complete one.
"""

from __future__ import annotations

from collections import defaultdict

# (weight, invert) -- invert=True means a LOWER raw value is BETTER
# (gets ranked toward 100), per the blueprint's Stage 3 table.
FACTOR_WEIGHTS: dict[str, tuple[float, bool]] = {
    "revenue_growth": (0.20, False),
    "roic_level": (0.15, False),
    "roic_trend": (0.10, False),
    "operating_margin": (0.10, False),
    "fcf_growth": (0.15, False),
    "fcf_margin": (0.10, False),
    "balance_sheet_strength_ratio": (0.10, True),
    "capex_discipline_deviation": (0.05, True),
    "distance_from_high": (0.05, False),
}

assert abs(sum(w for w, _ in FACTOR_WEIGHTS.values()) - 1.0) < 1e-9, "factor weights must sum to 100%"


def _percentile_ranks(values: dict[str, float], invert: bool) -> dict[str, float]:
    """0-100 continuous percentile rank, ties averaged. Needs at least 2
    distinct companies to mean anything; returns {} otherwise (nothing
    to rank against -- never assigns an arbitrary single-company score)."""
    if len(values) < 2:
        return {}

    ordered = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ordered)
    ranks: dict[str, float] = {}

    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        # average rank (0-indexed) across the tied block [i, j]
        avg_rank = (i + j) / 2
        for k in range(i, j + 1):
            ranks[ordered[k][0]] = avg_rank
        i = j + 1

    percentiles = {ticker: (rank / (n - 1)) * 100 for ticker, rank in ranks.items()}
    if invert:
        percentiles = {ticker: 100 - p for ticker, p in percentiles.items()}
    return percentiles


def compute_composite_scores_v1(
    all_inputs: list[dict[str, object]],
    factor_weights: dict[str, tuple[float, bool]] | None = None,
) -> list[dict[str, object]]:
    """`all_inputs`: one dict per company-fiscal-year, each shaped like
    inputs_v1.compute_scoring_inputs_v1's return value, PLUS a
    `fiscal_year` key (sec_filings.fiscal_year -- the label already used
    consistently across companies with different fiscal year-ends).

    `factor_weights`: optional override of the module-level FACTOR_
    WEIGHTS (same `{factor_name: (weight, invert)}` shape) -- lets a
    caller test an alternative weighting/factor-selection (e.g. a
    candidate V2 model) through the exact same ranking mechanism,
    without duplicating it. Weights need not sum to 1.0 here (unlike the
    module-level default, which is asserted); this function always
    renormalizes by whatever weight was actually covered per row.

    Returns one dict per input row: ticker, report_date, fiscal_year,
    composite_score (0-100 or None if zero factors were rankable),
    weight_covered (0-1, as a fraction of THIS call's total weight),
    factor_scores (per-factor percentile, only for factors that were
    rankable), factors_used (count)."""

    weights = factor_weights if factor_weights is not None else FACTOR_WEIGHTS
    total_weight = sum(w for w, _ in weights.values())

    by_year: dict[int, list[dict]] = defaultdict(list)
    for row in all_inputs:
        by_year[row["fiscal_year"]].append(row)

    results: list[dict[str, object]] = []
    for fiscal_year, rows in by_year.items():
        factor_ranks: dict[str, dict[str, float]] = {}
        for factor, (_weight, invert) in weights.items():
            raw_values = {
                row["ticker"]: row[factor]
                for row in rows
                if row.get(factor) is not None
            }
            factor_ranks[factor] = _percentile_ranks(raw_values, invert)

        for row in rows:
            ticker = row["ticker"]
            factor_scores: dict[str, float] = {}
            weighted_sum = 0.0
            weight_covered = 0.0
            for factor, (weight, _invert) in weights.items():
                score = factor_ranks[factor].get(ticker)
                if score is None:
                    continue
                factor_scores[factor] = score
                weighted_sum += score * weight
                weight_covered += weight

            composite = (weighted_sum / weight_covered) if weight_covered > 0 else None

            results.append({
                "ticker": ticker,
                "report_date": row["report_date"],
                "fiscal_year": fiscal_year,
                "composite_score": composite,
                "weight_covered": weight_covered / total_weight,
                "factors_used": len(factor_scores),
                "factor_scores": factor_scores,
            })

    return results
