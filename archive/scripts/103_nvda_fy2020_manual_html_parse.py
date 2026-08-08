"""
D-033 — approved manual recovery of a single Inline XBRL fact that
Arelle could not decode in NVDA's FY2020 10-K (2020-01-26, accession
0001045810-20-000010), applying the SAME rule approved in D-032
(now confirmed by the user to generalize to any deterministic
Arelle-parsing failure, evaluated fact-by-fact — not a blanket new
policy): "Read the value directly from the original locked SEC 10-K
HTML when Arelle cannot decode the Inline XBRL fact. This is not an
external source and not an estimate."

THE FACT (located and read directly from the locked, original SEC HTML
package on disk — the exact file already warehoused via Arelle by
scripts/91, never touched or modified):

  Source file:
    data/sec_filings_locked/NVDA/000104581020000010/nvda-2020x10k.htm
    (byte offset ~1,973,900 of 2,655,153 total)

  Exact HTML element (verbatim, as found in the filing):
    <ix:nonFraction id="d29785171e1120-wk-Fact-12183F73F0C670A50F86EBA117F1A41E"
      name="us-gaap:LineOfCredit"
      contextRef="FI2020Q4_us-gaap_CreditFacilityAxis_us-gaap_RevolvingCreditFacilityMember"
      unitRef="usd" decimals="INF" scale="0"
      format="ixt-sec:numwordsen">no</ix:nonFraction>t borrowed any
      amounts under this agreement.

  Full surrounding sentence (unedited): "...as of January 26, 2020, we
    had not borrowed any amounts under this agreement." — the tag
    wraps only the word "no"; the surrounding markup continues "...t
    borrowed any amounts..." completing "not" as plain text, a
    tagging pattern identical in kind to CRWD's FY2021 fact (D-032).

  Context reference: FI2020Q4_us-gaap_CreditFacilityAxis_us-gaap_RevolvingCreditFacilityMember
    (verified in the warehouse: dimension us-gaap:CreditFacilityAxis =
    us-gaap:RevolvingCreditFacilityMember, instant 2020-01-26 — the
    EXACT same context/dimension combination Arelle itself recorded
    for this fact, confirming this is the right element.)

  Dimensions: {"us-gaap:CreditFacilityAxis": "us-gaap:RevolvingCreditFacilityMember"}
  Displayed/visible text: "no"
  Sign: positive (no minus sign or negative-value indicator anywhere)
  Scale: 0 (scale attribute present and explicit)
  Format/transform attribute: ixt-sec:numwordsen — the SEC's own
    standard "number words, English" transform, built specifically to
    map words like "no" to 0. Arelle recorded
    value_raw="(ixTransformValueError)" for this exact fact — the same
    documented Arelle transform-implementation gap as D-032, not a
    genuine ambiguity in the filing's own text.
  Final parsed numeric value: 0.0 — fully deterministic: the visible
    text ("no"), the declared SEC transform, and the surrounding
    sentence ("we had not borrowed any amounts under this agreement")
    all agree unambiguously. No external source consulted; no value
    estimated.

This value is used ONLY as the undrawn-revolver evidence Policy B
already requires (identical evidence pattern already proven for CRWD).
It does not introduce a new accounting policy. The original Arelle
ixTransformValueError is preserved in lineage below.

Recalculates ONLY: NVDA FY2020 current_debt/total_debt/adjusted_net_debt/
invested_capital. average_invested_capital/roic for NVDA FY2020 are
NOT recalculated here — NVDA FY2020 is itself the FIRST fiscal year in
the dataset (no NVDA FY2019 is locked/warehoused), so no prior filing
exists to average against; this is the same class of permanent,
out-of-scope boundary already documented for AMZN/GOOGL 2021, META
2020, and (before D-032) CRWD 2022 — closing it would require locking
one additional NVDA FY2019 10-K, which this task does not authorize.
No other company, no other metric, no other fiscal year is touched.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import duckdb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
WAREHOUSE_DB_PATH = DATA_DIR / "database" / "xbrl_warehouse_proof.duckdb"
PRODUCTION_DB_PATH = DATA_DIR / "database" / "ai_stock_agent.duckdb"

ENGINE_VERSION = "v1-nvda-fy2020-manual-html-parse (scripts/103, D-033)"

TICKER = "NVDA"
REPORT_DATE = "2020-01-26"
ACCESSION_NUMBER = "0001045810-20-000010"
SOURCE_FILE_PATH = str(
    (DATA_DIR / "sec_filings_locked" / "NVDA" / "000104581020000010" / "nvda-2020x10k.htm").resolve()
)

MANUAL_FACT_RECOVERY = {
    "concept_qname": "us-gaap:LineOfCredit",
    "context_id": "FI2020Q4_us-gaap_CreditFacilityAxis_us-gaap_RevolvingCreditFacilityMember",
    "dimensions": {"us-gaap:CreditFacilityAxis": "us-gaap:RevolvingCreditFacilityMember"},
    "instant_date": "2020-01-26",
    "html_element": (
        '<ix:nonFraction id="d29785171e1120-wk-Fact-12183F73F0C670A50F86EBA117F1A41E" '
        'name="us-gaap:LineOfCredit" '
        'contextRef="FI2020Q4_us-gaap_CreditFacilityAxis_us-gaap_RevolvingCreditFacilityMember" '
        'unitRef="usd" decimals="INF" scale="0" '
        'format="ixt-sec:numwordsen">no</ix:nonFraction>'
    ),
    "displayed_text": "no",
    "surrounding_sentence": (
        "...as of January 26, 2020, we had not borrowed any amounts "
        "under this agreement."
    ),
    "sign": "positive (no minus sign or negative indicator present)",
    "scale": "0",
    "format_transform": "ixt-sec:numwordsen",
    "final_parsed_value": 0.0,
    "source_file_path": SOURCE_FILE_PATH,
    "arelle_value_raw": "(ixTransformValueError)",
    "arelle_value_numeric": None,
    "recovery_method": (
        "Manual parse of the original locked SEC 10-K HTML (not an "
        "external source, not an estimate) — approved per the D-032 "
        "rule, applied here to the identical class of Arelle transform "
        "failure found in NVDA's own FY2020 filing."
    ),
}

# Reuse scripts/92's already-approved, unchanged policy engine.
_spec = importlib.util.spec_from_file_location(
    "s92", PROJECT_DIR / "scripts" / "92_groups_1_3_4_debt_facility_aggregate_policy.py"
)
s92 = importlib.util.module_from_spec(_spec)
sys.modules["s92"] = s92
_spec.loader.exec_module(s92)

SUCCESSFUL_STATUSES = s92.SUCCESSFUL_STATUSES


def latest_metric(connection, ticker, report_date, metric_name):
    row = connection.execute(
        """
        WITH ranked AS (
            SELECT f.status, f.value,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.accession_number, f.metric_name
                       ORDER BY r.loaded_at DESC
                   ) rn
            FROM financial_metric_results f
            JOIN extraction_runs r ON r.extraction_run_id = f.extraction_run_id
            JOIN sec_filings s ON s.accession_number = r.accession_number
            WHERE s.ticker = ? AND s.report_date = ? AND f.metric_name = ?
        )
        SELECT status, value FROM ranked WHERE rn = 1
        """,
        [ticker, report_date, metric_name],
    ).fetchone()
    return {"status": row[0], "value": row[1]} if row else None


def main() -> None:
    total_start = time.perf_counter()
    warehouse_connection = duckdb.connect(database=str(WAREHOUSE_DB_PATH), read_only=True)
    prod_connection = duckdb.connect(database=str(PRODUCTION_DB_PATH))

    print(f"=== {TICKER} {REPORT_DATE} ({ACCESSION_NUMBER}) — manual HTML fact recovery ===")

    presentation = s92.reconstruct_presentation_dataframe(warehouse_connection, ACCESSION_NUMBER)

    ltd = s92.resolve_long_term_debt(warehouse_connection, ACCESSION_NUMBER, presentation, REPORT_DATE)
    print(f"  long_term_debt: {ltd['status']} value={ltd.get('value')}")

    unclaimed_current = s92.resolve_debt_classification_by_ancestry_from_warehouse(
        presentation, "current", set()
    )
    assert len(unclaimed_current) == 0, "unexpected: a current-classified candidate now exists"

    current_debt = {
        "status": "PASS",
        "value": MANUAL_FACT_RECOVERY["final_parsed_value"],
        "concept_qname": MANUAL_FACT_RECOVERY["concept_qname"],
        "selection_tier": "zero_explicit_undrawn_facility_manual_html_parse",
        "basis": "SEC_HTML_MANUAL_PARSE",
        "lineage": {
            "manual_fact_recovery": MANUAL_FACT_RECOVERY,
            "unclaimed_current_classified_candidates": 0,
            "note": (
                "Arelle recorded value_raw='(ixTransformValueError)' / "
                "value_numeric=NULL for this exact fact (same concept, "
                "context, and dimensions) in the warehouse — preserved "
                "here as evidence. The value was recovered by manually "
                "reading the same locked SEC HTML source, not estimated "
                "or sourced externally, per the D-032 approved rule."
            ),
        },
    }
    print("  current_debt: PASS value=0.0 basis=SEC_HTML_MANUAL_PARSE (manually recovered)")

    total_debt = s92.resolve_total_debt_with_aggregate_policy(
        warehouse_connection, ACCESSION_NUMBER, presentation, REPORT_DATE, current_debt, ltd
    )
    print(f"  total_debt: {total_debt['status']} value={total_debt.get('value')} basis={total_debt.get('basis')}")

    cash = s92._reconstruct_simple_metric(warehouse_connection, ACCESSION_NUMBER, presentation, "cash_and_equivalents", REPORT_DATE)
    sti = s92._reconstruct_simple_metric(warehouse_connection, ACCESSION_NUMBER, presentation, "short_term_investments", REPORT_DATE)
    equity = s92._reconstruct_simple_metric(warehouse_connection, ACCESSION_NUMBER, presentation, "stockholders_equity", REPORT_DATE)
    print(f"  cash_and_equivalents: {cash['status']} value={cash.get('value')}")
    print(f"  short_term_investments: {sti['status']} value={sti.get('value')}")
    print(f"  stockholders_equity: {equity['status']} value={equity.get('value')}")

    def _combine_status(*statuses):
        if all(s in SUCCESSFUL_STATUSES for s in statuses):
            for pref in ("PASS_DIRECT_AGGREGATE", "PASS_MATURITY_BASIS", "PASS_NORMALIZED_TAX"):
                if pref in statuses:
                    return pref
            return "PASS"
        return "REVIEW_REQUIRED"

    adj_status = _combine_status(total_debt["status"], cash["status"], sti["status"])
    adjusted_net_debt = {
        "status": adj_status,
        "value": (total_debt["value"] - cash["value"] - sti["value"]) if adj_status in SUCCESSFUL_STATUSES else None,
        "basis": total_debt.get("basis"),
    }
    print(f"  adjusted_net_debt: {adjusted_net_debt['status']} value={adjusted_net_debt.get('value')}")

    ic_status = _combine_status(total_debt["status"], equity["status"], cash["status"], sti["status"])
    invested_capital = {
        "status": ic_status,
        "value": (
            total_debt["value"] + equity["value"] - cash["value"] - sti["value"]
        ) if ic_status in SUCCESSFUL_STATUSES else None,
        "basis": total_debt.get("basis"),
    }
    print(f"  invested_capital: {invested_capital['status']} value={invested_capital.get('value')}")

    warehouse_connection.close()

    run_id = f"{ACCESSION_NUMBER}::{ENGINE_VERSION}"
    prod_connection.execute(
        """
        INSERT INTO extraction_runs (extraction_run_id, accession_number, engine_version)
        VALUES (?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        [run_id, ACCESSION_NUMBER, ENGINE_VERSION],
    )

    results_to_write = {
        "current_debt": current_debt,
        "total_debt": total_debt,
        "adjusted_net_debt": adjusted_net_debt,
        "invested_capital": invested_capital,
    }

    written = []
    for metric_name, metric in results_to_write.items():
        status = metric["status"]
        value = metric.get("value")
        selection_tier = metric.get("selection_tier")
        source_concept = metric.get("concept_qname")

        if metric_name == "current_debt":
            validation_reason = (
                f"D-033: value recovered by manual parse of the original locked SEC "
                f"HTML (not external, not estimated) — Arelle recorded "
                f"value_raw='(ixTransformValueError)' for this exact fact. Full "
                f"recovery lineage: {MANUAL_FACT_RECOVERY}"
            )
        elif metric.get("basis"):
            validation_reason = (
                f"D-033: recalculated using NVDA FY2020's now-resolved current_debt "
                f"(manually recovered, see current_debt's own lineage). basis={metric.get('basis')}"
            )
        else:
            validation_reason = metric.get("error")

        unit = "iso4217:USD" if value is not None else None

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
                metric_name in ("current_debt", "total_debt"),
                status, value, unit,
                MANUAL_FACT_RECOVERY["context_id"] if metric_name == "current_debt" else None,
                None, REPORT_DATE, source_concept, None, None, selection_tier,
                metric_name not in ("current_debt",), None, validation_reason,
            ],
        )

        check = latest_metric(prod_connection, TICKER, REPORT_DATE, metric_name)
        ok = check and check["status"] == status and (
            check["value"] == value or (check["value"] is None and value is None)
        )
        written.append((metric_name, status, value, ok))
        print(f"  db_write verified: {metric_name} status={status} value={value} ok={ok}")

    prod_connection.close()

    total_elapsed = time.perf_counter() - total_start
    print(f"\ntotal_elapsed_seconds = {total_elapsed:.3f}")

    if not all(ok for _, _, _, ok in written):
        print("ERROR: one or more writes did not verify correctly!")
        return

    print("\nAll 4 NVDA FY2020 metrics written and verified successfully.")


if __name__ == "__main__":
    main()
