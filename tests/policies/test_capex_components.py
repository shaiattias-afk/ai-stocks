"""
tests/policies/test_capex_components.py -- D-P2 (docs/CLEANUP_DECISIONS_
PENDING.md): capex is summed from whichever of a small, GAAP-concept-based
whitelist of physical-asset cash-flow lines are present (PaymentsFor
ConstructionInProcess, PaymentsToAcquireProductiveAssets, PaymentsTo
AcquirePropertyPlantAndEquipment), never guessed from label wording alone
-- measured at AEP, whose label for the same underlying concept drifted
"Acquisition of Assets" -> "Acquisitions of Renewable Energy Facilities"
-> "Acquisitions of Generation Facilities" across three fiscal years.
"""

from __future__ import annotations

from stock_agent.policies.capex_components import (
    find_capex_aggregate_components,
    resolve_capex_by_component_aggregate,
)

from tests.helpers import (
    CF_ROLE_DEF,
    CF_ROLE_URI,
    insert_fact,
    insert_usd_unit,
    make_warehouse_connection,
    pres_row,
    presentation_df,
)

ACCESSION = "ACC-CAPEX"
REPORT_DATE = "2024-12-31"


def test_no_components_present_returns_none():
    presentation = presentation_df([
        pres_row("us-gaap:NetCashProvidedByUsedInOperatingActivities", "Net cash from operating activities",
                 role_uri=CF_ROLE_URI, role_definition=CF_ROLE_DEF, period_type="duration"),
    ])
    assert find_capex_aggregate_components(presentation) is None


def test_single_component_resolves_as_plain_pass():
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_fact(conn, ACCESSION, "us-gaap:PaymentsForConstructionInProcess", 500.0, "C1",
                period_start="2024-01-01", period_end=REPORT_DATE, period_type="duration")
    presentation = presentation_df([
        pres_row("us-gaap:PaymentsForConstructionInProcess", "Construction Expenditures",
                 role_uri=CF_ROLE_URI, role_definition=CF_ROLE_DEF, period_type="duration"),
    ])
    result = resolve_capex_by_component_aggregate(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "PASS"
    assert result["value"] == 500.0


def test_two_components_are_summed_as_direct_aggregate():
    """Mirrors AEP: 'Construction Expenditures' (routine capex) reported
    alongside a separate 'Acquisitions of Generation Facilities' line --
    both are physical-asset capex and must be summed, not chosen between."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_fact(conn, ACCESSION, "us-gaap:PaymentsForConstructionInProcess", 500.0, "C1",
                period_start="2024-01-01", period_end=REPORT_DATE, period_type="duration")
    insert_fact(conn, ACCESSION, "us-gaap:PaymentsToAcquireProductiveAssets", 120.0, "C2",
                period_start="2024-01-01", period_end=REPORT_DATE, period_type="duration")
    presentation = presentation_df([
        pres_row("us-gaap:PaymentsForConstructionInProcess", "Construction Expenditures",
                 role_uri=CF_ROLE_URI, role_definition=CF_ROLE_DEF, period_type="duration"),
        pres_row("us-gaap:PaymentsToAcquireProductiveAssets", "Acquisitions of Generation Facilities",
                 role_uri=CF_ROLE_URI, role_definition=CF_ROLE_DEF, period_type="duration"),
    ])
    result = resolve_capex_by_component_aggregate(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "PASS_DIRECT_AGGREGATE"
    assert result["value"] == 620.0


def test_never_includes_nuclear_fuel_or_business_acquisition_concepts():
    """PaymentsForNuclearFuel and PaymentsToAcquireBusinessesNetOfCashAcquired
    are deliberately outside the whitelist -- fuel is an operating cost, a
    business acquisition is exactly the M&A case D-P2 must not fold into
    capex."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_fact(conn, ACCESSION, "us-gaap:PaymentsForConstructionInProcess", 500.0, "C1",
                period_start="2024-01-01", period_end=REPORT_DATE, period_type="duration")
    insert_fact(conn, ACCESSION, "us-gaap:PaymentsForNuclearFuel", 999.0, "C2",
                period_start="2024-01-01", period_end=REPORT_DATE, period_type="duration")
    insert_fact(conn, ACCESSION, "us-gaap:PaymentsToAcquireBusinessesNetOfCashAcquired", 888.0, "C3",
                period_start="2024-01-01", period_end=REPORT_DATE, period_type="duration")
    presentation = presentation_df([
        pres_row("us-gaap:PaymentsForConstructionInProcess", "Construction Expenditures",
                 role_uri=CF_ROLE_URI, role_definition=CF_ROLE_DEF, period_type="duration"),
        pres_row("us-gaap:PaymentsForNuclearFuel", "Acquisitions of Nuclear Fuel",
                 role_uri=CF_ROLE_URI, role_definition=CF_ROLE_DEF, period_type="duration"),
        pres_row("us-gaap:PaymentsToAcquireBusinessesNetOfCashAcquired", "Acquisition of a business, net of cash acquired",
                 role_uri=CF_ROLE_URI, role_definition=CF_ROLE_DEF, period_type="duration"),
    ])
    result = resolve_capex_by_component_aggregate(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "PASS"
    assert result["value"] == 500.0


def test_review_required_when_a_known_component_fails_to_resolve():
    """A component the whitelist identifies but that has no fact at all
    must fail closed, not silently drop out of the sum and understate
    capex."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_fact(conn, ACCESSION, "us-gaap:PaymentsForConstructionInProcess", 500.0, "C1",
                period_start="2024-01-01", period_end=REPORT_DATE, period_type="duration")
    presentation = presentation_df([
        pres_row("us-gaap:PaymentsForConstructionInProcess", "Construction Expenditures",
                 role_uri=CF_ROLE_URI, role_definition=CF_ROLE_DEF, period_type="duration"),
        pres_row("us-gaap:PaymentsToAcquireProductiveAssets", "Acquisitions of Generation Facilities",
                 role_uri=CF_ROLE_URI, role_definition=CF_ROLE_DEF, period_type="duration"),
    ])
    result = resolve_capex_by_component_aggregate(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["value"] is None


def test_combined_filing_narrows_to_registrant_before_summing():
    """The registrant's own consolidated "Construction Expenditures"
    ($500) already includes its subsidiary's share. A subsidiary-suffixed
    duplicate role (D-P1 convention, e.g. Exelon's "... - ComEd") that
    ALSO separately breaks out a generation-facility purchase must not be
    pulled in and added on top -- that would double-count spend already
    embedded in the registrant's own consolidated figure. Only the
    registrant's own (unqualified) role's components are summed."""
    conn = make_warehouse_connection()
    insert_usd_unit(conn, ACCESSION)
    insert_fact(conn, ACCESSION, "us-gaap:PaymentsForConstructionInProcess", 500.0, "C1",
                period_start="2024-01-01", period_end=REPORT_DATE, period_type="duration")
    insert_fact(conn, ACCESSION, "us-gaap:PaymentsToAcquireProductiveAssets", 50.0, "C2",
                period_start="2024-01-01", period_end=REPORT_DATE, period_type="duration")
    registrant_role_def = "0000003 - Statement - Consolidated Statements of Cash Flows"
    subsidiary_role_uri = "http://test.invalid/role/CashFlowSubsidiary"
    subsidiary_role_def = "0000010 - Statement - Consolidated Statements of Cash Flows - ComEd"
    presentation = presentation_df([
        pres_row("us-gaap:PaymentsForConstructionInProcess", "Construction Expenditures",
                 role_uri=CF_ROLE_URI, role_definition=registrant_role_def, period_type="duration"),
        pres_row("us-gaap:PaymentsToAcquireProductiveAssets", "Acquisitions of Generation Facilities",
                 role_uri=subsidiary_role_uri, role_definition=subsidiary_role_def, period_type="duration"),
    ])
    result = resolve_capex_by_component_aggregate(conn, ACCESSION, presentation, REPORT_DATE)
    assert result["status"] == "PASS"
    assert result["value"] == 500.0
