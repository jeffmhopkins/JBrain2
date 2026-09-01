"""Web search via a self-hosted SearXNG instance (docs/reference/ASSISTANT.md "Agent
selection").

SearXNG is a metasearch engine the owner runs on their own box, so a jerv search
leaves the box only as far as SearXNG's own upstreams — the same local-first
posture as the on-box geocoder. The base URL is pinned from config and never
model-supplied; only the query text is. This client speaks SearXNG's JSON API
(`/search?format=json`) and returns the top result rows; it never executes a tool
policy itself — the handler does.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

import httpx
import structlog
from cachetools import TTLCache

log = structlog.get_logger()

_TIMEOUT = 15.0
_DEFAULT_LIMIT = 6

# Repeat-search cache. A deep-research fan re-queries the same terms across its
# gather/analyst/refill rounds and across whole runs; each repeat is another hit on
# SearXNG's upstream engines, which is what gets the box rate-limited (429/403). A short
# in-process TTL cache collapses those repeats so identical searches leave the box once
# per window. In-process and per-key, like the OwnTracks token bucket — adequate at
# personal scale (one API process).
_CACHE_TTL_S = 3600.0  # 60 min: a long deep-research run can itself exceed the old 15 min,
# so a shorter window let mid-run repeats re-hit (and re-throttle) the upstreams; an hour
# folds a whole run plus a near re-run, and daily-news queries are date-qualified so a fresh
# day is a distinct key anyway — freshness within the hour isn't at risk.
_CACHE_MAX_ENTRIES = 256  # LRU bound so the cache can't grow without limit.


class WebSearchError(RuntimeError):
    """A search could not be completed — SearXNG unreachable, a non-2xx response,
    or a malformed body. Surfaced to the agent as a recoverable tool error."""


@dataclass(frozen=True)
class SearchHit:
    """One search result row: enough to cite and to follow with web_fetch."""

    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class Infobox:
    """A knowledge panel SearXNG blends from Wikidata/Wikipedia — a direct, zero-click answer
    to an entity query (who/what/when: a birth date, a population, a founder) that needs no
    web_fetch. `attributes` are the panel's labelled facts, capped; `url` is its canonical page."""

    title: str
    content: str
    attributes: tuple[tuple[str, str], ...] = ()
    url: str = ""


@dataclass(frozen=True)
class SearchResult:
    """A general web search: the ranked `hits` PLUS the zero-click extras SearXNG returns in the
    same response and we used to discard — a knowledge-panel `infobox` and any instant `answers`
    (definitions, unit/currency conversions, calculations). The extras let the agent answer a
    plain fact without spending a web_fetch; the hits are still unverified leads to open.
    `window_dropped` marks a result the caller asked to bound by recency that only came back
    once the window was retried away (see `SearxngClient.search`), so the tool can say so."""

    hits: list[SearchHit]
    infobox: Infobox | None = None
    answers: tuple[str, ...] = ()
    window_dropped: bool = False

    @property
    def is_empty(self) -> bool:
        """Nothing to show at all — no hits, no panel, no instant answer."""
        return not (self.hits or self.infobox or self.answers)


@dataclass(frozen=True)
class NewsHit:
    """One news result row: a search hit plus the article's publish date as the news
    engine reported it (`published`, "" when the engine gave none) — the freshness
    signal a general web result lacks, so a dated lead can be judged recent-or-stale."""

    title: str
    url: str
    snippet: str
    published: str


@dataclass(frozen=True)
class ScienceHit:
    """One scholarly result row (arXiv / PubMed / Scholar / Crossref): the paper's title, URL,
    abstract snippet, publish date, and authors (joined, "" when the engine gave none) — the
    citation signals a general web hit lacks, so a source can be judged primary and current."""

    title: str
    url: str
    snippet: str
    published: str
    authors: str


# SearXNG's recency filter values (`time_range`) — the coarse windows the engines support,
# mapping "today"/"this week" to a filter. Shared by every search that takes recency.
TIME_RANGES = ("day", "week", "month", "year")
# Back-compat alias (news was the first taker); prefer TIME_RANGES for new call sites.
NEWS_TIME_RANGES = TIME_RANGES

_MAX_INFOBOX_ATTRS = 6  # a knowledge panel's labelled facts, capped so one panel stays compact
_MAX_ANSWERS = 3  # instant answers surfaced; more than a few is noise, not a direct answer


def _parse_infobox(body: dict[str, object]) -> Infobox | None:
    """The first knowledge panel from SearXNG's `infoboxes`, or None. Each panel is
    `{infobox: title, content, urls: [{url}], attributes: [{label, value}]}`; we keep the
    title, the summary, a few labelled facts, and the canonical URL."""
    boxes = body.get("infoboxes")
    if not isinstance(boxes, list) or not boxes or not isinstance(boxes[0], dict):
        return None
    ib = boxes[0]
    title = str(ib.get("infobox") or ib.get("title") or "").strip()
    content = str(ib.get("content") or "").strip()
    attrs: list[tuple[str, str]] = []
    raw_attrs = ib.get("attributes")
    if isinstance(raw_attrs, list):
        for a in raw_attrs:
            if not isinstance(a, dict):
                continue
            label = str(a.get("label") or "").strip()
            value = str(a.get("value") or "").strip()
            if label and value:
                attrs.append((label, value))
            if len(attrs) >= _MAX_INFOBOX_ATTRS:
                break
    url = ""
    urls = ib.get("urls")
    if isinstance(urls, list):
        for u in urls:
            if isinstance(u, dict) and str(u.get("url") or "").strip():
                url = str(u["url"]).strip()
                break
    if not (title or content or attrs):
        return None
    return Infobox(title=title, content=content, attributes=tuple(attrs), url=url)


def _parse_answers(body: dict[str, object]) -> tuple[str, ...]:
    """SearXNG's instant `answers` as plain strings (a definition, a unit/currency conversion, a
    calculation). Newer SearXNG wraps each as `{answer, url}`; older gives a bare string — accept
    both. Capped and de-duped, order preserved."""
    raw = body.get("answers")
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for a in raw:
        text = str(a.get("answer") or "").strip() if isinstance(a, dict) else str(a).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= _MAX_ANSWERS:
            break
    return tuple(out)


def _join_authors(raw: object) -> str:
    """A science row's `authors` (a list, or already a string) as one compact ", "-joined string."""
    if isinstance(raw, list):
        names = [str(a).strip() for a in raw if str(a).strip()]
        return ", ".join(names)
    return str(raw or "").strip()


def _unresponsive_engines(body: dict[str, object]) -> list[str]:
    """The engines that FAILED this query, as SearXNG reported them in `unresponsive_engines`
    — rows of `[engine, human-readable reason]` (`webutils.get_json_response`). This is the only
    machine-readable per-engine health signal the JSON API gives us, and it was discarded.

    Without it, working out which engines are down means tailing the searxng CONTAINER log and
    correlating its error lines against search volume by hand — how the 2026-09-01 investigation
    had to proceed, and it read Brave as dead when Brave was answering 29 of 31 searches. The
    two failure modes look identical in that log and are opposites: an engine failing on every
    single query is blocked (DuckDuckGo, whose engine passes `suspended_time=0` so SearXNG never
    benches it), while one failing twice in a burst is merely rate-limited and recovers in
    minutes. Only a per-query record separates them.

    Tolerant of the row shape (list, tuple, or a bare name) because this is diagnostics: a
    format change upstream must never raise inside a search that otherwise succeeded."""
    raw = body.get("unresponsive_engines")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for row in raw:
        if isinstance(row, str):
            name, reason = row.strip(), ""
        elif isinstance(row, (list, tuple)) and row:
            name = str(row[0]).strip()
            reason = str(row[1]).strip() if len(row) > 1 else ""
        else:
            continue
        if name:
            out.append(f"{name}: {reason}" if reason else name)
    return out


class SearxngClient:
    """Query a pinned SearXNG instance. `transport` is injectable so tests run
    against a mock with no network (DEVELOPMENT.md "no network in tests")."""

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
        # One repeat-search cache per client (the client is an app-lifetime singleton),
        # keyed on (query, time_range, limit). cachetools.TTLCache supplies the TTL + LRU
        # eviction; `timer` threads our injectable clock for deterministic expiry tests. None
        # when disabled (`cache_ttl_s <= 0`). Only non-empty results are stored.
        self._cache: TTLCache[tuple[str, str, int], SearchResult] | None = self._new_cache(
            cache_ttl_s, clock
        )
        # Twin caches for the news and science categories, keyed the same way so a
        # deep-research fan's repeated category queries collapse like general searches do.
        self._news_cache: TTLCache[tuple[str, str, int], list[NewsHit]] | None = self._new_cache(
            cache_ttl_s, clock
        )
        self._science_cache: TTLCache[tuple[str, str, int], list[ScienceHit]] | None = (
            self._new_cache(cache_ttl_s, clock)
        )

    @staticmethod
    def _new_cache(cache_ttl_s: float, clock: Callable[[], float]) -> TTLCache | None:
        if cache_ttl_s <= 0:
            return None
        return TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)

    async def _query(
        self, query: str, *, categories: str = "", time_range: str = ""
    ) -> dict[str, object]:
        """Issue one SearXNG JSON query and return the parsed body. Shared by every category
        method so the base URL, the JSON format, the SSRF-free pinned host, and the identical
        error mapping (a 403 usually means the JSON format is off; a transport error means the
        instance is unreachable) live in ONE place. Raises WebSearchError on any failure."""
        if not self._base_url:
            raise WebSearchError("web search is not configured on this instance")
        params = {"q": query, "format": "json"}
        if categories:
            params["categories"] = categories
        if time_range:
            params["time_range"] = time_range
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.get(f"{self._base_url}/search", params=params)
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            # A reachable instance that refused the request — most often a 403 because the JSON
            # format is not enabled (deploy/searxng/settings.yml must list it). Log the status
            # so a config drift is diagnosable, not just "unavailable".
            log.warning("web.search_failed", status=exc.response.status_code, error=repr(exc))
            raise WebSearchError("the web search service is unavailable right now") from exc
        except (httpx.HTTPError, ValueError) as exc:
            # Transport-level failure (unreachable / timeout) or a non-JSON body.
            log.warning("web.search_failed", error=repr(exc))
            raise WebSearchError("the web search service is unavailable right now") from exc
        if not isinstance(body, dict):
            return {}
        # Per-engine health, one line per query that lost an engine. Deliberately WITHOUT the
        # query text: which engines are blocked is independent of what was asked, and this line
        # is for operating the box, not for reading what the owner searched for. `time_range`
        # rides along because a window itself removes engines (see `search`), so a reader can
        # tell a blocked engine from a filtered-out one.
        if down := _unresponsive_engines(body):
            log.info(
                "web.search_engines_down",
                engines=down,
                categories=categories or "general",
                time_range=time_range or "",
            )
        return body

    @staticmethod
    def _rows(body: dict[str, object], limit: int) -> list[dict]:
        rows = body.get("results")
        if not isinstance(rows, list):
            return []
        return [
            r
            for r in rows[: max(limit, 0)]
            if isinstance(r, dict) and str(r.get("url") or "").strip()
        ]

    async def search(
        self, query: str, limit: int = _DEFAULT_LIMIT, *, time_range: str = ""
    ) -> SearchResult:
        """A general web search. Returns a SearchResult: the ranked hits PLUS the zero-click
        extras SearXNG returns in the same response — a Wikidata/Wikipedia `infobox` and any
        instant `answers` (definitions, conversions, calculations) — so a plain fact can be
        answered without a web_fetch. `time_range` (one of TIME_RANGES; anything else = no
        window) optionally bounds recency for a general query, the same filter news uses.

        A window that comes back with NOTHING is retried once WITHOUT it, and the widened
        result is returned flagged (`window_dropped`). A recency window is far more
        destructive than it looks: SearXNG SKIPS every engine that lacks `time_range_support`
        outright (on this deployment that drops bing and wikipedia — the latter being the
        infobox source, so the knowledge panel goes too), and the engines that survive filter
        on when a page was PUBLISHED, which blanks any query about an evergreen page. Observed
        live: nine `since="day"` searches for local cinema showtimes returned zero results each
        while the same query with no window returned ten — the agent burned a whole turn
        rewording the query because the filter, not the wording, was the problem."""
        tr = time_range if time_range in TIME_RANGES else ""
        result = await self._search_window(query, limit, tr)
        if tr and result.is_empty:
            widened = await self._search_window(query, limit, "")
            if not widened.is_empty:
                return replace(widened, window_dropped=True)
        return result

    async def _search_window(self, query: str, limit: int, tr: str) -> SearchResult:
        """One general search at one (already validated) recency window, through the cache."""
        key = (query.strip(), tr, limit)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return cached
        body = await self._query(query, time_range=tr)
        hits = [
            SearchHit(
                title=str(r.get("title") or "").strip() or str(r["url"]).strip(),
                url=str(r["url"]).strip(),
                snippet=str(r.get("content") or "").strip(),
            )
            for r in self._rows(body, limit)
        ]
        result = SearchResult(hits=hits, infobox=_parse_infobox(body), answers=_parse_answers(body))
        # Cache only a result that carried SOMETHING (hits or an extra): an all-empty result is
        # often a transient throttle we should retry, not a real "nothing", for the whole TTL.
        if self._cache is not None and not result.is_empty:
            self._cache[key] = result
        return result

    async def search_news(
        self, query: str, *, time_range: str = "day", limit: int = _DEFAULT_LIMIT
    ) -> list[NewsHit]:
        """Search SearXNG's NEWS category — the same on-box metasearch, but dispatched to the
        news engines (Google/Bing/… News) and filtered to a recency window (`time_range`, one of
        TIME_RANGES; anything else means no window). News rows carry a `publishedDate` the general
        category lacks, so each hit keeps that date as a freshness signal. Order is SearXNG's
        blended ranking (not re-sorted), so a strong recent story stays on top rather than a bare
        date sort burying the relevant lead. Raises WebSearchError like `search`."""
        tr = time_range if time_range in TIME_RANGES else ""
        key = (query.strip(), tr, limit)
        if self._news_cache is not None and (cached := self._news_cache.get(key)) is not None:
            return cached
        body = await self._query(query, categories="news", time_range=tr)
        hits = [
            NewsHit(
                title=str(r.get("title") or "").strip() or str(r["url"]).strip(),
                url=str(r["url"]).strip(),
                snippet=str(r.get("content") or "").strip(),
                published=str(r.get("publishedDate") or "").strip(),
            )
            for r in self._rows(body, limit)
        ]
        if self._news_cache is not None and hits:
            self._news_cache[key] = hits
        return hits

    async def search_science(self, query: str, limit: int = _DEFAULT_LIMIT) -> list[ScienceHit]:
        """Search SearXNG's SCIENCE category — the scholarly engines (arXiv, PubMed, Semantic /
        Google Scholar, Crossref) instead of the open web, so a research question rests on primary
        literature. Science rows carry `publishedDate` and `authors` the general category lacks,
        kept so a source can be judged primary and current. Raises WebSearchError like `search`."""
        key = (query.strip(), "", limit)
        if self._science_cache is not None and (cached := self._science_cache.get(key)) is not None:
            return cached
        body = await self._query(query, categories="science")
        hits = [
            ScienceHit(
                title=str(r.get("title") or "").strip() or str(r["url"]).strip(),
                url=str(r["url"]).strip(),
                snippet=str(r.get("content") or "").strip(),
                published=str(r.get("publishedDate") or "").strip(),
                authors=_join_authors(r.get("authors")),
            )
            for r in self._rows(body, limit)
        ]
        if self._science_cache is not None and hits:
            self._science_cache[key] = hits
        return hits
