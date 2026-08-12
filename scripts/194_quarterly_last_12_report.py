"""Quarterly coverage across all companies, last 12 quarters only.

Reports what exists rather than what was hoped for: the quarterly table
carries SIX metrics, not the twelve available annually. The balance-sheet
family (cash, short-term investments, current/long-term debt, equity) and
everything derived from it (ROIC, invested capital, net debt, free cash
flow) were only ever built at annual frequency.

READ-ONLY.

    .venv\\Scripts\\python.exe scripts\\194_quarterly_last_12_report.py
"""

from __future__ import annotations

import json

import duckdb

from stock_agent import DATA_DIR, PRODUCTION_DB_PATH

RESULT_PATH = DATA_DIR / "quarterly_last_12_report.json"
QUARTERS_BACK = 12
PASS_STATUSES = ("PASS", "PASS_ROUNDING_TOLERANCE")


def main() -> None:
    c = duckdb.connect(str(PRODUCTION_DB_PATH), read_only=True)
    try:
        # rank each ticker's quarters newest-first, keep the most recent 12
        c.execute(f"""
            CREATE OR REPLACE TEMP VIEW last12 AS
            WITH ranked AS (
                SELECT ticker, fiscal_year_end, fiscal_quarter, metric_name,
                       value, result_status, period_end, availability_date,
                       DENSE_RANK() OVER (
                           PARTITION BY ticker
                           ORDER BY fiscal_year_end DESC, fiscal_quarter DESC
                       ) AS recency
                FROM quarterly_metric_results
            )
            SELECT * FROM ranked WHERE recency <= {QUARTERS_BACK}
        """)

        metrics = [r[0] for r in c.execute(
            "SELECT DISTINCT metric_name FROM last12 ORDER BY 1").fetchall()]
        tickers = c.execute("SELECT COUNT(DISTINCT ticker) FROM last12").fetchone()[0]
        rows = c.execute("SELECT COUNT(*) FROM last12").fetchone()[0]

        print("=" * 92)
        print(f"QUARTERLY DATA — LAST {QUARTERS_BACK} QUARTERS PER COMPANY")
        print("=" * 92)
        print(f"companies : {tickers}")
        print(f"rows      : {rows:,}")
        print(f"metrics   : {len(metrics)}  ->  {', '.join(metrics)}")

        # Which metrics exist ANNUALLY but not quarterly? Read from the
        # live tables, not asserted from memory.
        annual_metrics = [r[0] for r in c.execute(
            "SELECT DISTINCT metric_name FROM financial_metric_results ORDER BY 1").fetchall()]
        annual_only = sorted(set(annual_metrics) - set(metrics))
        print()
        print(f"metrics available ANNUALLY : {len(annual_metrics)}")
        print(f"annual-only (NOT quarterly): {len(annual_only)}")
        print(f"   {', '.join(annual_only)}")
        print()

        print("=== coverage per metric (last 12 quarters, all companies) ===")
        print(c.execute(f"""
            SELECT metric_name,
                   COUNT(*)                                        AS rows,
                   COUNT(DISTINCT ticker)                          AS companies,
                   SUM(CASE WHEN result_status IN {PASS_STATUSES} THEN 1 ELSE 0 END) AS usable,
                   ROUND(100.0 * SUM(CASE WHEN result_status IN {PASS_STATUSES} THEN 1 ELSE 0 END)
                         / COUNT(*), 1)                            AS pct
            FROM last12 GROUP BY 1 ORDER BY 1
        """).df().to_string(index=False))

        print()
        print("=== how many of the last 12 quarters does each company actually have? ===")
        dist = c.execute("""
            SELECT quarters_available, COUNT(*) AS companies FROM (
                SELECT ticker, COUNT(DISTINCT recency) AS quarters_available
                FROM last12 GROUP BY 1
            ) GROUP BY 1 ORDER BY 1 DESC
        """).df()
        print(dist.to_string(index=False))

        print()
        print("=== companies with a FULL 12 quarters on all 6 metrics ===")
        complete = c.execute(f"""
            SELECT ticker,
                   COUNT(*) AS rows,
                   SUM(CASE WHEN result_status IN {PASS_STATUSES} THEN 1 ELSE 0 END) AS usable
            FROM last12 GROUP BY 1
            HAVING COUNT(*) = {QUARTERS_BACK} * (SELECT COUNT(DISTINCT metric_name) FROM last12)
            ORDER BY 1
        """).df()
        print(f"count: {len(complete)}")
        print(", ".join(complete["ticker"].tolist()))

        print()
        print("=== companies with the LEAST quarterly history ===")
        print(c.execute("""
            SELECT ticker, COUNT(DISTINCT recency) AS quarters, COUNT(*) AS rows
            FROM last12 GROUP BY 1 ORDER BY 2 ASC, 1 LIMIT 15
        """).df().to_string(index=False))

        print()
        print("=== unusable rows, by reason ===")
        print(c.execute(f"""
            SELECT result_status, COUNT(*) AS n, COUNT(DISTINCT ticker) AS companies
            FROM last12 WHERE result_status NOT IN {PASS_STATUSES}
            GROUP BY 1 ORDER BY 2 DESC
        """).df().to_string(index=False))

        summary = c.execute(f"""
            SELECT COUNT(*) AS rows,
                   SUM(CASE WHEN result_status IN {PASS_STATUSES} THEN 1 ELSE 0 END) AS usable
            FROM last12
        """).fetchone()
        print()
        print("=" * 92)
        print(f"TOTAL usable: {summary[1]:,} / {summary[0]:,} = {100.0 * summary[1] / summary[0]:.1f}%")
        print("=" * 92)

        RESULT_PATH.write_text(json.dumps({
            "quarters_back": QUARTERS_BACK,
            "companies": tickers, "rows": rows,
            "metrics_available_quarterly": metrics,
            # derived by comparing the two live tables, not from memory
            "metrics_annual_only": annual_only,
            "usable_rows": int(summary[1]), "total_rows": int(summary[0]),
            "usable_pct": round(100.0 * summary[1] / summary[0], 1),
            "companies_with_full_12q": complete["ticker"].tolist(),
        }, indent=2, default=str), encoding="utf-8")
        print(f"written: {RESULT_PATH}")
    finally:
        c.close()


if __name__ == "__main__":
    main()
