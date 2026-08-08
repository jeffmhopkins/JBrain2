"""Federal Register search backing jerv's `federal_register` tool
(docs/reference/ASSISTANT.md "Agent selection").

The Federal Register API is the FREE, no-key source for federal rules and notices — including
FDA/agency DEBARMENTS and enforcement actions. For a person profile it catches a debarment or
enforcement notice naming a person or company (search a name + "debarment") that web-only search
misses. A hit is a LEAD to verify against the notice itself, not a verdict.

FREE and no-key, like CourtListener: the base URL is pinned from config and never
model-supplied; only the public query text goes out, with the descriptive `User-Agent` the API
requests. The client returns `(notices, ok)` so an outage degrades (ok=False) rather than
raising; it never runs a tool policy itself — the handler does.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import structlog
from cachetools import TTLCache

log = structlog.get_logger()

_TIMEOUT = 15.0
_DEFAULT_LIMIT = 10
_UA = "JBrain2 research (jeff.m.hopkins@gmail.com)"

# Repeat-search cache, mirroring CourtListenerClient (see there for the rationale).
_CACHE_TTL_S = 900.0  # 15 min
_CACHE_MAX_ENTRIES = 256

# The API returns `excerpts` as an HTML snippet with <span class="match"> highlight tags around
# the query hit; strip the tags for a clean one-line excerpt (the highlight is noise in prose).
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Notice:
    """One Federal Register document: enough to summarize and to verify against the notice."""

    title: str
    agencies: tuple[str, ...]
    publication_date: str
    url: str
    excerpt: str  # the matched passage, HTML stripped (may be empty for a term with no highlight)


class FederalRegisterClient:
    """Query the pinned Federal Register API by term. `transport` is injectable so tests run
    against a mock with no network. An empty `base_url` disables the client (the handler reports
    "not configured")."""

    def __init__(
        self,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        cache_ttl_s: float = _CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._cache: TTLCache[tuple[str, int], list[Notice]] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)
            if cache_ttl_s > 0
            else None
        )

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def search(self, query: str, *, limit: int = _DEFAULT_LIMIT) -> tuple[list[Notice], bool]:
        """Search documents for `query`, newest first. Returns (notices, ok); `ok` is False only
        on an outage so the tool tells a real "no notice" (ok=True, empty) apart from a source
        failure. Errors are logged and swallowed."""
        term = query.strip()
        if not self._base_url or not term:
            return [], bool(self._base_url)
        key = (term, limit)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return cached, True
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.get(
                    f"{self._base_url}/api/v1/documents.json",
                    params={"conditions[term]": term, "per_page": limit, "order": "newest"},
                    headers={"Accept": "application/json", "User-Agent": _UA},
                )
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web.federal_register_failed", error=repr(exc))
            return [], False
        rows = body.get("results") if isinstance(body, dict) else None
        notices = [
            n
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and (n := _parse_notice(row)) is not None
        ][:limit]
        if self._cache is not None and notices:
            self._cache[key] = notices
        return notices, True


def _clean_excerpt(raw: object) -> str:
    """Strip the API's <span class="match"> highlight HTML and collapse whitespace."""
    text = _TAG_RE.sub("", str(raw or ""))
    return _WS_RE.sub(" ", text).strip()


def _parse_notice(row: dict) -> Notice | None:
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    agencies = tuple(
        str(a.get("name") or "").strip()
        for a in (row.get("agencies") or [])
        if isinstance(a, dict) and a.get("name")
    )
    excerpt = _clean_excerpt(row.get("excerpts")) or _clean_excerpt(row.get("abstract"))
    return Notice(
        title=title,
        agencies=agencies,
        publication_date=str(row.get("publication_date") or "").strip(),
        url=str(row.get("html_url") or "").strip(),
        excerpt=excerpt,
    )
