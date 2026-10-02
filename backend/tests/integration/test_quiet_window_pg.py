"""The engine switch's quiet-window guard (`workflow.scheduler.quiet_window_guard`) against
real Postgres: it reads the schedules the tick fires and the run log, so the window it
enforces cannot drift from the scheduler's own rows (docs/plans/FLASH_NEXT_ENGINE_PLAN.md
F3a). The moments are far in the future so the migration-seeded nightly rows never fall
inside them."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain import queue
from jbrain.db.session import scoped_session
from jbrain.workflow.runlog import EnqueuedStep, PipelineRunLog
from jbrain.workflow.scheduler import quiet_window_guard
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

FAR = datetime(2099, 3, 1, 1, 45, tzinfo=UTC)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _schedule(maker: async_sessionmaker, *, interval: int, next_run_at: datetime) -> str:
    sid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    pipeline = f"quiet_window_{sid[:8]}"
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        await s.execute(
            text("INSERT INTO app.pipelines (name, version, steps) VALUES (:n, 1, '[]'::jsonb)"),
            {"n": pipeline},
        )
        await s.execute(
            text(
                "INSERT INTO app.schedules (id, interval_seconds, next_run_at, enabled)"
                " VALUES (:id, :iv, :nr, true)"
            ),
            {"id": sid, "iv": interval, "nr": next_run_at},
        )
        await s.execute(
            text("INSERT INTO app.triggers (id, on_schedule_id, pipeline) VALUES (:id, :s, :p)"),
            {"id": tid, "s": sid, "p": pipeline},
        )
    return pipeline


async def _drop(maker: async_sessionmaker, pipeline: str) -> None:
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        sids = (
            (
                await s.execute(
                    text("SELECT on_schedule_id FROM app.triggers WHERE pipeline = :p"),
                    {"p": pipeline},
                )
            )
            .scalars()
            .all()
        )
        await s.execute(text("DELETE FROM app.triggers WHERE pipeline = :p"), {"p": pipeline})
        for sid in sids:
            await s.execute(text("DELETE FROM app.schedules WHERE id = :id"), {"id": sid})


async def test_a_nightly_fire_within_the_lead_is_the_reason(maker: async_sessionmaker) -> None:
    pipeline = await _schedule(maker, interval=86_400, next_run_at=FAR + timedelta(minutes=15))
    try:
        reason = await quiet_window_guard(maker, FAR)
        assert reason is not None and pipeline in reason and "fires in 15 min" in reason
        assert await quiet_window_guard(maker, FAR - timedelta(hours=3)) is None
    finally:
        await _drop(maker, pipeline)


async def test_a_sub_day_interval_never_blocks(maker: async_sessionmaker) -> None:
    pipeline = await _schedule(maker, interval=300, next_run_at=FAR + timedelta(minutes=2))
    try:
        assert await quiet_window_guard(maker, FAR) is None
    finally:
        await _drop(maker, pipeline)


async def test_an_executing_pipeline_run_is_the_reason(maker: async_sessionmaker) -> None:
    job_id = await queue.enqueue(maker, queue.SYSTEM_CTX, "purge_deleted", {})
    run_id = await PipelineRunLog(maker).record(
        queue.SYSTEM_CTX,
        pipeline="quiet_window_run",
        trigger_id=None,
        ran_as="system",
        domain_code=None,
        principal_id=None,
        steps=[EnqueuedStep(kind="purge_deleted", job_id=job_id)],
    )
    try:
        # Queued (a deferred precondition, say) does not count: nothing is executing.
        assert await quiet_window_guard(maker, FAR - timedelta(hours=3)) is None
        async with scoped_session(maker, queue.SYSTEM_CTX) as s:
            await s.execute(
                text("UPDATE app.jobs SET status = 'running' WHERE id = :id"), {"id": job_id}
            )
        reason = await quiet_window_guard(maker, FAR - timedelta(hours=3))
        assert reason is not None and "quiet_window_run" in reason
    finally:
        async with scoped_session(maker, queue.SYSTEM_CTX) as s:
            await s.execute(text("DELETE FROM app.run_steps WHERE run_id = :r"), {"r": run_id})
            await s.execute(text("DELETE FROM app.runs WHERE id = :r"), {"r": run_id})
            await s.execute(text("DELETE FROM app.jobs WHERE id = :id"), {"id": job_id})
