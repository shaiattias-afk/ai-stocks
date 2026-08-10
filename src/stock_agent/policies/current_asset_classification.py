"""policies/current_asset_classification.py — resolve a balance-sheet row
that is ambiguous by LABEL but unambiguous by POSITION.

The problem, measured on Apple: its balance sheet carries two rows both
labelled exactly "Marketable securities" --

    parent us-gaap:AssetsCurrentAbstract      -> MarketableSecuritiesCurrent
    parent us-gaap:AssetsNoncurrentAbstract   -> MarketableSecuritiesNoncurrent

No label rule can separate them, because the labels are identical. The
engine was right to refuse: picking one at random would silently mix
long-dated holdings into a current-asset figure, corrupting invested
capital and net debt downstream.

The filer's own presentation structure settles it. This is the same
evidence D-019 already uses to classify debt as current or non-current
when the label carries no qualifier, applied to the asset side:
`us-gaap:AssetsCurrentAbstract` is the near-universal grouping concept
for the current-assets section, so a row beneath it IS current.

Fail-closed, as everywhere else: this narrows candidates by structural
evidence, and if that still leaves more than one, or none, it declines
rather than choosing.
"""

from __future__ import annotations

import re

import pandas as pd

# The standard grouping concept for the current-assets section, plus a
# label fallback for filers that use an extension concept for the header.
CURRENT_ASSETS_ANCESTOR_CONCEPT_PATTERN = r"AssetsCurrentAbstract|AssetsCurrent$"
CURRENT_ASSETS_ANCESTOR_LABEL_PATTERN = r"^\s*(?:total\s+)?current\s+assets\s*:?\s*$"

NONCURRENT_ASSETS_ANCESTOR_CONCEPT_PATTERN = r"AssetsNoncurrentAbstract|AssetsNoncurrent$"


def _ancestors(presentation: pd.DataFrame, row: pd.Series, max_depth: int = 12) -> list[pd.Series]:
    """Walks up the presentation tree from `row`, within its own role."""
    chain: list[pd.Series] = []
    role_rows = presentation[presentation["role_uri"] == row["role_uri"]]
    current = row
    for _ in range(max_depth):
        parent_qname = str(current.get("parent_qname") or "")
        if not parent_qname:
            break
        parents = role_rows[role_rows["concept_qname"].astype(str) == parent_qname]
        if parents.empty:
            # the section header may be abstract-only and appear solely as
            # a parent reference; record it and stop
            chain.append(pd.Series({"concept_qname": parent_qname, "label": "", "parent_qname": ""}))
            break
        current = parents.iloc[0]
        chain.append(current)
    return chain


def is_under_current_assets(presentation: pd.DataFrame, row: pd.Series) -> bool:
    """True when the row sits beneath the current-assets section."""
    if re.search(CURRENT_ASSETS_ANCESTOR_CONCEPT_PATTERN, str(row.get("parent_qname") or ""), re.IGNORECASE):
        return True
    for ancestor in _ancestors(presentation, row):
        concept = str(ancestor.get("concept_qname") or "")
        label = str(ancestor.get("label") or "")
        if re.search(NONCURRENT_ASSETS_ANCESTOR_CONCEPT_PATTERN, concept, re.IGNORECASE):
            return False
        if re.search(CURRENT_ASSETS_ANCESTOR_CONCEPT_PATTERN, concept, re.IGNORECASE):
            return True
        if re.search(CURRENT_ASSETS_ANCESTOR_LABEL_PATTERN, label, re.IGNORECASE):
            return True
    return False


def resolve_current_asset_row(
    presentation: pd.DataFrame,
    candidates: pd.DataFrame,
) -> dict[str, object] | None:
    """Narrows label-ambiguous candidates to the one in current assets.

    Returns the winning row as a dict, or None when the structure does not
    settle it -- never a guess. `None` leaves the caller's existing
    REVIEW_REQUIRED in place.
    """
    if candidates is None or candidates.empty:
        return None

    current_rows = [
        row for _, row in candidates.iterrows()
        if is_under_current_assets(presentation, row)
    ]
    if len(current_rows) != 1:
        return None

    winner = current_rows[0]
    return {
        "concept_qname": str(winner["concept_qname"]),
        "label": str(winner.get("label", "")),
        "selection_tier": "CURRENT_ASSETS_ANCESTRY",
        "basis": "AMBIGUOUS_LABEL_RESOLVED_BY_CURRENT_ASSETS_ANCESTRY",
    }


__all__ = [
    "CURRENT_ASSETS_ANCESTOR_CONCEPT_PATTERN",
    "is_under_current_assets",
    "resolve_current_asset_row",
]
