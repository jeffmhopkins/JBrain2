"""jerv's `portal_search` tool — actually QUERY a dynamic government search portal by name
(DYNAMIC_PORTAL_FETCH_PLAN.md, "dinosaurs").

ONE `web`-permission tool, `portal_search(name, jurisdiction, kind=…)`, dispatches to a pinned
per-portal resolver (FL Sunbiz corp search, FL DFS licensee lookup) that issues the portal's real
result request through the shared SSRF-guarded `WebFetcher`, parses the rows, and returns each with
a STATIC detail URL surfaced as a `WebSource` — so it flows into the deep-research `[^n]` citation
registry like any fetched page. This is the capability half of the portal work; `web_fetch`'s
"search form, not results" detector is the honesty half for portals with no adapter.

Complements, not duplicates, `public_records(sources=["license"])`: that fans the NATIONAL,
keyless registries (NPPES/NPI, CourtListener, Wikidata, Federal Register); `portal_search` reaches
a specific STATE portal's own search. Reach for `portal_search` when you need a state business
registry or a state license lookup; `public_records` for the national ones.

A hit is a LEAD to verify against its primary detail page (common names collide), never a verdict.
"""

from __future__ import annotations

import structlog

from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.web.fetch import WebFetcher
from jbrain.web.portals.base import (
    PortalResolver,
    PortalRow,
    advertised_capabilities,
    select_resolvers,
)

log = structlog.get_logger()

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 25


def _coerce_limit(raw: object) -> int:
    try:
        return max(1, min(int(raw), _MAX_LIMIT))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _format_row(row: PortalRow) -> str:
    """One compact result line: "Name — subtitle\\n  detail_url"."""
    head = row.title if not row.subtitle else f"{row.title} — {row.subtitle}"
    return f"- {head}\n  {row.detail_url}" if row.detail_url else f"- {head}"


def build_portal_handlers(
    fetcher: WebFetcher,
    resolvers: tuple[PortalResolver, ...],
    emit: BrainEmit | None = None,
) -> dict[str, ToolHandler]:
    """The `portal_search` handler over the given resolvers. Always registered (an empty/all-
    unconfigured registry just reports "not configured"), so the `.tool` sidecar always has its
    handler. `emit(kind, text)`, if given, fires a `web_search` wall tendril per query."""

    async def portal_search_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        name = str(arguments.get("name", "")).strip()
        caps = advertised_capabilities(resolvers)
        if not name:
            return (
                "portal_search needs a name to look up. Pass jurisdiction + kind (available: "
                f"{caps}) or an explicit key. Example: "
                'portal_search(name="Acme Corp", jurisdiction="FL", kind="business").'
            )
        jurisdiction = str(arguments.get("jurisdiction", "")).strip()
        kind = str(arguments.get("kind", "")).strip()
        key = str(arguments.get("key", "")).strip()
        if not (jurisdiction or kind or key):
            return (
                "portal_search needs a jurisdiction + kind (or an explicit key) to pick a portal. "
                f"Available: {caps}. Example: "
                'portal_search(name="Acme Corp", jurisdiction="FL", kind="business").'
            )
        chosen = select_resolvers(resolvers, jurisdiction=jurisdiction, kind=kind, key=key)
        configured = tuple(r for r in chosen if r.configured)
        if not configured:
            # Steer, never silently widen — mirror public_records rejecting a bad `sources`.
            if chosen:
                return (
                    f"that portal is not configured on this instance. Available: {caps}. "
                    "Try web_search for the record instead."
                )
            return (
                f"no portal matches jurisdiction={jurisdiction!r} kind={kind!r} key={key!r}. "
                f"Available: {caps}. Use one of those, or web_search for the record."
            )
        limit = _coerce_limit(arguments.get("limit", _DEFAULT_LIMIT))
        parts: list[str] = []
        web: list[WebSource] = []
        for resolver in configured:
            if emit:
                emit("web_search", name)
            try:
                result = await resolver.search(fetcher, name, limit=limit)
            except Exception as exc:  # noqa: BLE001 - degrade per resolver, never fail the sweep
                log.warning("portal_search.resolver_error", key=resolver.key, error=str(exc))
                parts.append(
                    f"══ {resolver.label} ══\n(this portal errored — try again shortly, or "
                    "web_search for the record)"
                )
                continue
            if not result.ok:
                parts.append(
                    f"══ {resolver.label} ══\n{result.note or 'portal unavailable right now'} — "
                    "this is NOT evidence the record is absent; try again shortly or web_search "
                    "for it."
                )
                continue
            if not result.rows:
                parts.append(
                    f'══ {resolver.label} ══\nsearched, no match under "{name}" — try a name '
                    "VARIANT (a prior/DBA/alternate name). Not found here is not proof no record "
                    "exists."
                )
                continue
            note = f" ({result.note})" if result.note else ""
            lines = "\n".join(_format_row(r) for r in result.rows)
            parts.append(f"══ {resolver.label} ══\n{len(result.rows)} result(s){note}:\n{lines}")
            web.extend(
                WebSource(url=r.detail_url, title=r.title, read=False)
                for r in result.rows
                if r.detail_url
            )
        header = (
            f'Portal search for "{name}". Each hit is a LEAD — open its detail URL with web_fetch '
            "to verify it is the right subject (common names collide) before relying on it."
        )
        return ToolOutput(header + "\n\n" + "\n\n".join(parts), web_sources=tuple(web))

    return {"portal_search": portal_search_tool}
