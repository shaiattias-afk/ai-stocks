"""
MSFT FY2024 quarterly extraction proof — bounded, single-company proof
of concept for deriving quarterly (Q1-Q4) values from 10-Q/10-K
filings, to decide whether the same method can be generalized to a
full quarterly system. This is explicitly NOT the production quarterly
schema — a proof only, per instruction.

Filings used (all already locked + warehoused, point-in-time correct):
  Q1 10-Q  report_date=2023-09-30  filed 2023-10-24
  Q2 10-Q  report_date=2023-12-31  filed 2024-01-30
  Q3 10-Q  report_date=2024-03-31  filed 2024-04-25
  FY 10-K  report_date=2024-06-30  filed 2024-07-30

Metrics: revenue, operating_income, pretax_income, income_tax_expense
(tax_expense), operating_cash_flow, capex.

Quarter logic (exactly as specified):
  1. Distinguish quarter-only duration facts (~90-92 days) from
     year-to-date duration facts (~181-184, ~271-275, ~364-366 days)
     by computing (period_end - period_start).days from the warehouse's
     own stored context dates — never assumed from a label.
  2. Never treat a YTD value as a standalone quarter.
  3. Prefer a reliable DIRECT quarter-duration fact when present
     (status confirmed for all 6 metrics in this proof — MSFT tags
     discrete 3-month durations for every one of them, including the
     cash-flow-statement items, which many filers do NOT do).
  4. Fallback (not needed for MSFT, but implemented and verified below
     to also produce the identical result as a cross-check):
       Q2 quarter = Q2 YTD (6mo) - Q1 YTD (3mo, i.e. Q1's own value)
       Q3 quarter = Q3 YTD (9mo) - Q2 YTD (6mo)
  5. Q4 = FY2024 10-K annual value - Q3 9-month YTD value.
     extraction_basis = DERIVED_Q4_FROM_10K_MINUS_9M for every Q4 result.
  6. Q4's point-in-time availability date = the FY2024 10-K's own
     filing_date (2024-07-30) — never an earlier date.
  7. No later-year comparative facts and no amended filings were read
     (only each accession's own current-period facts).

This script is READ-ONLY against the warehouse and does NOT write to
the production database (data/database/ai_stock_agent.duckdb) or the
Annual V1 snapshot — outputs go only to
data/quarterly_proof_msft_fy2024.json and .csv.
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

JSON_OUTPUT_PATH = DATA_DIR / "quarterly_proof_msft_fy2024.json"
CSV_OUTPUT_PATH = DATA_DIR / "quarterly_proof_msft_fy2024.csv"

QUARTER_DURATION_MIN_DAYS = 89
QUARTER_DURATION_MAX_DAYS = 92
YTD_6M_MIN_DAYS, YTD_6M_MAX_DAYS = 181, 184
YTD_9M_MIN_DAYS, YTD_9M_MAX_DAYS = 271, 275
YTD_12M_MIN_DAYS, YTD_12M_MAX_DAYS = 364, 366

FILINGS = {
    "Q1": {"form": "10-Q", "report_date": "2023-09-30", "filing_date": "2023-10-24",
           "accession_number": "0000950170-23-054855"},
    "Q2": {"form": "10-Q", "report_date": "2023-12-31", "filing_date": "2024-01-30",
           "accession_number": "0000950170-24-008814"},
    "Q3": {"form": "10-Q", "report_date": "2024-03-31", "filing_date": "2024-04-25",
           "accession_number": "0000950170-24-048288"},
    "FY": {"form": "10-K", "report_date": "2024-06-30", "filing_date": "2024-07-30",
           "accession_number": "0000950170-24-087843"},
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


def main() -> None:
    connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    print("=" * 100)
    print("MSFT FY2024 QUARTERLY EXTRACTION PROOF")
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
        # across all 4 MSFT filings, but resolved independently each
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
                "filing_date": fy_info["filing_date"],
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
                    "duration_days": q1_quarter["duration_days"],
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
                    "duration_days": q2_quarter_direct["duration_days"],
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
                    "duration_days": q3_quarter_direct["duration_days"],
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
                    "nine_month_ytd_value": q3_ytd["value"], "nine_month_ytd_context_id": q3_ytd["context_id"],
                    "nine_month_ytd_accession_number": q3_info["accession_number"],
                },
            }

            # --- reconciliation ---
            quarter_sum = sum(metric_result["quarters"][q]["value"] for q in ("Q1", "Q2", "Q3", "Q4"))
            difference = quarter_sum - annual["value"]
            metric_result["reconciliation"] = {
                "sum_q1_to_q4": quarter_sum, "annual_value": annual["value"],
                "difference": difference, "status": "PASS" if abs(difference) < 1 else "FAIL",
            }
            metric_result["status"] = "PASS" if abs(difference) < 1 else "FAIL"

            print(f"  Q1={metric_result['quarters']['Q1']['value']:>18,.0f} ({metric_result['quarters']['Q1']['extraction_basis']})")
            print(f"  Q2={metric_result['quarters']['Q2']['value']:>18,.0f} ({metric_result['quarters']['Q2']['extraction_basis']})")
            print(f"  Q3={metric_result['quarters']['Q3']['value']:>18,.0f} ({metric_result['quarters']['Q3']['extraction_basis']})")
            print(f"  Q4={metric_result['quarters']['Q4']['value']:>18,.0f} ({metric_result['quarters']['Q4']['extraction_basis']})")
            print(f"  Sum={quarter_sum:>17,.0f}  Annual={annual['value']:>17,.0f}  "
                  f"diff={difference:.2f}  {metric_result['reconciliation']['status']}")

            results[metric_name] = metric_result

            for quarter_label in ("Q1", "Q2", "Q3", "Q4"):
                q = metric_result["quarters"][quarter_label]
                csv_rows.append({
                    "ticker": "MSFT", "fiscal_year": "FY2024", "fiscal_quarter": quarter_label,
                    "metric_name": metric_name, "value": q["value"], "unit": "iso4217:USD",
                    "period_start": q["lineage"].get("period_start"), "period_end": (
                        FILINGS[quarter_label]["report_date"] if quarter_label != "Q4" else FILINGS["FY"]["report_date"]
                    ),
                    "filing_date": q["availability_date"],
                    "accession_number": q["lineage"].get("accession_number", q["lineage"].get("annual_accession_number")),
                    "concept_qname": q["lineage"].get("concept_qname", q["lineage"].get("annual_concept_qname")),
                    "context_id": q["lineage"].get("context_id", q["lineage"].get("nine_month_ytd_context_id")),
                    "extraction_basis": q["extraction_basis"],
                })

        print()

    connection.close()

    # --- write outputs ---
    output = {
        "company": "MSFT", "fiscal_year": "FY2024", "fiscal_year_end": "2024-06-30",
        "filings": FILINGS, "metrics": results,
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
    all_pass = all(r.get("status") == "PASS" for r in results.values())
    print(f"\nALL 6 METRICS PASS: {all_pass}")
    print("=" * 100)


if __name__ == "__main__":
    main()
