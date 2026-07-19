"""Migration 0132 against real Postgres: app.media_analysis_results is owner-only —
a deferred analyze_stream result is jerv chat output (frames, a summary, a transcript),
never domain content, so it carries no domain firewall column and a scoped non-owner
principal can neither read nor write it (CLAUDE.md rule 3; the app.jobs / app.runs
posture). Also exercises the media_results repo round-trip and the sticky-cancel
semantics that make a Stop win over a late completion (DEFERRED_TOOL_CALLS_PLAN.md P2)."""

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import media_results
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A scoped capability token (non-owner): it must not see or write a deferred result row.
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_repo_round_trip_create_progress_complete(maker: async_sessionmaker) -> None:
    rid = await media_results.create(maker, OWNER, session_id="chat-1")
    row = await media_results.get(maker, OWNER, rid)
    assert row is not None and row.status == "running" and row.progress == {} and row.result is None

    await media_results.set_progress(maker, OWNER, rid, step=3, total=8, label="Transcribing 3/8")
    row = await media_results.get(maker, OWNER, rid)
    assert row is not None
    assert row.progress == {"step": 3, "total": 8, "label": "Transcribing 3/8"}

    await media_results.complete(maker, OWNER, rid, result={"summary": "a talk", "mode": "full"})
    row = await media_results.get(maker, OWNER, rid)
    assert row is not None and row.status == "done"
    assert row.result == {"summary": "a talk", "mode": "full"}


async def test_cancel_is_sticky_and_beats_a_late_completion(maker: async_sessionmaker) -> None:
    rid = await media_results.create(maker, OWNER, session_id="chat-1")
    assert await media_results.cancel(maker, OWNER, rid) is True  # a running row cancels

    # A completion arriving after the cancel (the job hadn't noticed yet) is a no-op:
    # the guard is `status='running'`, so the row stays canceled — the Stop wins.
    await media_results.complete(maker, OWNER, rid, result={"summary": "too late"})
    row = await media_results.get(maker, OWNER, rid)
    assert row is not None and row.status == "canceled" and row.result is None

    assert await media_results.cancel(maker, OWNER, rid) is False  # already finished


async def test_claim_resume_is_exactly_once(maker: async_sessionmaker) -> None:
    # The auto-resume claim: a running result can't be claimed, a done one claims exactly
    # once (the guard `resumed_at IS NULL`), and the row remembers it was resumed. This is
    # what keeps a reopen-after-finish resuming while a reload/second tab never re-prompts.
    rid = await media_results.create(maker, OWNER, session_id="chat-1")
    assert await media_results.claim_resume(maker, OWNER, rid) is False  # still running

    await media_results.complete(maker, OWNER, rid, result={"summary": "a talk"})
    assert await media_results.claim_resume(maker, OWNER, rid) is True  # first claim wins
    assert await media_results.claim_resume(maker, OWNER, rid) is False  # already claimed

    row = await media_results.get(maker, OWNER, rid)
    assert row is not None and row.resumed_at is not None


async def test_pending_resumes_lists_unclaimed_done_for_the_session(
    maker: async_sessionmaker,
) -> None:
    # The backstop's sweep: only DONE + unclaimed results for THIS session, oldest first.
    # A running one, an already-claimed one, and another session's are all excluded.
    # A session id unique to this test — the container is shared across tests, so a generic
    # "chat-1" would pick up other tests' leftover done rows.
    sid = "chat-pending-sweep"
    done_a = await media_results.create(maker, OWNER, session_id=sid)
    await media_results.complete(maker, OWNER, done_a, result={"resume_message": "A"})
    running = await media_results.create(maker, OWNER, session_id=sid)  # not done
    claimed = await media_results.create(maker, OWNER, session_id=sid)
    await media_results.complete(maker, OWNER, claimed, result={"resume_message": "C"})
    assert await media_results.claim_resume(maker, OWNER, claimed) is True  # taken already
    other = await media_results.create(maker, OWNER, session_id="chat-other")  # other session
    await media_results.complete(maker, OWNER, other, result={"resume_message": "X"})

    pending = await media_results.pending_resumes(maker, OWNER, sid)
    assert [r.id for r in pending] == [done_a]
    assert (pending[0].result or {}).get("resume_message") == "A"
    assert running not in {r.id for r in pending}

    # Once claimed, it drops out of the pending sweep (exactly-once for the backstop too).
    assert await media_results.claim_resume(maker, OWNER, done_a) is True
    assert await media_results.pending_resumes(maker, OWNER, sid) == []


async def test_scoped_principal_cannot_claim_a_resume(maker: async_sessionmaker) -> None:
    # RLS: a scoped non-owner can't win (or even see) the claim — it matches no row, so the
    # owner's later claim still wins and the resume isn't stolen or blocked.
    rid = await media_results.create(maker, OWNER, session_id="chat-1")
    await media_results.complete(maker, OWNER, rid, result={"summary": "a talk"})
    assert await media_results.claim_resume(maker, GENERAL_ONLY, rid) is False
    assert await media_results.claim_resume(maker, OWNER, rid) is True  # owner still claims


async def test_scoped_principal_cannot_read_a_result(maker: async_sessionmaker) -> None:
    # RLS hides the owner's result from a scoped non-owner: the row is simply invisible,
    # not an error — get() returns None even though the owner sees it.
    rid = await media_results.create(maker, OWNER, session_id="chat-1")
    assert await media_results.get(maker, OWNER, rid) is not None
    assert await media_results.get(maker, GENERAL_ONLY, rid) is None


async def test_scoped_principal_cannot_write_a_result(maker: async_sessionmaker) -> None:
    # The WITH CHECK on the owner policy rejects a scoped INSERT outright.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.media_analysis_results (id, session_id)"
                    " VALUES (gen_random_uuid(), 'chat-1')"
                )
            )


async def test_scoped_update_fails_closed(maker: async_sessionmaker) -> None:
    # A scoped UPDATE matches no owner-writable rows: it silently affects nothing rather
    # than mutating a deferred result it should not even see.
    rid = await media_results.create(maker, OWNER, session_id="chat-1")
    async with scoped_session(maker, GENERAL_ONLY) as s:
        result = await s.execute(
            text("UPDATE app.media_analysis_results SET status = 'done' WHERE id = :id"),
            {"id": rid},
        )
        assert cast(CursorResult[Any], result).rowcount == 0
    row = await media_results.get(maker, OWNER, rid)
    assert row is not None and row.status == "running"  # untouched
