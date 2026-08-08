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

CACHE_DIR = (
    DATA_DIR
    / "arelle_cache"
)

OUTPUT_FILE = (
    DATA_DIR
    / "orcl_2024_arelle_online_presentation.csv"
)

LOG_FILE = (
    DATA_DIR
    / "orcl_2024_arelle_online.log"
)


def find_locked_manifest() -> Path:
    manifests = sorted(
        LOCKED_FILINGS_DIR.glob(
            "*/locked_filing_manifest.json"
        )
    )

    matching_manifests = []

    for manifest_file in manifests:
        manifest = json.loads(
            manifest_file.read_text(
                encoding="utf-8"
            )
        )

        if (
            manifest.get("report_date")
            == "2024-05-31"
            and manifest.get("form")
            == "10-K"
        ):
            matching_manifests.append(
                manifest_file
            )

    if len(matching_manifests) != 1:
        raise RuntimeError(
            "לא נמצא Manifest יחיד של "
            "Oracle 2024.\n"
            f"מספר התאמות: "
            f"{len(matching_manifests)}"
        )

    return matching_manifests[0]


def load_primary_document() -> Path:
    manifest_file = find_locked_manifest()

    manifest = json.loads(
        manifest_file.read_text(
            encoding="utf-8"
        )
    )

    primary_document_path = Path(
        manifest["primary_document_path"]
    ).resolve()

    if not primary_document_path.exists():
        raise FileNotFoundError(
            "קובץ ה־10-K הראשי לא נמצא:\n"
            f"{primary_document_path}"
        )

    return primary_document_path


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


def walk_tree(
    relationship_set: Any,
    role_uri: str,
    role_definition: str,
    concept: Any,
    records: list[dict[str, object]],
    depth: int,
    parent_qname: str,
    preferred_label: str,
    visited: set[
        tuple[str, str, int]
    ],
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
            "role_definition":
                role_definition,
            "depth": depth,
            "parent_qname":
                parent_qname,
            "concept_qname":
                concept_qname,
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
        child = (
            relationship.toModelObject
        )

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
            relationship_set=(
                relationship_set
            ),
            role_uri=role_uri,
            role_definition=(
                role_definition
            ),
            concept=child,
            records=records,
            depth=depth + 1,
            parent_qname=(
                concept_qname
            ),
            preferred_label=(
                child_preferred_label
            ),
            visited=visited,
        )


def extract_presentation(
    model_xbrl: Any,
) -> pd.DataFrame:
    records: list[
        dict[str, object]
    ] = []

    global_relationship_set = (
        model_xbrl.relationshipSet(
            XbrlConst.parentChild
        )
    )

    role_uris = sorted(
        global_relationship_set
        .linkRoleUris
    )

    for role_uri in role_uris:
        relationship_set = (
            model_xbrl.relationshipSet(
                XbrlConst.parentChild,
                role_uri,
            )
        )

        definition = (
            get_role_definition(
                model_xbrl,
                role_uri,
            )
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
                relationship_set=(
                    relationship_set
                ),
                role_uri=role_uri,
                role_definition=(
                    definition
                ),
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
        presentation[
            "role_definition"
        ].fillna("")
        + " "
        + presentation[
            "label"
        ].fillna("")
        + " "
        + presentation[
            "concept_qname"
        ].fillna("")
    )

    candidates = presentation[
        combined_text.str.contains(
            "revenue|revenues|sales",
            case=False,
            regex=True,
            na=False,
        )
    ].copy()

    print()
    print("=" * 125)
    print(
        "Revenue / Sales candidates"
    )
    print("=" * 125)

    if candidates.empty:
        print(
            "לא נמצאו מועמדים גם לאחר "
            "טעינת ה־taxonomy במצב Online."
        )
        return

    print(
        candidates[
            [
                "role_definition",
                "depth",
                "parent_qname",
                "concept_qname",
                "label",
                "is_abstract",
                "period_type",
            ]
        ].to_string(
            index=False
        )
    )


def main() -> None:
    primary_document = (
        load_primary_document()
    )

    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print("=" * 125)
    print(
        "ARELLE — ORACLE 2024 ONLINE "
        "TAXONOMY LOAD"
    )
    print("=" * 125)

    print(
        f"קובץ 10-K:\n"
        f"{primary_document}"
    )

    print()
    print(
        f"תיקיית Cache:\n"
        f"{CACHE_DIR.resolve()}"
    )

    print()
    print(
        "Arelle טוען כעת את ה־taxonomy. "
        "בהרצה הראשונה התהליך עשוי "
        "להימשך מספר דקות."
    )

    options = RuntimeOptions(
        entrypointFile=str(
            primary_document
        ),
        internetConnectivity="online",
        cacheDirectory=str(
            CACHE_DIR.resolve()
        ),
        keepOpen=True,
        logFile=str(
            LOG_FILE.resolve()
        ),
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
                "Arelle לא החזיר מודל "
                "יחיד וברור.\n"
                f"מספר מודלים: "
                f"{len(models)}"
            )

        model_xbrl = models[0]

        presentation = (
            extract_presentation(
                model_xbrl
            )
        )

    if presentation.empty:
        raise RuntimeError(
            "לא נמצאו Presentation "
            "relationships לאחר טעינת "
            "ה־taxonomy."
        )

    presentation.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
        quoting=csv.QUOTE_MINIMAL,
    )

    print()
    print(
        "מספר שורות Presentation: "
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
        f"{OUTPUT_FILE.resolve()}"
    )

    print()
    print(
        f"לוג Arelle נשמר כאן:\n"
        f"{LOG_FILE.resolve()}"
    )


if __name__ == "__main__":
    main()