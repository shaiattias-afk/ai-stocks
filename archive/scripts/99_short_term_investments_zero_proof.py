"""
D-030 — resolves CRWD's `short_term_investments::REVIEW_REQUIRED` for
FY2022 (2022-01-31) and FY2026 (2026-01-31) using a general,
ticker-agnostic zero-proof: the filing's OWN calculation-linkbase
children of `us-gaap:AssetsCurrent` (Total current assets) — the
standard universal GAAP concept, not a CRWD-specific tag, already used
elsewhere in this project the same way `LiabilitiesCurrent` is used for
ancestor-chain classification.

General rule (compared directly against CRWD's own already-PASS
control years, per instruction item 3): if `AssetsCurrent`'s
calculation children (a) do NOT include any concept/label matching the
short-term-investment vocabulary (`marketable securities` /
`short-term investments`), and (b) every child independently resolves
to a single PASS value whose sum EXACTLY reconciles with the reported
`AssetsCurrent` total (also PASS) — with zero unexplained gap — then no
short-term-investment asset class existed for that period:
`status=PASS`, `value=0.0`, `basis=ZERO_PROVEN_STRUCTURAL_ABSENCE`
(same basis name already used for debt in D-026/D-027, applied here to
a different metric under the identical proof-by-completeness logic).

Confirmed directly on CRWD's own FY2023/2024/2025 (all already PASS):
their `AssetsCurrent` calculation DOES include `us-gaap:ShortTermInvestments`
as an explicit child (even when its own value is $0) — proving the
filer's own presentation choice, not a value difference, is what
distinguishes the REVIEW_REQUIRED years from the PASS years. FY2022 and
FY2026's `AssetsCurrent` calculations have 4 children each (cash,
receivables, prepaid/other, deferred contract costs) that sum EXACTLY
to the reported total with zero gap — proving no 5th, short-term-
investment component was ever omitted.

Any candidate that fails either check (a mismatched sum, or an
unresolvable child) remains `REVIEW_REQUIRED`, explained — never
guessed. No CRWD-specific concept name or ticker check anywhere below;
the same function would apply to any filer with the same structural
evidence.

Warehouse-only (all 3 CRWD filings already warehoused). DuckDB only, no
Arelle. Every prior script preserved unchanged; this script reuses
scripts/92's presentation-reconstruction and fact-matching functions
via direct module import (read-only reuse, not duplicated).
"""

from __future__ import annotations

import importlib.util
import re
import sys
import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

ENGINE_VERSION = "v1-short-term-investments-zero-proof (scripts/99, D-030)"

# Only the 2 CRWD years whose short_term_investments is genuinely
# REVIEW_REQUIRED. CRWD 2023 is handled separately below (its own
# short_term_investments already PASSes — only its downstream
# average_invested_capital/roic are recalculated, via the prior-year
# lookup mechanism, unchanged from scripts/93/95).
TARGET_FILINGS: list[tuple[str, str, str]] = [
    ("CRWD", "2022-01-31", "0001535527-22-000006"),
    ("CRWD", "2026-01-31", "0001535527-26-000010"),
]

# Also recalculate downstream (adjusted_net_debt/invested_capital) for
# these same 2 filings, plus rerun the unchanged prior-year-lookup
# scripts (93/95) afterward for all 3 named years.
DOWNSTREAM_RECALC_FILINGS = TARGET_FILINGS

SHORT_TERM_INVESTMENT_VOCABULARY_PATTERN = r"marketable\s+securities|short-?term\s+investments"
ASSETS_CURRENT_CONCEPT_PATTERN = r":AssetsCurrent$"

# Reuse scripts/92's already-approved, unchanged functions.
_spec = importlib.util.spec_from_file_location(
    "s92", PROJECT_DIR / "scripts" / "92_groups_1_3_4_debt_facility_aggregate_policy.py"
)
s92 = importlib.util.module_from_spec(_spec)
sys.modules["s92"] = s92
_spec.loader.exec_module(s92)


def find_assets_current_row(presentation, report_date) -> dict | None:
    """Finds the filing's own 'Total current assets' row (standard
    concept us-gaap:AssetsCurrent — universal GAAP, not ticker-specific)
    on the primary balance sheet role."""

    is_statement = presentation["role_definition"].str.match(r"^\d+\s*-\s*Statement\s*-", na=False)
    is_bs_role = presentation["role_definition"].str.contains(
        r"balance\s+sheets?|financial\s+position", case=False, regex=True, na=False
    )
    is_excluded = presentation["role_definition"].str.contains("parenthetical", case=False, regex=True, na=False)
    bs_rows = presentation[is_statement & is_bs_role & ~is_excluded]

    matches = bs_rows[bs_rows["concept_qname"].str.contains(ASSETS_CURRENT_CONCEPT_PATTERN, regex=True, na=False)]
    matches = matches.drop_duplicates(subset=["role_uri", "concept_qname"])

    if len(matches) != 1:
        return None

    row = matches.iloc[0]
    return {"role_uri": str(row["role_uri"]), "concept_qname": str(row["concept_qname"])}


def attempt_short_term_investments_zero_proof(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation,
    report_date: str,
) -> dict:
    assets_current = find_assets_current_row(presentation, report_date)
    if assets_current is None:
        return {"status": "REVIEW_REQUIRED", "error": "no unique 'Total current assets' (AssetsCurrent) row found on the balance sheet"}

    children_by_parent = s92.reconstruct_calculation_children_by_parent(
        connection, accession_number, assets_current["role_uri"]
    )
    children = children_by_parent.get(assets_current["concept_qname"])

    if not children:
        return {"status": "REVIEW_REQUIRED", "error": "no calculation-linkbase children found for AssetsCurrent — cannot prove completeness"}

    label_by_concept = dict(zip(presentation["concept_qname"], presentation["label"]))

    for child in children:
        child_label = label_by_concept.get(child, child)
        if re.search(SHORT_TERM_INVESTMENT_VOCABULARY_PATTERN, child_label, re.IGNORECASE) or re.search(
            SHORT_TERM_INVESTMENT_VOCABULARY_PATTERN, child, re.IGNORECASE
        ):
            return {
                "status": "REVIEW_REQUIRED",
                "error": (
                    f"AssetsCurrent's own calculation includes a short-term-investment-"
                    f"vocabulary child ({child}, label={child_label!r}) that the standard "
                    "row-identification tier failed to resolve — a genuine ambiguity, not "
                    "a zero case"
                ),
            }

    child_values: dict[str, float] = {}
    for child in children:
        decision = s92.match_facts_from_warehouse(connection, accession_number, child, report_date, "instant")
        if decision["status"] != "PASS":
            return {
                "status": "REVIEW_REQUIRED",
                "error": f"AssetsCurrent child {child} did not resolve to a single value ({decision.get('error')})",
            }
        child_values[child] = decision["value"]

    total_decision = s92.match_facts_from_warehouse(
        connection, accession_number, assets_current["concept_qname"], report_date, "instant"
    )
    if total_decision["status"] != "PASS":
        return {"status": "REVIEW_REQUIRED", "error": "AssetsCurrent total itself did not resolve to a single value"}

    children_sum = sum(child_values.values())
    total_value = total_decision["value"]
    gap = total_value - children_sum

    if abs(gap) > 1:
        return {
            "status": "REVIEW_REQUIRED",
            "error": (
                f"AssetsCurrent children sum ({children_sum}) does not reconcile with the "
                f"reported total ({total_value}), gap={gap} — cannot assume the gap is "
                "short-term investments without guessing"
            ),
        }

    return {
        "status": "PASS",
        "value": 0.0,
        "concept_qname": "proven_zero_no_short_term_investments_component",
        "selection_tier": "zero_proven_no_component_found",
        "basis": "ZERO_PROVEN_STRUCTURAL_ABSENCE",
        "lineage": {
            "assets_current_concept": assets_current["concept_qname"],
            "assets_current_role_uri": assets_current["role_uri"],
            "assets_current_total_value": total_value,
            "calculation_children": [
                {"concept_qname": c, "label": label_by_concept.get(c, c), "value": child_values[c]}
                for c in children
            ],
            "children_sum": children_sum,
            "reconciliation_gap": gap,
            "report_date": report_date,
        },
    }


def latest_metric(connection, ticker, report_date, metric_name):
    row = connection.execute(
        """
        WITH ranked AS (
            SELECT f.status, f.value,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.accession_number, f.metric_name
                       ORDER BY r.loaded_at DESC
                   ) rn
            FROM financial_metric_results f
            JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id
            JOIN sec_filings s ON s.accession_number = r.accession_number
            WHERE s.ticker = ? AND s.report_date = ? AND f.metric_name = ?
        )
        SELECT status, value FROM ranked WHERE rn = 1
        """,
        [ticker, report_date, metric_name],
    ).fetchone()
    return {"status": row[0], "value": row[1]} if row else None


SUCCESSFUL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "PASS_NORMALIZED_TAX"}


def main() -> None:
    total_start = time.perf_counter()
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH))

    converted: list[str] = []
    still_blocked: list[str] = []

    for ticker, report_date, accession_number in TARGET_FILINGS:
        print(f"=== {ticker} {report_date} ({accession_number}) ===")

        current_status = latest_metric(prod_connection, ticker, report_date, "short_term_investments")
        if current_status and current_status["status"] != "REVIEW_REQUIRED":
            print(f"  SKIP — already {current_status['status']}")
            continue

        presentation = s92.reconstruct_presentation_dataframe(warehouse_connection, accession_number)
        proof = attempt_short_term_investments_zero_proof(warehouse_connection, accession_number, presentation, report_date)

        run_id = f"{accession_number}::{ENGINE_VERSION}"
        prod_connection.execute(
            """
            INSERT INTO extraction_runs (extraction_run_id, accession_number, engine_version)
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [run_id, accession_number, ENGINE_VERSION],
        )

        if proof["status"] != "PASS":
            still_blocked.append(f"{ticker} {report_date}: {proof['error']}")
            print(f"  short_term_investments: STILL REVIEW_REQUIRED — {proof['error']}")
            validation_reason = proof["error"]
            prod_connection.execute(
                """
                INSERT INTO financial_metric_results (
                    extraction_run_id, metric_name, is_primary_metric,
                    status, value, unit, context_id, period_start,
                    period_end, source_concept, label,
                    statement_role_definition, selection_tier,
                    is_derived_metric, formula, validation_reason
                )
                VALUES (?, 'short_term_investments', TRUE, 'REVIEW_REQUIRED', NULL, NULL,
                        NULL, NULL, ?, NULL, NULL, NULL, NULL, FALSE, NULL, ?)
                ON CONFLICT DO NOTHING
                """,
                [run_id, report_date, validation_reason],
            )
            print()
            continue

        validation_reason = (
            f"D-030: filing's own AssetsCurrent calculation-linkbase children "
            f"{[c['concept_qname'] for c in proof['lineage']['calculation_children']]} sum "
            f"exactly to the reported total ({proof['lineage']['assets_current_total_value']}), "
            f"gap={proof['lineage']['reconciliation_gap']} — no short-term-investment "
            f"vocabulary child present, proving zero. lineage={proof['lineage']}"
        )

        prod_connection.execute(
            """
            INSERT INTO financial_metric_results (
                extraction_run_id, metric_name, is_primary_metric,
                status, value, unit, context_id, period_start,
                period_end, source_concept, label,
                statement_role_definition, selection_tier,
                is_derived_metric, formula, validation_reason
            )
            VALUES (?, 'short_term_investments', TRUE, 'PASS', 0.0, 'iso4217:USD',
                    NULL, NULL, ?, ?, NULL, NULL, ?, FALSE, NULL, ?)
            ON CONFLICT DO NOTHING
            """,
            [run_id, report_date, proof["concept_qname"], proof["selection_tier"], validation_reason],
        )

        check = latest_metric(prod_connection, ticker, report_date, "short_term_investments")
        if not check or check["status"] != "PASS" or check["value"] != 0.0:
            print("  ERROR — write did not verify back correctly!")
            still_blocked.append(f"{ticker} {report_date}: write verification failed")
            continue

        converted.append(f"{ticker} {report_date}: short_term_investments -> PASS (0.0)")
        print(f"  short_term_investments -> PASS value=0.0 basis=ZERO_PROVEN_STRUCTURAL_ABSENCE [verified]")
        print(f"    calculation children: {[(c['concept_qname'], c['value']) for c in proof['lineage']['calculation_children']]}")
        print(f"    reconciles to reported total: {proof['lineage']['assets_current_total_value']}")
        print()

    # --- Recalculate downstream metrics (adjusted_net_debt, invested_capital)
    # for the 2 filings just fixed, ONLY if they were blocked by
    # short_term_investments specifically ---
    print("=" * 100)
    print("Recalculating downstream metrics blocked by short_term_investments...")
    for ticker, report_date, accession_number in DOWNSTREAM_RECALC_FILINGS:
        sti = latest_metric(prod_connection, ticker, report_date, "short_term_investments")
        if not sti or sti["status"] not in SUCCESSFUL_STATUSES:
            print(f"  {ticker} {report_date}: short_term_investments still not resolved — skipping downstream recalculation")
            continue

        adj_status = latest_metric(prod_connection, ticker, report_date, "adjusted_net_debt")
        ic_status = latest_metric(prod_connection, ticker, report_date, "invested_capital")

        if (adj_status and adj_status["status"] in SUCCESSFUL_STATUSES) and (
            ic_status and ic_status["status"] in SUCCESSFUL_STATUSES
        ):
            print(f"  {ticker} {report_date}: adjusted_net_debt/invested_capital already resolved — nothing to do")
            continue

        total_debt = latest_metric(prod_connection, ticker, report_date, "total_debt")
        cash = latest_metric(prod_connection, ticker, report_date, "cash_and_equivalents")
        equity = latest_metric(prod_connection, ticker, report_date, "stockholders_equity")

        components = {"total_debt": total_debt, "cash_and_equivalents": cash,
                       "short_term_investments": sti, "stockholders_equity": equity}
        all_ok = all(c and c["status"] in SUCCESSFUL_STATUSES for c in components.values())

        def _combine_status(*statuses):
            if all(s in SUCCESSFUL_STATUSES for s in statuses):
                for pref in ("PASS_DIRECT_AGGREGATE", "PASS_MATURITY_BASIS", "PASS_NORMALIZED_TAX"):
                    if pref in statuses:
                        return pref
                return "PASS"
            return "REVIEW_REQUIRED"

        run_id = f"{accession_number}::{ENGINE_VERSION}"
        prod_connection.execute(
            "INSERT INTO extraction_runs (extraction_run_id, accession_number, engine_version) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [run_id, accession_number, ENGINE_VERSION],
        )

        if not (adj_status and adj_status["status"] in SUCCESSFUL_STATUSES):
            adj_ok = all_ok
            adj_value = (total_debt["value"] - cash["value"] - sti["value"]) if adj_ok else None
            adj_status_new = _combine_status(total_debt["status"], cash["status"], sti["status"]) if adj_ok else "REVIEW_REQUIRED"
            prod_connection.execute(
                """
                INSERT INTO financial_metric_results (
                    extraction_run_id, metric_name, is_primary_metric, status, value, unit,
                    context_id, period_start, period_end, source_concept, label,
                    statement_role_definition, selection_tier, is_derived_metric, formula, validation_reason
                ) VALUES (?, 'adjusted_net_debt', TRUE, ?, ?, 'iso4217:USD', NULL, NULL, ?, NULL, NULL, NULL, NULL, TRUE,
                          'total_debt - cash_and_equivalents - short_term_investments', ?)
                ON CONFLICT DO NOTHING
                """,
                [run_id, adj_status_new, adj_value, report_date,
                 f"D-030 downstream recalculation now that short_term_investments resolved (basis={sti.get('status')})"],
            )
            print(f"  {ticker} {report_date}: adjusted_net_debt -> {adj_status_new} ({adj_value})")

        if not (ic_status and ic_status["status"] in SUCCESSFUL_STATUSES):
            ic_ok = all_ok
            ic_value = (total_debt["value"] + equity["value"] - cash["value"] - sti["value"]) if ic_ok else None
            ic_status_new = _combine_status(total_debt["status"], equity["status"], cash["status"], sti["status"]) if ic_ok else "REVIEW_REQUIRED"
            prod_connection.execute(
                """
                INSERT INTO financial_metric_results (
                    extraction_run_id, metric_name, is_primary_metric, status, value, unit,
                    context_id, period_start, period_end, source_concept, label,
                    statement_role_definition, selection_tier, is_derived_metric, formula, validation_reason
                ) VALUES (?, 'invested_capital', TRUE, ?, ?, 'iso4217:USD', NULL, NULL, ?, NULL, NULL, NULL, NULL, TRUE,
                          'total_debt + stockholders_equity - cash_and_equivalents - short_term_investments', ?)
                ON CONFLICT DO NOTHING
                """,
                [run_id, ic_status_new, ic_value, report_date,
                 f"D-030 downstream recalculation now that short_term_investments resolved (basis={sti.get('status')})"],
            )
            print(f"  {ticker} {report_date}: invested_capital -> {ic_status_new} ({ic_value})")

        # verify
        check_adj = latest_metric(prod_connection, ticker, report_date, "adjusted_net_debt")
        check_ic = latest_metric(prod_connection, ticker, report_date, "invested_capital")
        print(f"  verified: adjusted_net_debt={check_adj}, invested_capital={check_ic}")

    warehouse_connection.close()
    prod_connection.close()

    total_elapsed = time.perf_counter() - total_start
    print("\n" + "=" * 100)
    print(f"Converted ({len(converted)}):")
    for line in converted:
        print(f"  {line}")
    print(f"\nStill blocked ({len(still_blocked)}):")
    for line in still_blocked:
        print(f"  {line}")
    print(f"\ntotal_elapsed_seconds = {total_elapsed:.3f}")


if __name__ == "__main__":
    main()
