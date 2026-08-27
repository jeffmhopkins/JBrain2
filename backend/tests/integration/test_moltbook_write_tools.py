"""jmolt's Moltbook write tools stage into the outbox with the M8/M9/M10 guards, against
real Postgres (the outbox RLS + the tool logic together)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_guards import (
    MAX_COMMENTS_PER_NIGHT,
    MAX_COMMENTS_PER_POST,
    MAX_FOLLOWS_PER_NIGHT,
    MAX_VOTES_PER_NIGHT,
)
from jbrain.agent.loop import ToolContext
from jbrain.agent.moltbookwritetools import build_moltbook_write_handlers
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeSettingsStore

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
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return str(pid)


def _jmolt(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(
        maker, SessionContext(principal_kind="owner", domain_scopes=("jmolt",))
    ) as s:
        await s.execute(text("DELETE FROM app.jmolt_outbox"))
        await s.execute(text("DELETE FROM app.jmolt_action_ledger"))
    yield


def _ctx(pid: str) -> ToolContext:
    return ToolContext(session=_jmolt(pid), scopes=(), timezone="UTC")


# A body that clears MIN_POST_BODY_CHARS — a post needs one, so the tests that exercise the
# OTHER post guards (lint, near-dup) must carry a real body or they'd trip the body check first.
_BODY = "A real body with an actual argument in it, long enough to clear the minimum body length."


async def test_post_stages_into_outbox_with_a_publish_time(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    out = await h["moltbook_post"](
        {"submolt": "general", "title": "the quiet submolts are the good ones", "content": _BODY},
        _ctx(pid),
    )
    assert "Staged" in out
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("queued",))
    assert len(rows) == 1 and rows[0].kind == "post" and rows[0].publish_at is not None


async def test_post_rejects_a_bare_title(maker: async_sessionmaker) -> None:
    # A title with no (or a trivially short) body is not a post — the whole reason for this
    # guard is that a drifted 120B was publishing bare titles with the thesis crammed into the
    # headline. Both an empty and a one-line body are refused, and nothing is staged.
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    empty = await h["moltbook_post"](
        {"submolt": "general", "title": "Coordination dies when every agent needs the transcript"},
        _ctx(pid),
    )
    assert "real body" in empty
    short = await h["moltbook_post"](
        {"submolt": "general", "title": "a headline", "content": "too short"}, _ctx(pid)
    )
    assert "real body" in short
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert await OutboxRepo().list_by_status(s, pid, ("queued",)) == []  # nothing staged


async def test_post_is_blocked_by_content_lint(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    # A real body (so the body guard passes) but the crypto shill is in the title — the lint
    # screens title + body together and still blocks it.
    out = await h["moltbook_post"](
        {"submolt": "general", "title": "buy $MOLT now before the presale", "content": _BODY},
        _ctx(pid),
    )
    assert "blocked" in out
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert await OutboxRepo().list_by_status(s, pid, ("queued",)) == []  # nothing staged


async def test_post_rejects_near_duplicate(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    title = "Three weeks in and the general submolt is still mostly noise and duplicate posts"
    assert "Staged" in await h["moltbook_post"](
        {"submolt": "general", "title": title, "content": _BODY}, _ctx(pid)
    )
    out = await h["moltbook_post"](
        {"submolt": "general", "title": title + " tonight", "content": _BODY}, _ctx(pid)
    )
    assert "too similar" in out


async def test_profile_update_prepends_the_fixed_disclosure(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    store = FakeSettingsStore()
    store.values["moltbook_disclosure"] = "Autonomous experiment; a human reads the logs."
    h = build_moltbook_write_handlers(maker, store)  # type: ignore[arg-type]
    await h["moltbook_profile_update"]({"bio": "I like tide pools."}, _ctx(pid))
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("queued",))
    desc = rows[0].payload["description"]
    assert desc.startswith("Autonomous experiment") and "tide pools" in desc


async def test_comment_stages_and_is_recorded(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    await h["moltbook_comment"](
        {"post_id": "p1", "content": "answering the thing you asked"}, _ctx(pid)
    )
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("queued",))
        ledger = await ActionLedgerRepo().recent(s, pid)
    assert rows[0].kind == "comment"
    assert any(r.action == "stage_comment" for r in ledger)


async def test_a_second_top_level_comment_on_the_same_post_is_refused(
    maker: async_sessionmaker,
) -> None:
    # THE 17-comment regression. jmolt put seventeen comments on one post, nine of them
    # top-level, asking the same question in different words — its own comments are invisible
    # to it when it re-reads the thread, so every read looked like an unanswered question.
    # The nightly cap could not see that shape and the content-hash dedup key never fired:
    # seventeen paraphrases are seventeen distinct hashes.
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    first = await h["moltbook_comment"](
        {"post_id": "p1", "content": "does your owner's script or your own drive decide?"},
        _ctx(pid),
    )
    assert "Staged" in first
    again = await h["moltbook_comment"](
        {"post_id": "p1", "content": "is it the design that steers you, or the context?"},
        _ctx(pid),
    )
    assert "top-level" in again
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("queued",))
    assert len(rows) == 1  # the paraphrase never made it in


async def test_a_threaded_reply_on_the_same_post_is_still_allowed(
    maker: async_sessionmaker,
) -> None:
    # The cap must not ban conversation: answering a specific comment is the thing jmolt is
    # supposed to be good at, and it is exactly what a second OPENING remark is not.
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    await h["moltbook_comment"]({"post_id": "p1", "content": "an opening question"}, _ctx(pid))
    reply = await h["moltbook_comment"](
        {"post_id": "p1", "content": "answering what you said", "parent_id": "c9"}, _ctx(pid)
    )
    assert "Staged" in reply


async def test_comments_are_capped_per_post(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    await h["moltbook_comment"]({"post_id": "p1", "content": "opening remark"}, _ctx(pid))
    for i in range(MAX_COMMENTS_PER_POST - 1):
        out = await h["moltbook_comment"](
            {"post_id": "p1", "content": f"reply {i}", "parent_id": f"c{i}"}, _ctx(pid)
        )
        assert "Staged" in out
    over = await h["moltbook_comment"](
        {"post_id": "p1", "content": "one more", "parent_id": "c99"}, _ctx(pid)
    )
    assert "limit for one post" in over


async def test_the_per_post_cap_does_not_affect_a_different_post(
    maker: async_sessionmaker,
) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    await h["moltbook_comment"]({"post_id": "p1", "content": "opening remark"}, _ctx(pid))
    out = await h["moltbook_comment"]({"post_id": "p2", "content": "a new thread"}, _ctx(pid))
    assert "Staged" in out


async def test_comments_are_capped_per_night(maker: async_sessionmaker) -> None:
    # Comments had no brake — a drifted night once staged 30. The per-night cap holds at
    # stage time; the write past the limit is refused, not queued.
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    for i in range(MAX_COMMENTS_PER_NIGHT):
        out = await h["moltbook_comment"](
            {"post_id": f"p{i}", "content": f"a distinct reply number {i}"}, _ctx(pid)
        )
        assert "Staged" in out
    over = await h["moltbook_comment"](
        {"post_id": "p-over", "content": "one reply too many"}, _ctx(pid)
    )
    assert "nightly limit" in over
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("queued",))
    assert len(rows) == MAX_COMMENTS_PER_NIGHT  # the over-limit one never made it in


async def test_votes_are_capped_per_night(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    for i in range(MAX_VOTES_PER_NIGHT):
        assert "Staged" in await h["moltbook_vote"]({"target_id": f"t{i}"}, _ctx(pid))
    over = await h["moltbook_vote"]({"target_id": "t-over"}, _ctx(pid))
    assert "nightly limit" in over
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert len(await OutboxRepo().list_by_status(s, pid, ("queued",))) == MAX_VOTES_PER_NIGHT


async def test_duplicate_vote_is_deduped(maker: async_sessionmaker) -> None:
    # The re-staged-upvote bug: a fresh sitting can't see its pending queue, so it re-votes.
    # The dedup index makes the repeat a no-op.
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    first = await h["moltbook_vote"]({"target_id": "post-9", "up": True}, _ctx(pid))
    assert "Staged an upvote" in first
    again = await h["moltbook_vote"]({"target_id": "post-9", "up": True}, _ctx(pid))
    assert "already staged" in again
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await OutboxRepo().list_by_status(s, pid, ("queued",))
    assert len(rows) == 1  # the duplicate upvote was swallowed


async def test_duplicate_follow_is_deduped(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    assert "Staged" in await h["moltbook_social"]({"action": "follow", "name": "Luna24"}, _ctx(pid))
    again = await h["moltbook_social"]({"action": "follow", "name": "Luna24"}, _ctx(pid))
    assert "already staged" in again
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert len(await OutboxRepo().list_by_status(s, pid, ("queued",))) == 1


async def test_follows_are_capped_per_night(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    h = build_moltbook_write_handlers(maker, FakeSettingsStore())  # type: ignore[arg-type]
    for i in range(MAX_FOLLOWS_PER_NIGHT):
        staged = await h["moltbook_social"]({"action": "follow", "name": f"agent{i}"}, _ctx(pid))
        assert "Staged" in staged
    over = await h["moltbook_social"]({"action": "follow", "name": "one-too-many"}, _ctx(pid))
    assert "nightly limit" in over
