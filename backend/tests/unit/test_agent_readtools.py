"""The read-only tools: result formatting, RLS-scope passthrough to the
services, and the shipped sidecars bound + pinned to their versions."""

from datetime import UTC, datetime

from jbrain.agent.contracts import EntityRef, NoteSource
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.readtools import (
    TOOLS_DIR,
    build_entity_handlers,
    build_read_handlers,
    build_registry,
    entity_view_objects,
    format_entity,
    format_note,
    format_relations,
    format_search,
)
from jbrain.agent.toolfile import load_tool
from jbrain.connectors.base import ConnectorRegistry
from jbrain.connectors.medical import medical_connectors
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


def entity_view(entity_id: str = "e1") -> dict:
    return {
        "id": entity_id,
        "kind": "Person",
        "canonical_name": "Celine Hopkins",
        "status": "active",
        "domain": "general",
        "aliases": ["Celine"],
        "predicates": [
            {
                "predicate": "spouse",
                "qualifier": "",
                "current": {
                    "predicate": "spouse",
                    "statement": "married to Jeff",
                    "object_entity_id": "e2",
                    "object_entity_name": "Jeff",
                    "object_entity_domain": "general",
                },
                "history": [],
            },
            {"predicate": "employer", "qualifier": "", "current": None, "history": []},
        ],
        "inbound": [{"entity_id": "e2", "name": "Jeff", "predicate": "spouse", "statement": "..."}],
        "mentions": [{"note_id": "n1", "snippet": "...", "created_at": None}],
    }


class FakeEntities:
    def __init__(
        self,
        view: dict | None,
        matches: list[dict] | None = None,
        related: list[dict] | None = None,
    ):
        self.view = view
        self.matches = matches or []
        self.related = related or []
        self.searched: list[tuple] = []
        self.traversed: list[tuple] = []

    async def entity_view(self, ctx, entity_id):  # noqa: ANN001
        return self.view if self.view is not None and entity_id == self.view["id"] else None

    async def list_entities(self, ctx, q=None, kind=None, limit=200):  # noqa: ANN001
        self.searched.append((q, kind, limit))
        return self.matches

    async def relate(self, ctx, anchor_id, predicates, limit=8):  # noqa: ANN001
        self.traversed.append((anchor_id, tuple(predicates), limit))
        return self.related


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


async def test_search_tool_surfaces_structured_sources() -> None:
    results = [
        result(note_id="a", snippet="<mark>eggs</mark>"),
        result(note_id="b", domain="health"),
    ]
    out = await handlers(SearchResponse(degraded=False, results=results))["search"](
        {"query": "x"}, CTX
    )
    assert isinstance(out, ToolOutput)
    # One source per hit — id, domain, snippet — for the response's cards.
    assert out.sources == (
        NoteSource(note_id="a", domain="general", snippet="<mark>eggs</mark>"),
        NoteSource(note_id="b", domain="health", snippet="hello world"),
    )


async def test_search_tool_rejects_empty_query() -> None:
    out = await handlers()["search"]({"query": "  "}, CTX)
    assert isinstance(out, ToolOutput)
    assert "non-empty query" in out and out.sources == ()


async def test_read_note_found_surfaces_a_source() -> None:
    tools = handlers(stored=note(note_id="abc", domain="health", body="line one\nline two"))
    out = await tools["read_note"]({"note_id": "abc"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "line one" in out
    assert out.sources == (NoteSource(note_id="abc", domain="health", snippet="line one"),)


async def test_read_note_missing_has_no_source() -> None:
    out = await handlers(stored=note(note_id="abc"))["read_note"]({"note_id": "other"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "in scope" in out and out.sources == ()


async def test_read_note_needs_an_id() -> None:
    out = await handlers()["read_note"]({}, CTX)
    assert isinstance(out, ToolOutput)
    assert "needs a note_id" in out


def test_format_entity_shows_kind_aliases_and_edges() -> None:
    out = format_entity(entity_view())
    assert "Celine Hopkins [Person]" in out  # the schema.org kind
    assert "also known as: Celine" in out
    # A relationship edge surfaces the target's id so the model can chain into
    # read_entity (the "my wife's name" traversal).
    assert "- spouse: married to Jeff → Jeff (id=e2)" in out
    assert "Jeff spouse this" in out  # inbound edge
    assert "mentioned in 1 note" in out


def test_entity_view_objects_are_chips_for_relationship_edges() -> None:
    objects = entity_view_objects(entity_view())
    assert objects == (EntityRef(entity_id="e2", label="Jeff", domain="general"),)


async def test_read_entity_found_and_missing() -> None:
    tools = build_entity_handlers(FakeEntities(entity_view("abc")))  # type: ignore[arg-type]
    found = await tools["read_entity"]({"entity_id": "abc"}, CTX)
    assert isinstance(found, ToolOutput)
    assert "Celine Hopkins" in found
    # The spouse edge's target rides along as a chip the PWA can linkify.
    assert found.entities == (EntityRef(entity_id="e2", label="Jeff", domain="general"),)
    assert "in scope" in await tools["read_entity"]({"entity_id": "other"}, CTX)


async def test_read_entity_needs_an_id() -> None:
    tools = build_entity_handlers(FakeEntities(None))  # type: ignore[arg-type]
    assert "needs an entity_id" in await tools["read_entity"]({}, CTX)


async def test_find_entity_surfaces_refs_and_passes_the_query() -> None:
    matches = [
        {
            "id": "e1",
            "kind": "Person",
            "canonical_name": "Celine",
            "domain": "general",
            "aliases": ["Celine Hopkins"],
        },
        {"id": "e2", "kind": "Person", "canonical_name": "Celine R.", "domain": "health"},
    ]
    fake = FakeEntities(None, matches)
    out = await build_entity_handlers(fake)["find_entity"](  # type: ignore[arg-type]
        {"name": "celine", "kind": "Person"}, CTX
    )
    assert isinstance(out, ToolOutput)
    assert "id=e1" in out  # the model gets ids to chain into read_entity
    # Aliases ride along so the PWA can link a prose name that isn't the label.
    assert out.entities == (
        EntityRef(entity_id="e1", label="Celine", domain="general", aliases=["Celine Hopkins"]),
        EntityRef(entity_id="e2", label="Celine R.", domain="health"),
    )
    assert fake.searched == [("celine", "Person", 8)]


async def test_find_entity_handles_no_match_and_empty_name() -> None:
    none = await build_entity_handlers(FakeEntities(None, []))["find_entity"](  # type: ignore[arg-type]
        {"name": "ghost"}, CTX
    )
    assert isinstance(none, ToolOutput) and "No entity matching" in none and none.entities == ()
    empty = await build_entity_handlers(FakeEntities(None))["find_entity"]({}, CTX)  # type: ignore[arg-type]
    assert "needs a name" in empty


async def test_relate_anchors_on_the_owner_and_maps_the_relationship_word() -> None:
    related = [
        {
            "id": "e9",
            "kind": "Person",
            "canonical_name": "Celine",
            "domain": "general",
            "aliases": ["Celine Hopkins"],
            "predicate": "spouse",
        }
    ]
    fake = FakeEntities(None, related=related)
    out = await build_entity_handlers(fake)["relate"]({"relationship": "wife"}, CTX)  # type: ignore[arg-type]
    assert isinstance(out, ToolOutput)
    # The edge → entity line carries the id so the model can read it for the name.
    assert "spouse → Celine [Person] (general) id=e9" in out
    assert out.entities == (
        EntityRef(entity_id="e9", label="Celine", domain="general", aliases=["Celine Hopkins"]),
    )
    # No `from` → anchored on the owner ("Me"); "wife" mapped to the spouse predicate.
    anchor, preds, _ = fake.traversed[0]
    assert anchor is None
    assert "spouse" in preds


async def test_relate_passes_the_from_anchor_and_handles_no_match() -> None:
    fake = FakeEntities(None, related=[])
    out = await build_entity_handlers(fake)["relate"](  # type: ignore[arg-type]
        {"relationship": "manager", "from": "e1"}, CTX
    )
    assert isinstance(out, ToolOutput) and "No 'manager' relationship" in out and out.entities == ()
    assert fake.traversed[0][0] == "e1"  # the explicit anchor flows through


async def test_relate_needs_a_relationship() -> None:
    out = await build_entity_handlers(FakeEntities(None))["relate"]({}, CTX)  # type: ignore[arg-type]
    assert "needs a relationship" in out


def test_format_relations_shows_the_edge_and_ids() -> None:
    out = format_relations(
        [{"id": "e9", "kind": "Person", "canonical_name": "Celine", "domain": "general",
          "predicate": "spouse"}]
    )
    assert out == "- spouse → Celine [Person] (general) id=e9"


# --- registry + version guard --------------------------------------------


def test_build_registry_binds_the_shipped_sidecars() -> None:
    # build_memory_handlers only closes over the service (never calls it at build
    # time), so a placeholder stands in for the registry-shape assertion.
    registry = build_registry(
        FakeSearch(SearchResponse(False, [])),  # type: ignore[arg-type]
        FakeNotes(None),  # type: ignore[arg-type]
        FakeEntities(None),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        ConnectorRegistry(medical_connectors("http://rx", "http://mp")),
        object(),  # type: ignore[arg-type]
    )
    shipped = {
        "search",
        "read_note",
        "read_entity",
        "find_entity",
        "relate",
        "read_lists",
        "read_list",
        "create_list",
        "add_list_item",
        "check_list_item",
        "remove_list_item",
        "recall",
        "memory_read",
        "memory_edit",
        "remember",
        "propose_correction",
        "lookup_medication",
        "lookup_condition",
    }
    assert registry.names() == shipped
    # The connector tools are external (no domain restriction on visibility).
    assert {t.name for t in registry.schemas_for({"general"})} == shipped


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
        "read_entity.tool": (
            "read_entity",
            2,
            "74a50fc08a725755a7b54bcf0e0ed00cc4ae23ba67ce5d3a394ebc16f698ff6f",
        ),
        "find_entity.tool": (
            "find_entity",
            2,
            "0390b739d089e4185aac81918b0614384d0ef4889bf21bb09d5d43c492856da0",
        ),
        "relate.tool": (
            "relate",
            1,
            "f709cb46df3116817f8ee611eca38289b47b7b00b23dbedccb5f74bc0f6ca4ae",
        ),
        "read_lists.tool": (
            "read_lists",
            1,
            "6006f9bc4e80e0dc264686d15520574e34f09919c315a947f59df9ca24621b72",
        ),
        "read_list.tool": (
            "read_list",
            1,
            "d8d8ba35f781e7485b395a4c47b6f10a21beb020b28a86039a5e6a907579d562",
        ),
        "create_list.tool": (
            "create_list",
            1,
            "38b00d1ef10ae6ca013bed8b797a4220c49528f05bd2a8da0ca9abf4937eaf33",
        ),
        "add_list_item.tool": (
            "add_list_item",
            1,
            "b0f1dedb8bcacfad5af1b7ae495dea059e7a164965b71d3cda96324bfbaea49e",
        ),
        "check_list_item.tool": (
            "check_list_item",
            1,
            "cb981f0e953158d154aca08da4040059cee7b2608b881e16bf08e910ad27004d",
        ),
        "remove_list_item.tool": (
            "remove_list_item",
            1,
            "a2b43edbfe32b4397bebbf11f029ea086d6ad3d6391acb01bcb4c76937a0b54c",
        ),
        "recall.tool": (
            "recall",
            1,
            "a4854e45215e2d36f77a163ce02650ebd0167a383a96dd6f6f75459def4f9332",
        ),
        "memory_read.tool": (
            "memory_read",
            1,
            "1f6e2ff3084afbe2e752fc9fccf64b60a242692d1744af75a85f8b8b96c506e8",
        ),
        "memory_edit.tool": (
            "memory_edit",
            1,
            "2ff5624584b3474d65c8f1b66aa9f1f807d7343b957368c6ed1ef40cadb08efd",
        ),
        "remember.tool": (
            "remember",
            1,
            "194945fe66eb4a4ca5ab179da3b22541036de073a3f0c15e7bf2dc6763fce0ba",
        ),
        "propose_correction.tool": (
            "propose_correction",
            1,
            "d0e2fe9b1a1af0922a84fde2c5c299184ec2ac4e8d88ef28a9e2cd64bea9eaa6",
        ),
        "lookup_medication.tool": (
            "lookup_medication",
            1,
            "a318f9b67990265d3db6266c72d8e36e628c8f605417f052bfe6392c5f150335",
        ),
        "lookup_condition.tool": (
            "lookup_condition",
            1,
            "365558e91398836b3dca8ef6728e7fa86a82d49b3f3ccc25fa6efd79ca663af4",
        ),
    }
    for filename, expected in pins.items():
        tf = load_tool(TOOLS_DIR / filename)
        assert (tf.spec.name, tf.spec.version, tf.digest) == expected
