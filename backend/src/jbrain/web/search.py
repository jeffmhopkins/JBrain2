"""Web search: the self-hosted SearXNG instance first, then two metered hosted tiers behind it —
Brave's Search API, then Tavily's (docs/reference/ASSISTANT.md "Agent selection",
docs/plans/BROWSER_AGENT_PLAN.md B3).

SearXNG is a metasearch engine the owner runs on their own box, so most searches leave the box
only as far as SearXNG's own upstreams — the same local-first posture as the on-box geocoder.
When it errors, comes back thin (fewer than `THIN_RESULT_HITS`) or degraded (one engine left
standing), a general search falls through to Brave (keyed, enabled and under the owner's monthly
query budget) and then Tavily, each spending real money or credit only on the searches SearXNG
could not carry. Base URLs are pinned from config and never model-supplied; only the query text
is. The clients return result rows; they never execute a tool policy themselves — the handler
does. News and science searches stay on SearXNG's category engines.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from cachetools import TTLCache

from jbrain.web.tavily_health import TavilyHealth

log = structlog.get_logger()

_TIMEOUT = 15.0
# How long a general search waits on SearXNG when hosted tiers stand behind it: a hung instance
# would otherwise stall every search the full _TIMEOUT before Brave or Tavily is even asked.
SEARXNG_CHAIN_TIMEOUT_S = 8.0
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

# A SearXNG answer with fewer hits than this (or than the caller's own limit, if smaller) is too
# thin to stand alone, so the hosted tiers are asked. Three is "a lead, a second opinion and a
# spare": below it the agent is one dead link away from nothing.
THIN_RESULT_HITS = 3


class WebSearchError(RuntimeError):
    """A search could not be completed — SearXNG unreachable, a non-2xx response,
    or a malformed body. Surfaced to the agent as a recoverable tool error."""


@dataclass(frozen=True)
class SearchHit:
    """One search result row: enough to cite and to follow with web_fetch."""

    title: str
    url: str
    snippet: str
    # The page's publish/update date when the index reported one ("" otherwise) — Tavily's
    # `published_date`; SearXNG's general category carries none.
    published: str = ""


_MAX_SITES = 10  # domains per include/exclude list — a filter, not a crawl list


@dataclass(frozen=True)
class SearchOptions:
    """The agent's search controls beyond the query (docs.tavily.com best practices). Tavily
    honours all of them; SearXNG and Brave honour the site filters as `site:` operators (the
    same spelling serves both) and ignore the rest.

    `depth` is Tavily's `search_depth`: `basic` (1 credit) or `advanced` (2 credits — higher
    relevance on niche, local or multi-faceted queries, and up to three relevant passages per
    page instead of one generic summary). `include_domains` restricts to those sites (a
    business's own site, an official source); `exclude_domains` drops sites; `exact` returns
    only pages containing the query's quoted phrase(s) — for a proper name a general index
    would otherwise drown in look-alikes ("Epic Theatres" vs Epic Games)."""

    depth: str = "basic"
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    exact: bool = False

    def searxng_query(self, query: str) -> str:
        """The query with the site filters spelled as operators the scraper engines accept."""
        parts = [query]
        if len(self.include_domains) == 1:
            parts.append(f"site:{self.include_domains[0]}")
        elif self.include_domains:
            parts.append("(" + " OR ".join(f"site:{d}" for d in self.include_domains) + ")")
        parts += [f"-site:{d}" for d in self.exclude_domains]
        return " ".join(parts)


DEPTHS = ("basic", "advanced")


def normalize_domain(raw: object) -> str:
    """A bare lowercase host from whatever the model wrote ("https://www.X.com/path" -> "x.com"),
    or "" when nothing host-like is left."""
    text = str(raw or "").strip().lower()
    for prefix in ("https://", "http://"):
        text = text.removeprefix(prefix)
    text = text.split("/", 1)[0].split("?", 1)[0].removeprefix("www.").strip(".")
    if not text or "." not in text or any(c.isspace() for c in text):
        return ""
    return text


def domain_list(raw: object) -> tuple[str, ...]:
    """A capped, de-duplicated tuple of hosts from a list or a comma-separated string."""
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    seen: dict[str, None] = {}
    for item in items:
        if host := normalize_domain(item):
            seen.setdefault(host, None)
    return tuple(seen)[:_MAX_SITES]


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
    # Engine health for THIS query: the engines SearXNG reported failing (suspended ones
    # included) and the ones whose results actually came back.
    engines_down: tuple[str, ...] = ()
    engines_answered: tuple[str, ...] = ()
    # `source` is where the hits came from: "searxng", "brave" or "tavily". When SearXNG's own
    # thin answer is returned because every hosted tier that was tried FAILED, `hosted_failure`
    # says why, so the agent knows nothing better was available and the owner's quota problem
    # is not mistaken for an empty web.
    source: str = "searxng"
    hosted_failure: str = ""

    @property
    def is_empty(self) -> bool:
        """Nothing to show at all — no hits, no panel, no instant answer."""
        return not (self.hits or self.infobox or self.answers)

    @property
    def degraded(self) -> bool:
        """Engines failed AND at most one answered — the metasearch is down to a single index.
        Measured 2026-10-03: with DuckDuckGo, Brave, Qwant, Startpage and Mojeek all blocked,
        Bing alone ranked every "Epic" brand above the cinema the owner asked about, on twenty
        rewordings in a row. A single surviving index is not a blend, and its off-topic
        results are not a wording problem the agent can fix."""
        return bool(self.engines_down) and len(self.engines_answered) <= 1


@dataclass(frozen=True)
class NewsHit:
    """One news result row: a search hit plus the article's publish date as the news
    engine reported it (`published`, "" when the engine gave none) — the freshness
    signal a general web result lacks, so a dated lead can be judged recent-or-stale."""

    title: str
    url: str
    snippet: str
    published: str
    # The window blanked every engine that can filter by date, so this hit came from the
    # unwindowed search and passed the window on its own publish date instead (`search_news`).
    window_widened: bool = False


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
# How far back each window reaches when news is dated here rather than by SearXNG.
_WINDOW_SPAN = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=31),
    "year": timedelta(days=366),
}
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


def _unresponsive_rows(body: dict[str, object]) -> list[tuple[str, str]]:
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
    out: list[tuple[str, str]] = []
    for row in raw:
        if isinstance(row, str):
            name, reason = row.strip(), ""
        elif isinstance(row, (list, tuple)) and row:
            name = str(row[0]).strip()
            reason = str(row[1]).strip() if len(row) > 1 else ""
        else:
            continue
        if name:
            out.append((name, reason))
    return out


def _unresponsive_engines(body: dict[str, object]) -> list[str]:
    """`_unresponsive_rows` rendered for the log line: `name: reason`, or a bare name."""
    return [f"{n}: {r}" if r else n for n, r in _unresponsive_rows(body)]


def _published_at(value: str) -> datetime | None:
    """A news row's publish date as an aware datetime; SearXNG gives ISO text, naive as UTC."""
    try:
        when = datetime.fromisoformat(value.strip().replace(" ", "T", 1))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _within_window(hits: list[NewsHit], time_range: str) -> list[NewsHit]:
    """The hits published inside `time_range`, by their own dates, flagged as dated here."""
    cutoff = datetime.now(UTC) - _WINDOW_SPAN[time_range]
    kept: list[NewsHit] = []
    for h in hits:
        when = _published_at(h.published) if h.published else None
        if when is not None and when >= cutoff:
            kept.append(replace(h, window_widened=True))
    return kept


def _answered_engines(body: dict[str, object]) -> tuple[str, ...]:
    """The engines whose results came back, read off every result row's `engines` list (all
    rows, not just the ones a caller keeps — a hit trimmed by `limit` still proves its engine
    answered). Order of first appearance; empty when rows carry no `engines` field."""
    rows = body.get("results")
    if not isinstance(rows, list):
        return ()
    seen: dict[str, None] = {}
    for r in rows:
        engines = r.get("engines") if isinstance(r, dict) else None
        if isinstance(engines, list):
            for e in engines:
                if isinstance(e, str) and e.strip():
                    seen.setdefault(e.strip(), None)
    return tuple(seen)


@dataclass(frozen=True)
class HostedOutcome:
    """One hosted-search attempt: the hits, and `failure` — a human reason when the call
    FAILED (quota, rate limit, rejected key, transport), "" when it was simply off, keyless,
    cooling down after a known failure, or found nothing."""

    hits: list[SearchHit]
    failure: str = ""


HostedSearch = Callable[..., Awaitable[HostedOutcome]]

_TAVILY_SEARCH_TIMEOUT = 20.0


class TavilySearch:
    """Tavily's hosted Search API — `web_search`'s LAST tier, behind SearXNG and Brave. It does not
    share the box's residential IP, which is what the scraper engines behind SearXNG block
    (2026-10-03: DuckDuckGo, Brave, Qwant, Startpage and Mojeek all refused it, leaving Bing
    alone). Reads the SAME live toggle + key as the Tavily fetch tier (`settings` -> (enabled,
    key)), so the PWA's Tavily panel governs both and an unkeyed box never calls it. One basic
    search is one credit; a successful result is cached for the TTL so a research fan's repeats
    collapse to one call. Failures feed `health` (quota/rate/key state, the owner's notice, a
    cooldown). Only the query text and the owner's key travel."""

    def __init__(
        self,
        base_url: str,
        settings: Callable[[], Awaitable[tuple[bool, str]]],
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        health: TavilyHealth | None = None,
        cache_ttl_s: float = _CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._base_url = base_url.rstrip("/")
        self._settings = settings
        self._transport = transport
        self._health = health
        self._cache: TTLCache[tuple[str, str, int, SearchOptions], list[SearchHit]] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)
            if cache_ttl_s > 0
            else None
        )

    async def search(
        self,
        query: str,
        limit: int,
        *,
        time_range: str = "",
        options: SearchOptions | None = None,
    ) -> HostedOutcome:
        if not self._base_url:
            return HostedOutcome([])
        opts = options or SearchOptions()
        tr = time_range if time_range in TIME_RANGES else ""
        key = (query.strip(), tr, limit, opts)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return HostedOutcome(cached)
        try:
            enabled, api_key = await self._settings()
        except Exception:  # noqa: BLE001 — a settings hiccup must not fail the search it backs
            log.warning("web.tavily_search_settings_unreadable", exc_info=True)
            return HostedOutcome([])
        if not enabled or not api_key:
            return HostedOutcome([])
        if self._health is not None and self._health.cooling_down():
            return HostedOutcome([], (await self._health.current()).detail or "Tavily is failing")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        depth = opts.depth if opts.depth in DEPTHS else "basic"
        payload: dict[str, object] = {
            "query": query,
            "max_results": max(1, limit),
            "search_depth": depth,
            # Free, and the freshness signal a lead otherwise lacks.
            "include_published_date": True,
        }
        if depth == "advanced":
            payload["chunks_per_source"] = 3  # the page's most relevant passages, not a summary
        if tr:
            payload["time_range"] = tr
        if opts.include_domains:
            payload["include_domains"] = list(opts.include_domains)
        if opts.exclude_domains:
            payload["exclude_domains"] = list(opts.exclude_domains)
        if opts.exact:
            payload["exact_match"] = True
        try:
            async with httpx.AsyncClient(
                timeout=_TAVILY_SEARCH_TIMEOUT, transport=self._transport
            ) as client:
                resp = await client.post(f"{self._base_url}/search", json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            log.warning("web.tavily_search_failed", status=status)
            reason = await self._health.failed(status, "search") if self._health else ""
            return HostedOutcome([], reason or f"Tavily returned HTTP {status}")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web.tavily_search_failed", error=repr(exc))
            return HostedOutcome([], "Tavily could not be reached")
        if self._health is not None:
            await self._health.succeeded("search")
        rows = body.get("results") if isinstance(body, dict) else None
        hits = [
            SearchHit(
                title=str(r.get("title") or "").strip() or str(r["url"]).strip(),
                url=str(r["url"]).strip(),
                snippet=str(r.get("content") or "").strip(),
                published=str(r.get("published_date") or "").strip(),
            )
            for r in (rows if isinstance(rows, list) else [])[: max(limit, 0)]
            if isinstance(r, dict) and str(r.get("url") or "").strip()
        ]
        if self._cache is not None and hits:
            self._cache[key] = hits
        return HostedOutcome(hits)


_BRAVE_SEARCH_TIMEOUT = 15.0
_BRAVE_MAX_COUNT = 20  # the API's ceiling for `count`
# The recency windows as Brave's `freshness` codes (past day / week / month / year).
_BRAVE_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
_TAG_RE = re.compile(r"<[^>]+>")


def utc_month(now: datetime | None = None) -> str:
    """The UTC calendar month a Brave query is billed to, as "YYYY-MM"."""
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def usage_count(raw: object, month: str) -> int:
    """Queries already spent in `month` from a stored {"month", "count"} record. Another month's
    record — or junk — reads as zero, which is how the counter rolls over without a reset job."""
    if isinstance(raw, dict) and raw.get("month") == month:
        count = raw.get("count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            return count
    return 0


@dataclass(frozen=True)
class BraveConfig:
    """The owner's live Brave settings: toggle, effective key (stored or env), monthly budget."""

    enabled: bool
    api_key: str
    budget: int


class BraveUsage:
    """This month's Brave query count, persisted through `load`/`save` (one app.settings row).

    `reserve` is a check-and-increment under an in-process asyncio.Lock, so a research fan's
    concurrent searches cannot both read count = budget - 1 and both spend. That is sound because
    one API process owns web search on this single-owner box; a second process would need the
    increment moved into one conditional SQL UPDATE. A storage failure fails CLOSED (no query
    is sent), because the counter is what keeps the owner's spending cap from ever being met."""

    def __init__(
        self,
        load: Callable[[], Awaitable[object]],
        save: Callable[[dict[str, object]], Awaitable[None]],
        *,
        month: Callable[[], str] = utc_month,
    ):
        self._load = load
        self._save = save
        self._month = month
        self._lock = asyncio.Lock()

    async def reserve(self, budget: int) -> bool:
        """Count one query against this month if it is under `budget`; False = do not send.
        Counted BEFORE the request goes out, so a request whose outcome is unknown (a timeout
        after Brave received it) is never missed — the count can only err high."""
        async with self._lock:
            try:
                month = self._month()
                count = usage_count(await self._load(), month)
                if count >= budget:
                    return False
                await self._save({"month": month, "count": count + 1})
            except Exception:  # noqa: BLE001 — fail closed: an unrecorded query is unbudgeted
                log.warning("web.brave_usage_unavailable", exc_info=True)
                return False
            return True


def _brave_text(raw: object) -> str:
    """Brave marks query terms with <strong> and HTML-escapes the rest; the agent wants text."""
    return html.unescape(_TAG_RE.sub("", str(raw or ""))).strip()


class BraveSearch:
    """Brave's Search API — `web_search`'s metered MIDDLE tier, asked only when SearXNG errored or
    came back thin. Brave runs its own index (not a scrape of someone else's) from Brave's IP, so
    it answers exactly the queries the box's blocked scraper engines cannot.

    Every request is counted against the owner's monthly budget first (`BraveUsage`), so the free
    monthly credit is never overrun: at the budget, Brave is skipped until the month turns. A
    successful result is cached for the TTL, so a research fan's repeats cost one query. Failures
    are mapped to what they mean for the next call: 401/403 = the key is rejected (skipped until
    the owner saves a different one), 402 or a quota 429 = the plan's credit is spent (skipped for
    the rest of the month), a plain 429 = rate-limited (this query only). Key and credit failures
    are recorded for the Settings panel through `save_error`. Only the query text, the window and
    the owner's key travel."""

    def __init__(
        self,
        base_url: str,
        settings: Callable[[], Awaitable[BraveConfig]],
        usage: BraveUsage,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        save_error: Callable[[dict[str, str]], Awaitable[None]] | None = None,
        cache_ttl_s: float = _CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        month: Callable[[], str] = utc_month,
    ):
        self._base_url = base_url.rstrip("/")
        self._settings = settings
        self._usage = usage
        self._transport = transport
        self._save_error = save_error
        self._month = month
        self._cache: TTLCache[tuple[str, str, int, SearchOptions], list[SearchHit]] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)
            if cache_ttl_s > 0
            else None
        )
        # In-process memory of a dead key / a spent month, so a search does not spend a request
        # re-learning either. A restart forgets both and re-learns with one call.
        self._rejected_key = ""
        self._exhausted_month = ""
        # Whether a last-error record may be on file (None = unknown since start), so a success
        # clears it with one write rather than one write per search.
        self._error_on_file: bool | None = None

    @property
    def wired(self) -> bool:
        return bool(self._base_url)

    def blocked(self, api_key: str) -> str:
        """Why a search would skip Brave right now on what this process has learned: "key_rejected"
        (this key was refused), "credit_spent" (the plan ran out this month), or "" — so the
        Settings status agrees with what a search will actually do."""
        if api_key and api_key == self._rejected_key:
            return "key_rejected"
        if self._exhausted_month and self._exhausted_month == self._month():
            return "credit_spent"
        return ""

    async def search(
        self,
        query: str,
        limit: int,
        *,
        time_range: str = "",
        options: SearchOptions | None = None,
    ) -> HostedOutcome:
        if not self._base_url:
            return HostedOutcome([])
        opts = options or SearchOptions()
        tr = time_range if time_range in TIME_RANGES else ""
        try:
            cfg = await self._settings()
        except Exception:  # noqa: BLE001 — a settings hiccup must not fail the search it backs
            log.warning("web.brave_search_settings_unreadable", exc_info=True)
            return HostedOutcome([])
        # Before the cache: switching Brave off (or clearing its key) must take effect at once,
        # not after an hour of cached answers still labelled "brave".
        if not cfg.enabled or not cfg.api_key:
            return HostedOutcome([])
        key = (query.strip(), tr, limit, opts)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return HostedOutcome(cached)
        if cfg.api_key == self._rejected_key:
            return HostedOutcome([], "Brave rejected the API key")
        if self._exhausted_month == self._month():
            return HostedOutcome([], "Brave's plan credit is used up for this month")
        outcome = await self._call(cfg, query, limit, tr, opts)
        if outcome is None:
            log.info("web.brave_budget_reached", budget=cfg.budget)
            return HostedOutcome([])  # the owner's own stop, not a failure
        if self._cache is not None and outcome.hits:
            self._cache[key] = outcome.hits
        return outcome

    async def probe(self) -> tuple[bool, int, str]:
        """The Settings "Test key" button: one LIVE query, counted like any other (it spends a
        real query, and the panel says so). Bypasses the cache and the dead-key / spent-month
        memory, so a fixed key or a renewed plan is seen at once. -> (ok, hits, detail)."""
        if not self._base_url:
            return False, 0, "The Brave tier isn't wired on this box (no JBRAIN_BRAVE_URL)."
        try:
            cfg = await self._settings()
        except Exception:  # noqa: BLE001 — reported, not raised: this is a diagnostic
            log.warning("web.brave_search_settings_unreadable", exc_info=True)
            return False, 0, "Couldn't read the Brave settings — try again."
        if not cfg.api_key:
            return False, 0, "No Brave API key is set — paste one, then Save & test."
        if not cfg.enabled:
            return False, 0, "Brave is switched off — turn it on to test the key."
        outcome = await self._call(cfg, "test", 3, "", SearchOptions())
        if outcome is None:
            return False, 0, f"This month's budget of {cfg.budget} queries is used up."
        if outcome.failure:
            return False, 0, outcome.failure
        n = len(outcome.hits)
        return True, n, f"Brave answered with {n} result(s) — the key works."

    async def _call(
        self, cfg: BraveConfig, query: str, limit: int, tr: str, opts: SearchOptions
    ) -> HostedOutcome | None:
        """One budgeted request. None = the budget is reached and nothing was sent."""
        if not await self._usage.reserve(cfg.budget):
            return None
        params: dict[str, str | int] = {
            "q": opts.searxng_query(query),
            "count": min(max(1, limit), _BRAVE_MAX_COUNT),
            "safesearch": "moderate",
        }
        if tr:
            params["freshness"] = _BRAVE_FRESHNESS[tr]
        headers = {"X-Subscription-Token": cfg.api_key, "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(
                timeout=_BRAVE_SEARCH_TIMEOUT, transport=self._transport
            ) as client:
                resp = await client.get(
                    f"{self._base_url}/res/v1/web/search", params=params, headers=headers
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            return HostedOutcome([], await self._failed(exc.response, cfg.api_key))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web.brave_search_failed", error=repr(exc))
            return HostedOutcome([], "Brave could not be reached")
        await self._succeeded(cfg.api_key)
        web = body.get("web") if isinstance(body, dict) else None
        rows = web.get("results") if isinstance(web, dict) else None
        hits = [
            SearchHit(
                title=_brave_text(r.get("title")) or str(r["url"]).strip(),
                url=str(r["url"]).strip(),
                snippet=_brave_text(r.get("description")),
                published=str(r.get("page_age") or r.get("age") or "").strip(),
            )
            for r in (rows if isinstance(rows, list) else [])
            if isinstance(r, dict) and str(r.get("url") or "").strip()
        ][: max(limit, 0)]
        return HostedOutcome(hits)

    async def _failed(self, resp: httpx.Response, api_key: str) -> str:
        status = resp.status_code
        code = _brave_error_code(resp)
        # The status and Brave's machine code only: the body's prose and the headers stay out of
        # the log (the request headers carry the key).
        log.warning("web.brave_search_failed", status=status, code=code)
        why = f"HTTP {status}, {code}" if code else f"HTTP {status}"
        # Observed live 2026-10-04: a bad token is a 422 SUBSCRIPTION_TOKEN_INVALID, not a 401.
        # A 422 carrying some OTHER code is a request problem, not the key's.
        token_code = "TOKEN_INVALID" in code or "SUBSCRIPTION_TOKEN" in code
        if status in (401, 403) or token_code or (status == 422 and not code):
            self._rejected_key = api_key
            detail = f"Brave rejected the API key ({why}) — check it in the Brave dashboard"
        elif status == 429 and code in ("", "RATE_LIMITED"):
            return "Brave is rate-limiting this box (HTTP 429)"  # transient; nothing to record
        elif status in (402, 429):
            self._exhausted_month = self._month()
            detail = f"Brave's plan credit is used up for this month ({why})"
        else:
            detail = f"Brave returned {why}"
        await self._record_error(detail)
        return detail

    async def _succeeded(self, api_key: str) -> None:
        if api_key == self._rejected_key:
            self._rejected_key = ""
        self._exhausted_month = ""
        if self._error_on_file is not False:
            await self._record_error("")

    async def _record_error(self, detail: str) -> None:
        if self._save_error is None:
            return
        at = datetime.now(UTC).isoformat(timespec="seconds") if detail else ""
        try:
            await self._save_error({"detail": detail, "at": at})
            self._error_on_file = bool(detail)
        except Exception:  # noqa: BLE001 — bookkeeping must never fail the search
            log.warning("web.brave_error_save_failed", exc_info=True)


def _brave_error_code(resp: httpx.Response) -> str:
    """Brave's machine error code (`{"error": {"code": "QUOTA_LIMITED", ...}}`), upper-cased,
    or "" — tolerant because it only refines a status we already have."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    return str(err.get("code") or "").upper() if isinstance(err, dict) else ""


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
        brave: HostedSearch | None = None,
        hosted: HostedSearch | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        # The hosted tiers behind a general search, in order: Brave, then Tavily (`hosted`).
        # Asked only when SearXNG errored or came back thin; news and science stay on SearXNG's
        # category engines.
        self._tiers: tuple[tuple[str, HostedSearch], ...] = tuple(
            (source, tier) for source, tier in (("brave", brave), ("tavily", hosted)) if tier
        )
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
        self,
        query: str,
        *,
        categories: str = "",
        time_range: str = "",
        wait_s: float = _TIMEOUT,
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
            async with httpx.AsyncClient(timeout=wait_s, transport=self._transport) as client:
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
        self,
        query: str,
        limit: int = _DEFAULT_LIMIT,
        *,
        time_range: str = "",
        options: SearchOptions | None = None,
    ) -> SearchResult:
        """A general web search: SearXNG first; when it errors, comes back thin (`_thin`) or
        degraded, the hosted tiers in order — Brave (enabled, keyed, under its monthly budget),
        then Tavily — and the first with hits answers (`source` names it). When every tier that
        was tried failed, SearXNG's own thin result is returned with `hosted_failure` saying why;
        when SearXNG errored and no tier answered, its WebSearchError is raised. Each tier keeps
        its own one-hour cache, so a repeat costs neither SearXNG's upstreams nor a metered query.

        Returns a SearchResult: the ranked hits PLUS the zero-click extras SearXNG returns in the
        same response — a Wikidata/Wikipedia `infobox` and any instant `answers` (definitions,
        conversions, calculations) — so a plain fact can be answered without a web_fetch; they
        ride along when a hosted tier supplies the hits. `time_range` (one of TIME_RANGES;
        anything else = no window) optionally bounds recency, the same filter news uses.

        A window that comes back with NOTHING from SearXNG is retried once WITHOUT it, and the
        widened result is returned flagged (`window_dropped`). A recency window is far more
        destructive than it looks: SearXNG SKIPS every engine that lacks `time_range_support`
        outright (on this deployment that drops bing and wikipedia — the latter being the
        infobox source, so the knowledge panel goes too), and the engines that survive filter
        on when a page was PUBLISHED, which blanks any query about an evergreen page. Observed
        live: nine `since="day"` searches for local cinema showtimes returned zero results each
        while the same query with no window returned ten — the agent burned a whole turn
        rewording the query because the filter, not the wording, was the problem."""
        tr = time_range if time_range in TIME_RANGES else ""
        opts = options or SearchOptions()
        result: SearchResult | None = None
        error = WebSearchError("the web search service is unavailable right now")
        try:
            result = await self._searxng(opts.searxng_query(query), limit, tr)
        except WebSearchError as exc:
            if not self._tiers:
                raise
            error = exc
        if result is not None and not self._thin(result, limit):
            return result
        failures: list[str] = []
        for source, tier in self._tiers:
            outcome = await tier(query, limit, time_range=tr, options=opts)
            if outcome.hits:
                return SearchResult(
                    hits=outcome.hits,
                    infobox=result.infobox if result else None,
                    answers=result.answers if result else (),
                    source=source,
                )
            if outcome.failure:
                failures.append(outcome.failure)
        if result is None:
            if failures:
                log.warning("web.search_all_tiers_failed", reasons=failures)
                raise WebSearchError(f"{error} (hosted search also failed: {'; '.join(failures)})")
            raise error
        return replace(result, hosted_failure="; ".join(failures)) if failures else result

    @staticmethod
    def _thin(result: SearchResult, limit: int) -> bool:
        """Too little to stand alone: fewer hits than THIN_RESULT_HITS (or the caller's own,
        smaller limit), or a degraded index — one surviving engine's hits are the off-topic
        list the 2026-10-03 cinema search drowned in, however many of them there are."""
        return len(result.hits) < min(THIN_RESULT_HITS, max(limit, 1)) or result.degraded

    async def _searxng(self, query: str, limit: int, tr: str) -> SearchResult:
        """SearXNG at the window, retried once without a window that blanked it."""
        result = await self._search_window(query, limit, tr)
        if tr and result.is_empty:
            widened = await self._search_window(query, limit, "")
            if not widened.is_empty:
                result = replace(widened, window_dropped=True)
        return result

    async def _search_window(self, query: str, limit: int, tr: str) -> SearchResult:
        """One general search at one (already validated) recency window, through the cache."""
        key = (query.strip(), tr, limit)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return cached
        wait_s = SEARXNG_CHAIN_TIMEOUT_S if self._tiers else _TIMEOUT
        body = await self._query(query, time_range=tr, wait_s=wait_s)
        hits = [
            SearchHit(
                title=str(r.get("title") or "").strip() or str(r["url"]).strip(),
                url=str(r["url"]).strip(),
                snippet=str(r.get("content") or "").strip(),
            )
            for r in self._rows(body, limit)
        ]
        result = SearchResult(
            hits=hits,
            infobox=_parse_infobox(body),
            answers=_parse_answers(body),
            engines_down=tuple(n for n, _ in _unresponsive_rows(body)),
            engines_answered=_answered_engines(body),
        )
        # Cache only a result that carried SOMETHING (hits or an extra): an all-empty result is
        # often a transient throttle we should retry, not a real "nothing", for the whole TTL.
        # A degraded one is the same throttle with one engine left standing — caching it pinned
        # a single index's off-topic hits to that query for an hour after the engines recovered.
        if self._cache is not None and not result.is_empty and not result.degraded:
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
        date sort burying the relevant lead. Raises WebSearchError like `search`.

        A window that comes back EMPTY is retried without it, and the hits are then held to the
        window by their own publish dates (undated ones dropped), flagged `window_widened`. A
        window skips every engine without `time_range_support` — Google News, this deployment's
        one dependable news engine, among them — so on 2026-10-07 "AI news, last day" came back
        empty because the only engines left (Reuters, Brave News) were erroring and throttled."""
        tr = time_range if time_range in TIME_RANGES else ""
        key = (query.strip(), tr, limit)
        if self._news_cache is not None and (cached := self._news_cache.get(key)) is not None:
            return cached
        hits = self._news_hits(await self._query(query, categories="news", time_range=tr), limit)
        if tr and not hits:
            # Wider than `limit` so the date filter still leaves enough of them.
            wide = self._news_hits(await self._query(query, categories="news"), limit * 3)
            hits = _within_window(wide, tr)[:limit]
            log.info("web.news_window_widened", time_range=tr, found=len(wide), kept=len(hits))
        if self._news_cache is not None and hits:
            self._news_cache[key] = hits
        return hits

    def _news_hits(self, body: dict[str, object], limit: int) -> list[NewsHit]:
        return [
            NewsHit(
                title=str(r.get("title") or "").strip() or str(r["url"]).strip(),
                url=str(r["url"]).strip(),
                snippet=str(r.get("content") or "").strip(),
                published=str(r.get("publishedDate") or "").strip(),
            )
            for r in self._rows(body, limit)
        ]

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
