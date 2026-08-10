"""Why do some companies fail almost every metric? Distinguishes a
SYSTEMATIC fault (the engine cannot find a whole statement) from a long
tail of vocabulary gaps.

A company scoring 0 of 100 is not a wording problem. Constellation
Energy scores exactly that; Exelon 8%. Both utilities. If the income
statement or balance sheet role is simply not being located for these
filers, one fix repairs many companies at once -- a very different
prospect from fifty individual label patterns.

READ-ONLY.

    .venv\\Scripts\\python.exe scripts\\193_diagnose_systematic_failures.py
"""

from __future__ import annotations

import re

import duckdb

from stock_agent import PRODUCTION_DB_PATH, WAREHOUSE_DB_PATH
from stock_agent.extraction.core import BUILT_IN_METRICS, reconstruct_presentation_dataframe
from stock_agent.filings import archive as filings_archive

WORST = ["CEG", "EXC", "RKLB", "PAYX", "PCAR", "PEP", "WMT", "SBUX"]

ROLE_PATTERNS = {
    "income statement": BUILT_IN_METRICS["revenue"].role_include_pattern,
    "cash flow":        BUILT_IN_METRICS["capex"].role_include_pattern,
    "balance sheet":    BUILT_IN_METRICS["cash_and_equivalents"].role_include_pattern,
}


def main() -> None:
    production = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    targets = production.execute("""
        SELECT f.ticker, f.report_date, f.accession_number
        FROM sec_filings f
        WHERE f.form = '10-K' AND f.ticker IN ({})
        QUALIFY ROW_NUMBER() OVER (PARTITION BY f.ticker ORDER BY f.report_date DESC) = 1
        ORDER BY f.ticker
    """.format(",".join(f"'{t}'" for t in WORST))).fetchall()
    production.close()

    warehouse = duckdb.connect(str(WAREHOUSE_DB_PATH), read_only=True)
    try:
        for ticker, report_date, accession_number in targets:
            print("=" * 96)
            print(f"{ticker}  {report_date}  {accession_number}")
            print("=" * 96)

            presentation = reconstruct_presentation_dataframe(warehouse, accession_number)
            if presentation.empty:
                print("   NO PRESENTATION TREE AT ALL -- nothing was parsed for this filing")
                continue

            roles = presentation[["role_uri", "role_definition"]].drop_duplicates()
            statement_roles = [
                str(r) for r in roles["role_definition"]
                if re.search(r"statement", str(r), re.IGNORECASE)
            ]
            print(f"   presentation rows: {len(presentation)}   distinct roles: {len(roles)}")
            print(f"   Statement-titled roles: {len(statement_roles)}")

            for family, pattern in ROLE_PATTERNS.items():
                matched = [
                    r for r in roles["role_definition"]
                    if re.search(r"statement", str(r), re.IGNORECASE)
                    and re.search(pattern, str(r), re.IGNORECASE)
                ]
                marker = "OK " if matched else "MISSING"
                print(f"   [{marker}] {family:<18} pattern={pattern!r} -> {len(matched)} role(s)")
                for role in matched[:3]:
                    print(f"              {str(role)[:84]}")

            if not statement_roles:
                print("   -- no Statement-titled roles at all. Role titles present:")
                for role in list(roles['role_definition'])[:12]:
                    print(f"        {str(role)[:88]}")
            else:
                print("   -- Statement-titled roles found:")
                for role in statement_roles[:10]:
                    print(f"        {str(role)[:88]}")

            facts = warehouse.execute(
                "SELECT COUNT(*) FROM xbrl_facts WHERE accession_number = ?",
                [accession_number]).fetchone()[0]
            print(f"   facts in warehouse: {facts}")
            print()
    finally:
        warehouse.close()


if __name__ == "__main__":
    main()
