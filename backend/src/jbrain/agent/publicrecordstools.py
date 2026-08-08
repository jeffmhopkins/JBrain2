"""jerv's `public_records` umbrella — free, keyless public-records lookups by name
(docs/reference/ASSISTANT.md "Agent selection", docs/plans/TOOL_CATALOG_PLAN.md).

ONE tool, `public_records(name, sources=[…])`, fans a name across four keyless sources —
`identity` (Wikidata: canonical name + aliases/maiden/former names + occupation), `court`
(CourtListener: opinions + RECAP dockets + a judges/officials alias lookup), `license` (NPPES
NPI registry: a clinician's license number/state/specialty + other_names), and
`federal_register` (agency debarments + enforcement notices). Collapsed from four flat tools
after a tool-selection probe confirmed gpt-oss-120b fills `name`/`sources` reliably (including
narrowing to a single source).

Each source runs DIRECTLY (the `web` permission class, the bounded exception to invariant #9):
jerv holds no knowledge-base tools and no owner data, and each reach is a pinned public source,
so no personal context rides into the query. A hit is a LEAD, not a verdict — verify each match
against the primary document (common names collide), and an alias one source reveals is a
REQUIRED re-search key in the others. Hits surface as `WebSource` chips so the owner (and the
agent, via `web_fetch`) can open each record.
"""

from __future__ import annotations

import structlog

from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.contracts import WebSource
from jbrain.agent.federalregistertools import build_federal_register_handlers
from jbrain.agent.identitytools import build_resolve_identity_handlers
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.providerlicensetools import build_provider_license_handlers
from jbrain.web.federal_register import FederalRegisterClient
from jbrain.web.nppes import NppesClient
from jbrain.web.public_records import CourtListenerClient, Person, Record
from jbrain.web.wikidata import WikidataClient

log = structlog.get_logger()

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 25
_SOURCE_HEADER = (
    "CourtListener (free U.S. court records — opinions + RECAP dockets, rate-limited, by name)"
)

# The canonical source order (identity FIRST so an alias it finds is available to re-run the
# others under) + the human label for each section header.
_SOURCE_ORDER = ("identity", "court", "license", "federal_register")
_SOURCE_LABEL = {
    "identity": "Identity & aliases (Wikidata)",
    "court": "Court records (CourtListener)",
    "license": "Provider license (NPPES/NPI)",
    "federal_register": "Federal Register",
}


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


def _build_court_handler(client: CourtListenerClient, emit: BrainEmit | None) -> ToolHandler:
    """The `court` source: CourtListener opinions + RECAP dockets + the judges/officials alias
    lookup. Kept as an internal section handler behind the umbrella (was the flat public_records
    tool). Reads `name`/`limit` like the others."""

    async def court(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        name = str(arguments.get("name", "")).strip()
        if not name:
            return "the court source needs a name to search."
        if not client.configured:
            return "Public-records (court) search is not configured on this instance."
        limit = _coerce_limit(arguments.get("limit", _DEFAULT_LIMIT))
        if emit:
            emit("web_search", name)
        records, ok = await client.search(name, limit=limit)
        if not ok:
            return (
                f"The court source ({_SOURCE_HEADER}) is unavailable right now — try again"
                f' shortly, or web_search for court records under "{name}".'
            )
        # A supplement to the case search: the judges/officials DB, which links a person filed
        # under a prior name to their canonical record (best-effort — an outage here just omits
        # the section rather than failing the source).
        people, _people_ok = await client.search_people(name)
        if not records and not people:
            return (
                f'No public court records for "{name}". Try a name VARIANT (a prior/maiden name,'
                " initials, or alternate spelling) — many records are filed under a former name."
            )
        sections = []
        if records:
            lines = "\n".join(_format_record(r) for r in records)
            sections.append(f'{len(records)} record(s) for "{name}":\n{lines}')
        if people:
            plines = "\n".join(_format_person(p) for p in people)
            sections.append(
                f'{len(people)} judge/official record(s) for "{name}" (alias links + positions'
                f"):\n{plines}"
            )
        web = tuple(WebSource(url=r.url, title=r.case_name) for r in records if r.url) + tuple(
            WebSource(url=p.url, title=p.name) for p in people if p.url
        )
        return ToolOutput("\n".join(sections), web_sources=web)

    return court


def build_public_records_handlers(
    courtlistener: CourtListenerClient,
    wikidata: WikidataClient,
    nppes: NppesClient,
    federal_register: FederalRegisterClient,
    emit: BrainEmit | None = None,
) -> dict[str, ToolHandler]:
    """The umbrella: fan a name across the requested keyless sources. `emit(kind, text)`, if
    given, fires a wall-display tendril per source reached (the recognized `web_search` marker —
    each is a web reach)."""

    # Each source's section handler + the argument key it reads the name under.
    sources: dict[str, tuple[ToolHandler, str]] = {
        "identity": (build_resolve_identity_handlers(wikidata, emit)["resolve_identity"], "name"),
        "court": (_build_court_handler(courtlistener, emit), "name"),
        "license": (build_provider_license_handlers(nppes, emit)["provider_license"], "name"),
        "federal_register": (
            build_federal_register_handlers(federal_register, emit)["federal_register"],
            "query",
        ),
    }

    async def public_records_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        name = str(arguments.get("name", "")).strip()
        if not name:
            return (
                "public_records needs a name. Optionally pass sources= any of identity, court,"
                " license, federal_register (default: all four)."
            )
        raw = arguments.get("sources")
        if isinstance(raw, str):
            raw = [raw]
        chosen = {str(s).strip().lower() for s in raw} if raw else set(_SOURCE_ORDER)
        wanted = [s for s in _SOURCE_ORDER if s in chosen] or list(_SOURCE_ORDER)
        parts: list[str] = []
        web: list[WebSource] = []
        for s in wanted:
            fn, arg_key = sources[s]
            sub: dict = {arg_key: name}
            if s == "license" and str(arguments.get("state", "")).strip():
                sub["state"] = arguments["state"]
            if arguments.get("limit"):
                sub["limit"] = arguments["limit"]
            res = await fn(sub, ctx)
            parts.append(f"══ {_SOURCE_LABEL[s]} ══\n{res}")
            if isinstance(res, ToolOutput):
                web.extend(res.web_sources)
        header = (
            f'Public-records sweep for "{name}" — sources: {", ".join(wanted)}. Each hit is a'
            " LEAD to verify against its primary document (common names collide); an alias found"
            " in one source (a prior/maiden name) is a REQUIRED re-search key — re-run"
            " public_records under EACH such name."
        )
        return ToolOutput(header + "\n\n" + "\n\n".join(parts), web_sources=tuple(web))

    return {"public_records": public_records_tool}
