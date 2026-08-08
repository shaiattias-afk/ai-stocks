"""
policies/zero_inference.py -- D-017's 4-condition current_debt=0
structural proof (conditions 2 and 3; conditions 1 and 4 are the
preconditions already enforced by debt_current_long_term.py's tiers
1-3 finding zero explicit components, and the absence of any
contradicting fact respectively -- see D-017 in docs/DECISIONS_LOG.md
for the full 4-condition statement).

Ported byte-exact from scripts/92_groups_1_3_4_debt_facility_aggregate_
policy.py (logic-identical to scripts/79/82/84/87/89's own copies).
"""

from __future__ import annotations

import re

import duckdb
import pandas as pd

from stock_agent.extraction.core import TargetRowNotFound, match_facts_from_warehouse
from stock_agent.policies.debt_current_long_term import find_debt_maturity_schedule_role

def verify_condition_2_from_warehouse(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
) -> tuple[bool, str, dict[str, object]]:
    role_uri = find_debt_maturity_schedule_role(connection, accession_number)

    if role_uri is None:
        return False, "no unique debt maturity schedule role found", {}

    role_rows = presentation[
        (presentation["role_uri"] == role_uri) & (~presentation["is_abstract"].astype(bool))
    ]
    is_total_row = role_rows["label"].str.match(r"^\s*total\b", case=False, na=False)
    non_total_rows = role_rows[~is_total_row]

    if non_total_rows.empty:
        return False, "maturity schedule has no non-total rows", {"maturity_role_uri": role_uri}

    earliest_row = non_total_rows.iloc[0]
    decision = match_facts_from_warehouse(
        connection, accession_number, earliest_row["concept_qname"], report_date, "instant"
    )

    if decision["status"] != "PASS":
        return False, "earliest bucket value not reliably resolvable", {
            "maturity_role_uri": role_uri, "earliest_bucket_label": earliest_row["label"],
        }

    value = decision["value"]
    evidence = {
        "maturity_role_uri": role_uri,
        "earliest_bucket_label": earliest_row["label"],
        "earliest_bucket_concept": earliest_row["concept_qname"],
        "earliest_bucket_value": value,
    }

    if value == 0:
        return True, "", evidence

    return False, f"earliest bucket is nonzero ({value})", evidence


def verify_condition_3_from_warehouse(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
    maturity_role_uri: str,
    long_term_debt_row: dict[str, str] | None,
) -> tuple[bool, str, dict[str, object]]:
    if long_term_debt_row is None:
        return False, "long_term_debt not resolved from the balance sheet", {}

    role_rows = presentation[presentation["role_uri"] == maturity_role_uri]
    is_total_row = role_rows["label"].str.match(r"^\s*total\b", case=False, na=False)
    total_rows = role_rows[is_total_row]

    if len(total_rows) != 1:
        return False, "no unique 'Total' row in the maturity schedule", {}

    total_row = total_rows.iloc[0]
    total_decision = match_facts_from_warehouse(
        connection, accession_number, total_row["concept_qname"], report_date, "instant"
    )

    if total_decision["status"] != "PASS":
        return False, "maturity schedule Total not reliably resolvable", {}

    ltd_decision = match_facts_from_warehouse(
        connection, accession_number, long_term_debt_row["concept_qname"], report_date, "instant"
    )

    if ltd_decision["status"] != "PASS":
        return False, "long_term_debt not reliably resolvable for comparison", {}

    total_value = total_decision["value"]
    ltd_value = ltd_decision["value"]
    evidence = {"maturity_schedule_total": total_value, "long_term_debt": ltd_value}

    if abs(total_value - ltd_value) > 1:
        return False, (
            f"maturity schedule total ({total_value}) does not reconcile with "
            f"long_term_debt ({ltd_value})"
        ), evidence

    return True, "", evidence


def attempt_current_debt_zero_inference_from_warehouse(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
    long_term_debt_row: dict[str, str] | None,
) -> dict[str, object]:
    condition_2_ok, condition_2_detail, condition_2_evidence = (
        verify_condition_2_from_warehouse(
            connection, accession_number, presentation, report_date
        )
    )

    if not condition_2_ok:
        raise TargetRowNotFound(f"condition 2 not proven: {condition_2_detail}")

    condition_3_ok, condition_3_detail, condition_3_evidence = (
        verify_condition_3_from_warehouse(
            connection, accession_number, presentation, report_date,
            condition_2_evidence["maturity_role_uri"], long_term_debt_row,
        )
    )

    if not condition_3_ok:
        raise TargetRowNotFound(f"condition 3 not proven: {condition_3_detail}")

    return {
        "concept_qname": "inferred_zero",
        "label": "Inferred zero current debt (D-017)",
        "selection_tier": "zero_inference_proven",
        "value": 0.0,
    }

