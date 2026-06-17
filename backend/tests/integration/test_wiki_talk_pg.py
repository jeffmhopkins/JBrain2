"""The wiki Talk board (Phase 6, Wave T1) against real Postgres: owner-only RLS on the two new
tables, the builder's Build-log posts (on build + on a merge redirect), and the read/write store
round-trip. The board is owner-only (mirrors wiki_articles) and served active-only (like the
reader). Uses the deterministic StubRewriter + a faked embed client (no network)."""

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
from jbrain.wiki.talkstore import (
    TalkArticleNotFound,
    TalkBuildLogReadonly,
    TalkTopicNotFound,
    WikiTalkStore,
)
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

TOKEN = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


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


async def _entity(maker: async_sessionmaker, name: str, *, merged_into: str | None = None) -> str:
    eid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code, merged_into_id)"
                " VALUES (:i, 'Person', :n, 'general', :m)"
            ),
            {"i": eid, "n": name, "m": merged_into},
        )
    return eid


async def _fact(maker: async_sessionmaker, entity_id: str, statement: str) -> None:
    note, chunk, fact = (str(uuid.uuid4()) for _ in range(3))
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:i,:c,'general',:b)"
            ),
            {"i": note, "c": note[:12], "b": statement},
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:i,:n,'general','paragraph',0,:b)"
            ),
            {"i": chunk, "n": note, "b": statement},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, assertion,"
                " reported_at, note_id, chunk_id, extractor, prompt_version, domain_code)"
                " VALUES (:i,:e,'p','state',:st,'asserted','2026-01-01T00:00:00Z',:n,:c,'fk','v1',"
                " 'general')"
            ),
            {"i": fact, "e": entity_id, "st": statement, "n": note, "c": chunk},
        )


def _builder(maker: async_sessionmaker) -> WikiBuilder:
    return WikiBuilder(maker, embed=FakeEmbed(), rewriter=StubRewriter(), embedding_model="fake")


async def _article_id(maker: async_sessionmaker, entity_id: str) -> str:
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text("SELECT id FROM app.wiki_articles WHERE entity_ref = :e"), {"e": entity_id}
                )
            ).scalar()
        )


async def _notable(maker: async_sessionmaker, name: str) -> str:
    eid = await _entity(maker, name)
    for i in range(3):
        await _fact(maker, eid, f"{name} fact {i}")
    return eid


# ---- builder Build-log ------------------------------------------------------------------------


async def test_build_posts_a_build_log_entry(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    eid = await _notable(maker, "Celine")
    await _builder(maker).refresh()
    board = await WikiTalkStore(maker).get_board(OWNER, await _article_id(maker, eid))
    assert board is not None
    log = next(t for t in board["topics"] if t["kind"] == "build_log")
    assert log["meta"] == "auto · 1 entries"
    assert log["posts"][0]["author"] == "builder"
    assert log["posts"][0]["rev"] == 1
    assert "Created article" in log["posts"][0]["body"]
    # Domain-neutral: the summary reports counts, never a domain name.
    assert "general" not in log["posts"][0]["body"].lower()


async def test_build_log_find_or_create_is_idempotent(maker: async_sessionmaker) -> None:
    # A second build of the same article appends a second Build-log post to the SAME topic
    # (the partial-unique-index ON CONFLICT prevents a duplicate build_log topic).
    await _owner_pid(maker)
    eid = await _notable(maker, "Dana")
    builder = _builder(maker)
    await builder.refresh()
    await builder.rebuild(await _article_id(maker, eid))
    async with scoped_session(maker, OWNER) as s:
        topics = (
            await s.execute(
                text("SELECT count(*) FROM app.wiki_talk_topics WHERE kind = 'build_log'")
            )
        ).scalar()
        posts = (
            await s.execute(text("SELECT count(*) FROM app.wiki_talk_posts WHERE author='builder'"))
        ).scalar()
    assert topics == 1 and posts == 2


async def test_merge_posts_to_the_survivors_build_log(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    survivor = await _notable(maker, "Globex")
    gone = await _notable(maker, "Initech")
    await _builder(maker).refresh()  # both get articles
    survivor_article = await _article_id(maker, survivor)
    # Fold gone → survivor (sets merged_into_id, which the 0046 trigger dirties), then rebuild.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.entities SET merged_into_id = :s WHERE id = :g"),
            {"s": survivor, "g": gone},
        )
    await _builder(maker).refresh()
    board = await WikiTalkStore(maker).get_board(OWNER, survivor_article)
    assert board is not None
    log = next(t for t in board["topics"] if t["kind"] == "build_log")
    assert any("Merged in Initech" in p["body"] for p in log["posts"])


# ---- RLS isolation (per-new-table requirement) ------------------------------------------------


async def test_talk_tables_are_owner_only(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    eid = await _notable(maker, "Priya")
    await _builder(maker).refresh()  # creates the build_log topic + post as system/owner
    # A capability (non-owner) token sees no topics or posts and cannot write one.
    async with scoped_session(maker, TOKEN) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.wiki_talk_topics"))).scalar() == 0
        assert (await s.execute(text("SELECT count(*) FROM app.wiki_talk_posts"))).scalar() == 0
    aid = await _article_id(maker, eid)
    with pytest.raises(Exception):  # noqa: B017,PT011 — RLS WITH CHECK rejects the non-owner write
        async with scoped_session(maker, TOKEN) as s:
            await s.execute(
                text(
                    "INSERT INTO app.wiki_talk_topics (article_id, kind, title)"
                    " VALUES (:a, 'discussion', 'x')"
                ),
                {"a": aid},
            )


async def test_narrowed_owner_still_reads_talk(maker: async_sessionmaker) -> None:
    # Talk is owner-only (NOT domain-scoped): a narrowed owner still reads the board. This documents
    # the posture — a P7 follow-on would domain-scope Build-log posts; today summaries are neutral.
    pid = await _owner_pid(maker)
    eid = await _notable(maker, "Sam")
    await _builder(maker).refresh()
    board = await WikiTalkStore(maker).get_board(
        read_context(pid, ("health",)), await _article_id(maker, eid)
    )
    assert board is not None
    assert any(t["kind"] == "build_log" for t in board["topics"])


# ---- read/write round-trip --------------------------------------------------------------------


async def test_topic_reply_resolve_round_trip(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    eid = await _notable(maker, "Imogen")
    await _builder(maker).refresh()
    aid = await _article_id(maker, eid)
    store = WikiTalkStore(maker)

    topic = await store.create_topic(OWNER, aid, title="Outdated job", body="She left in March.")
    assert topic["status"] == "open" and topic["posts"][0]["author"] == "owner"
    tid = topic["id"]
    reply = await store.add_reply(OWNER, aid, tid, body="Please fix the Career section.")
    assert reply["author"] == "owner"
    await store.set_status(OWNER, aid, tid, status="resolved")

    board = await store.get_board(OWNER, aid)
    assert board is not None
    disc = next(t for t in board["topics"] if t["id"] == tid)
    assert disc["status"] == "resolved" and len(disc["posts"]) == 2
    # Discussion topics sort before the Build log.
    assert board["topics"][0]["kind"] == "discussion"
    assert board["topics"][-1]["kind"] == "build_log"


async def test_build_log_is_read_only_and_missing_article_404s(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    eid = await _notable(maker, "Tom")
    await _builder(maker).refresh()
    aid = await _article_id(maker, eid)
    store = WikiTalkStore(maker)
    board = await store.get_board(OWNER, aid)
    assert board is not None
    log_id = next(t["id"] for t in board["topics"] if t["kind"] == "build_log")
    with pytest.raises(TalkBuildLogReadonly):
        await store.add_reply(OWNER, aid, log_id, body="nope")
    with pytest.raises(TalkBuildLogReadonly):
        await store.set_status(OWNER, aid, log_id, status="resolved")
    with pytest.raises(TalkArticleNotFound):
        await store.create_topic(OWNER, str(uuid.uuid4()), title="x", body="y")
    with pytest.raises(TalkTopicNotFound):
        await store.add_reply(OWNER, aid, str(uuid.uuid4()), body="y")
    # A merged (non-active) article's board 404s in lockstep with the reader.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.wiki_articles SET status = 'merged' WHERE id = :a"), {"a": aid}
        )
    assert await store.get_board(OWNER, aid) is None
