from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
DATA_DIR = PROJECT_DIR / "data"

EXTRACT_SCRIPT = (
    SCRIPTS_DIR
    / "31_extract_inline_xbrl_facts.py"
)

DEDUPLICATE_SCRIPT = (
    SCRIPTS_DIR
    / "33_deduplicate_inline_xbrl_facts.py"
)

SELECT_METRICS_SCRIPT = (
    SCRIPTS_DIR
    / "34b_extract_core_income_metrics_with_priority.py"
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the complete annual income-metrics pipeline: "
            "extract Inline XBRL, remove technical duplicates, "
            "and select accounting metrics by concept priority."
        )
    )

    parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker, for example META, MSFT or ORCL.",
    )

    parser.add_argument(
        "--year",
        required=True,
        type=int,
        help="10-K filing year, for example 2024.",
    )

    return parser.parse_args()


def verify_required_scripts() -> None:
    required_scripts = [
        EXTRACT_SCRIPT,
        DEDUPLICATE_SCRIPT,
        SELECT_METRICS_SCRIPT,
    ]

    missing_scripts = [
        script
        for script in required_scripts
        if not script.exists()
    ]

    if missing_scripts:
        missing_text = "\n".join(
            str(script)
            for script in missing_scripts
        )

        raise FileNotFoundError(
            "חסרים סקריפטים הדרושים להפעלת התהליך:\n"
            f"{missing_text}"
        )


def run_script(
    script_path: Path,
    ticker: str,
    filing_year: int,
    step_number: int,
    step_name: str,
) -> None:
    command = [
        sys.executable,
        str(script_path),
        "--ticker",
        ticker,
        "--year",
        str(filing_year),
    ]

    print()
    print("=" * 110)
    print(
        f"שלב {step_number}: {step_name}"
    )
    print("=" * 110)

    print(
        "פקודה:"
    )
    print(
        " ".join(command)
    )
    print()

    completed_process = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        check=False,
    )

    if completed_process.returncode != 0:
        raise RuntimeError(
            f"שלב {step_number} נכשל.\n"
            f"שם השלב: {step_name}\n"
            f"קוד יציאה: "
            f"{completed_process.returncode}"
        )

    print()
    print(
        f"שלב {step_number} הסתיים בהצלחה."
    )


def build_output_files(
    ticker: str,
    filing_year: int,
) -> dict[str, Path]:
    ticker_lower = ticker.lower()

    return {
        "raw_facts": (
            DATA_DIR
            / (
                f"{ticker_lower}_"
                f"{filing_year}_"
                "inline_xbrl_facts.csv"
            )
        ),
        "deduplicated_facts": (
            DATA_DIR
            / (
                f"{ticker_lower}_"
                f"{filing_year}_"
                "inline_xbrl_facts_deduplicated.csv"
            )
        ),
        "duplicate_report": (
            DATA_DIR
            / (
                f"{ticker_lower}_"
                f"{filing_year}_"
                "inline_xbrl_duplicate_report.csv"
            )
        ),
        "selected_metrics": (
            DATA_DIR
            / (
                f"{ticker_lower}_"
                f"{filing_year}_"
                "core_income_metrics_selected.csv"
            )
        ),
    }


def verify_output_files(
    output_files: dict[str, Path],
) -> None:
    missing_files = [
        path
        for path in output_files.values()
        if not path.exists()
    ]

    if missing_files:
        missing_text = "\n".join(
            str(path)
            for path in missing_files
        )

        raise FileNotFoundError(
            "התהליך הסתיים אך חסרים קובצי פלט:\n"
            f"{missing_text}"
        )


def load_selected_metrics(
    selected_metrics_file: Path,
) -> pd.DataFrame:
    metrics = pd.read_csv(
        selected_metrics_file,
        dtype=str,
        keep_default_na=False,
    )

    required_columns = {
        "metric",
        "fiscal_year",
        "selected_concept",
        "selected_value",
        "status",
    }

    missing_columns = (
        required_columns
        - set(metrics.columns)
    )

    if missing_columns:
        raise RuntimeError(
            "בקובץ התוצאה חסרות עמודות:\n"
            + "\n".join(
                sorted(missing_columns)
            )
        )

    return metrics


def validate_pipeline_result(
    metrics: pd.DataFrame,
    filing_year: int,
) -> tuple[bool, pd.DataFrame]:
    target_metrics = {
        "revenue",
        "operating_income",
        "net_income",
    }

    target_years = {
        filing_year,
        filing_year - 1,
        filing_year - 2,
    }

    expected_pairs = {
        (metric, year)
        for metric in target_metrics
        for year in target_years
    }

    actual_pairs = set()

    for _, row in metrics.iterrows():
        try:
            fiscal_year = int(
                row["fiscal_year"]
            )
        except ValueError:
            continue

        actual_pairs.add(
            (
                row["metric"],
                fiscal_year,
            )
        )

    missing_pairs = (
        expected_pairs
        - actual_pairs
    )

    review_rows = metrics[
        metrics["status"]
        == "REVIEW_REQUIRED"
    ].copy()

    passed = (
        not missing_pairs
        and review_rows.empty
    )

    if missing_pairs:
        missing_records = [
            {
                "metric": metric,
                "fiscal_year": year,
                "issue": "MISSING",
            }
            for metric, year in sorted(
                missing_pairs
            )
        ]

        missing_df = pd.DataFrame(
            missing_records
        )
    else:
        missing_df = pd.DataFrame(
            columns=[
                "metric",
                "fiscal_year",
                "issue",
            ]
        )

    if not review_rows.empty:
        review_issues = review_rows[
            [
                "metric",
                "fiscal_year",
            ]
        ].copy()

        review_issues["issue"] = (
            "REVIEW_REQUIRED"
        )

        issues = pd.concat(
            [
                missing_df,
                review_issues,
            ],
            ignore_index=True,
        )
    else:
        issues = missing_df

    return passed, issues


def print_final_summary(
    ticker: str,
    filing_year: int,
    metrics: pd.DataFrame,
    passed: bool,
    issues: pd.DataFrame,
    output_files: dict[str, Path],
) -> None:
    print()
    print("=" * 110)
    print(
        f"סיכום תהליך — {ticker} {filing_year}"
    )
    print("=" * 110)

    display = metrics[
        [
            "metric",
            "fiscal_year",
            "selected_concept",
            "selected_value",
            "status",
        ]
    ].copy()

    display["selected_value"] = (
        pd.to_numeric(
            display["selected_value"],
            errors="coerce",
        )
        .map(
            lambda value: (
                ""
                if pd.isna(value)
                else f"{value:,.0f}"
            )
        )
    )

    display = display.sort_values(
        [
            "metric",
            "fiscal_year",
        ]
    )

    print()
    print(
        display.to_string(
            index=False
        )
    )

    print()
    print("=" * 110)

    if passed:
        print(
            "תוצאת התהליך: PASS"
        )
        print(
            "נמצאו שלושת המדדים "
            "עבור שלוש שנות הכספים, "
            "ללא ערכים הדורשים בדיקה."
        )
    else:
        print(
            "תוצאת התהליך: REVIEW_REQUIRED"
        )

        if not issues.empty:
            print()
            print(
                issues.to_string(
                    index=False
                )
            )

    print()
    print(
        "קובצי הפלט:"
    )

    for output_name, output_path in (
        output_files.items()
    ):
        print(
            f"- {output_name}: "
            f"{output_path}"
        )


def main() -> None:
    arguments = parse_arguments()

    ticker = arguments.ticker.upper()
    filing_year = arguments.year

    verify_required_scripts()

    print()
    print("=" * 110)
    print(
        "AI STOCK AGENT — INCOME PIPELINE"
    )
    print("=" * 110)

    print(
        f"Ticker: {ticker}"
    )
    print(
        f"10-K year: {filing_year}"
    )

    run_script(
        script_path=EXTRACT_SCRIPT,
        ticker=ticker,
        filing_year=filing_year,
        step_number=1,
        step_name=(
            "חילוץ Facts מתוך Inline XBRL"
        ),
    )

    run_script(
        script_path=DEDUPLICATE_SCRIPT,
        ticker=ticker,
        filing_year=filing_year,
        step_number=2,
        step_name=(
            "הסרת כפילויות טכניות"
        ),
    )

    run_script(
        script_path=SELECT_METRICS_SCRIPT,
        ticker=ticker,
        filing_year=filing_year,
        step_number=3,
        step_name=(
            "בחירת מדדי רווח והפסד "
            "לפי עדיפות חשבונאית"
        ),
    )

    output_files = build_output_files(
        ticker=ticker,
        filing_year=filing_year,
    )

    verify_output_files(
        output_files
    )

    metrics = load_selected_metrics(
        output_files[
            "selected_metrics"
        ]
    )

    passed, issues = (
        validate_pipeline_result(
            metrics=metrics,
            filing_year=filing_year,
        )
    )

    print_final_summary(
        ticker=ticker,
        filing_year=filing_year,
        metrics=metrics,
        passed=passed,
        issues=issues,
        output_files=output_files,
    )


if __name__ == "__main__":
    main()