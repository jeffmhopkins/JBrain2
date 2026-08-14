"""Wikidata identity resolution backing jerv's `resolve_identity` tool
(docs/reference/ASSISTANT.md "Agent selection").

This is the "find the alias first" step of a person profile: given a name it returns the
canonical entity(ies) plus ALL the aka / maiden / former names Wikidata records, the
occupation, and a few pivot-useful external IDs (an NPI links straight to `provider_license`).
Those alternate names are the REQUIRED re-run keys for the records/license searches — a
disciplinary action or court case is very often filed under a prior name, not the current one.

FREE and no-key, like CourtListener: the base URL is pinned from config and never
model-supplied; only the public name text goes out, with the descriptive `User-Agent`
Wikidata's API policy requires. Two surfaces answer the lookup — `wbsearchentities` (search
→ QIDs) then `Special:EntityData/<QID>.json` per hit (aliases + claims) — and one final
`wbgetentities` label call resolves the occupation/identifier QIDs to human labels in a single
batch, so a resolution costs `1 + N + 1` cheap GETs (N = entities surfaced, default ≤3). The
client returns `(entities, ok)` so an outage degrades (ok=False) rather than raising; it never
runs a tool policy itself — the handler does.
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
_DEFAULT_LIMIT = 3  # top entities surfaced per name — enough to disambiguate, cheap to fetch
_MAX_ALIASES = 12  # cap the alias list surfaced per entity (some public figures have dozens)
_UA = "JBrain2 research (jeff.m.hopkins@gmail.com)"

# Repeat-search cache, mirroring CourtListenerClient: a research fan re-queries the same name
# across rounds. A short in-process TTL folds those repeats. In-process + per-key — adequate at
# personal scale (one API process).
_CACHE_TTL_S = 900.0  # 15 min: long enough to fold a run's repeats, short enough to stay fresh.
_CACHE_MAX_ENTRIES = 256

# The occupation claim (P106). Its object QIDs are label-resolved in the batch call.
_OCCUPATION_PROP = "P106"
# Date of birth (P569) — a `time` claim (no QID to label-resolve). The records identity gate uses
# the YEAR to reject a court/criminal matter that predates the subject's adulthood (a common-name
# mix-up: a 1983 "United States v. Ingoglia" cannot be a person born in 1970). Year is enough — the
# gate needs an era, and Wikidata often records only the year anyway.
_BIRTH_DATE_PROP = "P569"
# Pivot-useful external identifiers worth surfacing — kept tight so the 200+ library/catalog
# IDs Wikidata carries don't drown the signal. NPI (P9450) links directly to `provider_license`.
# The property LABELS are resolved dynamically (never hardcoded), so this stays a plain id set.
_IDENTIFIER_PROPS = frozenset({"P9450"})


@dataclass(frozen=True)
class WikidataEntity:
    """One resolved entity: enough to disambiguate it and to seed the alias-driven re-runs."""

    qid: str
    label: str
    description: str
    aliases: tuple[str, ...]
    occupations: tuple[str, ...]
    identifiers: tuple[tuple[str, str], ...]  # (property label, value), e.g. ("NPI", "1234567890")
    url: str
    # Birth YEAR from P569, when Wikidata records it (else None) — the records identity gate's era
    # signal. Defaulted so existing constructors/fixtures that predate the field keep working.
    birth_year: int | None = None


class WikidataClient:
    """Resolve a name to its Wikidata entity(ies) over the pinned free API. `transport` is
    injectable so tests run against a mock with no network. An empty `base_url` disables the
    client (the handler reports "not configured")."""

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
        self._cache: TTLCache[tuple[str, int], list[WikidataEntity]] | None = (
            TTLCache(maxsize=_CACHE_MAX_ENTRIES, ttl=cache_ttl_s, timer=clock)
            if cache_ttl_s > 0
            else None
        )

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def resolve(
        self, name: str, *, limit: int = _DEFAULT_LIMIT
    ) -> tuple[list[WikidataEntity], bool]:
        """Search for `name`, fetch each top entity's aliases + claims, and resolve the
        occupation/identifier QIDs to labels. Returns (entities, ok); `ok` is False only on an
        outage so the tool tells a real "no entity" (ok=True, empty) apart from a source failure.
        Errors are logged and swallowed."""
        query = name.strip()
        if not self._base_url or not query:
            return [], bool(self._base_url)
        key = (query, limit)
        if self._cache is not None and (cached := self._cache.get(key)) is not None:
            return cached, True
        headers = {"Accept": "application/json", "User-Agent": _UA}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                hits = await self._search(client, query, limit, headers)
                # Fetch each entity's raw data (aliases + claims), keeping the QIDs we must
                # label-resolve so a single wbgetentities call covers them all.
                raw: list[tuple[dict, dict]] = []  # (search hit, entity claims/labels blob)
                to_label: set[str] = set()
                for hit in hits:
                    qid = str(hit.get("id") or "")
                    entity = await self._entity_data(client, qid, headers)
                    if entity is None:
                        continue
                    raw.append((hit, entity))
                    to_label.update(_referenced_qids(entity))
                labels = await self._labels(client, sorted(to_label), headers)
        except (httpx.HTTPError, ValueError) as exc:
            # Unreachable / timeout / non-2xx / malformed body: surface an outage flag, never
            # crash the turn — the tool degrades to "identity service is unavailable".
            log.warning("web.wikidata_failed", error=repr(exc))
            return [], False
        entities = [_build_entity(hit, entity, labels, self._base_url) for hit, entity in raw]
        if self._cache is not None and entities:
            self._cache[key] = entities
        return entities, True

    async def _search(
        self, client: httpx.AsyncClient, query: str, limit: int, headers: dict[str, str]
    ) -> list[dict]:
        resp = await client.get(
            f"{self._base_url}/w/api.php",
            params={
                "action": "wbsearchentities",
                "search": query,
                "language": "en",
                "format": "json",
                "limit": max(1, limit),
            },
            headers=headers,
        )
        resp.raise_for_status()
        body = resp.json()
        hits = body.get("search") if isinstance(body, dict) else None
        return [h for h in hits if isinstance(h, dict)][:limit] if isinstance(hits, list) else []

    async def _entity_data(
        self, client: httpx.AsyncClient, qid: str, headers: dict[str, str]
    ) -> dict | None:
        """The per-entity blob (labels/descriptions/aliases/claims) for one QID, or None."""
        if not qid:
            return None
        resp = await client.get(
            f"{self._base_url}/wiki/Special:EntityData/{qid}.json", headers=headers
        )
        resp.raise_for_status()
        body = resp.json()
        entities = body.get("entities") if isinstance(body, dict) else None
        entity = entities.get(qid) if isinstance(entities, dict) else None
        return entity if isinstance(entity, dict) else None

    async def _labels(
        self, client: httpx.AsyncClient, qids: list[str], headers: dict[str, str]
    ) -> dict[str, str]:
        """One batched `wbgetentities` call resolving the occupation + identifier-property QIDs
        to English labels. Empty input → no call."""
        if not qids:
            return {}
        resp = await client.get(
            f"{self._base_url}/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": "|".join(qids),
                "props": "labels",
                "languages": "en",
                "format": "json",
            },
            headers=headers,
        )
        resp.raise_for_status()
        body = resp.json()
        entities = body.get("entities") if isinstance(body, dict) else {}
        out: dict[str, str] = {}
        if isinstance(entities, dict):
            for qid, blob in entities.items():
                label = _en_value(blob.get("labels")) if isinstance(blob, dict) else ""
                if label:
                    out[qid] = label
        return out


def _en_value(section: object) -> str:
    """The English `value` from a Wikidata labels/descriptions section, or ""."""
    if isinstance(section, dict):
        en = section.get("en")
        if isinstance(en, dict):
            return str(en.get("value") or "")
    return ""


def _en_aliases(entity: dict) -> tuple[str, ...]:
    aliases = entity.get("aliases")
    en = aliases.get("en") if isinstance(aliases, dict) else None
    if not isinstance(en, list):
        return ()
    out = [str(a.get("value")).strip() for a in en if isinstance(a, dict) and a.get("value")]
    return tuple(out[:_MAX_ALIASES])


def _claim_qids(entity: dict, prop: str) -> list[str]:
    """The object QIDs of a wikibase-item claim (e.g. P106 occupation)."""
    claims = entity.get("claims")
    rows = claims.get(prop) if isinstance(claims, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for row in rows:
        snak = row.get("mainsnak") if isinstance(row, dict) else None
        if not isinstance(snak, dict) or snak.get("snaktype") != "value":
            continue
        value = (snak.get("datavalue") or {}).get("value")
        if isinstance(value, dict) and value.get("id"):
            out.append(str(value["id"]))
    return out


def _identifier_values(entity: dict) -> list[tuple[str, str]]:
    """The (property QID, value) pairs for the curated pivot-useful external IDs present."""
    claims = entity.get("claims")
    if not isinstance(claims, dict):
        return []
    out: list[tuple[str, str]] = []
    for prop in _IDENTIFIER_PROPS:
        for row in claims.get(prop) or []:
            snak = row.get("mainsnak") if isinstance(row, dict) else None
            if not isinstance(snak, dict) or snak.get("snaktype") != "value":
                continue
            value = (snak.get("datavalue") or {}).get("value")
            if isinstance(value, str) and value:
                out.append((prop, value))
    return out


def _birth_year(entity: dict) -> int | None:
    """The subject's birth YEAR from the P569 (date of birth) time claim, or None. The time value
    looks like `+1970-06-04T00:00:00Z` (or a negative/BCE lead); only the leading year is taken."""
    claims = entity.get("claims")
    rows = claims.get(_BIRTH_DATE_PROP) if isinstance(claims, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        snak = row.get("mainsnak") if isinstance(row, dict) else None
        if not isinstance(snak, dict) or snak.get("snaktype") != "value":
            continue
        value = (snak.get("datavalue") or {}).get("value")
        if isinstance(value, dict):
            m = re.match(r"^[+-]?(\d{1,4})-", str(value.get("time") or ""))
            if m:
                return int(m.group(1))
    return None


def _referenced_qids(entity: dict) -> set[str]:
    """Every QID whose label the entity needs resolved: its occupations + identifier props."""
    return set(_claim_qids(entity, _OCCUPATION_PROP)) | {p for p, _ in _identifier_values(entity)}


def _build_entity(hit: dict, entity: dict, labels: dict[str, str], base_url: str) -> WikidataEntity:
    qid = str(hit.get("id") or "")
    # Prefer the entity blob's label/description (canonical); fall back to the search hit.
    label = _en_value(entity.get("labels")) or str(hit.get("label") or "")
    description = _en_value(entity.get("descriptions")) or str(hit.get("description") or "")
    occupations = tuple(labels[q] for q in _claim_qids(entity, _OCCUPATION_PROP) if q in labels)
    identifiers = tuple(
        (labels.get(prop, prop), value) for prop, value in _identifier_values(entity)
    )
    return WikidataEntity(
        qid=qid,
        label=label,
        description=description,
        aliases=_en_aliases(entity),
        occupations=occupations,
        identifiers=identifiers,
        url=f"{base_url}/wiki/{qid}" if qid else "",
        birth_year=_birth_year(entity),
    )
