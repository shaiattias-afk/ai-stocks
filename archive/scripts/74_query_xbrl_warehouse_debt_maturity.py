"""
DuckDB-ONLY analysis of the debt-maturity disclosure — no Arelle import
anywhere in this file, no XBRL model of any kind opened. Proves the
central claim of the warehouse proof (scripts/73): that debt
classification evidence can be reconstructed entirely from the stored,
structured XBRL warehouse (data/database/xbrl_warehouse_proof.duckdb),
without ever reopening the original filing in Arelle.

Read-only with respect to everything: does not write to the warehouse,
does not touch the production database
(data/database/ai_stock_agent.duckdb), does not compute or change any
canonical metric (current_debt or otherwise) — this is evidence
reconstruction only, the same evidence-gathering step
attempt_current_debt_from_maturity_bucket() (scripts/72, D-021 proof)
already performs live against Arelle, reimplemented here purely as SQL/
pandas over the warehouse tables, to prove the two are informationally
equivalent for this purpose.

No ticker- or year-specific logic: every pattern below (role search,
non-debt exclusion, principal-vs-carrying-value concept check) is the
exact same GENERIC pattern already used in scripts/72 — reproduced here
against warehouse tables, not hand-tuned to this one filing's actual
labels/concepts.

Run twice in `main()` to demonstrate reproducibility (task requirement
9): both runs query the same static, already-built warehouse and must
produce byte-identical results.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"

TARGET_ACCESSION = "0001018724-25-000004"  # AMZN, report date 2024-12-31

STANDARD_LABEL_ROLE = "http://www.xbrl.org/2003/role/label"

# Identical patterns to scripts/72_xbrl_metric_engine.py (D-017/D-021) —
# reproduced here to prove the SAME evidence and the SAME classification
# logic can run purely against warehouse tables. Not redefined
# differently, not loosened, not tightened.
DEBT_DISCLOSURE_ROLE_PATTERN = r"debt|notes?\s+payable|borrowings?"
DEBT_MATURITY_ROLE_PATTERN = r"maturit|future\s+principal\s+payments?"
DEBT_MATURITY_ROLE_EXCLUDE_PATTERN = r"marketable|available.for.sale|investment"
NON_DEBT_MATURITY_EXCLUDE_PATTERN = (
    r"lease|purchase\s+obligation|interest\s+payment|interest\s+expense|"
    r"commitment"
)
CARRYING_VALUE_REJECT_CONCEPT_PATTERN = r"principal|face(?:value|amount)"


def find_debt_maturity_schedule_role(
    connection: duckdb.DuckDBPyConnection, accession_number: str
) -> str | None:
    """
    Identical selection rule to scripts/72's find_debt_maturity_schedule_
    role — run here entirely as a DuckDB query, not an Arelle-model walk.
    Returns None (not an error) if zero or more than one candidate role
    exists, exactly as the production logic does — never guesses.
    """

    roles = connection.execute(
        """
        SELECT DISTINCT role_uri, role_definition
        FROM xbrl_roles
        WHERE accession_number = ?
          AND relationship_type = 'presentation'
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

    candidates = sorted(
        roles.loc[is_debt & is_maturity & ~is_excluded, "role_uri"].unique()
    )

    if len(candidates) != 1:
        return None

    return candidates[0]


def build_ancestor_chain_from_warehouse(
    connection: duckdb.DuckDBPyConnection,
    accession_number: str,
    role_uri: str,
    concept_qname: str,
) -> list[dict[str, str]]:
    """
    Same generic parent-chain walk as scripts/72's build_ancestor_chain,
    reimplemented purely against the stored xbrl_presentation_
    relationships edge table — no Arelle, no live presentation tree.
    """

    edges = connection.execute(
        """
        SELECT parent_concept, child_concept
        FROM xbrl_presentation_relationships
        WHERE accession_number = ? AND role_uri = ?
        """,
        [accession_number, role_uri],
    ).fetchdf()

    parent_by_child = dict(
        zip(edges["child_concept"], edges["parent_concept"])
    )

    label_by_concept = _label_lookup(connection, accession_number)

    chain: list[dict[str, str]] = []
    current = concept_qname
    visited: set[str] = set()

    while True:
        parent = parent_by_child.get(current)

        if not parent or parent in visited:
            break

        visited.add(parent)
        chain.append(
            {
                "concept_qname": parent,
                "label": label_by_concept.get(parent, parent),
            }
        )
        current = parent

    return chain


def _label_lookup(
    connection: duckdb.DuckDBPyConnection, accession_number: str
) -> dict[str, str]:
    labels = connection.execute(
        """
        SELECT concept_qname, label_text
        FROM xbrl_labels
        WHERE accession_number = ?
          AND label_role = ?
          AND language IN ('en-US', 'en')
        """,
        [accession_number, STANDARD_LABEL_ROLE],
    ).fetchdf()

    lookup = dict(zip(labels["concept_qname"], labels["label_text"]))

    # Fallback: any label at all, for a concept the standard-role query
    # missed (e.g. a language variant) — never leaves a concept with no
    # displayable text if ANY label was preserved for it.
    fallback = connection.execute(
        """
        SELECT concept_qname, label_text
        FROM xbrl_labels
        WHERE accession_number = ?
        """,
        [accession_number],
    ).fetchdf()

    for concept_qname, label_text in zip(
        fallback["concept_qname"], fallback["label_text"]
    ):
        lookup.setdefault(concept_qname, label_text)

    return lookup


def classify_bucket_row(label: str, concept_qname: str) -> dict[str, object]:
    """
    Same classification logic as scripts/72's
    attempt_current_debt_from_maturity_bucket, reimplemented here against
    warehouse-derived label/concept strings only — no Arelle object,
    just the two plain strings the warehouse already stores.
    """

    if re.search(
        NON_DEBT_MATURITY_EXCLUDE_PATTERN, label, re.IGNORECASE
    ) or re.search(
        NON_DEBT_MATURITY_EXCLUDE_PATTERN, concept_qname, re.IGNORECASE
    ):
        return {
            "category": "excluded_non_debt",
            "reason": (
                "מזוהה כהתחייבות שאינה חוב נושא-ריבית (חכירה / התחייבות "
                "רכש / תשלום ריבית / התחייבות חוזית אחרת)."
            ),
        }

    concept_local_name = concept_qname.split(":")[-1]

    if re.search(
        CARRYING_VALUE_REJECT_CONCEPT_PATTERN, concept_local_name, re.IGNORECASE
    ):
        return {
            "category": "debt_principal_not_carrying_value",
            "reason": (
                "ה-concept מייצג במפורש סכום קרן (Principal) לא-מהוון, "
                "לא שווי בספרים (GAAP Carrying Amount)."
            ),
        }

    return {
        "category": "debt_carrying_value_candidate",
        "reason": (
            "אינו Principal/Face Value ואינו התחייבות שאינה-חוב — מועמד "
            "לשווי בספרים."
        ),
    }


def run_debt_maturity_analysis(
    connection: duckdb.DuckDBPyConnection, accession_number: str
) -> dict[str, object]:
    """
    The complete DuckDB-only debt-maturity evidence reconstruction —
    role location, candidate rows, labels, contexts, units, dimensions,
    parent chains, and principal-vs-carrying-value/non-debt
    classification. Deterministic: calling this twice against the same
    static warehouse must produce identical output (task requirement 9).
    """

    role_uri = find_debt_maturity_schedule_role(connection, accession_number)

    if role_uri is None:
        return {"role_uri": None, "candidates": []}

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
        SELECT parent_concept, child_concept, order_value, preferred_label,
               depth
        FROM xbrl_presentation_relationships
        WHERE accession_number = ? AND role_uri = ?
        ORDER BY order_value, child_concept
        """,
        [accession_number, role_uri],
    ).fetchdf()

    concepts = connection.execute(
        """
        SELECT qname, is_abstract, balance_type, period_type, is_extension
        FROM xbrl_concepts
        WHERE accession_number = ?
        """,
        [accession_number],
    ).fetchdf()
    concept_by_qname = concepts.set_index("qname").to_dict(orient="index")

    label_by_concept = _label_lookup(connection, accession_number)

    facts = connection.execute(
        """
        SELECT concept_qname, value_raw, value_numeric, context_id,
               unit_id, period_type, instant_date, dimensions_json, is_nil
        FROM xbrl_facts
        WHERE accession_number = ?
        """,
        [accession_number],
    ).fetchdf()

    non_abstract_rows = []

    for _, edge in edges.iterrows():
        child = edge["child_concept"]
        info = concept_by_qname.get(child, {})

        if info.get("is_abstract"):
            continue

        label = label_by_concept.get(child, child)

        if re.match(r"^\s*total\b", label, re.IGNORECASE):
            row_kind = "total"
        else:
            row_kind = "component"

        matching_facts = facts[facts["concept_qname"] == child]

        classification = classify_bucket_row(label, child)
        ancestor_chain = build_ancestor_chain_from_warehouse(
            connection, accession_number, role_uri, child
        )

        for _, fact in matching_facts.iterrows():
            non_abstract_rows.append(
                {
                    "role_uri": role_uri,
                    "role_definition": role_definition,
                    "row_kind": row_kind,
                    "presentation_order": edge["order_value"],
                    "label": label,
                    "concept_qname": child,
                    "is_extension_concept": bool(info.get("is_extension")),
                    "balance_type": info.get("balance_type"),
                    "period_type": info.get("period_type"),
                    "value_raw": fact["value_raw"],
                    "value_numeric": fact["value_numeric"],
                    "context_id": fact["context_id"],
                    "unit_id": fact["unit_id"],
                    "instant_date": fact["instant_date"],
                    "dimensions_json": fact["dimensions_json"],
                    "is_nil": bool(fact["is_nil"]),
                    "parent_chain": ancestor_chain,
                    "classification_category": classification["category"],
                    "classification_reason": classification["reason"],
                }
            )

    non_abstract_rows.sort(key=lambda r: (r["presentation_order"], r["concept_qname"]))

    earliest_bucket = next(
        (r for r in non_abstract_rows if r["row_kind"] == "component"),
        None,
    )

    return {
        "role_uri": role_uri,
        "role_definition": role_definition,
        "candidates": non_abstract_rows,
        "earliest_bucket": earliest_bucket,
    }


def main() -> None:
    connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)

    print(f"מסד נתונים (קריאה בלבד): {WAREHOUSE_DB_PATH}")
    print(f"Accession: {TARGET_ACCESSION}")
    print()

    # --- run 1 -------------------------------------------------------
    run1_start = time.perf_counter()
    result_1 = run_debt_maturity_analysis(connection, TARGET_ACCESSION)
    run1_seconds = time.perf_counter() - run1_start

    # --- run 2 (reproducibility check, task requirement 9) -----------
    run2_start = time.perf_counter()
    result_2 = run_debt_maturity_analysis(connection, TARGET_ACCESSION)
    run2_seconds = time.perf_counter() - run2_start

    connection.close()

    identical = json.dumps(result_1, sort_keys=True, default=str) == json.dumps(
        result_2, sort_keys=True, default=str
    )

    print(f"run_1_seconds = {run1_seconds:.6f}")
    print(f"run_2_seconds = {run2_seconds:.6f}")
    print(f"REPRODUCIBILITY (run1 == run2): {identical}")
    print()

    print(f"maturity schedule role: {result_1['role_uri']}")
    print(f"role definition: {result_1.get('role_definition')}")
    print()

    print("=== ALL CANDIDATE ROWS ===")
    for row in result_1["candidates"]:
        print(
            f"[{row['row_kind']:9s}] order={row['presentation_order']:<4} "
            f"label='{row['label']}' concept={row['concept_qname']} "
            f"ext={row['is_extension_concept']} balance={row['balance_type']} "
            f"value={row['value_numeric']} unit={row['unit_id']} "
            f"context={row['context_id']} instant={row['instant_date']} "
            f"dims={row['dimensions_json']}"
        )
        print(
            f"    classification: {row['classification_category']} — "
            f"{row['classification_reason']}"
        )
        print(
            "    parent_chain: "
            + " -> ".join(a["label"] for a in row["parent_chain"])
        )

    print()
    earliest = result_1["earliest_bucket"]

    if earliest is not None:
        print("=== EARLIEST BUCKET (first non-abstract, non-Total row) ===")
        print(json.dumps(earliest, indent=2, ensure_ascii=False, default=str))
        print()
        print(
            "current_debt classifiable from warehouse alone (DuckDB-only, "
            "no Arelle): "
            + (
                "YES for CLASSIFICATION (principal vs. carrying-value vs. "
                "non-debt is fully determinable from stored concept/label/"
                "role/context/unit data) — NO for VALUE EXTRACTION under "
                "this policy (the concept itself is a principal amount, "
                "not a GAAP carrying amount, exactly as scripts/72's live-"
                "Arelle proof found)."
                if earliest["classification_category"]
                == "debt_principal_not_carrying_value"
                else "YES — see classification_category above."
            )
        )

    print()
    print(f"row_counts: candidates={len(result_1['candidates'])}")


if __name__ == "__main__":
    main()
