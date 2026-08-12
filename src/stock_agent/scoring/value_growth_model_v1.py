"""
scoring/value_growth_model_v1.py -- the model D-063 pointed to directly:
entry-date valuation (P/E) characterized actual 5-year winners/losers
better than the 9-factor quality/growth composite (D-057), but a
P/E-only rule would have excluded some of the biggest winners (CRWD,
PLTR, PANW, RKLB), which were not yet profitable at entry.

Two buckets, split by profitability at entry (diluted_eps > 0 or not),
each ranked by the ONE signal that actually applies to it:

  - PROFITABLE bucket: ranked by P/E, ascending (cheaper = better) --
    the D-063 finding, made structural instead of a raw correlation
    number.
  - UNPROFITABLE bucket: ranked by revenue_growth, descending (faster
    growth = better) -- the standard substitute signal for a company
    valuation can't yet judge on earnings, and specifically chosen so
    a fast-growing pre-profit company is never penalized for lacking a
    P/E rather than excluded outright.

Both buckets are ranked WITHIN THE SAME (fiscal_year, bucket) group,
reusing composite_v1's own percentile-rank mechanism (ties averaged,
0-100, no ranking possible with fewer than 2 companies in a group --
same discipline as every other model in this project). A company with
NEITHER signal resolvable (unprofitable AND no revenue_growth) gets no
score -- never fabricated.

This is explicitly a SINGLE-signal-per-bucket model, not a weighted
composite of several factors -- D-062 already showed that combining
several weakly-correlated factors with fitted weights did not survive
a genuine train/test split. This model tests a sharper, narrower
hypothesis instead: does valuation ALONE (with a growth substitute for
the unprofitable case) do better than the broad quality composite?
"""

from __future__ import annotations

from stock_agent.scoring.composite_v1 import _percentile_ranks

PROFITABLE = "profitable"
UNPROFITABLE = "unprofitable"


def classify_bucket(diluted_eps: float | None) -> str:
    return PROFITABLE if (diluted_eps is not None and diluted_eps > 0) else UNPROFITABLE


def compute_value_growth_scores(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """`rows`: one dict per company-fiscal-year with `ticker`,
    `report_date`, `fiscal_year`, `diluted_eps` (or None), `raw_pe` (or
    None -- only meaningful when diluted_eps > 0), `revenue_growth` (or
    None). Returns one dict per input row: ticker, report_date,
    fiscal_year, bucket, value_growth_score (0-100 or None), the raw
    signal actually used."""

    by_group: dict[tuple[int, str], list[dict]] = {}
    for row in rows:
        bucket = classify_bucket(row.get("diluted_eps"))
        by_group.setdefault((row["fiscal_year"], bucket), []).append(row)

    results: list[dict[str, object]] = []
    for (fiscal_year, bucket), group in by_group.items():
        if bucket == PROFITABLE:
            raw_values = {r["ticker"]: r["raw_pe"] for r in group if r.get("raw_pe") is not None}
            ranks = _percentile_ranks(raw_values, invert=True)  # cheaper P/E -> higher score
            signal_key = "raw_pe"
        else:
            raw_values = {r["ticker"]: r["revenue_growth"] for r in group if r.get("revenue_growth") is not None}
            ranks = _percentile_ranks(raw_values, invert=False)  # faster growth -> higher score
            signal_key = "revenue_growth"

        for row in group:
            score = ranks.get(row["ticker"])
            results.append({
                "ticker": row["ticker"], "report_date": row["report_date"], "fiscal_year": fiscal_year,
                "bucket": bucket, "signal_used": signal_key if score is not None else None,
                "signal_value": row.get(signal_key) if score is not None else None,
                "composite_score": score,
            })

    return results
