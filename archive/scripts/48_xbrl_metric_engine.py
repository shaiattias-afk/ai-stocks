from __future__ import annotations

import argparse
import json
import multiprocessing
import time
import traceback
from dataclasses import dataclass
from datetime import timedelta, timezone, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd


# =============================================================================
# Generic, shared, ticker-agnostic XBRL statement-first metric engine.
#
# Consolidates the logic already proven separately in scripts 37c-46 into
# one reusable engine, organized into clearly separated concerns:
#   1. FILING LOCK LOADING       - locate the locked 10-K manifest
#   2. ARELLE SESSION LOADING    - bounded, child-process-safe model load
#   3. STATEMENT ROLE ID         - find the primary statement role
#   4. CANONICAL ROW ID          - find the target line item within it
#      (row identification is period-independent: the same concept/row
#      represents a metric in both the current and comparative columns
#      of the same statement, so it is only ever resolved once)
#   5. FACT MATCHING             - filter facts by context/period/unit/
#                                  dimensions/entity, aware of whether the
#                                  concept is instant (balance sheet) or
#                                  duration (income/cash-flow statement),
#                                  and at whichever reporting date is
#                                  requested (current or prior year)
#   6. DEDUPLICATION + STATUS    - collapse technical duplicates, decide
#                                  PASS / REVIEW_REQUIRED
#   7. BOUNDED ORCHESTRATION     - child process + timeouts + TIMEOUT
#   8. DERIVED METRICS           - computed from already-extracted metrics
#                                  (built-in or derived, resolved in
#                                  dependency order), never their own XBRL
#                                  row search. A small set of CUSTOM
#                                  derived metrics (Total Debt, Effective
#                                  Tax Rate) apply explicit accounting
#                                  policy beyond a plain N-component
#                                  combine and are computed by dedicated
#                                  functions instead of the generic one.
#
# No ticker-specific rule exists anywhere in this file. No manual concept
# tag list is used as the primary selection mechanism — concepts are
# always derived from the filing's own presentation structure (D-007).
#
# v5 (this file, 42-46 unmodified): implements the user's explicit
# accounting policy for NOPAT / ROIC:
#   - Total Debt: interest-bearing debt only (current + long-term debt),
#     preferring a single explicit "Total debt" row when one exists and
#     validates structurally; otherwise summing current_debt +
#     long_term_debt only because they are already guaranteed
#     non-overlapping by construction (mutually-exclusive label
#     patterns). Never infers a missing current-debt row as zero.
#   - Pretax Income, Income Tax Expense, Stockholders' Equity as new
#     built-in metrics.
#   - Reported Effective Tax Rate = income_tax_expense / pretax_income,
#     REVIEW_REQUIRED if pretax income is not positive or the rate falls
#     outside [0, 1].
#   - NOPAT = operating_income * (1 - effective_tax_rate).
#   - Invested Capital = total_debt + stockholders_equity -
#     cash_and_equivalents - short_term_investments, computed for both
#     the current and the prior fiscal year-end (the same locked filing's
#     comparative balance-sheet column), then averaged.
#   - ROIC = NOPAT / Average Invested Capital.
# This required two genuine architecture additions: (1) prior-fiscal-
# year-end fact extraction, reusing the same (period-independent) row
# identification at a second date computed generically as
# report_date-1-year; (2) a small set of CUSTOM derived metrics with
# their own accounting-policy logic, alongside the existing generic
# N-component "combine" derived metrics.
# =============================================================================


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / "arelle_cache"

EXPECTED_FORM = "10-K"

# Same bounding values already verified as safe (non-hanging) across all
# prior proofs (scripts 37c through 46).
TOTAL_TIMEOUT_SECONDS = 240
INTERNET_TIMEOUT_SECONDS = 20
TERMINATE_GRACE_SECONDS = 5

# A duration context is accepted as "annual" only within this tolerance,
# instead of assuming any single company's specific fiscal-year length.
# Not used for instant (balance sheet) metrics.
ANNUAL_DURATION_MIN_DAYS = 350
ANNUAL_DURATION_MAX_DAYS = 380


# =============================================================================
# Metric definitions — declarative, structural/label rules only. Adding a
# new metric means adding an entry here, never a ticker branch and never
# a hard-coded concept name.
# =============================================================================


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    role_include_pattern: str
    role_exclude_pattern: str | None
    mention_pattern: str
    exclude_label_pattern: str | None
    attributable_pattern: str | None
    plain_pattern: str


BUILT_IN_METRICS: dict[str, MetricDefinition] = {
    "revenue": MetricDefinition(
        name="revenue",
        role_include_pattern=r"operations|income",
        role_exclude_pattern=r"comprehensive",
        mention_pattern=r"revenues?",
        exclude_label_pattern=None,
        attributable_pattern=None,
        plain_pattern=r"^\s*(?:total\s+)?revenues?\s*$",
    ),
    "net_income": MetricDefinition(
        name="net_income",
        role_include_pattern=r"operations|income",
        role_exclude_pattern=r"comprehensive",
        mention_pattern=r"net\s+(?:income|loss)",
        exclude_label_pattern=r"per\s+share|weighted\s+average",
        attributable_pattern=(
            r"attributable\s+to.*(?:common|stockholders|shareholders|"
            r"corporation|company|\binc\.?\b|\bcorp\.?\b)"
        ),
        plain_pattern=r"^\s*net\s+(?:income\s*\(loss\)|income|loss)\s*$",
    ),
    "operating_income": MetricDefinition(
        name="operating_income",
        role_include_pattern=r"operations|income",
        role_exclude_pattern=r"comprehensive",
        mention_pattern=(
            r"operating\s+(?:income|loss)|"
            r"(?:income|loss)\s*(?:\(loss\)|\(income\))?\s*from\s+operations"
        ),
        exclude_label_pattern=r"per\s+share|weighted\s+average|margin|percentage",
        attributable_pattern=None,
        plain_pattern=(
            r"^\s*(?:"
            r"operating\s+(?:income\s*\(loss\)|income|loss)"
            r"|(?:income|loss)\s*(?:\(loss\)|\(income\))?\s*from\s+operations"
            r")\s*$"
        ),
    ),
    "operating_cash_flow": MetricDefinition(
        name="operating_cash_flow",
        role_include_pattern=r"cash\s*flows?",
        role_exclude_pattern=None,
        mention_pattern=r"net\s+cash.*(?:from\s+operations|operating\s+activities)",
        exclude_label_pattern=None,
        attributable_pattern=None,
        plain_pattern=(
            r"^\s*net\s+cash\s+(?:provided\s+by|used\s+in|from)\s+"
            r"operat(?:ing\s+activities|ions)\s*$"
        ),
    ),
    "capex": MetricDefinition(
        name="capex",
        role_include_pattern=r"cash\s*flows?",
        role_exclude_pattern=None,
        mention_pattern=(
            r"capital\s+expenditures?|"
            r"(?:additions|purchases)\s+(?:to|of)\s+property"
        ),
        exclude_label_pattern=(
            r"unpaid|incurred\s+but\s+not\s+yet\s+paid|"
            r"accounts\s+payable|accrued"
        ),
        attributable_pattern=None,
        plain_pattern=(
            r"^\s*capital\s+expenditures?\s*$|"
            r"^\s*(?:additions|purchases)\s+(?:to|of)\s+property"
            r"(?:,?\s+plant)?\s+and\s+equipment\s*$"
        ),
    ),
    # --- Balance Sheet metrics (instant context, not duration) ---
    "cash_and_equivalents": MetricDefinition(
        name="cash_and_equivalents",
        role_include_pattern=r"balance\s+sheets?|financial\s+position",
        role_exclude_pattern=r"parenthetical",
        mention_pattern=r"cash\s+and\s+cash\s+equivalents",
        exclude_label_pattern=None,
        attributable_pattern=None,
        plain_pattern=r"^\s*cash\s+and\s+cash\s+equivalents\s*$",
    ),
    "short_term_investments": MetricDefinition(
        name="short_term_investments",
        role_include_pattern=r"balance\s+sheets?|financial\s+position",
        role_exclude_pattern=r"parenthetical",
        mention_pattern=r"marketable\s+securities|short-?term\s+investments",
        exclude_label_pattern=None,
        attributable_pattern=None,
        plain_pattern=r"^\s*(?:marketable\s+securities|short-?term\s+investments)\s*$",
    ),
    "current_debt": MetricDefinition(
        name="current_debt",
        role_include_pattern=r"balance\s+sheets?|financial\s+position",
        role_exclude_pattern=r"parenthetical",
        mention_pattern=(
            r"short-?term\s+debt|commercial\s+paper|"
            r"notes?\s+payable.*current|current.*notes?\s+payable|"
            r"current\s+portion\s+of\s+(?:long-?term\s+)?debt|"
            r"current\s+maturities\s+of\s+long-?term\s+debt"
        ),
        exclude_label_pattern=r"non-?current",
        attributable_pattern=None,
        plain_pattern=(
            r"^\s*short-?term\s+debt\s*$"
            r"|^\s*commercial\s+paper\s*$"
            r"|^\s*notes?\s+payable(?:\s+and\s+other\s+borrowings)?,?\s*current\s*$"
            r"|^\s*current\s+portion\s+of\s+long-?term\s+debt\s*$"
            r"|^\s*current\s+maturities\s+of\s+long-?term\s+debt\s*$"
        ),
    ),
    "long_term_debt": MetricDefinition(
        name="long_term_debt",
        role_include_pattern=r"balance\s+sheets?|financial\s+position",
        role_exclude_pattern=r"parenthetical",
        mention_pattern=(
            r"long-?term\s+debt|"
            r"notes?\s+payable.*non-?current|non-?current.*notes?\s+payable"
        ),
        exclude_label_pattern=r"current\s+portion|current\s+maturities",
        attributable_pattern=None,
        plain_pattern=(
            r"^\s*long-?term\s+debt\s*$"
            r"|^\s*notes?\s+payable(?:\s+and\s+other\s+borrowings)?,?\s*non-?current\s*$"
        ),
    ),
    # Explicit "Total debt" row, preferred over summing current+long-term
    # debt when it exists (accounting policy — see compute_total_debt).
    # None of Oracle/Microsoft/Meta's 10-Ks present one on the balance
    # sheet; this exists so the engine prefers it automatically wherever
    # a filer does.
    "total_debt_explicit": MetricDefinition(
        name="total_debt_explicit",
        role_include_pattern=r"balance\s+sheets?|financial\s+position",
        role_exclude_pattern=r"parenthetical",
        mention_pattern=r"total\s+debt",
        exclude_label_pattern=None,
        attributable_pattern=None,
        plain_pattern=r"^\s*total\s+debt\s*$",
    ),
    "stockholders_equity": MetricDefinition(
        name="stockholders_equity",
        role_include_pattern=r"balance\s+sheets?|financial\s+position",
        role_exclude_pattern=r"parenthetical",
        mention_pattern=r"stockholders.?\s+equity|shareholders.?\s+equity",
        # No exclude_label_pattern needed: the anchored plain_pattern
        # below already naturally excludes a parent-only variant such as
        # Oracle's "Total Oracle Corporation stockholders' equity" (the
        # company name sits between "Total" and "stockholders' equity",
        # breaking the anchor), leaving only the entity-wide "Total
        # stockholders' equity" / "Total shareholders' equity" row — the
        # one that balances against Total Debt in the accounting
        # identity, consistent with how every other "Total X" metric in
        # this engine is selected.
        exclude_label_pattern=None,
        attributable_pattern=None,
        plain_pattern=r"^\s*total\s+(?:stockholders|shareholders).?\s+equity\s*$",
    ),
    "pretax_income": MetricDefinition(
        name="pretax_income",
        role_include_pattern=r"operations|income",
        role_exclude_pattern=r"comprehensive",
        mention_pattern=r"income\s*(?:\(loss\))?\s*before.*tax",
        exclude_label_pattern=None,
        attributable_pattern=None,
        plain_pattern=(
            r"^\s*income\s*(?:\(loss\))?\s*before\s+"
            r"(?:provision\s+for\s+)?income\s+taxes\s*$"
        ),
    ),
    "income_tax_expense": MetricDefinition(
        name="income_tax_expense",
        role_include_pattern=r"operations|income",
        role_exclude_pattern=r"comprehensive",
        mention_pattern=(
            r"(?:provision|benefit).*income\s+tax|"
            r"income\s+tax.*(?:provision|expense|benefit)"
        ),
        exclude_label_pattern=None,
        attributable_pattern=None,
        plain_pattern=(
            r"^\s*(?:provision|benefit)\s+for\s+income\s+taxes\s*$"
            r"|^\s*income\s+tax\s+(?:expense|provision|benefit)\s*$"
        ),
    ),
}


@dataclass(frozen=True)
class DerivedMetricDefinition:
    name: str
    # Names of other metrics (built-in, custom, or derived — including
    # "_prior" suffixed variants) this value is computed from, in the
    # exact order `combine` expects.
    component_metrics: tuple[str, ...]
    formula_description: str
    combine: Callable[[list[float]], float | None]
    # When True (default), all components must share the exact same
    # reporting period, and that period is carried forward as the
    # result's period. When False (e.g. averaging a current-year and a
    # prior-year balance, or a ratio of two differently-timed metrics),
    # the period check is skipped and the result's period spans the
    # earliest start to the latest end among components instead.
    require_same_period: bool = True


DERIVED_METRICS: dict[str, DerivedMetricDefinition] = {
    "free_cash_flow": DerivedMetricDefinition(
        name="free_cash_flow",
        component_metrics=("operating_cash_flow", "capex"),
        formula_description="operating_cash_flow - capex",
        combine=lambda values: values[0] - values[1],
    ),
    "adjusted_net_debt": DerivedMetricDefinition(
        name="adjusted_net_debt",
        component_metrics=(
            "total_debt",
            "cash_and_equivalents",
            "short_term_investments",
        ),
        formula_description=(
            "total_debt - cash_and_equivalents - short_term_investments"
        ),
        combine=lambda values: values[0] - values[1] - values[2],
    ),
    "nopat": DerivedMetricDefinition(
        name="nopat",
        component_metrics=("operating_income", "effective_tax_rate"),
        formula_description="operating_income * (1 - effective_tax_rate)",
        combine=lambda values: values[0] * (1 - values[1]),
    ),
    "invested_capital": DerivedMetricDefinition(
        name="invested_capital",
        component_metrics=(
            "total_debt",
            "stockholders_equity",
            "cash_and_equivalents",
            "short_term_investments",
        ),
        formula_description=(
            "total_debt + stockholders_equity - cash_and_equivalents - "
            "short_term_investments"
        ),
        combine=lambda values: values[0] + values[1] - values[2] - values[3],
    ),
    "invested_capital_prior": DerivedMetricDefinition(
        name="invested_capital_prior",
        component_metrics=(
            "total_debt_prior",
            "stockholders_equity_prior",
            "cash_and_equivalents_prior",
            "short_term_investments_prior",
        ),
        formula_description=(
            "total_debt_prior + stockholders_equity_prior - "
            "cash_and_equivalents_prior - short_term_investments_prior"
        ),
        combine=lambda values: values[0] + values[1] - values[2] - values[3],
    ),
    "average_invested_capital": DerivedMetricDefinition(
        name="average_invested_capital",
        component_metrics=("invested_capital", "invested_capital_prior"),
        formula_description="(invested_capital + invested_capital_prior) / 2",
        combine=lambda values: (values[0] + values[1]) / 2,
        require_same_period=False,
    ),
    "roic": DerivedMetricDefinition(
        name="roic",
        component_metrics=("nopat", "average_invested_capital"),
        formula_description="nopat / average_invested_capital",
        combine=(
            lambda values: (values[0] / values[1])
            if values[1] is not None and values[1] > 0
            else None
        ),
        require_same_period=False,
    ),
}


# Custom derived metrics apply accounting-policy logic beyond a plain
# N-component combine (see compute_total_debt, compute_effective_tax_rate)
# and are computed by dedicated functions in a fixed order, since neither
# currently depends on the other.
CUSTOM_METRIC_RAW_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "total_debt": ("current_debt", "long_term_debt", "total_debt_explicit"),
    "effective_tax_rate": ("pretax_income", "income_tax_expense"),
}


def compute_prior_report_date(report_date: str) -> str:
    """
    Generic (not company-specific) prior-fiscal-year-end date: exactly
    one year earlier, same month/day. Falls back to Feb 28 in the rare
    case of a Feb 29 report_date landing on a non-leap prior year.
    """

    year, month, day = (int(part) for part in report_date.split("-"))

    try:
        prior_date = datetime(year - 1, month, day).date()
    except ValueError:
        prior_date = datetime(year - 1, month, day - 1).date()

    return prior_date.isoformat()


def resolve_metric_dependencies(
    requested_names: list[str],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """
    Expands the requested metric names into:
      required_built_in_current — BUILT_IN metrics to extract at report_date
      required_built_in_prior   — BUILT_IN metrics to also extract at the
                                   prior fiscal year-end (requested via a
                                   "<name>_prior" component reference)
      ordered_custom            — CUSTOM metrics to compute (fixed order;
                                   see CUSTOM_METRIC_RAW_REQUIREMENTS)
      ordered_derived           — generic DERIVED metrics, topologically
                                   ordered so every component is already
                                   available when each one is computed
    """

    required_built_in_current: set[str] = set()
    required_built_in_prior: set[str] = set()
    needed_custom: set[str] = set()
    needed_derived: set[str] = set()

    def visit(name: str) -> None:
        # Exact-key lookups (BUILT_IN / CUSTOM / DERIVED) always take
        # priority over the generic "_prior" suffix-stripping fallback
        # below — "invested_capital_prior" is itself a literal
        # DERIVED_METRICS key (its component_metrics already reference
        # "_prior"-suffixed raw items directly), not a "_prior" variant
        # of a metric named "invested_capital_prior" minus the suffix.
        # Checking suffix-stripping first would misroute it and raise a
        # false "unsupported '_prior' variant" error.
        if name in BUILT_IN_METRICS:
            required_built_in_current.add(name)
            return

        if name in CUSTOM_METRIC_RAW_REQUIREMENTS:
            if name not in needed_custom:
                needed_custom.add(name)

                for raw_name in CUSTOM_METRIC_RAW_REQUIREMENTS[name]:
                    visit(raw_name)

            return

        if name in DERIVED_METRICS:
            if name not in needed_derived:
                needed_derived.add(name)

                for component_name in DERIVED_METRICS[name].component_metrics:
                    visit(component_name)

            return

        if name.endswith("_prior"):
            base_name = name[: -len("_prior")]

            if base_name in BUILT_IN_METRICS:
                required_built_in_prior.add(base_name)
                return

            if base_name in CUSTOM_METRIC_RAW_REQUIREMENTS:
                needed_custom.add(name)

                for raw_name in CUSTOM_METRIC_RAW_REQUIREMENTS[base_name]:
                    required_built_in_prior.add(raw_name)

                return

            raise ValueError(
                f"אין תמיכה בגרסת '_prior' עבור מדד: {base_name}"
            )

        raise ValueError(f"מדד לא מוכר: {name}")

    for requested_name in requested_names:
        visit(requested_name)

    ordered_derived: list[str] = []

    def is_available(name: str) -> bool:
        if name in required_built_in_current:
            return True
        if name.endswith("_prior") and (
            name[: -len("_prior")] in required_built_in_prior
        ):
            return True
        if name in needed_custom:
            return True
        if name in ordered_derived:
            return True
        return False

    remaining_derived = set(needed_derived)

    while remaining_derived:
        progressed = False

        for name in sorted(remaining_derived):
            components = DERIVED_METRICS[name].component_metrics

            if all(is_available(component_name) for component_name in components):
                ordered_derived.append(name)
                remaining_derived.discard(name)
                progressed = True

        if not progressed:
            raise RuntimeError(
                "תלות מעגלית או בלתי ניתנת לפתרון בין מדדים נגזרים: "
                f"{remaining_derived}"
            )

    # Neither current custom metric depends on the other, so any fixed
    # order is safe. A future custom metric that depends on another
    # would need this promoted to a real topological sort.
    ordered_custom = sorted(needed_custom)

    return (
        sorted(required_built_in_current),
        sorted(required_built_in_prior),
        ordered_custom,
        ordered_derived,
    )


ALL_REQUESTABLE_METRICS = (
    list(BUILT_IN_METRICS.keys())
    + list(DERIVED_METRICS.keys())
    + list(CUSTOM_METRIC_RAW_REQUIREMENTS.keys())
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generic, ticker-agnostic statement-first XBRL metric "
            "engine. Extracts one or more built-in metrics, and any "
            "derived/custom metrics computed from them (in dependency "
            "order, including prior-fiscal-year-end balance sheet "
            "values where needed for averaging), from an already locked "
            "10-K using Arelle presentation structure only — no "
            "per-company rule, no manual concept tag list as primary "
            "mechanism."
        )
    )

    parser.add_argument("--ticker", required=True, help="e.g. ORCL, MSFT.")
    parser.add_argument(
        "--report-date",
        required=True,
        help="Fiscal report date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--metrics",
        nargs="+",
        default=ALL_REQUESTABLE_METRICS,
        choices=ALL_REQUESTABLE_METRICS,
        help="Which metrics (built-in, derived, and/or custom) to extract.",
    )

    return parser.parse_args()


def output_paths(ticker: str, report_date: str) -> dict[str, Path]:
    prefix = f"{ticker.lower()}_{report_date.replace('-', '')}"

    return {
        "presentation_csv": (
            DATA_DIR / f"{prefix}_engine_v5b_presentation.csv"
        ),
        "row_candidates_csv": (
            DATA_DIR / f"{prefix}_engine_v5b_row_candidates.csv"
        ),
        "fact_candidates_csv": (
            DATA_DIR / f"{prefix}_engine_v5b_fact_candidates.csv"
        ),
        "result_file": DATA_DIR / f"{prefix}_engine_v5b_result.json",
        "arelle_log_file": (
            DATA_DIR / f"{prefix}_engine_v5b_arelle_child.log"
        ),
        "orchestration_log_file": (
            DATA_DIR / f"{prefix}_engine_v5b_orchestration.log"
        ),
    }


def log_line(orchestration_log_file: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"[{timestamp}] {message}"

    print(line)

    with orchestration_log_file.open(
        mode="a",
        encoding="utf-8",
    ) as log_file:
        log_file.write(line + "\n")


# =============================================================================
# 1. FILING LOCK LOADING
# =============================================================================


def load_locked_filing(ticker: str, report_date: str) -> dict[str, Any]:
    locked_dir = DATA_DIR / "sec_filings_locked" / ticker.upper()

    manifests = sorted(locked_dir.glob("*/locked_filing_manifest.json"))

    matching_manifests = []

    for manifest_file in manifests:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

        if (
            manifest.get("report_date") == report_date
            and manifest.get("form") == EXPECTED_FORM
        ):
            matching_manifests.append((manifest_file, manifest))

    if len(matching_manifests) != 1:
        raise RuntimeError(
            "לא נמצא Manifest נעול יחיד וברור עבור "
            f"{ticker} / {report_date}.\n"
            f"מספר התאמות: {len(matching_manifests)}\n"
            "יש לנעול את ההגשה תחילה עם "
            "36b_download_accession_locked_filing.py."
        )

    manifest_file, manifest = matching_manifests[0]

    primary_document_path = Path(
        manifest["primary_document_path"]
    ).resolve()

    if not primary_document_path.exists():
        raise FileNotFoundError(
            f"קובץ ה-10-K הראשי לא נמצא:\n{primary_document_path}"
        )

    sec_user_agent = str(manifest.get("sec_user_agent", "")).strip()

    if not sec_user_agent:
        raise RuntimeError("לא נמצא sec_user_agent ב-Manifest הנעול.")

    cik = manifest.get("cik")

    if not cik:
        raise RuntimeError("לא נמצא cik ב-Manifest הנעול.")

    return {
        "manifest_file": manifest_file,
        "primary_document_path": primary_document_path,
        "accession_number": manifest.get("accession_number"),
        "accession_compact": str(
            manifest.get("accession_number", "")
        ).replace("-", ""),
        "report_date": manifest.get("report_date"),
        "filing_date": manifest.get("filing_date"),
        "sec_user_agent": sec_user_agent,
        "cik": int(cik),
        "ticker": manifest.get("ticker", ticker.upper()),
        "company_name": manifest.get("company_name", ""),
        "primary_document_name": manifest.get("primary_document"),
    }


_COMBINED_QA_COLUMNS = {
    "revenue": "revenue_usd",
    "operating_cash_flow": "operating_cash_flow_usd",
    "capex": "capex_usd",
    "free_cash_flow": "fcf_usd",
}


def find_qa_reference_value(
    ticker: str,
    report_date: str,
    metric_name: str,
) -> dict[str, object] | None:
    """
    Best-effort QA-only lookup of a figure already computed by an earlier,
    independent pipeline in this project, if one happens to exist for
    this exact ticker/period/metric. Never used to select or validate the
    extracted fact — only reported for human comparison.
    """

    dedicated_file = DATA_DIR / f"{ticker.lower()}_{metric_name}_test.csv"

    if dedicated_file.exists():
        try:
            existing = pd.read_csv(dedicated_file, dtype=str)
        except Exception:
            existing = None

        if existing is not None and "period_end" in existing.columns:
            matching_rows = existing[existing["period_end"] == report_date]

            if len(matching_rows) == 1:
                return {
                    "source_file": str(dedicated_file),
                    "row": matching_rows.iloc[0].to_dict(),
                }

    combined_column = _COMBINED_QA_COLUMNS.get(metric_name)
    combined_file = DATA_DIR / f"{ticker.lower()}_fcf_test.csv"

    if combined_column and combined_file.exists():
        try:
            existing = pd.read_csv(combined_file, dtype=str)
        except Exception:
            existing = None

        if (
            existing is not None
            and "period_end" in existing.columns
            and combined_column in existing.columns
        ):
            matching_rows = existing[existing["period_end"] == report_date]

            if len(matching_rows) == 1:
                row = matching_rows.iloc[0]

                return {
                    "source_file": str(combined_file),
                    "value": row.get(combined_column),
                    "row": row.to_dict(),
                }

    return {
        "source_file": None,
        "note": (
            f"לא נמצא קובץ QA עצמאי עבור {metric_name} של "
            f"{ticker.upper()} לתקופה זו — אין נתון להשוואה."
        ),
    }


# =============================================================================
# 2. ARELLE SESSION LOADING + full presentation walk (statement-agnostic)
# =============================================================================


def _safe_label(concept: Any, preferred_label: str | None = None) -> str:
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
        label = concept.label(lang="en-US", fallbackToQname=True)

        if label:
            return str(label)
    except Exception:
        pass

    return str(getattr(concept, "qname", ""))


def _role_definition(model_xbrl: Any, role_uri: str) -> str:
    role_types = model_xbrl.roleTypes.get(role_uri, [])

    for role_type in role_types:
        definition = getattr(role_type, "definition", "")

        if definition:
            return str(definition)

    return ""


def _walk_tree(
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
    concept_qname = str(getattr(concept, "qname", ""))
    visit_key = (parent_qname, concept_qname, depth)

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
            "label": _safe_label(concept, preferred_label or None),
            "is_abstract": bool(getattr(concept, "isAbstract", False)),
            "period_type": str(getattr(concept, "periodType", "")),
            "balance": str(getattr(concept, "balance", "") or ""),
        }
    )

    relationships = relationship_set.fromModelObject(concept)

    relationships = sorted(
        relationships,
        key=lambda relationship: (
            float(getattr(relationship, "order", 0) or 0),
            str(getattr(relationship.toModelObject, "qname", "")),
        ),
    )

    for relationship in relationships:
        child = relationship.toModelObject

        if child is None:
            continue

        child_preferred_label = str(
            getattr(relationship, "preferredLabel", "") or ""
        )

        _walk_tree(
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


def extract_presentation(model_xbrl: Any) -> pd.DataFrame:
    from arelle import XbrlConst

    records: list[dict[str, object]] = []

    global_relationship_set = model_xbrl.relationshipSet(
        XbrlConst.parentChild
    )

    for role_uri in sorted(global_relationship_set.linkRoleUris):
        relationship_set = model_xbrl.relationshipSet(
            XbrlConst.parentChild,
            role_uri,
        )

        definition = _role_definition(model_xbrl, role_uri)

        roots = sorted(
            relationship_set.rootConcepts,
            key=lambda concept: str(getattr(concept, "qname", "")),
        )

        for root in roots:
            _walk_tree(
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


# =============================================================================
# 3 + 4. STATEMENT ROLE IDENTIFICATION + CANONICAL ROW IDENTIFICATION
#    (period-independent — resolved once per metric, reused at any date)
# =============================================================================


class TargetRowNotFound(Exception):
    """
    Raised when the presentation structure does not resolve to exactly
    one unambiguous row for a metric. Distinguished from other exceptions
    so the caller can report REVIEW_REQUIRED (insufficient evidence)
    instead of FAIL (execution error).
    """


def identify_canonical_row(
    presentation: pd.DataFrame,
    metric: MetricDefinition,
) -> tuple[dict[str, str], pd.DataFrame]:
    """
    Structure-first row selection, generic across metrics and tickers.
    See prior engine versions (42-46) for the full rationale. If more
    than one candidate survives every filter, or zero do, this fails
    closed with TargetRowNotFound (→ REVIEW_REQUIRED), never a guess.
    """

    is_statement_role = presentation["role_definition"].str.match(
        r"^\d+\s*-\s*Statement\s*-",
        na=False,
    )

    is_role_include = presentation["role_definition"].str.contains(
        metric.role_include_pattern,
        case=False,
        regex=True,
        na=False,
    )

    if metric.role_exclude_pattern:
        is_role_exclude = presentation["role_definition"].str.contains(
            metric.role_exclude_pattern,
            case=False,
            regex=True,
            na=False,
        )
    else:
        is_role_exclude = pd.Series(False, index=presentation.index)

    is_target_role = is_statement_role & is_role_include & ~is_role_exclude

    is_not_abstract = ~presentation["is_abstract"].astype(bool)

    mentions_metric = presentation["label"].str.contains(
        metric.mention_pattern,
        case=False,
        regex=True,
        na=False,
    )

    if metric.exclude_label_pattern:
        is_excluded_label = presentation["label"].str.contains(
            metric.exclude_label_pattern,
            case=False,
            regex=True,
            na=False,
        )
    else:
        is_excluded_label = pd.Series(False, index=presentation.index)

    base_candidates = presentation[
        is_target_role
        & is_not_abstract
        & mentions_metric
        & ~is_excluded_label
    ].copy()

    if base_candidates.empty:
        raise TargetRowNotFound(
            f"לא נמצאה אף שורת '{metric.name}' בתוך Statement role ראשי "
            "התואם לכללי ה-Role של המדד."
        )

    if metric.attributable_pattern:
        is_tier_a = base_candidates["label"].str.contains(
            metric.attributable_pattern,
            case=False,
            regex=True,
            na=False,
        )

        tier_a = base_candidates[is_tier_a]

        if len(tier_a) == 1:
            row = tier_a.iloc[0]

            return (
                {
                    "role_uri": str(row["role_uri"]),
                    "role_definition": str(row["role_definition"]),
                    "concept_qname": str(row["concept_qname"]),
                    "label": str(row["label"]),
                    "period_type": str(row["period_type"]),
                    "selection_tier": "attributable_to_shareholders",
                },
                base_candidates,
            )
    else:
        tier_a = base_candidates.iloc[0:0]

    is_tier_b = base_candidates["label"].str.match(
        metric.plain_pattern,
        case=False,
        na=False,
    )

    tier_b = base_candidates[is_tier_b]

    if len(tier_b) == 1:
        row = tier_b.iloc[0]

        return (
            {
                "role_uri": str(row["role_uri"]),
                "role_definition": str(row["role_definition"]),
                "concept_qname": str(row["concept_qname"]),
                "label": str(row["label"]),
                "period_type": str(row["period_type"]),
                "selection_tier": "plain",
            },
            base_candidates,
        )

    raise TargetRowNotFound(
        f"לא ניתן לזהות שורת '{metric.name}' יחידה וחד-משמעית.\n"
        f"מספר שורות מועמדות כוללות: {len(base_candidates)}, "
        f"מתוכן 'attributable': {len(tier_a)}, 'plain': {len(tier_b)}"
    )


# =============================================================================
# 5. FACT MATCHING — context / period / unit / dimensions / entity.
#    `report_date` here is whichever effective date is being requested
#    (current fiscal year-end, or a prior one) — not necessarily the
#    filing's own report_date.
# =============================================================================


def match_facts(
    model_xbrl: Any,
    target_concept_qname: str,
    expected_cik: int,
    report_date: str,
    expected_period_type: str,
) -> pd.DataFrame:
    expected_report_end_date = datetime.strptime(
        report_date, "%Y-%m-%d"
    ).date()

    records: list[dict[str, object]] = []

    for fact_index, fact in enumerate(model_xbrl.facts):
        concept = getattr(fact, "concept", None)

        if concept is None:
            continue

        concept_qname_str = str(getattr(concept, "qname", ""))

        if concept_qname_str != target_concept_qname:
            continue

        context = fact.context
        unit = fact.unit

        if context is None:
            continue

        is_duration = bool(getattr(context, "isStartEndPeriod", False))
        is_instant = bool(getattr(context, "isInstantPeriod", False))

        period_start = None
        period_end = None
        duration_days = None

        if is_duration:
            start_dt = context.startDatetime
            end_dt = context.endDatetime

            if start_dt is not None:
                period_start = start_dt.date().isoformat()

            if end_dt is not None:
                # XBRL duration end dates are exclusive (a point in time
                # at the start of the following day), so the last
                # actually-reported day is end - 1 day.
                period_end = (
                    (end_dt - timedelta(days=1)).date().isoformat()
                )

            if start_dt is not None and end_dt is not None:
                duration_days = (end_dt - start_dt).days

        elif is_instant:
            instant_dt = context.instantDatetime

            if instant_dt is not None:
                # Same exclusive "midnight of the following day"
                # convention as duration end dates — verified empirically
                # against real Arelle output (see engine 46's history).
                period_end = (
                    (instant_dt - timedelta(days=1)).date().isoformat()
                )
                period_start = period_end

        dims = getattr(context, "qnameDims", {}) or {}
        dimensions_count = len(dims)

        dimension_parts = []

        for dim_qname, dim_value in dims.items():
            member_repr = getattr(dim_value, "memberQname", None)

            if member_repr is None:
                member_repr = getattr(dim_value, "typedMember", None)

            dimension_parts.append(f"{dim_qname}={member_repr}")

        dimensions_repr = "; ".join(dimension_parts)

        entity_identifier = None
        entity_cik_ok = False

        entity_id_tuple = getattr(context, "entityIdentifier", None)

        if entity_id_tuple:
            entity_identifier = str(entity_id_tuple[1])

            try:
                entity_cik_ok = int(entity_identifier) == expected_cik
            except ValueError:
                entity_cik_ok = False

        unit_measures = ""
        unit_ok = False

        if unit is not None:
            measures = getattr(unit, "measures", None)

            if measures and measures[0]:
                unit_measures = ",".join(
                    str(measure) for measure in measures[0]
                )
                unit_ok = unit_measures == "iso4217:USD"

        no_dimensions_ok = dimensions_count == 0

        period_end_match_ok = (
            period_end is not None
            and period_end == expected_report_end_date.isoformat()
        )

        duration_annual_ok = (
            duration_days is not None
            and ANNUAL_DURATION_MIN_DAYS
            <= duration_days
            <= ANNUAL_DURATION_MAX_DAYS
        )

        if expected_period_type == "instant":
            period_type_ok = is_instant
        elif expected_period_type == "duration":
            period_type_ok = is_duration and duration_annual_ok
        else:
            period_type_ok = False

        all_filters_ok = (
            unit_ok
            and no_dimensions_ok
            and period_end_match_ok
            and period_type_ok
            and entity_cik_ok
            and not fact.isNil
        )

        value_raw = None if fact.isNil else fact.value

        value_numeric = None

        if not fact.isNil:
            try:
                value_numeric = float(fact.xValue)
            except (TypeError, ValueError):
                try:
                    value_numeric = float(fact.value)
                except (TypeError, ValueError):
                    value_numeric = None

        records.append(
            {
                "fact_index": fact_index,
                "concept_qname": concept_qname_str,
                "context_id": fact.contextID,
                "unit_id": fact.unitID,
                "unit_measures": unit_measures,
                "is_duration": is_duration,
                "is_instant": is_instant,
                "period_start": period_start,
                "period_end": period_end,
                "duration_days": duration_days,
                "entity_identifier": entity_identifier,
                "dimensions_count": dimensions_count,
                "dimensions": dimensions_repr,
                "decimals": fact.decimals,
                "is_nil": bool(fact.isNil),
                "value_raw": value_raw,
                "value_numeric": value_numeric,
                "unit_ok": unit_ok,
                "no_dimensions_ok": no_dimensions_ok,
                "period_end_match_ok": period_end_match_ok,
                "period_type_ok": period_type_ok,
                "entity_cik_ok": entity_cik_ok,
                "all_filters_ok": all_filters_ok,
            }
        )

    return pd.DataFrame(records)


# =============================================================================
# 6. DEDUPLICATION + STATUS DECISION
# =============================================================================


def deduplicate_and_decide(candidates: pd.DataFrame) -> dict[str, object]:
    """
    Collapses technical Inline XBRL duplicates (identical value reported
    under more than one fact instance) and decides PASS vs
    REVIEW_REQUIRED. Never guesses between genuinely different values.
    """

    outcome: dict[str, object] = {
        "matched_fact_count": int(len(candidates)),
        "filtered_fact_count": 0,
        "distinct_value_count": 0,
        "status": "REVIEW_REQUIRED",
        "error": None,
        "selected_value": None,
        "selected_context_id": None,
        "selected_period_start": None,
        "selected_period_end": None,
        "selected_unit": None,
        "selected_decimals": None,
        "note": None,
    }

    if candidates.empty:
        outcome["error"] = (
            "לא נמצא אף Fact עם ה-concept שזוהה מה-Presentation."
        )
        return outcome

    filtered = candidates[candidates["all_filters_ok"]].copy()
    outcome["filtered_fact_count"] = int(len(filtered))

    if filtered.empty:
        outcome["error"] = (
            "נמצאו facts עם ה-concept הנדרש, אך אף אחד לא עמד בכל תנאי "
            "הסינון (unit / ללא dimensions / תאריך תואם / סוג תקופה "
            "(instant/duration) תואם / CIK תואם)."
        )
        return outcome

    distinct_values = sorted(set(filtered["value_numeric"].tolist()))
    outcome["distinct_value_count"] = len(distinct_values)

    if len(distinct_values) == 1:
        selected_row = filtered.iloc[0]

        outcome["status"] = "PASS"
        outcome["selected_value"] = distinct_values[0]
        outcome["selected_context_id"] = str(selected_row["context_id"])
        outcome["selected_period_start"] = selected_row["period_start"]
        outcome["selected_period_end"] = selected_row["period_end"]
        outcome["selected_unit"] = str(selected_row["unit_measures"])
        outcome["selected_decimals"] = str(selected_row["decimals"])

        if len(filtered) > 1:
            outcome["note"] = (
                f"{len(filtered)} facts עברו את הסינון אך כולם בעלי אותו "
                "ערך — טופלו ככפילות טכנית (תופעה מוכרת מ-Inline XBRL)."
            )
    else:
        outcome["error"] = (
            "יותר ממועמד אחד עבר את הסינון עם ערכים שונים "
            f"({distinct_values}) — אין בסיס לבחור אוטומטית."
        )

    return outcome


# =============================================================================
# 7. BOUNDED ORCHESTRATION — child process. Row identification runs once
#    per BUILT_IN metric name (period-independent); fact matching then
#    runs once per (metric, effective_date) request, reusing the cached
#    row/concept — this is what makes prior-fiscal-year-end extraction
#    possible without re-deriving structure.
# =============================================================================


def engine_child_worker(
    primary_document: str,
    cache_directory: str,
    log_file: str,
    http_user_agent: str,
    internet_timeout_seconds: int,
    expected_cik: int,
    row_identify_metric_names: list[str],
    fact_match_requests: list[tuple[str, str, str]],
    presentation_csv: str,
    row_candidates_csv: str,
    fact_candidates_csv: str,
    result_file: str,
) -> None:
    per_metric_results: dict[str, dict[str, object]] = {
        result_key: {
            "status": "FAIL",
            "error": None,
            "target_concept_qname": None,
            "target_role_uri": None,
            "target_role_definition": None,
            "target_label": None,
            "selection_tier": None,
            "period_type": None,
            "matched_fact_count": 0,
            "filtered_fact_count": 0,
            "distinct_value_count": 0,
            "selected_value": None,
            "selected_context_id": None,
            "selected_period_start": None,
            "selected_period_end": None,
            "selected_unit": None,
            "selected_decimals": None,
            "note": None,
        }
        for (_, _, result_key) in fact_match_requests
    }

    presentation_row_count = 0
    all_row_candidates: list[pd.DataFrame] = []
    all_fact_candidates: list[pd.DataFrame] = []

    try:
        from arelle.RuntimeOptions import RuntimeOptions
        from arelle.api.Session import Session

        Path(cache_directory).mkdir(parents=True, exist_ok=True)

        options = RuntimeOptions(
            entrypointFile=primary_document,
            internetConnectivity="online",
            cacheDirectory=cache_directory,
            internetTimeout=internet_timeout_seconds,
            httpUserAgent=http_user_agent,
            keepOpen=True,
            logFile=log_file,
            logFormat=(
                "[%(levelname)s] [%(messageCode)s] "
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
                raise RuntimeError("Arelle לא הצליח לטעון את מודל ה-XBRL.")

            # --- step 2: presentation, loaded once, shared by all metrics
            presentation = extract_presentation(model_xbrl)

            if presentation.empty:
                raise RuntimeError("לא נמצאו Presentation relationships.")

            presentation.to_csv(
                presentation_csv,
                index=False,
                encoding="utf-8-sig",
            )

            presentation_row_count = int(len(presentation))

            # --- steps 3+4: row identification, once per metric name
            target_rows: dict[str, dict[str, str]] = {}
            row_id_errors: dict[str, str] = {}

            for metric_name in row_identify_metric_names:
                metric = BUILT_IN_METRICS[metric_name]

                try:
                    target_row, row_candidates = identify_canonical_row(
                        presentation, metric
                    )
                except TargetRowNotFound as exc:
                    row_id_errors[metric_name] = str(exc)
                    continue

                target_rows[metric_name] = target_row

                row_candidates = row_candidates.copy()
                row_candidates.insert(0, "metric", metric_name)
                all_row_candidates.append(row_candidates)

            # --- steps 5+6: fact matching + dedup, per requested date
            for metric_name, effective_date, result_key in fact_match_requests:
                metric_result = per_metric_results[result_key]

                if metric_name not in target_rows:
                    metric_result["status"] = "REVIEW_REQUIRED"
                    metric_result["error"] = row_id_errors.get(
                        metric_name,
                        f"שורה לא זוהתה עבור '{metric_name}'.",
                    )
                    continue

                target_row = target_rows[metric_name]

                metric_result["target_concept_qname"] = (
                    target_row["concept_qname"]
                )
                metric_result["target_role_uri"] = target_row["role_uri"]
                metric_result["target_role_definition"] = (
                    target_row["role_definition"]
                )
                metric_result["target_label"] = target_row["label"]
                metric_result["selection_tier"] = (
                    target_row["selection_tier"]
                )
                metric_result["period_type"] = target_row["period_type"]

                fact_candidates = match_facts(
                    model_xbrl=model_xbrl,
                    target_concept_qname=target_row["concept_qname"],
                    expected_cik=expected_cik,
                    report_date=effective_date,
                    expected_period_type=target_row["period_type"],
                )

                fact_candidates_tagged = fact_candidates.copy()
                fact_candidates_tagged.insert(0, "metric", result_key)
                all_fact_candidates.append(fact_candidates_tagged)

                decision = deduplicate_and_decide(fact_candidates)

                metric_result.update(decision)

    except Exception as exc:
        error_text = f"{exc}\n{traceback.format_exc()}"

        for metric_result in per_metric_results.values():
            if metric_result["status"] not in ("PASS", "REVIEW_REQUIRED"):
                metric_result["status"] = "FAIL"
                metric_result["error"] = error_text

    if all_row_candidates:
        pd.concat(all_row_candidates, ignore_index=True).to_csv(
            row_candidates_csv,
            index=False,
            encoding="utf-8-sig",
        )

    if all_fact_candidates:
        pd.concat(all_fact_candidates, ignore_index=True).to_csv(
            fact_candidates_csv,
            index=False,
            encoding="utf-8-sig",
        )

    Path(result_file).write_text(
        json.dumps(
            {
                "presentation_row_count": presentation_row_count,
                "metrics": per_metric_results,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


# =============================================================================
# 8. DERIVED + CUSTOM METRICS
# =============================================================================


def _component_lineage(result: dict[str, object]) -> dict[str, object]:
    return {
        "status": result.get("status"),
        "value": result.get("value"),
        "concept": result.get("source_concept"),
        "context_id": result.get("context_id"),
        "period_start": result.get("period_start"),
        "period_end": result.get("period_end"),
        "unit": result.get("unit"),
        "statement_role_definition": result.get("statement_role_definition"),
        "label": result.get("label"),
        "is_derived_metric": bool(result.get("is_derived_metric")),
        "formula": result.get("formula"),
    }


def compute_derived_metric(
    definition: DerivedMetricDefinition,
    metrics_out: dict[str, object],
) -> dict[str, object]:
    component_names = definition.component_metrics
    components = {name: metrics_out.get(name) for name in component_names}

    missing = [name for name, result in components.items() if result is None]

    if missing:
        return {
            "status": "FAIL",
            "error": (
                "לא ניתן לחשב מדד נגזר — רכיבים חסרים: "
                f"{missing}. יש לכלול אותם בבקשה."
            ),
            "is_derived_metric": True,
            "formula": definition.formula_description,
            "value": None,
            "components": components,
        }

    statuses = [str(result.get("status")) for result in components.values()]

    if any(status == "TIMEOUT" for status in statuses):
        overall_status = "TIMEOUT"
    elif any(status == "FAIL" for status in statuses):
        overall_status = "FAIL"
    elif any(status != "PASS" for status in statuses):
        overall_status = "REVIEW_REQUIRED"
    else:
        overall_status = "PASS"

    component_lineage = {
        name: _component_lineage(result) for name, result in components.items()
    }

    if overall_status != "PASS":
        return {
            "status": overall_status,
            "error": (
                "לא ניתן לחשב מדד נגזר כי לא כל הרכיבים עברו PASS: "
                f"{ {name: result.get('status') for name, result in components.items()} }"
            ),
            "is_derived_metric": True,
            "formula": definition.formula_description,
            "value": None,
            "components": component_lineage,
        }

    if definition.require_same_period:
        periods = {
            (result.get("period_start"), result.get("period_end"))
            for result in components.values()
        }

        if len(periods) != 1:
            period_summary = {
                name: (result.get("period_start"), result.get("period_end"))
                for name, result in components.items()
            }

            return {
                "status": "REVIEW_REQUIRED",
                "error": (
                    "תקופות הדיווח של הרכיבים אינן זהות — אין בסיס "
                    f"לחישוב אוטומטי.\n{period_summary}"
                ),
                "is_derived_metric": True,
                "formula": definition.formula_description,
                "value": None,
                "components": component_lineage,
            }

        reference_component = components[component_names[0]]
        result_period_start = reference_component.get("period_start")
        result_period_end = reference_component.get("period_end")
    else:
        starts = [
            result.get("period_start")
            for result in components.values()
            if result.get("period_start")
        ]
        ends = [
            result.get("period_end")
            for result in components.values()
            if result.get("period_end")
        ]
        result_period_start = min(starts) if starts else None
        result_period_end = max(ends) if ends else None

    values = [components[name].get("value") for name in component_names]
    value = definition.combine(values)

    if value is None:
        return {
            "status": "REVIEW_REQUIRED",
            "error": (
                "הנוסחה לא הניבה ערך תקין (למשל חלוקה במכנה לא חיובי) — "
                "אין בסיס לבחור אוטומטית."
            ),
            "is_derived_metric": True,
            "formula": definition.formula_description,
            "value": None,
            "components": component_lineage,
        }

    reference_component = components[component_names[0]]

    return {
        "status": "PASS",
        "error": None,
        "is_derived_metric": True,
        "formula": definition.formula_description,
        "value": value,
        "unit": reference_component.get("unit"),
        "period_start": result_period_start,
        "period_end": result_period_end,
        "components": component_lineage,
    }


def compute_total_debt(
    metrics_out: dict[str, object],
    current_debt_key: str,
    long_term_debt_key: str,
    explicit_key: str,
) -> dict[str, object]:
    """
    Accounting policy (explicit user decision):
    - Total Debt includes only interest-bearing debt (current/short-term
      borrowings, current portion of long-term debt, long-term debt) —
      never accounts payable, operating liabilities, or operating lease
      liabilities. The current_debt / long_term_debt row-selection
      patterns already only ever match debt-labeled rows, so this is
      structural, not a post-hoc filter.
    - Prefer a single, explicit "Total debt" row when the filing's own
      Statement Role, context, and instant date validate it.
    - Otherwise, sum current_debt + long_term_debt — but only because
      those two are already guaranteed structurally non-overlapping
      (selected via mutually-exclusive label patterns; see
      BUILT_IN_METRICS), never a summation across candidates that merely
      "look separate".
    - A missing current-debt row is never inferred as zero debt — it
      leaves the sum path unavailable and this metric REVIEW_REQUIRED.
    """

    explicit = metrics_out.get(explicit_key)

    if explicit is not None and explicit.get("status") == "PASS":
        return {
            "status": "PASS",
            "error": None,
            "is_derived_metric": True,
            "formula": (
                f"explicit '{explicit.get('label')}' row "
                "(Statement Role + instant context validated)"
            ),
            "value": explicit.get("value"),
            "unit": explicit.get("unit"),
            "period_start": explicit.get("period_start"),
            "period_end": explicit.get("period_end"),
            "components": {explicit_key: _component_lineage(explicit)},
        }

    explicit_status = explicit.get("status") if explicit else "not requested"

    sum_definition = DerivedMetricDefinition(
        name="total_debt_sum_fallback",
        component_metrics=(current_debt_key, long_term_debt_key),
        formula_description=(
            f"{current_debt_key} + {long_term_debt_key} (no unambiguous "
            f"explicit Total Debt row found — explicit-row status: "
            f"{explicit_status})"
        ),
        combine=lambda values: values[0] + values[1],
    )

    return compute_derived_metric(sum_definition, metrics_out)


def compute_effective_tax_rate(metrics_out: dict[str, object]) -> dict[str, object]:
    """
    Reported Effective Tax Rate = income_tax_expense / pretax_income.
    Accounting policy (explicit user decision): REVIEW_REQUIRED, never a
    guess, if pretax income is not positive or the resulting rate falls
    outside the plausible [0, 1] range.
    """

    components = {
        "pretax_income": metrics_out.get("pretax_income"),
        "income_tax_expense": metrics_out.get("income_tax_expense"),
    }

    missing = [name for name, result in components.items() if result is None]

    if missing:
        return {
            "status": "FAIL",
            "error": f"רכיבים חסרים: {missing}.",
            "is_derived_metric": True,
            "formula": "income_tax_expense / pretax_income",
            "value": None,
            "components": components,
        }

    statuses = [str(result.get("status")) for result in components.values()]

    if any(status == "TIMEOUT" for status in statuses):
        overall_status = "TIMEOUT"
    elif any(status == "FAIL" for status in statuses):
        overall_status = "FAIL"
    elif any(status != "PASS" for status in statuses):
        overall_status = "REVIEW_REQUIRED"
    else:
        overall_status = "PASS"

    component_lineage = {
        name: _component_lineage(result) for name, result in components.items()
    }

    if overall_status != "PASS":
        return {
            "status": overall_status,
            "error": (
                "לא ניתן לחשב Effective Tax Rate כי לא כל הרכיבים עברו "
                f"PASS: { {name: result.get('status') for name, result in components.items()} }"
            ),
            "is_derived_metric": True,
            "formula": "income_tax_expense / pretax_income",
            "value": None,
            "components": component_lineage,
        }

    pretax = components["pretax_income"]
    tax = components["income_tax_expense"]

    if (pretax.get("period_start"), pretax.get("period_end")) != (
        tax.get("period_start"),
        tax.get("period_end"),
    ):
        return {
            "status": "REVIEW_REQUIRED",
            "error": (
                "תקופות הדיווח של Pretax Income ו-Income Tax Expense "
                "אינן זהות — אין בסיס לחישוב אוטומטי."
            ),
            "is_derived_metric": True,
            "formula": "income_tax_expense / pretax_income",
            "value": None,
            "components": component_lineage,
        }

    pretax_value = pretax.get("value")
    tax_value = tax.get("value")

    if pretax_value is None or pretax_value <= 0:
        return {
            "status": "REVIEW_REQUIRED",
            "error": (
                f"Pretax Income אינו חיובי ({pretax_value}) — לא ניתן "
                "לחשב שיעור מס אפקטיבי מהימן."
            ),
            "is_derived_metric": True,
            "formula": "income_tax_expense / pretax_income",
            "value": None,
            "components": component_lineage,
        }

    rate = tax_value / pretax_value

    if not (0 <= rate <= 1):
        return {
            "status": "REVIEW_REQUIRED",
            "error": (
                f"שיעור המס האפקטיבי המדווח ({rate}) מחוץ לטווח הסביר "
                "[0, 1]."
            ),
            "is_derived_metric": True,
            "formula": "income_tax_expense / pretax_income",
            "value": rate,
            "components": component_lineage,
        }

    return {
        "status": "PASS",
        "error": None,
        "is_derived_metric": True,
        "formula": "income_tax_expense / pretax_income",
        "value": rate,
        "unit": "ratio",
        "period_start": pretax.get("period_start"),
        "period_end": pretax.get("period_end"),
        "components": component_lineage,
    }


def compute_custom_metric(
    name: str,
    metrics_out: dict[str, object],
) -> dict[str, object]:
    if name == "total_debt":
        return compute_total_debt(
            metrics_out, "current_debt", "long_term_debt", "total_debt_explicit"
        )

    if name == "total_debt_prior":
        return compute_total_debt(
            metrics_out,
            "current_debt_prior",
            "long_term_debt_prior",
            "total_debt_explicit_prior",
        )

    if name == "effective_tax_rate":
        return compute_effective_tax_rate(metrics_out)

    raise ValueError(f"מדד custom לא מוכר: {name}")


def run_engine(
    ticker: str,
    report_date: str,
    metric_names: list[str],
) -> dict[str, object]:
    paths = output_paths(ticker, report_date)

    locked_filing = load_locked_filing(ticker, report_date)

    (
        required_built_in_current,
        required_built_in_prior,
        ordered_custom,
        ordered_derived,
    ) = resolve_metric_dependencies(metric_names)

    prior_report_date = (
        compute_prior_report_date(report_date)
        if required_built_in_prior
        else None
    )

    row_identify_metric_names = sorted(
        set(required_built_in_current) | set(required_built_in_prior)
    )

    fact_match_requests: list[tuple[str, str, str]] = [
        (name, report_date, name) for name in required_built_in_current
    ] + [
        (name, prior_report_date, f"{name}_prior")
        for name in required_built_in_prior
    ]

    log_line(paths["orchestration_log_file"], "=" * 100)
    log_line(
        paths["orchestration_log_file"],
        f"{ticker.upper()} {report_date} — GENERIC XBRL METRIC ENGINE v5 "
        f"[{', '.join(metric_names)}]",
    )
    log_line(paths["orchestration_log_file"], "=" * 100)
    log_line(
        paths["orchestration_log_file"],
        f"קובץ 10-K: {locked_filing['primary_document_path']}",
    )
    log_line(
        paths["orchestration_log_file"],
        f"Accession: {locked_filing['accession_compact']}",
    )
    log_line(
        paths["orchestration_log_file"],
        f"BUILT_IN (שנה נוכחית, {report_date}): {required_built_in_current}",
    )
    log_line(
        paths["orchestration_log_file"],
        f"BUILT_IN (שנה קודמת, {prior_report_date}): {required_built_in_prior}",
    )
    log_line(
        paths["orchestration_log_file"],
        f"CUSTOM נדרשים: {ordered_custom}",
    )
    log_line(
        paths["orchestration_log_file"],
        f"DERIVED נדרשים (בסדר תלות): {ordered_derived}",
    )
    log_line(
        paths["orchestration_log_file"],
        f"Total timeout: {TOTAL_TIMEOUT_SECONDS}s | "
        f"Per-connection timeout: {INTERNET_TIMEOUT_SECONDS}s",
    )

    if paths["result_file"].exists():
        paths["result_file"].unlink()

    process = multiprocessing.Process(
        target=engine_child_worker,
        kwargs={
            "primary_document": str(
                locked_filing["primary_document_path"]
            ),
            "cache_directory": str(CACHE_DIR),
            "log_file": str(paths["arelle_log_file"]),
            "http_user_agent": locked_filing["sec_user_agent"],
            "internet_timeout_seconds": INTERNET_TIMEOUT_SECONDS,
            "expected_cik": locked_filing["cik"],
            "row_identify_metric_names": row_identify_metric_names,
            "fact_match_requests": fact_match_requests,
            "presentation_csv": str(paths["presentation_csv"]),
            "row_candidates_csv": str(paths["row_candidates_csv"]),
            "fact_candidates_csv": str(paths["fact_candidates_csv"]),
            "result_file": str(paths["result_file"]),
        },
    )

    run_started_at = datetime.now(timezone.utc)
    start_perf = time.perf_counter()

    log_line(paths["orchestration_log_file"], "מפעיל child process...")
    process.start()

    process.join(timeout=TOTAL_TIMEOUT_SECONDS)

    timed_out = False

    if process.is_alive():
        timed_out = True
        log_line(
            paths["orchestration_log_file"],
            f"חריגה מ-{TOTAL_TIMEOUT_SECONDS} שניות — "
            "שולח terminate() ל-child process.",
        )
        process.terminate()
        process.join(timeout=TERMINATE_GRACE_SECONDS)

        if process.is_alive():
            log_line(
                paths["orchestration_log_file"],
                "terminate() לא הספיק — שולח kill().",
            )
            process.kill()
            process.join(timeout=TERMINATE_GRACE_SECONDS)

    elapsed_seconds = time.perf_counter() - start_perf
    run_ended_at = datetime.now(timezone.utc)

    child_exit_code = process.exitcode
    log_line(
        paths["orchestration_log_file"],
        f"Child process הסתיים. exit_code={child_exit_code}",
    )

    child_result: dict[str, object] = {}

    if paths["result_file"].exists():
        child_result = json.loads(
            paths["result_file"].read_text(encoding="utf-8")
        )

    metrics_out: dict[str, object] = {}

    for metric_name, effective_date, result_key in fact_match_requests:
        if timed_out:
            metrics_out[result_key] = {
                "status": "TIMEOUT",
                "error": (
                    f"ה-child process לא הסתיים תוך "
                    f"{TOTAL_TIMEOUT_SECONDS} שניות ונהרג באופן אוטומטי."
                ),
            }
            continue

        metric_child_result = child_result.get("metrics", {}).get(result_key)

        if metric_child_result is None:
            metrics_out[result_key] = {
                "status": "FAIL",
                "error": (
                    "ה-child process הסתיים אך לא נכתבה תוצאה למדד זה. "
                    f"exit_code={child_exit_code}"
                ),
            }
            continue

        qa_reference = find_qa_reference_value(
            ticker, effective_date, metric_name
        )

        metric_child_result = dict(metric_child_result)
        metric_child_result["value"] = metric_child_result.get(
            "selected_value"
        )
        metric_child_result["context_id"] = metric_child_result.get(
            "selected_context_id"
        )
        metric_child_result["period_start"] = metric_child_result.get(
            "selected_period_start"
        )
        metric_child_result["period_end"] = metric_child_result.get(
            "selected_period_end"
        )
        metric_child_result["unit"] = metric_child_result.get(
            "selected_unit"
        )
        metric_child_result["source_concept"] = metric_child_result.get(
            "target_concept_qname"
        )
        metric_child_result["statement_role_definition"] = (
            metric_child_result.get("target_role_definition")
        )
        metric_child_result["label"] = metric_child_result.get(
            "target_label"
        )

        metrics_out[result_key] = {
            **metric_child_result,
            "qa_reference_only_not_used_for_selection": qa_reference,
        }

    for custom_name in ordered_custom:
        custom_result = compute_custom_metric(custom_name, metrics_out)

        qa_reference = find_qa_reference_value(
            ticker, report_date, custom_name
        )

        custom_result["qa_reference_only_not_used_for_selection"] = (
            qa_reference
        )

        metrics_out[custom_name] = custom_result

    for derived_name in ordered_derived:
        derived_result = compute_derived_metric(
            DERIVED_METRICS[derived_name], metrics_out
        )

        qa_reference = find_qa_reference_value(
            ticker, report_date, derived_name
        )

        derived_result["qa_reference_only_not_used_for_selection"] = (
            qa_reference
        )

        metrics_out[derived_name] = derived_result

    final_result = {
        "ticker": locked_filing["ticker"],
        "company_name": locked_filing["company_name"],
        "cik": locked_filing["cik"],
        "form": EXPECTED_FORM,
        "accession_number": locked_filing["accession_number"],
        "accession_compact": locked_filing["accession_compact"],
        "report_date": locked_filing["report_date"],
        "prior_report_date": prior_report_date,
        "filing_date": locked_filing["filing_date"],
        "source_document": locked_filing["primary_document_name"],
        "primary_document_path": str(
            locked_filing["primary_document_path"]
        ),
        "manifest_file": str(locked_filing["manifest_file"]),
        "run_started_at_utc": run_started_at.isoformat(),
        "run_ended_at_utc": run_ended_at.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "total_timeout_seconds": TOTAL_TIMEOUT_SECONDS,
        "internet_timeout_seconds": INTERNET_TIMEOUT_SECONDS,
        "cache_directory": str(CACHE_DIR),
        "http_user_agent": locked_filing["sec_user_agent"],
        "child_exit_code": child_exit_code,
        "timed_out": timed_out,
        "presentation_row_count": child_result.get(
            "presentation_row_count", 0
        ),
        "metrics_requested": metric_names,
        "built_in_required_current": required_built_in_current,
        "built_in_required_prior": required_built_in_prior,
        "custom_required_ordered": ordered_custom,
        "derived_required_ordered": ordered_derived,
        "metrics": metrics_out,
        "all_pass": all(
            metrics_out.get(name, {}).get("status") == "PASS"
            for name in metric_names
        ),
        "presentation_csv": (
            str(paths["presentation_csv"])
            if paths["presentation_csv"].exists()
            else None
        ),
        "row_candidates_csv": (
            str(paths["row_candidates_csv"])
            if paths["row_candidates_csv"].exists()
            else None
        ),
        "fact_candidates_csv": (
            str(paths["fact_candidates_csv"])
            if paths["fact_candidates_csv"].exists()
            else None
        ),
        "arelle_log_file": str(paths["arelle_log_file"]),
        "orchestration_log_file": str(paths["orchestration_log_file"]),
    }

    paths["result_file"].write_text(
        json.dumps(final_result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    for metric_name in metric_names:
        log_line(
            paths["orchestration_log_file"],
            f"{metric_name}: {metrics_out.get(metric_name, {}).get('status')}",
        )

    log_line(
        paths["orchestration_log_file"],
        f"קובץ תוצאה: {paths['result_file']}",
    )

    return final_result


def main() -> None:
    arguments = parse_arguments()

    result = run_engine(
        ticker=arguments.ticker,
        report_date=arguments.report_date,
        metric_names=arguments.metrics,
    )

    print()
    print("=" * 100)
    print(
        f"תוצאת מנוע ה-XBRL הגנרי — {arguments.ticker.upper()} "
        f"{arguments.report_date}"
    )
    print("=" * 100)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
