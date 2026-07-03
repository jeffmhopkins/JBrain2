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

from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 15.0
_DEFAULT_LIMIT = 6


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

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def search(self, query: str, limit: int = _DEFAULT_LIMIT) -> list[SearchHit]:
        if not self._base_url:
            raise WebSearchError("web search is not configured on this instance")
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
        return hits
