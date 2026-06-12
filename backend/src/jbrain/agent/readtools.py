"""The first read-only agent tools: `search` and `read_note`, thin handlers over
the existing search and notes services.

Each handler runs under the session's RLS scope (`ToolContext.session`), so a
narrowed session only ever sees in-scope data — the firewall is the services',
not the handler's. `build_registry` binds these handlers to their `.tool`
sidecars (docs/ASSISTANT_PLAN.md P4.4c).
"""

from pathlib import Path

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.agent.toolregistry import ToolRegistry, load_registry
from jbrain.notes.service import NoteInfo, NotesRepo
from jbrain.search.service import SearchResponse, SearchService

TOOLS_DIR = Path(__file__).parent / "tools"
_DEFAULT_LIMIT = 8


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


def build_read_handlers(search: SearchService, notes: NotesRepo) -> dict[str, ToolHandler]:
    async def search_tool(arguments: dict, ctx: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "search needs a non-empty query."
        limit = int(arguments.get("limit", _DEFAULT_LIMIT))
        resp = await search.search(ctx.session, query, None, limit)
        return format_search(resp)

    async def read_note_tool(arguments: dict, ctx: ToolContext) -> str:
        note_id = str(arguments.get("note_id", "")).strip()
        if not note_id:
            return "read_note needs a note_id."
        note = await notes.get_note(ctx.session, note_id)
        return format_note(note) if note is not None else "No note with that id is in scope."

    return {"search": search_tool, "read_note": read_note_tool}


def build_registry(search: SearchService, notes: NotesRepo) -> ToolRegistry:
    """The agent's read-only tool registry: the shipped sidecars bound to their
    handlers. Fails at startup if a sidecar and handler don't match exactly."""
    return load_registry(TOOLS_DIR, build_read_handlers(search, notes))
