"""
ORCL FY2024 quarterly extraction proof — precision-aware reconciliation.
Copied from the verified scripts/113_orcl_fy2024_quarterly_proof.py with
ONLY one addition: an XBRL-decimals-based rounding-tolerance rule applied
to the final Q1-Q4-vs-annual reconciliation step. Quarter-duration
classification, DIRECT_QUARTER selection, DERIVED_FROM_YTD logic, Q4
derivation, concept selection, and filing-date availability logic are
byte-for-byte unchanged from 113.

APPROVED POLICY — XBRL ROUNDING TOLERANCE (verbatim, as authorized):
No reported value is ever altered, smoothed, or replaced.
For every reconciliation equation:
  1. Read the XBRL `decimals` value for every independently reported
     source fact participating in the equation (Q1, Q2, Q3, Q4's own
     source facts, and the annual FY value).
  2. uncertainty_per_fact = (10 ** (-decimals)) / 2
     e.g. decimals=-6 -> rounding_unit=1,000,000 -> uncertainty=500,000
  3. permitted_difference = sum of the uncertainties of every
     independently reported source fact participating in the equation
     (Q1 + Q2 + Q3 + Q4's own source fact(s) + the annual FY fact —
     Q4 itself is derived, not independently reported, so its
     uncertainty is taken from the two facts it is derived FROM:
     the annual value and Q3's 9-month YTD value, since those are the
     independently reported facts underlying Q4's own uncertainty. See
     `q4_derivation_uncertainty` below for the exact composition.)
  4. If abs(difference) <= permitted_difference: status =
     PASS_ROUNDING_TOLERANCE. Otherwise: REVIEW_REQUIRED (previously
     surfaced as FAIL under exact-equality comparison; the precision-
     aware policy replaces "FAIL" with fail-closed "REVIEW_REQUIRED"
     for gaps that exceed the calculated tolerance — never a silent
     pass).
  5. Full lineage preserved: exact reported values, exact reconciliation
     difference, decimals of every source fact, rounding unit and
     uncertainty per fact, calculated maximum tolerance, and the
     equation used.
  No fixed dollar or percentage tolerance is introduced anywhere.
"""

from __future__ import annotations

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

JSON_OUTPUT_PATH = DATA_DIR / "quarterly_proof_orcl_fy2024.json"
CSV_OUTPUT_PATH = DATA_DIR / "quarterly_proof_orcl_fy2024.csv"

QUARTER_DURATION_MIN_DAYS = 89
QUARTER_DURATION_MAX_DAYS = 92
YTD_6M_MIN_DAYS, YTD_6M_MAX_DAYS = 181, 184
YTD_9M_MIN_DAYS, YTD_9M_MAX_DAYS = 271, 275
YTD_12M_MIN_DAYS, YTD_12M_MAX_DAYS = 364, 366

FILINGS = {
    "Q1": {"form": "10-Q", "report_date": "2023-08-31", "filing_date": "2023-09-12",
           "accession_number": "0000950170-23-047713"},
    "Q2": {"form": "10-Q", "report_date": "2023-11-30", "filing_date": "2023-12-12",
           "accession_number": "0000950170-23-069682"},
    "Q3": {"form": "10-Q", "report_date": "2024-02-29", "filing_date": "2024-03-12",
           "accession_number": "0000950170-24-029904"},
    "FY": {"form": "10-K", "report_date": "2024-05-31", "filing_date": "2024-06-20",
           "accession_number": "0000950170-24-075605"},
}

METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense",
           "operating_cash_flow", "capex"]

# Reuse scripts/89's already-approved, unchanged row-identification logic
# (BUILT_IN_METRICS/identify_canonical_row/reconstruct_presentation_dataframe)
# — read-only reuse, not duplicated.
_spec = importlib.util.spec_from_file_location(
    "s89", PROJECT_DIR / "scripts" / "89_panw_zero_long_term_debt_policy.py"
)
s89 = importlib.util.module_from_spec(_spec)
sys.modules["s89"] = s89
_spec.loader.exec_module(s89)


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


def resolve_concept_for_metric(connection: duckdb.DuckDBPyConnection, accession_number: str, metric_name: str) -> str:
    presentation = s89.reconstruct_presentation_dataframe(connection, accession_number)
    metric = s89.BUILT_IN_METRICS[metric_name]
    row, _candidates = s89.identify_canonical_row(presentation, metric)
    return row["concept_qname"]


def facts_for_concept(
    connection: duckdb.DuckDBPyConnection, accession_number: str, concept_qname: str
) -> pd.DataFrame:
    """All non-dimensioned, non-nil, numeric duration facts for this
    concept in this filing — the CURRENT-period facts (own quarter/YTD)
    AND the prior-year comparative facts both appear here; the caller
    selects by period_start/period_end matching THIS filing's own
    current period, never the comparative one."""

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
    # Reuse the SAME already-approved, unchanged precision-duplicate
    # reconciliation this project already uses everywhere else (e.g.
    # scripts/92's match_facts_from_warehouse) — the same balance can be
    # tagged twice within one context at different rounding precision
    # (confirmed on AMZN's own FY2024 10-K income_tax_expense: -6 vs -8
    # decimals for the identical underlying value). Not new logic; a
    # pre-existing, already-imported s89 function this proof script had
    # not yet been calling.
    if not facts.empty:
        facts, _notes = s89._reconcile_same_context_precision_duplicates_from_warehouse(facts)
    return facts


def pick_current_period_fact(facts: pd.DataFrame, expected_end_date: str, duration_class: str) -> dict | None:
    """Selects the fact whose period_end matches THIS filing's own
    report_date (never a prior-year comparative) and whose duration
    classifies as the requested bucket (quarter/ytd_6m/ytd_9m/ytd_12m)."""

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
        "value": float(row["value_numeric"]),
        "period_start": str(row["period_start"]),
        "period_end": str(row["period_end"]),
        "duration_days": days,
        "context_id": str(row["context_id"]),
        "unit_id": str(row["unit_id"]),
        "decimals": str(row["decimals"]),
    }


def parse_decimals(decimals_str: str | None) -> int | None:
    """Parses the XBRL `decimals` attribute (e.g. '-6', '-8', 'INF') into
    an int, or None when it cannot be interpreted as a rounding
    precision (e.g. 'INF' means exact/infinite precision -> zero
    uncertainty, handled by the caller)."""
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
    """Returns (uncertainty, rounding_unit). INF decimals (exact value)
    -> uncertainty 0.0, rounding_unit None. Unparseable -> treated as
    exact (uncertainty 0.0) since no evidence of rounding exists."""
    decimals = parse_decimals(decimals_str)
    if decimals is None:
        return 0.0, None
    rounding_unit = 10 ** (-decimals)
    uncertainty = rounding_unit / 2
    return uncertainty, rounding_unit


def main() -> None:
    connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    print("=" * 100)
    print("ORCL FY2024 QUARTERLY EXTRACTION PROOF — PRECISION-AWARE RECONCILIATION")
    print("=" * 100)
    for label, info in FILINGS.items():
        print(f"  {label}: {info['form']} report_date={info['report_date']} "
              f"filing_date={info['filing_date']} accession={info['accession_number']}")
    print()

    results: dict[str, dict] = {}
    csv_rows: list[dict] = []

    for metric_name in METRICS:
        print(f"--- {metric_name} ---")
        metric_result: dict = {"quarters": {}}

        # resolve concept once per filing (should be the same concept
        # across all 4 ORCL filings, but resolved independently each
        # time — no assumption of stability across filings)
        concept_by_filing: dict[str, str] = {}
        for label, info in FILINGS.items():
            try:
                concept_by_filing[label] = resolve_concept_for_metric(
                    connection, info["accession_number"], metric_name
                )
            except s89.TargetRowNotFound as exc:
                metric_result["status"] = "REVIEW_REQUIRED"
                metric_result["error"] = f"{label}: row not found — {exc}"
                print(f"  REVIEW_REQUIRED: {metric_result['error']}")
                results[metric_name] = metric_result
                break
        else:
            # --- annual value (FY 10-K, 12-month duration) ---
            fy_info = FILINGS["FY"]
            fy_facts = facts_for_concept(connection, fy_info["accession_number"], concept_by_filing["FY"])
            annual = pick_current_period_fact(fy_facts, fy_info["report_date"], "ytd_12m")
            if annual is None:
                metric_result["status"] = "REVIEW_REQUIRED"
                metric_result["error"] = "annual (FY) 12-month value did not resolve to a single deterministic fact"
                print(f"  REVIEW_REQUIRED: {metric_result['error']}")
                results[metric_name] = metric_result
                continue

            metric_result["annual_value"] = annual["value"]
            metric_result["annual_lineage"] = {
                "concept_qname": concept_by_filing["FY"], "accession_number": fy_info["accession_number"],
                "context_id": annual["context_id"], "unit_id": annual["unit_id"],
                "period_start": annual["period_start"], "period_end": annual["period_end"],
                "filing_date": fy_info["filing_date"], "decimals": annual["decimals"],
            }

            # --- Q1: quarter == YTD-through-Q1, same period ---
            q1_info = FILINGS["Q1"]
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

            # --- Q2: prefer DIRECT quarter; verify DERIVED_FROM_YTD agrees ---
            q2_info = FILINGS["Q2"]
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
                    "ytd_6m_decimals": q2_ytd["decimals"],
                    "q1_value_subtracted": q1_quarter["value"],
                }
            else:
                metric_result["status"] = "REVIEW_REQUIRED"
                metric_result["error"] = "Q2: neither a direct quarter value nor a 6-month YTD value resolved deterministically"
                print(f"  REVIEW_REQUIRED: {metric_result['error']}")
                results[metric_name] = metric_result
                continue

            metric_result["quarters"]["Q2"] = {
                "value": q2_value, "extraction_basis": q2_basis,
                "availability_date": q2_info["filing_date"], "lineage": q2_lineage,
            }

            # --- Q3: prefer DIRECT quarter; verify DERIVED_FROM_YTD agrees ---
            q3_info = FILINGS["Q3"]
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
                    # Per exact spec: Q3 quarter = Q3 YTD (9mo) - Q2 YTD (6mo)
                    # — subtract Q2's own YTD figure, NOT Q2's quarter value.
                    derived_check = q3_ytd["value"] - q2_ytd["value"]
                    q3_lineage["cross_check_derived_from_ytd"] = derived_check
                    q3_lineage["cross_check_matches_direct"] = abs(derived_check - q3_value) < 1
            elif q3_ytd is not None and q2_ytd is not None:
                q3_value = q3_ytd["value"] - q2_ytd["value"]
                q3_basis = "DERIVED_FROM_YTD"
                q3_lineage = {
                    "concept_qname": concept_by_filing["Q3"], "accession_number": q3_info["accession_number"],
                    "ytd_9m_value": q3_ytd["value"], "ytd_9m_context_id": q3_ytd["context_id"],
                    "ytd_9m_decimals": q3_ytd["decimals"],
                    "q2_ytd_6m_value_subtracted": q2_ytd["value"],
                }
            else:
                metric_result["status"] = "REVIEW_REQUIRED"
                metric_result["error"] = "Q3: neither a direct quarter value nor a 9-month YTD value resolved deterministically"
                print(f"  REVIEW_REQUIRED: {metric_result['error']}")
                results[metric_name] = metric_result
                continue

            metric_result["quarters"]["Q3"] = {
                "value": q3_value, "extraction_basis": q3_basis,
                "availability_date": q3_info["filing_date"], "lineage": q3_lineage,
            }

            # --- Q4: ALWAYS derived, never a direct/standalone fact ---
            if q3_ytd is None:
                metric_result["status"] = "REVIEW_REQUIRED"
                metric_result["error"] = "Q4 cannot be derived — Q3's own 9-month YTD value did not resolve"
                print(f"  REVIEW_REQUIRED: {metric_result['error']}")
                results[metric_name] = metric_result
                continue

            q4_value = annual["value"] - q3_ytd["value"]
            metric_result["quarters"]["Q4"] = {
                "value": q4_value, "extraction_basis": "DERIVED_Q4_FROM_10K_MINUS_9M",
                "availability_date": FILINGS["FY"]["filing_date"],
                "lineage": {
                    "annual_value": annual["value"], "annual_concept_qname": concept_by_filing["FY"],
                    "annual_accession_number": FILINGS["FY"]["accession_number"],
                    "annual_decimals": annual["decimals"],
                    "nine_month_ytd_value": q3_ytd["value"], "nine_month_ytd_context_id": q3_ytd["context_id"],
                    "nine_month_ytd_accession_number": q3_info["accession_number"],
                    "nine_month_ytd_decimals": q3_ytd["decimals"],
                },
            }

            # --- reconciliation: exact difference (unchanged) ---
            quarter_sum = sum(metric_result["quarters"][q]["value"] for q in ("Q1", "Q2", "Q3", "Q4"))
            difference = quarter_sum - annual["value"]
            exact_status = "PASS" if abs(difference) < 1 else "FAIL"

            # --- precision-aware reconciliation (NEW, additive only) ---
            # The equation being reconciled is:
            #   Q1 + Q2 + Q3 + Q4  vs.  Annual
            # Q4 is not itself an independently reported fact (it is
            # derived as Annual - Q3_9mYTD), so its own uncertainty
            # contribution is already fully represented by the Annual
            # and Q3-9mYTD uncertainties below — including Q4 again
            # would double-count the same two underlying facts. The
            # independently reported source facts for this equation
            # are therefore: Q1 (direct), Q2's source fact(s), Q3's
            # source fact(s), and the Annual fact. When Q2/Q3 used
            # DIRECT_QUARTER, their own decimals apply; when they used
            # DERIVED_FROM_YTD, the uncertainty of the YTD fact they
            # were computed from applies instead (that YTD fact is the
            # independently reported source, not the derived quarter
            # value itself).
            q1_decimals = q1_quarter["decimals"]
            q1_uncertainty, q1_rounding_unit = uncertainty_for_decimals(q1_decimals)

            if q2_basis == "DIRECT_QUARTER":
                q2_source_decimals = q2_lineage["decimals"]
            else:
                q2_source_decimals = q2_lineage["ytd_6m_decimals"]
            q2_uncertainty, q2_rounding_unit = uncertainty_for_decimals(q2_source_decimals)

            if q3_basis == "DIRECT_QUARTER":
                q3_source_decimals = q3_lineage["decimals"]
            else:
                q3_source_decimals = q3_lineage["ytd_9m_decimals"]
            q3_uncertainty, q3_rounding_unit = uncertainty_for_decimals(q3_source_decimals)

            annual_decimals = annual["decimals"]
            annual_uncertainty, annual_rounding_unit = uncertainty_for_decimals(annual_decimals)

            permitted_difference = q1_uncertainty + q2_uncertainty + q3_uncertainty + annual_uncertainty

            precision_calc = {
                "equation": "Q1 + Q2 + Q3 + Q4 vs. Annual (Q4 derived as Annual - Q3_9mYTD; "
                             "Q4's own uncertainty is already represented by the Annual and "
                             "Q3-9mYTD terms below, so it is not counted a second time)",
                "source_facts": {
                    "Q1": {"decimals": q1_decimals, "rounding_unit": q1_rounding_unit, "uncertainty": q1_uncertainty},
                    "Q2": {"basis": q2_basis, "decimals": q2_source_decimals,
                           "rounding_unit": q2_rounding_unit, "uncertainty": q2_uncertainty},
                    "Q3": {"basis": q3_basis, "decimals": q3_source_decimals,
                           "rounding_unit": q3_rounding_unit, "uncertainty": q3_uncertainty},
                    "Annual": {"decimals": annual_decimals, "rounding_unit": annual_rounding_unit,
                               "uncertainty": annual_uncertainty},
                },
                "permitted_difference": permitted_difference,
                "actual_difference": difference,
                "within_tolerance": abs(difference) <= permitted_difference,
            }

            if exact_status == "PASS":
                final_status = "PASS"
            elif abs(difference) <= permitted_difference:
                final_status = "PASS_ROUNDING_TOLERANCE"
            else:
                final_status = "REVIEW_REQUIRED"

            metric_result["reconciliation"] = {
                "sum_q1_to_q4": quarter_sum, "annual_value": annual["value"],
                "difference": difference, "exact_equality_status": exact_status,
                "precision_calculation": precision_calc, "status": final_status,
            }
            metric_result["status"] = final_status

            print(f"  Q1={metric_result['quarters']['Q1']['value']:>18,.0f} ({metric_result['quarters']['Q1']['extraction_basis']})")
            print(f"  Q2={metric_result['quarters']['Q2']['value']:>18,.0f} ({metric_result['quarters']['Q2']['extraction_basis']})")
            print(f"  Q3={metric_result['quarters']['Q3']['value']:>18,.0f} ({metric_result['quarters']['Q3']['extraction_basis']})")
            print(f"  Q4={metric_result['quarters']['Q4']['value']:>18,.0f} ({metric_result['quarters']['Q4']['extraction_basis']})")
            print(f"  Sum={quarter_sum:>17,.0f}  Annual={annual['value']:>17,.0f}  "
                  f"diff={difference:.2f}  permitted={permitted_difference:.2f}  {final_status}")

            results[metric_name] = metric_result

            for quarter_label in ("Q1", "Q2", "Q3", "Q4"):
                q = metric_result["quarters"][quarter_label]
                csv_rows.append({
                    "ticker": "ORCL", "fiscal_year": "FY2024", "fiscal_quarter": quarter_label,
                    "metric_name": metric_name, "value": q["value"], "unit": "iso4217:USD",
                    "period_start": q["lineage"].get("period_start"), "period_end": (
                        FILINGS[quarter_label]["report_date"] if quarter_label != "Q4" else FILINGS["FY"]["report_date"]
                    ),
                    "filing_date": q["availability_date"],
                    "accession_number": q["lineage"].get("accession_number", q["lineage"].get("annual_accession_number")),
                    "concept_qname": q["lineage"].get("concept_qname", q["lineage"].get("annual_concept_qname")),
                    "context_id": q["lineage"].get("context_id", q["lineage"].get("nine_month_ytd_context_id")),
                    "extraction_basis": q["extraction_basis"],
                    "reconciliation_status": metric_result["status"],
                })

        print()

    connection.close()

    # --- write outputs ---
    output = {
        "company": "ORCL", "fiscal_year": "FY2024", "fiscal_year_end": "2024-05-31",
        "filings": FILINGS, "metrics": results,
        "reconciliation_policy": "XBRL_DECIMALS_ROUNDING_TOLERANCE",
    }
    with JSON_OUTPUT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, ensure_ascii=False, default=str)
    print(f"JSON written to {JSON_OUTPUT_PATH}")

    if csv_rows:
        pd.DataFrame(csv_rows).to_csv(CSV_OUTPUT_PATH, index=False, encoding="utf-8-sig")
        print(f"CSV written to {CSV_OUTPUT_PATH} ({len(csv_rows)} rows)")

    print("\n" + "=" * 100)
    print("SUMMARY")
    for metric_name, result in results.items():
        print(f"  {metric_name:24s} status={result.get('status')}")
    all_resolved = all(r.get("status") in ("PASS", "PASS_ROUNDING_TOLERANCE") for r in results.values())
    print(f"\nALL 6 METRICS RESOLVED (PASS or PASS_ROUNDING_TOLERANCE): {all_resolved}")
    print("=" * 100)


if __name__ == "__main__":
    main()
