"""
Applies the pretax_income structural fix (scripts\\69_xbrl_metric_engine.py,
accounting policy D-020) to the persistent database and to the
REVIEW_REQUIRED analysis, WITHOUT overwriting any previous result.

Does three things, in order:

1. Loads the 9 newly re-extracted company-years (engine v15) into
   data\\database\\ai_stock_agent.duckdb as brand-new extraction_runs
   (extraction_run_id = accession_number + "::" + engine_version, and
   "v15 (scripts/69_xbrl_metric_engine.py)" is a different string from
   the existing "v14 (scripts/60_xbrl_metric_engine.py)" runs already
   in the database for the SAME accession numbers) — the old v14 rows
   for these 9 filings are left completely untouched, still queryable,
   still the point-in-time record of what v14 produced. Uses the same
   idempotent ON CONFLICT DO NOTHING pattern as scripts\\64/66, so this
   script is itself safe to re-run.

2. Builds a "latest state" merged view of all 45 company-years — v15
   results for the 9 affected filings, v14 results (unchanged) for the
   other 36 — purely in memory, and re-runs the SAME dependency-chain
   root-cause analysis as scripts\\68 against it.

3. Reports: previous REVIEW_REQUIRED count (222), new count, the exact
   set of results that changed status, remaining root causes, and
   confirms no regression against the 9 anchor years (all previously
   confirmed unchanged in this same pretax_income-fix session).

Read-only with respect to the 36 unaffected company-years and every
previously-written output file — nothing here overwrites
data\\historical_dataset_v1.* or any prior *_engine_v14_* file.
"""

from __future__ import annotations

import ast
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DATABASE_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"
OLD_DATASET_JSON = DATA_DIR / "historical_dataset_v1.json"

NEW_ENGINE_VERSION = "v15 (scripts/69_xbrl_metric_engine.py)"
OLD_ENGINE_VERSION = "v14 (scripts/60_xbrl_metric_engine.py)"

# The 9 company-years re-extracted with the pretax_income structural
# fix (accounting policy D-020).
AFFECTED_COMPANY_YEARS = [
    ("CRWD", "2022-01-31", "crwd_20220131"),
    ("CRWD", "2023-01-31", "crwd_20230131"),
    ("GOOGL", "2021-12-31", "googl_20211231"),
    ("GOOGL", "2022-12-31", "googl_20221231"),
    ("GOOGL", "2023-12-31", "googl_20231231"),
    ("MU", "2021-09-02", "mu_20210902"),
    ("MU", "2022-09-01", "mu_20220901"),
    ("PANW", "2021-07-31", "panw_20210731"),
    ("PANW", "2022-07-31", "panw_20220731"),
]

PRIMARY_METRICS = [
    "revenue", "net_income", "operating_income", "operating_cash_flow",
    "capex", "free_cash_flow", "cash_and_equivalents",
    "short_term_investments", "current_debt", "long_term_debt",
    "total_debt", "adjusted_net_debt", "pretax_income",
    "income_tax_expense", "stockholders_equity", "effective_tax_rate",
    "nopat", "invested_capital", "average_invested_capital", "roic",
]

DICT_PATTERN = re.compile(r"\{[^{}]*\}")


def parse_component_dict(validation_reason: str | None) -> dict[str, str] | None:
    if not validation_reason:
        return None
    match = DICT_PATTERN.search(validation_reason)
    if not match:
        return None
    try:
        parsed = ast.literal_eval(match.group(0))
    except (ValueError, SyntaxError):
        return None
    if isinstance(parsed, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        return parsed
    return None


def resolve_roots(
    metric_name: str,
    metrics: dict[str, dict[str, Any]],
    visited: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    if metric_name in visited:
        return []
    visited = visited | {metric_name}
    row = metrics.get(metric_name)
    if row is None:
        return [{"root_metric": metric_name, "root_reason": "רכיב לא נמצא", "path": [metric_name]}]
    component_dict = parse_component_dict(row.get("validation_reason"))
    if not component_dict:
        return [{"root_metric": metric_name, "root_reason": row.get("validation_reason") or "", "path": [metric_name]}]
    failing = [n for n, s in component_dict.items() if s != "PASS"]
    if not failing:
        return [{"root_metric": metric_name, "root_reason": row.get("validation_reason") or "", "path": [metric_name]}]
    results = []
    for component_name in failing:
        sub = resolve_roots(component_name, metrics, visited)
        if not sub:
            comp_row = metrics.get(component_name)
            reason = (comp_row.get("validation_reason") if comp_row else None) or f"רכיב '{component_name}' לא זמין"
            sub = [{"root_metric": component_name, "root_reason": reason, "path": [component_name]}]
        for s in sub:
            results.append({**s, "path": [metric_name] + s["path"]})
    return results


# =============================================================================
# Phase 1: load the 9 new v15 results into the persistent database as
# new extraction_runs (never touching the existing v14 rows).
# =============================================================================


def load_new_extraction_runs(connection: duckdb.DuckDBPyConnection) -> int:
    inserted = 0

    for ticker, report_date, file_prefix in AFFECTED_COMPANY_YEARS:
        result_path = DATA_DIR / f"{file_prefix}_engine_v15_result.json"
        with result_path.open(encoding="utf-8-sig") as handle:
            result = json.load(handle)

        accession_number = result["accession_number"]
        run_id = f"{accession_number}::{NEW_ENGINE_VERSION}"

        connection.execute(
            """
            INSERT INTO extraction_runs (
                extraction_run_id, accession_number, engine_version
            )
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [run_id, accession_number, NEW_ENGINE_VERSION],
        )

        for metric_name, metric_result in result["metrics"].items():
            is_primary = metric_name in PRIMARY_METRICS
            value = metric_result.get("value")
            if value is None:
                value = metric_result.get("selected_value")

            connection.execute(
                """
                INSERT INTO financial_metric_results (
                    extraction_run_id, metric_name, is_primary_metric,
                    status, value, unit, context_id, period_start,
                    period_end, source_concept, label,
                    statement_role_definition, selection_tier,
                    is_derived_metric, formula, validation_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                [
                    run_id,
                    metric_name,
                    is_primary,
                    metric_result.get("status"),
                    value,
                    metric_result.get("unit") or metric_result.get("selected_unit"),
                    metric_result.get("context_id") or metric_result.get("selected_context_id"),
                    metric_result.get("period_start") or metric_result.get("selected_period_start"),
                    metric_result.get("period_end") or metric_result.get("selected_period_end"),
                    metric_result.get("source_concept") or metric_result.get("target_concept_qname"),
                    metric_result.get("label") or metric_result.get("target_label"),
                    metric_result.get("statement_role_definition") or metric_result.get("target_role_definition"),
                    metric_result.get("selection_tier"),
                    bool(metric_result.get("is_derived_metric")),
                    metric_result.get("formula"),
                    metric_result.get("error"),
                ],
            )
            inserted += 1

    return inserted


# =============================================================================
# Phase 2+3: merged "latest state" analysis
# =============================================================================


def main() -> None:
    timings: dict[str, float] = {}
    total_start = time.perf_counter()

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    phase_start = time.perf_counter()
    connection = duckdb.connect(database=str(DATABASE_PATH))
    new_rows_inserted = load_new_extraction_runs(connection)
    connection.close()
    timings["load_new_extraction_runs"] = time.perf_counter() - phase_start

    print(f"מסד נתונים עודכן: {DATABASE_PATH}")
    print(f"שורות extraction_run חדשות (v15) שנוצרו/אומתו: {len(AFFECTED_COMPANY_YEARS)}")
    print(f"שורות financial_metric_results חדשות (v15) שעובדו: {new_rows_inserted}")
    print()

    # --- build merged "latest state" -------------------------------------
    phase_start = time.perf_counter()

    with OLD_DATASET_JSON.open(encoding="utf-8-sig") as handle:
        old_rows = json.load(handle)

    affected_keys = {(t, rd) for t, rd, _ in AFFECTED_COMPANY_YEARS}

    metrics_by_company_year: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)

    # unaffected 36 company-years: keep exactly as in the v14 dataset
    for row in old_rows:
        key = (row["ticker"], str(row["report_date"]))
        if key in affected_keys:
            continue
        metrics_by_company_year[key][row["metric_name"]] = {
            "status": row["status"],
            "value": row["value"],
            "validation_reason": row["validation_reason"],
            "source_concept": row["source_concept"],
            "accession_number": row["accession_number"],
            "is_primary_metric": row["is_primary_metric"],
        }

    # affected 9 company-years: use the fresh v15 result files
    for ticker, report_date, file_prefix in AFFECTED_COMPANY_YEARS:
        result_path = DATA_DIR / f"{file_prefix}_engine_v15_result.json"
        with result_path.open(encoding="utf-8-sig") as handle:
            result = json.load(handle)
        key = (ticker, report_date)
        for metric_name, metric_result in result["metrics"].items():
            metrics_by_company_year[key][metric_name] = {
                "status": metric_result.get("status"),
                "value": metric_result.get("value") if metric_result.get("value") is not None else metric_result.get("selected_value"),
                "validation_reason": metric_result.get("error"),
                "source_concept": metric_result.get("source_concept") or metric_result.get("target_concept_qname"),
                "accession_number": result["accession_number"],
                "is_primary_metric": metric_name in PRIMARY_METRICS,
            }

    timings["build_merged_state"] = time.perf_counter() - phase_start

    # --- previous (v14-only) REVIEW_REQUIRED count -------------------------
    phase_start = time.perf_counter()

    previous_review_required = {
        (row["ticker"], str(row["report_date"]), row["metric_name"])
        for row in old_rows
        if row["is_primary_metric"] and row["status"] == "REVIEW_REQUIRED"
    }
    previous_count = len(previous_review_required)

    new_review_required_rows = []
    for key, metrics in metrics_by_company_year.items():
        for metric_name in PRIMARY_METRICS:
            row = metrics.get(metric_name)
            if row and row["status"] == "REVIEW_REQUIRED":
                new_review_required_rows.append((key[0], key[1], metric_name, row))

    new_count = len(new_review_required_rows)
    new_review_required_keys = {
        (t, rd, m) for t, rd, m, _ in new_review_required_rows
    }

    converted_to_pass = sorted(previous_review_required - new_review_required_keys)
    newly_review_required = sorted(new_review_required_keys - previous_review_required)

    timings["compare_before_after"] = time.perf_counter() - phase_start

    # --- rebuild root-cause ranking on the NEW state -----------------------
    phase_start = time.perf_counter()

    root_cause_counts: dict[str, int] = defaultdict(int)
    for ticker, report_date, metric_name, row in new_review_required_rows:
        roots = resolve_roots(metric_name, metrics_by_company_year[(ticker, report_date)])
        for root in roots:
            reason = root["root_reason"]
            label = "leaf"
            if "מחוץ לטווח הסביר" in reason:
                label = "outside_plausible_range"
            elif "אינו חיובי" in reason:
                label = "pretax_income_not_positive"
            elif "לא נמצא באופן חד-משמעי ביאור" in reason:
                label = "zero_inference_role_not_found"
            elif "אינה אפס (" in reason:
                label = "zero_inference_earliest_bucket_nonzero"
            elif "אינו מתיישב עם long_term_debt" in reason:
                label = "zero_inference_total_mismatch_with_ltd"
            elif "וגם לא נמצאה שורת חוב לא-שוטף" in reason:
                label = "ancestry_confirmed_absent"
            elif "לא ניתן לחלץ ערך מהימן ויחיד עבור השורה המוקדמת" in reason:
                label = "zero_inference_prior_bucket_unreliable"
            elif "יחידה וחד-משמעית" in reason:
                label = "row_ambiguous"
            elif "לא נמצאה אף שורת" in reason:
                label = "row_not_found"
            root_cause_counts[f"{root['root_metric']}::{label}"] += 1

    timings["rebuild_root_causes"] = time.perf_counter() - phase_start
    timings["total"] = time.perf_counter() - total_start

    # --- report -------------------------------------------------------------
    print("=== סיכום: REVIEW_REQUIRED לפני/אחרי התיקון ===")
    print(f"קודם (v14, 45 שנות-חברה): {previous_count}")
    print(f"חדש (מצב מעודכן, v15 עבור 9 שנות-חברה + v14 עבור 36): {new_count}")
    print(f"הומרו ל-PASS: {len(converted_to_pass)}")
    print(f"חדשים כ-REVIEW_REQUIRED (רגרסיה פוטנציאלית): {len(newly_review_required)}")
    print()

    print("=== תוצאות שהומרו ל-PASS ===")
    for ticker, report_date, metric_name in converted_to_pass:
        print(f"  {ticker:6s} {report_date:12s} {metric_name}")
    print()

    if newly_review_required:
        print("=== !!! רגרסיה: תוצאות חדשות שהפכו ל-REVIEW_REQUIRED !!! ===")
        for ticker, report_date, metric_name in newly_review_required:
            print(f"  {ticker:6s} {report_date:12s} {metric_name}")
    else:
        print("=== אין רגרסיה: אין תוצאות חדשות שהפכו ל-REVIEW_REQUIRED ===")
    print()

    print("=== גורמי שורש נותרים (מצב חדש) — ממוינים ===")
    for label, count in sorted(root_cause_counts.items(), key=lambda kv: -kv[1]):
        print(f"{label:55s} {count}")
    print()

    print("=== זמני ריצה בפועל (שניות) ===")
    for phase, duration in timings.items():
        print(f"{phase:28s} {duration:.4f}")


if __name__ == "__main__":
    main()
