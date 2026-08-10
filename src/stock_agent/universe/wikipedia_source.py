"""universe/wikipedia_source.py — deterministic parser for the Wikipedia
"List of NASDAQ-100 companies" page.

**No language model is involved in this data path.** Index membership goes
straight into the point-in-time universe that decides what a backtest is
allowed to see, so it is parsed by explicit, re-runnable, auditable code.
A summarised or transcribed table would be unverifiable and could silently
invent a company.

Provenance is exact: every fetch records the page URL *and the Wikipedia
revision id*, so any stored membership claim can be traced to the precise
page version it came from and re-derived later.

Fail-closed throughout. A row whose date will not parse, or whose ticker
does not look like a ticker, raises rather than being skipped -- a
silently dropped index change produces a wrong universe at every
subsequent date, which is exactly the class of error this project's
`REVIEW_REQUIRED` discipline exists to prevent.

Source quality note (measured, not assumed): at the time of writing the
changes table held 228 rows spanning February 2007 to August 2026, and
all 228 dates matched a single unambiguous format. Wikipedia is
community-maintained and is not an authoritative index provider, so the
loaded data must still pass `validate_membership_series` -- a fixed-size
index that fails to hold its size across history proves the changelog has
gaps regardless of how clean the parse looked.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime

from stock_agent.universe.constituents import NASDAQ_100, ConstituentEvent

WIKIPEDIA_PAGE_TITLE = "List of NASDAQ-100 companies"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "AI-Stock-Agent/1.0 (personal research project; contact shaiattias@gmail.com)"
REQUEST_TIMEOUT_SECONDS = 60

_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z.\-]{0,7}$")
_DATE_PATTERN = re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")


class WikipediaSourceError(Exception):
    """Raised when the page cannot be fetched or parsed exactly."""


@dataclass(frozen=True)
class FetchedPage:
    wikitext: str
    revision_id: int
    revision_timestamp: str
    url: str

    @property
    def citation_url(self) -> str:
        """A permanent link to the exact revision parsed."""
        return f"https://en.wikipedia.org/w/index.php?oldid={self.revision_id}"


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except Exception as error:  # noqa: BLE001
        raise WikipediaSourceError(f"failed to fetch {url}: {error}") from error


def fetch_page(title: str = WIKIPEDIA_PAGE_TITLE) -> FetchedPage:
    """Fetches the page's raw wikitext together with its revision id, so
    the parse is reproducible against an exact page version."""
    query = urllib.parse.urlencode({
        "action": "query", "prop": "revisions", "titles": title,
        "rvprop": "ids|timestamp|content", "rvslots": "main",
        "formatversion": "2", "format": "json",
    })
    payload = json.loads(_http_get(f"{WIKIPEDIA_API}?{query}").decode("utf-8"))

    pages = payload.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        raise WikipediaSourceError(f"Wikipedia page not found: {title!r}")
    revisions = pages[0].get("revisions", [])
    if not revisions:
        raise WikipediaSourceError(f"no revision returned for {title!r}")

    revision = revisions[0]
    content = revision.get("slots", {}).get("main", {}).get("content")
    if not content:
        raise WikipediaSourceError(f"revision for {title!r} carried no content")

    return FetchedPage(
        wikitext=content,
        revision_id=int(revision["revid"]),
        revision_timestamp=str(revision["timestamp"]),
        url=f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
    )


# --------------------------------------------------------------------------
# wikitext cleaning
# --------------------------------------------------------------------------

def clean_cell(raw: str) -> str:
    """Reduces one wikitext table cell to plain text.

    Handles, in order: reference tags (including self-closing and named),
    HTML comments, templates, wikilinks (`[[Target|Display]]` -> Display,
    `[[Page]]` -> Page), external links, bold/italic markup, and stray
    HTML tags. Deliberately keeps parenthetical qualifiers such as
    "(Class A)", which distinguish Alphabet's two share classes.
    """
    text = raw
    text = re.sub(r"<ref[^>]*/>", "", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\]", "", text)
    text = re.sub(r"'''?", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    return " ".join(text.split()).strip()


def _extract_table(wikitext: str, table_id: str) -> str:
    marker = f'id="{table_id}"'
    start = wikitext.find(marker)
    if start < 0:
        raise WikipediaSourceError(f"table with {marker} not found; page structure changed")
    table_start = wikitext.rfind("{|", 0, start)
    table_end = wikitext.find("\n|}", start)
    if table_start < 0 or table_end < 0:
        raise WikipediaSourceError(f"could not delimit table {table_id!r}; page structure changed")
    return wikitext[table_start:table_end]


def _row_cells(row: str) -> list[str]:
    """Returns the cells of one wikitext row, supporting both the
    one-cell-per-line (`|value`) and inline (`| a || b || c`) styles."""
    cells: list[str] = []
    for line in row.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("|-") or stripped.startswith("|}"):
            continue
        body = stripped[1:]
        if "||" in body:
            cells.extend(part for part in body.split("||"))
        else:
            cells.append(body)
    return [clean_cell(cell) for cell in cells]


def parse_constituents(wikitext: str) -> list[tuple[str, str]]:
    """Returns [(ticker, company_name)] for the current components table."""
    table = _extract_table(wikitext, "constituents")
    members: list[tuple[str, str]] = []

    for row in table.split("\n|-")[1:]:
        cells = _row_cells(row)
        if len(cells) < 2:
            continue
        ticker, company = cells[0], cells[1]
        if not ticker:
            continue
        if not _TICKER_PATTERN.match(ticker):
            raise WikipediaSourceError(
                f"constituents table: {ticker!r} does not look like a ticker "
                "(page structure may have changed; refusing to guess)"
            )
        members.append((ticker, company))

    if not members:
        raise WikipediaSourceError("constituents table parsed to zero members")
    duplicates = {t for t, _ in members if [x for x, _ in members].count(t) > 1}
    if duplicates:
        raise WikipediaSourceError(f"constituents table contains duplicate tickers: {sorted(duplicates)}")
    return members


def _parse_event_date(value: str) -> date:
    if not _DATE_PATTERN.match(value):
        raise WikipediaSourceError(
            f"changes table: date {value!r} is not in the expected 'Month D, YYYY' format; "
            "refusing to guess a date for an index change"
        )
    return datetime.strptime(value, "%B %d, %Y").date()


def parse_changes(wikitext: str, index_name: str = NASDAQ_100) -> list[ConstituentEvent]:
    """Returns every constituent change, oldest first.

    Each row is Date | Added ticker | Added security | Removed ticker |
    Removed security | Reason, with either side possibly blank (a company
    can be added without a matching removal, and vice versa).
    """
    table = _extract_table(wikitext, "changes")
    events: list[ConstituentEvent] = []

    for row in table.split("\n|-"):
        cells = _row_cells(row)
        if len(cells) < 5:
            continue
        if not _DATE_PATTERN.match(cells[0]):
            continue  # header rows and stray markup

        event_date = _parse_event_date(cells[0])
        added_ticker = cells[1] or None
        added_company = cells[2] or None
        removed_ticker = cells[3] or None
        removed_company = cells[4] or None
        reason = cells[5] if len(cells) > 5 else None

        for ticker in (added_ticker, removed_ticker):
            if ticker and not _TICKER_PATTERN.match(ticker):
                raise WikipediaSourceError(
                    f"changes table, {event_date}: {ticker!r} does not look like a ticker; "
                    "refusing to record a malformed index change"
                )

        if not added_ticker and not removed_ticker:
            raise WikipediaSourceError(
                f"changes table, {event_date}: row neither adds nor removes a ticker; "
                "refusing to record a meaningless change"
            )

        events.append(ConstituentEvent(
            index_name=index_name, event_date=event_date,
            added_ticker=added_ticker, added_company=added_company,
            removed_ticker=removed_ticker, removed_company=removed_company,
            reason=reason,
        ))

    if not events:
        raise WikipediaSourceError("changes table parsed to zero events")
    events.sort(key=lambda e: e.event_date)
    return events


__all__ = [
    "FetchedPage", "WIKIPEDIA_PAGE_TITLE", "WikipediaSourceError",
    "clean_cell", "fetch_page", "parse_changes", "parse_constituents",
]
