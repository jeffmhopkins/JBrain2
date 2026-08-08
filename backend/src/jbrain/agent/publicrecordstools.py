"""jerv's `public_records` tool — free public-records search by name
(docs/reference/ASSISTANT.md "Agent selection").

v1 is CourtListener (opinions + RECAP dockets). Like `web_search`/`web_fetch` it runs
DIRECTLY (the `web` permission class, the bounded exception to invariant #9): jerv holds
no knowledge-base tools and no owner data, and the reach is a pinned public source, so no
personal context rides into the query. A hit is a LEAD, not a verdict — the handler's
prose (and the reply framing) tell the model to verify every match against the primary
document, because common names collide. Hits surface as `WebSource` chips so the owner
(and the agent, via `web_fetch`) can open each record.
"""

from __future__ import annotations

import structlog

from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.web.public_records import CourtListenerClient, Person, Record

log = structlog.get_logger()

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 25
_SOURCE_HEADER = (
    "CourtListener (free U.S. court records — opinions + RECAP dockets, rate-limited, by name)"
)


def _coerce_limit(raw: object) -> int:
    try:
        return max(1, min(int(raw), _MAX_LIMIT))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _format_record(record: Record) -> str:
    """One compact result line: "CaseName — Court (dateFiled) [docket] URL"."""
    parts = [record.case_name]
    if record.court:
        parts.append(f"— {record.court}")
    if record.date_filed:
        parts.append(f"({record.date_filed})")
    if record.docket_number:
        parts.append(f"[{record.docket_number}]")
    line = " ".join(parts)
    return f"- {line}\n  {record.url}" if record.url else f"- {line}"


def _format_person(person: Person) -> str:
    """One judge/official line, surfacing the alias link and positions."""
    lines = [f"- {person.name}"]
    if person.is_alias_of:
        lines[0] += "  (ALIAS record — links to a canonical record)"
        lines.append(f"  canonical: {person.is_alias_of}")
    if person.positions:
        suffix = (
            f" (+{person.position_count - len(person.positions)} more)"
            if (person.position_count > len(person.positions))
            else ""
        )
        lines.append("  positions: " + "; ".join(person.positions) + suffix)
    elif person.position_count:
        lines.append(f"  {person.position_count} position(s) on record")
    if person.url:
        lines.append(f"  {person.url}")
    return "\n".join(lines)


def build_public_records_handlers(
    client: CourtListenerClient, emit: BrainEmit | None = None
) -> dict[str, ToolHandler]:
    """`emit(kind, text)`, if given, fires a wall-display tendril when jerv reaches the
    records source — reusing the recognized `web_search` marker (this is a web reach)."""

    async def public_records_tool(arguments: dict, ctx: ToolContext) -> str:
        name = str(arguments.get("name", "")).strip()
        if not name:
            return "public_records needs a name to search."
        if not client.configured:
            return "Public-records search is not configured on this instance."
        limit = _coerce_limit(arguments.get("limit", _DEFAULT_LIMIT))
        if emit:
            emit("web_search", name)
        records, ok = await client.search(name, limit=limit)
        if not ok:
            return (
                f"The public-records source ({_SOURCE_HEADER}) is unavailable right now — try"
                f' again shortly, or web_search for court records under "{name}".'
            )
        # A supplement to the case search: the judges/officials DB, which links a person filed
        # under a prior name to their canonical record (best-effort — an outage here just omits
        # the section rather than failing the whole tool).
        people, _people_ok = await client.search_people(name)
        if not records and not people:
            return (
                f'Source: {_SOURCE_HEADER}.\nNo public court records for "{name}". Try a name'
                " VARIANT (a prior/maiden name, initials, or alternate spelling) — many records"
                " are filed under a former name."
            )
        sections = [f"Source: {_SOURCE_HEADER}."]
        if records:
            lines = "\n".join(_format_record(r) for r in records)
            sections.append(f'{len(records)} record(s) for "{name}":\n{lines}')
        if people:
            plines = "\n".join(_format_person(p) for p in people)
            sections.append(
                f'{len(people)} judge/official record(s) for "{name}" (alias links + positions'
                f"):\n{plines}"
            )
        sections.append(
            "These are LEADS, not confirmed facts: a common name can collide with a different"
            " person, so VERIFY each hit belongs to this individual against the primary"
            " document (open the URL with web_fetch) before relying on it. An ALIAS record links"
            " a prior name to a canonical one — follow it and re-run under that name. Run this"
            " once per name variant (including any prior/maiden name)."
        )
        body = "\n".join(sections)
        sources = tuple(WebSource(url=r.url, title=r.case_name) for r in records if r.url) + tuple(
            WebSource(url=p.url, title=p.name) for p in people if p.url
        )
        return ToolOutput(body, web_sources=sources)

    return {"public_records": public_records_tool}
