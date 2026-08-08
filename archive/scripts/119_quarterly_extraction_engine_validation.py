"""
Validation harness for the consolidated quarterly extraction engine
(scripts/118_quarterly_extraction_engine.py). Not a company-specific
script — a generic test harness that happens to invoke the generic
engine three times (MSFT/AMZN/ORCL), using accessions already
warehoused by the prior verified proofs. Read-only against both
databases; the engine itself never writes to either.

Two independent checks, per the task's explicit instructions:

1. REPRODUCTION CHECK — run the engine for MSFT FY2024, AMZN FY2024, and
   ORCL FY2024 (24 results each, 72 total) and compare every quarterly
   value, extraction_basis, and reconciliation status against the
   existing verified JSON outputs (scripts/109/111/117's own
   data/quarterly_proof_*.json). The engine's own outputs are written to
   NEW file paths (data/quarterly_engine_*_fy2024.json/.csv) rather than
   overwriting the original verified files, preserving them as
   historical baselines per project convention.

2. FAIL-CLOSED SYNTHETIC TEST — calls
   compute_precision_aware_reconciliation() directly with hand-built,
   synthetic numbers (no database access, no company, nothing written to
   either database) chosen so the reconciliation difference exceeds the
   calculated XBRL-decimals tolerance, and confirms the engine returns
   REVIEW_REQUIRED rather than any PASS variant.

Writes a combined result to
data/quarterly_extraction_engine_validation_result.json.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

_spec = importlib.util.spec_from_file_location(
    "s118", PROJECT_DIR / "scripts" / "118_quarterly_extraction_engine.py"
)
s118 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s118)

VALIDATION_RESULT_PATH = DATA_DIR / "quarterly_extraction_engine_validation_result.json"

COMPANIES = {
    "MSFT": {
        "fiscal_year_end": "2024-06-30",
        "q1_accession": "0000950170-23-054855",
        "q2_accession": "0000950170-24-008814",
        "q3_accession": "0000950170-24-048288",
        "fy_accession": "0000950170-24-087843",
        "original_json": DATA_DIR / "quarterly_proof_msft_fy2024.json",
        "engine_json": DATA_DIR / "quarterly_engine_msft_fy2024.json",
        "engine_csv": DATA_DIR / "quarterly_engine_msft_fy2024.csv",
    },
    "AMZN": {
        "fiscal_year_end": "2024-12-31",
        "q1_accession": "0001018724-24-000083",
        "q2_accession": "0001018724-24-000130",
        "q3_accession": "0001018724-24-000161",
        "fy_accession": "0001018724-25-000004",
        "original_json": DATA_DIR / "quarterly_proof_amzn_fy2024.json",
        "engine_json": DATA_DIR / "quarterly_engine_amzn_fy2024.json",
        "engine_csv": DATA_DIR / "quarterly_engine_amzn_fy2024.csv",
    },
    "ORCL": {
        "fiscal_year_end": "2024-05-31",
        "q1_accession": "0000950170-23-047713",
        "q2_accession": "0000950170-23-069682",
        "q3_accession": "0000950170-24-029904",
        "fy_accession": "0000950170-24-075605",
        "original_json": DATA_DIR / "quarterly_proof_orcl_fy2024.json",
        "engine_json": DATA_DIR / "quarterly_engine_orcl_fy2024.json",
        "engine_csv": DATA_DIR / "quarterly_engine_orcl_fy2024.csv",
    },
}

METRICS = ["revenue", "operating_income", "pretax_income", "income_tax_expense",
           "operating_cash_flow", "capex"]
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]


def values_close(a: float, b: float, tolerance: float = 1.0) -> bool:
    return math.isclose(a, b, abs_tol=tolerance)


def compare_company(ticker: str, engine_output: dict, original_json_path: Path) -> list[dict]:
    """Compares the engine's freshly computed output against the
    already-verified JSON produced by the original per-company proof
    script. Returns a list of difference records (empty = identical)."""

    differences: list[dict] = []
    with original_json_path.open(encoding="utf-8") as handle:
        original = json.load(handle)

    for metric_name in METRICS:
        engine_metric = engine_output["metrics"].get(metric_name, {})
        original_metric = original["metrics"].get(metric_name, {})

        engine_status = engine_metric.get("status")
        original_status = original_metric.get("status")
        if engine_status != original_status:
            differences.append({
                "ticker": ticker, "metric": metric_name, "field": "status",
                "engine_value": engine_status, "original_value": original_status,
            })

        for quarter in QUARTERS:
            engine_q = engine_metric.get("quarters", {}).get(quarter, {})
            original_q = original_metric.get("quarters", {}).get(quarter, {})

            engine_value = engine_q.get("value")
            original_value = original_q.get("value")
            if engine_value is None or original_value is None:
                if engine_value != original_value:
                    differences.append({
                        "ticker": ticker, "metric": metric_name, "quarter": quarter, "field": "value",
                        "engine_value": engine_value, "original_value": original_value,
                    })
            elif not values_close(float(engine_value), float(original_value)):
                differences.append({
                    "ticker": ticker, "metric": metric_name, "quarter": quarter, "field": "value",
                    "engine_value": engine_value, "original_value": original_value,
                })

            engine_basis = engine_q.get("extraction_basis")
            original_basis = original_q.get("extraction_basis")
            if engine_basis != original_basis:
                differences.append({
                    "ticker": ticker, "metric": metric_name, "quarter": quarter, "field": "extraction_basis",
                    "engine_value": engine_basis, "original_value": original_basis,
                })

    return differences


def run_reproduction_check() -> dict:
    print("=" * 100)
    print("REPRODUCTION CHECK — consolidated engine vs. verified per-company proofs")
    print("=" * 100)

    all_differences: list[dict] = []
    per_company_results: dict[str, dict] = {}
    total_results = 0

    for ticker, spec in COMPANIES.items():
        print(f"\n>>> Running engine for {ticker} FY (fiscal_year_end={spec['fiscal_year_end']}) <<<")
        engine_output = s118.run_quarterly_extraction_engine(
            ticker=ticker,
            fiscal_year_end=spec["fiscal_year_end"],
            q1_accession=spec["q1_accession"],
            q2_accession=spec["q2_accession"],
            q3_accession=spec["q3_accession"],
            fy_accession=spec["fy_accession"],
            json_output_path=spec["engine_json"],
            csv_output_path=spec["engine_csv"],
        )

        result_count = sum(
            len(m.get("quarters", {})) for m in engine_output["metrics"].values()
        )
        total_results += result_count

        differences = compare_company(ticker, engine_output, spec["original_json"])
        all_differences.extend(differences)

        statuses = {m: engine_output["metrics"][m].get("status") for m in METRICS}
        per_company_results[ticker] = {
            "result_count": result_count,
            "statuses": statuses,
            "differences_vs_original": differences,
        }

        print(f"{ticker}: {result_count} quarterly results, statuses={statuses}, "
              f"differences_vs_original={len(differences)}")

    print(f"\nTOTAL RESULTS across 3 companies: {total_results} (expected 72)")
    print(f"TOTAL DIFFERENCES vs. original verified JSONs: {len(all_differences)}")
    for diff in all_differences:
        print(f"  DIFF: {diff}")

    return {
        "total_results": total_results,
        "expected_total_results": 72,
        "total_results_matches_expected": total_results == 72,
        "total_differences": len(all_differences),
        "differences": all_differences,
        "per_company": per_company_results,
    }


def run_fail_closed_synthetic_test() -> dict:
    """No database access, no company processed, nothing written to
    either database. Hand-built numbers only: 4 source facts each
    reported at decimals=-6 (uncertainty $500,000 each -> permitted
    difference $2,000,000, exactly as derived for ORCL in D-035), but
    with a reconciliation gap of $5,000,000 — well beyond what 4x
    $500,000 can explain. Confirms the engine's fail-closed branch
    (REVIEW_REQUIRED) actually triggers when the gap is genuinely too
    large, not just when it happens to be small."""

    print("\n" + "=" * 100)
    print("FAIL-CLOSED SYNTHETIC TEST")
    print("=" * 100)

    # Q1-Q4 sum to an exact $4,000,000,000. Annual is deliberately set
    # $5,000,000 higher ($4,005,000,000) — a gap that exceeds the
    # $2,000,000 permitted difference (4 independently reported facts x
    # $500,000 uncertainty at decimals=-6). Q4 is passed directly here
    # (this pure function takes it as an input, unlike the real engine
    # which derives it as Annual-Q3_9mYTD) specifically so the gap is
    # real and not self-cancelling by construction.
    q1_value = 1_000_000_000.0
    q2_value = 1_000_000_000.0
    q3_value = 1_000_000_000.0
    q4_value = 1_000_000_000.0
    annual_value = 4_005_000_000.0

    result = s118.compute_precision_aware_reconciliation(
        q1_value=q1_value, q1_decimals="-6",
        q2_value=q2_value, q2_basis="DIRECT_QUARTER", q2_source_decimals="-6",
        q3_value=q3_value, q3_basis="DIRECT_QUARTER", q3_source_decimals="-6",
        q4_value=q4_value,
        annual_value=annual_value, annual_decimals="-6",
    )

    print(f"Q1={q1_value:,.0f}  Q2={q2_value:,.0f}  Q3={q3_value:,.0f}  Q4={q4_value:,.0f}")
    print(f"Annual={annual_value:,.0f}")
    print(f"actual_difference={result['difference']:,.2f}")
    print(f"permitted_difference={result['precision_calculation']['permitted_difference']:,.2f}")
    print(f"status={result['status']}")

    test_passed = result["status"] == "REVIEW_REQUIRED"
    print(f"\nTEST {'PASSED' if test_passed else 'FAILED'}: "
          f"engine returned {result['status']!r}, expected 'REVIEW_REQUIRED' "
          f"(difference {abs(result['difference']):,.0f} exceeds permitted "
          f"{result['precision_calculation']['permitted_difference']:,.0f})")

    return {
        "test_name": "fail_closed_synthetic_test",
        "synthetic_inputs": {
            "q1_value": q1_value, "q2_value": q2_value, "q3_value": q3_value,
            "q4_value": q4_value, "annual_value": annual_value,
            "all_decimals": "-6",
        },
        "engine_result": result,
        "test_passed": test_passed,
        "note": "No database access. No company processed. Nothing written to either database.",
    }


def main() -> None:
    reproduction_result = run_reproduction_check()
    fail_closed_result = run_fail_closed_synthetic_test()

    combined = {
        "reproduction_check": reproduction_result,
        "fail_closed_synthetic_test": fail_closed_result,
        "overall_pass": (
            reproduction_result["total_results_matches_expected"]
            and reproduction_result["total_differences"] == 0
            and fail_closed_result["test_passed"]
        ),
    }

    with VALIDATION_RESULT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(combined, handle, indent=2, ensure_ascii=False, default=str)

    print("\n" + "=" * 100)
    print("OVERALL VALIDATION RESULT")
    print(f"  total_results == 72: {reproduction_result['total_results_matches_expected']}")
    print(f"  total_differences == 0: {reproduction_result['total_differences'] == 0}")
    print(f"  fail_closed_test_passed: {fail_closed_result['test_passed']}")
    print(f"  OVERALL PASS: {combined['overall_pass']}")
    print(f"\nWritten to {VALIDATION_RESULT_PATH}")
    print("=" * 100)


if __name__ == "__main__":
    main()
