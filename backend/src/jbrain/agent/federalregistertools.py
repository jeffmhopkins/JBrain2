"""jerv's `federal_register` tool — federal rules/notices search
(docs/reference/ASSISTANT.md "Agent selection").

Searches the Federal Register by term for federal rules and notices, including FDA/agency
DEBARMENTS and enforcement actions — a debarment or enforcement notice naming a person or
company (search a name + "debarment") that web-only search misses. A hit is a LEAD to verify
against the notice itself, not a verdict.

Like `web_search` it runs DIRECTLY (the `web` permission class, the bounded exception to
invariant #9): jerv holds no knowledge-base tools and no owner data, and the reach is a pinned
public source. Notices surface as `WebSource` chips so the owner (and web_fetch) can open each.
"""

from __future__ import annotations

import structlog

from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.web.federal_register import FederalRegisterClient, Notice

log = structlog.get_logger()

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 25
_MAX_EXCERPT_CHARS = 300
_SOURCE_HEADER = "Federal Register (free U.S. federal rules & notices — incl. agency debarments)"


def _coerce_limit(raw: object) -> int:
    try:
        return max(1, min(int(raw), _MAX_LIMIT))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _format_notice(notice: Notice) -> str:
    parts = [notice.title]
    if notice.agencies:
        parts.append("— " + ", ".join(notice.agencies))
    if notice.publication_date:
        parts.append(f"({notice.publication_date})")
    lines = ["- " + " ".join(parts)]
    if notice.excerpt:
        excerpt = notice.excerpt[:_MAX_EXCERPT_CHARS]
        lines.append(f"  “…{excerpt}…”")
    if notice.url:
        lines.append(f"  {notice.url}")
    return "\n".join(lines)


def build_federal_register_handlers(
    client: FederalRegisterClient, emit: BrainEmit | None = None
) -> dict[str, ToolHandler]:
    """`emit(kind, text)`, if given, fires a wall-display tendril when jerv reaches the Federal
    Register — reusing the recognized `web_search` marker (this is a web reach)."""

    async def federal_register_tool(arguments: dict, ctx: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "federal_register needs a query (a name, company, or term)."
        if not client.configured:
            return "Federal Register search is not configured on this instance."
        limit = _coerce_limit(arguments.get("limit", _DEFAULT_LIMIT))
        if emit:
            emit("web_search", query)
        notices, ok = await client.search(query, limit=limit)
        if not ok:
            return (
                f"The Federal Register ({_SOURCE_HEADER}) is unavailable right now — try again"
                f' shortly, or web_search for "{query}".'
            )
        if not notices:
            return (
                f'Source: {_SOURCE_HEADER}.\nNo Federal Register documents for "{query}". Try a'
                ' name VARIANT, a company name, or a broader term (e.g. a surname + "debarment").'
            )
        blocks = "\n".join(_format_notice(n) for n in notices)
        body = (
            f"Source: {_SOURCE_HEADER}.\n"
            f'{len(notices)} document(s) for "{query}" (newest first):\n{blocks}\n\n'
            "These are federal rules/notices (incl. FDA and agency debarments/enforcement). Each"
            " is a LEAD — open the notice with web_fetch and confirm it names THIS person/entity"
            " before relying on it. Run once per name variant."
        )
        sources = tuple(WebSource(url=n.url, title=n.title) for n in notices if n.url)
        return ToolOutput(body, web_sources=sources)

    return {"federal_register": federal_register_tool}
