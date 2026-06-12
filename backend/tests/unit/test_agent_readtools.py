"""The read-only tools: result formatting, RLS-scope passthrough to the
services, and the shipped sidecars bound + pinned to their versions."""

from datetime import UTC, datetime

from jbrain.agent.loop import ToolContext
from jbrain.agent.readtools import (
    TOOLS_DIR,
    build_read_handlers,
    build_registry,
    format_note,
    format_search,
)
from jbrain.agent.toolfile import load_tool
from jbrain.db.session import SessionContext
from jbrain.notes.service import NoteInfo
from jbrain.search.service import SearchResponse, SearchResult

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=("general",))


def result(
    note_id: str = "n1", domain: str = "general", snippet: str = "hello world"
) -> SearchResult:
    return SearchResult(
        note_id=note_id,
        chunk_id="c1",
        snippet=snippet,
        match="both",
        score=1.0,
        domain=domain,
        destination=None,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        body_preview="...",
        attachment_count=0,
        source_kind="note",
        source_anchor=None,
    )


def note(note_id: str = "n1", domain: str = "health", body: str = "BP 120/80") -> NoteInfo:
    return NoteInfo(
        id=note_id,
        client_id="c",
        domain=domain,
        destination=None,
        body=body,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


class FakeSearch:
    def __init__(self, resp: SearchResponse):
        self.resp = resp
        self.calls: list[tuple] = []

    async def search(self, ctx, q, domain, limit):  # noqa: ANN001
        self.calls.append((ctx, q, domain, limit))
        return self.resp


class FakeNotes:
    def __init__(self, stored: NoteInfo | None):
        self.stored = stored

    async def get_note(self, ctx, note_id):  # noqa: ANN001
        return self.stored if self.stored is not None and note_id == self.stored.id else None


def handlers(search_resp: SearchResponse | None = None, stored: NoteInfo | None = None):
    resp = search_resp if search_resp is not None else SearchResponse(degraded=False, results=[])
    return build_read_handlers(FakeSearch(resp), FakeNotes(stored))  # type: ignore[arg-type]


# --- formatting ----------------------------------------------------------


def test_format_search_lists_results() -> None:
    out = format_search(SearchResponse(degraded=False, results=[result(snippet="eggs and coffee")]))
    assert "note n1 [general]" in out and "eggs and coffee" in out


def test_format_search_empty_and_degraded() -> None:
    assert (
        format_search(SearchResponse(degraded=False, results=[])) == "No matching notes in scope."
    )
    degraded = format_search(SearchResponse(degraded=True, results=[result()]))
    assert degraded.startswith("(keyword-only search")


def test_format_note_includes_body_and_domain() -> None:
    assert format_note(note(body="my note")) == "note n1 [health] 2026-06-01\nmy note"


# --- handlers ------------------------------------------------------------


async def test_search_tool_forwards_scope_and_query() -> None:
    fake = FakeSearch(SearchResponse(degraded=False, results=[result()]))
    tools = build_read_handlers(fake, FakeNotes(None))  # type: ignore[arg-type]
    out = await tools["search"]({"query": "groceries", "limit": 3}, CTX)
    assert "note n1" in out
    # The handler ran the search under the session's scope, with its query.
    assert fake.calls == [(CTX.session, "groceries", None, 3)]


async def test_search_tool_rejects_empty_query() -> None:
    out = await handlers()["search"]({"query": "  "}, CTX)
    assert "non-empty query" in out


async def test_read_note_found_and_missing() -> None:
    tools = handlers(stored=note(note_id="abc", body="the note"))
    assert "the note" in await tools["read_note"]({"note_id": "abc"}, CTX)
    assert "in scope" in await tools["read_note"]({"note_id": "other"}, CTX)


async def test_read_note_needs_an_id() -> None:
    assert "needs a note_id" in await handlers()["read_note"]({}, CTX)


# --- registry + version guard --------------------------------------------


def test_build_registry_binds_the_shipped_sidecars() -> None:
    registry = build_registry(FakeSearch(SearchResponse(False, [])), FakeNotes(None))  # type: ignore[arg-type]
    assert registry.names() == {"search", "read_note"}
    assert {t.name for t in registry.schemas_for({"general"})} == {"search", "read_note"}


def test_sidecars_pinned_to_their_versions() -> None:
    """Editing a tool's behavior must be a deliberate version bump (the CI guard)."""
    pins = {
        "search.tool": (
            "search",
            1,
            "7d3db2e761fa949b5e63799cc9a06e0535c2eb2d8f97d870a3da839c35aa4267",
        ),
        "read_note.tool": (
            "read_note",
            1,
            "17ae0e655486be95b41ba0b9ab1c1952b45be0f63822e831276cc238b93f66c8",
        ),
    }
    for filename, expected in pins.items():
        tf = load_tool(TOOLS_DIR / filename)
        assert (tf.spec.name, tf.spec.version, tf.digest) == expected
