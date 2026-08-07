"""
D-027 (Groups 1/3/4) — applies the three newly-approved debt policies
directly against already-warehoused data (no Arelle, no reload):

  Policy A (debt maturity classification, extends D-021/D-022):
    current_debt = principal due <=12mo; long_term_debt = principal due
    >12mo; operating leases excluded; carrying value preferred;
    PASS_MATURITY_BASIS when only maturity-principal is available.

  Policy B (undrawn revolving credit, NEW): an explicitly undrawn
    revolver (its own "amount outstanding" XBRL fact = 0 at the exact
    report date) is zero debt for that facility. If, in addition, no
    OTHER debt-vocabulary candidate exists anywhere unclaimed, both
    current_debt and long_term_debt may be zero
    (ZERO_PROVEN_STRUCTURAL_ABSENCE, already used by D-026); if real
    long-term debt exists elsewhere (e.g. CRWD's term-loan-like
    LongTermDebtNoncurrent), only current_debt becomes zero
    (ZERO_EXPLICIT_UNDRAWN_FACILITY), long_term_debt is untouched.

  Policy C (direct aggregate, NEW): when a filing's own debt-maturity
    schedule reports an explicit "Total" row, that reported total is
    authoritative for total_debt (PASS_DIRECT_AGGREGATE) even if it
    does not reconcile exactly with the balance-sheet long_term_debt
    carrying value (e.g. face value vs. carrying value net of
    unamortized discount/issuance costs) — the gap is preserved in
    lineage, never silently discarded, and never blocks total_debt,
    adjusted_net_debt, invested_capital, or (eventually) roic.
    current_debt/long_term_debt individually may remain separately
    classified/REVIEW_REQUIRED — Policy C does not force them.

Scope (bounded, ticker lists used only to define WHICH filings to
process — no ticker-specific accounting rule anywhere below):
  Group 1: PANW 2025-07-31
  Group 3: CRWD 2022-01-31, 2023-01-31, 2024-01-31, 2025-01-31, 2026-01-31
  Group 4: META 2020-12-31, 2021-12-31, 2022-12-31, 2023-12-31,
           2024-12-31; NVDA 2020-01-26

All 11 filings are already in the XBRL warehouse (loaded by
scripts/91; META 2024-12-31 and NVDA 2024-01-28 were already warehoused
earlier). This script performs ZERO Arelle calls — DuckDB-only.

This script computes and writes current_debt, long_term_debt,
total_debt, adjusted_net_debt, invested_capital ONLY.
average_invested_capital/roic are computed by a later script (93) that
needs invested_capital already written for the prior fiscal year across
ALL groups (including Group 2, AMZN/GOOGL) first.

Writes new versioned extraction_run rows directly to the production
database (data/database/ai_stock_agent.duckdb) — idempotent via
ON CONFLICT DO NOTHING, never overwrites any prior row.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

STANDARD_LABEL_ROLE = "http://www.xbrl.org/2003/role/label"
ENGINE_VERSION = "v1-debt-facility-aggregate-policy (scripts/92, D-027)"

TARGET_FILINGS: list[tuple[str, str, str]] = [
    ("PANW", "2025-07-31", "0001327567-25-000027"),
    ("CRWD", "2022-01-31", "0001535527-22-000006"),
    ("CRWD", "2023-01-31", "0001535527-23-000008"),
    ("CRWD", "2024-01-31", "0001535527-24-000007"),
    ("CRWD", "2025-01-31", "0001535527-25-000009"),
    ("CRWD", "2026-01-31", "0001535527-26-000010"),
    ("META", "2020-12-31", "0001326801-21-000014"),
    ("META", "2021-12-31", "0001326801-22-000018"),
    ("META", "2022-12-31", "0001326801-23-000013"),
    ("META", "2023-12-31", "0001326801-24-000012"),
    ("META", "2024-12-31", "0001326801-25-000017"),
    ("NVDA", "2020-01-26", "0001045810-20-000010"),
]

WRITTEN_METRICS = [
    "current_debt", "long_term_debt", "total_debt", "adjusted_net_debt",
    "invested_capital",
]


# =============================================================================
# Copied UNCHANGED from scripts/89 (D-026) — pure string/DataFrame logic.
# =============================================================================


class TargetRowNotFound(Exception):
    pass


def _strip_parenthetical_asides(label: str) -> str:
    stripped = re.sub(r"\s*\([^)]*\)", " ", label)
    return re.sub(r"\s+", " ", stripped).strip()


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    role_include_pattern: str
    role_exclude_pattern: str | None
    mention_pattern: str
    exclude_label_pattern: str | None
    attributable_pattern: str | None
    plain_pattern: str


BUILT_IN_METRICS: dict[str, MetricDefinition] = {
    "cash_and_equivalents": MetricDefinition(
        "cash_and_equivalents", r"balance\s+sheets?|financial\s+position",
        r"parenthetical|reconciliation", r"cash\s+and\s+(?:cash\s+)?equivalents", None, None,
        r"^\s*cash\s+and\s+(?:cash\s+)?equivalents\s*$",
    ),
    "short_term_investments": MetricDefinition(
        "short_term_investments", r"balance\s+sheets?|financial\s+position",
        r"parenthetical", r"marketable\s+securities|short-?term\s+investments",
        None, None,
        r"^\s*(?:marketable\s+securities|short-?term\s+investments)\s*$",
    ),
    "current_debt": MetricDefinition(
        "current_debt", r"balance\s+sheets?|financial\s+position",
        r"parenthetical",
        r"short-?term\s+debt|commercial\s+paper|"
        r"notes?\s+payable.*current|current.*notes?\s+payable|"
        r"current\s+portion\s+of\s+(?:long-?term\s+)?debt|"
        r"current\s+maturities\s+of\s+long-?term\s+debt|"
        r"current\s+debt",
        r"non-?current", None,
        r"^\s*short-?term\s+debt\s*$"
        r"|^\s*commercial\s+paper\s*$"
        r"|^\s*notes?\s+payable(?:\s+and\s+other\s+borrowings)?,?\s*current\s*$"
        r"|^\s*current\s+portion\s+of\s+long-?term\s+debt\s*$"
        r"|^\s*current\s+maturities\s+of\s+long-?term\s+debt\s*$"
        r"|^\s*current\s+debt\s*$",
    ),
    "long_term_debt": MetricDefinition(
        "long_term_debt", r"balance\s+sheets?|financial\s+position",
        r"parenthetical",
        r"long-?term\s+debt|"
        r"notes?\s+payable.*non-?current|non-?current.*notes?\s+payable",
        r"current\s+portion|current\s+maturities", None,
        r"^\s*long-?term\s+debt\s*$"
        r"|^\s*notes?\s+payable(?:\s+and\s+other\s+borrowings)?,?\s*non-?current\s*$",
    ),
    "stockholders_equity": MetricDefinition(
        "stockholders_equity", r"balance\s+sheets?|financial\s+position",
        r"parenthetical",
        r"stockholders.?\s+equity|shareholders.?\s+equity|total\s+equity",
        None, None,
        r"^\s*total\s+(?:stockholders|shareholders).?\s+equity\s*$"
        r"|^\s*total\s+equity\s*$",
    ),
}

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

ANNUAL_DURATION_MIN_DAYS = 350
ANNUAL_DURATION_MAX_DAYS = 380


def identify_canonical_row(
    presentation: pd.DataFrame, metric: MetricDefinition
) -> tuple[dict[str, str], pd.DataFrame]:
    is_statement_role = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-", na=False
    )
    is_role_include = presentation["role_definition"].str.contains(
        metric.role_include_pattern, case=False, regex=True, na=False
    )

    if metric.role_exclude_pattern:
        is_role_exclude = presentation["role_definition"].str.contains(
            metric.role_exclude_pattern, case=False, regex=True, na=False
        )
    else:
        is_role_exclude = pd.Series(False, index=presentation.index)

    is_target_role = is_statement_role & is_role_include & ~is_role_exclude
    is_not_abstract = ~presentation["is_abstract"].astype(bool)

    label_stripped_all = presentation["label"].map(_strip_parenthetical_asides)
    mentions_metric = presentation["label"].str.contains(
        metric.mention_pattern, case=False, regex=True, na=False
    ) | label_stripped_all.str.contains(
        metric.mention_pattern, case=False, regex=True, na=False
    )

    if metric.exclude_label_pattern:
        is_excluded_label = presentation["label"].str.contains(
            metric.exclude_label_pattern, case=False, regex=True, na=False
        )
    else:
        is_excluded_label = pd.Series(False, index=presentation.index)

    base_candidates = presentation[
        is_target_role & is_not_abstract & mentions_metric & ~is_excluded_label
    ].copy()

    if base_candidates.empty:
        raise TargetRowNotFound(
            f"לא נמצאה אף שורת '{metric.name}' בתוך Statement role ראשי."
        )

    if metric.attributable_pattern:
        is_tier_a = base_candidates["label"].str.contains(
            metric.attributable_pattern, case=False, regex=True, na=False
        )
        tier_a = base_candidates[is_tier_a]

        if len(tier_a) == 1:
            row = tier_a.iloc[0]
            return (
                {
                    "role_uri": str(row["role_uri"]),
                    "role_definition": str(row["role_definition"]),
                    "concept_qname": str(row["concept_qname"]),
                    "label": str(row["label"]),
                    "period_type": str(row["period_type"]),
                    "selection_tier": "attributable_to_shareholders",
                },
                base_candidates,
            )

    label_stripped = base_candidates["label"].map(_strip_parenthetical_asides)
    is_tier_b = base_candidates["label"].str.match(
        metric.plain_pattern, case=False, na=False
    ) | label_stripped.str.match(metric.plain_pattern, case=False, na=False)
    tier_b = base_candidates[is_tier_b]

    if len(tier_b) == 1:
        row = tier_b.iloc[0]
        return (
            {
                "role_uri": str(row["role_uri"]),
                "role_definition": str(row["role_definition"]),
                "concept_qname": str(row["concept_qname"]),
                "label": str(row["label"]),
                "period_type": str(row["period_type"]),
                "selection_tier": "plain",
            },
            base_candidates,
        )

    raise TargetRowNotFound(
        f"לא ניתן לזהות שורת '{metric.name}' יחידה וחד-משמעית "
        f"(מועמדים: {len(base_candidates)}, tier-B: {len(tier_b)})."
    )


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


def reconstruct_calculation_children_by_parent(
    connection: duckdb.DuckDBPyConnection, accession_number: str, role_uri: str
) -> dict[str, list[str]]:
    edges = connection.execute(
        """
        SELECT parent_concept, child_concept
        FROM xbrl_calculation_relationships
        WHERE accession_number = ? AND role_uri = ?
        """,
        [accession_number, role_uri],
    ).fetchdf()

    children_by_parent: dict[str, list[str]] = {}

    for parent, child in zip(edges["parent_concept"], edges["child_concept"]):
        if not parent or not child:
            continue
        children_by_parent.setdefault(parent, []).append(child)

    return children_by_parent


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


import datetime as _datetime_module

PRIOR_PERIOD_DATE_TOLERANCE_DAYS = 10


def match_facts_from_warehouse(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    concept_qname: str,
    report_date: str,
    expected_period_type: str,
    date_tolerance_days: int = 0,
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
        base["_date_diff_days"] = base["instant_date"].map(
            lambda d: abs((_datetime_module.date.fromisoformat(str(d)) - requested_date).days)
        )
        candidates = base[base["_date_diff_days"] <= date_tolerance_days]
    else:
        base = facts[
            (facts["dimensions_json"] == "{}")
            & (facts["unit_id"].isin(usd_unit_ids))
            & (~facts["is_nil"])
            & facts["value_numeric"].notna()
            & facts["period_end"].notna()
        ].copy()
        base["_date_diff_days"] = base["period_end"].map(
            lambda d: abs((_datetime_module.date.fromisoformat(str(d)) - requested_date).days)
        )
        candidates = base[base["_date_diff_days"] <= date_tolerance_days]

        if not candidates.empty:
            starts = pd.to_datetime(candidates["period_start"])
            ends = pd.to_datetime(candidates["period_end"])
            duration_days = (ends - starts).dt.days
            candidates = candidates[
                (duration_days >= ANNUAL_DURATION_MIN_DAYS)
                & (duration_days <= ANNUAL_DURATION_MAX_DAYS)
            ]

    date_field = "instant_date" if expected_period_type == "instant" else "period_end"
    matched_dates = sorted(set(str(d) for d in candidates[date_field].tolist())) if not candidates.empty else []

    if candidates.empty:
        return {
            "status": "REVIEW_REQUIRED",
            "error": "no fact passed unit/dimension/period filters",
            "value": None,
            "requested_date": report_date,
            "date_tolerance_days": date_tolerance_days,
            "matched_dates": [],
        }

    if len(matched_dates) > 1:
        return {
            "status": "REVIEW_REQUIRED",
            "error": (
                f"more than one candidate date within tolerance "
                f"({date_tolerance_days} days): {matched_dates} — cannot "
                "select unambiguously"
            ),
            "value": None,
            "requested_date": report_date,
            "date_tolerance_days": date_tolerance_days,
            "matched_dates": matched_dates,
        }

    candidates, _notes = _reconcile_same_context_precision_duplicates_from_warehouse(
        candidates
    )

    distinct_values = sorted(set(candidates["value_numeric"].tolist()))

    if len(distinct_values) == 1:
        matched_date = matched_dates[0]
        actual_diff_days = abs(
            (_datetime_module.date.fromisoformat(matched_date) - requested_date).days
        )
        return {
            "status": "PASS",
            "value": distinct_values[0],
            "context_id": str(candidates.iloc[0]["context_id"]),
            "unit_id": str(candidates.iloc[0]["unit_id"]),
            "requested_date": report_date,
            "matched_date": matched_date,
            "date_diff_days": actual_diff_days,
            "date_tolerance_days": date_tolerance_days,
        }

    return {
        "status": "REVIEW_REQUIRED",
        "error": f"multiple distinct values: {distinct_values}",
        "value": None,
        "requested_date": report_date,
        "matched_dates": matched_dates,
        "date_tolerance_days": date_tolerance_days,
    }


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
            rounded = _round_to_xbrl_decimals(
                most_precise["value_numeric"], other_row["decimals"]
            )

            if rounded is None or rounded != other_row["value_numeric"]:
                all_consistent = False
                break

        if all_consistent:
            reconciled_rows.append(most_precise)
            notes.append(
                f"context {context_id}: {len(group)} facts at different "
                "rounding precision reconciled to the most precise value "
                f"({most_precise['value_numeric']}, decimals="
                f"{most_precise['decimals']})."
            )
        else:
            for _, row in group.iterrows():
                reconciled_rows.append(row)

    return pd.DataFrame(reconciled_rows), notes


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


# =============================================================================
# NEW (this script, Policy B) — undrawn revolving-credit-facility evidence.
# =============================================================================

REVOLVER_OUTSTANDING_CONCEPT_PATTERN = r"^us-gaap:LineOfCredit$"
REVOLVER_MEMBER_PATTERN = r"revolving"


def find_undrawn_revolver_evidence(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    report_date: str,
) -> dict[str, object] | None:
    """
    Policy B: an explicitly undrawn revolving-credit facility is zero
    debt for that facility. Evidence accepted ONLY as a real, resolved
    (non-nil, numeric) XBRL fact for the facility's own "amount
    outstanding" concept (us-gaap:LineOfCredit — the standard tag filers
    use for revolver draw balances, confirmed generic across CRWD/META/
    NVDA/PANW's own filings, never a ticker-specific tag), dimensioned
    by a CreditFacilityAxis/LongtermDebtTypeAxis member whose name
    contains "Revolving" (case-insensitive — never a company-specific
    member name), at the EXACT report date, equal to 0. Never inferred
    from the credit limit/availability alone (policy requirement — an
    unused commitment is not evidence of an outstanding balance either
    way, so it is never consulted here).
    """

    facts = connection.execute(
        """
        SELECT value_numeric, instant_date, dimensions_json, context_id, is_nil
        FROM xbrl_facts
        WHERE accession_number = ?
          AND regexp_matches(concept_qname, ?, 'i')
          AND regexp_matches(dimensions_json, ?, 'i')
          AND is_nil = FALSE
        """,
        [accession_number, REVOLVER_OUTSTANDING_CONCEPT_PATTERN, REVOLVER_MEMBER_PATTERN],
    ).fetchdf()

    if facts.empty:
        return None

    at_report_date = facts[
        (facts["instant_date"] == report_date) & facts["value_numeric"].notna()
    ]

    if at_report_date.empty:
        return None

    distinct_values = sorted(set(at_report_date["value_numeric"].tolist()))

    if len(distinct_values) != 1:
        return None  # genuinely ambiguous — do not guess, fall through

    if distinct_values[0] != 0:
        return None  # revolver IS drawn — not evidence of zero

    row = at_report_date.iloc[0]
    return {
        "concept_qname": "us-gaap:LineOfCredit",
        "value": 0.0,
        "instant_date": str(row["instant_date"]),
        "context_id": str(row["context_id"]),
        "dimensions_json": str(row["dimensions_json"]),
    }


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


def resolve_total_debt_with_aggregate_policy(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
    current_debt_result: dict[str, object],
    long_term_debt_result: dict[str, object],
) -> dict[str, object]:
    """
    3-tier preference (extends D-022's 2-tier resolver with the NEW
    Policy C direct-aggregate tier in the middle):
      1. GAAP_CARRYING_VALUE — current_debt + long_term_debt, both PASS.
      2. PASS_DIRECT_AGGREGATE (NEW) — the filing's OWN reported "Total"
         row in its debt-maturity schedule, used AS-IS even when it does
         not reconcile with the balance-sheet long_term_debt carrying
         value (e.g. face value vs. carrying value net of unamortized
         discount/issuance costs) — the gap is recorded, never silently
         dropped, and never blocks this or downstream metrics.
      3. PASS_MATURITY_BASIS (D-022, unchanged) — bucket-sum fallback
         when no reported Total row exists either.
    """

    if (
        current_debt_result.get("status") == "PASS"
        and long_term_debt_result.get("status") == "PASS"
    ):
        return {
            "status": "PASS",
            "value": current_debt_result["value"] + long_term_debt_result["value"],
            "basis": "GAAP_CARRYING_VALUE",
        }

    maturity = classify_maturity_buckets(connection, accession_number, presentation, report_date)

    if maturity["reported_total"] is not None:
        return {
            "status": "PASS_DIRECT_AGGREGATE",
            "value": maturity["reported_total"],
            "basis": "DIRECT_AGGREGATE_REPORTED_TOTAL",
            "reconciliation_gap": maturity["reconciliation_gap"],
            "maturity_role_uri": maturity["role_uri"],
            "carrying_value_current_debt": current_debt_result.get("value"),
            "carrying_value_current_debt_status": current_debt_result.get("status"),
            "carrying_value_long_term_debt": long_term_debt_result.get("value"),
            "carrying_value_long_term_debt_status": long_term_debt_result.get("status"),
            "lineage": {
                "reported_total_face_value": maturity["reported_total"],
                "reconciliation_gap_vs_bucket_sum": maturity["reconciliation_gap"],
                "note": (
                    "Policy C: reported total used as-is; gap (if any) vs. "
                    "balance-sheet long_term_debt carrying value is a known, "
                    "expected face-value-vs-carrying-value difference "
                    "(e.g. unamortized debt issuance costs/discount), not a "
                    "data error — never blocks total_debt or downstream "
                    "metrics per approved Policy C."
                ),
            },
        }

    if maturity["total_debt_maturity"] is not None:
        return {
            "status": "PASS_MATURITY_BASIS",
            "value": maturity["total_debt_maturity"],
            "basis": "MATURITY_PRINCIPAL",
            "source_current_debt_maturity": maturity["current_debt_maturity"],
            "source_long_term_debt_maturity": maturity["long_term_debt_maturity"],
            "maturity_role_uri": maturity["role_uri"],
            "carrying_value_current_debt": current_debt_result.get("value"),
            "carrying_value_current_debt_status": current_debt_result.get("status"),
            "carrying_value_long_term_debt": long_term_debt_result.get("value"),
            "carrying_value_long_term_debt_status": long_term_debt_result.get("status"),
        }

    return {
        "status": "REVIEW_REQUIRED",
        "value": None,
        "basis": None,
        "error": (
            f"no reliable carrying-value total (current_debt="
            f"{current_debt_result.get('status')}, long_term_debt="
            f"{long_term_debt_result.get('status')}), no reported "
            "debt-maturity-schedule Total row, and no maturity-bucket-sum "
            "fallback available either"
        ),
    }


SUCCESSFUL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE", "PASS_NORMALIZED_TAX"}


def compute_company_year(
    connection: duckdb.DuckDBPyConnection,
    ticker: str,
    report_date: str,
    accession_number: str,
) -> dict[str, object]:
    presentation = reconstruct_presentation_dataframe(connection, accession_number)

    ltd = resolve_long_term_debt(connection, accession_number, presentation, report_date)

    cd_resolution = resolve_current_debt_with_facility_policy(
        connection, accession_number, presentation, report_date, ltd
    )

    if cd_resolution["mode"] == "components":
        values = []
        component_ok = True
        for r in cd_resolution["rows"]:
            decision = match_facts_from_warehouse(
                connection, accession_number, r["concept_qname"], report_date, "instant"
            )
            if decision["status"] != "PASS":
                cd = {
                    "status": "REVIEW_REQUIRED",
                    "error": f"component {r['concept_qname']} not resolvable",
                    "value": None,
                }
                component_ok = False
                break
            values.append(decision["value"])
        if component_ok:
            cd = {
                "status": "PASS",
                "value": sum(values),
                "concept_qname": " + ".join(r["concept_qname"] for r in cd_resolution["rows"]),
                "selection_tier": cd_resolution["rows"][0]["selection_tier"],
            }
    elif cd_resolution["mode"] == "zero":
        cd = {
            "status": "PASS",
            "value": cd_resolution["value"],
            "concept_qname": cd_resolution["concept_qname"],
            "selection_tier": cd_resolution["selection_tier"],
            "basis": cd_resolution["basis"],
            "lineage": cd_resolution["lineage"],
        }
    else:
        cd = {"status": "REVIEW_REQUIRED", "error": cd_resolution["error"], "value": None}

    total_debt = resolve_total_debt_with_aggregate_policy(
        connection, accession_number, presentation, report_date, cd, ltd
    )

    cash = _reconstruct_simple_metric(connection, accession_number, presentation, "cash_and_equivalents", report_date)
    sti = _reconstruct_simple_metric(connection, accession_number, presentation, "short_term_investments", report_date)
    equity = _reconstruct_simple_metric(connection, accession_number, presentation, "stockholders_equity", report_date)

    def _combine_status(*statuses: str | None) -> str:
        if all(s in SUCCESSFUL_STATUSES for s in statuses):
            if "PASS_DIRECT_AGGREGATE" in statuses:
                return "PASS_DIRECT_AGGREGATE"
            if "PASS_MATURITY_BASIS" in statuses:
                return "PASS_MATURITY_BASIS"
            return "PASS"
        return "REVIEW_REQUIRED"

    adj_status = _combine_status(total_debt["status"], cash["status"], sti["status"])
    adjusted_net_debt = {
        "status": adj_status,
        "value": (
            total_debt["value"] - cash["value"] - sti["value"]
            if adj_status in SUCCESSFUL_STATUSES else None
        ),
        "basis": total_debt.get("basis"),
    }

    ic_status = _combine_status(total_debt["status"], equity["status"], cash["status"], sti["status"])
    invested_capital = {
        "status": ic_status,
        "value": (
            total_debt["value"] + equity["value"] - cash["value"] - sti["value"]
            if ic_status in SUCCESSFUL_STATUSES else None
        ),
        "basis": total_debt.get("basis"),
    }

    return {
        "ticker": ticker, "report_date": report_date, "accession_number": accession_number,
        "current_debt": cd, "long_term_debt": ltd, "total_debt": total_debt,
        "cash_and_equivalents": cash, "short_term_investments": sti, "stockholders_equity": equity,
        "adjusted_net_debt": adjusted_net_debt, "invested_capital": invested_capital,
    }


def _reconstruct_simple_metric(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    metric_name: str,
    report_date: str,
) -> dict[str, object]:
    metric = BUILT_IN_METRICS[metric_name]
    try:
        row, _candidates = identify_canonical_row(presentation, metric)
    except TargetRowNotFound as exc:
        return {"status": "REVIEW_REQUIRED", "error": str(exc), "value": None}

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
# Production DB write (idempotent, ON CONFLICT DO NOTHING, never overwrites)
# =============================================================================


def write_result_to_production_db(
    prod_connection: duckdb.DuckDBPyConnection,
    result: dict[str, object],
) -> int:
    accession_number = result["accession_number"]
    run_id = f"{accession_number}::{ENGINE_VERSION}"

    prod_connection.execute(
        """
        INSERT INTO extraction_runs (extraction_run_id, accession_number, engine_version)
        VALUES (?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        [run_id, accession_number, ENGINE_VERSION],
    )

    rows_written = 0
    for metric_name in WRITTEN_METRICS:
        metric = result[metric_name]
        status = metric["status"]
        value = metric.get("value")
        selection_tier = metric.get("selection_tier")
        source_concept = metric.get("concept_qname")

        validation_reason = None
        if status == "REVIEW_REQUIRED":
            validation_reason = metric.get("error")
        elif metric.get("basis") == "ZERO_EXPLICIT_UNDRAWN_FACILITY":
            validation_reason = (
                f"D-027 Policy B: explicitly undrawn revolving-credit facility "
                f"(us-gaap:LineOfCredit = 0 at report date) proves zero "
                f"current_debt; lineage={metric.get('lineage')}"
            )
        elif metric.get("basis") == "ZERO_PROVEN_STRUCTURAL_ABSENCE":
            validation_reason = (
                f"D-027/D-026: zero proven — no debt-vocabulary candidate "
                f"found anywhere on the balance sheet; lineage={metric.get('lineage')}"
            )
        elif metric.get("basis") == "DIRECT_AGGREGATE_REPORTED_TOTAL":
            validation_reason = (
                f"D-027 Policy C: filing's own reported debt-maturity-schedule "
                f"Total used as authoritative total_debt; "
                f"lineage={metric.get('lineage')}"
            )
        elif metric.get("basis") == "MATURITY_PRINCIPAL":
            validation_reason = (
                "D-022 Policy A: no reliable carrying-value total; "
                f"maturity-principal fallback used. source_current_debt_maturity="
                f"{metric.get('source_current_debt_maturity')}, "
                f"source_long_term_debt_maturity={metric.get('source_long_term_debt_maturity')}"
            )

        unit = None
        if value is not None:
            unit = "iso4217:USD"

        prod_connection.execute(
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
                run_id, metric_name,
                metric_name in ("current_debt", "long_term_debt", "total_debt"),
                status, value, unit, None, None, result["report_date"],
                source_concept, None, None, selection_tier,
                metric_name not in ("current_debt", "long_term_debt"),
                None, validation_reason,
            ],
        )
        rows_written += 1

    return rows_written


def main() -> None:
    total_start = time.perf_counter()
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH))

    all_results: dict[str, dict[str, object]] = {}
    print(f"Processing {len(TARGET_FILINGS)} filings (Groups 1/3/4), warehouse-only, zero Arelle calls.")
    print()

    for ticker, report_date, accession_number in TARGET_FILINGS:
        filing_start = time.perf_counter()
        print(f"=== {ticker} {report_date} ({accession_number}) ===")
        print(f"  arelle_required=NO (already warehoused)  warehouse_status=PASS")

        result = compute_company_year(warehouse_connection, ticker, report_date, accession_number)
        all_results[f"{ticker}_{report_date}"] = result

        rows_written = write_result_to_production_db(prod_connection, result)

        for metric_name in WRITTEN_METRICS:
            m = result[metric_name]
            print(f"  {metric_name:20s} status={m['status']:22s} value={m.get('value')} basis={m.get('basis')}")

        filing_elapsed = time.perf_counter() - filing_start
        print(f"  db_write_status=OK rows_written={rows_written} elapsed={filing_elapsed:.3f}s")
        print()

    warehouse_connection.close()
    prod_connection.close()

    total_elapsed = time.perf_counter() - total_start
    print("=" * 100)
    print(f"GROUPS 1/3/4 COMPLETE — total_elapsed_seconds = {total_elapsed:.3f}")
    print("=" * 100)

    output_path = DATA_DIR / "d027_groups_1_3_4_results.json"
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(all_results, handle, indent=2, default=str, ensure_ascii=False)
    print(f"Full results written to {output_path}")


if __name__ == "__main__":
    main()
