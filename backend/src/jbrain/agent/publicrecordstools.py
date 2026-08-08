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
from jbrain.web.public_records import CourtListenerClient, Record

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
        if not records:
            return (
                f'Source: {_SOURCE_HEADER}.\nNo public court records for "{name}". Try a name'
                " VARIANT (a prior/maiden name, initials, or alternate spelling) — many records"
                " are filed under a former name."
            )
        lines = "\n".join(_format_record(r) for r in records)
        body = (
            f"Source: {_SOURCE_HEADER}.\n"
            f'{len(records)} record(s) for "{name}":\n{lines}\n\n'
            "These are LEADS, not confirmed facts: a common name can collide with a different"
            " person, so VERIFY each hit belongs to this individual against the primary"
            " document (open the URL with web_fetch) before relying on it. Run this once per"
            " name variant (including any prior/maiden name)."
        )
        sources = tuple(WebSource(url=r.url, title=r.case_name) for r in records if r.url)
        return ToolOutput(body, web_sources=sources)

    return {"public_records": public_records_tool}
