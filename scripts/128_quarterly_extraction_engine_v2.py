"""
Quarterly extraction engine v2 — identical to
scripts/118_quarterly_extraction_engine.py in every respect EXCEPT how
the annual (FY 10-K) anchor value is resolved.

WHY: the read-only root-cause audit (scripts/127) found that 54 of 111
REVIEW_REQUIRED quarterly metric-year cases (ANNUAL_ROW_NOT_RESOLVED,
concentrated in MU and NVDA) fail because scripts/118 tries to
re-identify the annual statement row independently via
s89.identify_canonical_row on the FY 10-K — even though the exact same
accession/metric ALREADY has a resolved, authoritative value sitting in
the production `financial_metric_results` table (900/900 resolved, 0
REVIEW_REQUIRED). This is a general, non-ticker-specific implementation
gap: two different code paths solving the same "what is this filing's
annual X" question, one of which already works.

THE ONE CHANGE: instead of calling `resolve_concept_for_metric` +
`facts_for_concept` + `pick_current_period_fact` against the warehouse for
the FY filing, the annual value/concept/lineage are read directly from the
already-resolved `financial_metric_results` row for the exact same FY
accession and metric_name (joined through `extraction_runs` by
accession_number) — fail-closed (REVIEW_REQUIRED, never a guess, never a
fallback to another filing) if that lookup does not return exactly one row
with an accepted status. The XBRL `decimals` value needed for the
unchanged precision-tolerance calculation is fetched from the warehouse
using the SAME already-identified concept_qname + context_id + accession
(not a new selection — a metadata lookup on an already-pinned fact).

UNCHANGED from scripts/118 (verbatim, not re-derived):
  - 10-Q (Q1/Q2/Q3) concept/row resolution (s89.identify_canonical_row)
  - statement-role selection, context selection, duration classification
  - DIRECT_QUARTER / DERIVED_FROM_YTD logic
  - Q4 = Annual - Q3_9mYTD equation
  - filing_date availability logic
  - same-context precision-duplicate reconciliation
  - XBRL-decimals rounding-tolerance policy (D-035) and its pure function
  - REVIEW_REQUIRED behavior and overall output structure

Read-only against both databases; writes nothing to either.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

QUARTER_DURATION_MIN_DAYS = 89
QUARTER_DURATION_MAX_DAYS = 92
YTD_6M_MIN_DAYS, YTD_6M_MAX_DAYS = 181, 184
YTD_9M_MIN_DAYS, YTD_9M_MAX_DAYS = 271, 275
YTD_12M_MIN_DAYS, YTD_12M_MAX_DAYS = 364, 366

METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense",
           "operating_cash_flow", "capex"]

ACCEPTED_ANNUAL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_NORMALIZED_TAX", "PASS_DIRECT_AGGREGATE"}

_spec = importlib.util.spec_from_file_location(
    "s89", PROJECT_DIR / "scripts" / "89_panw_zero_long_term_debt_policy.py"
)
s89 = importlib.util.module_from_spec(_spec)
sys.modules["s89"] = s89
_spec.loader.exec_module(s89)


# =====================================================================
# 1. DURATION CLASSIFICATION — unchanged from 118
# =====================================================================

def duration_days(period_start: str, period_end: str) -> int | None:
    try:
        return (date.fromisoformat(period_end) - date.fromisoformat(period_start)).days
    except (TypeError, ValueError):
        return None


def classify_duration(days: int | None) -> str | None:
    if days is None:
        return None
    if QUARTER_DURATION_MIN_DAYS <= days <= QUARTER_DURATION_MAX_DAYS:
        return "quarter"
    if YTD_6M_MIN_DAYS <= days <= YTD_6M_MAX_DAYS:
        return "ytd_6m"
    if YTD_9M_MIN_DAYS <= days <= YTD_9M_MAX_DAYS:
        return "ytd_9m"
    if YTD_12M_MIN_DAYS <= days <= YTD_12M_MAX_DAYS:
        return "ytd_12m"
    return "other"


# =====================================================================
# 2. FILING METADATA LOOKUP — unchanged from 118
# =====================================================================

def load_filing_metadata(prod_connection, accession_number: str, expected_form: str) -> dict:
    row = prod_connection.execute(
        "SELECT ticker, form, report_date, filing_date FROM sec_filings WHERE accession_number = ?",
        [accession_number],
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Accession {accession_number} not found in sec_filings. Refusing to guess.")
    ticker, form, report_date, filing_date = row
    if form != expected_form:
        raise RuntimeError(f"Accession {accession_number} has form={form!r}, expected {expected_form!r}. Refusing to proceed.")
    return {"ticker": ticker, "form": form, "report_date": str(report_date), "filing_date": str(filing_date),
            "accession_number": accession_number}


# =====================================================================
# 3. FACT SELECTION (Q1/Q2/Q3 only in v2) — unchanged from 118
# =====================================================================

def resolve_concept_for_metric(connection, accession_number: str, metric_name: str) -> str:
    presentation = s89.reconstruct_presentation_dataframe(connection, accession_number)
    metric = s89.BUILT_IN_METRICS[metric_name]
    row, _candidates = s89.identify_canonical_row(presentation, metric)
    return row["concept_qname"]


def facts_for_concept(connection, accession_number: str, concept_qname: str) -> pd.DataFrame:
    facts = connection.execute(
        """
        SELECT value_numeric, period_start, period_end, context_id, unit_id, decimals
        FROM xbrl_facts
        WHERE accession_number = ? AND concept_qname = ?
          AND dimensions_json = '{}' AND is_nil = FALSE AND value_numeric IS NOT NULL
          AND period_start IS NOT NULL AND period_end IS NOT NULL
        """,
        [accession_number, concept_qname],
    ).fetchdf()
    facts = facts.drop_duplicates(subset=["period_start", "period_end", "value_numeric"])
    if not facts.empty:
        facts, _notes = s89._reconcile_same_context_precision_duplicates_from_warehouse(facts)
    return facts


def pick_current_period_fact(facts: pd.DataFrame, expected_end_date: str, duration_class: str) -> dict | None:
    matches = []
    for _, row in facts.iterrows():
        if str(row["period_end"]) != expected_end_date:
            continue
        days = duration_days(str(row["period_start"]), str(row["period_end"]))
        if classify_duration(days) != duration_class:
            continue
        matches.append((row, days))

    distinct_values = {round(r["value_numeric"], 2) for r, _ in matches}
    if len(distinct_values) != 1:
        return None

    row, days = matches[0]
    return {
        "value": float(row["value_numeric"]), "period_start": str(row["period_start"]),
        "period_end": str(row["period_end"]), "duration_days": days,
        "context_id": str(row["context_id"]), "unit_id": str(row["unit_id"]), "decimals": str(row["decimals"]),
    }


# =====================================================================
# 3b. ANNUAL ANCHOR RESOLUTION — THE ONE CHANGE. Reads the already-
#     authoritative annual production result for the exact FY accession
#     + metric, instead of re-identifying the row against the warehouse.
#     Fail-closed on 0 rows, >1 rows, accession mismatch, unsupported
#     status, or missing concept — never guesses, never falls back to
#     another filing.
# =====================================================================

def resolve_annual_anchor(prod_connection, fy_accession_number: str, metric_name: str) -> dict:
    rows = prod_connection.execute(
        """
        SELECT f.value, f.unit, f.source_concept, f.status, f.extraction_run_id,
               f.context_id, f.period_start, f.period_end, f.label,
               f.statement_role_definition, f.selection_tier, f.validation_reason,
               r.accession_number, r.loaded_at
        FROM financial_metric_results f
        JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id
        WHERE r.accession_number = ? AND f.metric_name = ?
        """,
        [fy_accession_number, metric_name],
    ).fetchall()

    query_evidence = {"accession_number": fy_accession_number, "metric_name": metric_name, "rows_found": len(rows)}

    if len(rows) == 0:
        return {"resolved": False, "reason": "annual production lookup returned 0 rows for this exact accession and metric",
                "query_evidence": query_evidence}
    if len(rows) > 1:
        return {"resolved": False, "reason": f"annual production lookup returned {len(rows)} rows (ambiguous) for this exact accession and metric",
                "query_evidence": {**query_evidence, "candidate_statuses": [r[3] for r in rows]}}

    (value, unit, source_concept, status, extraction_run_id, context_id, period_start, period_end,
     label, statement_role_definition, selection_tier, validation_reason, accession_number, loaded_at) = rows[0]

    if accession_number != fy_accession_number:
        return {"resolved": False, "reason": "accession mismatch between query filter and returned row (should be impossible)",
                "query_evidence": query_evidence}

    if status not in ACCEPTED_ANNUAL_STATUSES:
        return {"resolved": False, "reason": f"annual status {status!r} is not in the accepted set {sorted(ACCEPTED_ANNUAL_STATUSES)}",
                "query_evidence": {**query_evidence, "actual_status": status}}

    if source_concept is None:
        return {"resolved": False, "reason": "annual row is missing source_concept (required lineage) — refusing to use an unattributed value",
                "query_evidence": query_evidence}

    return {
        "resolved": True, "value": value, "unit": unit, "concept_qname": source_concept, "status": status,
        "extraction_run_id": extraction_run_id, "loaded_at": str(loaded_at), "context_id": context_id,
        "period_start": str(period_start) if period_start else None, "period_end": str(period_end) if period_end else None,
        "label": label, "statement_role_definition": statement_role_definition,
        "selection_tier": selection_tier, "validation_reason": validation_reason,
        "accession_number": accession_number, "query_evidence": query_evidence,
    }


def lookup_annual_fact_decimals(warehouse_connection, accession_number: str, concept_qname: str | None, context_id: str | None) -> str | None:
    """Metadata-only lookup on an already-fully-identified fact (exact
    accession + concept + context) — not a new selection. A single
    (accession, concept, context) key can still legitimately carry more
    than one raw row at different XBRL precisions (the same known
    same-context rounding-duplicate pattern every other fact lookup in
    this engine already resolves via s89's unchanged, already-approved
    `_reconcile_same_context_precision_duplicates_from_warehouse` — e.g.
    a value tagged once at decimals=-6 in a statement table and again at
    decimals=-8 in narrative prose). Reusing that exact same function
    here (not new logic, not a new selection rule) collapses that down to
    the single most-precise decimals value the same way the rest of the
    engine already would. Returns None (fail-closed downstream) only if
    even after reconciliation more than one distinct decimals value
    remains — a genuine, unresolved conflict, not guessed away."""
    if not concept_qname or not context_id:
        return None
    facts = warehouse_connection.execute(
        "SELECT value_numeric, context_id, decimals FROM xbrl_facts WHERE accession_number = ? AND concept_qname = ? "
        "AND context_id = ? AND is_nil = FALSE",
        [accession_number, concept_qname, context_id],
    ).fetchdf()
    if facts.empty:
        return None
    facts = facts.drop_duplicates(subset=["context_id", "value_numeric", "decimals"])
    reconciled, _notes = s89._reconcile_same_context_precision_duplicates_from_warehouse(facts)
    distinct_decimals = reconciled["decimals"].unique()
    if len(distinct_decimals) == 1:
        return distinct_decimals[0]
    return None


# =====================================================================
# 4. XBRL-DECIMALS ROUNDING-TOLERANCE POLICY — unchanged from 118 (D-035)
# =====================================================================

def parse_decimals(decimals_str: str | None) -> int | None:
    if decimals_str is None:
        return None
    text = str(decimals_str).strip()
    if text.upper() == "INF":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def uncertainty_for_decimals(decimals_str: str | None) -> tuple[float, float | None]:
    decimals = parse_decimals(decimals_str)
    if decimals is None:
        return 0.0, None
    rounding_unit = 10 ** (-decimals)
    uncertainty = rounding_unit / 2
    return uncertainty, rounding_unit


def compute_precision_aware_reconciliation(
    q1_value: float, q1_decimals: str,
    q2_value: float, q2_basis: str, q2_source_decimals: str,
    q3_value: float, q3_basis: str, q3_source_decimals: str,
    q4_value: float,
    annual_value: float, annual_decimals: str,
) -> dict:
    quarter_sum = q1_value + q2_value + q3_value + q4_value
    difference = quarter_sum - annual_value
    exact_status = "PASS" if abs(difference) < 1 else "FAIL"

    q1_uncertainty, q1_rounding_unit = uncertainty_for_decimals(q1_decimals)
    q2_uncertainty, q2_rounding_unit = uncertainty_for_decimals(q2_source_decimals)
    q3_uncertainty, q3_rounding_unit = uncertainty_for_decimals(q3_source_decimals)
    annual_uncertainty, annual_rounding_unit = uncertainty_for_decimals(annual_decimals)

    permitted_difference = q1_uncertainty + q2_uncertainty + q3_uncertainty + annual_uncertainty

    precision_calculation = {
        "equation": "Q1 + Q2 + Q3 + Q4 vs. Annual (Q4 derived as Annual - Q3_9mYTD; "
                     "Q4's own uncertainty is already represented by the Annual and "
                     "Q3-source terms below, so it is not counted a second time)",
        "source_facts": {
            "Q1": {"decimals": q1_decimals, "rounding_unit": q1_rounding_unit, "uncertainty": q1_uncertainty},
            "Q2": {"basis": q2_basis, "decimals": q2_source_decimals, "rounding_unit": q2_rounding_unit, "uncertainty": q2_uncertainty},
            "Q3": {"basis": q3_basis, "decimals": q3_source_decimals, "rounding_unit": q3_rounding_unit, "uncertainty": q3_uncertainty},
            "Annual": {"decimals": annual_decimals, "rounding_unit": annual_rounding_unit, "uncertainty": annual_uncertainty},
        },
        "permitted_difference": permitted_difference, "actual_difference": difference,
        "within_tolerance": abs(difference) <= permitted_difference,
    }

    if exact_status == "PASS":
        final_status = "PASS"
    elif abs(difference) <= permitted_difference:
        final_status = "PASS_ROUNDING_TOLERANCE"
    else:
        final_status = "REVIEW_REQUIRED"

    return {"sum_q1_to_q4": quarter_sum, "annual_value": annual_value, "difference": difference,
            "exact_equality_status": exact_status, "precision_calculation": precision_calculation, "status": final_status}


# =====================================================================
# 5. THE ENGINE — same shape as 118; only the annual-anchor block differs
# =====================================================================

def run_quarterly_extraction_engine_v2(
    ticker: str, fiscal_year_end: str, q1_accession: str, q2_accession: str, q3_accession: str, fy_accession: str,
    json_output_path: str | Path, csv_output_path: str | Path,
) -> dict:
    json_output_path = Path(json_output_path)
    csv_output_path = Path(csv_output_path)

    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH), read_only=True)
    filings = {
        "Q1": load_filing_metadata(prod_connection, q1_accession, "10-Q"),
        "Q2": load_filing_metadata(prod_connection, q2_accession, "10-Q"),
        "Q3": load_filing_metadata(prod_connection, q3_accession, "10-Q"),
        "FY": load_filing_metadata(prod_connection, fy_accession, "10-K"),
    }

    for label, info in filings.items():
        if info["ticker"] != ticker:
            raise RuntimeError(f"{label} accession {info['accession_number']} belongs to ticker {info['ticker']!r}, expected {ticker!r}.")
    if filings["FY"]["report_date"] != fiscal_year_end:
        raise RuntimeError(f"FY accession {fy_accession}'s own report_date ({filings['FY']['report_date']}) "
                            f"does not match the supplied fiscal_year_end ({fiscal_year_end}).")

    fiscal_year_label = f"FY{fiscal_year_end[:4]}"
    connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    print("=" * 100)
    print(f"{ticker} {fiscal_year_label} QUARTERLY EXTRACTION ENGINE v2 (annual-anchor-from-production)")
    print("=" * 100)
    for label, info in filings.items():
        print(f"  {label}: {info['form']} report_date={info['report_date']} filing_date={info['filing_date']} accession={info['accession_number']}")
    print()

    results: dict[str, dict] = {}
    csv_rows: list[dict] = []

    for metric_name in METRICS:
        print(f"--- {metric_name} ---")
        metric_result: dict = {"quarters": {}}

        # --- THE ONE CHANGE: annual anchor from authoritative annual production, not re-identified ---
        annual_anchor = resolve_annual_anchor(prod_connection, filings["FY"]["accession_number"], metric_name)
        if not annual_anchor["resolved"]:
            metric_result["status"] = "REVIEW_REQUIRED"
            metric_result["error"] = f"annual anchor: {annual_anchor['reason']}"
            metric_result["annual_anchor_evidence"] = annual_anchor
            print(f"  REVIEW_REQUIRED: {metric_result['error']}")
            results[metric_name] = metric_result
            continue

        annual_decimals = lookup_annual_fact_decimals(
            connection, filings["FY"]["accession_number"], annual_anchor["concept_qname"], annual_anchor["context_id"]
        )
        if annual_decimals is None:
            metric_result["status"] = "REVIEW_REQUIRED"
            metric_result["error"] = ("annual anchor resolved but its XBRL decimals metadata could not be uniquely "
                                       "retrieved from the warehouse for the precision-tolerance calculation")
            metric_result["annual_anchor_evidence"] = annual_anchor
            print(f"  REVIEW_REQUIRED: {metric_result['error']}")
            results[metric_name] = metric_result
            continue

        annual = {
            "value": annual_anchor["value"], "context_id": annual_anchor["context_id"],
            "unit_id": annual_anchor["unit"], "period_start": annual_anchor["period_start"],
            "period_end": annual_anchor["period_end"], "decimals": annual_decimals,
        }
        metric_result["annual_value"] = annual["value"]
        metric_result["annual_lineage"] = {
            "concept_qname": annual_anchor["concept_qname"], "accession_number": filings["FY"]["accession_number"],
            "context_id": annual["context_id"], "unit_id": annual["unit_id"],
            "period_start": annual["period_start"], "period_end": annual["period_end"],
            "filing_date": filings["FY"]["filing_date"], "decimals": annual["decimals"],
            "annual_anchor_source": "AUTHORITATIVE_ANNUAL_PRODUCTION_RESULT",
            "annual_anchor_accession": filings["FY"]["accession_number"],
            "annual_anchor_metric": metric_name,
            "annual_anchor_status": annual_anchor["status"],
            "annual_anchor_extraction_run_id": annual_anchor["extraction_run_id"],
        }

        # --- Q1/Q2/Q3 concept resolution — UNCHANGED logic from 118, now only over the 3 quarterly filings ---
        concept_by_filing: dict[str, str] = {}
        for label in ("Q1", "Q2", "Q3"):
            info = filings[label]
            try:
                concept_by_filing[label] = resolve_concept_for_metric(connection, info["accession_number"], metric_name)
            except s89.TargetRowNotFound as exc:
                metric_result["status"] = "REVIEW_REQUIRED"
                metric_result["error"] = f"{label}: row not found — {exc}"
                print(f"  REVIEW_REQUIRED: {metric_result['error']}")
                results[metric_name] = metric_result
                break
        else:
            # --- Q1: quarter == YTD-through-Q1, same period --- (unchanged from 118)
            q1_info = filings["Q1"]
            q1_facts = facts_for_concept(connection, q1_info["accession_number"], concept_by_filing["Q1"])
            q1_quarter = pick_current_period_fact(q1_facts, q1_info["report_date"], "quarter")
            if q1_quarter is None:
                metric_result["status"] = "REVIEW_REQUIRED"
                metric_result["error"] = "Q1 quarter-duration value did not resolve to a single deterministic fact"
                print(f"  REVIEW_REQUIRED: {metric_result['error']}")
                results[metric_name] = metric_result
                continue

            metric_result["quarters"]["Q1"] = {
                "value": q1_quarter["value"], "extraction_basis": "DIRECT_QUARTER",
                "availability_date": q1_info["filing_date"],
                "lineage": {
                    "concept_qname": concept_by_filing["Q1"], "accession_number": q1_info["accession_number"],
                    "context_id": q1_quarter["context_id"], "unit_id": q1_quarter["unit_id"],
                    "period_start": q1_quarter["period_start"], "period_end": q1_quarter["period_end"],
                    "duration_days": q1_quarter["duration_days"], "decimals": q1_quarter["decimals"],
                },
            }

            # --- Q2 --- (unchanged from 118)
            q2_info = filings["Q2"]
            q2_facts = facts_for_concept(connection, q2_info["accession_number"], concept_by_filing["Q2"])
            q2_quarter_direct = pick_current_period_fact(q2_facts, q2_info["report_date"], "quarter")
            q2_ytd = pick_current_period_fact(q2_facts, q2_info["report_date"], "ytd_6m")

            if q2_quarter_direct is not None:
                q2_value = q2_quarter_direct["value"]
                q2_basis = "DIRECT_QUARTER"
                q2_lineage = {
                    "concept_qname": concept_by_filing["Q2"], "accession_number": q2_info["accession_number"],
                    "context_id": q2_quarter_direct["context_id"], "unit_id": q2_quarter_direct["unit_id"],
                    "period_start": q2_quarter_direct["period_start"], "period_end": q2_quarter_direct["period_end"],
                    "duration_days": q2_quarter_direct["duration_days"], "decimals": q2_quarter_direct["decimals"],
                }
                if q2_ytd is not None:
                    derived_check = q2_ytd["value"] - q1_quarter["value"]
                    q2_lineage["cross_check_derived_from_ytd"] = derived_check
                    q2_lineage["cross_check_matches_direct"] = abs(derived_check - q2_value) < 1
            elif q2_ytd is not None:
                q2_value = q2_ytd["value"] - q1_quarter["value"]
                q2_basis = "DERIVED_FROM_YTD"
                q2_lineage = {
                    "concept_qname": concept_by_filing["Q2"], "accession_number": q2_info["accession_number"],
                    "ytd_6m_value": q2_ytd["value"], "ytd_6m_context_id": q2_ytd["context_id"],
                    "ytd_6m_decimals": q2_ytd["decimals"], "q1_value_subtracted": q1_quarter["value"],
                }
            else:
                metric_result["status"] = "REVIEW_REQUIRED"
                metric_result["error"] = "Q2: neither a direct quarter value nor a 6-month YTD value resolved deterministically"
                print(f"  REVIEW_REQUIRED: {metric_result['error']}")
                results[metric_name] = metric_result
                continue

            metric_result["quarters"]["Q2"] = {"value": q2_value, "extraction_basis": q2_basis,
                                                "availability_date": q2_info["filing_date"], "lineage": q2_lineage}

            # --- Q3 --- (unchanged from 118)
            q3_info = filings["Q3"]
            q3_facts = facts_for_concept(connection, q3_info["accession_number"], concept_by_filing["Q3"])
            q3_quarter_direct = pick_current_period_fact(q3_facts, q3_info["report_date"], "quarter")
            q3_ytd = pick_current_period_fact(q3_facts, q3_info["report_date"], "ytd_9m")

            if q3_quarter_direct is not None:
                q3_value = q3_quarter_direct["value"]
                q3_basis = "DIRECT_QUARTER"
                q3_lineage = {
                    "concept_qname": concept_by_filing["Q3"], "accession_number": q3_info["accession_number"],
                    "context_id": q3_quarter_direct["context_id"], "unit_id": q3_quarter_direct["unit_id"],
                    "period_start": q3_quarter_direct["period_start"], "period_end": q3_quarter_direct["period_end"],
                    "duration_days": q3_quarter_direct["duration_days"], "decimals": q3_quarter_direct["decimals"],
                }
                if q3_ytd is not None and q2_ytd is not None:
                    derived_check = q3_ytd["value"] - q2_ytd["value"]
                    q3_lineage["cross_check_derived_from_ytd"] = derived_check
                    q3_lineage["cross_check_matches_direct"] = abs(derived_check - q3_value) < 1
            elif q3_ytd is not None and q2_ytd is not None:
                q3_value = q3_ytd["value"] - q2_ytd["value"]
                q3_basis = "DERIVED_FROM_YTD"
                q3_lineage = {
                    "concept_qname": concept_by_filing["Q3"], "accession_number": q3_info["accession_number"],
                    "ytd_9m_value": q3_ytd["value"], "ytd_9m_context_id": q3_ytd["context_id"],
                    "ytd_9m_decimals": q3_ytd["decimals"], "q2_ytd_6m_value_subtracted": q2_ytd["value"],
                }
            else:
                metric_result["status"] = "REVIEW_REQUIRED"
                metric_result["error"] = "Q3: neither a direct quarter value nor a 9-month YTD value resolved deterministically"
                print(f"  REVIEW_REQUIRED: {metric_result['error']}")
                results[metric_name] = metric_result
                continue

            metric_result["quarters"]["Q3"] = {"value": q3_value, "extraction_basis": q3_basis,
                                                "availability_date": q3_info["filing_date"], "lineage": q3_lineage}

            # --- Q4 --- (unchanged equation from 118; lineage gains the annual_anchor_* fields)
            if q3_ytd is None:
                metric_result["status"] = "REVIEW_REQUIRED"
                metric_result["error"] = "Q4 cannot be derived — Q3's own 9-month YTD value did not resolve"
                print(f"  REVIEW_REQUIRED: {metric_result['error']}")
                results[metric_name] = metric_result
                continue

            q4_value = annual["value"] - q3_ytd["value"]
            metric_result["quarters"]["Q4"] = {
                "value": q4_value, "extraction_basis": "DERIVED_Q4_FROM_10K_MINUS_9M",
                "availability_date": filings["FY"]["filing_date"],
                "lineage": {
                    "annual_value": annual["value"], "annual_concept_qname": annual_anchor["concept_qname"],
                    "annual_accession_number": filings["FY"]["accession_number"], "annual_decimals": annual["decimals"],
                    "nine_month_ytd_value": q3_ytd["value"], "nine_month_ytd_context_id": q3_ytd["context_id"],
                    "nine_month_ytd_accession_number": q3_info["accession_number"], "nine_month_ytd_decimals": q3_ytd["decimals"],
                    "annual_anchor_source": "AUTHORITATIVE_ANNUAL_PRODUCTION_RESULT",
                    "annual_anchor_accession": filings["FY"]["accession_number"],
                    "annual_anchor_metric": metric_name,
                    "annual_anchor_status": annual_anchor["status"],
                    "annual_anchor_extraction_run_id": annual_anchor["extraction_run_id"],
                },
            }

            # --- reconciliation: precision-aware (D-035), unchanged from 118 ---
            q2_source_decimals = q2_lineage["decimals"] if q2_basis == "DIRECT_QUARTER" else q2_lineage["ytd_6m_decimals"]
            q3_source_decimals = q3_lineage["decimals"] if q3_basis == "DIRECT_QUARTER" else q3_lineage["ytd_9m_decimals"]

            reconciliation = compute_precision_aware_reconciliation(
                q1_value=q1_quarter["value"], q1_decimals=q1_quarter["decimals"],
                q2_value=q2_value, q2_basis=q2_basis, q2_source_decimals=q2_source_decimals,
                q3_value=q3_value, q3_basis=q3_basis, q3_source_decimals=q3_source_decimals,
                q4_value=q4_value, annual_value=annual["value"], annual_decimals=annual["decimals"],
            )
            metric_result["reconciliation"] = reconciliation
            metric_result["status"] = reconciliation["status"]

            print(f"  Q1={metric_result['quarters']['Q1']['value']:>18,.0f} ({metric_result['quarters']['Q1']['extraction_basis']})")
            print(f"  Q2={metric_result['quarters']['Q2']['value']:>18,.0f} ({metric_result['quarters']['Q2']['extraction_basis']})")
            print(f"  Q3={metric_result['quarters']['Q3']['value']:>18,.0f} ({metric_result['quarters']['Q3']['extraction_basis']})")
            print(f"  Q4={metric_result['quarters']['Q4']['value']:>18,.0f} ({metric_result['quarters']['Q4']['extraction_basis']})")
            print(f"  Sum={reconciliation['sum_q1_to_q4']:>17,.0f}  Annual={reconciliation['annual_value']:>17,.0f}  "
                  f"diff={reconciliation['difference']:.2f}  permitted={reconciliation['precision_calculation']['permitted_difference']:.2f}  "
                  f"{reconciliation['status']}")

            results[metric_name] = metric_result

            for quarter_label in ("Q1", "Q2", "Q3", "Q4"):
                q = metric_result["quarters"][quarter_label]
                csv_rows.append({
                    "ticker": ticker, "fiscal_year": fiscal_year_label, "fiscal_quarter": quarter_label,
                    "metric_name": metric_name, "value": q["value"], "unit": "iso4217:USD",
                    "period_start": q["lineage"].get("period_start"),
                    "period_end": filings[quarter_label]["report_date"] if quarter_label != "Q4" else filings["FY"]["report_date"],
                    "filing_date": q["availability_date"],
                    "accession_number": q["lineage"].get("accession_number", q["lineage"].get("annual_accession_number")),
                    "concept_qname": q["lineage"].get("concept_qname", q["lineage"].get("annual_concept_qname")),
                    "context_id": q["lineage"].get("context_id", q["lineage"].get("nine_month_ytd_context_id")),
                    "extraction_basis": q["extraction_basis"], "reconciliation_status": metric_result["status"],
                })

        print()

    connection.close()
    prod_connection.close()

    output = {
        "company": ticker, "fiscal_year": fiscal_year_label, "fiscal_year_end": fiscal_year_end,
        "filings": filings, "metrics": results, "reconciliation_policy": "XBRL_DECIMALS_ROUNDING_TOLERANCE",
        "annual_anchor_policy": "AUTHORITATIVE_ANNUAL_PRODUCTION_RESULT", "engine_script": "128_quarterly_extraction_engine_v2.py",
    }
    json_output_path.parent.mkdir(parents=True, exist_ok=True)
    with json_output_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, ensure_ascii=False, default=str)
    print(f"JSON written to {json_output_path}")

    if csv_rows:
        pd.DataFrame(csv_rows).to_csv(csv_output_path, index=False, encoding="utf-8-sig")
        print(f"CSV written to {csv_output_path} ({len(csv_rows)} rows)")

    print("\n" + "=" * 100)
    print("SUMMARY")
    for metric_name, result in results.items():
        print(f"  {metric_name:24s} status={result.get('status')}")
    all_resolved = all(r.get("status") in ("PASS", "PASS_ROUNDING_TOLERANCE") for r in results.values())
    print(f"\nALL 6 METRICS RESOLVED (PASS or PASS_ROUNDING_TOLERANCE): {all_resolved}")
    print("=" * 100)

    return output


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quarterly extraction engine v2 (annual anchor from production).")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--fiscal-year-end", required=True)
    parser.add_argument("--q1-accession", required=True)
    parser.add_argument("--q2-accession", required=True)
    parser.add_argument("--q3-accession", required=True)
    parser.add_argument("--fy-accession", required=True)
    parser.add_argument("--json-output", required=True)
    parser.add_argument("--csv-output", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    run_quarterly_extraction_engine_v2(
        ticker=arguments.ticker, fiscal_year_end=arguments.fiscal_year_end,
        q1_accession=arguments.q1_accession, q2_accession=arguments.q2_accession,
        q3_accession=arguments.q3_accession, fy_accession=arguments.fy_accession,
        json_output_path=arguments.json_output, csv_output_path=arguments.csv_output,
    )
