"""Resolves ticker -> (CIK, canonical company name) uniformly for any
company SEC has ever assigned a CIK to -- currently listed or long
delisted, acquired, or taken private.

scripts/107_download_accession_locked_filing_any_form.py and
scripts/162_download_xbrl_only_filing.py both resolve CIK exclusively
through SEC's `company_tickers.json`, which lists ONLY tickers SEC
currently treats as actively registered. A company removed from an
index years ago (e.g. acquired) is usually absent from that file even
though EDGAR still holds its full filing history under the CIK it
always had -- verified directly against SEC's own `browse-edgar` company
search for Citrix Systems (delisted 2022, ticker CTXS no longer in
company_tickers.json) and Maxim Integrated (delisted 2021, ticker MXIM):
both resolve cleanly by company name, output=atom, to CIK 0000877890
and 0000743316 respectively.

This module adds exactly ONE additional, uniform fallback tier, tried
identically for every ticker -- never a special case selected because a
ticker is "known to be delisted" (this project's own D-002/D-008
discipline: no ticker-specific branches, fail closed on ambiguity):
  1. Fast path: SEC's `company_tickers.json`, keyed by ticker symbol
     (same source scripts/107/162 already use).
  2. Fallback, tried only if (1) finds nothing: SEC's own company-NAME
     search (`browse-edgar?action=getcompany&company=...&output=atom`),
     which is not restricted to active registrants. The caller supplies
     the company name to search by -- SEC's `company=` parameter matches
     on registrant name, not ticker, so the ticker symbol itself cannot
     drive this fallback. Requires the match to be unique and requires
     the returned conformed name to start with the supplied name
     (case-insensitive) -- anything else is REVIEW_REQUIRED, never a
     guess among multiple candidates.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from stock_agent.ingestion.rate_limiter import SEC_RATE_LIMITER

SEC_BASE_URL = "https://www.sec.gov"
USER_AGENT = "Shai Attias shaiattias@gmail.com"
REQUEST_TIMEOUT_SECONDS = 60

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class CikResolutionError(Exception):
    """Raised when neither the ticker fast-path nor the company-name
    fallback can uniquely resolve a CIK -- fail closed, never a guess."""


def _sec_get_bytes(url: str) -> bytes:
    SEC_RATE_LIMITER.acquire()
    request = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json,application/atom+xml,text/xml,*/*",
        "Accept-Encoding": "identity",
        "Connection": "close",
    })
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as error:
        raise RuntimeError(f"SEC HTTP error {error.code} for {url}") from error
    except URLError as error:
        raise RuntimeError(f"Could not connect to SEC: {url}: {error}") from error


def resolve_cik_by_ticker(ticker: str) -> dict[str, Any] | None:
    """Fast path: SEC's company_tickers.json, keyed by ticker. Returns
    None (never raises) when the ticker is not present, so the caller
    can try the company-name fallback -- the same behavior for every
    ticker, active or delisted."""
    import json
    ticker_data = json.loads(_sec_get_bytes(f"{SEC_BASE_URL}/files/company_tickers.json").decode("utf-8"))
    matches = [r for r in ticker_data.values() if str(r.get("ticker", "")).upper() == ticker.upper()]
    if len(matches) != 1:
        return None
    match = matches[0]
    return {"cik_str": int(match["cik_str"]), "title": str(match.get("title", "")), "resolution_method": "TICKER_ACTIVE_REGISTRY"}


def resolve_cik_by_company_name(company_name: str, form: str = "10-K") -> dict[str, Any] | None:
    """Fallback: SEC's own company-name search, not restricted to
    active registrants. Fails closed (returns None) unless exactly one
    CIK is returned AND its conformed name starts with the supplied
    name (case-insensitive, whitespace-normalized) -- never picks among
    multiple candidates."""
    from urllib.parse import quote
    url = (
        f"{SEC_BASE_URL}/cgi-bin/browse-edgar?action=getcompany&company={quote(company_name)}"
        f"&type={quote(form)}&dateb=&owner=include&count=40&output=atom"
    )
    raw = _sec_get_bytes(url)
    root = ElementTree.fromstring(raw)
    cik_elements = root.findall(".//atom:company-info/atom:cik", _ATOM_NS)
    name_elements = root.findall(".//atom:company-info/atom:conformed-name", _ATOM_NS)
    if len(cik_elements) != 1 or len(name_elements) != 1:
        return None

    conformed_name = (name_elements[0].text or "").strip()
    normalized_query = re.sub(r"\s+", " ", company_name).strip().upper()
    normalized_result = re.sub(r"\s+", " ", conformed_name).strip().upper()
    if not normalized_result.startswith(normalized_query):
        return None

    return {
        "cik_str": int(cik_elements[0].text), "title": conformed_name,
        "resolution_method": "COMPANY_NAME_SEARCH_FULL_REGISTRY",
    }


def resolve_company_record(ticker: str, company_name_hint: str | None = None, form: str = "10-K") -> dict[str, Any]:
    """Drop-in-compatible with scripts/107/162's find_company_record()
    return shape ({"cik_str": int, "title": str}), plus a
    "resolution_method" field disclosing which tier resolved it.
    Raises CikResolutionError (never guesses) if the ticker fast-path
    finds nothing AND no company_name_hint was supplied, or if the
    name-search fallback itself does not uniquely resolve."""
    by_ticker = resolve_cik_by_ticker(ticker)
    if by_ticker is not None:
        return by_ticker

    if not company_name_hint:
        raise CikResolutionError(
            f"Ticker {ticker!r} not found in SEC's active company_tickers.json, and no "
            "company_name_hint was supplied to try the full-registry name-search fallback."
        )

    by_name = resolve_cik_by_company_name(company_name_hint, form=form)
    if by_name is not None:
        return by_name

    raise CikResolutionError(
        f"Ticker {ticker!r} not found in SEC's active company_tickers.json, and the company-name "
        f"fallback for {company_name_hint!r} did not uniquely resolve (zero or ambiguous matches, "
        "or the returned conformed name did not start with the supplied name)."
    )
