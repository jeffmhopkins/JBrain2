"""The first read-only agent tools: `search` and `read_note`, thin handlers over
the existing search and notes services.

Each handler runs under the session's RLS scope (`ToolContext.session`), so a
narrowed session only ever sees in-scope data — the firewall is the services',
not the handler's. `build_registry` binds these handlers to their `.tool`
sidecars (docs/ASSISTANT_PLAN.md P4.4c).
"""

from pathlib import Path
from typing import Any, Protocol

from jbrain.agent.connectortools import build_connector_handlers
from jbrain.agent.contracts import EntityRef, NoteSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.memory import MemoryService
from jbrain.agent.memorytools import build_memory_handlers
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.proposaltools import build_proposal_handlers
from jbrain.agent.toolregistry import ToolRegistry, load_registry
from jbrain.connectors.base import ConnectorRegistry
from jbrain.db.session import SessionContext
from jbrain.notes.service import NoteInfo, NotesRepo
from jbrain.search.service import SearchResponse, SearchService

TOOLS_DIR = Path(__file__).parent / "tools"
_DEFAULT_LIMIT = 8


class EntityReader(Protocol):
    """The slice of the analysis repo the entity tools need — the entity-page view
    and the name/alias search behind find_entity."""

    async def entity_view(self, ctx: SessionContext, entity_id: str) -> dict[str, Any] | None: ...

    async def list_entities(
        self,
        ctx: SessionContext,
        q: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...


def format_search(resp: SearchResponse) -> str:
    if not resp.results:
        return "No matching notes in scope."
    lines = ["(keyword-only search — semantic ranking unavailable)"] if resp.degraded else []
    lines += [
        f"- note {r.note_id} [{r.domain}] {r.created_at:%Y-%m-%d}: {r.snippet.strip()}"
        for r in resp.results
    ]
    return "\n".join(lines)


def format_note(note: NoteInfo) -> str:
    return f"note {note.id} [{note.domain}] {note.created_at:%Y-%m-%d}\n{note.body}"


def search_sources(resp: SearchResponse) -> tuple[NoteSource, ...]:
    """The structured twin of format_search: a source per hit for the UI's cards."""
    return tuple(
        NoteSource(note_id=r.note_id, domain=r.domain, snippet=r.snippet.strip())
        for r in resp.results
    )


def _note_snippet(body: str, limit: int = 140) -> str:
    """A one-line preview of a note's body for its source card."""
    line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return line[:limit]


def format_entity(view: dict[str, Any]) -> str:
    """The structured/graph view: schema.org kind, names, facts-as-edges, inbound
    edges, and a mention count. Text-only now; an entity_card view comes with the
    component registry (the text-first tool path, ASSISTANT_PLAN.md)."""
    lines = [f"{view['canonical_name']} [{view['kind']}] ({view['domain']})"]
    if aliases := view.get("aliases"):
        lines.append("also known as: " + ", ".join(aliases))
    current = [p["current"] for p in view.get("predicates", []) if p.get("current")]
    if current:
        lines.append("facts:")
        lines += [f"- {f['predicate']}: {f['statement']}" for f in current]
    if inbound := view.get("inbound"):
        lines.append("referenced by:")
        lines += [f"- {r['name']} {r['predicate']} this" for r in inbound]
    if mentions := view.get("mentions"):
        lines.append(f"mentioned in {len(mentions)} note(s).")
    return "\n".join(lines)


def build_read_handlers(search: SearchService, notes: NotesRepo) -> dict[str, ToolHandler]:
    async def search_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolOutput("search needs a non-empty query.")
        limit = int(arguments.get("limit", _DEFAULT_LIMIT))
        resp = await search.search(ctx.session, query, None, limit)
        return ToolOutput(format_search(resp), search_sources(resp))

    async def read_note_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        note_id = str(arguments.get("note_id", "")).strip()
        if not note_id:
            return ToolOutput("read_note needs a note_id.")
        note = await notes.get_note(ctx.session, note_id)
        if note is None:
            return ToolOutput("No note with that id is in scope.")
        source = NoteSource(note_id=note.id, domain=note.domain, snippet=_note_snippet(note.body))
        return ToolOutput(format_note(note), (source,))

    return {"search": search_tool, "read_note": read_note_tool}


_ENTITY_LIMIT = 8


def entity_refs(rows: list[dict[str, Any]]) -> tuple[EntityRef, ...]:
    """Map entity rows to refs for the response's tappable entity chips."""
    return tuple(
        EntityRef(entity_id=str(r["id"]), label=str(r["canonical_name"]), domain=r["domain"])
        for r in rows
    )


def format_entities(rows: list[dict[str, Any]]) -> str:
    """The model-facing list — names + ids so it can chain into read_entity."""
    return "\n".join(
        f"- {r['canonical_name']} [{r['kind']}] ({r['domain']}) id={r['id']}" for r in rows
    )


def build_entity_handlers(entities: EntityReader) -> dict[str, ToolHandler]:
    async def read_entity_tool(arguments: dict, ctx: ToolContext) -> str:
        entity_id = str(arguments.get("entity_id", "")).strip()
        if not entity_id:
            return "read_entity needs an entity_id."
        view = await entities.entity_view(ctx.session, entity_id)
        return format_entity(view) if view is not None else "No entity with that id is in scope."

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

    return {"read_entity": read_entity_tool, "find_entity": find_entity_tool}


def build_registry(
    search: SearchService,
    notes: NotesRepo,
    entities: EntityReader,
    memory: MemoryService,
    proposals: ProposalRepo,
    connectors: ConnectorRegistry,
) -> ToolRegistry:
    """The agent's tool registry: every shipped sidecar bound to its handler — the
    read tools, the Tier-A memory tools, propose_correction (which stages a
    Proposal, never writes), and the egress connector tools (which stage an egress
    Proposal, never call out). Fails at startup if a sidecar and handler don't
    match exactly, so a new .tool can never ship unwired."""
    return load_registry(
        TOOLS_DIR,
        {
            **build_read_handlers(search, notes),
            **build_entity_handlers(entities),
            **build_memory_handlers(memory),
            **build_proposal_handlers(proposals),
            **build_connector_handlers(connectors, proposals),
        },
    )
