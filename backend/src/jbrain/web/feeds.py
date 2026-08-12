"""Curated RSS/Atom feeds as a jerv gather source (docs/plans/NEWS_FEED_PLAN.md).

The daily-news brief's discovery step — SearXNG's `news` category — fans out to
Google/Bing News, the exact upstreams a residential IP gets throttled on. Curated
per-category feeds sidestep that: they are pinned, low-volume, and — for the feeds that
carry `content:encoded` (NASA, Spaceflight Now, Space Coast Daily) — deliver the FULL
article body, so a full-text item is an already-opened article rather than a lead.

Egress goes through the shared SSRF-guarded WebFetcher (`fetch_feed`), and parsing is a
PURE OFFLINE function over the fetched bytes (`parse_feed` → feedparser): no network in
the parser, so tests run against a MockTransport with no network, like every other web
client. The base feed list is pinned from config and never model-supplied; only the
category name (validated against the config keys) is chosen by the caller.
"""

from __future__ import annotations

import calendar
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import structlog
from cachetools import TTLCache

from jbrain.web.fetch import WebFetcher, WebFetchError, _extract_html

log = structlog.get_logger()

# One-window repeat cache: the five-angle scout fan re-queries the same category feeds
# within a run, so a short TTL collapses those repeats to one fetch per feed per window —
# the same reasoning as SearxngClient's news cache. Fewer entries than search (a handful
# of categories, not open query text).
_CACHE_TTL_S = 3600.0
_CACHE_MAX_ENTRIES = 64
_DEFAULT_LIMIT = 8
_MAX_LIMIT = 20
# Below this many extracted chars a feed item's body is too thin to stand in for the
# article — treat it as a summary-only lead the reader must open, not a full read. Sized
# so a real `content:encoded` article clears it while a one-paragraph teaser does not.
_MIN_BODY_CHARS = 400
# Recency windows the tool accepts, mapped to seconds — the coarse "last day/week/…"
# filter the news brief uses. Anything else falls back to `day`.
_WINDOWS: dict[str, int] = {
    "day": 86_400,
    "week": 604_800,
    "month": 2_592_000,
    "year": 31_536_000,
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_MAX_SUMMARY_CHARS = 500  # a feed lead is a sentence or two; cap so a fat summary stays a lead


@dataclass(frozen=True)
class FeedItem:
    """One feed entry. `body` is the extracted full-article markdown when the feed carried
    `content:encoded` (else ""); `summary` is the short lead every feed gives. `epoch` is the
    parsed publish time (UTC seconds) for sorting/windowing, or None when the entry was
    undated. `published` keeps the human string as the feed wrote it for display."""

    title: str
    url: str
    published: str
    summary: str
    body: str
    source: str
    epoch: float | None

    @property
    def has_full_body(self) -> bool:
        """Whether this item already carries the article text — so the reader can cite it
        directly instead of web_fetching the URL again."""
        return bool(self.body)


def _clean_summary(raw: str) -> str:
    """A feed `summary`/`description` (often HTML) as a short plain-text lead — tags stripped,
    whitespace collapsed, capped. Not the article body (that's `_entry_body`); just enough to
    tell one lead from another before the reader opens it."""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()
    return text[:_MAX_SUMMARY_CHARS]


def _entry_body(contents: object, *, base: str) -> str:
    """The extracted full-article markdown from an entry's `content:encoded`, or "" when the
    feed carried no full content (summary-only) or the content is too thin to count as the
    article. Runs the HTML through the same `_extract_html` the page fetcher uses, so a
    full-text feed item reads identically to a fetched page downstream."""
    html = ""
    if isinstance(contents, list) and contents:
        first = contents[0]
        html = str(first.get("value") or "") if isinstance(first, Mapping) else ""
    if not html.strip():
        return ""
    _title, text, _hrefs = _extract_html(html, base=base)
    text = text.strip()
    return text if len(text) >= _MIN_BODY_CHARS else ""


def parse_feed(raw: bytes, *, source_hint: str = "") -> list[FeedItem]:
    """Parse RSS/Atom/JSON-feed bytes into FeedItems. PURE and OFFLINE — feedparser does no
    network on bytes, so this runs the same under a test transport. `source_hint` labels the
    outlet when the feed itself carried no title. Entries with no link are skipped (nothing to
    open or cite)."""
    import feedparser  # lazy like trafilatura — keeps module import cheap

    parsed = feedparser.parse(raw)
    feed_meta = parsed.feed
    title_meta = feed_meta.get("title") if isinstance(feed_meta, Mapping) else ""
    source = (str(title_meta or "") or source_hint).strip()
    items: list[FeedItem] = []
    for entry in parsed.entries:
        url = str(entry.get("link") or "").strip()
        if not url:
            continue
        title = str(entry.get("title") or "").strip() or url
        tstruct = entry.get("published_parsed") or entry.get("updated_parsed")
        # feedparser's *_parsed is a UTC time.struct_time; calendar.timegm reads it as UTC
        # (time.mktime would wrongly apply the local zone). Guard the type — a malformed date
        # leaves *_parsed unset, and the item is simply kept as undated.
        epoch = float(calendar.timegm(tstruct)) if isinstance(tstruct, time.struct_time) else None
        items.append(
            FeedItem(
                title=title,
                url=url,
                published=str(entry.get("published") or entry.get("updated") or "").strip(),
                summary=_clean_summary(str(entry.get("summary") or "")),
                body=_entry_body(entry.get("content"), base=url),
                source=source,
                epoch=epoch,
            )
        )
    return items


class FeedClient:
    """Pull curated per-category feeds through a WebFetcher. The feed URLs are pinned from
    config (`Settings.news_feeds`), keyed by a lowercase category; only the category name is
    ever chosen by the caller. `clock` (wall time) is injectable so the recency window filters
    deterministically in tests."""

    def __init__(
        self,
        fetcher: WebFetcher,
        feeds: Mapping[str, Sequence[str]],
        *,
        cache_ttl_s: float = _CACHE_TTL_S,
        clock: Callable[[], float] = time.time,
    ):
        self._fetcher = fetcher
        # Category keys lowercased once so lookup is case-insensitive; blank URLs dropped.
        self._feeds: dict[str, tuple[str, ...]] = {
            str(cat).strip().lower(): tuple(u.strip() for u in urls if u.strip())
            for cat, urls in feeds.items()
            if str(cat).strip()
        }
        self._feeds = {cat: urls for cat, urls in self._feeds.items() if urls}
        self._clock = clock
        self._cache: TTLCache[tuple[str, str, int], list[FeedItem]] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=time.monotonic)
            if cache_ttl_s > 0
            else None
        )

    @property
    def enabled(self) -> bool:
        """Whether any category has feeds configured — else the tool reports 'not configured'."""
        return bool(self._feeds)

    def categories(self) -> tuple[str, ...]:
        """The configured category names, sorted — surfaced to the model when it asks for an
        unknown one."""
        return tuple(sorted(self._feeds))

    def _resolve(self, category: str) -> tuple[str, tuple[str, ...]]:
        """Map a requested category to (canonical_key, urls): an exact match, else a substring
        match (so 'space industry' → 'space'), else ("", ()) — the model picks the category from
        an angle title, so it won't always be an exact key."""
        cat = category.strip().lower()
        if cat in self._feeds:
            return cat, self._feeds[cat]
        for key, urls in self._feeds.items():
            if key in cat or cat in key:
                return key, urls
        return "", ()

    async def fetch_category(
        self, category: str, *, since: str = "day", limit: int = _DEFAULT_LIMIT
    ) -> list[FeedItem]:
        """Pull every feed for `category`, dedupe by URL, keep items within the `since` window
        (undated items are kept), sort newest-first, and cap at `limit`. A feed that errors is
        skipped best-effort (a 403/timeout on one outlet never blanks the category). Returns []
        when the category is unknown or every feed failed/was empty."""
        window = _WINDOWS.get(since, _WINDOWS["day"])
        since_key = since if since in _WINDOWS else "day"
        limit = max(1, min(limit, _MAX_LIMIT))
        key, urls = self._resolve(category)
        if not urls:
            return []
        cache_key = (key, since_key, limit)
        if self._cache is not None and (cached := self._cache.get(cache_key)) is not None:
            return cached
        collected: list[FeedItem] = []
        seen: set[str] = set()
        for url in urls:
            raw = await self._safe_fetch(url)
            if raw is None:
                continue
            for item in parse_feed(raw, source_hint=url):
                if item.url in seen:
                    continue
                seen.add(item.url)
                collected.append(item)
        now = self._clock()
        fresh = [it for it in collected if it.epoch is None or (now - it.epoch) <= window]
        # Newest first; undated (epoch None) sink to the bottom rather than jumping the queue.
        fresh.sort(key=lambda it: it.epoch if it.epoch is not None else 0.0, reverse=True)
        result = fresh[:limit]
        if self._cache is not None and result:
            self._cache[cache_key] = result
        return result

    async def _safe_fetch(self, url: str) -> bytes | None:
        """Fetch one feed's raw bytes, or None on any fetch error — best-effort so one dead
        feed never sinks the category (unlike a page fetch, a feed miss is expected churn)."""
        try:
            return await self._fetcher.fetch_feed(url)
        except WebFetchError as exc:
            log.warning("web.feed_failed", url=url, error=repr(exc))
            return None
