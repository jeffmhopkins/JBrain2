"""Migration 0045 against real Postgres: the Phase-6 wiki spine firewall (CLAUDE.md rule 3).

- `wiki_articles` is the owner-visible cross-domain shell (owner sees; non-owner none).
- `wiki_sections` is the firewall unit: owner + single in-scope domain; a narrowed session
  sees only its domain's sections and cannot create outside scope; a non-owner sees none.
- `wiki_revisions` inherit their section's visibility (the EXISTS policy).
- `wiki_index` is domain-narrowed like sections.
- `wiki_source_exclusions` is owner + domain-scoped.
- a subsection's domain must equal its parent's (the BEFORE trigger).
- `notes.wiki_built` defaults false (the dirty bit).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


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


async def _article(maker: async_sessionmaker, tag: str) -> str:
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.wiki_articles (title, slug) VALUES (:t, :sl) RETURNING id"
                    ),
                    {"t": tag, "sl": f"{tag}-{uuid.uuid4().hex[:6]}"},
                )
            ).scalar()
        )


async def _section(maker: async_sessionmaker, article_id: str, code: str, ctx=OWNER) -> str:
    async with scoped_session(maker, ctx) as s:
        return str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.wiki_sections (article_id, domain_code)"
                        " VALUES (:a, :c) RETURNING id"
                    ),
                    {"a": article_id, "c": code},
                )
            ).scalar()
        )


async def test_articles_owner_only(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    tag = uuid.uuid4().hex[:8]
    await _article(maker, tag)
    like = {"t": f"{tag}%"}
    async with scoped_session(maker, OWNER) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.wiki_articles WHERE title LIKE :t"), like
            )
        ).scalar() == 1
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker, token) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.wiki_articles WHERE title LIKE :t"), like
            )
        ).scalar() == 0


async def test_sections_domain_narrowed(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    tag = uuid.uuid4().hex[:8]
    aid = await _article(maker, tag)
    for code in ("general", "health", "finance"):
        await _section(maker, aid, code)
    q = text("SELECT domain_code FROM app.wiki_sections WHERE article_id = :a")

    async with scoped_session(maker, read_context(pid, ("health",))) as s:
        assert list((await s.execute(q, {"a": aid})).scalars()) == ["health"]
    async with scoped_session(maker, OWNER) as s:
        assert len(list((await s.execute(q, {"a": aid})).scalars())) == 3
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    async with scoped_session(maker, token) as s:
        assert len(list((await s.execute(q, {"a": aid})).scalars())) == 0


async def test_narrowed_owner_cannot_create_section_outside_scope(
    maker: async_sessionmaker,
) -> None:
    pid = await _owner_pid(maker)
    aid = await _article(maker, uuid.uuid4().hex[:8])
    with pytest.raises(ProgrammingError):
        await _section(maker, aid, "finance", ctx=read_context(pid, ("health",)))


async def test_revisions_follow_their_section(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    aid = await _article(maker, uuid.uuid4().hex[:8])
    sid = await _section(maker, aid, "health")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("INSERT INTO app.wiki_revisions (section_id, seq, body) VALUES (:s, 1, 'body')"),
            {"s": sid},
        )
    cnt = text("SELECT count(*) FROM app.wiki_revisions WHERE section_id = :s")
    async with scoped_session(maker, read_context(pid, ("general",))) as s:
        assert (await s.execute(cnt, {"s": sid})).scalar() == 0
    async with scoped_session(maker, read_context(pid, ("health",))) as s:
        assert (await s.execute(cnt, {"s": sid})).scalar() == 1


async def test_index_domain_narrowed(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    aid = await _article(maker, uuid.uuid4().hex[:8])
    sid = await _section(maker, aid, "finance")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.wiki_index (section_id, domain_code, summary)"
                " VALUES (:s, 'finance', 'sum')"
            ),
            {"s": sid},
        )
    cnt = text("SELECT count(*) FROM app.wiki_index WHERE section_id = :s")
    async with scoped_session(maker, read_context(pid, ("general",))) as s:
        assert (await s.execute(cnt, {"s": sid})).scalar() == 0
    async with scoped_session(maker, read_context(pid, ("finance",))) as s:
        assert (await s.execute(cnt, {"s": sid})).scalar() == 1


async def test_subsection_must_inherit_parent_domain(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    aid = await _article(maker, uuid.uuid4().hex[:8])
    parent = await _section(maker, aid, "health")
    # A finance subsection under a health parent is rejected by the BEFORE trigger.
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.wiki_sections (article_id, domain_code, parent_section_id)"
                    " VALUES (:a, 'finance', :p)"
                ),
                {"a": aid, "p": parent},
            )
    # A health subsection under the health parent is fine.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.wiki_sections (article_id, domain_code, parent_section_id)"
                " VALUES (:a, 'health', :p)"
            ),
            {"a": aid, "p": parent},
        )


async def test_subsection_trigger_holds_under_a_narrowed_session(maker: async_sessionmaker) -> None:
    # The firewall-critical case: under a HEALTH-narrowed session the parent SELECT is
    # RLS-filtered, so an INVOKER trigger would see NULL and let a cross-domain child through.
    # The SECURITY DEFINER trigger must still see the finance parent and reject.
    pid = await _owner_pid(maker)
    aid = await _article(maker, uuid.uuid4().hex[:8])
    finance_parent = await _section(maker, aid, "finance")  # created as full OWNER
    health = read_context(pid, ("health",))
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, health) as s:
            await s.execute(
                text(
                    "INSERT INTO app.wiki_sections (article_id, domain_code, parent_section_id)"
                    " VALUES (:a, 'health', :p)"
                ),
                {"a": aid, "p": finance_parent},
            )


async def test_subsection_trigger_holds_on_update_reparent(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    aid = await _article(maker, uuid.uuid4().hex[:8])
    finance_parent = await _section(maker, aid, "finance")
    child = await _section(maker, aid, "health")
    # Re-parenting a health child under a finance parent must be rejected on UPDATE.
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text("UPDATE app.wiki_sections SET parent_section_id = :p WHERE id = :c"),
                {"p": finance_parent, "c": child},
            )


async def test_cannot_change_domain_of_section_with_subsections(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    aid = await _article(maker, uuid.uuid4().hex[:8])
    parent = await _section(maker, aid, "health")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.wiki_sections (article_id, domain_code, parent_section_id)"
                " VALUES (:a, 'health', :p)"
            ),
            {"a": aid, "p": parent},
        )
    # Changing the parent's domain would orphan the subtree into a mismatch — rejected.
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text("UPDATE app.wiki_sections SET domain_code = 'finance' WHERE id = :p"),
                {"p": parent},
            )


async def test_index_domain_must_match_section(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    aid = await _article(maker, uuid.uuid4().hex[:8])
    health_section = await _section(maker, aid, "health")
    # A mislabeled index row (general for a health section) must be rejected by the trigger,
    # or it would make the health embedding rankable by a general-scoped ANN query.
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.wiki_index (section_id, domain_code, summary)"
                    " VALUES (:s, 'general', 'sum')"
                ),
                {"s": health_section},
            )


async def test_non_owner_cannot_write(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    aid = await _article(maker, uuid.uuid4().hex[:8])
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    # A non-owner capability token can neither create an article nor a section (WITH CHECK).
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, token) as s:
            await s.execute(
                text("INSERT INTO app.wiki_articles (title, slug) VALUES ('x', 'x-tok')")
            )
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, token) as s:
            await s.execute(
                text(
                    "INSERT INTO app.wiki_sections (article_id, domain_code) VALUES (:a, 'general')"
                ),
                {"a": aid},
            )


async def test_source_exclusions_owner_and_domain(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    nid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, ingest_state,"
                " integration_state) VALUES (:i, :c, 'health', 'b', 'indexed', 'integrated')"
            ),
            {"i": nid, "c": nid[:12]},
        )
        await s.execute(
            text(
                "INSERT INTO app.wiki_source_exclusions (note_id, domain_code, reason)"
                " VALUES (:n, 'health', 'noisy')"
            ),
            {"n": nid},
        )
    cnt = text("SELECT count(*) FROM app.wiki_source_exclusions WHERE note_id = :n")
    async with scoped_session(maker, read_context(pid, ("general",))) as s:
        assert (await s.execute(cnt, {"n": nid})).scalar() == 0
    async with scoped_session(maker, read_context(pid, ("health",))) as s:
        assert (await s.execute(cnt, {"n": nid})).scalar() == 1
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    async with scoped_session(maker, token) as s:
        assert (await s.execute(cnt, {"n": nid})).scalar() == 0


async def test_exactly_one_exclusion_target(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    with pytest.raises(DBAPIError):  # the CHECK: exactly one of note_id / fact_id
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.wiki_source_exclusions (domain_code, reason)"
                    " VALUES ('general', 'neither target')"
                )
            )


async def test_notes_wiki_built_defaults_false(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    nid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, ingest_state,"
                " integration_state) VALUES (:i, :c, 'general', 'b', 'indexed', 'integrated')"
            ),
            {"i": nid, "c": nid[:12]},
        )
        built = (
            await s.execute(text("SELECT wiki_built FROM app.notes WHERE id = :i"), {"i": nid})
        ).scalar()
    assert built is False
