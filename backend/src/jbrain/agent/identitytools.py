"""jerv's `resolve_identity` tool — Wikidata alias/disambiguation lookup
(docs/reference/ASSISTANT.md "Agent selection").

The "find the alias first" step of a person profile: resolve a name to its canonical
entity(ies) and harvest ALL the alternate names (aka / maiden / former) Wikidata records, plus
occupation and a few pivot-useful external IDs. Those names are the re-run keys — the handler's
prose tells the model to re-run `provider_license` / `public_records` / `federal_register` under
EACH variant, because a record is often filed under a prior name.

Like `web_search` it runs DIRECTLY (the `web` permission class, the bounded exception to
invariant #9): jerv holds no knowledge-base tools and no owner data, and the reach is a pinned
public source. Entities surface as `WebSource` chips so the owner can open each Wikidata page.
"""

from __future__ import annotations

import structlog

from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.web.wikidata import WikidataClient, WikidataEntity

log = structlog.get_logger()

_DEFAULT_LIMIT = 3
_MAX_LIMIT = 5
_SOURCE_HEADER = "Wikidata (free identity registry — canonical name + aliases + occupation)"


def _coerce_limit(raw: object) -> int:
    try:
        return max(1, min(int(raw), _MAX_LIMIT))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _format_entity(entity: WikidataEntity) -> str:
    """One entity block: label + description, then aliases, occupation, and any external IDs."""
    head = entity.label or entity.qid
    if entity.description:
        head += f" — {entity.description}"
    lines = [f"- {head}  [{entity.qid}]"]
    if entity.aliases:
        lines.append("  also known as: " + "; ".join(entity.aliases))
    if entity.occupations:
        lines.append("  occupation: " + ", ".join(entity.occupations))
    if entity.identifiers:
        lines.append(
            "  IDs: " + ", ".join(f"{label} {value}" for label, value in entity.identifiers)
        )
    if entity.url:
        lines.append(f"  {entity.url}")
    return "\n".join(lines)


def build_resolve_identity_handlers(
    client: WikidataClient, emit: BrainEmit | None = None
) -> dict[str, ToolHandler]:
    """`emit(kind, text)`, if given, fires a wall-display tendril when jerv reaches Wikidata —
    reusing the recognized `web_search` marker (this is a web reach)."""

    async def resolve_identity_tool(arguments: dict, ctx: ToolContext) -> str:
        name = str(arguments.get("name", "")).strip()
        if not name:
            return "resolve_identity needs a name to look up."
        if not client.configured:
            return "Identity resolution is not configured on this instance."
        limit = _coerce_limit(arguments.get("limit", _DEFAULT_LIMIT))
        if emit:
            emit("web_search", name)
        entities, ok = await client.resolve(name, limit=limit)
        if not ok:
            return (
                f"The identity source ({_SOURCE_HEADER}) is unavailable right now — try again"
                f' shortly, or web_search for "{name}".'
            )
        if not entities:
            return (
                f'Source: {_SOURCE_HEADER}.\nNo Wikidata entity for "{name}". Not everyone has a'
                " Wikidata page — fall back to the FEC committee name, an official bio, or news to"
                " harvest a prior/maiden name, then re-run the records searches under each."
            )
        blocks = "\n".join(_format_entity(e) for e in entities)
        body = (
            f"Source: {_SOURCE_HEADER}.\n"
            f'{len(entities)} entity match(es) for "{name}":\n{blocks}\n\n'
            "Pick the entity that matches this person (the description/occupation disambiguates),"
            " then RE-RUN public_records under EACH alternate name above — its license source (for"
            " a clinician), court source, and federal_register source — because many records are"
            " filed under a prior or maiden name. An external NPI feeds the license source"
            " directly."
        )
        sources = tuple(WebSource(url=e.url, title=e.label or e.qid) for e in entities if e.url)
        return ToolOutput(body, web_sources=sources)

    return {"resolve_identity": resolve_identity_tool}
