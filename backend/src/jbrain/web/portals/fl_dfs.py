"""FL DFS licensee-search resolver (kind=`license`).

The FL Department of Financial Services licensee search (`licenseesearch.fldfs.com`) is a
session + CSRF-gated ASP.NET flow, not a plain GET: the page issues a POST to `/` (the form's
own action) carrying a session cookie and a page-embedded `csrf_token`, and answers with a
server-rendered results table whose rows carry `data-licensee-id` / `data-license-num` /
`data-licensee-name` and link to the STATIC `/Licensee/<id>` detail page. This resolver replays
that exact flow through `WebFetcher.submit_form` (GET the page to prime the session + read the
token, then POST the form on the same cookie jar) and parses the rows — no browser needed. It
closes the name→id→detail gap that made a candidate profile miss an individual license
(`W690060`) filed here rather than on the national NPPES registry.
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

_CACHE_TTL_S = 900.0
_CACHE_MAX_ENTRIES = 256
_MIN_NAME = 3  # the portal validates the individual last name at a 3-char minimum

# A result row carries everything on the <tr> as data-attributes (stabler than cell order).
_ROW_RE = re.compile(r"<tr\b([^>]*\bdata-licensee-id=[^>]*)>", re.IGNORECASE)


def _attr(attrs: str, name: str) -> str:
    m = re.search(rf'{name}="([^"]*)"', attrs, re.IGNORECASE)
    return html.unescape(m.group(1)) if m else ""


def _split_name(name: str) -> tuple[str, str]:
    """(first, last) for the portal's individual-name filters. A single token is the last name
    (the required, min-3 field); otherwise first token → first, last token → last."""
    parts = name.split()
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], parts[-1]


def _build_body(page_html: str, *, last: str, first: str) -> str:
    """Serialize the primed page's form (all hidden inputs incl. `csrf_token`), then set the
    individual-name filters + the required paging fields — the faithful, browser-free equivalent
    of the page's `$('#frmMainSearch').serialize()`."""
    fields: dict[str, str] = {}
    for m in re.finditer(r"<input\b[^>]*>", page_html, re.IGNORECASE):
        nm = re.search(r'name="([^"]*)"', m.group(0))
        if not nm:
            continue
        val = re.search(r'value="([^"]*)"', m.group(0))
        fields[nm.group(1)] = html.unescape(val.group(1)) if val else ""
    fields["IndividualLNameFilter"] = last
    fields["IndividualFNameFilter"] = first
    fields["LicenseeSearchInfo.PagingInfo.SortBy"] = "Name"
    fields["LicenseeSearchInfo.PagingInfo.SortDesc"] = "False"
    fields["LicenseeSearchInfo.PagingInfo.CurrentPage"] = "1"
    return urlencode(fields)


def _parse_rows(body: str, *, base: str, limit: int) -> tuple[PortalRow, ...]:
    rows: list[PortalRow] = []
    for m in _ROW_RE.finditer(body):
        attrs = m.group(1)
        lic_id = _attr(attrs, "data-licensee-id")
        name = _attr(attrs, "data-licensee-name")
        if not lic_id or not name:
            continue
        num = _attr(attrs, "data-license-num")
        rows.append(
            PortalRow(
                title=name,
                subtitle=f"License {num}" if num else "",
                detail_url=urljoin(base, f"/Licensee/{lic_id}"),
                identifiers=(("license_number", num),) if num else (),
            )
        )
        if len(rows) >= limit:
            break
    return tuple(rows)


class FlDfsResolver:
    """Query the pinned FL DFS licensee registry by person name via its session+CSRF POST flow.
    `base_url` empty disables it (the tool reports "not configured"); the base URL is pinned from
    config, never model-supplied — only the public name goes out."""

    key = "fl_license"
    jurisdiction = "FL"
    kind = "license"
    label = "Florida DFS licensee search"

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
        first, last = _split_name(query)
        if len(last) < _MIN_NAME:
            # The portal requires a 3-char minimum last name — a real constraint, not an outage.
            return PortalResult(
                rows=(), ok=True, note=f"DFS needs a last name of at least {_MIN_NAME} characters"
            )
        key = (query.lower(), limit)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return PortalResult(rows=cached, ok=True)
        root = f"{self._base_url}/"
        try:
            final_url, body = await fetcher.submit_form(
                root, root, lambda page: _build_body(page, last=last, first=first)
            )
        except WebFetchError as exc:
            log.warning("web.dfs_failed", error=repr(exc))
            return PortalResult(rows=(), ok=False, note="DFS licensee search is unavailable now")
        rows = _parse_rows(body, base=final_url, limit=limit)
        if self._cache is not None and rows:
            self._cache[key] = rows
        return PortalResult(rows=rows, ok=True)
