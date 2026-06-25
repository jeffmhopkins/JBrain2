"""The first read-only agent tools: `search` and `read_note`, thin handlers over
the existing search and notes services.

Each handler runs under the session's RLS scope (`ToolContext.session`), so a
narrowed session only ever sees in-scope data — the firewall is the services',
not the handler's. `build_registry` binds these handlers to their `.tool`
sidecars (docs/archive/ASSISTANT_PLAN.md P4.4c).
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.citygeocode import CityGeocoder
    from jbrain.geocode import NominatimReverseClient
    from jbrain.llm.router import LlmRouter
    from jbrain.settings_store import SqlSettingsStore

from jbrain.agent.appointmenttools import (
    build_appointment_handlers,
    build_appointment_write_handlers,
)
from jbrain.agent.archivisttools import build_archivist_memory_handlers
from jbrain.agent.clock import build_clock_handlers
from jbrain.agent.connectortools import build_connector_handlers
from jbrain.agent.contracts import EntityRef, NoteSource
from jbrain.agent.geocodetools import build_geocode_handlers
from jbrain.agent.listtools import build_list_handlers
from jbrain.agent.locationtools import build_location_handlers
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.memory import MemoryService
from jbrain.agent.memorytools import build_memory_handlers
from jbrain.agent.mergetools import build_merge_handlers
from jbrain.agent.metricstools import build_metrics_handlers
from jbrain.agent.presencetools import build_presence_handlers
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.proposaltools import build_proposal_handlers
from jbrain.agent.toolregistry import ToolRegistry, load_registry
from jbrain.analysis.relationships import predicate_candidates
from jbrain.appointments.service import AppointmentsRepo
from jbrain.connectors.base import ConnectorRegistry
from jbrain.db.session import SessionContext
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.lists.service import ListsRepo
from jbrain.locations import SqlLocationRepo
from jbrain.notes.service import NoteInfo, NotesRepo
from jbrain.search.service import (
    SearchResponse,
    SearchResult,
    SearchService,
    WikiSearchResult,
)

TOOLS_DIR = Path(__file__).parent / "tools"
_DEFAULT_LIMIT = 8

# The side-effecting image-gen sidecars (each produces a stored image): the `web`-class,
# expensive, jerv-only pair the on-box pipeline drives.
IMAGE_TOOL_NAMES = frozenset({"generate_image", "edit_image"})
# Every jerv-only image sidecar wired behind ComfyUI — the gen pair plus the read-only
# `analyze_image` (a vision read, not an image producer). All optional: dropped from the
# registry when no ComfyUI is configured (no handlers passed), so an unconfigured box
# silently lacks the feature.
OPTIONAL_IMAGE_TOOLS = IMAGE_TOOL_NAMES | frozenset({"analyze_image"})
# jerv's on-box audio transcription sidecar, dropped from the registry when the
# whisper gateway is unconfigured (graceful degrade, like the image tools).
OPTIONAL_TRANSCRIBE_TOOL = frozenset({"transcribe"})
# jerv's on-box video analysis sidecar, dropped from the registry when ffmpeg is
# absent (graceful degrade, like the image/whisper tools).
OPTIONAL_VIDEO_TOOL = frozenset({"analyze_video"})
# The archivist persona's Gmail sidecars (`web`-class, opt-in), dropped from the
# registry when Gmail is unconfigured — no refresh token, so no handlers are passed
# (graceful degrade, docs/EMAIL_ARCHIVIST_PLAN.md).
OPTIONAL_GMAIL_TOOLS = frozenset(
    {
        "gmail_search",
        "gmail_read",
        "gmail_list_labels",
        "gmail_create_label",
        "gmail_label",
        "gmail_archive",
        "gmail_count",
        "gmail_sender_breakdown",
        "gmail_bulk_label",
    }
)


class EntityReader(Protocol):
    """The slice of the analysis repo the read/entity tools need — the entity-page
    view behind read_entity, the name/alias search behind find_entity, the
    relationship traversal behind relate, and the note-currency overlay that tells
    the retrieval tools which of a note's facts are no longer live."""

    async def entity_view(self, ctx: SessionContext, entity_id: str) -> dict[str, Any] | None: ...

    async def list_entities(
        self,
        ctx: SessionContext,
        q: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...

    async def relate(
        self,
        ctx: SessionContext,
        anchor_id: str | None,
        predicates: Any,
        limit: int = 8,
    ) -> list[dict[str, Any]]: ...

    async def note_currency(
        self, ctx: SessionContext, note_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]: ...


def _search_flag(stale: list[dict[str, Any]]) -> str:
    """A compact currency flag under a hit whose note has non-live facts — so the
    agent knows BEFORE acting on the snippet, and where the current value lives."""
    statuses = sorted({f["status"].replace("_review", "") for f in stale})
    ids = sorted({f["entity_id"] for f in stale})
    return (
        f"  ⚠ {len(stale)} fact(s) here are no longer current ({', '.join(statuses)})"
        f" — read_entity {', '.join(ids)} for current values"
    )


def format_search(
    resp: SearchResponse, currency: dict[str, list[dict[str, Any]]] | None = None
) -> str:
    if not resp.results:
        return "No matching notes in scope."
    lines = ["(keyword-only search — semantic ranking unavailable)"] if resp.degraded else []
    for r in resp.results:
        if isinstance(r, WikiSearchResult):
            # A wiki article is the answer layer: surface it as a read_wiki target above notes.
            lines.append(f'- wiki "{r.title}" [{r.domain}]: {r.snippet.strip()}')
            continue
        line = f"- note {r.note_id} [{r.domain}] {r.created_at:%Y-%m-%d}: {r.snippet.strip()}"
        stale = (currency or {}).get(r.note_id)
        if stale:
            line += "\n" + _search_flag(stale)
        lines.append(line)
    return "\n".join(lines)


def _currency_address(f: dict[str, Any]) -> str:
    qualifier = f.get("qualifier")
    return f"{f['entity_name']}.{f['predicate']}" + (f".{qualifier}" if qualifier else "")


def _currency_line(f: dict[str, Any]) -> str:
    pointer = f" → read_entity {f['entity_id']} for the current value."
    if f["status"] == "superseded":
        current = f.get("current_value")
        now = f" Current value: {current}." if current else " No current value is recorded."
        return (
            f"- {_currency_address(f)}: SUPERSEDED — this note's value was replaced"
            f" by a newer note.{now}{pointer}"
        )
    if f["status"] == "retracted":
        return (
            f"- {_currency_address(f)}: RETRACTED — no longer asserted (an extraction"
            f" error or a correction)."
            f"{pointer}"
        )
    return (
        f"- {_currency_address(f)}: PENDING REVIEW — unverified, contested by the"
        f" review process."
        f"{pointer}"
    )


def format_currency(stale: list[dict[str, Any]]) -> str:
    """The currency overlay appended to a note's prose: which facts the note
    states are no longer the live value, and where the current value lives. The
    note above is the original record; the graph knows what has since changed, so
    the agent should prefer the current values (or read_entity to confirm)."""
    if not stale:
        return ""
    header = (
        "\n\n⚠ currency overlay (from the fact graph — the note text above is the"
        " original record, but these facts are no longer current; prefer the values"
        " below):"
    )
    return header + "\n" + "\n".join(_currency_line(f) for f in stale)


def format_note(note: NoteInfo) -> str:
    return f"note {note.id} [{note.domain}] {note.created_at:%Y-%m-%d}\n{note.body}"


def search_sources(resp: SearchResponse) -> tuple[NoteSource, ...]:
    """The structured twin of format_search: a note source per note hit for the UI's cards.
    Wiki hits are surfaced in the prose (read_wiki targets); a wiki source card is a later UI."""
    return tuple(
        NoteSource(note_id=r.note_id, domain=r.domain, snippet=r.snippet.strip())
        for r in resp.results
        if isinstance(r, SearchResult)
    )


def _note_snippet(body: str, limit: int = 140) -> str:
    """A one-line preview of a note's body for its source card."""
    line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return line[:limit]


def _edge_line(f: dict[str, Any]) -> str:
    """One current fact as an edge. A relationship edge names another entity —
    surface its id so the model can read_entity it and follow the relationship
    one hop further (the chain behind "my wife's name")."""
    base = f"- {f['predicate']}: {f['statement']}"
    if obj := f.get("object_entity_id"):
        name = f.get("object_entity_name") or ""
        return f"{base} → {name} (id={obj})"
    return base


def _current_facts(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [p["current"] for p in view.get("predicates", []) if p.get("current")]


def format_entity(view: dict[str, Any]) -> str:
    """The structured/graph view: schema.org kind, names, facts-as-edges (with the
    target entity's id on relationship edges, so they can be chained through),
    inbound edges, and a mention count. Text-only now; an entity_card view comes
    with the component registry (the text-first tool path, docs/archive/ASSISTANT_PLAN.md)."""
    lines = [f"{view['canonical_name']} [{view['kind']}] ({view['domain']})"]
    if aliases := view.get("aliases"):
        lines.append("also known as: " + ", ".join(aliases))
    if current := _current_facts(view):
        lines.append("facts:")
        lines += [_edge_line(f) for f in current]
    if inbound := view.get("inbound"):
        lines.append("referenced by:")
        lines += [f"- {r['name']} {r['predicate']} this" for r in inbound]
    if mentions := view.get("mentions"):
        lines.append(f"mentioned in {len(mentions)} note(s).")
    return "\n".join(lines)


def entity_view_objects(view: dict[str, Any]) -> tuple[EntityRef, ...]:
    """The entities this one points at via its relationship edges — tappable chips
    so the PWA linkifies a related name the agent mentions, and a structured twin
    of the ids `format_entity` prints for the model to chain on."""
    return tuple(
        EntityRef(
            entity_id=str(f["object_entity_id"]),
            label=str(f.get("object_entity_name") or f["object_entity_id"]),
            domain=f.get("object_entity_domain") or view.get("domain", "general"),
        )
        for f in _current_facts(view)
        if f.get("object_entity_id")
    )


def build_read_handlers(
    search: SearchService, notes: NotesRepo, entities: EntityReader
) -> dict[str, ToolHandler]:
    async def search_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolOutput("search needs a non-empty query.")
        limit = int(arguments.get("limit", _DEFAULT_LIMIT))
        resp = await search.search(ctx.session, query, None, limit)
        # Overlay the supersession/review outcome the snippet's prose can't show (note hits only).
        note_ids = list({r.note_id for r in resp.results if isinstance(r, SearchResult)})
        currency = await entities.note_currency(ctx.session, note_ids) if note_ids else {}
        return ToolOutput(format_search(resp, currency), search_sources(resp))

    async def read_note_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        note_id = str(arguments.get("note_id", "")).strip()
        if not note_id:
            return ToolOutput("read_note needs a note_id.")
        note = await notes.get_note(ctx.session, note_id)
        if note is None:
            return ToolOutput("No note with that id is in scope.")
        # The note body is the original record; the graph knows what has since
        # changed — append the currency overlay so the agent doesn't quote a value
        # a later note superseded or a correction retracted.
        currency = await entities.note_currency(ctx.session, [note.id])
        body = format_note(note) + format_currency(currency.get(note.id, []))
        source = NoteSource(note_id=note.id, domain=note.domain, snippet=_note_snippet(note.body))
        return ToolOutput(body, (source,))

    return {"search": search_tool, "read_note": read_note_tool}


_ENTITY_LIMIT = 8


def entity_refs(rows: list[dict[str, Any]]) -> tuple[EntityRef, ...]:
    """Map entity rows to refs for the response's tappable entity chips —
    carrying aliases so a name in the prose links even when it isn't the label."""
    return tuple(
        EntityRef(
            entity_id=str(r["id"]),
            label=str(r["canonical_name"]),
            domain=r["domain"],
            aliases=[str(a) for a in r.get("aliases", [])],
        )
        for r in rows
    )


def format_entities(rows: list[dict[str, Any]]) -> str:
    """The model-facing list — names + ids so it can chain into read_entity."""
    return "\n".join(
        f"- {r['canonical_name']} [{r['kind']}] ({r['domain']}) id={r['id']}" for r in rows
    )


def format_relations(rows: list[dict[str, Any]]) -> str:
    """The model-facing list for relate: which edge led to which entity, with ids
    to chain into read_entity (e.g. read the spouse for their name)."""
    return "\n".join(
        f"- {r['predicate']} → {r['canonical_name']} [{r['kind']}] ({r['domain']}) id={r['id']}"
        for r in rows
    )


def build_entity_handlers(entities: EntityReader) -> dict[str, ToolHandler]:
    async def read_entity_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        entity_id = str(arguments.get("entity_id", "")).strip()
        if not entity_id:
            return ToolOutput("read_entity needs an entity_id.")
        view = await entities.entity_view(ctx.session, entity_id)
        if view is None:
            return ToolOutput("No entity with that id is in scope.")
        return ToolOutput(format_entity(view), entities=entity_view_objects(view))

    async def find_entity_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        name = str(arguments.get("name", "")).strip()
        if not name:
            return ToolOutput("find_entity needs a name.")
        kind = str(arguments.get("kind", "")).strip() or None
        rows = (await entities.list_entities(ctx.session, name, kind, _ENTITY_LIMIT))[
            :_ENTITY_LIMIT
        ]
        if not rows:
            return ToolOutput(f"No entity matching '{name}' in scope.")
        return ToolOutput(format_entities(rows), entities=entity_refs(rows))

    async def relate_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        relationship = str(arguments.get("relationship", "")).strip()
        if not relationship:
            return ToolOutput("relate needs a relationship.")
        anchor = str(arguments.get("from", "")).strip() or None
        rows = await entities.relate(
            ctx.session, anchor, predicate_candidates(relationship), _ENTITY_LIMIT
        )
        if not rows:
            whose = "the owner" if anchor is None else "that entity"
            return ToolOutput(f"No '{relationship}' relationship for {whose} in scope.")
        return ToolOutput(format_relations(rows), entities=entity_refs(rows))

    return {
        "read_entity": read_entity_tool,
        "find_entity": find_entity_tool,
        "relate": relate_tool,
    }


class WikiReader(Protocol):
    async def get_article(self, ctx: SessionContext, article_id: str) -> dict[str, Any] | None:
        """The assembled article (lead + sections + references), RLS-scoped; None if not visible."""
        ...


def format_wiki_article(article: dict[str, Any]) -> str:
    """Render an article for the agent to discuss/cite: the lead + each section's prose, then the
    numbered References (the [n] markers in the prose index into them)."""
    lines = [f"# {article['title']}", str(article.get("subtitle", ""))]
    for para in article.get("lead", []):
        lines.append(str(para.get("text", "")))
    for section in article.get("sections", []):
        lines.append(f"\n## {section['heading']} [{section['domain']}]")
        for block in section.get("blocks", []):
            lines.append(str(block.get("text", "")))
        for sub in section.get("subsections", []):
            lines.append(f"### {sub['heading']}")
            for block in sub.get("blocks", []):
                lines.append(str(block.get("text", "")))
    refs = article.get("references", [])
    if refs:
        lines.append("\nReferences:")
        lines.extend(f"[{r['n']}] {r['meta']} — {r['snippet']}" for r in refs)
    return "\n".join(line for line in lines if line.strip())


def build_wiki_handlers(wiki: WikiReader) -> dict[str, ToolHandler]:
    """The read-only wiki-editorial tool: read a machine-written article (with its sources) so the
    agent can explain or discuss it in Talk. Read-only — the wiki is never edited directly; the
    write levers (correction note, source exclusion, rebuild) are separate."""

    async def read_wiki_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        article_id = str(arguments.get("article_id", "")).strip()
        if not article_id:
            return ToolOutput("read_wiki needs an article_id.")
        article = await wiki.get_article(ctx.session, article_id)
        if article is None:
            return ToolOutput("No wiki article with that id is in scope.")
        return ToolOutput(format_wiki_article(article))

    return {"read_wiki": read_wiki_tool}


def build_registry(
    search: SearchService,
    notes: NotesRepo,
    entities: EntityReader,
    memory: MemoryService,
    proposals: ProposalRepo,
    connectors: ConnectorRegistry,
    lists: ListsRepo,
    appointments: AppointmentsRepo,
    wiki: WikiReader,
    wiki_write: dict[str, ToolHandler],
    locations: SqlLocationRepo,
    devices: SqlDeviceRepo,
    web_handlers: dict[str, ToolHandler],
    city_geocoder: "CityGeocoder",
    maker: "async_sessionmaker[AsyncSession]",
    external_reverse: "NominatimReverseClient | None" = None,
    router: "LlmRouter | None" = None,
    settings: "SqlSettingsStore | None" = None,
    image_handlers: dict[str, ToolHandler] | None = None,
    transcribe_handlers: dict[str, ToolHandler] | None = None,
    video_handlers: dict[str, ToolHandler] | None = None,
    gmail_handlers: dict[str, ToolHandler] | None = None,
) -> ToolRegistry:
    """The agent's tool registry: every shipped sidecar bound to its handler — the
    read tools, the Tier-A memory tools, the list tools (which write the owner's
    own data directly), the appointment read tools (over the notes-derived
    projection), propose_correction and propose_merge (which stage a Proposal,
    never write), and the egress connector tools (which stage an egress Proposal,
    never call out).
    `image_handlers` is jerv's local image-gen tools, present only when a ComfyUI is
    configured; when absent the `generate_image`/`edit_image` sidecars are dropped
    (graceful degrade, docs/IMAGE_GEN_PLAN.md).
    Fails at startup if a sidecar and handler don't match exactly, so a new .tool
    can never ship unwired."""
    return load_registry(
        TOOLS_DIR,
        {
            **build_read_handlers(search, notes, entities),
            # A clock read — no owner data, no domain — so every agent that holds it
            # (the curator by default; jerv by allowlist) can ground time-relative talk.
            **build_clock_handlers(),
            **build_entity_handlers(entities),
            **build_list_handlers(lists),
            **build_appointment_handlers(appointments),
            **build_appointment_write_handlers(proposals, appointments),
            **build_memory_handlers(memory),
            **build_proposal_handlers(proposals),
            **build_merge_handlers(proposals, entities),
            **build_connector_handlers(connectors, proposals),
            **build_geocode_handlers(city_geocoder),
            **build_location_handlers(locations, devices, entities, proposals),
            # jerv's owner-approved, jerv-only location read (a `web`-gated, opt-in
            # tool, never offered to the curator). It names the live PWA fix the turn
            # carried via the offline city geocoder (no saved-place / device read),
            # escalating to the external geocoder only for a requested street address.
            **build_presence_handlers(city_geocoder, external_reverse),
            **build_wiki_handlers(wiki),
            # The owner-only host-telemetry read (query_server_metrics): RLS-gated
            # by the metrics tables' owner policy, so a non-owner session sees nothing.
            **build_metrics_handlers(maker),
            **wiki_write,
            # The jerv chatbot's internet tools (`web` permission), opt-in per agent.
            **web_handlers,
            # jerv's local image-gen tools (`web`-gated, on-box), present only when a
            # ComfyUI is configured; otherwise their sidecars are dropped below.
            **(image_handlers or {}),
            # jerv's local audio transcription (`web`-gated, on-box), present only when
            # the whisper gateway is configured; otherwise its sidecar is dropped below.
            **(transcribe_handlers or {}),
            # jerv's local video analysis (`web`-gated, on-box), present only when
            # ffmpeg is available; otherwise its sidecar is dropped below.
            **(video_handlers or {}),
            # The archivist persona's Gmail tools (`web`-gated), present only when a
            # Gmail refresh token is configured; otherwise their sidecars are dropped.
            **(gmail_handlers or {}),
            # The archivist's cross-session memory (`web`-gated, archivist-only) over
            # the owner-only `archivist_memory` table — always wired (the table always
            # exists); curator never sees it (the opt-in web class).
            **build_archivist_memory_handlers(maker),
        },
        optional=(
            OPTIONAL_IMAGE_TOOLS
            | OPTIONAL_TRANSCRIBE_TOOL
            | OPTIONAL_VIDEO_TOOL
            | OPTIONAL_GMAIL_TOOLS
        ),
    )
