"""The search wiki leg (Phase 6, Wave C3) against real Postgres: a built article is searchable,
its display identity (title/kind/blurb) comes back, and the RLS firewall holds — a section's
content never ranks or leaks to a session that can't see that section's domain.
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
from jbrain.search.repo import SqlSearchRepo
from jbrain.search.service import SearchService, WikiSearchResult
from jbrain.wiki.builder import StubRewriter, WikiBuilder
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


async def test_wiki_fts_returns_article_identity(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    await _build(
        maker,
        "Priya Nair",
        "Person",
        [("general", f"Priya founded a pediatric clinic {i}") for i in range(3)],
    )
    repo = SqlSearchRepo(maker)
    hits = await repo.wiki_fts_search(OWNER, "pediatric", None, 10)
    assert any(h.title == "Priya Nair" and h.entity_kind == "Person" for h in hits)
    assert all(h.domain == "general" for h in hits)
    assert all(h.blurb for h in hits)  # the lead_summary blurb rides through build→search


async def test_service_returns_wiki_hit_as_headline(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    await _build(
        maker,
        "Globex",
        "Organization",
        [("general", f"Globex ships widgets worldwide {i}") for i in range(3)],
    )
    resp = await SearchService(SqlSearchRepo(maker), FakeEmbed()).search(OWNER, "widgets", None, 20)
    wiki = [r for r in resp.results if isinstance(r, WikiSearchResult)]
    assert any(w.title == "Globex" and w.kind == "wiki" for w in wiki)


async def test_wiki_and_note_hits_coexist_with_the_article_as_headline(
    maker: async_sessionmaker,
) -> None:
    # The article's three source notes also contain "anvils", so a real search returns BOTH a
    # wiki hit (the Acme article) AND note hits — the union, with the article as the headline.
    await _owner_pid(maker)
    await _build(
        maker,
        "Acme",
        "Organization",
        [("general", f"Acme forges anvils daily {i}") for i in range(3)],
    )
    resp = await SearchService(SqlSearchRepo(maker), FakeEmbed()).search(OWNER, "anvils", None, 20)
    kinds = [r.kind for r in resp.results]
    assert kinds[0] == "wiki"  # the article heads the list
    assert "note" in kinds  # ...with the source-note passages beneath
    assert isinstance(resp.results[0], WikiSearchResult)
    assert resp.results[0].title == "Acme"


async def test_health_section_is_hidden_from_a_general_scoped_search(
    maker: async_sessionmaker,
) -> None:
    pid = await _owner_pid(maker)
    # An entity with enough general facts to be notable, plus a distinctive HEALTH fact.
    await _build(
        maker,
        "Sam",
        "Person",
        [
            ("general", "Sam lives in Brookline town"),
            ("general", "Sam works as an architect"),
            ("health", "Sam has a severe penicillin allergy documented"),
        ],
    )
    repo = SqlSearchRepo(maker)
    # The owner finds the health section; a general-narrowed session never sees it (RLS).
    assert len(await repo.wiki_fts_search(OWNER, "penicillin", None, 10)) == 1
    general = read_context(pid, ("general",))
    assert await repo.wiki_fts_search(general, "penicillin", None, 10) == []
    # ...but the same scoped session still finds the general-domain content.
    assert len(await repo.wiki_fts_search(general, "architect", None, 10)) == 1


async def test_non_owner_token_sees_no_wiki_articles(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    await _build(
        maker, "Acme", "Organization", [("general", f"Acme makes anvils {i}") for i in range(3)]
    )
    repo = SqlSearchRepo(maker)
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    # wiki_articles is owner-only (Phase 7 relaxes), so a capability token gets no wiki hits.
    assert await repo.wiki_fts_search(token, "anvils", None, 10) == []
