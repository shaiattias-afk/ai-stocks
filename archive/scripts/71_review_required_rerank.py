"""
Read-only rerank of the REVIEW_REQUIRED results using the LATEST
extraction run per filing (engine v15 for the 9 pretax_income-affected
company-years from D-020, engine v14 unchanged for the other 36).

Does NOT touch the database, does NOT call Arelle, does NOT re-extract
anything, does NOT implement any correction or change any accounting
policy. Pure re-analysis of already-produced result files, reusing the
same dependency-chain root-cause method as scripts\\68 and \\70, extended
to separately report primary-root-item counts vs downstream-cascade
counts per root cause (matching the structure requested in this task).
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
OLD_DATASET_JSON = DATA_DIR / "historical_dataset_v1.json"

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

LEAF_PATTERNS: list[tuple[str, str]] = [
    ("zero_inference_role_not_found", r"לא נמצא באופן חד-משמעי ביאור לוח פירעונות חוב"),
    ("zero_inference_earliest_bucket_nonzero", r"אינה אפס \("),
    ("zero_inference_prior_bucket_unreliable", r"לא ניתן לחלץ ערך מהימן ויחיד עבור השורה המוקדמת ביותר"),
    ("zero_inference_total_mismatch_with_ltd", r"אינו מתיישב עם long_term_debt מהמאזן"),
    ("ancestry_confirmed_absent", r"וגם לא נמצאה שורת חוב לא-שוטף"),
    ("outside_plausible_range", r"מחוץ לטווח הסביר"),
    ("pretax_income_not_positive", r"Pretax Income אינו חיובי"),
    ("row_ambiguous", r"לא ניתן לזהות שורת .* יחידה וחד-משמעית"),
    ("row_not_found", r"לא נמצאה אף שורת"),
    ("period_mismatch", r"תקופות הדיווח.*אינן זהות"),
    ("formula_invalid_result", r"הנוסחה לא הניבה ערך תקין"),
]

# Root causes that reflect an engine/extraction gap (potentially
# fixable by broadening row identification / structural rules) vs
# root causes that reflect a genuine, correctly-flagged accounting
# fact (not an extraction bug — D-008/D-015 fail-closed working as
# designed).
GENERAL_EXTRACTION_LEAVES = {
    "zero_inference_role_not_found",
    "zero_inference_earliest_bucket_nonzero",
    "zero_inference_prior_bucket_unreliable",
    "zero_inference_total_mismatch_with_ltd",
    "ancestry_confirmed_absent",
    "row_ambiguous",
    "row_not_found",
    "period_mismatch",
    "formula_invalid_result",
}
GENUINE_ACCOUNTING_LEAVES = {
    "outside_plausible_range",
    "pretax_income_not_positive",
}


def classify_leaf(reason: str) -> str:
    for label, pattern in LEAF_PATTERNS:
        if re.search(pattern, reason):
            return label
    return "unclassified"


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


def resolve_root_leaves(
    metric_name: str,
    metrics: dict[str, dict[str, Any]],
    visited: frozenset[str] = frozenset(),
) -> set[str]:
    """Returns the set of root_cause labels ('metric::leaf_class') this metric's
    REVIEW_REQUIRED status ultimately traces back to."""
    if metric_name in visited:
        return set()
    visited = visited | {metric_name}
    row = metrics.get(metric_name)
    if row is None:
        return {f"{metric_name}::missing_component"}
    reason = row.get("validation_reason") or ""
    component_dict = parse_component_dict(reason)
    if not component_dict:
        return {f"{metric_name}::{classify_leaf(reason)}"}
    failing = [n for n, s in component_dict.items() if s != "PASS"]
    if not failing:
        return {f"{metric_name}::{classify_leaf(reason)}"}
    roots: set[str] = set()
    for component_name in failing:
        roots |= resolve_root_leaves(component_name, metrics, visited)
    return roots


def build_merged_state() -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
    with OLD_DATASET_JSON.open(encoding="utf-8-sig") as handle:
        old_rows = json.load(handle)

    affected_keys = {(t, rd) for t, rd, _ in AFFECTED_COMPANY_YEARS}
    metrics_by_company_year: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)

    for row in old_rows:
        key = (row["ticker"], str(row["report_date"]))
        if key in affected_keys:
            continue
        metrics_by_company_year[key][row["metric_name"]] = {
            "status": row["status"],
            "validation_reason": row["validation_reason"],
            "is_primary_metric": row["is_primary_metric"],
        }

    for ticker, report_date, file_prefix in AFFECTED_COMPANY_YEARS:
        result_path = DATA_DIR / f"{file_prefix}_engine_v15_result.json"
        with result_path.open(encoding="utf-8-sig") as handle:
            result = json.load(handle)
        key = (ticker, report_date)
        for metric_name, metric_result in result["metrics"].items():
            metrics_by_company_year[key][metric_name] = {
                "status": metric_result.get("status"),
                "validation_reason": metric_result.get("error"),
                "is_primary_metric": metric_name in PRIMARY_METRICS,
            }

    return metrics_by_company_year


def main() -> None:
    metrics_by_company_year = build_merged_state()

    review_required: list[tuple[str, str, str, dict[str, Any]]] = []
    for (ticker, report_date), metrics in metrics_by_company_year.items():
        for metric_name in PRIMARY_METRICS:
            row = metrics.get(metric_name)
            if row and row["status"] == "REVIEW_REQUIRED":
                review_required.append((ticker, report_date, metric_name, row))

    total_count = len(review_required)

    # classify each REVIEW_REQUIRED result as primary (its own reason is
    # a leaf) or downstream (its own reason cascades from other metrics)
    primary_items: list[tuple[str, str, str, str]] = []  # ticker, date, metric, root_cause
    downstream_items: list[tuple[str, str, str, set[str]]] = []  # ticker, date, metric, root_cause set

    for ticker, report_date, metric_name, row in review_required:
        reason = row.get("validation_reason") or ""
        component_dict = parse_component_dict(reason)
        if not component_dict or not any(s != "PASS" for s in component_dict.values()):
            root_cause = f"{metric_name}::{classify_leaf(reason)}"
            primary_items.append((ticker, report_date, metric_name, root_cause))
        else:
            roots = resolve_root_leaves(metric_name, metrics_by_company_year[(ticker, report_date)])
            downstream_items.append((ticker, report_date, metric_name, roots))

    # aggregate per root cause
    agg: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "primary_count": 0,
        "downstream_count": 0,
        "company_years": set(),
    })

    for ticker, report_date, metric_name, root_cause in primary_items:
        agg[root_cause]["primary_count"] += 1
        agg[root_cause]["company_years"].add((ticker, report_date))

    for ticker, report_date, metric_name, roots in downstream_items:
        for root_cause in roots:
            agg[root_cause]["downstream_count"] += 1
            agg[root_cause]["company_years"].add((ticker, report_date))

    ranked = sorted(
        agg.items(),
        key=lambda kv: -(kv[1]["primary_count"] + kv[1]["downstream_count"]),
    )

    print(f"REVIEW_REQUIRED total (latest state, merged v15+v14): {total_count}")
    print(f"  primary root items: {len(primary_items)}")
    print(f"  downstream/cascading items: {len(downstream_items)}")
    print()

    print("=== TOP 5 ROOT CAUSES (ranked by total potentially resolved) ===")
    for root_cause, info in ranked[:5]:
        total_potential = info["primary_count"] + info["downstream_count"]
        metric_name = root_cause.split("::")[0]
        leaf = root_cause.split("::")[1]
        kind = (
            "general extraction issue" if leaf in GENERAL_EXTRACTION_LEAVES
            else "genuine accounting review" if leaf in GENUINE_ACCOUNTING_LEAVES
            else "unclassified"
        )
        by_ticker: dict[str, list[str]] = defaultdict(list)
        for t, rd in sorted(info["company_years"]):
            by_ticker[t].append(rd)
        print(f"\n--- {root_cause} ---")
        print(f"  kind: {kind}")
        print(f"  primary root-item count: {info['primary_count']}")
        print(f"  downstream result count: {info['downstream_count']}")
        print(f"  total results potentially resolved: {total_potential}")
        print(f"  companies/years affected: " + "; ".join(f"{t}({', '.join(v)})" for t, v in by_ticker.items()))

    print()
    print("=== FULL ROOT-CAUSE RANKING (all causes) ===")
    for root_cause, info in ranked:
        total_potential = info["primary_count"] + info["downstream_count"]
        print(f"{root_cause:55s} primary={info['primary_count']:<4d} downstream={info['downstream_count']:<4d} total={total_potential:<4d}")


if __name__ == "__main__":
    main()
