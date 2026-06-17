"""The wiki read APIs (Phase 6) against real Postgres: GET-equivalent assembly of the reader
article + the landing rails from built articles, RLS-scoped — an out-of-scope section (and its
references) never appears in a rendered article, and a non-owner sees nothing.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.wiki.builder import StubRewriter, WikiBuilder
from jbrain.wiki.readstore import WikiReadStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class FakeEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        return str(
            (await s.execute(text("SELECT id FROM app.principals WHERE kind='owner'"))).scalar()
        )


async def _fact(maker: async_sessionmaker, entity_id: str, domain: str, statement: str) -> None:
    note, chunk, fact = (str(uuid.uuid4()) for _ in range(3))
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("INSERT INTO app.notes (id, client_id, domain_code, body) VALUES (:i,:c,:d,:b)"),
            {"i": note, "c": note[:12], "d": domain, "b": statement},
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:i,:n,:d,'paragraph',0,:b)"
            ),
            {"i": chunk, "n": note, "d": domain, "b": statement},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, assertion,"
                " reported_at, note_id, chunk_id, extractor, prompt_version, domain_code)"
                " VALUES (:i,:e,'p','state',:st,'asserted','2026-01-01T00:00:00Z',:n,:c,'fk',"
                " 'v1',:d)"
            ),
            {"i": fact, "e": entity_id, "st": statement, "n": note, "c": chunk, "d": domain},
        )


async def _build(
    maker: async_sessionmaker, name: str, kind: str, facts: list[tuple[str, str]]
) -> str:
    eid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:i,:k,:n,'general')"
            ),
            {"i": eid, "k": kind, "n": name},
        )
    for domain, statement in facts:
        await _fact(maker, eid, domain, statement)
    await WikiBuilder(
        maker, embed=FakeEmbed(), rewriter=StubRewriter(), embedding_model="fake"
    ).refresh()
    return eid


async def _article_id(maker: async_sessionmaker, entity_id: str) -> str:
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text("SELECT id FROM app.wiki_articles WHERE entity_ref = :e"), {"e": entity_id}
                )
            ).scalar()
        )


async def test_get_article_assembles_the_reader_shape(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    eid = await _build(
        maker, "Priya Nair", "Person", [("general", f"Priya did thing {i}") for i in range(3)]
    )
    store = WikiReadStore(maker)
    art = await store.get_article(OWNER, await _article_id(maker, eid))
    assert art is not None
    assert art["title"] == "Priya Nair"
    assert art["subtitle"].startswith("Person ·")
    assert art["infobox"]["kind"] == "Person"
    assert art["lead"] and art["lead"][0]["kind"] == "p"
    assert any(s["domain"] == "general" for s in art["sections"])
    assert art["sections"][0]["blocks"][0]["kind"] == "p"
    # The three facts each produced a clause + a [n] citation → three references.
    assert len(art["references"]) == 3
    ref = art["references"][0]
    assert {"n", "note_id", "meta", "domain", "snippet"} <= ref.keys()
    assert ref["meta"].startswith("Note ·")


async def test_get_article_emits_the_profile_photo_when_set(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    eid = await _build(maker, "Imogen", "Person", [("general", f"fact {i}") for i in range(3)])
    aid = await _article_id(maker, eid)
    store = WikiReadStore(maker)
    # No image yet → the reader falls back to the type disc (no photo).
    art = await store.get_article(OWNER, aid)
    assert art is not None and art["infobox"]["photo"] is False
    assert art["infobox"]["image_url"] is None
    # Once the article carries an image sha, the infobox emits photo + the serve URL.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.wiki_articles SET image_sha = 'abc123' WHERE id = :a"), {"a": aid}
        )
    art = await store.get_article(OWNER, aid)
    assert art is not None and art["infobox"]["photo"] is True
    assert art["infobox"]["image_url"] == f"/api/wiki/{aid}/image?v=abc123"


async def test_article_health_section_hidden_from_general_scope(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    eid = await _build(
        maker,
        "Sam",
        "Person",
        [
            ("general", "Sam lives in town"),
            ("general", "Sam works hard"),
            ("health", "Sam has a documented allergy"),
        ],
    )
    aid = await _article_id(maker, eid)
    store = WikiReadStore(maker)
    owner_art = await store.get_article(OWNER, aid)
    assert owner_art is not None
    assert {s["domain"] for s in owner_art["sections"]} == {"general", "health"}
    # A general-narrowed session sees the article but NOT the health section or its references.
    scoped = await store.get_article(read_context(pid, ("general",)), aid)
    assert scoped is not None
    assert {s["domain"] for s in scoped["sections"]} == {"general"}
    assert all(r["domain"] == "general" for r in scoped["references"])


async def test_get_article_404_and_non_owner(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    eid = await _build(maker, "X", "Person", [("general", f"x {i}") for i in range(3)])
    aid = await _article_id(maker, eid)
    store = WikiReadStore(maker)
    assert await store.get_article(OWNER, str(uuid.uuid4())) is None  # unknown id
    assert await store.get_article(OWNER, "not-a-uuid") is None  # malformed id
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await store.get_article(token, aid) is None  # owner-only shell


async def test_get_landing_groups_recent_and_hubs(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    priya = await _build(maker, "Priya Nair", "Person", [("general", f"p {i}") for i in range(3)])
    await _build(maker, "Globex", "Organization", [("general", f"g {i}") for i in range(3)])
    # A link from Globex's article INTO Priya's article makes her a hub (counted by to_article_id).
    async with scoped_session(maker, OWNER) as s:
        other_section, priya_article = (
            await s.execute(
                text(
                    "SELECT (SELECT s.id FROM app.wiki_sections s JOIN app.wiki_articles a"
                    "         ON a.id = s.article_id WHERE a.title = 'Globex' LIMIT 1),"
                    "       (SELECT id FROM app.wiki_articles WHERE entity_ref = :p)"
                ),
                {"p": priya},
            )
        ).one()
        await s.execute(
            text(
                "INSERT INTO app.wiki_links (from_section_id, to_entity_id, to_article_id, anchor,"
                " domain_code) VALUES (:s, :e, :a, 'Priya', 'general')"
            ),
            {"s": other_section, "e": priya, "a": priya_article},
        )
    landing = await WikiReadStore(maker).get_landing(OWNER)
    titles = {e["title"] for e in landing["recent"]}
    assert {"Priya Nair", "Globex"} <= titles
    types = {g["type"] for g in landing["groups"]}
    assert {"People", "Organizations"} <= types
    assert any(h["title"] == "Priya Nair" and h["links"] >= 1 for h in landing["hubs"])


async def test_self_links_do_not_make_a_hub(maker: async_sessionmaker) -> None:
    # A reflexive link (from an article's own section to its own entity) must NOT count toward
    # "most connected" — else an entity is a hub of itself.
    await _owner_pid(maker)
    eid = await _build(maker, "Solo", "Organization", [("general", f"s {i}") for i in range(3)])
    async with scoped_session(maker, OWNER) as s:
        own_section, own_article = (
            await s.execute(
                text(
                    "SELECT sec.id, a.id FROM app.wiki_sections sec JOIN app.wiki_articles a"
                    " ON a.id = sec.article_id WHERE a.entity_ref = :e LIMIT 1"
                ),
                {"e": eid},
            )
        ).one()
        # A link from Solo's own section into Solo's own article — must be excluded from hubs.
        await s.execute(
            text(
                "INSERT INTO app.wiki_links (from_section_id, to_entity_id, to_article_id, anchor,"
                " domain_code) VALUES (:s, :e, :a, 'Solo', 'general')"
            ),
            {"s": own_section, "e": eid, "a": own_article},
        )
    landing = await WikiReadStore(maker).get_landing(OWNER)
    assert not any(h["title"] == "Solo" for h in landing["hubs"])
