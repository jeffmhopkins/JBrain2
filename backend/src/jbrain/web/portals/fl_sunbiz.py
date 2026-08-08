"""FL Sunbiz corporation-search resolver (kind=`business`).

The failing run fetched `search.sunbiz.org/Inquiry/CorporationSearch/ByName?search_term=…` — the
search FORM, which `web_fetch` can't submit. This adapter instead issues the request the form
actually makes, a GET to `…/CorporationSearch/SearchResults?inquiryType=EntityName&…`, which
returns a server-rendered results table; each row links to the STATIC
`…/CorporationSearch/SearchResultDetail?…` page. We parse the rows and hand back each entity with
that detail URL verbatim (read from the results HTML, never constructed), so the agent can fetch
and cite it. No JS execution needed — the results page is server-rendered.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from urllib.parse import urlencode, urljoin

import structlog
from cachetools import TTLCache

from jbrain.web.fetch import WebFetcher, WebFetchError
from jbrain.web.portals.base import PortalResult, PortalRow

log = structlog.get_logger()

_CACHE_TTL_S = 900.0  # 15 min — polite repeat-search cache, mirroring the public-records clients
_CACHE_MAX_ENTRIES = 256
_SEARCH_PATH = "/Inquiry/CorporationSearch/SearchResults"

_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_DETAIL_HREF_RE = re.compile(r"""href=["']([^"']*SearchResultDetail[^"']*)["']""", re.IGNORECASE)


def _text(fragment: str) -> str:
    """Strip tags + unescape entities + collapse whitespace to a single clean line."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _parse_rows(body: str, *, base: str, limit: int) -> tuple[PortalRow, ...]:
    """Parse the Sunbiz SearchResults table into rows. A results row has a first cell linking to
    SearchResultDetail (the entity name), then the document number and status cells. A row with no
    detail link (the header, layout rows) is skipped, so only real result rows survive."""
    rows: list[PortalRow] = []
    for row_html in _ROW_RE.findall(body):
        href_match = _DETAIL_HREF_RE.search(row_html)
        if not href_match:
            continue
        cells = [_text(c) for c in _CELL_RE.findall(row_html)]
        name = cells[0] if cells else ""
        if not name:
            continue
        doc_number = cells[1] if len(cells) > 1 else ""
        status = cells[2] if len(cells) > 2 else ""
        subtitle = " · ".join(p for p in (status, doc_number) if p)
        identifiers = tuple(("document_number", doc_number) for _ in (doc_number,) if doc_number)
        rows.append(
            PortalRow(
                title=name,
                subtitle=subtitle,
                detail_url=urljoin(base, html.unescape(href_match.group(1))),
                identifiers=identifiers,
            )
        )
        if len(rows) >= limit:
            break
    return tuple(rows)


class FlSunbizResolver:
    """Query the pinned FL Sunbiz corporation registry by entity name. `base_url` empty disables
    the resolver (the tool reports it "not configured"); the base URL is pinned from config and
    never model-supplied — only the public entity name goes out."""

    key = "fl_business"
    jurisdiction = "FL"
    kind = "business"
    label = "Florida Sunbiz corporation search"

    def __init__(
        self,
        base_url: str,
        *,
        cache_ttl_s: float = _CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._cache: TTLCache[tuple[str, int], tuple[PortalRow, ...]] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)
            if cache_ttl_s > 0
            else None
        )

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def search(self, fetcher: WebFetcher, name: str, *, limit: int) -> PortalResult:
        query = name.strip()
        if not self._base_url or not query:
            return PortalResult(rows=(), ok=bool(self._base_url))
        key = (query.lower(), limit)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return PortalResult(rows=cached, ok=True)
        params = {
            "inquiryType": "EntityName",
            "searchNameOrder": re.sub(r"[^A-Za-z0-9]", "", query).upper(),
            "searchTerm": query,
        }
        url = f"{self._base_url}{_SEARCH_PATH}?{urlencode(params)}"
        try:
            final_url, body = await fetcher.fetch_html(url)
        except WebFetchError as exc:
            log.warning("web.sunbiz_failed", error=repr(exc))
            return PortalResult(rows=(), ok=False, note="Sunbiz search is unavailable right now")
        rows = _parse_rows(body, base=final_url, limit=limit)
        if self._cache is not None and rows:
            self._cache[key] = rows
        return PortalResult(rows=rows, ok=True)
