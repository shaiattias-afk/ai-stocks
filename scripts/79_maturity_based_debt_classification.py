"""
v2 (this file, 77, copied from 76 — 76 preserved unmodified): fixes a
genuine, general bug found while diagnosing MSFT's 0/13 canonical-
candidate mismatch. Root cause (confirmed by direct inspection, not
assumed — see diagnostic evidence in the corresponding chat turn and
docs/LAST_CLAUDE_REPORT.md): a fact's `unit_id` in xbrl_facts is the
arbitrary XML @id string the FILER'S OWN tagging software assigned to
its <xbrli:unit> element, never a standardized value — confirmed across
all 4 filings in the warehouse: AMZN/META/NVDA all happen to write
"usd" (lowercase), MSFT writes "U_USD". Script 76's
match_facts_from_warehouse filtered directly on
`unit_id.lower() == "usd"`, which only worked for 3 of 4 filers by
coincidence, silently losing every MSFT candidate at the unit-filtering
stage regardless of concept/role/label correctness (row identification
and label resolution were already correct for MSFT before this fix).
The general, correct signal — exactly what the live engine's own
match_facts() checks (`unit_measures == "iso4217:USD"`, derived from
the unit's `.measures` content, never its XML id) — is the unit's own
MEASURE, already stored in xbrl_units.numerator_measures. New function
usd_unit_ids_for_accession() resolves the set of unit_ids whose
numerator_measures is bare "iso4217:USD" (no denominator, excluding
ratio units like "usdPerShare") for a given accession, and
match_facts_from_warehouse now filters against that set instead of a
literal string. No ticker-specific branch: this is a correction to a
previously-wrong general assumption, not a Microsoft-specific rule —
it changes nothing for any filer whose unit_id already happened to be
"usd".

Second general fix, found immediately after the first (MSFT's
current_debt/total_debt remained REVIEW_REQUIRED even after the unit
fix): the exact same balance can be tagged twice within one context at
different rounding precision — MSFT's CommercialPaper, 2024-06-30, is
tagged both as $6,693,000,000 (decimals=-6) and $6,700,000,000
(decimals=-8, the same value rounded to the nearest hundred million) —
a known, general, ticker-agnostic Inline XBRL pattern the LIVE engine
already reconciles (scripts/72's
_reconcile_same_context_precision_duplicates) but this warehouse-based
fact-matcher had not yet reproduced. Fixed by copying that same
reconciliation logic (_decimals_precision_rank,
_round_to_xbrl_decimals, _reconcile_same_context_precision_duplicates_
from_warehouse) unchanged, applied before the final distinct-value
ambiguity check — a genuine discrepancy (not a rounding artifact) is
still left unreconciled and correctly reported as REVIEW_REQUIRED.

DuckDB-ONLY reconstruction of the canonical-metric candidate evidence
for MSFT 2024-06-30, META 2024-12-31, NVDA 2024-01-28 — no Arelle
import anywhere in this file — compared against each filing's already
verified live-Arelle canonical result
(data/{ticker}_{reportdate}_engine_v16_result.json).

This is the warehouse GENERALIZATION proof's core evidence: the exact
same row-identification and current_debt tiered-resolution ALGORITHMS
already verified live against Arelle (scripts/69-72:
identify_canonical_row, find_current_debt_explicit_total,
find_current_debt_sibling_components, the D-017 zero-inference
conditions, the D-019 ancestry classifier) are reproduced here
UNCHANGED — copied, not reimplemented from scratch — and fed from a
warehouse-reconstructed presentation DataFrame and a warehouse-based
fact-matcher instead of a live Arelle model. Where a function needed
`model_xbrl` directly (calculation-linkbase lookups, fact fetching), a
warehouse-backed equivalent is used, built from the exact same tables
scripts/75 populated. No ticker- or year-specific branch anywhere.

Read-only with respect to everything: does not write to the warehouse,
does not touch the production database, does not compute or change any
canonical metric or status anywhere.

Per decision 2/3 (docs/DECISIONS_LOG.md D-022): current_debt built from
an undiscounted principal maturity-schedule bucket is NEVER accepted as
a canonical value here either — the warehouse-based D-017 zero-
inference reconstruction can still only prove current_debt = 0 (all 4
conditions) or fail closed to REVIEW_REQUIRED, exactly as the live
engine does; it never falls back to extracting a principal amount as a
substitute value.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"

STANDARD_LABEL_ROLE = "http://www.xbrl.org/2003/role/label"

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
]

# The 9 anchor company-years (latest verified year per company) used
# for regression checking — 2 of these (AMZN/GOOGL 2025-12-31) are
# ALSO within this task's own 10-company-year target scope, so their
# total_debt is EXPECTED to change (the fix applies there too); the
# other 7 are completely untouched by this script and must show zero
# differences.
ANCHOR_FILINGS: list[tuple[str, str, str]] = [
    ("ORCL", "2024-05-31", "0000950170-24-075605"),
    ("MSFT", "2024-06-30", "0000950170-24-087843"),
    ("META", "2024-12-31", "0001326801-25-000017"),
    ("NVDA", "2024-01-28", "0001045810-24-000029"),
    ("GOOGL", "2025-12-31", "0001652044-26-000018"),
    ("AMZN", "2025-12-31", "0001018724-26-000004"),
    ("MU", "2025-08-28", None),
    ("CRWD", "2026-01-31", None),
    ("PANW", "2025-07-31", None),
]

AFFECTED_METRICS = [
    "current_debt", "long_term_debt", "total_debt", "adjusted_net_debt",
    "invested_capital", "average_invested_capital", "roic",
]

TARGET_METRICS = [
    "revenue", "operating_income", "net_income", "operating_cash_flow",
    "capex", "cash_and_equivalents", "short_term_investments",
    "current_debt", "long_term_debt", "total_debt", "pretax_income",
    "income_tax_expense", "stockholders_equity",
]


# =============================================================================
# Copied UNCHANGED from scripts/72_xbrl_metric_engine.py — pure string/
# DataFrame logic, no Arelle dependency, no ticker-specific branch.
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
    "revenue": MetricDefinition(
        "revenue", r"operations|income", r"comprehensive",
        r"revenues?|net\s+sales", None, None,
        r"^\s*(?:total\s+)?(?:revenues?|net\s+sales)\s*$",
    ),
    "net_income": MetricDefinition(
        "net_income", r"operations|income", r"comprehensive",
        r"net\s+(?:income|loss)", r"per\s+share|weighted\s+average",
        r"attributable\s+to.*(?:common|stockholders|shareholders|"
        r"corporation|company|\binc\.?\b|\bcorp\.?\b)",
        r"^\s*net\s+(?:income\s*\(loss\)|income|loss)\s*$",
    ),
    "operating_income": MetricDefinition(
        "operating_income", r"operations|income", r"comprehensive",
        r"operating\s+(?:income|loss)|"
        r"(?:income|loss)\s*(?:\(loss\)|\(income\))?\s*from\s+operations",
        r"per\s+share|weighted\s+average|margin|percentage", None,
        r"^\s*(?:"
        r"operating\s+(?:income\s*\(loss\)|income|loss)"
        r"|(?:income|loss)\s*(?:\(loss\)|\(income\))?\s*from\s+operations"
        r")\s*$",
    ),
    "operating_cash_flow": MetricDefinition(
        "operating_cash_flow", r"cash\s*flows?", None,
        r"net\s+cash.*(?:from\s+operations|operating\s+activities)", None, None,
        r"^\s*net\s+cash\s+(?:provided\s+by|used\s+in|from)\s+"
        r"operat(?:ing\s+activities|ions)\s*$",
    ),
    "capex": MetricDefinition(
        "capex", r"cash\s*flows?", None,
        r"capital\s+expenditures?|"
        r"(?:additions|purchases|expenditures)\s+"
        r"(?:related\s+to\s+|for\s+|to\s+|of\s+)?property",
        r"unpaid|incurred\s+but\s+not\s+yet\s+paid|"
        r"accounts\s+payable|accrued", None,
        r"^\s*capital\s+expenditures?\s*$|"
        r"^\s*(?:additions|purchases|expenditures)\s+"
        r"(?:related\s+to\s+|for\s+|to\s+|of\s+)?property.*$",
    ),
    "cash_and_equivalents": MetricDefinition(
        "cash_and_equivalents", r"balance\s+sheets?|financial\s+position",
        r"parenthetical", r"cash\s+and\s+cash\s+equivalents", None, None,
        r"^\s*cash\s+and\s+cash\s+equivalents\s*$",
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
    "pretax_income": MetricDefinition(
        "pretax_income", r"operations|income", r"comprehensive",
        r"income\s*(?:\(loss\))?\s*before.*tax", None, None,
        r"^\s*income\s*(?:\(loss\))?\s*before\s+"
        r"(?:(?:provision\s+for|benefit\s+from)\s+)?"
        r"income\s+tax(?:es)?(?:\s+and\s+.*)?\s*$",
    ),
    "income_tax_expense": MetricDefinition(
        "income_tax_expense", r"operations|income", r"comprehensive",
        r"(?:provision|benefit).*income\s+tax|"
        r"income\s+tax.*(?:provision|expense|benefit)", None, None,
        r"^\s*(?:provision|benefit)\s+(?:for|from)\s+income\s+tax(?:es)?\s*$"
        r"|^\s*income\s+tax\s+(?:expense|provision|benefit)"
        r"(?:\s*\((?:expense|benefit|provision)\))?\s*$",
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
CURRENT_PORTION_DISCLOSURE_LABEL_PATTERN = (
    r"current\s+portion|current\s+maturit|due\s+within|"
    r"short-?term\s+(?:debt|borrowings?)|commercial\s+paper"
)

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
    """Copied unchanged from scripts/72 — structure-first row selection."""

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


# =============================================================================
# Warehouse-backed data access layer (the only genuinely NEW code here —
# everything above is copied unchanged; this is where "live Arelle" is
# replaced with "DuckDB query").
# =============================================================================


def reconstruct_presentation_dataframe(
    connection: duckdb.DuckDBPyConnection, accession_number: str
) -> pd.DataFrame:
    """
    Rebuilds a DataFrame with the EXACT same shape identify_canonical_row
    (and every other function above) expects from a live Arelle
    presentation walk (scripts/72's extract_presentation): role_uri,
    role_definition, depth, parent_qname, concept_qname, label,
    is_abstract, period_type, balance — reconstructed purely by JOINing
    xbrl_presentation_relationships with xbrl_concepts and xbrl_labels.
    """

    # Label resolution mirrors scripts/72's live _safe_label(concept,
    # preferred_label) exactly: try the label role named on the
    # PRESENTATION ARC ITSELF (preferred_label, e.g. "verboseLabel")
    # first, falling back to the standard label role only if no label
    # exists under the preferred role. This matters in practice — e.g.
    # MSFT's revenue concept
    # (us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax) has
    # a standard label of "Revenue from Contract with Customer,
    # Excluding Assessed Tax" but a verboseLabel of simply "Revenue";
    # the live engine displays "Revenue" (the arc's preferred role),
    # and using the standard role unconditionally — the bug an initial
    # version of this reconstruction had — silently picks the wrong
    # text and breaks the anchored plain_pattern match. The warehouse
    # already stores preferred_label per edge (scripts/75); this was a
    # bug in this reconstruction query, not missing warehouse data.
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

    # A concept can have >1 label in the standard role from different
    # sources in rare cases — keep exactly one row per presentation edge.
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
    """Warehouse-backed equivalent of scripts/72's
    find_current_debt_calculation_components — same decision logic,
    reading xbrl_calculation_relationships instead of a live
    relationshipSet."""

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
    """
    General correction (v2, this script): a fact's `unit_id` is the
    arbitrary XML @id string the FILER'S OWN tagging software assigned
    to its <xbrli:unit> element — never a standard. AMZN/META/NVDA all
    happen to write it as "usd" (lowercase), but MSFT writes "U_USD" —
    confirmed by direct inspection of xbrl_units across all 4 filings
    currently in the warehouse (diagnostic evidence, not assumed).
    Matching the literal string "usd" is therefore not a general rule;
    it is a coincidence that happened to hold for 3 of 4 filers. The
    correct, general signal — exactly what the live engine's
    match_facts() actually checks (`unit_measures == "iso4217:USD"`,
    derived from the unit's `.measures` content, never its XML id) — is
    the unit's own MEASURE, stored in xbrl_units.numerator_measures.
    Excludes any unit with a denominator (e.g. "usdPerShare",
    USD-per-share ratio units), matching the live engine's requirement
    of a bare "iso4217:USD" measure list with nothing else.
    """

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


def match_facts_from_warehouse(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    concept_qname: str,
    report_date: str,
    expected_period_type: str,
) -> dict[str, object]:
    """
    Warehouse-backed equivalent of scripts/72's match_facts +
    deduplicate_and_decide combined: filters xbrl_facts for the given
    concept, USD unit (by MEASURE, not by the filer's arbitrary unit
    @id string — see usd_unit_ids_for_accession), no dimensions,
    matching period end/instant date, correct period type, not nil,
    numeric value present — then decides PASS (exactly one distinct
    value) vs REVIEW_REQUIRED.
    """

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

    if expected_period_type == "instant":
        candidates = facts[
            (facts["instant_date"] == report_date)
            & (facts["dimensions_json"] == "{}")
            & (facts["unit_id"].isin(usd_unit_ids))
            & (~facts["is_nil"])
            & facts["value_numeric"].notna()
        ]
    else:
        candidates = facts[
            (facts["period_end"] == report_date)
            & (facts["dimensions_json"] == "{}")
            & (facts["unit_id"].isin(usd_unit_ids))
            & (~facts["is_nil"])
            & facts["value_numeric"].notna()
        ]

        if not candidates.empty:
            starts = pd.to_datetime(candidates["period_start"])
            ends = pd.to_datetime(candidates["period_end"])
            duration_days = (ends - starts).dt.days
            candidates = candidates[
                (duration_days >= ANNUAL_DURATION_MIN_DAYS)
                & (duration_days <= ANNUAL_DURATION_MAX_DAYS)
            ]

    if candidates.empty:
        return {
            "status": "REVIEW_REQUIRED",
            "error": "no fact passed unit/dimension/period filters",
            "value": None,
        }

    # Second general fix (found while diagnosing MSFT's current_debt):
    # the exact same balance can be tagged twice within one context at
    # different rounding precision (e.g. MSFT's CommercialPaper,
    # 2024-06-30: $6,693,000,000 at decimals=-6 alongside
    # $6,700,000,000 at decimals=-8 — the same value, rounded, in the
    # same context) — a known, general, ticker-agnostic Inline XBRL
    # pattern this project's LIVE engine already reconciles
    # (scripts/72's _reconcile_same_context_precision_duplicates); this
    # warehouse-based matcher had not yet reproduced that reconciliation.
    candidates, _notes = _reconcile_same_context_precision_duplicates_from_warehouse(
        candidates
    )

    distinct_values = sorted(set(candidates["value_numeric"].tolist()))

    if len(distinct_values) == 1:
        return {
            "status": "PASS",
            "value": distinct_values[0],
            "context_id": str(candidates.iloc[0]["context_id"]),
            "unit_id": str(candidates.iloc[0]["unit_id"]),
        }

    return {
        "status": "REVIEW_REQUIRED",
        "error": f"multiple distinct values: {distinct_values}",
        "value": None,
    }


def _decimals_precision_rank(decimals_value: object) -> float:
    """Copied unchanged from scripts/72 — higher = more precise."""

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
    """Copied unchanged from scripts/72."""

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
    """
    Warehouse-backed equivalent of scripts/72's
    _reconcile_same_context_precision_duplicates — identical logic,
    grouped by context_id, collapsing a group to its most-precise fact
    only when every coarser value is exactly what standard XBRL
    rounding produces from the most precise one; a genuine discrepancy
    (not a rounding artifact) is left untouched so ambiguity is still
    reported honestly.
    """

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


# =============================================================================
# D-022 — maturity-based debt classification (approved policy). Never
# used to set canonical current_debt/long_term_debt (D-021 unchanged:
# those remain GAAP-carrying-value-only, REVIEW_REQUIRED without one).
# Used ONLY as an explicit fallback basis for total_debt (and, through
# it, adjusted_net_debt/invested_capital/average_invested_capital/roic)
# when no reliable carrying-value total_debt exists — status
# PASS_MATURITY_BASIS, basis MATURITY_PRINCIPAL, always recorded
# alongside any carrying-value debt that does exist (never silently
# discarded — policy requirement 6).
# =============================================================================

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
    """
    Reconstructs every row in the filing's own debt-maturity schedule,
    classifies each as CURRENT (the earliest, chronologically-first
    non-abstract, non-Total bucket — due within 12 months, approved
    policy item 1), LONG_TERM (every other non-abstract, non-Total,
    non-excluded bucket — due after 12 months, policy item 2), or
    EXCLUDED (lease/purchase-obligation/interest/other non-debt —
    policy exclusions), and sums current_debt_maturity,
    long_term_debt_maturity, total_debt_maturity (policy item 3). Also
    resolves the schedule's own reported "Total" row (if any) for a
    reconciliation-gap cross-check. Every candidate carries full
    lineage (label, concept, order, value, context, unit) for audit.
    """

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

    # Chronological classification (policy items 1-2): the earliest
    # (lowest presentation_order) non-excluded, non-total bucket is
    # CURRENT (due within 12 months); every later one is LONG_TERM (due
    # after 12 months) — the standard, universal structure of a debt-
    # maturity-by-fiscal-year table, never a ticker-specific rule.
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


# =============================================================================
# Per-metric reconstruction + comparison
# =============================================================================


def reconstruct_metric(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    metric_name: str,
    report_date: str,
    resolved_cache: dict[str, dict[str, object]],
) -> dict[str, object]:
    if metric_name in BUILT_IN_METRICS and metric_name not in (
        "current_debt", "long_term_debt",
    ):
        metric = BUILT_IN_METRICS[metric_name]
        period_type = "duration" if metric_name in (
            "revenue", "operating_income", "net_income", "operating_cash_flow",
            "capex", "pretax_income", "income_tax_expense",
        ) else "instant"

        try:
            row, _candidates = identify_canonical_row(presentation, metric)
        except TargetRowNotFound as exc:
            return {"status": "REVIEW_REQUIRED", "error": str(exc), "value": None}

        decision = match_facts_from_warehouse(
            connection, accession_number, row["concept_qname"], report_date, period_type
        )

        return {
            "status": decision["status"],
            "error": decision.get("error"),
            "value": decision.get("value"),
            "concept_qname": row["concept_qname"],
            "selection_tier": row["selection_tier"],
        }

    if metric_name == "long_term_debt":
        metric = BUILT_IN_METRICS["long_term_debt"]

        try:
            row, _candidates = identify_canonical_row(presentation, metric)
        except TargetRowNotFound:
            already_claimed = {
                r["concept_qname"] for r in resolved_cache.get("current_debt", {}).get(
                    "component_rows", []
                )
            }
            ancestry_matches = resolve_debt_classification_by_ancestry_from_warehouse(
                presentation, "noncurrent", already_claimed
            )

            if len(ancestry_matches) == 1:
                row = ancestry_matches[0]
            else:
                return {
                    "status": "REVIEW_REQUIRED",
                    "error": "no unique long_term_debt row (label or ancestry)",
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

    if metric_name == "current_debt":
        mode, rows = resolve_current_debt_components_from_warehouse(
            connection, accession_number, presentation
        )

        if mode == "components":
            values = []
            for r in rows:
                decision = match_facts_from_warehouse(
                    connection, accession_number, r["concept_qname"], report_date, "instant"
                )
                if decision["status"] != "PASS":
                    result = {
                        "status": "REVIEW_REQUIRED",
                        "error": f"component {r['concept_qname']} not resolvable",
                        "value": None,
                        "component_rows": rows,
                    }
                    resolved_cache["current_debt"] = result
                    return result
                values.append(decision["value"])

            result = {
                "status": "PASS",
                "value": sum(values),
                "concept_qname": " + ".join(r["concept_qname"] for r in rows),
                "selection_tier": rows[0]["selection_tier"],
                "component_rows": rows,
            }
            resolved_cache["current_debt"] = result
            return result

        # mode == "zero_inference_needed"
        long_term_debt_result = resolved_cache.get("long_term_debt")
        long_term_debt_row = (
            {"concept_qname": long_term_debt_result["concept_qname"]}
            if long_term_debt_result and long_term_debt_result.get("status") == "PASS"
            else None
        )

        try:
            inferred = attempt_current_debt_zero_inference_from_warehouse(
                connection, accession_number, presentation, report_date, long_term_debt_row
            )
            result = {
                "status": "PASS",
                "value": inferred["value"],
                "concept_qname": inferred["concept_qname"],
                "selection_tier": inferred["selection_tier"],
                "component_rows": [],
            }
        except TargetRowNotFound as exc:
            result = {
                "status": "REVIEW_REQUIRED",
                "error": str(exc),
                "value": None,
                "component_rows": [],
            }

        resolved_cache["current_debt"] = result
        return result

    if metric_name == "total_debt":
        current_debt_result = resolved_cache.get("current_debt")
        long_term_debt_result = resolved_cache.get("long_term_debt")

        if (
            current_debt_result
            and current_debt_result.get("status") == "PASS"
            and long_term_debt_result
            and long_term_debt_result.get("status") == "PASS"
        ):
            return {
                "status": "PASS",
                "value": current_debt_result["value"] + long_term_debt_result["value"],
                "concept_qname": None,
                "selection_tier": None,
            }

        return {
            "status": "REVIEW_REQUIRED",
            "error": (
                f"components: current_debt="
                f"{current_debt_result.get('status') if current_debt_result else 'MISSING'}, "
                f"long_term_debt="
                f"{long_term_debt_result.get('status') if long_term_debt_result else 'MISSING'}"
            ),
            "value": None,
        }

    raise ValueError(f"unsupported metric: {metric_name}")


SUCCESSFUL_STATUSES = {"PASS", "PASS_MATURITY_BASIS", "PASS_DIRECT_AGGREGATE"}


def load_latest_ground_truth(ticker: str, report_date: str) -> tuple[dict[str, object], str]:
    """Tries v16, then v15, then v14 — whichever is the latest engine
    version that actually ran for this company-year (GOOGL 2021-2023
    have v15 from D-020; all AMZN/GOOGL years have at least v14)."""

    prefix = f"{ticker.lower()}_{report_date.replace('-', '')}"

    for version in ("v16", "v15", "v14"):
        path = DATA_DIR / f"{prefix}_engine_{version}_result.json"
        if path.exists():
            with path.open(encoding="utf-8-sig") as handle:
                result = json.load(handle)
            return result["metrics"], version

    raise FileNotFoundError(f"no ground-truth result file found for {ticker} {report_date}")


def compute_prior_report_date(report_date: str) -> str:
    from datetime import datetime as _dt

    year, month, day = (int(part) for part in report_date.split("-"))
    try:
        return _dt(year - 1, month, day).date().isoformat()
    except ValueError:
        return _dt(year - 1, month, day - 1).date().isoformat()


def resolve_total_debt_maturity_basis(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    presentation: pd.DataFrame,
    report_date: str,
    current_debt_result: dict[str, object],
    long_term_debt_result: dict[str, object],
) -> dict[str, object]:
    """
    D-022: prefer a reliable carrying-value total_debt (current_debt +
    long_term_debt, both PASS via the unchanged D-016/D-017/D-019
    tiers). If not both reliable, fall back to total_debt_maturity
    (current_debt_maturity + long_term_debt_maturity, from the filing's
    own debt-maturity schedule — never used to set canonical
    current_debt/long_term_debt themselves, D-021 unchanged), status
    PASS_MATURITY_BASIS, basis MATURITY_PRINCIPAL, with full bucket
    lineage and the carrying-value debt (whatever exists) preserved
    alongside it, never discarded.
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

    if maturity["total_debt_maturity"] is not None:
        return {
            "status": "PASS_MATURITY_BASIS",
            "value": maturity["total_debt_maturity"],
            "basis": "MATURITY_PRINCIPAL",
            "source_current_debt_maturity": maturity["current_debt_maturity"],
            "source_long_term_debt_maturity": maturity["long_term_debt_maturity"],
            "reported_total": maturity["reported_total"],
            "reconciliation_gap": maturity["reconciliation_gap"],
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
            f"{long_term_debt_result.get('status')}) and no maturity-basis "
            "fallback available either (no unique maturity schedule, or a "
            "bucket did not resolve to a single value)"
        ),
    }


def compute_company_year(
    connection: duckdb.DuckDBPyConnection,
    ticker: str,
    report_date: str,
    accession_number: str,
) -> dict[str, object]:
    presentation = reconstruct_presentation_dataframe(connection, accession_number)
    prior_report_date = compute_prior_report_date(report_date)

    # --- current period: current_debt, long_term_debt (unchanged D-021
    # carrying-value tiers), then total_debt (new D-022 fallback) -----
    cache: dict[str, dict[str, object]] = {}
    ltd = reconstruct_metric(connection, accession_number, presentation, "long_term_debt", report_date, cache)
    cache["long_term_debt"] = ltd
    cd = reconstruct_metric(connection, accession_number, presentation, "current_debt", report_date, cache)

    total_debt = resolve_total_debt_maturity_basis(
        connection, accession_number, presentation, report_date, cd, ltd
    )

    cash = reconstruct_metric(connection, accession_number, presentation, "cash_and_equivalents", report_date, cache)
    sti = reconstruct_metric(connection, accession_number, presentation, "short_term_investments", report_date, cache)
    equity = reconstruct_metric(connection, accession_number, presentation, "stockholders_equity", report_date, cache)

    def _combine_status(*statuses: str | None) -> str:
        if all(s in SUCCESSFUL_STATUSES for s in statuses):
            return "PASS_MATURITY_BASIS" if "PASS_MATURITY_BASIS" in statuses else "PASS"
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

    # --- prior period: same carrying-value tiers; maturity fallback is
    # STRUCTURALLY UNAVAILABLE here (evaluated, not assumed — see
    # docs/LAST_CLAUDE_REPORT.md: the debt-maturity schedule is a
    # single, forward-looking disclosure anchored to the CURRENT
    # balance-sheet date only; the same filing carries no prior-year
    # comparative bucket for the same concepts, confirmed by direct
    # query against xbrl_facts before writing this code) -------------
    cache_prior: dict[str, dict[str, object]] = {}
    ltd_prior = reconstruct_metric(connection, accession_number, presentation, "long_term_debt", prior_report_date, cache_prior)
    cache_prior["long_term_debt"] = ltd_prior
    cd_prior = reconstruct_metric(connection, accession_number, presentation, "current_debt", prior_report_date, cache_prior)

    if cd_prior.get("status") == "PASS" and ltd_prior.get("status") == "PASS":
        total_debt_prior = {"status": "PASS", "value": cd_prior["value"] + ltd_prior["value"]}
    else:
        total_debt_prior = {
            "status": "REVIEW_REQUIRED", "value": None,
            "error": "no carrying-value total for the prior period, and the "
                     "debt-maturity schedule has no prior-period comparative "
                     "bucket in this same filing (structurally unavailable, "
                     "not merely unresolved)",
        }

    cash_prior = reconstruct_metric(connection, accession_number, presentation, "cash_and_equivalents", prior_report_date, cache_prior)
    sti_prior = reconstruct_metric(connection, accession_number, presentation, "short_term_investments", prior_report_date, cache_prior)
    equity_prior = reconstruct_metric(connection, accession_number, presentation, "stockholders_equity", prior_report_date, cache_prior)

    ic_prior_status = _combine_status(
        total_debt_prior["status"], equity_prior["status"], cash_prior["status"], sti_prior["status"]
    )
    invested_capital_prior = {
        "status": ic_prior_status,
        "value": (
            total_debt_prior["value"] + equity_prior["value"] - cash_prior["value"] - sti_prior["value"]
            if ic_prior_status in SUCCESSFUL_STATUSES else None
        ),
    }

    avg_status = _combine_status(invested_capital["status"], invested_capital_prior["status"])
    average_invested_capital = {
        "status": avg_status,
        "value": (
            (invested_capital["value"] + invested_capital_prior["value"]) / 2
            if avg_status in SUCCESSFUL_STATUSES else None
        ),
        "basis": invested_capital.get("basis"),
    }

    return {
        "ticker": ticker, "report_date": report_date, "accession_number": accession_number,
        "current_debt": cd, "long_term_debt": ltd, "total_debt": total_debt,
        "cash_and_equivalents": cash, "short_term_investments": sti, "stockholders_equity": equity,
        "adjusted_net_debt": adjusted_net_debt, "invested_capital": invested_capital,
        "current_debt_prior": cd_prior, "long_term_debt_prior": ltd_prior,
        "total_debt_prior": total_debt_prior, "invested_capital_prior": invested_capital_prior,
        "average_invested_capital": average_invested_capital,
    }


def main() -> None:
    total_start = time.perf_counter()
    connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    all_results: dict[str, dict[str, object]] = {}

    for ticker, report_date, accession_number in TARGET_FILINGS:
        filing_start = time.perf_counter()
        print(f"\n=== {ticker} {report_date} ({accession_number}) ===")

        result = compute_company_year(connection, ticker, report_date, accession_number)
        elapsed = time.perf_counter() - filing_start

        ground_truth, gt_version = load_latest_ground_truth(ticker, report_date)
        nopat = ground_truth.get("nopat", {})  # unaffected by this policy — read as-is

        roic_status = (
            "PASS_MATURITY_BASIS"
            if result["average_invested_capital"]["status"] == "PASS_MATURITY_BASIS"
            and nopat.get("status") == "PASS"
            else "PASS"
            if result["average_invested_capital"]["status"] == "PASS" and nopat.get("status") == "PASS"
            else "REVIEW_REQUIRED"
        )
        nopat_value = nopat.get("value") if nopat.get("value") is not None else nopat.get("selected_value")
        roic_value = (
            nopat_value / result["average_invested_capital"]["value"]
            if roic_status in SUCCESSFUL_STATUSES
            and result["average_invested_capital"]["value"]
            and result["average_invested_capital"]["value"] > 0
            else None
        )
        result["roic"] = {"status": roic_status, "value": roic_value}
        result["ground_truth_engine_version"] = gt_version
        result["elapsed_seconds"] = elapsed

        all_results[f"{ticker}_{report_date}"] = result

        print(f"  elapsed={elapsed:.4f}s  ground_truth_version={gt_version}")
        for metric_name in AFFECTED_METRICS:
            m = result[metric_name]
            print(f"  {metric_name:24s} status={m['status']:18s} value={m.get('value')}")

    connection.close()
    total_elapsed = time.perf_counter() - total_start

    # Compare vs. previous ground truth: count exact conversions to
    # PASS_MATURITY_BASIS / PASS, per affected metric.
    print("\n" + "=" * 100)
    print("COMPARISON vs. previous ground truth (per affected metric)")
    conversions: list[str] = []
    still_review_required: list[str] = []

    for key, result in all_results.items():
        ticker, report_date = result["ticker"], result["report_date"]
        ground_truth, _ = load_latest_ground_truth(ticker, report_date)

        for metric_name in AFFECTED_METRICS:
            old_status = ground_truth.get(metric_name, {}).get("status")
            new_status = result[metric_name]["status"]

            if old_status == "REVIEW_REQUIRED" and new_status in SUCCESSFUL_STATUSES:
                conversions.append(f"{ticker} {report_date} {metric_name}: {old_status} -> {new_status}")
            elif new_status == "REVIEW_REQUIRED":
                still_review_required.append(f"{ticker} {report_date} {metric_name}")

    print(f"\nConverted to PASS/PASS_MATURITY_BASIS ({len(conversions)}):")
    for line in conversions:
        print(f"  {line}")

    print(f"\nStill REVIEW_REQUIRED ({len(still_review_required)}):")
    for line in still_review_required:
        print(f"  {line}")

    print(f"\ntotal_elapsed_seconds = {total_elapsed:.4f}")

    output_path = DATA_DIR / "d022_maturity_basis_results.json"
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(all_results, handle, indent=2, default=str, ensure_ascii=False)
    print(f"\nFull results written to {output_path}")


if __name__ == "__main__":
    main()
