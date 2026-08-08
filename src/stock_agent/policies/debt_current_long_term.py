"""
policies/debt_current_long_term.py -- current_debt / long_term_debt
resolution: D-016 (sum-of-explicit-components), D-017 (4-condition zero
inference precondition helpers live in policies/zero_inference.py),
D-019 (presentation-ancestry classification for unlabeled current/
non-current rows), D-022/D-027-Policy-A/D-028 (debt-maturity-schedule
classification and, as a final fallback tier, using the maturity
schedule's own earliest bucket directly as current_debt).

Ported byte-exact from scripts/92_groups_1_3_4_debt_facility_aggregate_
policy.py (which is a confirmed logic-identical descendant of scripts/
79, 82, 84, 87, 89 for every function below). resolve_current_debt_with_
facility_policy's REVIEW_REQUIRED path additionally gains the D-028
maturity-principal-as-current_debt tier (scripts/96,
current_debt_maturity_basis_policy) as its final fallback -- ported
separately below as resolve_current_debt_maturity_basis_fallback,
composed in metrics/annual.py exactly where scripts/96 applied it
(only when every earlier tier, including D-017 zero-inference and the
Policy B/structural-absence zero tiers, has already failed to resolve
current_debt).
"""

from __future__ import annotations

import re

import duckdb
import pandas as pd

from stock_agent.extraction.core import (
    BUILT_IN_METRICS,
    TargetRowNotFound,
    identify_canonical_row,
    match_facts_from_warehouse,
    reconstruct_calculation_children_by_parent,
)

CURRENT_DEBT_NEVER_ALLOWED_PATTERN = (
    r"accounts\s+payable|accrued|operating\s+lease|"
    r"unpaid|incurred\s+but\s+not\s+yet\s+paid"
)


CURRENT_DEBT_EXPLICIT_TOTAL_PLAIN = (
    r"^\s*total\s+(?:current\s+)?(?:short-?term\s+)?"
    r"(?:borrowings|debt)\s*$"
)


DEBT_DISCLOSURE_ROLE_PATTERN = r"debt|notes?\s+payable|borrowings?"


DEBT_MATURITY_ROLE_PATTERN = r"maturit|future\s+principal\s+payments?"


DEBT_MATURITY_ROLE_EXCLUDE_PATTERN = r"marketable|available.for.sale|investment"


DEBT_LABEL_VOCABULARY_PATTERN = (
    r"short-?term\s+debt|commercial\s+paper|"
    r"notes?\s+payable|current\s+debt|"
    r"current\s+portion\s+of\s+(?:long-?term\s+)?debt|"
    r"current\s+maturities\s+of\s+long-?term\s+debt|"
    r"long-?term\s+debt|"
    r"convertible\s+(?:senior\s+)?notes?|senior\s+notes?|"
    r"term\s+loan|borrowings?"
)


DEBT_LABEL_EXCLUSION_PATTERN = (
    r"accounts\s+payable|accrued|operating\s+lease|finance\s+lease|"
    r"unpaid|incurred\s+but\s+not\s+yet\s+paid|non-?current|"
    r"equity\s+component|conversion\s+option|"
    r"derivative\s+liabilit|embedded\s+derivative|"
    r"unamortized\s+discount$"
)


CURRENT_LIABILITIES_ANCESTOR_CONCEPT_PATTERN = r"LiabilitiesCurrent"


CURRENT_LIABILITIES_ANCESTOR_LABEL_PATTERN = r"current\s+liabilit"


LIABILITIES_SECTION_ANCESTOR_CONCEPT_PATTERN = r"Liabilities"


LIABILITIES_SECTION_ANCESTOR_LABEL_PATTERN = r"liabilit"


def find_current_debt_explicit_total(
    presentation: pd.DataFrame,
) -> dict[str, str] | None:
    metric = BUILT_IN_METRICS["current_debt"]
    is_statement_role = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-", na=False
    )
    is_role_include = presentation["role_definition"].str.contains(
        metric.role_include_pattern, case=False, regex=True, na=False
    )
    is_role_exclude = presentation["role_definition"].str.contains(
        metric.role_exclude_pattern, case=False, regex=True, na=False
    )
    is_target_role = is_statement_role & is_role_include & ~is_role_exclude
    is_not_abstract = ~presentation["is_abstract"].astype(bool)
    is_total_label = presentation["label"].str.match(
        CURRENT_DEBT_EXPLICIT_TOTAL_PLAIN, case=False, na=False
    )

    candidates = presentation[is_target_role & is_not_abstract & is_total_label]

    if len(candidates) != 1:
        return None

    row = candidates.iloc[0]
    return {
        "role_uri": str(row["role_uri"]),
        "role_definition": str(row["role_definition"]),
        "concept_qname": str(row["concept_qname"]),
        "label": str(row["label"]),
        "period_type": str(row["period_type"]),
        "selection_tier": "explicit_total",
    }


def find_current_debt_sibling_components(
    presentation: pd.DataFrame,
) -> list[dict[str, str]] | None | str:
    metric = BUILT_IN_METRICS["current_debt"]
    is_statement_role = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-", na=False
    )
    is_role_include = presentation["role_definition"].str.contains(
        metric.role_include_pattern, case=False, regex=True, na=False
    )
    is_role_exclude = presentation["role_definition"].str.contains(
        metric.role_exclude_pattern, case=False, regex=True, na=False
    )
    is_target_role = is_statement_role & is_role_include & ~is_role_exclude
    is_not_abstract = ~presentation["is_abstract"].astype(bool)
    mentions = presentation["label"].str.contains(
        metric.mention_pattern, case=False, regex=True, na=False
    )
    excluded = presentation["label"].str.contains(
        metric.exclude_label_pattern, case=False, regex=True, na=False
    )
    is_plain = presentation["label"].str.match(
        metric.plain_pattern, case=False, na=False
    )

    candidates = presentation[
        is_target_role & is_not_abstract & mentions & ~excluded & is_plain
    ].drop_duplicates(subset=["concept_qname"])

    if candidates.empty:
        return None

    if candidates["parent_qname"].nunique() != 1:
        return "AMBIGUOUS"

    return [
        {
            "role_uri": str(row["role_uri"]),
            "role_definition": str(row["role_definition"]),
            "concept_qname": str(row["concept_qname"]),
            "label": str(row["label"]),
            "period_type": str(row["period_type"]),
            "selection_tier": "sum_of_sibling_components",
        }
        for _, row in candidates.iterrows()
    ]


def find_debt_vocabulary_rows(
    presentation: pd.DataFrame, role_uri: str, already_claimed_concepts: set[str]
) -> pd.DataFrame:
    role_rows = presentation[presentation["role_uri"] == role_uri]
    is_not_abstract = ~role_rows["is_abstract"].astype(bool)
    mentions = role_rows["label"].str.contains(
        DEBT_LABEL_VOCABULARY_PATTERN, case=False, regex=True, na=False
    )
    excluded = role_rows["label"].str.contains(
        DEBT_LABEL_EXCLUSION_PATTERN, case=False, regex=True, na=False
    )
    not_claimed = ~role_rows["concept_qname"].isin(already_claimed_concepts)

    return role_rows[is_not_abstract & mentions & ~excluded & not_claimed].drop_duplicates(
        subset=["concept_qname"]
    )


def classify_current_or_noncurrent_by_ancestry(
    ancestor_chain: list[dict[str, str]],
) -> tuple[str | None, str]:
    for ancestor in ancestor_chain:
        if re.search(
            CURRENT_LIABILITIES_ANCESTOR_CONCEPT_PATTERN, ancestor["concept_qname"]
        ) or re.search(
            CURRENT_LIABILITIES_ANCESTOR_LABEL_PATTERN, ancestor["label"], re.IGNORECASE
        ):
            return "current", f"ancestor '{ancestor['label']}' identifies current liabilities"

    for ancestor in ancestor_chain:
        if re.search(
            LIABILITIES_SECTION_ANCESTOR_CONCEPT_PATTERN, ancestor["concept_qname"]
        ) or re.search(
            LIABILITIES_SECTION_ANCESTOR_LABEL_PATTERN, ancestor["label"], re.IGNORECASE
        ):
            return "noncurrent", f"ancestor chain reaches general liabilities ('{ancestor['label']}') without passing through current"

    return None, "ancestor chain does not reach an identified liabilities section"


def find_current_debt_calculation_components_from_warehouse(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
) -> list[dict[str, str]] | None:
    metric = BUILT_IN_METRICS["current_debt"]
    is_statement_role = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-", na=False
    )
    is_role_include = presentation["role_definition"].str.contains(
        metric.role_include_pattern, case=False, regex=True, na=False
    )
    is_role_exclude = presentation["role_definition"].str.contains(
        metric.role_exclude_pattern, case=False, regex=True, na=False
    )
    balance_sheet_role_uris = sorted(
        presentation.loc[
            is_statement_role & is_role_include & ~is_role_exclude, "role_uri"
        ].unique()
    )

    label_by_concept = dict(zip(presentation["concept_qname"], presentation["label"]))

    for role_uri in balance_sheet_role_uris:
        children_by_parent = reconstruct_calculation_children_by_parent(
            connection, accession_number, role_uri
        )

        for child_qnames in children_by_parent.values():
            unique_child_qnames = sorted(set(child_qnames))

            if len(unique_child_qnames) < 2:
                continue

            child_labels = [
                label_by_concept.get(qname, "") for qname in unique_child_qnames
            ]
            all_are_allowed_debt_components = all(
                re.search(metric.mention_pattern, label, re.IGNORECASE)
                and not re.search(
                    CURRENT_DEBT_NEVER_ALLOWED_PATTERN, label, re.IGNORECASE
                )
                for label in child_labels
            )

            if not all_are_allowed_debt_components:
                continue

            matching_rows = presentation[
                presentation["concept_qname"].isin(unique_child_qnames)
                & (presentation["role_uri"] == role_uri)
            ].drop_duplicates(subset=["concept_qname"])

            if len(matching_rows) == len(unique_child_qnames):
                return [
                    {
                        "role_uri": str(row["role_uri"]),
                        "role_definition": str(row["role_definition"]),
                        "concept_qname": str(row["concept_qname"]),
                        "label": str(row["label"]),
                        "period_type": str(row["period_type"]),
                        "selection_tier": "calculation_verified",
                    }
                    for _, row in matching_rows.iterrows()
                ]

    return None


def build_ancestor_chain_from_warehouse(
    presentation: pd.DataFrame, role_uri: str, concept_qname: str
) -> list[dict[str, str]]:
    role_rows = presentation[presentation["role_uri"] == role_uri]
    by_concept = {
        str(row["concept_qname"]): row
        for _, row in role_rows.drop_duplicates(
            subset=["concept_qname"], keep="first"
        ).iterrows()
    }

    chain: list[dict[str, str]] = []
    current_qname = concept_qname
    visited: set[str] = set()

    while True:
        row = by_concept.get(current_qname)
        if row is None:
            break
        parent_qname = str(row.get("parent_qname", "") or "")
        if not parent_qname or parent_qname in visited:
            break
        visited.add(parent_qname)
        parent_row = by_concept.get(parent_qname)
        if parent_row is None:
            break
        chain.append(
            {"concept_qname": parent_qname, "label": str(parent_row.get("label", ""))}
        )
        current_qname = parent_qname

    return chain


def resolve_debt_classification_by_ancestry_from_warehouse(
    presentation: pd.DataFrame, desired_classification: str, already_claimed: set[str]
) -> list[dict[str, object]]:
    metric = BUILT_IN_METRICS["current_debt"]
    is_statement_role = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-", na=False
    )
    is_role_include = presentation["role_definition"].str.contains(
        metric.role_include_pattern, case=False, regex=True, na=False
    )
    is_role_exclude = presentation["role_definition"].str.contains(
        metric.role_exclude_pattern, case=False, regex=True, na=False
    )
    balance_sheet_role_uris = sorted(
        presentation.loc[
            is_statement_role & is_role_include & ~is_role_exclude, "role_uri"
        ].unique()
    )

    matches: list[dict[str, object]] = []

    for role_uri in balance_sheet_role_uris:
        candidates = find_debt_vocabulary_rows(presentation, role_uri, already_claimed)

        for _, row in candidates.iterrows():
            concept_qname = str(row["concept_qname"])
            ancestor_chain = build_ancestor_chain_from_warehouse(
                presentation, role_uri, concept_qname
            )
            classification, _reason = classify_current_or_noncurrent_by_ancestry(
                ancestor_chain
            )

            if classification == desired_classification:
                matches.append(
                    {
                        "role_uri": role_uri,
                        "role_definition": str(row["role_definition"]),
                        "concept_qname": concept_qname,
                        "label": str(row["label"]),
                        "period_type": str(row["period_type"]),
                        "selection_tier": "ancestry_classified",
                    }
                )

    return matches


def resolve_current_debt_components_from_warehouse(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
) -> tuple[str, list[dict[str, str]]]:
    explicit_total = find_current_debt_explicit_total(presentation)
    if explicit_total is not None:
        return "components", [explicit_total]

    calculation_components = find_current_debt_calculation_components_from_warehouse(
        connection, accession_number, presentation
    )
    if calculation_components is not None:
        return "components", calculation_components

    sibling_components = find_current_debt_sibling_components(presentation)
    if sibling_components == "AMBIGUOUS":
        raise TargetRowNotFound(
            "multiple current-debt candidates found without a shared parent — cannot prove non-overlap"
        )
    if sibling_components is not None:
        return "components", sibling_components

    ancestry_matches = resolve_debt_classification_by_ancestry_from_warehouse(
        presentation, "current", set()
    )
    if len(ancestry_matches) == 1:
        return "components", ancestry_matches
    if len(ancestry_matches) > 1:
        raise TargetRowNotFound(
            "multiple ancestry-classified current-debt candidates — cannot choose unambiguously"
        )

    return "zero_inference_needed", []


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

    is_debt = roles["role_definition"].str.contains(
        DEBT_DISCLOSURE_ROLE_PATTERN, case=False, regex=True, na=False
    )
    is_maturity = roles["role_definition"].str.contains(
        DEBT_MATURITY_ROLE_PATTERN, case=False, regex=True, na=False
    )
    is_excluded = roles["role_definition"].str.contains(
        DEBT_MATURITY_ROLE_EXCLUDE_PATTERN, case=False, regex=True, na=False
    )
    candidates = sorted(roles.loc[is_debt & is_maturity & ~is_excluded, "role_uri"].unique())

    if len(candidates) != 1:
        return None

    return candidates[0]


NON_DEBT_MATURITY_EXCLUDE_PATTERN = (
    r"lease|purchase\s+obligation|interest\s+payment|interest\s+expense|"
    r"commitment"
)


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
        WHERE accession_number = ? AND role_uri = ?
          AND relationship_type = 'presentation'
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
        decision = match_facts_from_warehouse(
            connection, accession_number, child, report_date, "instant"
        )

        row = {
            "role_uri": role_uri,
            "role_definition": role_definition,
            "label": label,
            "concept_qname": child,
            "presentation_order": edge["order_value"],
            "status": decision["status"],
            "value": decision.get("value"),
            "context_id": decision.get("context_id"),
            "unit_id": decision.get("unit_id"),
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

    current_values = [
        b["value"] for b in buckets
        if b["classification"] == "CURRENT_MATURITY" and b["status"] == "PASS"
    ]
    long_term_values = [
        b["value"] for b in buckets
        if b["classification"] == "LONG_TERM_MATURITY" and b["status"] == "PASS"
    ]
    all_pending_resolved = all(b["status"] == "PASS" for b in pending)

    current_debt_maturity = sum(current_values) if all_pending_resolved and pending else None
    long_term_debt_maturity = (
        sum(long_term_values) if all_pending_resolved and len(pending) > 1 else (
            0.0 if all_pending_resolved and len(pending) == 1 else None
        )
    )
    total_debt_maturity = (
        current_debt_maturity + long_term_debt_maturity
        if current_debt_maturity is not None and long_term_debt_maturity is not None
        else None
    )

    reported_total = (
        reported_total_row["value"]
        if reported_total_row and reported_total_row["status"] == "PASS"
        else None
    )
    reconciliation_gap = (
        reported_total - total_debt_maturity
        if reported_total is not None and total_debt_maturity is not None
        else None
    )

    return {
        "role_uri": role_uri,
        "role_definition": role_definition,
        "buckets": buckets,
        "current_debt_maturity": current_debt_maturity,
        "long_term_debt_maturity": long_term_debt_maturity,
        "total_debt_maturity": total_debt_maturity,
        "reported_total": reported_total,
        "reconciliation_gap": reconciliation_gap,
    }


def _balance_sheet_role_uris(presentation: pd.DataFrame) -> list[str]:
    metric_cd = BUILT_IN_METRICS["current_debt"]
    is_statement = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-", na=False
    )
    is_role_include = presentation["role_definition"].str.contains(
        metric_cd.role_include_pattern, case=False, regex=True, na=False
    )
    is_role_exclude = presentation["role_definition"].str.contains(
        metric_cd.role_exclude_pattern, case=False, regex=True, na=False
    )
    return sorted(
        presentation.loc[
            is_statement & is_role_include & ~is_role_exclude, "role_uri"
        ].unique()
    )


def _unclaimed_debt_vocabulary_candidates(
    presentation: pd.DataFrame, already_claimed: set[str]
) -> list[str]:
    candidates: list[str] = []
    for role_uri in _balance_sheet_role_uris(presentation):
        found = find_debt_vocabulary_rows(presentation, role_uri, already_claimed)
        candidates.extend(found["concept_qname"].tolist())
    return candidates



# --- composing resolvers (Policy B/D-017/D-019 combined) -------------------
# These two depend on policies.zero_inference and policies.debt_undrawn_revolver;
# imported here (after the tier-1..4 helpers above) to keep the import graph
# acyclic (zero_inference imports find_debt_maturity_schedule_role from this
# module's first half).

from stock_agent.policies.debt_undrawn_revolver import find_undrawn_revolver_evidence
from stock_agent.policies.zero_inference import attempt_current_debt_zero_inference_from_warehouse

def resolve_current_debt_with_facility_policy(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
    long_term_debt_result: dict[str, object],
) -> dict[str, object]:
    """
    Extends D-021/D-026's current_debt resolution with the two newly-
    approved zero tiers, tried ONLY when every existing tier (explicit
    total, calculation components, sibling components, ancestry) has
    already found nothing (mode == "zero_inference_needed" — i.e.
    zero ancestry-classified CURRENT candidates already confirmed):
      1. (unchanged D-017) filing's own maturity schedule proves zero.
      2. (NEW, Policy B) an explicitly undrawn revolver + zero further
         unclaimed CURRENT-classified debt-vocabulary candidates.
      3. (NEW, Policy B's broader clause, symmetric with D-026's
         long_term_debt tier) zero unclaimed debt-vocabulary candidates
         ANYWHERE (current or noncurrent) — proves no financial debt at
         all exists, so current_debt is zero right alongside
         long_term_debt.
    Any candidate that can't be classified/proven still fails closed to
    REVIEW_REQUIRED — never a guess.
    """

    mode, rows = resolve_current_debt_components_from_warehouse(
        connection, accession_number, presentation
    )

    if mode == "components":
        return {"mode": "components", "rows": rows}

    long_term_debt_row = (
        {"concept_qname": long_term_debt_result["concept_qname"]}
        if long_term_debt_result.get("status") == "PASS"
        else None
    )

    try:
        inferred = attempt_current_debt_zero_inference_from_warehouse(
            connection, accession_number, presentation, report_date, long_term_debt_row
        )
        return {
            "mode": "zero",
            "value": inferred["value"],
            "concept_qname": inferred["concept_qname"],
            "selection_tier": inferred["selection_tier"],
            "basis": "MATURITY_SCHEDULE_ZERO_PROVEN",
            "lineage": {},
        }
    except TargetRowNotFound as exc:
        d017_error = str(exc)

    revolver_evidence = find_undrawn_revolver_evidence(
        connection, accession_number, report_date
    )
    unclaimed_current = resolve_debt_classification_by_ancestry_from_warehouse(
        presentation, "current", set()
    )

    if revolver_evidence is not None and len(unclaimed_current) == 0:
        return {
            "mode": "zero",
            "value": 0.0,
            "concept_qname": revolver_evidence["concept_qname"],
            "selection_tier": "zero_explicit_undrawn_facility",
            "basis": "ZERO_EXPLICIT_UNDRAWN_FACILITY",
            "lineage": {
                "revolver_evidence": revolver_evidence,
                "unclaimed_current_classified_candidates": 0,
                "d017_attempt_result": d017_error,
            },
        }

    unclaimed_anywhere = _unclaimed_debt_vocabulary_candidates(presentation, set())

    if len(unclaimed_anywhere) == 0:
        return {
            "mode": "zero",
            "value": 0.0,
            "concept_qname": "proven_zero_no_debt_found",
            "selection_tier": "zero_proven_no_debt_candidate",
            "basis": "ZERO_PROVEN_STRUCTURAL_ABSENCE",
            "lineage": {
                "unclaimed_debt_vocabulary_candidates_found": 0,
                "balance_sheet_roles_searched": _balance_sheet_role_uris(presentation),
                "d017_attempt_result": d017_error,
            },
        }

    return {
        "mode": "review_required",
        "error": (
            f"D-017 zero-inference failed ({d017_error}); no undrawn-revolver "
            "evidence available or a current-classified candidate still "
            f"exists unclaimed; {len(unclaimed_anywhere)} unclaimed debt-"
            f"vocabulary candidate(s) remain: {unclaimed_anywhere} — cannot "
            "prove zero"
        ),
    }


def resolve_long_term_debt(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
) -> dict[str, object]:
    """Unchanged from D-026 (scripts/89) — ancestry + structural-absence
    zero-proof tiers, current_debt's claimed set re-derived independently
    (order-independent, read-only)."""

    metric = BUILT_IN_METRICS["long_term_debt"]

    try:
        row, _candidates = identify_canonical_row(presentation, metric)
    except TargetRowNotFound:
        cd_mode, cd_rows = resolve_current_debt_components_from_warehouse(
            connection, accession_number, presentation
        )
        already_claimed = (
            {r["concept_qname"] for r in cd_rows} if cd_mode == "components" else set()
        )
        ancestry_matches = resolve_debt_classification_by_ancestry_from_warehouse(
            presentation, "noncurrent", already_claimed
        )

        if len(ancestry_matches) == 1:
            row = ancestry_matches[0]
        elif len(ancestry_matches) == 0:
            unclaimed_candidates = _unclaimed_debt_vocabulary_candidates(
                presentation, already_claimed
            )

            if unclaimed_candidates:
                return {
                    "status": "REVIEW_REQUIRED",
                    "error": (
                        "no unique long_term_debt row via label or ancestry, "
                        f"but {len(unclaimed_candidates)} unclaimed debt-"
                        f"vocabulary candidate(s) still exist unclassified: "
                        f"{unclaimed_candidates} — cannot prove zero"
                    ),
                    "value": None,
                }

            return {
                "status": "PASS",
                "value": 0.0,
                "concept_qname": "proven_zero_no_noncurrent_debt_found",
                "selection_tier": "zero_proven_no_long_term_debt_candidate",
                "basis": "ZERO_PROVEN_STRUCTURAL_ABSENCE",
                "lineage": {
                    "current_debt_components_claimed": sorted(already_claimed),
                    "ancestry_noncurrent_candidates_found": 0,
                    "unclaimed_debt_vocabulary_candidates_found": 0,
                    "balance_sheet_roles_searched": _balance_sheet_role_uris(presentation),
                },
            }
        else:
            return {
                "status": "REVIEW_REQUIRED",
                "error": (
                    "multiple ancestry-classified noncurrent candidates — "
                    "cannot choose unambiguously"
                ),
                "value": None,
            }

    decision = match_facts_from_warehouse(
        connection, accession_number, row["concept_qname"], report_date, "instant"
    )
    return {
        "status": decision["status"],
        "error": decision.get("error"),
        "value": decision.get("value"),
        "concept_qname": row["concept_qname"],
        "selection_tier": row["selection_tier"],
    }


# =============================================================================
# D-028 (scripts/96_current_debt_maturity_basis_policy.py) — final fallback
# tier for current_debt: when every earlier tier (explicit total,
# calculation components, sibling components, ancestry classification,
# D-017 zero-inference, Policy B undrawn-revolver/structural-absence zero)
# has already failed to resolve current_debt, the filing's OWN debt-
# maturity schedule's earliest bucket (principal contractually due within
# 12 months) may be used directly as current_debt with
# status=PASS_MATURITY_BASIS, basis=MATURITY_PRINCIPAL. Approved scope:
# AMZN (5 years), GOOGL (5 years), META (2022/2023/2024) — 13 company-years
# — CRWD and NVDA explicitly excluded by D-028. metrics/annual.py applies
# this tier generically (the mechanism itself carries no ticker-specific
# logic, per D-028's own text: "Supersedes D-021 rule 3 for current_debt
# specifically") but the verification step confirms it only ever fires
# for that same 13-company-year scope in practice, since the maturity
# schedule precondition genuinely does not resolve elsewhere.
# =============================================================================


def resolve_current_debt_maturity_basis_fallback(
    maturity: dict[str, object],
) -> dict[str, object] | None:
    """
    Mirrors scripts/96_current_debt_maturity_basis_policy.py's main()
    body exactly:

        if maturity["current_debt_maturity"] is None:
            # still REVIEW_REQUIRED
            ...
        value = maturity["current_debt_maturity"]
        # status = PASS_MATURITY_BASIS, basis = MATURITY_PRINCIPAL

    `maturity` is the dict returned by classify_maturity_buckets(...).
    Returns None when the maturity schedule does not resolve a current
    bucket either (callers should leave current_debt REVIEW_REQUIRED).
    """

    if maturity["current_debt_maturity"] is None:
        return None

    value = maturity["current_debt_maturity"]
    current_buckets = [
        b for b in maturity["buckets"] if b["classification"] == "CURRENT_MATURITY"
    ]

    return {
        "status": "PASS_MATURITY_BASIS",
        "value": value,
        "concept_qname": None,
        "selection_tier": "maturity_basis_direct",
        "basis": "MATURITY_PRINCIPAL",
        "lineage": {
            "maturity_role_uri": maturity["role_uri"],
            "maturity_role_definition": maturity["role_definition"],
            "current_maturity_buckets": [
                {"label": b["label"], "concept_qname": b["concept_qname"], "value": b["value"]}
                for b in current_buckets
            ],
        },
    }

