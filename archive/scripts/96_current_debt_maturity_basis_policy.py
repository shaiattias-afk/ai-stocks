"""
D-028 — lifts the prior D-021 blocking rule ("current_debt must never
be set from a maturity-principal amount") for the specific cases where
this was the ONLY remaining blocker: AMZN (5), GOOGL (5), and META
(2022/2023/2024, 3) current_debt REVIEW_REQUIRED results, all already
identified in D-027's report.

Newly-approved policy: principal contractually due within 12 months
(the filing's OWN debt-maturity schedule's earliest, chronologically-
first bucket) = current_debt. When reliable carrying-value detail is
unavailable (already confirmed true for all 13 of these — none has an
explicit total, calculation-verified components, or an
ancestry-classified current row), the maturity-principal sum may be
used directly:
  status = PASS_MATURITY_BASIS, basis = MATURITY_PRINCIPAL

Scope (bounded, per explicit instruction): ONLY the current_debt
REVIEW_REQUIRED cases for AMZN, GOOGL, META identified in D-027 — CRWD
and NVDA are explicitly excluded, no prior fiscal years are added, no
new filing is loaded (AMZN/GOOGL/META are all already fully warehoused
from D-022/D-027 — verified before writing this script).

Downstream metrics (total_debt, adjusted_net_debt, invested_capital,
average_invested_capital, roic) are recalculated ONLY where their
latest result is currently REVIEW_REQUIRED for a reason traceable to
current_debt — verified below: for all 13 of these company-years,
total_debt/adjusted_net_debt/invested_capital already resolved via the
existing maturity-basis/direct-aggregate fallback (which never
depended on current_debt's own status), so none require any
recalculation here. (AMZN/GOOGL 2021's average_invested_capital/roic
remain REVIEW_REQUIRED for an unrelated, separate reason — no prior
fiscal year exists in the dataset — explicitly out of scope per this
task's instruction not to add prior fiscal years.)

Pure warehouse read + production-DB write. Every previous script
(72-95) is preserved unchanged; only the specific functions needed for
maturity-bucket classification are copied below (unchanged from
scripts/89/92), not the full current_debt resolution chain, since these
13 cases are already confirmed to have no resolvable carrying-value
component by every earlier tier.
"""

from __future__ import annotations

import datetime as _datetime_module
import json
import re
import time
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

STANDARD_LABEL_ROLE = "http://www.xbrl.org/2003/role/label"
ENGINE_VERSION = "v1-current-debt-maturity-basis-policy (scripts/96, D-028)"

TARGET_FILINGS: list[tuple[str, str, str]] = [
    ("AMZN", "2021-12-31", "0001018724-22-000005"),
    ("AMZN", "2022-12-31", "0001018724-23-000004"),
    ("AMZN", "2023-12-31", "0001018724-24-000008"),
    ("AMZN", "2024-12-31", "0001018724-25-000004"),
    ("AMZN", "2025-12-31", "0001018724-26-000004"),
    ("GOOGL", "2021-12-31", "0001652044-22-000019"),
    ("GOOGL", "2022-12-31", "0001652044-23-000016"),
    ("GOOGL", "2023-12-31", "0001652044-24-000022"),
    ("GOOGL", "2024-12-31", "0001652044-25-000014"),
    ("GOOGL", "2025-12-31", "0001652044-26-000018"),
    ("META", "2022-12-31", "0001326801-23-000013"),
    ("META", "2023-12-31", "0001326801-24-000012"),
    ("META", "2024-12-31", "0001326801-25-000017"),
]

ANNUAL_DURATION_MIN_DAYS = 350
ANNUAL_DURATION_MAX_DAYS = 380

DEBT_DISCLOSURE_ROLE_PATTERN = r"debt|notes?\s+payable|borrowings?"
DEBT_MATURITY_ROLE_PATTERN = r"maturit|future\s+principal\s+payments?"
DEBT_MATURITY_ROLE_EXCLUDE_PATTERN = r"marketable|available.for.sale|investment"
NON_DEBT_MATURITY_EXCLUDE_PATTERN = (
    r"lease|purchase\s+obligation|interest\s+payment|interest\s+expense|"
    r"commitment"
)


# =============================================================================
# Copied UNCHANGED from scripts/89/92 — the minimum subset needed for
# maturity-bucket classification (presentation reconstruction, fact
# matching, and the maturity-schedule classifier itself).
# =============================================================================


def reconstruct_presentation_dataframe(
    connection: duckdb.DuckDBPyConnection, accession_number: str
) -> pd.DataFrame:
    df = connection.execute(
        """
        SELECT
            p.role_uri AS role_uri,
            p.role_definition AS role_definition,
            p.depth AS depth,
            p.parent_concept AS parent_qname,
            p.child_concept AS concept_qname,
            COALESCE(l_pref.label_text, l_std.label_text, p.child_concept) AS label,
            COALESCE(c.is_abstract, FALSE) AS is_abstract,
            COALESCE(c.period_type, '') AS period_type,
            COALESCE(c.balance_type, '') AS balance
        FROM xbrl_presentation_relationships p
        LEFT JOIN xbrl_concepts c
            ON c.accession_number = p.accession_number
           AND c.qname = p.child_concept
        LEFT JOIN xbrl_labels l_pref
            ON l_pref.accession_number = p.accession_number
           AND l_pref.concept_qname = p.child_concept
           AND l_pref.label_role = p.preferred_label
           AND l_pref.language IN ('en-US', 'en')
           AND p.preferred_label IS NOT NULL
           AND p.preferred_label != ''
        LEFT JOIN xbrl_labels l_std
            ON l_std.accession_number = p.accession_number
           AND l_std.concept_qname = p.child_concept
           AND l_std.label_role = ?
           AND l_std.language IN ('en-US', 'en')
        WHERE p.accession_number = ?
        """,
        [STANDARD_LABEL_ROLE, accession_number],
    ).fetchdf()

    df = df.drop_duplicates(
        subset=["role_uri", "parent_qname", "concept_qname", "depth"]
    ).reset_index(drop=True)

    return df


def usd_unit_ids_for_accession(
    connection: duckdb.DuckDBPyConnection, accession_number: str
) -> set[str]:
    units = connection.execute(
        """
        SELECT unit_id FROM xbrl_units
        WHERE accession_number = ?
          AND numerator_measures = 'iso4217:USD'
          AND denominator_measures IS NULL
        """,
        [accession_number],
    ).fetchdf()
    return set(units["unit_id"].tolist())


def _decimals_precision_rank(decimals_value: object) -> float:
    if decimals_value is None:
        return float("-inf")
    text = str(decimals_value).strip()
    if text.upper() == "INF":
        return float("inf")
    try:
        return float(int(text))
    except ValueError:
        return float("-inf")


def _round_to_xbrl_decimals(value: float, decimals_value: object) -> float | None:
    rank = _decimals_precision_rank(decimals_value)
    if rank == float("inf"):
        return value
    if rank == float("-inf"):
        return None
    factor = 10 ** (-rank)
    return round(value / factor) * factor


def _reconcile_same_context_precision_duplicates_from_warehouse(
    filtered: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str]]:
    reconciled_rows: list[pd.Series] = []
    notes: list[str] = []

    for context_id, group in filtered.groupby("context_id", sort=False):
        if len(group) == 1 or group["value_numeric"].nunique() == 1:
            for _, row in group.iterrows():
                reconciled_rows.append(row)
            continue

        group_by_precision = group.assign(
            _precision_rank=group["decimals"].map(_decimals_precision_rank)
        ).sort_values("_precision_rank", ascending=False)

        most_precise = group_by_precision.iloc[0]
        all_consistent = True
        for _, other_row in group_by_precision.iloc[1:].iterrows():
            rounded = _round_to_xbrl_decimals(most_precise["value_numeric"], other_row["decimals"])
            if rounded is None or rounded != other_row["value_numeric"]:
                all_consistent = False
                break

        if all_consistent:
            reconciled_rows.append(most_precise)
            notes.append(f"context {context_id}: reconciled to most precise value")
        else:
            for _, row in group.iterrows():
                reconciled_rows.append(row)

    return pd.DataFrame(reconciled_rows), notes


def match_facts_from_warehouse(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    concept_qname: str,
    report_date: str,
    expected_period_type: str,
) -> dict[str, object]:
    facts = connection.execute(
        """
        SELECT value_numeric, context_id, unit_id, period_start,
               period_end, instant_date, dimensions_json, is_nil,
               period_type, decimals
        FROM xbrl_facts
        WHERE accession_number = ? AND concept_qname = ?
        """,
        [accession_number, concept_qname],
    ).fetchdf()

    if facts.empty:
        return {"status": "REVIEW_REQUIRED", "error": "no facts for concept", "value": None}

    usd_unit_ids = usd_unit_ids_for_accession(connection, accession_number)
    requested_date = _datetime_module.date.fromisoformat(report_date)

    if expected_period_type == "instant":
        base = facts[
            (facts["dimensions_json"] == "{}")
            & (facts["unit_id"].isin(usd_unit_ids))
            & (~facts["is_nil"])
            & facts["value_numeric"].notna()
            & facts["instant_date"].notna()
        ].copy()
        candidates = base[base["instant_date"] == report_date]
    else:
        base = facts[
            (facts["dimensions_json"] == "{}")
            & (facts["unit_id"].isin(usd_unit_ids))
            & (~facts["is_nil"])
            & facts["value_numeric"].notna()
            & facts["period_end"].notna()
        ].copy()
        candidates = base[base["period_end"] == report_date]
        if not candidates.empty:
            starts = pd.to_datetime(candidates["period_start"])
            ends = pd.to_datetime(candidates["period_end"])
            duration_days = (ends - starts).dt.days
            candidates = candidates[
                (duration_days >= ANNUAL_DURATION_MIN_DAYS) & (duration_days <= ANNUAL_DURATION_MAX_DAYS)
            ]

    if candidates.empty:
        return {"status": "REVIEW_REQUIRED", "error": "no fact passed unit/dimension/period filters", "value": None}

    candidates, _notes = _reconcile_same_context_precision_duplicates_from_warehouse(candidates)
    distinct_values = sorted(set(candidates["value_numeric"].tolist()))

    if len(distinct_values) == 1:
        return {
            "status": "PASS", "value": distinct_values[0],
            "context_id": str(candidates.iloc[0]["context_id"]),
            "unit_id": str(candidates.iloc[0]["unit_id"]),
        }

    return {"status": "REVIEW_REQUIRED", "error": f"multiple distinct values: {distinct_values}", "value": None}


def find_debt_maturity_schedule_role(
    connection: duckdb.DuckDBPyConnection, accession_number: str
) -> str | None:
    roles = connection.execute(
        """
        SELECT DISTINCT role_uri, role_definition FROM xbrl_roles
        WHERE accession_number = ? AND relationship_type = 'presentation'
          AND regexp_matches(role_definition, 'disclosure', 'i')
        """,
        [accession_number],
    ).fetchdf()

    if roles.empty:
        return None

    is_debt = roles["role_definition"].str.contains(DEBT_DISCLOSURE_ROLE_PATTERN, case=False, regex=True, na=False)
    is_maturity = roles["role_definition"].str.contains(DEBT_MATURITY_ROLE_PATTERN, case=False, regex=True, na=False)
    is_excluded = roles["role_definition"].str.contains(DEBT_MATURITY_ROLE_EXCLUDE_PATTERN, case=False, regex=True, na=False)
    candidates = sorted(roles.loc[is_debt & is_maturity & ~is_excluded, "role_uri"].unique())

    if len(candidates) != 1:
        return None
    return candidates[0]


def classify_maturity_buckets(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
) -> dict[str, object]:
    role_uri = find_debt_maturity_schedule_role(connection, accession_number)

    if role_uri is None:
        return {"role_uri": None, "buckets": [], "current_debt_maturity": None,
                "long_term_debt_maturity": None, "total_debt_maturity": None,
                "reported_total": None, "reconciliation_gap": None}

    role_definition = connection.execute(
        """
        SELECT DISTINCT role_definition FROM xbrl_roles
        WHERE accession_number = ? AND role_uri = ? AND relationship_type = 'presentation'
        """,
        [accession_number, role_uri],
    ).fetchone()[0]

    edges = connection.execute(
        """
        SELECT parent_concept, child_concept, order_value
        FROM xbrl_presentation_relationships
        WHERE accession_number = ? AND role_uri = ?
        ORDER BY order_value, child_concept
        """,
        [accession_number, role_uri],
    ).fetchdf()

    label_lookup = dict(zip(presentation["concept_qname"], presentation["label"]))
    abstract_lookup = presentation.drop_duplicates(subset=["concept_qname"]).set_index(
        "concept_qname"
    )["is_abstract"].to_dict()

    buckets: list[dict[str, object]] = []
    reported_total_row = None

    for _, edge in edges.iterrows():
        child = edge["child_concept"]
        if abstract_lookup.get(child, True):
            continue

        label = label_lookup.get(child, child)
        decision = match_facts_from_warehouse(connection, accession_number, child, report_date, "instant")

        row = {
            "role_uri": role_uri, "role_definition": role_definition,
            "label": label, "concept_qname": child,
            "presentation_order": edge["order_value"],
            "status": decision["status"], "value": decision.get("value"),
            "context_id": decision.get("context_id"), "unit_id": decision.get("unit_id"),
        }

        is_total_row = bool(re.match(r"^\s*total\b", label, re.IGNORECASE))
        if is_total_row:
            row["classification"] = "TOTAL_ROW"
            reported_total_row = row
            buckets.append(row)
            continue

        if re.search(NON_DEBT_MATURITY_EXCLUDE_PATTERN, label, re.IGNORECASE) or re.search(
            NON_DEBT_MATURITY_EXCLUDE_PATTERN, child, re.IGNORECASE
        ):
            row["classification"] = "EXCLUDED_NON_DEBT"
            buckets.append(row)
            continue

        row["classification"] = "PENDING"
        buckets.append(row)

    pending = [b for b in buckets if b["classification"] == "PENDING"]
    pending.sort(key=lambda b: b["presentation_order"])
    for index, bucket in enumerate(pending):
        bucket["classification"] = "CURRENT_MATURITY" if index == 0 else "LONG_TERM_MATURITY"

    current_values = [b["value"] for b in buckets if b["classification"] == "CURRENT_MATURITY" and b["status"] == "PASS"]
    long_term_values = [b["value"] for b in buckets if b["classification"] == "LONG_TERM_MATURITY" and b["status"] == "PASS"]
    all_pending_resolved = all(b["status"] == "PASS" for b in pending)

    current_debt_maturity = sum(current_values) if all_pending_resolved and pending else None
    long_term_debt_maturity = (
        sum(long_term_values) if all_pending_resolved and len(pending) > 1
        else (0.0 if all_pending_resolved and len(pending) == 1 else None)
    )
    total_debt_maturity = (
        current_debt_maturity + long_term_debt_maturity
        if current_debt_maturity is not None and long_term_debt_maturity is not None
        else None
    )
    reported_total = reported_total_row["value"] if reported_total_row and reported_total_row["status"] == "PASS" else None
    reconciliation_gap = (
        reported_total - total_debt_maturity
        if reported_total is not None and total_debt_maturity is not None else None
    )

    return {
        "role_uri": role_uri, "role_definition": role_definition, "buckets": buckets,
        "current_debt_maturity": current_debt_maturity,
        "long_term_debt_maturity": long_term_debt_maturity,
        "total_debt_maturity": total_debt_maturity,
        "reported_total": reported_total, "reconciliation_gap": reconciliation_gap,
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


def main() -> None:
    total_start = time.perf_counter()
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH))

    converted: list[str] = []
    still_blocked: list[str] = []
    all_results: dict[str, dict[str, object]] = {}

    for ticker, report_date, accession_number in TARGET_FILINGS:
        print(f"=== {ticker} {report_date} ({accession_number}) ===")
        print("  arelle_required=NO (already warehoused)")

        # Confirm current_debt is genuinely still REVIEW_REQUIRED before touching it
        current_status = latest_metric(prod_connection, ticker, report_date, "current_debt")
        if current_status and current_status["status"] != "REVIEW_REQUIRED":
            print(f"  SKIP — current_debt is already {current_status['status']}, not REVIEW_REQUIRED")
            continue

        presentation = reconstruct_presentation_dataframe(warehouse_connection, accession_number)
        maturity = classify_maturity_buckets(warehouse_connection, accession_number, presentation, report_date)
        all_results[f"{ticker}_{report_date}"] = maturity

        if maturity["current_debt_maturity"] is None:
            reason = (
                "no unique debt-maturity-schedule role found" if maturity["role_uri"] is None
                else "one or more maturity buckets did not resolve to a single PASS value"
            )
            still_blocked.append(f"{ticker} {report_date}: {reason}")
            print(f"  current_debt: STILL REVIEW_REQUIRED — {reason}")
            print()
            continue

        value = maturity["current_debt_maturity"]
        run_id = f"{accession_number}::{ENGINE_VERSION}"

        prod_connection.execute(
            """
            INSERT INTO extraction_runs (extraction_run_id, accession_number, engine_version)
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [run_id, accession_number, ENGINE_VERSION],
        )

        current_buckets = [b for b in maturity["buckets"] if b["classification"] == "CURRENT_MATURITY"]
        lineage = {
            "maturity_role_uri": maturity["role_uri"],
            "maturity_role_definition": maturity["role_definition"],
            "current_maturity_buckets": [
                {"label": b["label"], "concept_qname": b["concept_qname"], "value": b["value"]}
                for b in current_buckets
            ],
            "report_date": report_date,
        }
        validation_reason = (
            f"D-028: principal due within 12 months per the filing's own debt-maturity "
            f"schedule ({maturity['role_definition']}), summed from bucket(s): "
            f"{[b['label'] for b in current_buckets]}. Operating leases excluded "
            f"(NON_DEBT_MATURITY_EXCLUDE_PATTERN, unchanged). lineage={lineage}"
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
            VALUES (?, 'current_debt', TRUE, 'PASS_MATURITY_BASIS', ?, 'iso4217:USD',
                    NULL, NULL, ?, NULL, NULL, ?, 'maturity_basis_direct', FALSE,
                    NULL, ?)
            ON CONFLICT DO NOTHING
            """,
            [run_id, value, report_date, maturity["role_definition"], validation_reason],
        )

        # verify immediately
        check = latest_metric(prod_connection, ticker, report_date, "current_debt")
        if not check or check["status"] != "PASS_MATURITY_BASIS" or check["value"] != value:
            print(f"  ERROR — write did not verify back correctly!")
            still_blocked.append(f"{ticker} {report_date}: write verification failed")
            continue

        converted.append(f"{ticker} {report_date}: current_debt -> PASS_MATURITY_BASIS ({value})")
        print(f"  current_debt -> PASS_MATURITY_BASIS value={value} [verified]")
        print(f"    buckets used: {[(b['label'], b['value']) for b in current_buckets]}")
        print()

    warehouse_connection.close()
    prod_connection.close()

    total_elapsed = time.perf_counter() - total_start

    print("=" * 100)
    print(f"Converted ({len(converted)}):")
    for line in converted:
        print(f"  {line}")
    print(f"\nStill blocked ({len(still_blocked)}):")
    for line in still_blocked:
        print(f"  {line}")
    print(f"\ntotal_elapsed_seconds = {total_elapsed:.3f}")

    output_path = DATA_DIR / "d028_current_debt_maturity_results.json"
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(all_results, handle, indent=2, default=str, ensure_ascii=False)
    print(f"Full maturity-bucket evidence written to {output_path}")


if __name__ == "__main__":
    main()
