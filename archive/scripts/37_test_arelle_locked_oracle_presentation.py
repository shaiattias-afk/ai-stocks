from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd

from arelle import XbrlConst
from arelle.RuntimeOptions import RuntimeOptions
from arelle.api.Session import Session


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

LOCKED_FILINGS_DIR = (
    DATA_DIR
    / "sec_filings_locked"
    / "ORCL"
)

OUTPUT_FILE = (
    DATA_DIR
    / "orcl_2024_arelle_locked_presentation.csv"
)

LOG_FILE = (
    DATA_DIR
    / "orcl_2024_arelle_locked.log"
)


def find_locked_manifest() -> Path:
    manifests = sorted(
        LOCKED_FILINGS_DIR.glob(
            "*/locked_filing_manifest.json"
        )
    )

    if len(manifests) != 1:
        raise RuntimeError(
            "לא נמצא Manifest יחיד וברור.\n"
            f"מספר Manifests: {len(manifests)}"
        )

    return manifests[0]


def load_primary_document() -> Path:
    manifest_file = find_locked_manifest()

    manifest = json.loads(
        manifest_file.read_text(
            encoding="utf-8"
        )
    )

    report_date = str(
        manifest.get(
            "report_date",
            "",
        )
    )

    if report_date != "2024-05-31":
        raise RuntimeError(
            "ה־Manifest אינו של Oracle 2024.\n"
            f"Report date שנמצא: {report_date}"
        )

    primary_document_path = Path(
        manifest.get(
            "primary_document_path",
            "",
        )
    )

    if not primary_document_path.exists():
        raise FileNotFoundError(
            "קובץ ה־10-K הראשי לא נמצא:\n"
            f"{primary_document_path}"
        )

    return primary_document_path.resolve()


def safe_label(
    concept: Any,
    preferred_label: str | None = None,
) -> str:
    try:
        label = concept.label(
            preferredLabel=preferred_label,
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

    return str(
        getattr(
            concept,
            "qname",
            "",
        )
    )


def role_definition(
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


def walk_tree(
    relationship_set: Any,
    role_uri: str,
    role_name: str,
    concept: Any,
    records: list[dict[str, object]],
    depth: int,
    parent_qname: str,
    preferred_label: str,
    visited: set[tuple[str, str, int]],
) -> None:
    concept_qname = str(
        getattr(
            concept,
            "qname",
            "",
        )
    )

    visit_key = (
        parent_qname,
        concept_qname,
        depth,
    )

    if visit_key in visited:
        return

    visited.add(visit_key)

    records.append(
        {
            "role_uri": role_uri,
            "role_definition": role_name,
            "depth": depth,
            "parent_qname": parent_qname,
            "concept_qname": concept_qname,
            "label": safe_label(
                concept,
                preferred_label or None,
            ),
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

    relationships = (
        relationship_set
        .fromModelObject(concept)
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

        walk_tree(
            relationship_set=relationship_set,
            role_uri=role_uri,
            role_name=role_name,
            concept=child,
            records=records,
            depth=depth + 1,
            parent_qname=concept_qname,
            preferred_label=child_preferred_label,
            visited=visited,
        )


def extract_presentation(
    model_xbrl: Any,
) -> pd.DataFrame:
    records: list[dict[str, object]] = []

    global_relationship_set = (
        model_xbrl.relationshipSet(
            XbrlConst.parentChild
        )
    )

    for role_uri in sorted(
        global_relationship_set.linkRoleUris
    ):
        relationship_set = (
            model_xbrl.relationshipSet(
                XbrlConst.parentChild,
                role_uri,
            )
        )

        definition = role_definition(
            model_xbrl,
            role_uri,
        )

        roots = sorted(
            relationship_set.rootConcepts,
            key=lambda concept: str(
                getattr(
                    concept,
                    "qname",
                    "",
                )
            ),
        )

        for root in roots:
            walk_tree(
                relationship_set=relationship_set,
                role_uri=role_uri,
                role_name=definition,
                concept=root,
                records=records,
                depth=0,
                parent_qname="",
                preferred_label="",
                visited=set(),
            )

    return pd.DataFrame(records)


def print_revenue_candidates(
    presentation: pd.DataFrame,
) -> None:
    combined_text = (
        presentation["role_definition"]
        + " "
        + presentation["label"]
        + " "
        + presentation["concept_qname"]
    )

    candidates = presentation[
        combined_text.str.contains(
            "revenue|sales",
            case=False,
            regex=True,
            na=False,
        )
    ].copy()

    print()
    print("=" * 120)
    print("Revenue / Sales candidates")
    print("=" * 120)

    if candidates.empty:
        print(
            "לא נמצאו מועמדים ל־Revenue או Sales."
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
        candidates[
            display_columns
        ].to_string(
            index=False
        )
    )


def main() -> None:
    primary_document = (
        load_primary_document()
    )

    print()
    print("=" * 120)
    print(
        "ARELLE — LOCKED ORACLE 2024 PRESENTATION"
    )
    print("=" * 120)

    print(
        f"קובץ 10-K:\n"
        f"{primary_document}"
    )

    options = RuntimeOptions(
        entrypointFile=str(
            primary_document
        ),
        internetConnectivity="offline",
        keepOpen=True,
        logFile=str(LOG_FILE),
    )

    with Session() as session:
        session.run(options)

        models = session.get_models()

        if len(models) != 1:
            raise RuntimeError(
                "Arelle לא החזיר מודל יחיד.\n"
                f"מספר מודלים: {len(models)}"
            )

        model_xbrl = models[0]

        presentation = (
            extract_presentation(
                model_xbrl
            )
        )

    if presentation.empty:
        raise RuntimeError(
            "לא נמצאו Presentation relationships."
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

    print_revenue_candidates(
        presentation
    )

    print()
    print(
        f"קובץ התוצאה נשמר כאן:\n"
        f"{OUTPUT_FILE}"
    )

    print()
    print(
        f"לוג Arelle נשמר כאן:\n"
        f"{LOG_FILE}"
    )


if __name__ == "__main__":
    main()