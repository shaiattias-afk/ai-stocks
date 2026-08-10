"""For every metric that failed with ROW_NOT_FOUND, show the labels that
DO exist in the correct statement, so patterns can be extended against
evidence rather than guessed at.

READ-ONLY. Prints candidate wording; changes nothing.

The output is the raw material for a vocabulary fix: each distinct label
below is a real phrase a real filer used for a concept the engine already
understands, that the current pattern does not recognise.

    .venv\\Scripts\\python.exe scripts\\187_diagnose_label_gaps.py
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

import duckdb

from stock_agent import DATA_DIR, WAREHOUSE_DB_PATH
from stock_agent.extraction.core import BUILT_IN_METRICS, reconstruct_presentation_dataframe

COVERAGE_PATH = DATA_DIR / "pilot_full_engine_coverage.json"

# concepts the engine already knows are the right answer for each metric,
# used only to FIND the row whose label we failed to recognise
KNOWN_CONCEPTS = {
    "capex": r"PaymentsToAcquireProductiveAssets|PaymentsToAcquirePropertyPlantAndEquipment",
    "operating_cash_flow": r"NetCashProvidedByUsedInOperatingActivities",
    "revenue": r"Revenues$|RevenueFromContractWithCustomer",
    "pretax_income": r"IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
    "short_term_investments": r"ShortTermInvestments|AvailableForSaleSecuritiesCurrent|MarketableSecuritiesCurrent",
}

TARGET_METRICS = list(KNOWN_CONCEPTS)


def main() -> None:
    coverage = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))

    failing: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row in coverage["results"]:
        for metric_name, result in row.get("metrics", {}).items():
            if metric_name in TARGET_METRICS and result.get("status") == "REVIEW_REQUIRED":
                failing[metric_name].append(
                    (row["ticker"], row["report_date"], row["accession_number"]))

    connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    try:
        for metric_name in TARGET_METRICS:
            cases = failing.get(metric_name, [])
            metric = BUILT_IN_METRICS[metric_name]
            print("=" * 100)
            print(f"{metric_name}  --  {len(cases)} failing company-years")
            print("=" * 100)
            print(f"current pattern: {getattr(metric, 'plain_pattern', None)!r}")
            print()

            labels: Counter = Counter()
            by_ticker: dict[str, set] = defaultdict(set)
            concept_pattern = KNOWN_CONCEPTS[metric_name]

            for ticker, report_date, accession_number in cases:
                try:
                    presentation = reconstruct_presentation_dataframe(connection, accession_number)
                except Exception:  # noqa: BLE001
                    continue
                hits = presentation[
                    presentation["concept_qname"].astype(str).str.contains(
                        concept_pattern, case=False, regex=True, na=False)
                    & (~presentation["is_abstract"].astype(bool))
                ]
                for _, r in hits.iterrows():
                    role = str(r["role_definition"])
                    if not re.search(r"statement", role, re.IGNORECASE):
                        continue
                    label = str(r["label"]).strip()
                    labels[label] += 1
                    by_ticker[ticker].add(label)

            if not labels:
                print("   (no row found carrying a known concept in a Statement role --")
                print("    this metric's gap is NOT simple label wording)")
            else:
                print("   distinct labels found on the right concept, by frequency:")
                for label, count in labels.most_common(12):
                    print(f"      {count:>3}x  {label[:82]}")
                print()
                print("   by company:")
                for ticker in sorted(by_ticker):
                    for label in sorted(by_ticker[ticker])[:3]:
                        print(f"      {ticker:<6} {label[:76]}")
            print()
    finally:
        connection.close()


if __name__ == "__main__":
    main()
