"""Job queue against real Postgres: claim contention, retry backoff,
idempotent completion, startup backfill, and the system-table RLS policy."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain import queue
from jbrain.db.session import SessionContext, scoped_session
from jbrain.notes.repo import SqlNotesRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SCOPED = SessionContext(principal_kind="capability_token", domain_scopes=("general", "health"))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def job_row(maker: async_sessionmaker[AsyncSession], job_id: str) -> dict:
    async with scoped_session(maker, OWNER) as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, attempts, last_error, run_after > now() AS deferred,"
                    " finished_at FROM app.jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
        ).one()
        return dict(row._mapping)


async def test_enqueue_claim_complete_roundtrip(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    job_id = await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "abc"})

    job = await queue.claim(maker, OWNER)
    assert job is not None
    assert job.id == job_id
    assert job.kind == "ingest_note"
    assert job.payload == {"note_id": "abc"}

    # Already running: nothing else to claim.
    assert await queue.claim(maker, OWNER) is None

    await queue.complete(maker, OWNER, job_id)
    assert (await job_row(maker, job_id))["status"] == "done"

    # Idempotent completion: a duplicate call neither errors nor regresses.
    await queue.complete(maker, OWNER, job_id)
    assert (await job_row(maker, job_id))["status"] == "done"


async def test_concurrent_claims_never_double_claim(
    maker: async_sessionmaker[AsyncSession], database_url: str  # noqa: F811
) -> None:
    job_id = await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "contended"})
    other_engine = create_async_engine(database_url, poolclass=NullPool)
    other_maker = async_sessionmaker(other_engine, expire_on_commit=False)
    try:
        results = await asyncio.gather(
            queue.claim(maker, OWNER), queue.claim(other_maker, OWNER)
        )
        claimed = [j for j in results if j is not None]
        assert len(claimed) == 1  # SKIP LOCKED: exactly one winner
        assert claimed[0].id == job_id
        await queue.complete(maker, OWNER, job_id)
    finally:
        await other_engine.dispose()


async def test_failure_requeues_with_backoff(maker: async_sessionmaker[AsyncSession]) -> None:
    job_id = await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "flaky"})
    job = await queue.claim(maker, OWNER)
    assert job is not None and job.id == job_id

    await queue.fail(maker, OWNER, job_id, "transient boom")
    row = await job_row(maker, job_id)
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["last_error"] == "transient boom"
    assert row["deferred"]  # run_after pushed into the future

    # Backoff means it is not claimable right now.
    assert await queue.claim(maker, OWNER) is None

    # Once the backoff elapses (simulated), it is claimable again.
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.jobs SET run_after = now() WHERE id = :id"), {"id": job_id}
        )
    reclaimed = await queue.claim(maker, OWNER)
    assert reclaimed is not None and reclaimed.id == job_id
    await queue.complete(maker, OWNER, job_id)


async def test_exhausted_attempts_fail_permanently(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    job_id = await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "doomed"})
    for attempt in range(5):  # max_attempts default
        await queue.fail(maker, OWNER, job_id, f"boom {attempt}")
    row = await job_row(maker, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 5
    assert row["finished_at"] is not None
    assert await queue.claim(maker, OWNER) is None


async def test_jobs_are_invisible_outside_the_system_context(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "secret"})

    for ctx in (UNSCOPED, SCOPED):
        async with scoped_session(maker, ctx) as session:
            count = (await session.execute(text("SELECT count(*) FROM app.jobs"))).scalar()
        assert count == 0, "jobs table must be owner-only"

    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        await queue.enqueue(maker, SCOPED, "ingest_note", {"note_id": "forged"})


async def test_backfill_enqueues_pending_notes_exactly_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id="bf-1", domain="general", destination=None, body="left behind"
    )
    indexed, _ = await repo.create_note(
        OWNER, client_id="bf-2", domain="health", destination=None, body="already done"
    )
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.notes SET ingest_state = 'indexed' WHERE id = :id"),
            {"id": indexed.id},
        )
        # Quiesce jobs other tests (or API wiring) may have queued.
        await session.execute(text("UPDATE app.jobs SET status = 'done'"))

    first = await queue.backfill_pending_notes(maker, OWNER)
    assert first >= 1
    async with scoped_session(maker, OWNER) as session:
        targets = set(
            (
                await session.execute(
                    text(
                        "SELECT payload->>'note_id' FROM app.jobs"
                        " WHERE kind = 'ingest_note' AND status = 'queued'"
                    )
                )
            ).scalars()
        )
    assert note.id in targets
    assert indexed.id not in targets  # completed ingestion is not re-enqueued

    # A queued job suppresses duplicates: the second sweep is a no-op.
    assert await queue.backfill_pending_notes(maker, OWNER) == 0
