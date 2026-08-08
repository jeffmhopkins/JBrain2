"""jerv's `provider_license` tool — NPPES / NPI registry lookup
(docs/reference/ASSISTANT.md "Agent selection").

The national-registry replacement for scraping a single state DOH portal: given a clinician's
name it returns the NPI, credential, per-state license number + specialty, and — the pivot —
any `other_names` (a prior/maiden name). The handler frames it plainly: this is a REGISTRY, not
a discipline database, so a hit gives you the identifier and the alias to take to the RIGHT
state board, never a clean/dirty verdict.

Like `web_search` it runs DIRECTLY (the `web` permission class, the bounded exception to
invariant #9): jerv holds no knowledge-base tools and no owner data, and the reach is a pinned
public source. Matches surface as `WebSource` chips pointing at each NPI's public profile.
"""

from __future__ import annotations

import structlog

from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.web.nppes import NppesClient, Provider

log = structlog.get_logger()

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 25
_SOURCE_HEADER = "NPPES NPI Registry (free U.S. national provider registry — by name)"
# The public NPI profile page — a stable, keyless URL keyed on the number, so a match is openable.
_PROFILE_URL = "https://npiregistry.cms.hhs.gov/provider-view/{npi}"


def _coerce_limit(raw: object) -> int:
    try:
        return max(1, min(int(raw), _MAX_LIMIT))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _format_licenses(provider: Provider) -> str:
    """The per-state license lines: "NC #12345 — Internal Medicine (primary)"."""
    lines = []
    for tax in provider.taxonomies:
        parts = []
        if tax.state or tax.license:
            parts.append(f"{tax.state} #{tax.license}".strip())
        if tax.desc:
            parts.append(f"— {tax.desc}")
        if tax.primary:
            parts.append("(primary)")
        if parts:
            lines.append("    " + " ".join(parts))
    return "\n".join(lines)


def _format_provider(provider: Provider) -> str:
    head = provider.name
    if provider.credential:
        head += f", {provider.credential}"
    bits = [f"NPI {provider.npi}"] if provider.npi else []
    if provider.status:
        bits.append(provider.status)
    where = ", ".join(p for p in (provider.city, provider.state) if p)
    if where:
        bits.append(where)
    lines = [f"- {head}" + (f" [{'; '.join(bits)}]" if bits else "")]
    if provider.other_names:
        # The whole point: surface the aliases prominently as re-search keys.
        lines.append("  other names (RE-SEARCH under these): " + "; ".join(provider.other_names))
    licenses = _format_licenses(provider)
    if licenses:
        lines.append("  licenses:\n" + licenses)
    return "\n".join(lines)


def build_provider_license_handlers(
    client: NppesClient, emit: BrainEmit | None = None
) -> dict[str, ToolHandler]:
    """`emit(kind, text)`, if given, fires a wall-display tendril when jerv reaches NPPES —
    reusing the recognized `web_search` marker (this is a web reach)."""

    async def provider_license_tool(arguments: dict, ctx: ToolContext) -> str:
        name = str(arguments.get("name", "")).strip()
        if not name:
            return "provider_license needs a name to search."
        if not client.configured:
            return "Provider-license lookup is not configured on this instance."
        state = str(arguments.get("state", "")).strip()
        limit = _coerce_limit(arguments.get("limit", _DEFAULT_LIMIT))
        if emit:
            emit("web_search", name)
        providers, ok = await client.search(name, state=state, limit=limit)
        if not ok:
            return (
                f"The provider registry ({_SOURCE_HEADER}) is unavailable right now — try again"
                f' shortly, or web_search for "{name}" + the state board.'
            )
        if not providers:
            scope = f" in {state.upper()}" if state else ""
            return (
                f'Source: {_SOURCE_HEADER}.\nNo provider registered under "{name}"{scope}. Try a'
                " name VARIANT (a prior/maiden name) or drop the state — the NPI may be filed"
                " under a former name or a different state."
            )
        blocks = "\n".join(_format_provider(p) for p in providers)
        body = (
            f"Source: {_SOURCE_HEADER}.\n"
            f'{len(providers)} provider(s) for "{name}":\n{blocks}\n\n'
            "This is the national REGISTRY: it gives the license number, state, and specialty and"
            " any other_names (a prior/maiden name) — the pivot to the RIGHT state board. It does"
            " NOT include disciplinary actions, so a hit is an identifier, not a clean record;"
            " take the license number + state (and each other_name) to that state's board to"
            " check for discipline. Common names collide — confirm the match is THIS person."
        )
        sources = tuple(
            WebSource(url=_PROFILE_URL.format(npi=p.npi), title=p.name) for p in providers if p.npi
        )
        return ToolOutput(body, web_sources=sources)

    return {"provider_license": provider_license_tool}
