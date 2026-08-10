"""
metrics/annual.py — orchestrators composing extraction/ + policies/ into
full metric results for one company-year, in the same precedence order
production actually used.

``compute_company_year`` is the direct port of scripts/92_groups_1_3_4_
debt_facility_aggregate_policy.py's own ``compute_company_year`` (D-015/
D-016/D-017/D-018/D-019/D-022/D-026/D-027 A/B/C combined) — current_debt
resolution tier order: (1) explicit total row, (2) calculation-linkbase-
verified components, (3) presentation-sibling components, (4) presentation-
ancestry classification, (5) D-017's 4-condition maturity-schedule zero
proof, (6) Policy B explicit-undrawn-revolver zero, (7) Policy B's
broader structural-absence zero — extended here, after the identical
scripts/92 body, with two further fallback tiers scripts/92 itself does
not contain (they were added by later, narrower scripts reusing 92's
engine unchanged):
  (8) D-032/D-033 manual SEC-HTML fact recovery (exactly two hardcoded,
      narrowly-scoped overrides — see policies/manual_fact_recovery.py).
  (9) D-028 maturity-basis-as-current_debt (see policies/
      debt_current_long_term.py's resolve_current_debt_maturity_basis_
      fallback).
  Otherwise: REVIEW_REQUIRED.

total_debt resolution is SCOPE-gated, reproducing exactly which of the
two historical resolvers production actually used for each accession
(see policies/debt_total_aggregate.py's module docstring for the full
evidence): scripts/92's Policy C tier (PASS_DIRECT_AGGREGATE-capable)
is applied only within its own original 12-filing TARGET_FILINGS scope
(POLICY_C_APPLIED_SCOPE); scripts/79's narrower, pre-Policy-C 2-tier
precedence (resolve_total_debt_maturity_basis_d022) is applied within
ITS own original 10-filing scope (D022_APPLIED_SCOPE — AMZN/GOOGL
2021-2025); outside both scopes, scripts/92's resolver is used as the
default (safe because tier 1, GAAP_CARRYING_VALUE, is byte-identical
between the two, and it is the only tier that ever actually fires for
every accession outside both scopes — verified empirically against the
full 45+5 dataset, not assumed).

``compute_full_company_year`` is new orchestration glue (not a copy of
any single script) that composes ``compute_company_year``'s 8 metrics
with the cross-company-year policies (prior-fiscal-year average_
invested_capital lookup, normalized-tax NOPAT, final ROIC combination)
and passes through the 9 metric families whose row identification is
out of scope for this PR (revenue, net_income, operating_income,
pretax_income, income_tax_expense, operating_cash_flow, capex,
free_cash_flow — always sourced, in the live financial_metric_results
table, from scripts/60/69, confirmed by direct query: these 9 metrics
have exactly 2 possible engine_version values — v14/v15 — across all
45 company-years, zero exceptions). See docs/PR1 report for the full
scoping rationale.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from stock_agent.extraction.core import (
    SUCCESSFUL_STATUSES,
    BUILT_IN_METRICS,
    TargetRowNotFound,
    identify_canonical_row,
    match_facts_from_warehouse,
    reconstruct_presentation_dataframe,
)
from stock_agent.policies.debt_current_long_term import (
    classify_maturity_buckets,
    find_debt_maturity_schedule_role,
    resolve_current_debt_components_from_warehouse,
    resolve_current_debt_maturity_basis_fallback,
    resolve_current_debt_with_facility_policy,
    resolve_long_term_debt,
)
from stock_agent.policies.debt_total_aggregate import (
    D022_APPLIED_SCOPE,
    POLICY_C_APPLIED_SCOPE,
    resolve_total_debt_maturity_basis_d022,
    resolve_total_debt_with_aggregate_policy,
)
from stock_agent.policies.current_asset_classification import resolve_current_asset_row
from stock_agent.policies.manual_fact_recovery import recovered_current_debt_zero
from stock_agent.policies.prior_fiscal_year_lookup import combine_current_and_prior_invested_capital
from stock_agent.policies.roic_nopat import combine_average_invested_capital_and_nopat_into_roic
from stock_agent.policies.short_term_investments import attempt_short_term_investments_zero_proof
from stock_agent.policies.tax_normalization import compute_normalized_tax_nopat
from stock_agent.metrics import production_lookup

# The 9 metric families whose row identification is always sourced, in the
# live production table, from scripts/60 (v14) / scripts/69 (v15) — out of
# scope for this PR (see module docstring). Read through unchanged.
OUT_OF_SCOPE_PASSTHROUGH_METRICS = [
    "revenue", "net_income", "operating_income", "pretax_income",
    "income_tax_expense", "operating_cash_flow", "capex", "free_cash_flow",
]

ALL_20_METRIC_NAMES = [
    "revenue", "operating_income", "net_income", "operating_cash_flow", "capex",
    "free_cash_flow", "cash_and_equivalents", "short_term_investments",
    "current_debt", "long_term_debt", "total_debt", "adjusted_net_debt",
    "stockholders_equity", "invested_capital", "average_invested_capital",
    "pretax_income", "income_tax_expense", "effective_tax_rate", "nopat", "roic",
]


def _resolve_short_term_investments_by_ancestry(
    presentation: pd.DataFrame,
    metric,
) -> dict[str, object] | None:
    """Collects the balance-sheet rows whose label matches the metric's
    own vocabulary, then lets current-assets ancestry choose between
    them. Returns None whenever the structure does not settle it."""
    if presentation.empty:
        return None

    role_mask = presentation["role_definition"].astype(str).str.contains(
        metric.role_include_pattern, case=False, regex=True, na=False)
    if metric.role_exclude_pattern:
        role_mask &= ~presentation["role_definition"].astype(str).str.contains(
            metric.role_exclude_pattern, case=False, regex=True, na=False)

    candidates = presentation[
        role_mask
        & ~presentation["is_abstract"].astype(bool)
        & presentation["label"].astype(str).str.contains(
            metric.mention_pattern, case=False, regex=True, na=False)
    ]
    return resolve_current_asset_row(presentation, candidates)


def _reconstruct_simple_metric(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    metric_name: str,
    report_date: str,
) -> dict[str, object]:
    """Ported byte-exact from scripts/92, extended with two
    short_term_investments fallbacks — each tried only for that metric,
    and only after the standard label-based row identification has
    already failed:

      1. current-assets ancestry (policies.current_asset_classification),
         for a label that is genuinely ambiguous because the filer uses
         the SAME text twice. Measured on Apple, whose balance sheet
         carries two rows both reading "Marketable securities", one under
         AssetsCurrentAbstract and one under AssetsNoncurrentAbstract.
         Position settles what the label cannot.
      2. the D-030 zero-proof (scripts/99), for a filing that genuinely
         reports no short-term investments at all.
    """
    metric = BUILT_IN_METRICS[metric_name]
    try:
        row, _candidates = identify_canonical_row(presentation, metric)
    except TargetRowNotFound as exc:
        if metric_name == "short_term_investments":
            ancestry_row = _resolve_short_term_investments_by_ancestry(presentation, metric)
            if ancestry_row is not None:
                decision = match_facts_from_warehouse(
                    connection, accession_number, ancestry_row["concept_qname"],
                    report_date, "instant",
                )
                if decision["status"] in SUCCESSFUL_STATUSES:
                    return {
                        "status": decision["status"], "error": decision.get("error"),
                        "value": decision.get("value"),
                        "concept_qname": ancestry_row["concept_qname"],
                        "selection_tier": ancestry_row["selection_tier"],
                        "basis": ancestry_row["basis"],
                    }

            zero_proof = attempt_short_term_investments_zero_proof(
                connection, accession_number, presentation, report_date
            )
            if zero_proof["status"] == "PASS":
                return zero_proof
        return {"status": "REVIEW_REQUIRED", "error": str(exc), "value": None}

    decision = match_facts_from_warehouse(
        connection, accession_number, row["concept_qname"], report_date, "instant"
    )
    return {
        "status": decision["status"],
        "error": decision.get("error"),
        "value": decision.get("value"),
        "concept_qname": row["concept_qname"],
        "selection_tier": row["selection_tier"],
    }


def _resolve_current_debt(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
    ltd: dict[str, object],
) -> dict[str, object]:
    """current_debt resolution: scripts/92's own tiers (1-7) followed by
    the two later fallback tiers (D-032/D-033 manual recovery, D-028
    maturity basis) — see module docstring for the full tier list."""

    cd_resolution = resolve_current_debt_with_facility_policy(
        connection, accession_number, presentation, report_date, ltd
    )

    if cd_resolution["mode"] == "components":
        values = []
        component_ok = True
        for r in cd_resolution["rows"]:
            decision = match_facts_from_warehouse(
                connection, accession_number, r["concept_qname"], report_date, "instant"
            )
            if decision["status"] != "PASS":
                component_ok = False
                break
            values.append(decision["value"])
        if component_ok:
            return {
                "status": "PASS",
                "value": sum(values),
                "concept_qname": " + ".join(r["concept_qname"] for r in cd_resolution["rows"]),
                "selection_tier": cd_resolution["rows"][0]["selection_tier"],
            }
        # a claimed component failed to resolve to a single value — fall
        # through to REVIEW_REQUIRED below (scripts/92 behavior, unchanged)
        cd_resolution = {"mode": "review_required", "error": "a claimed current_debt component did not resolve to a single value"}

    if cd_resolution["mode"] == "zero":
        return {
            "status": "PASS",
            "value": cd_resolution["value"],
            "concept_qname": cd_resolution["concept_qname"],
            "selection_tier": cd_resolution["selection_tier"],
            "basis": cd_resolution["basis"],
            "lineage": cd_resolution["lineage"],
        }

    # mode == "review_required" — try the two post-scripts/92 fallback tiers.
    manual_recovery = recovered_current_debt_zero(accession_number)
    if manual_recovery is not None:
        return manual_recovery

    maturity = classify_maturity_buckets(connection, accession_number, presentation, report_date)
    maturity_fallback = resolve_current_debt_maturity_basis_fallback(maturity)
    if maturity_fallback is not None:
        return maturity_fallback

    return {"status": "REVIEW_REQUIRED", "error": cd_resolution["error"], "value": None}


def compute_company_year(
    connection: duckdb.DuckDBPyConnection,
    ticker: str,
    report_date: str,
    accession_number: str,
) -> dict[str, object]:
    """The 8-metric debt/cash/equity engine: current_debt, long_term_debt,
    total_debt, cash_and_equivalents, short_term_investments,
    stockholders_equity, adjusted_net_debt, invested_capital. Ported from
    scripts/92's compute_company_year, extended with the two later
    fallback tiers described in the module docstring."""

    presentation = reconstruct_presentation_dataframe(connection, accession_number)

    ltd = resolve_long_term_debt(connection, accession_number, presentation, report_date)
    cd = _resolve_current_debt(connection, accession_number, presentation, report_date, ltd)

    # Scope-gated resolver selection -- see module docstring and
    # policies/debt_total_aggregate.py for the full evidence: production
    # never applied Policy C outside scripts/92's own original scope.
    if accession_number in D022_APPLIED_SCOPE and accession_number not in POLICY_C_APPLIED_SCOPE:
        total_debt = resolve_total_debt_maturity_basis_d022(
            connection, accession_number, presentation, report_date, cd, ltd
        )
    else:
        total_debt = resolve_total_debt_with_aggregate_policy(
            connection, accession_number, presentation, report_date, cd, ltd
        )

    cash = _reconstruct_simple_metric(connection, accession_number, presentation, "cash_and_equivalents", report_date)
    sti = _reconstruct_simple_metric(connection, accession_number, presentation, "short_term_investments", report_date)
    equity = _reconstruct_simple_metric(connection, accession_number, presentation, "stockholders_equity", report_date)

    def _combine_status(*statuses: str | None) -> str:
        if all(s in SUCCESSFUL_STATUSES for s in statuses):
            if "PASS_DIRECT_AGGREGATE" in statuses:
                return "PASS_DIRECT_AGGREGATE"
            if "PASS_MATURITY_BASIS" in statuses:
                return "PASS_MATURITY_BASIS"
            return "PASS"
        return "REVIEW_REQUIRED"

    adj_status = _combine_status(total_debt["status"], cash["status"], sti["status"])
    adjusted_net_debt = {
        "status": adj_status,
        "value": (
            total_debt["value"] - cash["value"] - sti["value"]
            if adj_status in SUCCESSFUL_STATUSES else None
        ),
        "basis": total_debt.get("basis"),
    }

    ic_status = _combine_status(total_debt["status"], equity["status"], cash["status"], sti["status"])
    invested_capital = {
        "status": ic_status,
        "value": (
            total_debt["value"] + equity["value"] - cash["value"] - sti["value"]
            if ic_status in SUCCESSFUL_STATUSES else None
        ),
        "basis": total_debt.get("basis"),
    }

    return {
        "ticker": ticker, "report_date": report_date, "accession_number": accession_number,
        "current_debt": cd, "long_term_debt": ltd, "total_debt": total_debt,
        "cash_and_equivalents": cash, "short_term_investments": sti, "stockholders_equity": equity,
        "adjusted_net_debt": adjusted_net_debt, "invested_capital": invested_capital,
    }


def compute_full_company_year(
    warehouse_connection: duckdb.DuckDBPyConnection,
    production_connection: duckdb.DuckDBPyConnection,
    ticker: str,
    report_date: str,
    accession_number: str,
) -> dict[str, dict[str, object]]:
    """
    Composes compute_company_year's 8 metrics with the remaining 12 to
    produce all 20 primary annual metrics for one company-year:

      - revenue, operating_income, net_income, operating_cash_flow, capex,
        free_cash_flow, pretax_income, income_tax_expense: read through
        from production (out of scope — see module docstring).
      - cash_and_equivalents, short_term_investments, current_debt,
        long_term_debt, total_debt, adjusted_net_debt, stockholders_equity,
        invested_capital: from compute_company_year (this PR's ported
        engine).
      - average_invested_capital: policies.prior_fiscal_year_lookup,
        combining this year's own invested_capital (just computed) with
        the prior fiscal year's invested_capital — read from production
        if available there, otherwise recomputed on demand via this same
        function (the supplementary-accession pattern, scripts/98/101/105).
      - effective_tax_rate, nopat: production pass-through UNLESS
        policies.tax_normalization's D-027 Policy D condition is met
        (pretax_income<=0 or reported rate outside [0,1]), in which case
        the normalized 21% rate is used instead.
      - roic: policies.roic_nopat, combining nopat and
        average_invested_capital exactly as scripts/95 did.
    """

    core = compute_company_year(warehouse_connection, ticker, report_date, accession_number)

    result: dict[str, dict[str, object]] = dict(core)

    for metric_name in OUT_OF_SCOPE_PASSTHROUGH_METRICS:
        looked_up = production_lookup.latest_metric(production_connection, ticker, report_date, metric_name)
        result[metric_name] = (
            {"status": looked_up["status"], "value": looked_up["value"], "basis": "PRODUCTION_PASSTHROUGH_OUT_OF_SCOPE"}
            if looked_up is not None
            else {"status": "REVIEW_REQUIRED", "value": None, "error": "no production row found"}
        )

    # --- average_invested_capital (policies.prior_fiscal_year_lookup) ------
    # Must use the SAME annual-only universe scripts/93 used: sec_filings
    # JOINed through extraction_runs (the annual pipeline's own run table —
    # sec_filings also carries every 10-Q, so filtering by ticker alone
    # would incorrectly pick up a quarterly report_date as "prior fiscal
    # year"; extraction_runs only ever has rows for 10-K accessions).
    company_years = production_lookup.all_company_years(production_connection)
    all_dates = [rd for (tkr, rd, _acc) in company_years if tkr == ticker]
    prior_report_date = production_lookup.prior_report_date_for(ticker, report_date, all_dates)

    if prior_report_date is None:
        # This ticker's first locked annual filing under the ported
        # prior-fiscal-year-lookup policy (scripts/93/98/101/105): no
        # separate, earlier locked 10-K exists to average against. A
        # small number of tickers (MSFT/ORCL's actual first year, and
        # MU/PANW's first year via an EARLIER, narrower within-filing
        # prior-period-date-tolerance mechanism — scripts/84/87, D-024 —
        # which predates and is superseded in general by scripts/93 but
        # was never reprocessed since it already PASSed) still carry a
        # valid production value computed by a mechanism out of scope
        # for this PR; read it through rather than force REVIEW_REQUIRED.
        looked_up = production_lookup.latest_metric(production_connection, ticker, report_date, "average_invested_capital")
        result["average_invested_capital"] = (
            {"status": looked_up["status"], "value": looked_up["value"], "basis": "PRODUCTION_PASSTHROUGH_OUT_OF_SCOPE"}
            if looked_up is not None
            else {"status": "REVIEW_REQUIRED", "value": None, "error": "no prior fiscal year in the dataset (this ticker's first locked filing)"}
        )
    else:
        # ALWAYS recompute the prior year from the warehouse; never read it
        # from production (D-P3, option B).
        #
        # This previously preferred a stored production value and only
        # recomputed when none was available. That made the engine's output
        # depend on what happened to be in the database, so loading
        # unrelated companies could silently change an already-verified
        # figure. Measured: PANW 2021-07-31's average_invested_capital was
        # PASS 698,750,000 until the universe expansion loaded PANW
        # 2020-07-31 for the first time as REVIEW_REQUIRED -- a lookup that
        # had found nothing (and so recomputed) then found a failed value
        # and propagated it.
        #
        # Recomputing unconditionally makes the result a function of the
        # filings alone. Production is still consulted for the prior
        # ACCESSION -- which filing to read -- but never for its value.
        prior_accession_row = production_connection.execute(
            "SELECT accession_number FROM sec_filings WHERE ticker = ? AND report_date = ?",
            [ticker, prior_report_date],
        ).fetchone()
        if prior_accession_row is None:
            prior_ic_status, prior_ic_value = "REVIEW_REQUIRED", None
        else:
            prior_core = compute_company_year(warehouse_connection, ticker, prior_report_date, prior_accession_row[0])
            prior_ic_status = prior_core["invested_capital"]["status"]
            prior_ic_value = prior_core["invested_capital"]["value"]

        result["average_invested_capital"] = combine_current_and_prior_invested_capital(
            core["invested_capital"]["status"], core["invested_capital"]["value"],
            prior_ic_status, prior_ic_value,
        )

    # --- effective_tax_rate / nopat (production baseline + D-027 Policy D) -
    etr_lookup = production_lookup.latest_metric(production_connection, ticker, report_date, "effective_tax_rate")
    nopat_lookup = production_lookup.latest_metric(production_connection, ticker, report_date, "nopat")

    normalized = None
    pretax = result.get("pretax_income", {})
    tax_expense = result.get("income_tax_expense", {})
    operating_income = result.get("operating_income", {})
    if (
        pretax.get("status") == "PASS"
        and tax_expense.get("status") == "PASS"
        and operating_income.get("status") == "PASS"
    ):
        normalized = compute_normalized_tax_nopat(pretax["value"], tax_expense["value"], operating_income["value"])

    if normalized is not None:
        result["effective_tax_rate"] = {"status": normalized["status"], "value": normalized["effective_tax_rate"], "basis": normalized["basis"]}
        result["nopat"] = {"status": normalized["status"], "value": normalized["nopat"], "basis": normalized["basis"]}
    else:
        result["effective_tax_rate"] = (
            {"status": etr_lookup["status"], "value": etr_lookup["value"], "basis": "PRODUCTION_PASSTHROUGH_OUT_OF_SCOPE"}
            if etr_lookup is not None else {"status": "REVIEW_REQUIRED", "value": None}
        )
        result["nopat"] = (
            {"status": nopat_lookup["status"], "value": nopat_lookup["value"], "basis": "PRODUCTION_PASSTHROUGH_OUT_OF_SCOPE"}
            if nopat_lookup is not None else {"status": "REVIEW_REQUIRED", "value": None}
        )

    # --- roic (policies.roic_nopat) -----------------------------------------
    result["roic"] = combine_average_invested_capital_and_nopat_into_roic(
        result["average_invested_capital"]["status"], result["average_invested_capital"]["value"],
        result["nopat"]["status"], result["nopat"]["value"],
    )

    return result
