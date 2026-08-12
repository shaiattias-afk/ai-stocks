"""
scoring/valuation_wide_v1.py -- extends D-046's diluted-EPS resolution
(originally 9 tickers, archive/scripts/160_valuation_v1_per_share_
inputs.py) to the full universe, reusing the exact same rule: the
single consolidated (non-dimensional), full-fiscal-year
`us-gaap:EarningsPerShareDiluted` fact matching the filing's own
report_date. Never averages or guesses between multiple genuinely
differing values -- REVIEW_REQUIRED (AMBIGUOUS) instead.

Deliberately NOT `extraction.core.match_facts_from_warehouse`: that
function's unit filter (`usd_unit_ids_for_accession`) only accepts pure
`iso4217:USD` units with no denominator, which would silently exclude
EVERY EPS fact -- EPS's unit is USD-per-share (numerator iso4217:USD,
denominator xbrli:shares), a different unit type entirely. Verified
directly on real data before writing this: 685 of the 732 wide-universe
accessions (93.6%) carry the EarningsPerShareDiluted concept at all.
"""

from __future__ import annotations

import duckdb

from stock_agent.extraction.core import ANNUAL_DURATION_MAX_DAYS, ANNUAL_DURATION_MIN_DAYS

EPS_CONCEPT = "us-gaap:EarningsPerShareDiluted"


def _usd_per_share_unit_ids(connection: duckdb.DuckDBPyConnection, accession_number: str) -> set[str]:
    rows = connection.execute(
        "SELECT unit_id FROM xbrl_units WHERE accession_number = ? "
        "AND numerator_measures = 'iso4217:USD' AND denominator_measures = 'xbrli:shares'",
        [accession_number],
    ).fetchall()
    return {r[0] for r in rows}


def resolve_diluted_eps(
    connection: duckdb.DuckDBPyConnection, accession_number: str, report_date: str
) -> dict[str, object]:
    """Same resolution rule as D-046: the single consolidated, full-
    fiscal-year diluted EPS fact whose period_end matches report_date
    exactly and whose duration is a genuine annual period (350-380
    days). Returns status PASS / AMBIGUOUS (REVIEW_REQUIRED, multiple
    differing values) / UNAVAILABLE (no fact at all)."""
    unit_ids = _usd_per_share_unit_ids(connection, accession_number)
    if not unit_ids:
        return {"status": "UNAVAILABLE", "value": None, "error": "no USD-per-share unit in this accession"}

    placeholders = ", ".join("?" for _ in unit_ids)
    rows = connection.execute(
        f"""
        SELECT DISTINCT context_id, value_numeric, period_start, period_end
        FROM xbrl_facts
        WHERE accession_number = ? AND concept_qname = ? AND dimensions_json = '{{}}'
          AND unit_id IN ({placeholders}) AND is_nil = FALSE AND value_numeric IS NOT NULL
          AND period_end = ?
          AND date_diff('day', CAST(period_start AS DATE), CAST(period_end AS DATE))
              BETWEEN ? AND ?
        """,
        [accession_number, EPS_CONCEPT, *unit_ids, report_date, ANNUAL_DURATION_MIN_DAYS, ANNUAL_DURATION_MAX_DAYS],
    ).fetchall()

    if not rows:
        return {"status": "UNAVAILABLE", "value": None, "error": "no matching annual EarningsPerShareDiluted fact"}

    distinct_values = sorted({round(float(r[1]), 6) for r in rows})
    if len(distinct_values) == 1:
        return {"status": "PASS", "value": distinct_values[0]}
    return {
        "status": "REVIEW_REQUIRED", "value": None,
        "error": f"multiple differing EarningsPerShareDiluted values: {distinct_values}",
    }
