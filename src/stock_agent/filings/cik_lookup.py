"""filings/cik_lookup.py — resolve a ticker to a CIK, including for
companies that no longer trade.

Why this module has to exist
----------------------------
SEC's `company_tickers.json` holds only ~10,400 *currently listed*
registrants. Every company that was acquired or delisted silently
disappears from it, even though all of its historical filings remain on
EDGAR forever. Measured on the pilot cohort: ATVI (Activision), ALXN
(Alexion) and ANSS (Ansys) all failed lookup, and so did AEP (American
Electric Power) despite still trading.

That makes the ticker file a survivor-only index. Relying on it alone
would reproduce, at the infrastructure level, exactly the survivorship
bias the point-in-time universe exists to remove: the delisted companies
whose filings we most need are the ones we could not fetch.

The fallback is SEC's complete CIK lookup file (~1.05 million entries,
including defunct filers), matched by company name.

Why name matching must verify rather than guess
-----------------------------------------------
Company names are not unique. "ANSYS" matches both `ANSYS INC`
(CIK 1013462) and `ANSYS DIAGNOSTICS INC` (CIK 1068965) -- unrelated
companies. Silently choosing one would attach the wrong company's
financials to a ticker, which is worse than failing: it produces a
plausible number that is simply false.

So every name-derived candidate is CONFIRMED against SEC's own
submissions record before it is accepted, and ambiguity fails closed with
`REVIEW_REQUIRED` (D-008), listing the candidates rather than picking one.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date

from stock_agent import DATA_DIR

SEC_BASE_URL = "https://www.sec.gov"
SEC_DATA_URL = "https://data.sec.gov"
CIK_LOOKUP_URL = f"{SEC_BASE_URL}/Archives/edgar/cik-lookup-data.txt"

USER_AGENT = "Shai Attias shaiattias@gmail.com"
REQUEST_DELAY_SECONDS = 0.1
REQUEST_TIMEOUT_SECONDS = 300

CIK_LOOKUP_CACHE_PATH = DATA_DIR / "sec_cik_lookup_data.txt"

RESOLVED_BY_TICKER_FILE = "TICKER_FILE"
RESOLVED_BY_NAME_CONFIRMED = "NAME_MATCH_CONFIRMED_BY_SUBMISSIONS"
REVIEW_REQUIRED = "REVIEW_REQUIRED"

# Corporate-form noise that carries no identifying information and only
# gets in the way of matching a plain index name to a registrant name.
_SUFFIX_PATTERN = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|plc|ltd|limited|"
    r"holdings?|group|the|sa|nv|ag|lp|llc|class\s+[abc])\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


class CikLookupError(Exception):
    pass


@dataclass
class CikResolution:
    ticker: str
    status: str
    cik: int | None = None
    company_name: str | None = None
    method: str | None = None
    evidence: dict = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "PASS" and self.cik is not None


def _http_get(url: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Connection": "close",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
    except Exception as error:  # noqa: BLE001
        raise CikLookupError(f"SEC request failed for {url}: {error}") from error
    time.sleep(REQUEST_DELAY_SECONDS)
    return content


def normalise_company_name(name: str) -> str:
    """Reduces a company name to a comparable core: lowercase, corporate
    suffixes and punctuation removed, whitespace collapsed."""
    text = name.lower()
    text = text.replace("&", " and ")
    text = _NON_ALNUM.sub(" ", text)
    text = _SUFFIX_PATTERN.sub(" ", text)
    return " ".join(text.split())


# --------------------------------------------------------------------------
# ticker file (fast path)
# --------------------------------------------------------------------------

_ticker_cache: dict[str, dict] | None = None


def _load_ticker_file() -> dict[str, dict]:
    global _ticker_cache
    if _ticker_cache is None:
        payload = json.loads(_http_get(f"{SEC_BASE_URL}/files/company_tickers.json").decode("utf-8"))
        _ticker_cache = {}
        for record in payload.values():
            _ticker_cache.setdefault(str(record.get("ticker", "")).upper(), record)
    return _ticker_cache


# --------------------------------------------------------------------------
# full CIK lookup file (fallback path)
# --------------------------------------------------------------------------

_name_index_cache: list[tuple[str, str, int]] | None = None


def _load_cik_lookup_file(refresh: bool = False) -> list[tuple[str, str, int]]:
    """Returns [(raw_name, normalised_name, cik)] for every SEC filer ever.

    Cached on disk: the file is ~40 MB and changes rarely, so re-fetching
    it per lookup would be both slow and impolite to SEC.
    """
    global _name_index_cache
    if _name_index_cache is not None and not refresh:
        return _name_index_cache

    if refresh or not CIK_LOOKUP_CACHE_PATH.exists():
        CIK_LOOKUP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CIK_LOOKUP_CACHE_PATH.write_bytes(_http_get(CIK_LOOKUP_URL))

    entries: list[tuple[str, str, int]] = []
    for line in CIK_LOOKUP_CACHE_PATH.read_text(encoding="latin-1").splitlines():
        parts = line.rstrip(":").rsplit(":", 1)
        if len(parts) != 2:
            continue
        raw_name, cik_text = parts[0].strip(), parts[1].strip()
        if not raw_name or not cik_text.isdigit():
            continue
        entries.append((raw_name, normalise_company_name(raw_name), int(cik_text)))

    if not entries:
        raise CikLookupError(f"CIK lookup file parsed to zero entries: {CIK_LOOKUP_CACHE_PATH}")
    _name_index_cache = entries
    return entries


def find_cik_candidates_by_name(company_name: str) -> list[dict]:
    """Candidate CIKs whose normalised name equals, or begins with, the
    normalised search name. Exact matches are preferred and returned
    alone when present -- a prefix match is only consulted if nothing
    matches exactly, which stops `ANSYS` from dragging in
    `ANSYS DIAGNOSTICS`."""
    target = normalise_company_name(company_name)
    if not target:
        return []

    exact: dict[int, dict] = {}
    prefix: dict[int, dict] = {}
    for raw_name, normalised, cik in _load_cik_lookup_file():
        if normalised == target:
            exact.setdefault(cik, {"cik": cik, "name": raw_name, "match": "EXACT"})
        elif normalised.startswith(target + " "):
            prefix.setdefault(cik, {"cik": cik, "name": raw_name, "match": "PREFIX"})

    return sorted(exact.values(), key=lambda c: c["cik"]) or sorted(prefix.values(), key=lambda c: c["cik"])


# --------------------------------------------------------------------------
# confirmation against SEC's own submissions record
# --------------------------------------------------------------------------

def confirm_cik(cik: int, ticker: str, window_start: date, window_end: date) -> dict:
    """Checks a candidate CIK against SEC's submissions record.

    Two independent signals, either of which confirms:
      * SEC lists the ticker among the company's own registered tickers
      * the company filed at least one annual report in the window

    The second matters for acquired companies, whose ticker list is often
    emptied once they stop trading -- their filings are still there.
    """
    payload = json.loads(
        _http_get(f"{SEC_DATA_URL}/submissions/CIK{str(cik).zfill(10)}.json", timeout=60).decode("utf-8")
    )
    sec_tickers = [str(t).upper() for t in payload.get("tickers", []) or []]
    recent = payload.get("filings", {}).get("recent", {}) or {}

    forms = recent.get("form", []) or []
    report_dates = recent.get("reportDate", []) or []
    annual_in_window = [
        report_dates[i] for i, form in enumerate(forms)
        if form in ("10-K", "20-F", "40-F")
        and i < len(report_dates) and report_dates[i]
        and window_start.isoformat() <= report_dates[i] <= window_end.isoformat()
    ]

    return {
        "cik": cik,
        "sec_company_name": payload.get("name"),
        "sec_tickers": sec_tickers,
        "ticker_matches": ticker.upper() in sec_tickers,
        "annual_filings_in_window": len(annual_in_window),
        "former_names": [n.get("name") for n in (payload.get("formerNames") or [])],
        "confirmed": (ticker.upper() in sec_tickers) or bool(annual_in_window),
    }


def resolve_cik(
    ticker: str,
    company_name: str | None = None,
    window_start: date = date(2020, 1, 1),
    window_end: date = date(2026, 12, 31),
) -> CikResolution:
    """Resolves `ticker` to a CIK, falling back to a confirmed name match
    for companies absent from SEC's current-listings ticker file.

    Never guesses. Multiple confirmed candidates, or none, return
    `REVIEW_REQUIRED` with the candidate list attached.
    """
    ticker = ticker.upper()

    record = _load_ticker_file().get(ticker)
    if record is not None:
        return CikResolution(
            ticker=ticker, status="PASS", cik=int(record["cik_str"]),
            company_name=str(record.get("title", "")), method=RESOLVED_BY_TICKER_FILE,
            evidence={"source": "company_tickers.json"},
        )

    if not company_name:
        return CikResolution(
            ticker=ticker, status=REVIEW_REQUIRED, method=None,
            reason=("absent from SEC's current-listings ticker file and no company name "
                    "was supplied to search the full CIK index with"),
        )

    candidates = find_cik_candidates_by_name(company_name)
    if not candidates:
        return CikResolution(
            ticker=ticker, status=REVIEW_REQUIRED, company_name=company_name,
            reason=f"no CIK found for company name {company_name!r} in SEC's full lookup file",
        )

    confirmed: list[dict] = []
    for candidate in candidates[:10]:
        try:
            evidence = confirm_cik(candidate["cik"], ticker, window_start, window_end)
        except CikLookupError as error:
            candidate["confirmation_error"] = str(error)
            continue
        candidate.update(evidence)
        if evidence["confirmed"]:
            confirmed.append(candidate)

    if len(confirmed) == 1:
        winner = confirmed[0]
        return CikResolution(
            ticker=ticker, status="PASS", cik=winner["cik"],
            company_name=winner.get("sec_company_name") or winner["name"],
            method=RESOLVED_BY_NAME_CONFIRMED,
            evidence={k: winner[k] for k in
                      ("name", "match", "sec_tickers", "ticker_matches", "annual_filings_in_window")
                      if k in winner},
            candidates=candidates,
        )

    return CikResolution(
        ticker=ticker, status=REVIEW_REQUIRED, company_name=company_name,
        candidates=candidates,
        reason=(f"{len(confirmed)} candidate CIKs confirmed for {company_name!r} "
                f"({[c['cik'] for c in confirmed]}); refusing to guess which company a "
                "ticker belongs to, because attaching the wrong company's financials "
                "produces a plausible but false number"),
    )


__all__ = [
    "CikLookupError", "CikResolution", "REVIEW_REQUIRED",
    "RESOLVED_BY_NAME_CONFIRMED", "RESOLVED_BY_TICKER_FILE",
    "confirm_cik", "find_cik_candidates_by_name", "normalise_company_name", "resolve_cik",
]
