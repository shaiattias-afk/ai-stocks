from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pandas as pd

from arelle import XbrlConst
from arelle.RuntimeOptions import RuntimeOptions
from arelle.api.Session import Session


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

FACTS_FILE = (
    DATA_DIR
    / "orcl_2024_inline_xbrl_facts.csv"
)

OUTPUT_FILE = (
    DATA_DIR
    / "orcl_2024_arelle_presentation.csv"
)

LOG_FILE = (
    DATA_DIR
    / "orcl_2024_arelle.log"
)


def load_exact_source_file() -> Path:
    """
    Uses the source_file already recorded during the successful
    Oracle Inline XBRL extraction.

    This prevents the script from searching directories or choosing
    a filing by file size.
    """

    if not FACTS_FILE.exists():
        raise FileNotFoundError(
            "קובץ העובדות של Oracle לא נמצא:\n"
            f"{FACTS_FILE}"
        )

    facts = pd.read_csv(
        FACTS_FILE,
        dtype=str,
        keep_default_na=False,
        usecols=["source_file"],
    )

    source_files = [
        value.strip()
        for value in facts["source_file"].unique()
        if value.strip()
    ]

    if len(source_files) != 1:
        raise RuntimeError(
            "לא נמצא קובץ מקור יחיד וברור.\n"
            f"מספר קובצי מקור שנמצאו: {len(source_files)}\n"
            "הסקריפט נעצר ולא בוחר קובץ בניחוש."
        )

    source_file = Path(source_files[0])

    if not source_file.is_absolute():
        source_file = PROJECT_DIR / source_file

    source_file = source_file.resolve()

    if not source_file.exists():
        raise FileNotFoundError(
            "קובץ ה־10-K שנרשם כמקור אינו קיים:\n"
            f"{source_file}"
        )

    return source_file


def safe_label(
    concept: Any,
    role: str | None = None,
) -> str:
    try:
        label = concept.label(
            preferredLabel=role,
            lang="en-US",
            fallbackToQname=True,
        )

        if label:
            return str(label)
    except Exception:
        pass

    try:
        label = concept.label(
            lang="en-US",
            fallbackToQname=True,
        )

        if label:
            return str(label)
    except Exception:
        pass

    try:
        return str(concept.qname)
    except Exception:
        return ""


def get_role_definition(
    model_xbrl: Any,
    role_uri: str,
) -> str:
    role_types = model_xbrl.roleTypes.get(
        role_uri,
        [],
    )

    for role_type in role_types:
        definition = getattr(
            role_type,
            "definition",
            "",
        )

        if definition:
            return str(definition)

    return ""


def walk_presentation_tree(
    relationship_set: Any,
    role_uri: str,
    role_definition: str,
    concept: Any,
    records: list[dict[str, object]],
    depth: int,
    parent_qname: str,
    preferred_label: str,
    visited_path: set[str],
) -> None:
    concept_qname = str(
        getattr(
            concept,
            "qname",
            "",
        )
    )

    path_key = (
        f"{role_uri}|{parent_qname}|"
        f"{concept_qname}|{depth}"
    )

    if path_key in visited_path:
        return

    visited_path.add(path_key)

    label = safe_label(
        concept,
        role=preferred_label or None,
    )

    records.append(
        {
            "role_uri": role_uri,
            "role_definition": role_definition,
            "depth": depth,
            "parent_qname": parent_qname,
            "concept_qname": concept_qname,
            "concept_name": str(
                getattr(
                    concept,
                    "name",
                    "",
                )
            ),
            "label": label,
            "preferred_label_role": preferred_label,
            "is_abstract": bool(
                getattr(
                    concept,
                    "isAbstract",
                    False,
                )
            ),
            "period_type": str(
                getattr(
                    concept,
                    "periodType",
                    "",
                )
            ),
            "balance": str(
                getattr(
                    concept,
                    "balance",
                    "",
                )
                or ""
            ),
        }
    )

    relationships = relationship_set.fromModelObject(
        concept
    )

    relationships = sorted(
        relationships,
        key=lambda relationship: (
            float(
                getattr(
                    relationship,
                    "order",
                    0,
                )
                or 0
            ),
            str(
                getattr(
                    relationship.toModelObject,
                    "qname",
                    "",
                )
            ),
        ),
    )

    for relationship in relationships:
        child = relationship.toModelObject

        if child is None:
            continue

        child_preferred_label = str(
            getattr(
                relationship,
                "preferredLabel",
                "",
            )
            or ""
        )

        walk_presentation_tree(
            relationship_set=relationship_set,
            role_uri=role_uri,
            role_definition=role_definition,
            concept=child,
            records=records,
            depth=depth + 1,
            parent_qname=concept_qname,
            preferred_label=child_preferred_label,
            visited_path=visited_path,
        )


def extract_presentation(
    model_xbrl: Any,
) -> pd.DataFrame:
    records: list[dict[str, object]] = []

    presentation_set = model_xbrl.relationshipSet(
        XbrlConst.parentChild
    )

    role_uris = sorted(
        presentation_set.linkRoleUris
    )

    for role_uri in role_uris:
        role_relationship_set = (
            model_xbrl.relationshipSet(
                XbrlConst.parentChild,
                role_uri,
            )
        )

        role_definition = get_role_definition(
            model_xbrl=model_xbrl,
            role_uri=role_uri,
        )

        roots = sorted(
            role_relationship_set.rootConcepts,
            key=lambda concept: str(
                getattr(
                    concept,
                    "qname",
                    "",
                )
            ),
        )

        for root in roots:
            walk_presentation_tree(
                relationship_set=role_relationship_set,
                role_uri=role_uri,
                role_definition=role_definition,
                concept=root,
                records=records,
                depth=0,
                parent_qname="",
                preferred_label="",
                visited_path=set(),
            )

    return pd.DataFrame(records)


def print_relevant_roles(
    presentation: pd.DataFrame,
) -> None:
    if presentation.empty:
        print(
            "לא נמצאו Presentation relationships."
        )
        return

    role_summary = (
        presentation.groupby(
            [
                "role_uri",
                "role_definition",
            ],
            dropna=False,
        )
        .agg(
            concept_count=(
                "concept_qname",
                "count",
            ),
            maximum_depth=(
                "depth",
                "max",
            ),
        )
        .reset_index()
    )

    search_text = (
        role_summary["role_definition"]
        + " "
        + role_summary["role_uri"]
    ).str.lower()

    relevant = role_summary[
        search_text.str.contains(
            (
                "income|operations|earnings|"
                "revenue|statement"
            ),
            regex=True,
            na=False,
        )
    ].copy()

    print()
    print("=" * 120)
    print("Presentation Roles relevant to income statement")
    print("=" * 120)

    if relevant.empty:
        print(
            "לא נמצא Role שהכותרת שלו מכילה "
            "Income, Operations, Earnings או Revenue."
        )

        print()
        print(
            "כל ה־Roles נשמרו בקובץ CSV "
            "לבדיקה מלאה."
        )

        return

    print(
        relevant.to_string(
            index=False
        )
    )

    relevant_role_uris = set(
        relevant["role_uri"]
    )

    relevant_rows = presentation[
        presentation["role_uri"].isin(
            relevant_role_uris
        )
    ].copy()

    revenue_rows = relevant_rows[
        (
            relevant_rows["label"]
            + " "
            + relevant_rows["concept_qname"]
        )
        .str.contains(
            "revenue|sales",
            case=False,
            regex=True,
            na=False,
        )
    ].copy()

    print()
    print("=" * 120)
    print("Revenue / Sales concepts inside relevant Roles")
    print("=" * 120)

    if revenue_rows.empty:
        print(
            "לא נמצאו שורות Revenue או Sales "
            "בתוך ה־Roles הרלוונטיים."
        )
        return

    display_columns = [
        "role_definition",
        "depth",
        "parent_qname",
        "concept_qname",
        "label",
        "is_abstract",
        "period_type",
    ]

    print(
        revenue_rows[
            display_columns
        ].to_string(
            index=False
        )
    )


def main() -> None:
    source_file = load_exact_source_file()

    print()
    print("=" * 120)
    print("ARELLE — ORACLE 2024 PRESENTATION TEST")
    print("=" * 120)

    print(
        f"קובץ 10-K מדויק:\n{source_file}"
    )

    options = RuntimeOptions(
        entrypointFile=str(source_file),
        internetConnectivity="online",
        keepOpen=True,
        logFile=str(LOG_FILE),
        logFormat=(
            "[%(levelname)s] "
            "[%(messageCode)s] "
            "%(message)s - %(file)s"
        ),
    )

    with Session() as session:
        session.run(options)

        models = session.get_models()

        if len(models) != 1:
            raise RuntimeError(
                "Arelle לא החזיר מודל יחיד וברור.\n"
                f"מספר מודלים: {len(models)}"
            )

        model_xbrl = models[0]

        if model_xbrl is None:
            raise RuntimeError(
                "Arelle לא הצליח לטעון את מודל ה־XBRL."
            )

        presentation = extract_presentation(
            model_xbrl
        )

    if presentation.empty:
        raise RuntimeError(
            "Arelle טען את הדוח אך לא נמצאו "
            "Presentation relationships."
        )

    presentation.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
        quoting=csv.QUOTE_MINIMAL,
    )

    print(
        f"\nמספר שורות Presentation: "
        f"{len(presentation):,}"
    )

    print(
        "מספר Roles ייחודיים: "
        f"{presentation['role_uri'].nunique():,}"
    )

    print_relevant_roles(
        presentation
    )

    print()
    print("=" * 120)
    print(
        f"קובץ התוצאה נשמר כאן:\n"
        f"{OUTPUT_FILE}"
    )

    print()
    print(
        f"לוג Arelle נשמר כאן:\n"
        f"{LOG_FILE}"
    )

    print()
    print(
        "בשלב זה לא נבחר Revenue ולא בוצע Mapping."
    )


if __name__ == "__main__":
    main()