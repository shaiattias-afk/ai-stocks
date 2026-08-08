"""
D-032 — approved, one-time manual recovery of a single Inline XBRL fact
that Arelle itself could not decode in CRWD's FY2021 10-K (2021-01-31,
accession 0001535527-21-000007), per the user's explicit approved
decision: "Read the value directly from the original locked SEC 10-K
HTML when Arelle cannot decode the Inline XBRL fact. This is not an
external source and not an estimate. It is a manual parse of the same
locked SEC filing."

THE FACT (located and read directly from the locked, original SEC HTML
package on disk — the exact file scripts/100 warehoused via Arelle,
never touched or modified):

  Source file:
    data/sec_filings_locked/CRWD/000153552721000007/crwd-20210131.htm
    (byte offset ~2,857,393 of 3,619,388 total)

  Exact HTML element (verbatim, as found in the filing):
    <ix:nonFraction unitRef="usd"
      contextRef="i36456875e4304ffba24e889b8aca8952_I20210131"
      decimals="INF" format="ixt-sec:numwordsen"
      name="us-gaap:LineOfCredit" scale="0"
      id="id3VybDovL2RvY3MudjEvZG9jOjJiYTdmNGRjMDUzMDQwMGU5MzI3OGJiMTYwYTQwMjU1L3NlYzoyYmE3ZjRkYzA1MzA0MDBlOTMyNzhiYjE2MGE0MDI1NV8xMjcvZnJhZzpmM2NhMWFmYjIwMjk0ZDI2YWIxZTZlYzBjM2VjMTgzYS90ZXh0cmVnaW9uOmYzY2ExYWZiMjAyOTRkMjZhYjFlNmVjMGMzZWMxODNhXzI4MjE_d947932e-c00d-4be7-bfee-8d9f8ff30060"
      >No</ix:nonFraction> amounts were outstanding under the A&amp;R
      Credit Agreement as of January 31, 2021.

  Context reference: i36456875e4304ffba24e889b8aca8952_I20210131
    (xbrli:context, verified in the same file: entity CIK 0001535527,
    segment dimension us-gaap:CreditFacilityAxis =
    us-gaap:RevolvingCreditFacilityMember, instant 2021-01-31 — the
    EXACT same context/dimension combination Arelle itself recorded in
    the warehouse for this fact, confirming this is the right element.)

  Dimensions: {"us-gaap:CreditFacilityAxis": "us-gaap:RevolvingCreditFacilityMember"}
  Displayed/visible text: "No"
  Surrounding sentence (full, unedited): "No amounts were outstanding
    under the A&R Credit Agreement as of January 31, 2021."
  Sign: positive (no minus sign or negative-value indicator anywhere
    in the element or its attributes)
  Scale: 0 (scale attribute present and explicit)
  Format/transform attribute: ixt-sec:numwordsen — the SEC's own
    standard Inline XBRL "number words, English" transform registry,
    whose documented purpose is exactly to map English number words
    (including "no" used idiomatically for zero, as in "no amounts")
    to a numeric value. Arelle's own implementation of this specific
    transform did not resolve "No" to 0 in this filing (recorded by
    Arelle itself as value_raw="(ixTransformValueError)" in the
    warehouse) — a documented Arelle/filer-registry edge case, not a
    genuine ambiguity in the filing's own text.
  Final parsed numeric value: 0.0 — fully deterministic: the visible
    text ("No"), the declared SEC transform (built specifically to
    parse number-words including "no"=0), and the surrounding sentence
    ("No amounts were outstanding...") all agree unambiguously. No
    external source was consulted; no value was estimated.

This value is used ONLY as the undrawn-revolver evidence Policy B
already requires (identical evidence pattern already proven for CRWD
2022-2026) — it does not introduce any new accounting policy. The
original Arelle ixTransformValueError is preserved in lineage below,
alongside an explicit note that the value was recovered by manual read
of the same locked SEC source, per the user's explicit approval for
this one specific, already-identified fact.

Recalculates ONLY: CRWD FY2021 current_debt/total_debt/adjusted_net_debt/
invested_capital, then (by rerunning scripts/93 and scripts/95,
unchanged) CRWD FY2022 average_invested_capital/roic. No other company,
no other metric, no other fiscal year is touched.
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

ENGINE_VERSION = "v1-crwd-fy2021-manual-html-parse (scripts/102, D-032)"

TICKER = "CRWD"
REPORT_DATE = "2021-01-31"
ACCESSION_NUMBER = "0001535527-21-000007"
SOURCE_FILE_PATH = str(
    (DATA_DIR / "sec_filings_locked" / "CRWD" / "000153552721000007" / "crwd-20210131.htm").resolve()
)

MANUAL_FACT_RECOVERY = {
    "concept_qname": "us-gaap:LineOfCredit",
    "context_id": "i36456875e4304ffba24e889b8aca8952_I20210131",
    "dimensions": {"us-gaap:CreditFacilityAxis": "us-gaap:RevolvingCreditFacilityMember"},
    "instant_date": "2021-01-31",
    "html_element": (
        '<ix:nonFraction unitRef="usd" '
        'contextRef="i36456875e4304ffba24e889b8aca8952_I20210131" '
        'decimals="INF" format="ixt-sec:numwordsen" '
        'name="us-gaap:LineOfCredit" scale="0" '
        'id="id3VybDovL2RvY3MudjEvZG9jOjJiYTdmNGRjMDUzMDQwMGU5MzI3OGJiMTYwYTQwMjU1L3NlYzoyYmE3ZjRkYzA1MzA0MDBlOTMyNzhiYjE2MGE0MDI1NV8xMjcvZnJhZzpmM2NhMWFmYjIwMjk0ZDI2YWIxZTZlYzBjM2VjMTgzYS90ZXh0cmVnaW9uOmYzY2ExYWZiMjAyOTRkMjZhYjFlNmVjMGMzZWMxODNhXzI4MjE_'
        'd947932e-c00d-4be7-bfee-8d9f8ff30060">No</ix:nonFraction>'
    ),
    "displayed_text": "No",
    "surrounding_sentence": (
        "No amounts were outstanding under the A&R Credit Agreement "
        "as of January 31, 2021."
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
        "external source, not an estimate) — approved by the user "
        "explicitly for this one identified Arelle ixTransformValueError."
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

    # long_term_debt: unchanged, already resolves via the standard tier.
    ltd = s92.resolve_long_term_debt(warehouse_connection, ACCESSION_NUMBER, presentation, REPORT_DATE)
    print(f"  long_term_debt: {ltd['status']} value={ltd.get('value')}")

    # current_debt: re-derive the SAME structural checks scripts/92 already
    # performed (already confirmed: mode=zero_inference_needed, D-017
    # zero-inference fails [no maturity schedule], zero unclaimed
    # CURRENT-classified candidates) — the ONLY missing piece was the
    # revolver's own outstanding-balance evidence, now recovered manually.
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
                "or sourced externally, per explicit user approval "
                "(D-032)."
            ),
        },
    }
    print(f"  current_debt: PASS value=0.0 basis=SEC_HTML_MANUAL_PARSE (manually recovered)")

    total_debt = s92.resolve_total_debt_with_aggregate_policy(
        warehouse_connection, ACCESSION_NUMBER, presentation, REPORT_DATE, current_debt, ltd
    )
    print(f"  total_debt: {total_debt['status']} value={total_debt.get('value')} basis={total_debt.get('basis')}")

    cash = s92._reconstruct_simple_metric(warehouse_connection, ACCESSION_NUMBER, presentation, "cash_and_equivalents", REPORT_DATE)
    sti = s92._reconstruct_simple_metric(warehouse_connection, ACCESSION_NUMBER, presentation, "short_term_investments", REPORT_DATE)
    equity = s92._reconstruct_simple_metric(warehouse_connection, ACCESSION_NUMBER, presentation, "stockholders_equity", REPORT_DATE)

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

    # --- write to production DB -------------------------------------
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
                f"D-032: value recovered by manual parse of the original locked SEC "
                f"HTML (not external, not estimated) — Arelle recorded "
                f"value_raw='(ixTransformValueError)' for this exact fact. Full "
                f"recovery lineage: {MANUAL_FACT_RECOVERY}"
            )
        elif metric.get("basis"):
            validation_reason = (
                f"D-032: recalculated using CRWD FY2021's now-resolved current_debt "
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

        # verify immediately
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

    print("\nAll 4 CRWD FY2021 metrics written and verified successfully.")


if __name__ == "__main__":
    main()
