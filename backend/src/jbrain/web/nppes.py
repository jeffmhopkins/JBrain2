"""NPPES / NPI registry lookup backing jerv's `provider_license` tool
(docs/reference/ASSISTANT.md "Agent selection").

The NPPES NPI Registry (CMS) is the FREE, no-key national registry of U.S. health-care
providers. For a person profile it gives a clinician's license number + state + specialty and,
crucially, any `other_names` — a prior or maiden name — which is the pivot to the RIGHT state
board (a disciplinary action is often filed under the former name). It is the national-registry
replacement for scraping a single state DOH portal; it carries NO disciplinary actions itself,
so a hit is the identifier + the alias, not a clean/dirty verdict.

FREE and no-key, like CourtListener: the base URL is pinned from config and never
model-supplied; only a public name (split into first/last) and an optional state go out. The
client returns `(providers, ok)` so an outage degrades (ok=False) rather than raising; it never
runs a tool policy itself — the handler does.
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
_API_VERSION = "2.1"

# Repeat-search cache, mirroring CourtListenerClient (see there for the rationale).
_CACHE_TTL_S = 900.0  # 15 min
_CACHE_MAX_ENTRIES = 256

# NPPES `basic.status` codes: "A" = active, "D" = deactivated. Surfaced verbatim + expanded.
_STATUS_LABELS = {"A": "active", "D": "deactivated"}


@dataclass(frozen=True)
class Taxonomy:
    """One provider taxonomy row: the specialty plus the state license it maps to."""

    desc: str
    license: str
    state: str
    primary: bool


@dataclass(frozen=True)
class Provider:
    """One NPI match: the identifier, the person, and — the point of the tool — any other_names."""

    npi: str
    name: str
    credential: str
    status: str  # the expanded status ("active"/"deactivated"/raw code)
    other_names: tuple[str, ...]  # each "First Last (type)" — a prior/maiden name to re-search
    taxonomies: tuple[Taxonomy, ...]
    city: str
    state: str


class NppesClient:
    """Query the pinned NPPES NPI registry by name. `transport` is injectable so tests run
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
        self._cache: TTLCache[tuple[str, str, int], list[Provider]] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)
            if cache_ttl_s > 0
            else None
        )

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def search(
        self, name: str, *, state: str = "", limit: int = _DEFAULT_LIMIT
    ) -> tuple[list[Provider], bool]:
        """Search the registry for `name` (split into first/last), optionally narrowed to a
        `state`. Returns (providers, ok); `ok` is False only on an outage so the tool tells a
        real "no provider" (ok=True, empty) apart from a source failure. Errors are logged and
        swallowed."""
        query = name.strip()
        state = state.strip().upper()
        if not self._base_url or not query:
            return [], bool(self._base_url)
        key = (query.lower(), state, limit)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return cached, True
        first, last = _split_name(query)
        params: dict[str, str | int] = {"version": _API_VERSION, "limit": limit}
        if first:
            params["first_name"] = first
        if last:
            params["last_name"] = last
        if state:
            params["state"] = state
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.get(f"{self._base_url}/api/", params=params)
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web.nppes_failed", error=repr(exc))
            return [], False
        rows = body.get("results") if isinstance(body, dict) else None
        providers = [
            p
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and (p := _parse_provider(row)) is not None
        ][:limit]
        if self._cache is not None and providers:
            self._cache[key] = providers
        return providers, True


def _split_name(name: str) -> tuple[str, str]:
    """Split a full name into (first, last) for the registry's separate fields. The last token
    is the surname (the field records/boards index on); everything before it is the first/given
    part. A single token is treated as a surname (the more selective field)."""
    parts = name.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _other_name(row: dict) -> str:
    """One `other_names` row rendered as "First Last (type)" — the alias to re-search under."""
    name = " ".join(
        str(row.get(k) or "").strip() for k in ("first_name", "last_name") if row.get(k)
    ).strip()
    if not name:
        # Organization-style other_name rows carry `organization_name` instead of first/last.
        name = str(row.get("organization_name") or "").strip()
    kind = str(row.get("type") or "").strip()
    return f"{name} ({kind})" if name and kind else name


def _parse_provider(row: dict) -> Provider | None:
    basic_raw = row.get("basic")
    basic: dict = basic_raw if isinstance(basic_raw, dict) else {}
    name = " ".join(
        str(basic.get(k) or "").strip()
        for k in ("name_prefix", "first_name", "last_name")
        if basic.get(k)
    ).strip()
    if not name:
        return None
    status_code = str(basic.get("status") or "").strip()
    others = tuple(
        n for o in (row.get("other_names") or []) if isinstance(o, dict) and (n := _other_name(o))
    )
    taxonomies = tuple(
        Taxonomy(
            desc=str(t.get("desc") or "").strip(),
            license=str(t.get("license") or "").strip(),
            state=str(t.get("state") or "").strip(),
            primary=bool(t.get("primary")),
        )
        for t in (row.get("taxonomies") or [])
        if isinstance(t, dict)
    )
    addr: dict = next((a for a in (row.get("addresses") or []) if isinstance(a, dict)), {})
    return Provider(
        npi=str(row.get("number") or "").strip(),
        name=name,
        credential=str(basic.get("credential") or "").strip(),
        status=_STATUS_LABELS.get(status_code, status_code),
        other_names=others,
        taxonomies=taxonomies,
        city=str(addr.get("city") or "").strip(),
        state=str(addr.get("state") or "").strip(),
    )
