"""The read-only tools: result formatting, RLS-scope passthrough to the
services, and the shipped sidecars bound + pinned to their versions."""

from datetime import UTC, datetime

from jbrain.agent.contracts import EntityRef, NoteSource
from jbrain.agent.hurricanetools import build_hurricane_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.readtools import (
    TOOLS_DIR,
    build_entity_handlers,
    build_read_handlers,
    build_registry,
    entity_view_objects,
    entity_view_ref,
    entity_view_sources,
    format_currency,
    format_entity,
    format_neighborhood,
    format_note,
    format_relations,
    format_search,
    format_wiki_article,
    neighborhood_entities,
    neighborhood_sources,
)
from jbrain.agent.toolfile import load_tool
from jbrain.agent.weatherhistorytools import build_weather_history_handlers
from jbrain.agent.weathertools import build_weather_handlers
from jbrain.agent.webtools import build_web_handlers
from jbrain.agent.wikiwritetools import build_wiki_write_handlers
from jbrain.connectors.base import ConnectorRegistry
from jbrain.connectors.medical import medical_connectors
from jbrain.db.session import SessionContext
from jbrain.notes.service import NoteInfo
from jbrain.search.service import SearchResponse, SearchResult
from jbrain.web import (
    HurricaneClient,
    NhcGisClient,
    NhcSurgeClient,
    NwsClient,
    SearxngClient,
    WeatherClient,
    WeatherHistoryClient,
    WebFetcher,
)

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
        "mentions": [
            # Two mentions in one note plus one in another: the view lists
            # DISTINCT notes newest-first, first mention per note winning.
            {
                "note_id": "n1",
                "snippet": "met **Celine** at the lake",
                "created_at": datetime(2026, 6, 20, tzinfo=UTC),
                "domain": "general",
                "note_created_at": datetime(2026, 6, 20, tzinfo=UTC),
            },
            {
                "note_id": "n1",
                "snippet": "Celine again",
                "created_at": datetime(2026, 6, 20, tzinfo=UTC),
                "domain": "general",
                "note_created_at": datetime(2026, 6, 20, tzinfo=UTC),
            },
            {
                "note_id": "n2",
                "snippet": "Celine started lisinopril",
                "created_at": datetime(2026, 6, 1, tzinfo=UTC),
                "domain": "health",
                "note_created_at": datetime(2026, 6, 1, tzinfo=UTC),
            },
        ],
    }


class FakeEntities:
    def __init__(
        self,
        view: dict | None,
        matches: list[dict] | None = None,
        related: list[dict] | None = None,
        currency: dict[str, list[dict]] | None = None,
        owner_id: str | None = None,
        vicinity: dict | None = None,
    ):
        self.view = view
        self.owner_id = owner_id
        self.matches = matches or []
        self.related = related or []
        self.currency = currency or {}
        self.vicinity = vicinity
        self.searched: list[tuple] = []
        self.traversed: list[tuple] = []
        self.walked: list[tuple] = []
        self.currency_calls: list[list[str]] = []

    async def entity_view(self, ctx, entity_id):  # noqa: ANN001
        return self.view if self.view is not None and entity_id == self.view["id"] else None

    async def owner_entity_id(self, ctx):  # noqa: ANN001
        return self.owner_id

    async def list_entities(self, ctx, q=None, kind=None, limit=200):  # noqa: ANN001
        self.searched.append((q, kind, limit))
        return self.matches

    async def relate(self, ctx, anchor_id, predicates, limit=8):  # noqa: ANN001
        self.traversed.append((anchor_id, tuple(predicates), limit))
        return self.related

    async def neighborhood(self, ctx, entity_id, *, depth=2, kinds="both", total_cap=75):  # noqa: ANN001
        self.walked.append((entity_id, depth, kinds, total_cap))
        if self.vicinity is not None and entity_id == self.vicinity["anchor"]:
            return self.vicinity
        return None

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
    # Source notes list DISTINCT notes with ids the model can read_note —
    # the entity is the doorway into the prose, not just a mention count.
    assert "source notes (2 total, newest first" in out
    assert "- note n1 [general] 2026-06-20: met **Celine** at the lake" in out
    assert "- note n2 [health] 2026-06-01: Celine started lisinopril" in out
    assert out.count("note n1") == 1  # two mentions in n1 collapse to one line


def test_format_entity_caps_source_notes_and_counts_the_rest() -> None:
    view = entity_view()
    view["mentions"] = [
        {
            "note_id": f"n{i}",
            "snippet": f"snippet {i}",
            "created_at": datetime(2026, 6, 28 - i, tzinfo=UTC),
            "domain": "general",
            "note_created_at": datetime(2026, 6, 28 - i, tzinfo=UTC),
        }
        for i in range(7)
    ]
    out = format_entity(view)
    assert "source notes (7 total" in out
    assert "note n4" in out and "note n5" not in out  # top 5 listed, rest counted
    assert "(+2 more" in out


def test_entity_view_sources_are_note_cards_for_the_listed_notes() -> None:
    sources = entity_view_sources(entity_view())
    assert sources == (
        NoteSource(note_id="n1", domain="general", snippet="met **Celine** at the lake"),
        NoteSource(note_id="n2", domain="health", snippet="Celine started lisinopril"),
    )


def test_entity_view_objects_are_chips_for_relationship_edges() -> None:
    objects = entity_view_objects(entity_view())
    assert objects == (EntityRef(entity_id="e2", label="Jeff", domain="general"),)


def test_entity_view_ref_carries_the_subjects_facts_for_grounding() -> None:
    # The read subject itself, with its current-fact statements — so a claim answered
    # from one of those facts grounds against the fact text, not just the name.
    ref = entity_view_ref(entity_view("abc"))
    assert ref == EntityRef(
        entity_id="abc",
        label="Celine Hopkins",
        domain="general",
        aliases=["Celine"],
        facts=["married to Jeff"],
    )


async def test_read_entity_found_and_missing() -> None:
    tools = build_entity_handlers(FakeEntities(entity_view("abc")))  # type: ignore[arg-type]
    found = await tools["read_entity"]({"entity_id": "abc"}, CTX)
    assert isinstance(found, ToolOutput)
    assert "Celine Hopkins" in found
    # The listed source notes ride along as openable cards.
    assert [s.note_id for s in found.sources] == ["n1", "n2"]
    # The subject leads — carrying its current-fact statements so an answer from one
    # of its facts grounds (not just its name/aliases) — then the spouse edge's
    # target rides along as a chip the PWA can linkify.
    assert found.entities == (
        EntityRef(
            entity_id="abc",
            label="Celine Hopkins",
            domain="general",
            aliases=["Celine"],
            facts=["married to Jeff"],
        ),
        EntityRef(entity_id="e2", label="Jeff", domain="general"),
    )
    assert "in scope" in await tools["read_entity"]({"entity_id": "other"}, CTX)


async def test_read_entity_needs_an_id() -> None:
    tools = build_entity_handlers(FakeEntities(None))  # type: ignore[arg-type]
    assert "needs an entity_id" in await tools["read_entity"]({}, CTX)


async def test_read_entity_resolves_the_owner_sentinel() -> None:
    # The model's natural reach for the owner is read_entity "me" — resolve the
    # sentinel to the owner entity so it is one successful call, not a failed guess.
    fake = FakeEntities(entity_view("abc"), owner_id="abc")
    tools = build_entity_handlers(fake)  # type: ignore[arg-type]
    for sentinel in ("me", "Me", "owner", "myself"):
        out = await tools["read_entity"]({"entity_id": sentinel}, CTX)
        assert "Celine Hopkins" in out, sentinel


async def test_read_entity_sentinel_without_a_me_entity_reports_absent() -> None:
    # No owner entity yet (a graph with no Me) → the sentinel resolves to nothing,
    # and the tool says so rather than reading a bogus id.
    tools = build_entity_handlers(FakeEntities(None, owner_id=None))  # type: ignore[arg-type]
    assert "in scope" in await tools["read_entity"]({"entity_id": "me"}, CTX)


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


# --- neighborhood (the n-hop vicinity tool) --------------------------------


def vicinity(anchor_id: str = "e0") -> dict:
    """A canned SqlAnalysisRepo.neighborhood result: anchor + one neighbor per
    hop (mixed edge kinds along the hop-2 path) and one connecting note."""
    return {
        "anchor": anchor_id,
        "depth": 2,
        "entities": [
            {
                "id": anchor_id,
                "name": "Me",
                "kind": "person",
                "domain": "general",
                "hop": 0,
                "path": "Me",
            },
            {
                "id": "e2",
                "name": "Celine",
                "kind": "person",
                "domain": "general",
                "hop": 1,
                "path": "Me -spouse-> Celine",
            },
            {
                "id": "e3",
                "name": "Dr. Patel",
                "kind": "person",
                "domain": "health",
                "hop": 2,
                "path": "Me -spouse-> Celine -co-mention(note n7)-> Dr. Patel",
            },
        ],
        "notes": [
            {"note_id": "n7", "domain": "health", "hop": 1, "connects": ["Celine", "Dr. Patel"]},
        ],
    }


def test_format_neighborhood_groups_hops_and_lists_connecting_notes() -> None:
    lines = format_neighborhood(vicinity()).splitlines()
    assert lines[0] == "Me [person] (general) id=e0 — neighborhood within 2 hop(s):"
    assert "hop 1:" in lines and "hop 2:" in lines
    # format_relations' line idiom: name, kind, domain, a chainable id — plus
    # the one connecting path the traversal kept.
    assert "- Celine [person] (general) id=e2 — Me -spouse-> Celine" in lines
    assert (
        "- Dr. Patel [person] (health) id=e3 — Me -spouse-> Celine -co-mention(note n7)-> Dr. Patel"
    ) in lines
    # The connecting note carries a read_note-able id and WHO it ties together.
    assert "connecting notes:" in lines
    assert "- note n7 (hop 1) — Celine, Dr. Patel" in lines


def test_format_neighborhood_lonely_anchor() -> None:
    lonely = {"anchor": "e0", "depth": 2, "entities": vicinity()["entities"][:1], "notes": []}
    out = format_neighborhood(lonely)
    assert "no connected entities in scope." in out
    assert "connecting notes:" not in out


def test_neighborhood_chips_are_entities_and_note_sources() -> None:
    assert neighborhood_entities(vicinity()) == (
        EntityRef(entity_id="e0", label="Me", domain="general"),
        EntityRef(entity_id="e2", label="Celine", domain="general"),
        EntityRef(entity_id="e3", label="Dr. Patel", domain="health"),
    )
    # The snippet names why the note surfaced — the body was never fetched.
    assert neighborhood_sources(vicinity()) == (
        NoteSource(note_id="n7", domain="health", snippet="connects Celine, Dr. Patel"),
    )


async def test_neighborhood_defaults_anchor_to_owner_and_forwards_args() -> None:
    fake = FakeEntities(None, owner_id="e0", vicinity=vicinity())
    out = await build_entity_handlers(fake)["neighborhood"]({}, CTX)  # type: ignore[arg-type]
    assert isinstance(out, ToolOutput)
    assert "neighborhood within 2 hop(s)" in out
    # No anchor → the owner's entity, with the ratified defaults.
    assert fake.walked == [("e0", 2, "both", 75)]
    assert out.entities == neighborhood_entities(vicinity())
    assert out.sources == neighborhood_sources(vicinity())


async def test_neighborhood_resolves_sentinels_clamps_and_validates() -> None:
    fake = FakeEntities(None, owner_id="e0", vicinity=vicinity())
    tools = build_entity_handlers(fake)  # type: ignore[arg-type]
    # A sentinel anchor resolves to the owner; hops/limit clamp to the caps.
    await tools["neighborhood"]({"anchor": "Me", "hops": 9, "limit": 900}, CTX)
    assert fake.walked[-1] == ("e0", 3, "both", 75)
    await tools["neighborhood"](
        {"anchor": "e0", "hops": 0, "kinds": "co-mentions", "limit": 5}, CTX
    )
    assert fake.walked[-1] == ("e0", 1, "co-mentions", 5)
    bad = await tools["neighborhood"]({"anchor": "e0", "kinds": "webs"}, CTX)
    assert "kinds must be" in bad
    assert len(fake.walked) == 2  # the invalid call never reached the repo


async def test_neighborhood_misses_quietly() -> None:
    # No Me entity yet → the owner default resolves to nothing.
    no_me = build_entity_handlers(FakeEntities(None, owner_id=None))  # type: ignore[arg-type]
    assert "in scope" in await no_me["neighborhood"]({}, CTX)
    # An unknown/out-of-scope anchor is a quiet miss (RLS makes them one thing).
    tools = build_entity_handlers(FakeEntities(None, vicinity=vicinity()))  # type: ignore[arg-type]
    assert "in scope" in await tools["neighborhood"]({"anchor": "ghost"}, CTX)


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
        object(),  # type: ignore[arg-type]  # location repo
        object(),  # type: ignore[arg-type]  # device repo
        {
            **build_web_handlers(SearxngClient(""), WebFetcher()),
            **build_weather_handlers(WeatherClient("", ""), object()),  # type: ignore[arg-type]
            **build_weather_history_handlers(
                WeatherHistoryClient(""),
                WeatherClient("", ""),
                object(),  # type: ignore[arg-type]
            ),
            **build_hurricane_handlers(
                HurricaneClient(""),
                WeatherClient("", ""),
                object(),  # type: ignore[arg-type]
                NhcGisClient(""),
                NwsClient(""),
                NhcSurgeClient(""),
            ),
        },
        object(),  # type: ignore[arg-type]  # city geocoder
        object(),  # type: ignore[arg-type]  # sessionmaker (query_server_metrics)
        object(),  # type: ignore[arg-type]  # external reverse geocoder
    )
    # The `web` (opt-in) permission class — never offered to the default knowledge
    # agent (allow=None) at any scope. current_location is on-box but rides this gate;
    # so do the archivist's memory tools (owner-only scratchpad, archivist-only) — they
    # sit with the web tools here.
    web = {
        "web_search",
        "web_fetch",
        "current_location",
        "weather",
        "weather_history",
        "hurricane",
        "archivist_memory_read",
        "archivist_memory_write",
        # The spawn primitive is `web`-classed + NEVER_DEFAULT: offered to jerv (and
        # research/review children) by allowlist, never to the curator wildcard.
        "spawn_subagent",
    }
    shipped = {
        "search",
        "current_time",
        "read_wiki",
        "file_correction",
        "request_rebuild",
        "add_source_exclusion",
        "read_note",
        "read_entity",
        "find_entity",
        "relate",
        "neighborhood",
        "query_server_metrics",
        "read_lists",
        "read_list",
        "create_list",
        "add_list_item",
        "check_list_item",
        "remove_list_item",
        "read_appointments",
        "read_appointment",
        "manage_appointment",
        "read_labs",
        "read_encounters",
        "recall",
        "memory_read",
        "memory_edit",
        "remember",
        "propose_correction",
        "make_intake_link",
        "propose_merge",
        "lookup_medication",
        "lookup_condition",
        "geocode_reverse",
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
        *web,
    }
    assert registry.names() == shipped
    # The connector tools are external (no domain restriction). The geocode and
    # location read tools are location-domain, so a general-only scope doesn't see
    # them; a location scope sees the full set.
    location = {
        "geocode_reverse",
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
    # The web tools are the opt-in `web` class: never offered to the default
    # knowledge agent (allow=None), regardless of scope — only jerv allowlists them.
    assert {t.name for t in registry.schemas_for({"general"})} == shipped - location - web
    assert {t.name for t in registry.schemas_for({"location"})} == shipped - web
    # jerv's allowlist surfaces exactly the web tools and nothing else.
    assert {t.name for t in registry.schemas_for(set(), web)} == web


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
            4,
            "53a69c1aa3e21178b43a569fc8cfba56b3b95e5e6e8ee7cd170fef97c6e309fd",
        ),
        "find_entity.tool": (
            "find_entity",
            2,
            "0390b739d089e4185aac81918b0614384d0ef4889bf21bb09d5d43c492856da0",
        ),
        "relate.tool": (
            "relate",
            2,
            "826d35a411f61531c903ef6033ae8cb7a4a43e72443e473447afe22bd1aad40a",
        ),
        "neighborhood.tool": (
            "neighborhood",
            1,
            "2467a92dbe6f5d6737dbb036ead5900dedd41fc5077d6346e15d4f7859a9f288",
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
            3,
            "3b0f675e18a9484412d1309a3552522278ddda8b0c2d73c64eb3e332af9ad06c",
        ),
        "make_intake_link.tool": (
            "make_intake_link",
            3,
            "e8bec8c70f9af1ab3742a9a50eb6a9d7e6d5b74fc2807d4bdb0301cbac627daf",
        ),
        "propose_merge.tool": (
            "propose_merge",
            1,
            "2dc2c76d99bfcb2ffcc9f91747b506b595b08588714b5a4cd97cac9823e91fc1",
        ),
        "lookup_medication.tool": (
            "lookup_medication",
            2,
            "66fab04404884eeb9da92f2f0febfcae0c867f040f438ab3583ee16f5736b947",
        ),
        "lookup_condition.tool": (
            "lookup_condition",
            2,
            "9f883018796ab954acadb6a916529a7efc8c01d9a472e3f5bdba4420eaff3628",
        ),
        "read_appointments.tool": (
            "read_appointments",
            1,
            "8ea83f930e0f6cfe662e5349c786d61a2c11bc98eadfd807928f3640d8a65d8d",
        ),
        "read_labs.tool": (
            "read_labs",
            1,
            "d0eb90ac9c6e5953509d9ada921025adb5e3509a679880d6852c90e11c3b0cdc",
        ),
        "read_encounters.tool": (
            "read_encounters",
            1,
            "537bb247f07b51d14ef10a1fd980dfd757aea5e61e663419a68b322e30de7983",
        ),
        "read_appointment.tool": (
            "read_appointment",
            1,
            "5dc14fae478e6696019b6da85fae655c6e10ac6c36fe1c6a9d2e78dcdb94ee95",
        ),
        "manage_appointment.tool": (
            "manage_appointment",
            2,
            "23c3ad6224f2324edc96c71d9ab64b9345d347f8b086c6db6a14d484b8801bc0",
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
        "geocode_reverse.tool": (
            "geocode_reverse",
            2,
            "e6a3e9bd05accc6aa1e72ea4c12f165f9840873c39ba6b01cde5130db9c69ce6",
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
        "current_time.tool": (
            "current_time",
            1,
            "1139d8705fe31c1738afc01d13487a27626339de11b370cbafea4a446f35e02c",
        ),
        "current_location.tool": (
            "current_location",
            6,
            "430a0a9a9c1f31f3a2f8c826ff795f68093042657514776b4d44c84469f28b8b",
        ),
        "web_search.tool": (
            "web_search",
            1,
            "3bcaeea6850405c7fcdba6320c7ca00a3ad0f5d495531c12c041eda081192cf8",
        ),
        "web_fetch.tool": (
            "web_fetch",
            2,
            "126d4615631dcd528970f53d8cb96183982a89843daf462494c211240170b66e",
        ),
        "generate_image.tool": (
            "generate_image",
            6,
            "95ea01195ec987596896de7d1e38da19fbe2ebca9b6434659cbb85d8e31bb4d6",
        ),
        "edit_image.tool": (
            "edit_image",
            6,
            "d5ae86c49b7fd81eabe15cb14447420288268f69300240ce306bfae056c3ba39",
        ),
        "analyze_image.tool": (
            "analyze_image",
            1,
            "e20ed2107fb866bf2f5d09357f63317146e4604780274796105ea68eae76bdc0",
        ),
        "transcribe.tool": (
            "transcribe",
            2,
            "f7d5a16c2e858d890595e9fd452c3f3d25154679574e4d45181678866c288017",
        ),
        "analyze_video.tool": (
            "analyze_video",
            1,
            "d160a8e87df2b6ea2577c6ae485d37d9e43641981aa0b49716a6f6dad786e78f",
        ),
        "query_server_metrics.tool": (
            "query_server_metrics",
            1,
            "c913e40d4769f173fe57a03f30f1a9ef5380970fb423729001f4e3dffa353647",
        ),
        "weather.tool": (
            "weather",
            3,
            "82727cb75c53aa71beb7fda415a918f18417cdc1192786d715f36290f8f99056",
        ),
        "weather_history.tool": (
            "weather_history",
            3,
            "fec732e5f17cdecf54df9efdb6a139ddd19186d584eb7c248a1e71ee57046a82",
        ),
        "hurricane.tool": (
            "hurricane",
            3,
            "09606a541b29539f643447c27ba33a13e0219e7cd76a871c14e67d285ba77ad7",
        ),
        "gmail_search.tool": (
            "gmail_search",
            1,
            "ed90c5e6eff5ca4ec063a5a0f15b829ac9a4d18566f93b89fb4f0bbdaa2c3318",
        ),
        "gmail_read.tool": (
            "gmail_read",
            1,
            "568ebdb2e62865b2044fa6ce35ee02e09fccf1f5dcc52d95c0a2df460925ab69",
        ),
        "gmail_list_labels.tool": (
            "gmail_list_labels",
            1,
            "5c9447164ccd0feea0389ddb6c5780f77acc9153435976ce1b34b64d57126996",
        ),
        "gmail_create_label.tool": (
            "gmail_create_label",
            1,
            "1b0b281b0eb755e49c6d5982c2702e8380f997c2eee7ea14d2940a98546ef2d1",
        ),
        "gmail_label.tool": (
            "gmail_label",
            1,
            "6504995269735307d76f3b33ffbf560b4a40f7e6317a8428d7b0838569304b37",
        ),
        "gmail_archive.tool": (
            "gmail_archive",
            1,
            "965d8f55bde6ce3ed7ac181ca06b60e7a6e2fdb237ec043bfcdc9ac8940b5607",
        ),
        "gmail_count.tool": (
            "gmail_count",
            1,
            "fff8ba00d650a1e2180f7fdad5b1f15b511cce63392a6b9815c171a1a81d1e23",
        ),
        "gmail_sender_breakdown.tool": (
            "gmail_sender_breakdown",
            1,
            "1d00b606a3eeb36d8f99f056403f17b99b6a7744bea1848d8319036a04cf518e",
        ),
        "gmail_bulk_label.tool": (
            "gmail_bulk_label",
            1,
            "8fd9bf109b08255b11ce674cb105fc720e041e6f300d9541f106f3dccdf89f44",
        ),
        "archivist_memory_read.tool": (
            "archivist_memory_read",
            1,
            "ecc3e239973ba3ab28bb52ecac3827f1bf2cf2bf2e94545e86d0c47ec64923ef",
        ),
        "archivist_memory_write.tool": (
            "archivist_memory_write",
            1,
            "29c46e596faccbd1779549d6b6d020d185fa38b2d3d8acf2c31e01c2462ccbb8",
        ),
        "spawn_subagent.tool": (
            "spawn_subagent",
            5,
            "0951333387033c01df060fe90a71058d3414ec6edd22e1c44ef8bb01f417a744",
        ),
    }
    # Every shipped sidecar must appear above — a new `.tool` cannot slip in
    # unpinned (the gap this closes: propose_merge was registered but never pinned).
    on_disk = {p.name for p in TOOLS_DIR.glob("*.tool")}
    assert on_disk == set(pins), f"unpinned sidecars: {sorted(on_disk - set(pins))}"
    for filename, expected in pins.items():
        tf = load_tool(TOOLS_DIR / filename)
        assert (tf.spec.name, tf.spec.version, tf.digest) == expected


def test_query_server_metrics_offered_to_jerv() -> None:
    """The host-metrics read is on jerv's allowlist and (declaring no domains) is
    visible to jerv's empty-scope session — owner data stays gated by the tables' RLS,
    but the hardware-telemetry summary is offered."""
    from jbrain.agent.agents import JERV_TOOLS
    from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry

    async def noop(_args: dict, _ctx: ToolContext) -> ToolOutput:
        return ToolOutput("")

    assert "query_server_metrics" in JERV_TOOLS
    registry = ToolRegistry(
        [RegisteredTool(load_tool(TOOLS_DIR / "query_server_metrics.tool"), noop)]
    )
    # Empty scopes (a sandboxed jerv session) + jerv's allowlist still surfaces it.
    assert {t.name for t in registry.schemas_for(set(), JERV_TOOLS)} == {"query_server_metrics"}


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
