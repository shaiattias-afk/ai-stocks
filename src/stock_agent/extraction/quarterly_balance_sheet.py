"""
extraction/quarterly_balance_sheet.py -- extends Quarterly Data V1's
coverage from 6 income-statement/cash-flow metrics to also include
balance-sheet metrics and the ROIC chain, WITHOUT touching Engine V5 or
any of its already-loaded rows (D-042's freeze forbids in-place engine
changes; this is a new, additive engine).

**Root cause this fixes (D-076)**: the quarterly engine (extraction/
quarterly.py) was never missing balance-sheet DATA -- every archived
10-Q already carries a full balance sheet in the XBRL warehouse
(confirmed by direct query on AMZN/MSFT/PANW/CRWD). It was missing
balance-sheet EXTRACTION LOGIC, because `pick_current_period_fact` and
`facts_for_concept` are built exclusively for durational (period-start +
period-end) facts and would silently return "no fact" for an
instantaneous (as-of-a-date) balance-sheet concept.

**The fix reuses the annual engine's own resolvers unchanged, rather
than inventing new ones.** `metrics.annual.compute_company_year` already
does exactly the right thing for ANY (accession_number, report_date)
pair: it resolves 8 balance-sheet metrics (current_debt, long_term_debt,
total_debt, cash_and_equivalents, short_term_investments,
stockholders_equity, adjusted_net_debt, invested_capital) via instant-
fact matching (`extraction.core.match_facts_from_warehouse`,
`expected_period_type="instant"`), and nothing in it assumes the
accession is a 10-K rather than a 10-Q -- it was simply never called on
a 10-Q before. Each quarter's own `period_end` (already stored in
`quarterly_metric_results`, resolved by Engine V5 for the same
accession) is exactly the "as of" date an instant fact needs, and Q4's
`period_end`/`accession_number` already point at the same 10-K + fiscal-
year-end the annual engine itself uses -- so Q4's balance-sheet metrics
computed here are provably byte-identical to the frozen annual figures
for that same company-year (tests/test_quarterly_balance_sheet.py's own
proof).

**NOPAT is computed as a trailing-twelve-month (TTM) figure, not a
single quarter's**, and this is a genuine new policy choice, not a
mechanical port -- flagged here plainly rather than buried in code.
Annual NOPAT is naturally a 12-month figure (operating_income and tax
are already summed over the fiscal year); dividing a single quarter's
NOPAT by an averaged invested-capital base would inject the same
quarter-to-quarter seasonality this project has already gone out of its
way to avoid for growth factors (D-065's YoY-not-sequential rule). TTM
(this quarter + the trailing 3) keeps ROIC comparable across quarters
the same way the annual metric is comparable across years -- summing the
4 quarters' operating_income/pretax_income/income_tax_expense first,
then applying the exact same D-015 item 7 / D-027 Policy D tax-
normalization formula the annual engine already uses on the summed
figures. Needs the user's sign-off the same way D-P1/D-P2 needed it
(implemented because it is needed to make progress and is, in this
implementer's judgement, unambiguously correct -- but it is a policy
choice, not a mechanical port, so it belongs in docs/CLEANUP_DECISIONS_
PENDING.md until ratified).

average_invested_capital averages the current quarter's invested_capital
with the SAME quarter 4 periods earlier (YoY) -- the direct quarterly
analogue of the annual metric's "average of fiscal year start and fiscal
year end" (also, in effect, a 12-months-apart average), not a sequential
quarter-over-quarter average which would reintroduce seasonality.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import duckdb

from stock_agent.metrics.annual import compute_company_year
from stock_agent.policies.prior_fiscal_year_lookup import combine_current_and_prior_invested_capital
from stock_agent.policies.roic_nopat import combine_average_invested_capital_and_nopat_into_roic
from stock_agent.policies.tax_normalization import compute_normalized_tax_nopat

# The 8 metrics compute_company_year already resolves generically for any
# (accession_number, report_date) pair -- reused unchanged here.
BALANCE_SHEET_METRIC_NAMES = [
    "current_debt", "long_term_debt", "total_debt", "cash_and_equivalents",
    "short_term_investments", "stockholders_equity", "adjusted_net_debt", "invested_capital",
]

# The 4 additional metrics this module computes on top of those 8:
# effective_tax_rate/nopat (TTM), average_invested_capital, roic.
DERIVED_METRIC_NAMES = ["effective_tax_rate", "nopat", "average_invested_capital", "roic"]

ALL_NEW_QUARTERLY_METRIC_NAMES = BALANCE_SHEET_METRIC_NAMES + DERIVED_METRIC_NAMES

ENGINE_VERSION = "quarterly_balance_sheet_v1"

SUCCESSFUL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "PASS_NORMALIZED_TAX"}


def _quarter_identity(
    connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year_end: str, fiscal_quarter: str
) -> dict[str, str] | None:
    """(period_end, accession_number) already resolved by Engine V5 for
    this quarter -- every one of the 6 existing metric rows for a given
    quarter shares the same accession_number/period_end, so any one
    metric_name works as the anchor. Returns None if this quarter was
    never loaded by Engine V5 at all."""
    row = connection.execute(
        """
        SELECT qmr.period_end, qmr.accession_number
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qer.ticker = ? AND qmr.fiscal_year_end = ? AND qmr.fiscal_quarter = ?
          AND qmr.period_end IS NOT NULL AND qmr.accession_number IS NOT NULL
        LIMIT 1
        """,
        [ticker, fiscal_year_end, fiscal_quarter],
    ).fetchone()
    if row is None:
        return None
    return {"period_end": row[0], "accession_number": row[1]}


def compute_quarterly_balance_sheet_metrics(
    warehouse_connection: duckdb.DuckDBPyConnection,
    quarterly_connection: duckdb.DuckDBPyConnection,
    ticker: str,
    fiscal_year_end: str,
    fiscal_quarter: str,
) -> dict[str, dict[str, object]] | None:
    """The 8 balance-sheet metrics for one company-quarter, resolved
    directly from the quarter's own (accession_number, report_date) via
    the SAME compute_company_year the annual engine uses -- unchanged,
    not reimplemented. Returns None if this quarter was never loaded by
    Engine V5 (no accession/period_end to anchor to)."""
    identity = _quarter_identity(quarterly_connection, ticker, fiscal_year_end, fiscal_quarter)
    if identity is None:
        return None

    core = compute_company_year(warehouse_connection, ticker, identity["period_end"], identity["accession_number"])
    return {
        name: core[name]
        for name in BALANCE_SHEET_METRIC_NAMES
    }


def _quarterly_income_statement_metric(
    connection: duckdb.DuckDBPyConnection, ticker: str, fiscal_year_end: str, fiscal_quarter: str, metric_name: str
) -> float | None:
    row = connection.execute(
        """
        SELECT qmr.value FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qer.ticker = ? AND qmr.fiscal_year_end = ? AND qmr.fiscal_quarter = ?
          AND qmr.metric_name = ? AND qmr.result_status = 'PASS' AND qmr.value IS NOT NULL
        """,
        [ticker, fiscal_year_end, fiscal_quarter, metric_name],
    ).fetchone()
    return float(row[0]) if row is not None else None


def compute_ttm_nopat(
    connection: duckdb.DuckDBPyConnection,
    ticker: str,
    fiscal_year_end: str,
    fiscal_quarter: str,
    quarters_before_fn,
) -> dict[str, object]:
    """Trailing-twelve-month NOPAT/effective_tax_rate: sums pretax_income,
    income_tax_expense, and operating_income across this quarter and the
    3 immediately preceding it (via `quarters_before_fn`, the same
    gap-tolerant chronological walker quarterly_trend_v1 already uses),
    then applies D-015 item 3/4's plain formula, falling back to D-027
    Policy D's 21%-normalized-rate formula when the summed figures need
    it (compute_normalized_tax_nopat, unchanged from the annual policy).
    REVIEW_REQUIRED if fewer than 4 quarters of durational history exist
    yet, or any of the 4 quarters' 3 income-statement metrics is missing."""
    trailing = quarters_before_fn(connection, ticker, fiscal_year_end, fiscal_quarter, 3)
    if len(trailing) < 3:
        return {
            "effective_tax_rate": {"status": "REVIEW_REQUIRED", "value": None, "error": "fewer than 4 quarters of history"},
            "nopat": {"status": "REVIEW_REQUIRED", "value": None, "error": "fewer than 4 quarters of history"},
        }

    quarters = trailing + [(fiscal_year_end, fiscal_quarter)]
    pretax_sum = tax_sum = op_income_sum = 0.0
    for fye, q in quarters:
        pretax = _quarterly_income_statement_metric(connection, ticker, fye, q, "pretax_income")
        tax = _quarterly_income_statement_metric(connection, ticker, fye, q, "income_tax_expense")
        op_income = _quarterly_income_statement_metric(connection, ticker, fye, q, "operating_income")
        if pretax is None or tax is None or op_income is None:
            return {
                "effective_tax_rate": {"status": "REVIEW_REQUIRED", "value": None, "error": f"missing income-statement metric in TTM window at {fye} {q}"},
                "nopat": {"status": "REVIEW_REQUIRED", "value": None, "error": f"missing income-statement metric in TTM window at {fye} {q}"},
            }
        pretax_sum += pretax
        tax_sum += tax
        op_income_sum += op_income

    normalized = compute_normalized_tax_nopat(pretax_sum, tax_sum, op_income_sum)
    if normalized is not None:
        return {
            "effective_tax_rate": {"status": normalized["status"], "value": normalized["effective_tax_rate"], "basis": normalized["basis"]},
            "nopat": {"status": normalized["status"], "value": normalized["nopat"], "basis": normalized["basis"]},
        }

    effective_tax_rate = tax_sum / pretax_sum
    nopat = op_income_sum * (1 - effective_tax_rate)
    return {
        "effective_tax_rate": {"status": "PASS", "value": effective_tax_rate, "basis": "TTM_PLAIN_RATE"},
        "nopat": {"status": "PASS", "value": nopat, "basis": "TTM_PLAIN_RATE"},
    }


def compute_quarterly_average_invested_capital_and_roic(
    warehouse_connection: duckdb.DuckDBPyConnection,
    quarterly_connection: duckdb.DuckDBPyConnection,
    ticker: str,
    fiscal_year_end: str,
    fiscal_quarter: str,
    current_invested_capital: dict[str, object],
    quarters_before_fn,
) -> dict[str, dict[str, object]]:
    """average_invested_capital: average of THIS quarter's invested_capital
    and the SAME quarter 4 periods earlier (YoY, not sequential -- see
    module docstring). roic: TTM nopat / average_invested_capital, via the
    same combiner the annual engine uses (policies.roic_nopat), unchanged."""
    prior_quarters = quarters_before_fn(quarterly_connection, ticker, fiscal_year_end, fiscal_quarter, 4)
    if len(prior_quarters) < 4:
        avg_ic = {"status": "REVIEW_REQUIRED", "value": None, "error": "fewer than 4 quarters of history for a YoY comparison"}
    else:
        prior_fye, prior_q = prior_quarters[0]
        prior_metrics = compute_quarterly_balance_sheet_metrics(warehouse_connection, quarterly_connection, ticker, prior_fye, prior_q)
        if prior_metrics is None:
            avg_ic = {"status": "REVIEW_REQUIRED", "value": None, "error": "prior-year quarter was never loaded by Engine V5"}
        else:
            avg_ic = combine_current_and_prior_invested_capital(
                current_invested_capital["status"], current_invested_capital["value"],
                prior_metrics["invested_capital"]["status"], prior_metrics["invested_capital"]["value"],
            )

    ttm = compute_ttm_nopat(quarterly_connection, ticker, fiscal_year_end, fiscal_quarter, quarters_before_fn)
    roic = combine_average_invested_capital_and_nopat_into_roic(
        avg_ic["status"], avg_ic["value"], ttm["nopat"]["status"], ttm["nopat"]["value"],
    )

    return {
        "average_invested_capital": avg_ic,
        "effective_tax_rate": ttm["effective_tax_rate"],
        "nopat": ttm["nopat"],
        "roic": roic,
    }


def compute_all_new_quarterly_metrics(
    warehouse_connection: duckdb.DuckDBPyConnection,
    quarterly_connection: duckdb.DuckDBPyConnection,
    ticker: str,
    fiscal_year_end: str,
    fiscal_quarter: str,
    quarters_before_fn,
) -> dict[str, dict[str, object]] | None:
    """The full 12 new metrics for one company-quarter (8 balance-sheet +
    effective_tax_rate + nopat + average_invested_capital + roic). Returns
    None if this quarter was never loaded by Engine V5."""
    balance_sheet = compute_quarterly_balance_sheet_metrics(warehouse_connection, quarterly_connection, ticker, fiscal_year_end, fiscal_quarter)
    if balance_sheet is None:
        return None

    derived = compute_quarterly_average_invested_capital_and_roic(
        warehouse_connection, quarterly_connection, ticker, fiscal_year_end, fiscal_quarter,
        balance_sheet["invested_capital"], quarters_before_fn,
    )

    return {**balance_sheet, **derived}


def list_all_engine_v5_quarters(quarterly_connection: duckdb.DuckDBPyConnection) -> list[dict]:
    """Every (run_id, ticker, fiscal_year_end, fiscal_quarter, period_end,
    accession_number) Engine V5 has already loaded -- the universe this
    module's batch orchestration walks. One row per company-quarter."""
    rows = quarterly_connection.execute(
        """
        SELECT DISTINCT qer.run_id, qer.ticker, qmr.fiscal_year_end, qmr.fiscal_quarter,
               qmr.period_end, qmr.accession_number
        FROM quarterly_metric_results qmr
        JOIN quarterly_extraction_runs qer ON qer.run_id = qmr.run_id
        WHERE qmr.period_end IS NOT NULL AND qmr.accession_number IS NOT NULL
        """
    ).fetchall()
    return [
        {"run_id": r[0], "ticker": r[1], "fiscal_year_end": r[2], "fiscal_quarter": r[3],
         "period_end": r[4], "accession_number": r[5]}
        for r in rows
    ]


def build_balance_sheet_cache(
    warehouse_db_path: str, quarters: list[dict], max_workers: int = 8,
) -> dict[str, dict[str, dict[str, object]]]:
    """Computes the 8 balance-sheet metrics ONCE per DISTINCT accession_
    number across the whole batch -- not once per quarter that references
    it, and not once per LATER quarter that needs it again as a "prior
    year" comparison -- concurrently across accessions. Each worker opens
    its OWN short-lived read-only connection to the warehouse (multiple
    concurrent read-only DuckDB connections to the same file already
    verified safe by this project's own D-072 testing; never share one
    connection object across threads). Returns
    {accession_number: {metric_name: {status, value, ...}}}."""
    by_accession: dict[str, dict] = {}
    for q in quarters:
        by_accession.setdefault(q["accession_number"], q)  # dedupe -- any one quarter sharing this accession will do

    def _compute(accession_number: str, ticker: str, report_date: str) -> dict:
        connection = duckdb.connect(warehouse_db_path, read_only=True)
        try:
            core = compute_company_year(connection, ticker, report_date, accession_number)
            return {name: core[name] for name in BALANCE_SHEET_METRIC_NAMES}
        finally:
            connection.close()

    cache: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_compute, acc, q["ticker"], q["period_end"]): acc
            for acc, q in by_accession.items()
        }
        for future in as_completed(futures):
            accession_number = futures[future]
            cache[accession_number] = future.result()
    return cache


def compute_derived_metrics_from_cache(
    warehouse_connection: duckdb.DuckDBPyConnection,
    quarterly_connection: duckdb.DuckDBPyConnection,
    balance_sheet_cache: dict[str, dict[str, dict[str, object]]],
    quarter: dict,
    quarters_before_fn,
) -> dict[str, dict[str, object]]:
    """average_invested_capital/effective_tax_rate/nopat/roic for one
    quarter, using `balance_sheet_cache` as a WRITE-THROUGH cache keyed
    by accession_number -- a cache hit (e.g. pre-warmed by
    build_balance_sheet_cache, or already computed for an earlier
    quarter that happens to share this accession) skips
    compute_company_year entirely; a miss computes it once and populates
    the cache in place, so later quarters that need the SAME accession
    (most commonly: a later quarter's own "4 quarters ago" YoY lookup)
    never recompute it either. This lets a caller process quarters
    incrementally (one at a time, writing + checkpointing as it goes)
    while still getting the batch path's full memoization benefit,
    instead of requiring an upfront, all-or-nothing cache build."""
    def _cached(accession_number: str, ticker: str, report_date: str) -> dict:
        if accession_number not in balance_sheet_cache:
            core = compute_company_year(warehouse_connection, ticker, report_date, accession_number)
            balance_sheet_cache[accession_number] = {name: core[name] for name in BALANCE_SHEET_METRIC_NAMES}
        return balance_sheet_cache[accession_number]

    current = _cached(quarter["accession_number"], quarter["ticker"], quarter["period_end"])

    prior_quarters = quarters_before_fn(quarterly_connection, quarter["ticker"], quarter["fiscal_year_end"], quarter["fiscal_quarter"], 4)
    if len(prior_quarters) < 4:
        avg_ic = {"status": "REVIEW_REQUIRED", "value": None, "error": "fewer than 4 quarters of history for a YoY comparison"}
    else:
        prior_fye, prior_q = prior_quarters[0]
        prior_identity = _quarter_identity(quarterly_connection, quarter["ticker"], prior_fye, prior_q)
        if prior_identity is None:
            avg_ic = {"status": "REVIEW_REQUIRED", "value": None, "error": "prior-year quarter was never loaded by Engine V5"}
        else:
            prior = _cached(prior_identity["accession_number"], quarter["ticker"], prior_identity["period_end"])
            avg_ic = combine_current_and_prior_invested_capital(
                current["invested_capital"]["status"], current["invested_capital"]["value"],
                prior["invested_capital"]["status"], prior["invested_capital"]["value"],
            )

    ttm = compute_ttm_nopat(quarterly_connection, quarter["ticker"], quarter["fiscal_year_end"], quarter["fiscal_quarter"], quarters_before_fn)
    roic = combine_average_invested_capital_and_nopat_into_roic(
        avg_ic["status"], avg_ic["value"], ttm["nopat"]["status"], ttm["nopat"]["value"],
    )

    return {
        **current,
        "average_invested_capital": avg_ic,
        "effective_tax_rate": ttm["effective_tax_rate"],
        "nopat": ttm["nopat"],
        "roic": roic,
    }


def already_loaded(
    production_connection: duckdb.DuckDBPyConnection, run_id: str, fiscal_quarter: str,
) -> bool:
    """True if this run_id/fiscal_quarter already has ANY of this
    module's new metric rows -- used to skip already-processed
    company-quarters, the same idempotency pattern quarterly_extension.
    already_loaded uses for whole company-years."""
    row = production_connection.execute(
        "SELECT 1 FROM quarterly_metric_results WHERE run_id = ? AND fiscal_quarter = ? "
        "AND engine_version = ? LIMIT 1",
        [run_id, fiscal_quarter, ENGINE_VERSION],
    ).fetchone()
    return row is not None


def write_quarterly_balance_sheet_metrics(
    production_connection: duckdb.DuckDBPyConnection,
    run_id: str,
    ticker: str,
    fiscal_year_end: str,
    fiscal_quarter: str,
    period_end: str,
    accession_number: str,
    metrics: dict[str, dict[str, object]],
) -> int:
    """INSERTs the new metric rows for one company-quarter under the SAME
    run_id Engine V5 already created for this (ticker, fiscal_year_end)
    -- adding rows, never touching or replacing the existing 6-metric
    rows (different metric_name values, same primary key columns
    (run_id, fiscal_quarter, metric_name) so no collision is possible).
    `accession_number`/`period_end` are the QUARTER's own identity
    (`quarterly_metric_results.accession_number` is NOT NULL, so this is
    required, not optional lineage) -- correct even for a derived metric
    like `roic` that has no single source concept of its own: it still
    traces back to this quarter's own filing. `reconciliation_status` is
    explicitly 'NOT_APPLICABLE_INSTANT_FACT' for every row here: there is
    no quarter-sum-to-annual reconciliation for a point-in-time (instant)
    balance-sheet quantity the way there is for a durational flow
    (D-035) -- each quarter's value simply stands alone as-of its own
    date. Returns the number of rows inserted."""
    created_at = datetime.now(timezone.utc).isoformat()
    n_inserted = 0
    for metric_name, result in metrics.items():
        lineage = {k: v for k, v in result.items() if k not in {"status", "value"}}
        production_connection.execute(
            "INSERT INTO quarterly_metric_results "
            "(run_id, ticker, fiscal_year_end, fiscal_quarter, metric_name, value, unit, result_status, "
            " extraction_basis, period_start, period_end, availability_date, accession_number, concept_qname, "
            " context_id, dimensions_json, lineage_json, reconciliation_status, reconciliation_difference, "
            " permitted_difference, created_at, engine_version, loaded_at, is_active) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                run_id, ticker, fiscal_year_end, fiscal_quarter, metric_name, result.get("value"), "iso4217:USD",
                result["status"], str(result.get("basis") or result["status"]), None, period_end, None,
                accession_number, result.get("concept_qname"), None, "{}", json.dumps(lineage, default=str),
                "NOT_APPLICABLE_INSTANT_FACT", None, None, created_at, ENGINE_VERSION, created_at, True,
            ],
        )
        n_inserted += 1
    return n_inserted


__all__ = [
    "ALL_NEW_QUARTERLY_METRIC_NAMES", "BALANCE_SHEET_METRIC_NAMES", "DERIVED_METRIC_NAMES",
    "ENGINE_VERSION", "already_loaded", "build_balance_sheet_cache", "compute_all_new_quarterly_metrics",
    "compute_derived_metrics_from_cache", "compute_quarterly_average_invested_capital_and_roic",
    "compute_quarterly_balance_sheet_metrics", "compute_ttm_nopat", "list_all_engine_v5_quarters",
    "write_quarterly_balance_sheet_metrics",
]
