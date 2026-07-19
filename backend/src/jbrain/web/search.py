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
from dataclasses import dataclass

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
_CACHE_TTL_S = 900.0  # 15 min: long enough to fold a run's (and near re-runs') repeats,
# short enough that results stay fresh.
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
        # keyed on (query, limit). cachetools.TTLCache supplies the TTL + LRU eviction;
        # `timer` threads our injectable clock for deterministic expiry tests. None when
        # disabled (`cache_ttl_s <= 0`). Only non-empty results are stored (see `search`).
        self._cache: TTLCache[tuple[str, int], list[SearchHit]] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)
            if cache_ttl_s > 0
            else None
        )

    async def search(self, query: str, limit: int = _DEFAULT_LIMIT) -> list[SearchHit]:
        if not self._base_url:
            raise WebSearchError("web search is not configured on this instance")
        key = (query.strip(), limit)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return cached
        params = {"q": query, "format": "json"}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.get(f"{self._base_url}/search", params=params)
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            # A reachable instance that refused the request — most often a 403 because
            # the JSON format is not enabled (deploy/searxng/settings.yml must list it).
            # Log the status so a config drift is diagnosable, not just "unavailable".
            log.warning("web.search_failed", status=exc.response.status_code, error=repr(exc))
            raise WebSearchError("the web search service is unavailable right now") from exc
        except (httpx.HTTPError, ValueError) as exc:
            # Transport-level failure (unreachable / timeout) or a non-JSON body.
            log.warning("web.search_failed", error=repr(exc))
            raise WebSearchError("the web search service is unavailable right now") from exc
        rows = body.get("results") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return []
        hits: list[SearchHit] = []
        for row in rows[: max(limit, 0)]:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            hits.append(
                SearchHit(
                    title=str(row.get("title") or "").strip() or url,
                    url=url,
                    snippet=str(row.get("content") or "").strip(),
                )
            )
        # Cache only a non-empty result: an empty list is often a transient throttle, not
        # a real "no results", so we must retry it next time rather than serve [] for the
        # whole TTL.
        if self._cache is not None and hits:
            self._cache[key] = hits
        return hits
