"""Grokipedia access for the jerv agent (docs/archive/GROKIPEDIA_TOOL_PLAN.md).

Grokipedia is xAI's AI-generated encyclopedia. This client reaches it over the
**open internet only** — no xAI/Grok API key — via the site's own first-party
surface, and turns a page into a small, agent-friendly model: a section outline
(table of contents), the markdown of any single section (so the agent drills in
instead of loading a 6-figure-character article), and the article's citations as
followable primary-source URLs.

**API-first, SSR-HTML fallback.** Grokipedia's own frontend calls unauthenticated
same-origin JSON endpoints — `/api/typeahead` (search) and `/api/page-preview`
(a page's markdown + structured `citations[]` + metadata). Those are the primary
path: real full-corpus search and pre-parsed citations, the cleanest route to the
tool's goal. When an `/api/` call fails or comes back unparseable (these are
reverse-engineered endpoints and can drift), `fetch_article` falls back to parsing
the server-side-rendered `/page/<slug>` HTML, which carries the same article, TOC,
and a `<li id="ref-N">` reference list. A definitive 404/410 is NOT laundered
through the fallback — the page does not exist, so the status rides back on
`GrokipediaError.status` and the caller corrects the slug. Both surfaces produce
the same `GrokArticle`, so the tool layer never sees which one answered.

The base host is pinned (never model-supplied); only the query text and slug are,
and the slug is path-encoded — so there is no SSRF surface here (following a
citation's URL off to its primary source is `web_fetch`'s job, guarded there).
Cloudflare fronts the site and 403s a bare library User-Agent, so requests present
as an ordinary browser (`BROWSER_HEADERS`) and reuse one cookie jar across calls
so the `__cf_bm` bot cookie persists.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

import httpx
import structlog
from cachetools import TTLCache

from jbrain.htmltext import extract_page
from jbrain.web.fetch import BROWSER_HEADERS

log = structlog.get_logger()

DEFAULT_BASE_URL = "https://grokipedia.com"
_TIMEOUT = 20.0
_DEFAULT_LIMIT = 6
_MAX_LIMIT = 10
_MAX_RELATED = 20  # cap the traversal links surfaced from a page's inline wiki-links
# Cache the parsed article per slug: the agent's outline→section→citations→related loop
# hits the same page repeatedly, and a Grokipedia page changes at most a few times a year,
# so one fetch serves the whole drill-down. In-process + per-slug, like the SearXNG
# repeat-search cache; adequate at personal scale (one API process).
_CACHE_TTL_S = 900.0  # 15 min: long enough to fold a turn's drill-down, short enough to stay fresh
_CACHE_MAX_ENTRIES = 128
# A definitive "no such page" — not retried through the SSR fallback (which would only
# render the origin's soft "page not found" stub). The real status rides back to the caller.
_GONE_STATUSES = frozenset({404, 410})

# A markdown heading line: 1-6 '#', a space, the title. Grokipedia's page-preview `content`
# is markdown and `extract_page` emits markdown headings, so both surfaces share this.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t#]*$", re.MULTILINE)
# An inline wiki-link to another Grokipedia page — `[Title](/page/Slug)` in raw preview
# markdown, or the same resolved to an absolute grokipedia.com URL after `extract_page`.
# Either form yields the linked slug for corpus traversal.
_WIKILINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://[^/)]*grokipedia\.com)?/page/([^)\s#]+)")
# The SSR reference list: `<li id="ref-N">` … first external `href`. Used only on the
# HTML fallback path; the JSON path reads structured `citations[]` directly.
_REF_ID_RE = re.compile(r'id="ref-(\d+)"', re.IGNORECASE)
_HREF_RE = re.compile(r'href="(https?://[^"]+)"', re.IGNORECASE)
_MAX_REF_SPAN = 2000  # how far past a `ref-N` anchor to look for its link, for the last item


class GrokipediaError(RuntimeError):
    """A Grokipedia request could not be completed — unreachable, a non-2xx response,
    or an unusable body. Surfaced to the agent as a recoverable tool error. `status`
    carries the upstream HTTP status on an HTTP error (a definitive 404/410 means the
    page is gone), else None."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GrokSearchHit:
    """One search result: enough to pick a page and fetch it (by `slug`)."""

    slug: str
    title: str
    snippet: str
    view_count: int | None = None


@dataclass(frozen=True)
class GrokSection:
    """One table-of-contents entry. `number` is the hierarchical index ("1", "1.1"),
    `anchor` the slugified heading id ("early-life") — both stable drill-in keys the
    agent passes back to fetch just this section."""

    number: str
    level: int
    title: str
    anchor: str


@dataclass(frozen=True)
class GrokCitation:
    """One reference: a primary-source URL the agent can follow with web_fetch.
    `publisher` is derived from the URL's domain (Grokipedia often ships citation
    titles blank), so a citation is always attributable."""

    ref: int
    url: str
    title: str = ""
    publisher: str = ""


@dataclass(frozen=True)
class GrokRelated:
    """A linked page for corpus traversal."""

    slug: str
    title: str


@dataclass(frozen=True)
class GrokArticle:
    """A parsed Grokipedia page, surface-agnostic (built the same from JSON or SSR
    HTML). `content` is the full markdown; `outline`/`section` let the agent navigate
    it without loading the whole thing; `citations` are the followable sources."""

    slug: str
    title: str
    content: str
    outline: tuple[GrokSection, ...] = ()
    citations: tuple[GrokCitation, ...] = ()
    related: tuple[GrokRelated, ...] = ()
    last_modified: datetime | None = None
    categories: tuple[str, ...] = ()

    def section(self, ref: str) -> tuple[GrokSection, str] | None:
        """Find a section by its `number`, `anchor`, or `title` (case-insensitive) and
        return (the entry, its markdown incl. the heading line, spanning to the next
        heading of the same or higher level). None when no section matches — so the
        tool can steer the agent back to `outline`."""
        wanted = ref.strip().lower()
        if not wanted:
            return None
        headings = _headings(self.content)
        for i, entry in enumerate(self.outline):
            if wanted not in (entry.number.lower(), entry.anchor.lower(), entry.title.lower()):
                continue
            level, _title, start, _end = headings[i]
            stop = len(self.content)
            for deeper_level, _t, next_start, _e in headings[i + 1 :]:
                if deeper_level <= level:
                    stop = next_start
                    break
            return entry, self.content[start:stop].strip()
        return None


# --- parsing (pure, no network — the same functions cover both surfaces) --------


def _slugify(title: str) -> str:
    """A heading's anchor id, matching Grokipedia's SSR heading ids ("Early Life" →
    "early-life")."""
    text = re.sub(r"[^\w\s-]", "", title.strip().lower())
    return re.sub(r"[\s_]+", "-", text).strip("-")


def _publisher(url: str) -> str:
    """The bare domain of a citation URL ("www.britannica.com" → "britannica.com") —
    a reliable attribution when the source's own title is missing."""
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _headings(content: str) -> list[tuple[int, str, int, int]]:
    """(level, title, heading_start, heading_end) for each markdown heading, in order."""
    return [
        (len(m.group(1)), m.group(2).strip(), m.start(), m.end())
        for m in _HEADING_RE.finditer(content)
    ]


def _section_numbers(levels: list[int]) -> list[str]:
    """Hierarchical section numbers ("1", "1.1", "2") from heading levels, resetting
    deeper counters when a shallower heading opens — the TOCData numbering shape."""
    numbers: list[str] = []
    counters: dict[int, int] = {}
    for level in levels:
        counters[level] = counters.get(level, 0) + 1
        for deeper in [lvl for lvl in counters if lvl > level]:
            del counters[deeper]
        numbers.append(".".join(str(counters[lvl]) for lvl in sorted(counters)))
    return numbers


def _build_outline(content: str) -> tuple[GrokSection, ...]:
    headings = _headings(content)
    numbers = _section_numbers([level for level, _t, _s, _e in headings])
    return tuple(
        GrokSection(number=number, level=level, title=title, anchor=_slugify(title))
        for (level, title, _s, _e), number in zip(headings, numbers, strict=True)
    )


def _related(content: str, *, self_slug: str) -> tuple[GrokRelated, ...]:
    out: list[GrokRelated] = []
    seen: set[str] = set()
    for match in _WIKILINK_RE.finditer(content):
        slug = match.group(2).strip()
        if not slug or slug == self_slug or slug in seen:
            continue
        seen.add(slug)
        out.append(GrokRelated(slug=slug, title=match.group(1).strip()))
        if len(out) >= _MAX_RELATED:
            break
    return tuple(out)


def _epoch_to_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, int | float) or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _str_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _citation_ref(raw: object, fallback: int) -> int:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return fallback


def _citations_from_json(raw: object) -> tuple[GrokCitation, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[GrokCitation] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        out.append(
            GrokCitation(
                ref=_citation_ref(item.get("id"), index),
                url=url,
                title=str(item.get("title") or "").strip(),
                publisher=_publisher(url),
            )
        )
    return tuple(out)


def _citations_from_html(html: str) -> tuple[GrokCitation, ...]:
    """Citations from the SSR reference list: each `<li id="ref-N">` and the first
    external link inside it. Used only on the HTML fallback path."""
    anchors = list(_REF_ID_RE.finditer(html))
    out: list[GrokCitation] = []
    seen: set[int] = set()
    for i, anchor in enumerate(anchors):
        ref = int(anchor.group(1))
        if ref in seen:
            continue
        end = (
            anchors[i + 1].start()
            if i + 1 < len(anchors)
            else min(len(html), anchor.end() + _MAX_REF_SPAN)
        )
        link = _HREF_RE.search(html, anchor.end(), end)
        if link is None:
            continue
        seen.add(ref)
        out.append(GrokCitation(ref=ref, url=link.group(1), publisher=_publisher(link.group(1))))
    return tuple(out)


def _clean_title(title: str, slug: str) -> str:
    text = re.sub(r"\s+", " ", title).strip()
    for suffix in (" - Grokipedia", " | Grokipedia", " — Grokipedia"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text or slug.replace("_", " ")


def parse_typeahead(body: object, limit: int) -> list[GrokSearchHit]:
    rows = body.get("results") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return []
    hits: list[GrokSearchHit] = []
    for row in rows[: max(limit, 0)]:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        view_count = row.get("viewCount")
        hits.append(
            GrokSearchHit(
                slug=slug,
                title=str(row.get("title") or "").strip() or slug.replace("_", " "),
                snippet=str(row.get("snippet") or "").strip(),
                view_count=view_count if isinstance(view_count, int) else None,
            )
        )
    return hits


def article_from_preview(body: object, slug: str) -> GrokArticle | None:
    """Build a GrokArticle from a `/api/page-preview` JSON body. None when the body
    has no usable content (so `fetch_article` falls back to the SSR page)."""
    if not isinstance(body, dict):
        return None
    raw_page = body.get("page")
    # Some responses carry the page fields at the top level rather than under "page".
    page: dict[str, object] = raw_page if isinstance(raw_page, dict) else body
    content = str(page.get("content") or "")
    if not content.strip():
        return None
    raw_meta = page.get("metadata")
    metadata: dict[str, object] = raw_meta if isinstance(raw_meta, dict) else {}
    title = str(page.get("title") or metadata.get("title") or "").strip() or slug.replace("_", " ")
    return GrokArticle(
        slug=slug,
        title=title,
        content=content,
        outline=_build_outline(content),
        citations=_citations_from_json(page.get("citations")),
        related=_related(content, self_slug=slug),
        last_modified=_epoch_to_datetime(metadata.get("lastModified")),
        categories=_str_tuple(metadata.get("categories")),
    )


def article_from_html(html: str, slug: str) -> GrokArticle:
    """Build a GrokArticle from the SSR `/page/<slug>` HTML — the fallback path.
    Content/outline come from `extract_page` (the dependency-free HTML→markdown pass);
    citations come from the raw HTML's `<li id="ref-N">` reference list."""
    raw_title, content, _hrefs = extract_page(html, base=f"{DEFAULT_BASE_URL}/page/{slug}")
    return GrokArticle(
        slug=slug,
        title=_clean_title(raw_title, slug),
        content=content,
        outline=_build_outline(content),
        citations=_citations_from_html(html),
        related=_related(content, self_slug=slug),
    )


# --- the client (the one thing that touches the network) -----------------------


class GrokipediaClient:
    """Query Grokipedia over the open internet. `transport` is injectable so tests run
    with no network (DEVELOPMENT.md "no network in tests")."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        cache_ttl_s: float = _CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        # One cookie jar reused across calls so Cloudflare's `__cf_bm` bot cookie set on
        # the first response rides on the next request instead of re-triggering a challenge.
        self._cookies = httpx.Cookies()
        # Per-slug parsed-article cache (None when disabled with cache_ttl_s <= 0). `timer`
        # threads an injectable clock for deterministic expiry tests.
        self._article_cache: TTLCache[str, GrokArticle] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)
            if cache_ttl_s > 0
            else None
        )

    async def search(self, query: str, limit: int = _DEFAULT_LIMIT) -> list[GrokSearchHit]:
        cleaned = query.strip()
        if not cleaned:
            return []
        limit = max(1, min(limit, _MAX_LIMIT))
        body = await self._get_json("/api/typeahead", {"query": cleaned, "limit": limit})
        return parse_typeahead(body, limit)

    async def fetch_article(self, slug: str) -> GrokArticle:
        """Fetch and parse a page by slug: the `/api/page-preview` JSON first, the SSR
        `/page/<slug>` HTML as the fallback. Raises `GrokipediaError` if the page is
        gone (404/410) or neither surface yields readable content."""
        slug = slug.strip()
        if not slug:
            raise GrokipediaError("a Grokipedia page slug is required")
        if self._article_cache is not None and (hit := self._article_cache.get(slug)) is not None:
            return hit
        try:
            body = await self._get_json("/api/page-preview", {"slug": slug})
            article = article_from_preview(body, slug)
            if article is not None:
                return self._cache(slug, article)
            log.warning("grokipedia.preview_unparseable", slug=slug)
        except GrokipediaError as exc:
            if exc.status in _GONE_STATUSES:
                raise  # the page genuinely does not exist — don't launder it through the fallback
            log.warning("grokipedia.preview_failed", slug=slug, status=exc.status)
        html = await self._get_text(f"/page/{quote(slug, safe='')}")
        article = article_from_html(html, slug)
        if not article.content.strip():
            raise GrokipediaError(f"could not read the Grokipedia page for '{slug}'")
        return self._cache(slug, article)

    def _cache(self, slug: str, article: GrokArticle) -> GrokArticle:
        if self._article_cache is not None:
            self._article_cache[slug] = article
        return article

    async def _request(
        self, path: str, params: dict[str, str | int] | None = None
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                transport=self._transport,
                cookies=self._cookies,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url, params=params, headers=BROWSER_HEADERS)
                # httpx copies the jar into the client, so merge any Set-Cookie back into
                # ours — that is what carries Cloudflare's `__cf_bm` cookie to the next call.
                self._cookies.update(client.cookies)
                resp.raise_for_status()
                return resp
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            log.warning("grokipedia.request_failed", path=path, status=status, error=repr(exc))
            raise GrokipediaError(_error_message(status), status=status) from exc
        except httpx.HTTPError as exc:
            log.warning("grokipedia.request_failed", path=path, error=repr(exc))
            raise GrokipediaError("Grokipedia is unavailable right now") from exc

    async def _get_json(self, path: str, params: dict[str, str | int]) -> object:
        resp = await self._request(path, params)
        try:
            return resp.json()
        except ValueError as exc:
            log.warning("grokipedia.non_json", path=path, error=repr(exc))
            raise GrokipediaError("Grokipedia returned an unexpected response") from exc

    async def _get_text(self, path: str) -> str:
        resp = await self._request(path)
        return resp.text


def _error_message(status: int) -> str:
    if status in _GONE_STATUSES:
        return f"that Grokipedia page does not exist (HTTP {status})"
    return f"Grokipedia could not be reached right now (HTTP {status})"
