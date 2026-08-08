"""Free public-records search backing jerv's `public_records` tool
(docs/reference/ASSISTANT.md "Agent selection").

v1 is CourtListener (Free Law Project): a FREE, no-key REST API for U.S. court
opinions and RECAP dockets, searched by name. This is the fix for a real research
miss — a candidate's disciplinary/court history filed under a prior/maiden name that
web-only search never surfaced (CourtListener returns it directly by name). Like the
SearXNG client the base URL is pinned from config and never model-supplied; only the
name text is, and an optional token merely lifts the anonymous rate limit. This client
speaks CourtListener's `/api/rest/v4/search/` JSON API and returns a small merged row
list; it never executes a tool policy itself — the handler does.

It also speaks the `/api/rest/v4/people/` endpoint (`search_people`) — the small judges/
officials DB — surfacing alias links (`is_alias_of`) and positions, so a person filed under a
prior name is tied to their canonical record. Still keyless; a supplement to the case search.
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
_DEFAULT_LIMIT = 10

# Repeat-search cache, mirroring SearxngClient: a research fan re-queries the same name
# across rounds, and each repeat is another hit on CourtListener's rate-limited free API.
# A short in-process TTL cache collapses those repeats so an identical (name, limit) leaves
# the box once per window. In-process and per-key — adequate at personal scale (one API
# process).
_CACHE_TTL_S = 900.0  # 15 min: long enough to fold a run's repeats, short enough to stay fresh.
_CACHE_MAX_ENTRIES = 256  # LRU bound so the cache can't grow without limit.

# The two search corpora CourtListener exposes for a name query: `o` = opinions (decided
# cases), `r` = RECAP dockets (filings). We query both and merge so a name that only appears
# on a docket (a case with no published opinion) is still found.
_SEARCH_TYPES = (("o", "opinion"), ("r", "docket"))
_MAX_PEOPLE = 5  # judges/officials surfaced per name — the people DB is small, alias links matter
_MAX_POSITION_TITLES = 4  # position titles listed per person before collapsing to a count


@dataclass(frozen=True)
class Record:
    """One public-records hit: enough to summarize and to verify against the primary source."""

    case_name: str
    court: str
    date_filed: str
    docket_number: str
    url: str
    kind: str  # "opinion" (a decided case) or "docket" (a filing)


@dataclass(frozen=True)
class Person:
    """One CourtListener judge/official record. The point here is the ALIAS edge: a person
    filed under a prior name links (`is_alias_of`) to their canonical record, and `positions`
    names the offices held — the pivot the opinion/docket search alone can't surface."""

    name: str
    url: str
    is_alias_of: str  # a URL to the canonical record when this row is an alias, else ""
    positions: tuple[str, ...]  # position titles held (courts/offices), capped
    position_count: int


class CourtListenerClient:
    """Query the pinned CourtListener free API by name. `transport` is injectable so tests
    run against a mock with no network (DEVELOPMENT.md "no network in tests"). An empty
    `base_url` disables the client (the handler reports "not configured"); a non-empty
    `token` is sent as `Authorization: Token …` to lift the anonymous rate limit."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        cache_ttl_s: float = _CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._transport = transport
        # One repeat-search cache per client (an app-lifetime singleton), keyed on (name,
        # limit); cachetools.TTLCache supplies the TTL + LRU eviction and `timer` threads our
        # injectable clock for deterministic expiry tests. None when disabled (`cache_ttl_s
        # <= 0`). Only a successful, non-empty result is stored (see `search`).
        self._cache: TTLCache[tuple[str, int], list[Record]] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)
            if cache_ttl_s > 0
            else None
        )
        # A sibling cache for the people/alias lookup — keyed on the name alone (the people DB
        # is small and unpaged here), same TTL + LRU bound as the case-search cache.
        self._people_cache: TTLCache[str, list[Person]] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)
            if cache_ttl_s > 0
            else None
        )

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def search(self, name: str, *, limit: int = _DEFAULT_LIMIT) -> tuple[list[Record], bool]:
        """Search opinions AND RECAP dockets for `name`, merge + dedup, and return
        (records, ok). `ok` is False only when the source was unreachable or errored — so the
        tool can tell a real "no records for this name" (ok=True, empty) apart from an outage
        (ok=False), never raising into the turn. A hit found in both corpora collapses to one
        row (the opinion, queried first). Errors are logged and swallowed."""
        query = name.strip()
        if not self._base_url or not query:
            return [], bool(self._base_url)
        key = (query, limit)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return cached, True
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Token {self._token}"
        merged: dict[tuple[str, str, str], Record] = {}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                for type_code, kind in _SEARCH_TYPES:
                    rows = await self._search_type(client, query, type_code, headers)
                    for row in rows:
                        record = _parse_row(row, kind=kind, base_url=self._base_url)
                        if record is None:
                            continue
                        # Dedup the o/r overlap: the same case can surface as both an opinion
                        # and a docket. Keyed on the case identity (name + docket + court), the
                        # first-seen (opinion) wins, so a case is reported once.
                        merged.setdefault(_dedup_key(record), record)
                        if len(merged) >= limit:
                            break
        except (httpx.HTTPError, ValueError) as exc:
            # Unreachable / timeout / non-2xx / malformed body: surface an outage flag, never
            # crash the turn — the tool degrades to "records service is unavailable".
            log.warning("web.public_records_failed", error=repr(exc))
            return [], False
        records = list(merged.values())[:limit]
        # Cache only a successful, non-empty result: an empty list may be a transient throttle,
        # so we must retry it next time rather than serve [] for the whole TTL (as SearxngClient).
        if self._cache is not None and records:
            self._cache[key] = records
        return records, True

    async def _search_type(
        self, client: httpx.AsyncClient, query: str, type_code: str, headers: dict[str, str]
    ) -> list[dict]:
        """One `/search/` call for a single corpus type; returns the raw result rows."""
        resp = await client.get(
            f"{self._base_url}/api/rest/v4/search/",
            params={"q": query, "type": type_code},
            headers=headers,
        )
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("results") if isinstance(body, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def search_people(self, name: str) -> tuple[list[Person], bool]:
        """Look up judges/officials by name in CourtListener's `people` DB and surface their
        alias links (`is_alias_of`) and positions. This is a SUPPLEMENT to `search`: a person
        filed under a prior name is linked here to their canonical record. Returns (people, ok);
        `ok` is False only on an outage (never raises). Keyless, like `search`."""
        query = name.strip()
        if not self._base_url or not query:
            return [], bool(self._base_url)
        if self._people_cache is not None and (cached := self._people_cache.get(query)) is not None:
            return cached, True
        first, last = _split_name(query)
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Token {self._token}"
        params: dict[str, str] = {}
        if first:
            params["name_first"] = first
        if last:
            params["name_last"] = last
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.get(
                    f"{self._base_url}/api/rest/v4/people/", params=params, headers=headers
                )
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web.public_records_people_failed", error=repr(exc))
            return [], False
        rows = body.get("results") if isinstance(body, dict) else None
        people = [
            p
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and (p := _parse_person(row, base_url=self._base_url))
        ][:_MAX_PEOPLE]
        if self._people_cache is not None and people:
            self._people_cache[query] = people
        return people, True


def _parse_row(row: dict, *, kind: str, base_url: str) -> Record | None:
    """Map one CourtListener result row to a `Record`, or None when it carries no case name.
    `absolute_url`/`docket_absolute_url` are site-relative paths, joined to the pinned base."""
    case_name = str(row.get("caseName") or "").strip()
    if not case_name:
        return None
    path = str(row.get("absolute_url") or row.get("docket_absolute_url") or "").strip()
    url = f"{base_url}{path}" if path.startswith("/") else path
    return Record(
        case_name=case_name,
        court=str(row.get("court") or "").strip(),
        date_filed=str(row.get("dateFiled") or "").strip(),
        docket_number=str(row.get("docketNumber") or "").strip(),
        url=url,
        kind=kind,
    )


def _dedup_key(record: Record) -> tuple[str, str, str]:
    """The case-identity key for collapsing the opinion/docket overlap — normalized so the
    same case in both corpora maps to one entry."""
    return (
        record.case_name.lower(),
        record.docket_number.lower(),
        record.court.lower(),
    )


def _split_name(name: str) -> tuple[str, str]:
    """Split a full name into (first, last) for the people endpoint's separate fields. The last
    token is the surname; everything before it is the first/given part. A single token is a
    surname (the more selective field)."""
    parts = name.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _position_title(row: object) -> str:
    """A short title for one `positions` entry. The people endpoint returns positions as nested
    objects (a `position_type` / `job_title` plus a court/organization) or as bare resource URLs
    when unexpanded; only the object form yields a title."""
    if not isinstance(row, dict):
        return ""
    kind = str(row.get("position_type") or row.get("job_title") or "").strip()
    where = str(row.get("organization_name") or row.get("court") or "").strip()
    return f"{kind} — {where}" if kind and where else kind or where


def _parse_person(row: dict, *, base_url: str) -> Person | None:
    """Map one CourtListener `people` row to a `Person`, or None when it carries no name."""
    name = " ".join(
        str(row.get(k) or "").strip()
        for k in ("name_first", "name_middle", "name_last", "name_suffix")
        if row.get(k)
    ).strip()
    if not name:
        return None
    person_id = str(row.get("id") or "").strip()
    slug = str(row.get("slug") or "").strip()
    url = f"{base_url}/person/{person_id}/{slug}/" if person_id else ""
    positions_raw = row.get("positions")
    positions: list = positions_raw if isinstance(positions_raw, list) else []
    titles = tuple(t for p in positions if (t := _position_title(p)))
    return Person(
        name=name,
        url=url,
        is_alias_of=str(row.get("is_alias_of") or "").strip(),
        positions=titles[:_MAX_POSITION_TITLES],
        position_count=len(positions),
    )
