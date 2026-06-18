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
    format_currency,
    format_entity,
    format_note,
    format_relations,
    format_search,
    format_wiki_article,
)
from jbrain.agent.toolfile import load_tool
from jbrain.agent.wikiwritetools import build_wiki_write_handlers
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
        currency: dict[str, list[dict]] | None = None,
    ):
        self.view = view
        self.matches = matches or []
        self.related = related or []
        self.currency = currency or {}
        self.searched: list[tuple] = []
        self.traversed: list[tuple] = []
        self.currency_calls: list[list[str]] = []

    async def entity_view(self, ctx, entity_id):  # noqa: ANN001
        return self.view if self.view is not None and entity_id == self.view["id"] else None

    async def list_entities(self, ctx, q=None, kind=None, limit=200):  # noqa: ANN001
        self.searched.append((q, kind, limit))
        return self.matches

    async def relate(self, ctx, anchor_id, predicates, limit=8):  # noqa: ANN001
        self.traversed.append((anchor_id, tuple(predicates), limit))
        return self.related

    async def note_currency(self, ctx, note_ids):  # noqa: ANN001
        self.currency_calls.append(list(note_ids))
        return {n: self.currency[n] for n in note_ids if n in self.currency}


def stale(
    status: str = "superseded",
    entity_id: str = "e9",
    entity_name: str = "Sarah",
    predicate: str = "homeLocation",
    qualifier: str = "",
    stale_value: str = "Sarah lives in Austin.",
    current_value: str | None = "Sarah lives in Denver.",
) -> dict:
    return {
        "entity_id": entity_id,
        "entity_name": entity_name,
        "predicate": predicate,
        "qualifier": qualifier,
        "status": status,
        "stale_value": stale_value,
        "current_value": current_value,
    }


def handlers(
    search_resp: SearchResponse | None = None,
    stored: NoteInfo | None = None,
    currency: dict[str, list[dict]] | None = None,
):
    resp = search_resp if search_resp is not None else SearchResponse(degraded=False, results=[])
    return build_read_handlers(
        FakeSearch(resp),  # type: ignore[arg-type]
        FakeNotes(stored),  # type: ignore[arg-type]
        FakeEntities(None, currency=currency),  # type: ignore[arg-type]
    )


# --- formatting ----------------------------------------------------------


def test_format_search_lists_results() -> None:
    out = format_search(SearchResponse(degraded=False, results=[result(snippet="eggs and coffee")]))
    assert "note n1 [general]" in out and "eggs and coffee" in out


def test_format_search_surfaces_wiki_hits_and_sources_stay_notes() -> None:
    from jbrain.agent.readtools import search_sources
    from jbrain.search.service import WikiSearchResult

    wiki = WikiSearchResult(
        article_id="a1",
        title="Priya Nair",
        blurb="a pediatrician",
        entity_kind="Person",
        domain="general",
        snippet="founded the clinic",
        match="both",
        score=2.0,
    )
    resp = SearchResponse(degraded=False, results=[wiki, result(snippet="eggs")])
    out = format_search(resp)
    # The wiki article is surfaced as a read_wiki target; the note line is still there.
    assert 'wiki "Priya Nair" [general]' in out
    assert "note n1 [general]" in out
    # Structured source cards stay note-only (the wiki hit is in the prose, not a NoteSource).
    sources = search_sources(resp)
    assert [s.note_id for s in sources] == ["n1"]


def test_format_search_empty_and_degraded() -> None:
    assert (
        format_search(SearchResponse(degraded=False, results=[])) == "No matching notes in scope."
    )
    degraded = format_search(SearchResponse(degraded=True, results=[result()]))
    assert degraded.startswith("(keyword-only search")


def test_format_note_includes_body_and_domain() -> None:
    assert format_note(note(body="my note")) == "note n1 [health] 2026-06-01\nmy note"


# --- currency overlay ----------------------------------------------------


def test_format_currency_inlines_the_current_value_for_superseded() -> None:
    out = format_currency([stale()])
    assert "currency overlay" in out
    assert "Sarah.homeLocation: SUPERSEDED" in out
    assert "Current value: Sarah lives in Denver." in out  # inlined, not just a pointer
    assert "read_entity e9 for the current value" in out


def test_format_currency_distinguishes_retracted_and_pending() -> None:
    out = format_currency(
        [
            stale(status="retracted", current_value=None),
            stale(status="pending_review", predicate="birthDate", current_value=None),
        ]
    )
    assert "RETRACTED — no longer asserted" in out
    assert "PENDING REVIEW — unverified" in out


def test_format_currency_empty_is_blank() -> None:
    assert format_currency([]) == ""


def test_format_currency_qualifier_in_the_address() -> None:
    out = format_currency([stale(predicate="name", qualifier="nickname")])
    assert "Sarah.name.nickname:" in out


def test_format_search_flags_a_hit_whose_note_has_stale_facts() -> None:
    resp = SearchResponse(degraded=False, results=[result(note_id="n1"), result(note_id="n2")])
    out = format_search(resp, {"n1": [stale(), stale(status="retracted")]})
    assert "⚠ 2 fact(s) here are no longer current (retracted, superseded)" in out
    assert "read_entity e9" in out
    # The clean hit (n2) carries no flag.
    n2_line = [ln for ln in out.splitlines() if "note n2" in ln][0]
    assert "⚠" not in n2_line


# --- handlers ------------------------------------------------------------


async def test_search_tool_forwards_scope_and_query() -> None:
    fake = FakeSearch(SearchResponse(degraded=False, results=[result()]))
    tools = build_read_handlers(fake, FakeNotes(None), FakeEntities(None))  # type: ignore[arg-type]
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


async def test_read_note_appends_the_currency_overlay() -> None:
    tools = handlers(
        stored=note(note_id="abc", body="Sarah lives in Austin."),
        currency={"abc": [stale()]},
    )
    out = await tools["read_note"]({"note_id": "abc"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "Sarah lives in Austin." in out  # the original note prose
    assert "SUPERSEDED" in out and "Current value: Sarah lives in Denver." in out


async def test_search_tool_flags_stale_hits_and_scopes_the_lookup() -> None:
    resp = SearchResponse(degraded=False, results=[result(note_id="n1")])
    entities = FakeEntities(None, currency={"n1": [stale(status="retracted", current_value=None)]})
    tools = build_read_handlers(FakeSearch(resp), FakeNotes(None), entities)  # type: ignore[arg-type]
    out = await tools["search"]({"query": "where does sarah live"}, CTX)
    assert "no longer current (retracted)" in out
    assert entities.currency_calls == [["n1"]]  # looked up exactly the hit notes


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
        [
            {
                "id": "e9",
                "kind": "Person",
                "canonical_name": "Celine",
                "domain": "general",
                "predicate": "spouse",
            }
        ]
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
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]  # wiki reader
        build_wiki_write_handlers(object(), object(), object()),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]  # geocoder client
        object(),  # type: ignore[arg-type]  # location repo
        object(),  # type: ignore[arg-type]  # device repo
    )
    shipped = {
        "search",
        "read_wiki",
        "file_correction",
        "request_rebuild",
        "add_source_exclusion",
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
        "read_appointments",
        "read_appointment",
        "manage_appointment",
        "recall",
        "memory_read",
        "memory_edit",
        "remember",
        "propose_correction",
        "propose_merge",
        "lookup_medication",
        "lookup_condition",
        "geocode_reverse",
        "geocode_forward",
        "propose_prompt_edit",
        "where_is",
        "where_was_i",
        "device_status",
        "home_status",
        "nearby_now",
        "location_history",
        "location_query",
        "time_at_place",
        "find_when_at",
        "save_place",
    }
    assert registry.names() == shipped
    # The connector tools are external (no domain restriction). The geocode and
    # location read tools are location-domain, so a general-only scope doesn't see
    # them; a location scope sees the full set.
    location = {
        "geocode_reverse",
        "geocode_forward",
        "where_is",
        "where_was_i",
        "device_status",
        "home_status",
        "nearby_now",
        "location_history",
        "location_query",
        "time_at_place",
        "find_when_at",
        "save_place",
    }
    assert {t.name for t in registry.schemas_for({"general"})} == shipped - location
    assert {t.name for t in registry.schemas_for({"location"})} == shipped


def test_sidecars_pinned_to_their_versions() -> None:
    """Editing a tool's behavior must be a deliberate version bump (the CI guard)."""
    pins = {
        "search.tool": (
            "search",
            2,
            "b67c76b8f8bf8a7a0910fbbaed57d35332c787bec7c9408c07219b3acc982ac4",
        ),
        "read_note.tool": (
            "read_note",
            2,
            "5ab04ed74de5965d5bf67befcb0071c9ea9edc1e4b16d00871c373e216296a40",
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
            2,
            "978041c1ab636277dc626cdf996544eb0464116db0699065a0b3a2b08f8c4ee9",
        ),
        "propose_merge.tool": (
            "propose_merge",
            1,
            "2dc2c76d99bfcb2ffcc9f91747b506b595b08588714b5a4cd97cac9823e91fc1",
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
        "read_appointments.tool": (
            "read_appointments",
            1,
            "8ea83f930e0f6cfe662e5349c786d61a2c11bc98eadfd807928f3640d8a65d8d",
        ),
        "read_appointment.tool": (
            "read_appointment",
            1,
            "5dc14fae478e6696019b6da85fae655c6e10ac6c36fe1c6a9d2e78dcdb94ee95",
        ),
        "manage_appointment.tool": (
            "manage_appointment",
            1,
            "7fcd25cf5705ae0de9199a7a7c926b0551eb91e0fa3db62a8d03dd32c108fc7e",
        ),
        "read_wiki.tool": (
            "read_wiki",
            1,
            "16c880ea78ce613abb9373ec926a3a266bc40cff04176aaa49f59b87a89c2997",
        ),
        "file_correction.tool": (
            "file_correction",
            1,
            "00bdff904f7d167f0e865ce11ec01e0221d0de7570a589c531ee2af61a69d87d",
        ),
        "request_rebuild.tool": (
            "request_rebuild",
            1,
            "3e26e964361fc0f20826ae116661700603253a93bd0459f6ad276225157a9e1c",
        ),
        "add_source_exclusion.tool": (
            "add_source_exclusion",
            1,
            "216ca56795bcb7484aa43b7d14b4bf970c717a463e3b8af71d77dfd2c13eccf8",
        ),
        "propose_prompt_edit.tool": (
            "propose_prompt_edit",
            1,
            "b0d43cb16aa8fa9679c5b9ec6347e96a7876b62106dfa468f78d7516332fb13d",
        ),
        "geocode_reverse.tool": (
            "geocode_reverse",
            1,
            "e7478dc924b2e38f35609df78a7ed4c1b07b5a782f5e14068b126093584ba7e3",
        ),
        "geocode_forward.tool": (
            "geocode_forward",
            1,
            "e705abe592942eb4f39efafbdd8962e9a40a65836581c677cfca4897fadcc584",
        ),
        "where_is.tool": (
            "where_is",
            1,
            "1e4352c4c38a7a5e3b26286b1a061e08c8d8922ef94927b22e7843dbcb5cc5ef",
        ),
        "where_was_i.tool": (
            "where_was_i",
            1,
            "e87e5d2495c91cc6f9bbd50d1ed145810d0ac25da4fac7904a0a1aef7d03cea6",
        ),
        "device_status.tool": (
            "device_status",
            1,
            "a01af27bdd492b70ce2ee74e0cd642599dd17a2aa48acd65e4131c1815d7db93",
        ),
        "home_status.tool": (
            "home_status",
            1,
            "c08e4e0a3dcdaa6d798a36aaabd4ef1da3626145e13891d02f9dc074e6e1a916",
        ),
        "nearby_now.tool": (
            "nearby_now",
            1,
            "e6f14ba96a43708b9a9b5ebe1d700200342bd2dc597ed325bf48433345b286fe",
        ),
        "location_history.tool": (
            "location_history",
            1,
            "1e932f86aa8f42779d94184e9c8a36ddd3c913494c8afb6b8e7df1ba907cb0ab",
        ),
        "location_query.tool": (
            "location_query",
            1,
            "b4c20f12082cd1705284fd9132ac7601d4cfa0e85ee3127be3b896bf43502ef4",
        ),
        "time_at_place.tool": (
            "time_at_place",
            1,
            "9d8dc4826bc5e1043160cd685911db2026fc09cc152af5d20db5a216dded3083",
        ),
        "find_when_at.tool": (
            "find_when_at",
            1,
            "7e372556fb39944c622e6f7a11abe0be626d2d705a9e7a0259e3e976373c9fd8",
        ),
        "save_place.tool": (
            "save_place",
            1,
            "138da8801602ddaf693328a47ac7edec65cd06f62d75180f5bd02c4426bb3a57",
        ),
    }
    # Every shipped sidecar must appear above — a new `.tool` cannot slip in
    # unpinned (the gap this closes: propose_merge was registered but never pinned).
    on_disk = {p.name for p in TOOLS_DIR.glob("*.tool")}
    assert on_disk == set(pins), f"unpinned sidecars: {sorted(on_disk - set(pins))}"
    for filename, expected in pins.items():
        tf = load_tool(TOOLS_DIR / filename)
        assert (tf.spec.name, tf.spec.version, tf.digest) == expected


# --- read_wiki (the read-only wiki-editorial tool) ------------------------


class FakeWiki:
    def __init__(self, article: dict | None):
        self.article = article

    async def get_article(self, ctx, article_id):  # noqa: ANN001
        return self.article if self.article and article_id == self.article["id"] else None


def _article() -> dict:
    return {
        "id": "a1",
        "title": "Priya Nair",
        "subtitle": "Person · machine-written",
        "lead": [{"kind": "p", "text": "Priya is a pediatrician.[1]"}],
        "sections": [
            {
                "heading": "Career",
                "domain": "general",
                "blocks": [{"kind": "p", "text": "Founded a clinic.[1]"}],
                "subsections": [
                    {"heading": "Training", "blocks": [{"kind": "p", "text": "Residency."}]}
                ],
            }
        ],
        "references": [
            {
                "n": 1,
                "meta": "Note · Sep 5, 2024",
                "domain": "general",
                "snippet": "opened the clinic",
            }
        ],
    }


def test_format_wiki_article_renders_lead_sections_and_references() -> None:
    out = format_wiki_article(_article())
    assert "# Priya Nair" in out
    assert "## Career [general]" in out
    assert "### Training" in out
    assert "Founded a clinic.[1]" in out
    assert "References:" in out and "[1] Note · Sep 5, 2024 — opened the clinic" in out


async def test_read_wiki_tool_returns_the_article_else_a_quiet_miss() -> None:
    from jbrain.agent.readtools import build_wiki_handlers

    handler = build_wiki_handlers(FakeWiki(_article()))["read_wiki"]  # type: ignore[arg-type]
    hit = await handler({"article_id": "a1"}, CTX)
    assert isinstance(hit, ToolOutput) and "Priya Nair" in hit
    miss = await handler({"article_id": "nope"}, CTX)
    assert "No wiki article" in miss
    empty = await handler({"article_id": ""}, CTX)
    assert "needs an article_id" in empty
