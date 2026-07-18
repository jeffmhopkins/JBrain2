"""The wiki Talk board (Phase 6, Wave T1) against real Postgres: owner-only RLS on the two new
tables, the builder's Build-log posts (on build + on a merge redirect), and the read/write store
round-trip. The board is owner-only (mirrors wiki_articles) and served active-only (like the
reader). Uses the deterministic StubRewriter + a faked embed client (no network)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.externaltools import build_external_handlers
from jbrain.agent.hurricanetools import build_hurricane_handlers
from jbrain.agent.readtools import build_registry
from jbrain.agent.session import read_context
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.agent.weatherhistorytools import build_weather_history_handlers
from jbrain.agent.weathertools import build_weather_handlers
from jbrain.agent.webtools import build_web_handlers
from jbrain.agent.wikiwritetools import build_wiki_write_handlers
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.connectors.base import ConnectorRegistry
from jbrain.connectors.medical import medical_connectors
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import LlmTurn, LlmUsage, ToolCall
from jbrain.notes.repo import SqlNotesRepo
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
from jbrain.wiki.builder import StubRewriter, WikiBuilder
from jbrain.wiki.editor import run_editor_turn
from jbrain.wiki.readstore import WikiReadStore
from jbrain.wiki.talkstore import (
    TalkArticleNotFound,
    TalkBuildLogReadonly,
    TalkEditorConflict,
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
    # A second build of the same article reuses the SAME build_log topic and appends a second
    # builder post (SELECT-first find-or-create). Counts are scoped to THIS article — the module
    # shares one database across tests, so a global count would see sibling tests' build logs.
    await _owner_pid(maker)
    eid = await _notable(maker, "Dana")
    builder = _builder(maker)
    await builder.refresh()
    aid = await _article_id(maker, eid)
    await builder.rebuild(aid)
    async with scoped_session(maker, OWNER) as s:
        topics = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.wiki_talk_topics"
                    " WHERE article_id = :a AND kind = 'build_log'"
                ),
                {"a": aid},
            )
        ).scalar()
        posts = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.wiki_talk_posts p"
                    " JOIN app.wiki_talk_topics t ON t.id = p.topic_id"
                    " WHERE t.article_id = :a AND p.author = 'builder'"
                ),
                {"a": aid},
            )
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


# ---- T2: the Editor turn's store seam (idempotency + editor posts) ----------------------------


async def test_editor_idempotency_and_post(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    eid = await _notable(maker, "Edie")
    await _builder(maker).refresh()
    aid = await _article_id(maker, eid)
    store = WikiTalkStore(maker)
    topic = await store.create_topic(OWNER, aid, title="Wrong job", body="She left.")
    tid = topic["id"]
    first_post = topic["posts"][0]["id"]

    # The latest post is the owner reply → the Editor turn is allowed; context comes back.
    topic_title, article_title, posts = await store.topic_for_editor(OWNER, aid, tid, first_post)
    assert topic_title == "Wrong job" and article_title == "Edie" and len(posts) == 1

    # A stale after_post_id (not the latest) → 409 conflict (double-tap / retry guard).
    with pytest.raises(TalkEditorConflict):
        await store.topic_for_editor(OWNER, aid, tid, str(uuid.uuid4()))

    # The Editor posts its reply; it shows as an 'editor' post with the outcome chip.
    ed = await store.add_editor_post(
        OWNER, aid, tid, body="It cites one note.", outcome="correction filed → rebuild queued"
    )
    assert ed["author"] == "editor" and ed["outcome"].startswith("correction filed")

    # Now the editor post is the latest, so re-running against the original owner post 409s — the
    # turn never double-fires for the same owner reply.
    with pytest.raises(TalkEditorConflict):
        await store.topic_for_editor(OWNER, aid, tid, first_post)

    board = await store.get_board(OWNER, aid)
    assert board is not None
    disc = next(t for t in board["topics"] if t["id"] == tid)
    assert [p["author"] for p in disc["posts"]] == ["owner", "editor"]


async def test_editor_turn_refused_on_build_log_and_inactive(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    eid = await _notable(maker, "Edwin")
    await _builder(maker).refresh()
    aid = await _article_id(maker, eid)
    store = WikiTalkStore(maker)
    board = await store.get_board(OWNER, aid)
    assert board is not None
    log_id = next(t["id"] for t in board["topics"] if t["kind"] == "build_log")
    with pytest.raises(TalkBuildLogReadonly):
        await store.topic_for_editor(OWNER, aid, log_id, str(uuid.uuid4()))
    with pytest.raises(TalkArticleNotFound):
        await store.topic_for_editor(OWNER, str(uuid.uuid4()), log_id, str(uuid.uuid4()))


class _FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, ctx: object, kind: str, payload: dict, **_kw: object) -> str:
        self.enqueued.append((kind, payload))
        return "job-1"


def _editor_registry(maker: async_sessionmaker, jobs: _FakeJobs) -> ToolRegistry:
    # The full registry, but only file_correction is exercised by the scripted turn; the unused
    # services are inert stubs (only build_connector_handlers touches its arg at construction).
    notes = SqlNotesRepo(maker)
    stub: Any = object()
    return build_registry(
        stub,  # search
        notes,
        stub,  # entities
        stub,  # memory
        stub,  # proposals
        ConnectorRegistry(medical_connectors("http://x", "http://y")),
        stub,  # lists
        stub,  # appointments
        WikiReadStore(maker),
        build_wiki_write_handlers(notes, jobs, maker),  # type: ignore[arg-type]
        stub,  # location repo
        stub,  # device repo
        {
            **build_web_handlers(SearxngClient(""), WebFetcher()),
            **build_weather_handlers(WeatherClient("", ""), stub),
            **build_weather_history_handlers(WeatherHistoryClient(""), WeatherClient("", ""), stub),
            **build_hurricane_handlers(
                HurricaneClient(""),
                WeatherClient("", ""),
                stub,
                NhcGisClient(""),
                NwsClient(""),
                NhcSurgeClient(""),
            ),
            **build_external_handlers(stub, stub),  # the external-corpus sidecars' handlers
        },  # unused by the editor turn
        stub,  # city geocoder
        maker,  # sessionmaker for query_server_metrics
        # search_external/check_channel are non-optional sidecars, so a full registry
        # build must supply their handlers (stubs — the editor turn never calls them).
        external_handlers=build_external_handlers(stub, stub),
    )


async def test_editor_turn_files_a_correction_end_to_end(maker: async_sessionmaker) -> None:
    # The headline T2 path: a scripted agent turn calls file_correction (in-scope domain), which
    # actually creates the owner_correction note; run_editor_turn returns the chip + prose.
    await _owner_pid(maker)
    eid = await _notable(maker, "Edmund")
    await _builder(maker).refresh()
    aid = await _article_id(maker, eid)

    jobs = _FakeJobs()
    registry = _editor_registry(maker, jobs)
    fake = FakeLlmClient(
        turns=[
            LlmTurn(
                text="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="file_correction",
                        arguments={
                            "body": "She left Globex in March 2026.",
                            "domain": "general",
                            "article_id": aid,
                        },
                    ),
                ),
                stop_reason="tool_use",
                usage=LlmUsage(1, 1),
            ),
            LlmTurn(
                text="Filed your correction; the article will rebuild.",
                tool_calls=(),
                stop_reason="end_turn",
                usage=LlmUsage(1, 1),
            ),
        ]
    )
    router = LlmRouter({"xai": fake}, {"agent.turn": ("xai", "m")})

    reply = await run_editor_turn(
        router,
        registry,
        OWNER,
        article_id=aid,
        article_title="Edmund",
        topic_title="Outdated",
        posts=[{"author": "owner", "body": "she left globex"}],
    )
    assert reply is not None
    assert reply.outcome == "correction filed → rebuild queued"
    assert "Filed" in reply.body
    # The lever actually fired: an owner_correction note was created + an ingest job queued.
    assert any(kind == "ingest_note" for kind, _ in jobs.enqueued)
    async with scoped_session(maker, OWNER) as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM app.notes WHERE provenance = 'owner_correction'")
            )
        ).scalar()
    assert count == 1
