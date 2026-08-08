"""
filings/locked.py — thin helpers for reading locked-filing manifests
under data/sec_filings_locked/. Deliberately minimal for PR1: just
enough for warehouse/extraction code to find the exact locked package
for a (ticker, report_date, form), the same job
warehouse.loader.find_locked_manifest already does for one caller.
This module factors that lookup out to a shared, general helper so any
future caller (not just the warehouse loader) can use it without its
own copy.
"""

from __future__ import annotations

import json
from pathlib import Path

from stock_agent import PROJECT_DIR

LOCKED_FILINGS_DIR = PROJECT_DIR / "data" / "sec_filings_locked"


class LockedFilingNotFound(Exception):
    pass


def find_locked_manifest(ticker: str, report_date: str, form: str = "10-K") -> dict:
    """Returns the parsed locked_filing_manifest.json for the single
    locked package matching (ticker, report_date, form) under
    data/sec_filings_locked/<TICKER>/<accession>/. Raises
    LockedFilingNotFound if zero or more than one manifest matches
    (fail closed, never guess — consistent with this project's D-002 /
    D-008 rules)."""

    locked_dir = LOCKED_FILINGS_DIR / ticker.upper()
    manifests = sorted(locked_dir.glob("*/locked_filing_manifest.json"))
    matching = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in manifests]
    matching = [
        (p, m) for p, m in matching
        if m.get("report_date") == report_date and m.get("form") == form
    ]

    if len(matching) != 1:
        raise LockedFilingNotFound(
            f"Expected exactly 1 locked {form} manifest for {ticker}/{report_date}, found {len(matching)}."
        )

    manifest_path, manifest = matching[0]
    manifest["_manifest_path"] = str(manifest_path)
    manifest["_locked_dir"] = str(manifest_path.parent)
    return manifest
