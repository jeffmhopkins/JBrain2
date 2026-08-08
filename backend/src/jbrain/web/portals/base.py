"""Portal resolvers — adapters that actually QUERY a dynamic government search portal
(DYNAMIC_PORTAL_FETCH_PLAN.md, "dinosaurs").

`web_fetch` can only see a dynamic portal's empty SEARCH FORM — it can't run the form's
client-side / POST query — so a name lookup on FL Sunbiz / FL DFS came back as the form and read
as "no record". A resolver closes that gap: given a name + jurisdiction it issues the portal's
*real* result request through the shared `WebFetcher` egress (so the same per-hop SSRF guard, byte
cap, and browser posture apply — a resolver never opens its own outbound leg), parses the result
rows, and returns each as a `PortalRow` carrying a STATIC, fetchable `detail_url` the research
agent then `web_fetch`es and cites. A hit is a LEAD to verify against that detail page (common
names collide), never a verdict — same discipline as `public_records`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from jbrain.web.fetch import WebFetcher


@dataclass(frozen=True)
class PortalRow:
    """One result row from a portal search — a LEAD with a fetchable detail page."""

    title: str  # entity / person name as the portal reports it
    subtitle: str = ""  # status, doc/license number, filing date … (a compact one-liner)
    detail_url: str = ""  # the STATIC, fetchable detail page — the citation target
    identifiers: tuple[tuple[str, str], ...] = ()  # (label, value): doc no., license no., …


@dataclass(frozen=True)
class PortalResult:
    """A resolver's return. `ok` is False ONLY on a source outage / parse failure — NOT on a
    real empty result (ok=True, rows=()), so the tool tells "searched, none here" apart from
    "the portal is down", and neither ever reads as "no record exists"."""

    rows: tuple[PortalRow, ...]
    ok: bool
    note: str = ""  # e.g. "results truncated at N", "portal returned a challenge page"


@runtime_checkable
class PortalResolver(Protocol):
    """One portal adapter. Pure over `(WebFetcher, name)` → `PortalResult`: it holds no network
    of its own (every byte rides the injected `WebFetcher`), so it is trivially unit-testable
    with the same injected `httpx` transport the other web clients use."""

    key: str  # dispatch key, e.g. "fl_business" / "fl_license"
    jurisdiction: str  # "FL"
    kind: str  # "business" | "license"
    label: str  # human header, e.g. "Florida Sunbiz corporation search"

    @property
    def configured(self) -> bool: ...

    async def search(self, fetcher: WebFetcher, name: str, *, limit: int) -> PortalResult: ...


def select_resolvers(
    resolvers: tuple[PortalResolver, ...],
    *,
    jurisdiction: str = "",
    kind: str = "",
    key: str = "",
) -> tuple[PortalResolver, ...]:
    """Pick the resolver(s) a `portal_search` call targets. An explicit `key` selects exactly one;
    otherwise filter by `jurisdiction` and/or `kind` (each case-insensitive, empty = any). Returns
    them in registry order; empty when nothing matches (the tool steers, never silently widens)."""
    if key:
        k = key.strip().lower()
        return tuple(r for r in resolvers if r.key.lower() == k)
    j, kd = jurisdiction.strip().lower(), kind.strip().lower()
    return tuple(
        r
        for r in resolvers
        if (not j or r.jurisdiction.lower() == j) and (not kd or r.kind.lower() == kd)
    )


def advertised_capabilities(resolvers: tuple[PortalResolver, ...]) -> str:
    """A compact human list of the `(jurisdiction, kind)` pairs the configured resolvers cover —
    surfaced in the tool's steering message so the model discovers what it can actually resolve.
    Only configured resolvers are advertised (an unconfigured one can't answer)."""
    pairs = sorted({(r.jurisdiction, r.kind) for r in resolvers if r.configured})
    return ", ".join(f"{j}/{k}" for j, k in pairs) if pairs else "(none configured)"
