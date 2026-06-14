"""Job queue against real Postgres: claim contention, retry backoff,
idempotent completion, startup backfill, and the system-table RLS policy."""

import asyncio
import uuid
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
    maker: async_sessionmaker[AsyncSession],
    database_url: str,  # noqa: F811
) -> None:
    job_id = await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "contended"})
    other_engine = create_async_engine(database_url, poolclass=NullPool)
    other_maker = async_sessionmaker(other_engine, expire_on_commit=False)
    try:
        results = await asyncio.gather(queue.claim(maker, OWNER), queue.claim(other_maker, OWNER))
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
    # fail() reports exhaustion only on the attempt that burns the budget —
    # the signal the worker's OCR fallback keys on.
    for attempt in range(4):
        assert await queue.fail(maker, OWNER, job_id, f"boom {attempt}") is False
    assert await queue.fail(maker, OWNER, job_id, "boom 4") is True
    row = await job_row(maker, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 5
    assert row["finished_at"] is not None
    assert await queue.claim(maker, OWNER) is None


async def test_permanent_fail_reports_exhaustion_immediately(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    job_id = await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "hopeless"})
    assert await queue.fail(maker, OWNER, job_id, "no retry can help", permanent=True) is True
    assert (await job_row(maker, job_id))["status"] == "failed"
    # A vanished row is not exhaustion: no fallback may fire for it.
    assert await queue.fail(maker, OWNER, str(uuid.uuid4()), "gone") is False


async def test_has_active_statuses_filter(maker: async_sessionmaker[AsyncSession]) -> None:
    await quiesce_jobs(maker)
    job_id = await queue.enqueue(maker, OWNER, "integrate_note", {"note_id": "dedup-1"})
    kwargs: dict = {"payload_field": "note_id", "value": "dedup-1"}
    assert await queue.has_active(maker, OWNER, "integrate_note", **kwargs) is True
    assert (
        await queue.has_active(maker, OWNER, "integrate_note", statuses=("queued",), **kwargs)
        is True
    )

    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.jobs SET status = 'running' WHERE id = :id"), {"id": job_id}
        )
    # The ingest gate's queued-only dedup must NOT see a running job...
    assert (
        await queue.has_active(maker, OWNER, "integrate_note", statuses=("queued",), **kwargs)
        is False
    )
    # ...while the API's default in-flight guard still does.
    assert await queue.has_active(maker, OWNER, "integrate_note", **kwargs) is True


async def test_has_active_ocr_for_note_spans_the_notes_attachments(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await quiesce_jobs(maker)
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id="ocrgate-1", domain="general", destination=None, body="img"
    )
    other, _ = await repo.create_note(
        OWNER, client_id="ocrgate-2", domain="general", destination=None, body="no img"
    )
    att = await repo.add_attachment(
        OWNER,
        note_id=note.id,
        sha256="ab" * 32,
        filename="x.png",
        media_type="image/png",
        size_bytes=4,
    )
    assert att is not None
    assert await queue.has_active_ocr_for_note(maker, OWNER, note.id) is False

    job_id = await queue.enqueue(maker, OWNER, "ocr_attachment", {"attachment_id": att.id})
    assert await queue.has_active_ocr_for_note(maker, OWNER, note.id) is True
    assert await queue.has_active_ocr_for_note(maker, OWNER, other.id) is False

    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.jobs SET status = 'failed' WHERE id = :id"), {"id": job_id}
        )
    # Failed/done jobs are not outstanding work — the gate must open.
    assert await queue.has_active_ocr_for_note(maker, OWNER, note.id) is False


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


async def quiesce_jobs(maker: async_sessionmaker[AsyncSession]) -> None:
    """Park jobs left queued by earlier tests so claim() sees only ours."""
    async with scoped_session(maker, OWNER) as session:
        await session.execute(text("UPDATE app.jobs SET status = 'done'"))


async def backdate_lock(maker: async_sessionmaker[AsyncSession], job_id: str, minutes: int) -> None:
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "UPDATE app.jobs SET locked_at = now() - make_interval(mins => :m) WHERE id = :id"
            ),
            {"id": job_id, "m": minutes},
        )


async def test_stale_running_job_is_reclaimed_at_attempt_cost(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await quiesce_jobs(maker)
    job_id = await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "stuck"})
    first = await queue.claim(maker, OWNER)
    assert first is not None and first.attempts == 0

    # A freshly locked running job is not reclaimable.
    assert await queue.claim(maker, OWNER) is None

    await backdate_lock(maker, job_id, 11)
    reclaimed = await queue.claim(maker, OWNER)
    assert reclaimed is not None and reclaimed.id == job_id
    assert reclaimed.attempts == 1  # the reclaim cost an attempt
    assert (await job_row(maker, job_id))["status"] == "running"
    await queue.complete(maker, OWNER, job_id)


async def test_stale_reclaim_exhaustion_fails_permanently(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await quiesce_jobs(maker)
    job_id = await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "poison"})
    assert await queue.claim(maker, OWNER) is not None
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.jobs SET attempts = 4 WHERE id = :id"), {"id": job_id}
        )
    await backdate_lock(maker, job_id, 11)

    # The reclaim would be attempt 5 of 5: fail it instead of re-running.
    assert await queue.claim(maker, OWNER) is None
    row = await job_row(maker, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 5
    assert row["finished_at"] is not None


async def test_concurrent_stale_reclaims_have_one_winner(
    maker: async_sessionmaker[AsyncSession],
    database_url: str,  # noqa: F811
) -> None:
    await quiesce_jobs(maker)
    job_id = await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": "contended-stale"})
    assert await queue.claim(maker, OWNER) is not None
    await backdate_lock(maker, job_id, 11)

    other_engine = create_async_engine(database_url, poolclass=NullPool)
    other_maker = async_sessionmaker(other_engine, expire_on_commit=False)
    try:
        results = await asyncio.gather(queue.claim(maker, OWNER), queue.claim(other_maker, OWNER))
        claimed = [j for j in results if j is not None]
        assert len(claimed) == 1  # SKIP LOCKED protects the reaper path too
        assert claimed[0].id == job_id and claimed[0].attempts == 1
        await queue.complete(maker, OWNER, job_id)
    finally:
        await other_engine.dispose()


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


async def test_backfill_unembedded_notes_targets_null_embeddings_once(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlNotesRepo(maker)
    bare, _ = await repo.create_note(
        OWNER, client_id="emb-1", domain="general", destination=None, body="no vectors yet"
    )
    done, _ = await repo.create_note(
        OWNER, client_id="emb-2", domain="general", destination=None, body="already embedded"
    )
    planted = "[" + ",".join(["1.0"] + ["0.0"] * 383) + "]"
    async with scoped_session(maker, OWNER) as session:
        for note_id, embedding in ((bare.id, None), (done.id, planted)):
            await session.execute(
                text(
                    "INSERT INTO app.chunks"
                    " (id, note_id, domain_code, granularity, seq, text, embedding)"
                    " VALUES (gen_random_uuid(), :nid, 'general', 'paragraph', 0, 'c',"
                    "         cast(:emb AS vector))"
                ),
                {"nid": note_id, "emb": embedding},
            )
    await quiesce_jobs(maker)

    assert await queue.backfill_unembedded_notes(maker, OWNER) == 1
    async with scoped_session(maker, OWNER) as session:
        targets = list(
            (
                await session.execute(
                    text(
                        "SELECT payload->>'note_id' FROM app.jobs"
                        " WHERE kind = 'embed_note' AND status = 'queued'"
                    )
                )
            ).scalars()
        )
    assert targets == [bare.id]  # fully embedded notes are left alone

    # The queued job suppresses duplicates on the next sweep.
    assert await queue.backfill_unembedded_notes(maker, OWNER) == 0

