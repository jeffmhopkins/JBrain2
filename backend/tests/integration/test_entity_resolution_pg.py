"""Entity resolution layers 2b/2/3 against real Postgres: relationship hops,
embedding similarity (fake embed client), and the batched entity.disambiguate
call (fake LLM) including its degrade-to-review path."""

import json
import math
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.analysis.entities import (
    DISAMBIGUATE_MAX_TOKENS,
    DISAMBIGUATE_SCHEMA,
    DISAMBIGUATE_SYSTEM,
    AmbiguousEntity,
    NeedsDisambiguation,
    ResolvedEntity,
    get_or_create_me,
    register_declared_alias,
    resolve_entity,
)
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, EntityAlias, Fact
from jbrain.models.core import Subject
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

NOTE_TIME = datetime(2026, 6, 2, 16, 0, tzinfo=UTC)
EARLIER = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
MAY_FIRST = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean(database_url: str) -> AsyncIterator[None]:  # noqa: F811
    """Clean slate at setup; the app role lacks TRUNCATE on facts, so reuse
    the admin swap the other integration suites use."""
    admin_url = database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test")
    engine = create_async_engine(admin_url, poolclass=NullPool)
    async with async_sessionmaker(engine)() as s:
        await s.execute(
            text(
                "TRUNCATE app.facts, app.entities, app.entity_mentions, app.entity_aliases,"
                " app.temporal_tokens, app.review_items, app.note_analysis,"
                " app.chunks, app.notes, app.subjects CASCADE"
            )
        )
        await s.commit()
    await engine.dispose()
    yield


# --- seeding helpers ---------------------------------------------------------


async def seed_note(
    maker: async_sessionmaker[AsyncSession], *, body: str = "seed", domain: str = "general"
) -> uuid.UUID:
    note_id = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, created_at)"
                " VALUES (:i, :c, :d, :b, :t)"
            ),
            {"i": str(note_id), "c": str(note_id)[:12], "d": domain, "b": body, "t": NOTE_TIME},
        )
    return note_id


async def seed_chunk(
    maker: async_sessionmaker[AsyncSession], note_id: uuid.UUID, body: str, domain: str = "general"
) -> None:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:i, :n, :d, 'paragraph', 1, :b)"
            ),
            {"i": str(uuid.uuid4()), "n": str(note_id), "d": domain, "b": body},
        )


async def seed_entity(
    maker: async_sessionmaker[AsyncSession],
    name: str,
    *,
    kind: str = "Person",
    domain: str = "general",
    with_subject: bool = False,
) -> uuid.UUID:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        subject_id = None
        if with_subject:
            subject = Subject(id=uuid.uuid4(), display_name=name, kind="person")
            s.add(subject)
            await s.flush()
            subject_id = subject.id
        entity = Entity(
            id=uuid.uuid4(),
            kind=kind,
            canonical_name=name,
            status="provisional",
            subject_id=subject_id,
            domain_code=domain,
        )
        s.add(entity)
        s.add(
            EntityAlias(
                id=uuid.uuid4(),
                entity_id=entity.id,
                alias=name,
                alias_norm=name.casefold(),
                domain_code=domain,
            )
        )
        await s.flush()
        return entity.id


async def seed_fact(
    maker: async_sessionmaker[AsyncSession],
    *,
    entity_id: uuid.UUID,
    note_id: uuid.UUID,
    predicate: str,
    statement: str,
    kind: str = "relationship",
    object_entity_id: uuid.UUID | None = None,
    value_json: dict[str, Any] | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    status: str = "active",
    assertion: str = "asserted",
    domain: str = "general",
) -> None:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        s.add(
            Fact(
                id=uuid.uuid4(),
                entity_id=entity_id,
                predicate=predicate,
                kind=kind,
                statement=statement,
                value_json=value_json,
                object_entity_id=object_entity_id,
                assertion=assertion,
                valid_from=valid_from,
                valid_to=valid_to,
                reported_at=NOTE_TIME,
                status=status,
                note_id=note_id,
                extractor="test:fake",
                prompt_version="test",
                domain_code=domain,
            )
        )


async def resolve(
    maker: async_sessionmaker[AsyncSession],
    name: str,
    *,
    kind_hint: str = "Person",
    domain: str = "general",
    note_time: datetime | None = NOTE_TIME,
    surface: str | None = None,
    embedder: Any = None,
):
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return await resolve_entity(
            s,
            name,
            kind_hint=kind_hint,
            domain=domain,
            note_time=note_time,
            surface=surface,
            embedder=embedder,
            embed_model="test-embed",
        )


async def seed_rat_graph(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """Summer owns Ricky (Animal) whose name fact mentions 'rat'."""
    note = await seed_note(maker)
    summer = await seed_entity(maker, "Summer")
    ricky = await seed_entity(maker, "Ricky", kind="Animal")
    await seed_fact(
        maker,
        entity_id=ricky,
        note_id=note,
        predicate="name",
        kind="attribute",
        statement="Summer's rat is named Ricky.",
        value_json={"name": "Ricky", "species": "rat"},
    )
    await seed_fact(
        maker,
        entity_id=summer,
        note_id=note,
        predicate="owns",
        statement="Summer owns Ricky.",
        object_entity_id=ricky,
    )
    return ricky


# --- layer 2b: relationship hop ----------------------------------------------


async def test_exact_alias_still_wins(maker: async_sessionmaker[AsyncSession]) -> None:
    ricky = await seed_rat_graph(maker)
    outcome = await resolve(maker, "Ricky", kind_hint="Animal")
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.id == ricky
    assert not outcome.created
    assert outcome.method == "exact_alias"
    assert outcome.confidence == 1.0


async def test_possessive_hop_resolves_owned_object(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    ricky = await seed_rat_graph(maker)
    outcome = await resolve(maker, "Summer's rat", kind_hint="Animal")
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.id == ricky
    assert outcome.method == "relationship"
    assert outcome.confidence < 1.0


async def test_possessive_unknown_owner_falls_through_to_creation(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await seed_rat_graph(maker)
    outcome = await resolve(maker, "Bob's car", kind_hint="Thing")
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.created


async def test_definite_resolves_only_when_unique(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    ricky = await seed_rat_graph(maker)
    outcome = await resolve(maker, "the rat", kind_hint="Animal")
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.id == ricky

    # A second rat-matching entity makes the bare definite undecidable.
    note = await seed_note(maker)
    splinter = await seed_entity(maker, "Splinter", kind="Animal")
    await seed_fact(
        maker,
        entity_id=splinter,
        note_id=note,
        predicate="species",
        kind="attribute",
        statement="Splinter is a rat.",
        value_json={"species": "rat"},
    )
    outcome = await resolve(maker, "the rat", kind_hint="Animal")
    assert isinstance(outcome, NeedsDisambiguation)
    assert {c.id for c in outcome.candidates} == {ricky, splinter}


async def seed_live_rat_graph(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """The field-observed shapes, NOT the idealized ones: Ricky's kind came
    back 'pet', he has no facts of his own, and the only rat-evidence is the
    STATEMENT of Summer's owns edge."""
    note = await seed_note(maker)
    summer = await seed_entity(maker, "Summer")
    ricky = await seed_entity(maker, "Ricky", kind="pet")
    await seed_fact(
        maker,
        entity_id=summer,
        note_id=note,
        predicate="owns",
        statement="Summer owns a rat named Ricky.",
        object_entity_id=ricky,
    )
    return ricky


async def test_normalized_name_resolves_via_surface_and_object_edge(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # The model normalized "the rat" to the invented name "Rat"; the verbatim
    # surface supplies the reference shape, the owns-edge statement the noun,
    # and the creature tolerance bridges hint "animal" vs kind "pet".
    ricky = await seed_live_rat_graph(maker)
    outcome = await resolve(maker, "Rat", kind_hint="animal", surface="The rat")
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.id == ricky
    assert outcome.method == "relationship"


async def test_possessive_surface_resolves_through_edge_statement(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    ricky = await seed_live_rat_graph(maker)
    outcome = await resolve(maker, "Rat", kind_hint="pet", surface="Summer's rat")
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.id == ricky


async def test_negated_object_edge_is_not_noun_evidence(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # "Summer does not own a rat" must not make Ricky rat-shaped: the mention
    # falls through to provisional creation rather than a wrong silent link.
    note = await seed_note(maker)
    summer = await seed_entity(maker, "Summer")
    ricky = await seed_entity(maker, "Ricky", kind="pet")
    await seed_fact(
        maker,
        entity_id=summer,
        note_id=note,
        predicate="owns",
        statement="Summer does not own a rat.",
        object_entity_id=ricky,
        assertion="negated",
    )
    outcome = await resolve(maker, "Rat", kind_hint="animal", surface="The rat")
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.created
    assert outcome.id != ricky


async def test_role_hop_respects_validity_at_note_time(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note = await seed_note(maker)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        me = await get_or_create_me(s)
    okafor = await seed_entity(maker, "Dr. Okafor")
    pruitt = await seed_entity(maker, "Dr. Pruitt")
    # The previous dentist's interval closed when Dr. Okafor took over: the
    # hop must pick the fact valid AT the note's time, not any dentist ever.
    await seed_fact(
        maker,
        entity_id=pruitt,
        note_id=note,
        predicate="dentist_of",
        statement="Dr. Pruitt was my dentist.",
        object_entity_id=me.id,
        valid_from=datetime(2024, 1, 1, tzinfo=UTC),
        valid_to=MAY_FIRST,
        status="superseded",
    )
    await seed_fact(
        maker,
        entity_id=okafor,
        note_id=note,
        predicate="dentist_of",
        statement="Dr. Okafor is my dentist.",
        object_entity_id=me.id,
        valid_from=MAY_FIRST,
    )
    outcome = await resolve(maker, "my dentist", note_time=NOTE_TIME)
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.id == okafor

    # Before the fact's valid_from no dentist relationship holds: review,
    # never a fresh "my dentist" entity.
    outcome = await resolve(maker, "my dentist", note_time=EARLIER)
    assert isinstance(outcome, AmbiguousEntity)
    assert outcome.candidate_ids == []


async def test_hop_never_crosses_the_domain_firewall(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note = await seed_note(maker, domain="health")
    ricky = await seed_entity(maker, "Ricky", kind="Animal", domain="health")
    await seed_fact(
        maker,
        entity_id=ricky,
        note_id=note,
        predicate="species",
        kind="attribute",
        statement="Ricky is a rat.",
        value_json={"species": "rat"},
        domain="health",
    )
    outcome = await resolve(maker, "the rat", kind_hint="Animal", domain="general")
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.created  # a new provisional, NOT the health-domain Ricky
    assert outcome.id != ricky


# --- layer 2: embedding similarity --------------------------------------------


def _vec(primary: float) -> list[float]:
    """384-dim unit vector at a chosen cosine to the mention axis."""
    return [primary, math.sqrt(1.0 - primary * primary)] + [0.0] * 382


MENTION_AXIS = [1.0] + [0.0] * 383


class FakeEmbed:
    """Deterministic embeddings: first substring rule wins, mention axis
    otherwise — similarity is then exactly the rule's primary component."""

    def __init__(self, rules: list[tuple[str, list[float]]]):
        self._rules = rules
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            vec = next((v for sub, v in self._rules if sub in t), MENTION_AXIS)
            out.append(vec)
        return out


async def test_embedding_strong_single_match_links(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    robert = await seed_entity(maker, "Robert Smith")
    fake = FakeEmbed([("Robert Smith", _vec(0.95))])
    outcome = await resolve(maker, "Bob Smith", embedder=fake)
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.id == robert
    assert outcome.method == "embedding"
    assert outcome.confidence == pytest.approx(0.95, abs=0.01)
    # The missing vector was backfilled from name+aliases and persisted.
    async with scoped_session(maker, SYSTEM_CTX) as s:
        model = (
            await s.execute(
                text("SELECT embedding_model FROM app.entities WHERE id = :i"),
                {"i": str(robert)},
            )
        ).scalar_one()
    assert model == "test-embed"


async def test_embedding_near_tie_goes_to_layer3(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    robert = await seed_entity(maker, "Robert Smith")
    bobby = await seed_entity(maker, "Bobby Smith")
    fake = FakeEmbed([("Robert Smith", _vec(0.85)), ("Bobby Smith", _vec(0.84))])
    outcome = await resolve(maker, "Bob Smith", embedder=fake)
    assert isinstance(outcome, NeedsDisambiguation)
    assert {c.id for c in outcome.candidates} == {robert, bobby}


async def test_embedding_below_band_creates_provisional(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await seed_entity(maker, "Robert Smith")
    fake = FakeEmbed([("Robert Smith", _vec(0.30))])
    outcome = await resolve(maker, "Acme Fencing", kind_hint="Organization", embedder=fake)
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.created


async def test_embedding_never_autolinks_subject_bearing_entities(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    mom = await seed_entity(maker, "Mom", with_subject=True)
    fake = FakeEmbed([("Mom", _vec(0.97))])
    outcome = await resolve(maker, "Mum", embedder=fake)
    # Cross-subject misattribution is a leak: strong similarity alone may
    # not attach a subject-bearing entity — layer 3 / review decides.
    assert isinstance(outcome, NeedsDisambiguation)
    assert [c.id for c in outcome.candidates] == [mom]


# --- layer 3: batched disambiguation through the pipeline ---------------------

BODY = "Bob Smith called about the fence."


def extraction_json() -> str:
    return json.dumps(
        {
            "title": "Bob called",
            "tags": ["bob"],
            "mentions": [{"name": "Bob Smith", "kind": "Person", "surface_text": "Bob Smith"}],
            "facts": [
                {
                    "predicate": "contact",
                    "qualifier": "",
                    "kind": "event",
                    "statement": "Bob Smith called about the fence.",
                    "value_json": {"topic": "fence"},
                    "assertion": "asserted",
                    "entity_ref": "Bob Smith",
                    "object_entity_ref": None,
                    "temporal": None,
                    "domain": "general",
                    "confidence": 0.9,
                }
            ],
            "temporal_tokens": [],
        }
    )


def near_tie_embedder() -> FakeEmbed:
    return FakeEmbed([("Robert Smith", _vec(0.85)), ("Bobby Smith", _vec(0.84))])


async def run_note(
    maker: async_sessionmaker[AsyncSession],
    fake_llm: FakeLlmClient,
    *,
    tasks: dict[str, tuple[str, str]],
    embedder: FakeEmbed,
) -> None:
    note_id = await seed_note(maker, body=BODY)
    await seed_chunk(maker, note_id, BODY)
    pipeline = AnalysisPipeline(
        maker,
        LlmRouter({"xai": fake_llm}, tasks),
        embedder=embedder,
        embed_model="test-embed",
    )
    await pipeline.analyze_note({"note_id": str(note_id)})


BOTH_TASKS = {
    "note.extract": ("xai", "grok-4.3"),
    "entity.disambiguate": ("xai", "grok-4.3"),
}


async def fetch_rows(maker: async_sessionmaker[AsyncSession], sql: str) -> list[Any]:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return list((await s.execute(text(sql))).all())


async def test_disambiguation_call_shape_and_link(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    robert = await seed_entity(maker, "Robert Smith")
    bobby = await seed_entity(maker, "Bobby Smith")
    fake = FakeLlmClient(
        [
            extraction_json(),
            json.dumps({"choices": [{"name": "Bob Smith", "entity_id": str(robert)}]}),
        ]
    )
    await run_note(maker, fake, tasks=BOTH_TASKS, embedder=near_tie_embedder())

    assert len(fake.calls) == 2
    call = fake.calls[1]
    assert call["system"] == DISAMBIGUATE_SYSTEM
    assert call["json_schema"] == DISAMBIGUATE_SCHEMA
    assert call["max_tokens"] == DISAMBIGUATE_MAX_TOKENS
    payload = json.loads(call["user_text"])
    [item] = payload["mentions"]
    assert item["name"] == "Bob Smith"
    assert {c["id"] for c in item["candidates"]} == {str(robert), str(bobby)}
    assert "Bob Smith" in (item["context"] or "")

    mentions = await fetch_rows(
        maker, "SELECT entity_id::text AS eid, link_method FROM app.entity_mentions"
    )
    assert [(m.eid, m.link_method) for m in mentions] == [(str(robert), "llm")]
    facts = await fetch_rows(maker, "SELECT entity_id::text AS eid FROM app.facts")
    assert [f.eid for f in facts] == [str(robert)]
    # The mention resolved: no third "Bob Smith" entity, no review item.
    assert len(await fetch_rows(maker, "SELECT 1 FROM app.entities")) == 2
    assert await fetch_rows(maker, "SELECT 1 FROM app.review_items") == []


async def test_disambiguation_none_creates_provisional(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await seed_entity(maker, "Robert Smith")
    await seed_entity(maker, "Bobby Smith")
    fake = FakeLlmClient(
        [
            extraction_json(),
            json.dumps({"choices": [{"name": "Bob Smith", "entity_id": None}]}),
        ]
    )
    await run_note(maker, fake, tasks=BOTH_TASKS, embedder=near_tie_embedder())

    rows = await fetch_rows(
        maker, "SELECT canonical_name, status FROM app.entities ORDER BY canonical_name"
    )
    assert ("Bob Smith", "provisional") in [(r.canonical_name, r.status) for r in rows]
    assert await fetch_rows(maker, "SELECT 1 FROM app.review_items") == []


async def test_disambiguation_degrades_to_review_when_task_unrouted(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await seed_entity(maker, "Robert Smith")
    await seed_entity(maker, "Bobby Smith")
    fake = FakeLlmClient([extraction_json()])
    await run_note(
        maker,
        fake,
        tasks={"note.extract": ("xai", "grok-4.3")},  # the harness router shape
        embedder=near_tie_embedder(),
    )

    # No disambiguate call was even attempted; the mention filed for review
    # instead of linking (or minting) anything.
    assert len(fake.calls) == 1
    reviews = await fetch_rows(maker, "SELECT kind, payload->>'name' AS name FROM app.review_items")
    assert [(r.kind, r.name) for r in reviews] == [("ambiguous_mention", "Bob Smith")]
    assert await fetch_rows(maker, "SELECT 1 FROM app.entity_mentions") == []
    assert await fetch_rows(maker, "SELECT 1 FROM app.facts") == []
    names = {
        r.canonical_name for r in await fetch_rows(maker, "SELECT canonical_name FROM app.entities")
    }
    assert "Bob Smith" not in names


# --- declared-name aliasing --------------------------------------------------


async def test_declared_alias_links_a_later_bare_name_to_me(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """ "my full name is Jeffrey Mark Hopkins" registers the name on Me, so a
    later bare "Jeffrey Mark Hopkins" resolves to the owner — not a new row."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        me = await get_or_create_me(s)
        added = await register_declared_alias(s, me.id, "Jeffrey Mark Hopkins")
    assert added == "jeffrey mark hopkins"

    outcome = await resolve(maker, "Jeffrey Mark Hopkins")
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.id == me.id and not outcome.created
    # The alias inherits Me's general firewall partition.
    rows = await fetch_rows(
        maker,
        "SELECT domain_code FROM app.entity_aliases WHERE alias_norm = 'jeffrey mark hopkins'",
    )
    assert [r.domain_code for r in rows] == ["general"]


async def test_declared_alias_skips_collision_with_another_entity(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A name that already keys a DIFFERENT live entity is NOT silently widened
    onto a second one — that is a merge proposal, not an alias."""
    other = await seed_entity(maker, "Jeffrey Mark Hopkins")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        me = await get_or_create_me(s)
        added = await register_declared_alias(s, me.id, "Jeffrey Mark Hopkins")
    assert added is None

    # The name still resolves to the original entity, never to Me.
    outcome = await resolve(maker, "Jeffrey Mark Hopkins")
    assert isinstance(outcome, ResolvedEntity)
    assert outcome.id == other and outcome.id != me.id
    owners = await fetch_rows(
        maker,
        "SELECT entity_id FROM app.entity_aliases WHERE alias_norm = 'jeffrey mark hopkins'",
    )
    assert [r.entity_id for r in owners] == [other]


async def test_declared_alias_is_idempotent_and_ignores_pronouns(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        me = await get_or_create_me(s)
        first = await register_declared_alias(s, me.id, "Jeffrey Mark Hopkins")
        repeat = await register_declared_alias(s, me.id, "Jeffrey Mark Hopkins")
        pronoun = await register_declared_alias(s, me.id, "my")
    assert first == "jeffrey mark hopkins"
    assert repeat is None and pronoun is None
    count = await fetch_rows(
        maker,
        "SELECT count(*) AS n FROM app.entity_aliases WHERE alias_norm = 'jeffrey mark hopkins'",
    )
    assert count[0].n == 1
