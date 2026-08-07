"""
Read-only root-cause analysis of every REVIEW_REQUIRED primary metric
result in data\\database\\ai_stock_agent.duckdb.

Does NOT modify the database (connects read_only=True). Does NOT call
the XBRL engine, Arelle, or SEC EDGAR. Does NOT change any accounting
policy, metric value, or status — this script only reads and reports.

Method: for every REVIEW_REQUIRED primary metric, the engine's own
stored `validation_reason` text is inspected for an embedded Python
dict literal of the form "{'component': 'STATUS', ...}" — this is
exactly how compute_derived_metric() and the custom effective_tax_rate/
total_debt functions already report *which* component(s) blocked them
(see scripts\\60_xbrl_metric_engine.py). Recursively following the
first-failing component(s) named in that dict, within the SAME
company-year's full metric set (not just the primary 20 — includes
`_prior` support metrics and total_debt_explicit), walks every
REVIEW_REQUIRED result back to the LEAF metric(s) that have no such
dict — i.e. a metric whose own row/fact extraction genuinely failed or
whose own validation rule genuinely rejected a value. That leaf is the
"root cause"; everything above it in the chain is "downstream
propagated". No dependency graph is hard-coded — the chain is derived
entirely from the engine's own already-stored reasoning, so it can
never drift from what the engine actually did.

Outputs (data\\):
  review_required_summary.csv          — the 222 rows themselves, with
                                          root-cause classification
  review_required_root_causes.csv      — one row per unique root cause,
                                          ranked
  review_required_dependency_chains.csv — one row per (company-year,
                                          primary metric) with its full
                                          downstream -> root chain
  review_required_examples.csv         — >=3 examples for the 5 largest
                                          root causes
"""

from __future__ import annotations

import ast
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DATABASE_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

DICT_PATTERN = re.compile(r"\{[^{}]*\}")

# Applied ONLY to a leaf-level (non-cascading) validation_reason, after
# the dependency chain has already been walked down to it — so, unlike
# the earlier per-message classifier in scripts\61, this never needs a
# special case for a cascading message wearing a different metric's
# wording, because cascading messages are never classified directly.
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


def classify_leaf(validation_reason: str) -> str:
    for label, pattern in LEAF_PATTERNS:
        if re.search(pattern, validation_reason or ""):
            return label
    return "unclassified_leaf"


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
    """
    Returns a list of {"root_metric", "root_reason", "path"} dicts — one
    per independent leaf-level root cause reachable from metric_name
    (which must itself be REVIEW_REQUIRED in `metrics`).
    """

    if metric_name in visited:
        return []

    visited = visited | {metric_name}
    row = metrics.get(metric_name)

    if row is None:
        return [
            {
                "root_metric": metric_name,
                "root_reason": "רכיב לא נמצא בנתוני שנת-החברה",
                "path": [metric_name],
            }
        ]

    component_dict = parse_component_dict(row.get("validation_reason"))

    if not component_dict:
        return [
            {
                "root_metric": metric_name,
                "root_reason": row.get("validation_reason") or "",
                "path": [metric_name],
            }
        ]

    failing = [
        name for name, status in component_dict.items() if status != "PASS"
    ]

    if not failing:
        return [
            {
                "root_metric": metric_name,
                "root_reason": row.get("validation_reason") or "",
                "path": [metric_name],
            }
        ]

    results: list[dict[str, Any]] = []

    for component_name in failing:
        sub_results = resolve_roots(component_name, metrics, visited)

        if not sub_results:
            component_row = metrics.get(component_name)
            reason = (
                component_row.get("validation_reason")
                if component_row
                else f"רכיב '{component_name}' לא נמצא בנתונים"
            ) or f"רכיב '{component_name}' עבר PASS או שאינו זמין לניתוח"
            sub_results = [
                {
                    "root_metric": component_name,
                    "root_reason": reason,
                    "path": [component_name],
                }
            ]

        for sub in sub_results:
            results.append({**sub, "path": [metric_name] + sub["path"]})

    return results


def main() -> None:
    timings: dict[str, float] = {}
    total_start = time.perf_counter()

    phase_start = time.perf_counter()
    connection = duckdb.connect(database=str(DATABASE_PATH), read_only=True)
    timings["connect"] = time.perf_counter() - phase_start

    phase_start = time.perf_counter()
    all_rows = connection.execute(
        """
        SELECT
            sf.ticker, sf.report_date, sf.fiscal_year, sf.accession_number,
            sf.filing_date, er.engine_version,
            fmr.metric_name, fmr.is_primary_metric, fmr.status, fmr.value,
            fmr.unit, fmr.source_concept, fmr.label, fmr.validation_reason
        FROM financial_metric_results fmr
        JOIN extraction_runs er ON fmr.extraction_run_id = er.extraction_run_id
        JOIN sec_filings sf ON er.accession_number = sf.accession_number
        """
    ).fetchall()
    columns = [
        "ticker", "report_date", "fiscal_year", "accession_number",
        "filing_date", "engine_version", "metric_name", "is_primary_metric",
        "status", "value", "unit", "source_concept", "label",
        "validation_reason",
    ]
    all_rows = [dict(zip(columns, row)) for row in all_rows]
    timings["read_database"] = time.perf_counter() - phase_start

    phase_start = time.perf_counter()

    metrics_by_company_year: dict[tuple[str, str], dict[str, dict[str, Any]]] = (
        defaultdict(dict)
    )
    for row in all_rows:
        key = (row["ticker"], str(row["report_date"]))
        metrics_by_company_year[key][row["metric_name"]] = row

    review_required_primary = [
        row
        for row in all_rows
        if row["is_primary_metric"] and row["status"] == "REVIEW_REQUIRED"
    ]

    total_review_required = len(review_required_primary)

    summary_rows: list[dict[str, Any]] = []
    dependency_chain_rows: list[dict[str, Any]] = []
    unique_root_problems: set[tuple[str, str, str]] = set()
    root_cause_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "primary_item_count": 0,
            "downstream_caused_count": 0,
            "companies": set(),
            "fiscal_years": set(),
            "company_years": set(),
        }
    )

    for row in review_required_primary:
        key = (row["ticker"], str(row["report_date"]))
        metrics = metrics_by_company_year[key]

        roots = resolve_roots(row["metric_name"], metrics)

        is_primary_root_item = (
            len(roots) == 1 and roots[0]["root_metric"] == row["metric_name"]
        )

        root_labels = []
        for root in roots:
            leaf_class = classify_leaf(root["root_reason"])
            root_cause_label = f"{root['root_metric']}::{leaf_class}"
            root_labels.append(root_cause_label)

            unique_root_problems.add(
                (row["ticker"], key[1], root_cause_label)
            )

            stats = root_cause_stats[root_cause_label]
            stats["companies"].add(row["ticker"])
            stats["fiscal_years"].add(row["fiscal_year"])
            stats["company_years"].add(key)
            if is_primary_root_item and root["root_metric"] == row["metric_name"]:
                stats["primary_item_count"] += 1
            else:
                stats["downstream_caused_count"] += 1

            dependency_chain_rows.append(
                {
                    "ticker": row["ticker"],
                    "report_date": row["report_date"],
                    "fiscal_year": row["fiscal_year"],
                    "downstream_metric": row["metric_name"],
                    "chain_path": " -> ".join(root["path"]),
                    "root_metric": root["root_metric"],
                    "root_cause": root_cause_label,
                    "root_reason": root["root_reason"],
                    "is_primary_root_item": is_primary_root_item,
                }
            )

        summary_rows.append(
            {
                "ticker": row["ticker"],
                "report_date": row["report_date"],
                "fiscal_year": row["fiscal_year"],
                "accession_number": row["accession_number"],
                "metric_name": row["metric_name"],
                "status": row["status"],
                "value": row["value"],
                "validation_reason": row["validation_reason"],
                "is_primary_root_item": is_primary_root_item,
                "root_metrics": ";".join(r["root_metric"] for r in roots),
                "root_causes": ";".join(root_labels),
            }
        )

    primary_root_count = sum(1 for r in summary_rows if r["is_primary_root_item"])
    propagated_count = total_review_required - primary_root_count

    timings["dependency_analysis"] = time.perf_counter() - phase_start

    # --- root-cause ranking table ---------------------------------------
    phase_start = time.perf_counter()

    root_cause_rows = []
    for root_cause, stats in root_cause_stats.items():
        total_caused = stats["primary_item_count"] + stats["downstream_caused_count"]
        root_cause_rows.append(
            {
                "root_cause": root_cause,
                "primary_item_count": stats["primary_item_count"],
                "downstream_caused_count": stats["downstream_caused_count"],
                "total_results_affected": total_caused,
                "companies_affected": len(stats["companies"]),
                "companies_list": ",".join(sorted(stats["companies"])),
                "fiscal_years_affected": len(stats["fiscal_years"]),
                "fiscal_years_list": ",".join(
                    str(y) for y in sorted(stats["fiscal_years"])
                ),
                "unique_company_years": len(stats["company_years"]),
                "potential_pass_conversions_if_resolved": total_caused,
            }
        )

    root_cause_rows.sort(key=lambda r: -r["total_results_affected"])

    timings["ranking"] = time.perf_counter() - phase_start

    # --- write outputs ----------------------------------------------------
    phase_start = time.perf_counter()

    import csv

    def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8-sig")
            return
        fieldnames = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    write_csv(DATA_DIR / "review_required_summary.csv", summary_rows)
    write_csv(DATA_DIR / "review_required_root_causes.csv", root_cause_rows)
    write_csv(
        DATA_DIR / "review_required_dependency_chains.csv",
        dependency_chain_rows,
    )

    # examples: >=3 for the 5 largest root causes
    example_rows: list[dict[str, Any]] = []
    for root_cause_row in root_cause_rows[:5]:
        root_cause = root_cause_row["root_cause"]
        matches = [
            chain
            for chain in dependency_chain_rows
            if chain["root_cause"] == root_cause
        ]
        # prefer distinct company-years for variety
        seen_company_years = set()
        picked = []
        for chain in matches:
            cy = (chain["ticker"], chain["report_date"])
            if cy in seen_company_years:
                continue
            seen_company_years.add(cy)
            picked.append(chain)
            if len(picked) >= 3:
                break
        if len(picked) < 3:
            picked = matches[:3]

        for chain in picked:
            key = (chain["ticker"], str(chain["report_date"]))
            root_metric_row = metrics_by_company_year[key].get(
                chain["root_metric"]
            )
            downstream_affected = sorted(
                {
                    c["downstream_metric"]
                    for c in dependency_chain_rows
                    if c["ticker"] == chain["ticker"]
                    and c["report_date"] == chain["report_date"]
                    and c["root_metric"] == chain["root_metric"]
                }
            )
            example_rows.append(
                {
                    "root_cause": root_cause,
                    "ticker": chain["ticker"],
                    "fiscal_year": chain["fiscal_year"],
                    "report_date": chain["report_date"],
                    "accession_number": next(
                        r["accession_number"]
                        for r in review_required_primary
                        if r["ticker"] == chain["ticker"]
                        and str(r["report_date"]) == str(chain["report_date"])
                    ),
                    "root_metric": chain["root_metric"],
                    "root_metric_status": (
                        root_metric_row["status"] if root_metric_row else None
                    ),
                    "root_metric_value": (
                        root_metric_row["value"] if root_metric_row else None
                    ),
                    "root_metric_validation_reason": chain["root_reason"],
                    "root_metric_selected_concept": (
                        root_metric_row["source_concept"]
                        if root_metric_row
                        else None
                    ),
                    "downstream_metrics_affected": ";".join(downstream_affected),
                }
            )

    write_csv(DATA_DIR / "review_required_examples.csv", example_rows)

    timings["write_outputs"] = time.perf_counter() - phase_start
    timings["total"] = time.perf_counter() - total_start

    # --- console report ---------------------------------------------------
    print(f"מסד נתונים (קריאה בלבד): {DATABASE_PATH}")
    print()
    print(f"סה\"כ תוצאות REVIEW_REQUIRED ראשוניות: {total_review_required}")
    print(f"בעיות שורש ראשוניות (primary root-cause items): {primary_root_count}")
    print(f"תוצאות מורשות/משוקללות (downstream propagated): {propagated_count}")
    print(f"בעיות שורש ייחודיות (חברה-שנה x root_cause): {len(unique_root_problems)}")
    print()

    print("=== 10 גורמי השורש הגדולים ביותר ===")
    for r in root_cause_rows[:10]:
        print(
            f"{r['root_cause']:55s} total={r['total_results_affected']:3d}  "
            f"primary={r['primary_item_count']:2d}  downstream={r['downstream_caused_count']:3d}  "
            f"companies={r['companies_affected']}  years={r['fiscal_years_affected']}"
        )
    print()

    orcl_fy2021_tax = next(
        (
            r
            for r in summary_rows
            if r["ticker"] == "ORCL"
            and str(r["report_date"]) == "2021-05-31"
            and r["metric_name"] == "effective_tax_rate"
        ),
        None,
    )
    print("=== אימות: ORCL FY2021 effective_tax_rate ===")
    if orcl_fy2021_tax:
        print(
            f"is_primary_root_item={orcl_fy2021_tax['is_primary_root_item']}, "
            f"root_causes={orcl_fy2021_tax['root_causes']}"
        )
    else:
        print("לא נמצא!")
    print()

    print("=== זמני ריצה בפועל (שניות) ===")
    for phase, duration in timings.items():
        print(f"{phase:24s} {duration:.4f}")

    connection.close()


if __name__ == "__main__":
    main()
