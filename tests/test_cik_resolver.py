"""
tests/test_cik_resolver.py -- proves the "no special-casing" requirement
for the point-in-time universe work: resolve_company_record() is ONE
function, tried in the same order for every ticker; whether a given
ticker resolves via the fast path or the fallback is decided purely by
what SEC's own endpoints return, never by a ticker-specific branch in
this project's code. No real network access -- every SEC response is a
canned fixture via monkeypatching `_sec_get_bytes`.
"""

from __future__ import annotations

import json

import pytest

from stock_agent.ingestion import cik_resolver


ACTIVE_TICKERS_JSON = json.dumps({
    "0": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
}).encode("utf-8")

DELISTED_TICKERS_JSON = json.dumps({
    "0": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}).encode("utf-8")  # CTXS deliberately absent, like the real active-registry file


def _atom_response(cik: str, conformed_name: str) -> bytes:
    return f"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <company-info>
    <cik>{cik}</cik>
    <conformed-name>{conformed_name}</conformed-name>
  </company-info>
</feed>""".encode("utf-8")


_NO_MATCH_ATOM = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Company Search Feed</title>
</feed>"""


def test_ticker_fast_path_used_when_found(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get_bytes(url: str) -> bytes:
        calls.append(url)
        return ACTIVE_TICKERS_JSON

    monkeypatch.setattr(cik_resolver, "_sec_get_bytes", fake_get_bytes)

    result = cik_resolver.resolve_company_record("MSFT")
    assert result["cik_str"] == 789019
    assert result["resolution_method"] == "TICKER_ACTIVE_REGISTRY"
    assert len(calls) == 1, "fast path succeeded -- the name-search fallback must never be called"


def test_falls_back_to_company_name_search_when_ticker_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact scenario a delisted ticker hits -- CTXS is absent from
    the active-registry file, so the SAME resolve_company_record() call
    automatically tries the name-search fallback, with no branch in this
    project's code keyed on "this ticker is delisted"."""
    calls: list[str] = []

    def fake_get_bytes(url: str) -> bytes:
        calls.append(url)
        if "company_tickers.json" in url:
            return DELISTED_TICKERS_JSON
        assert "browse-edgar" in url and "company=Citrix" in url
        return _atom_response("0000877890", "CITRIX SYSTEMS INC")

    monkeypatch.setattr(cik_resolver, "_sec_get_bytes", fake_get_bytes)

    result = cik_resolver.resolve_company_record("CTXS", company_name_hint="Citrix Systems Inc")
    assert result["cik_str"] == 877890
    assert result["resolution_method"] == "COMPANY_NAME_SEARCH_FULL_REGISTRY"
    assert len(calls) == 2, "fast path must be tried first even though it fails"


def test_no_hint_raises_without_attempting_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get_bytes(url: str) -> bytes:
        calls.append(url)
        return DELISTED_TICKERS_JSON

    monkeypatch.setattr(cik_resolver, "_sec_get_bytes", fake_get_bytes)

    with pytest.raises(cik_resolver.CikResolutionError):
        cik_resolver.resolve_company_record("CTXS")
    assert len(calls) == 1, "no company_name_hint means the fallback is never attempted -- fail closed, never guess"


def test_ambiguous_or_missing_name_match_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_bytes(url: str) -> bytes:
        if "company_tickers.json" in url:
            return DELISTED_TICKERS_JSON
        return _NO_MATCH_ATOM

    monkeypatch.setattr(cik_resolver, "_sec_get_bytes", fake_get_bytes)

    with pytest.raises(cik_resolver.CikResolutionError):
        cik_resolver.resolve_company_record("CTXS", company_name_hint="Some Nonexistent Company")


def test_name_mismatch_fails_closed_never_guesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single company-info result whose conformed name does NOT start
    with the supplied hint must not be accepted -- guards against a
    partial/fuzzy SEC match silently resolving to the wrong company."""
    def fake_get_bytes(url: str) -> bytes:
        if "company_tickers.json" in url:
            return DELISTED_TICKERS_JSON
        return _atom_response("0000999999", "SOME COMPLETELY DIFFERENT COMPANY INC")

    monkeypatch.setattr(cik_resolver, "_sec_get_bytes", fake_get_bytes)

    with pytest.raises(cik_resolver.CikResolutionError):
        cik_resolver.resolve_company_record("CTXS", company_name_hint="Citrix Systems Inc")
