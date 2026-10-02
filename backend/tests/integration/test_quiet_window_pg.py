"""The engine switch's quiet-window guard (`workflow.scheduler`) against real Postgres: it
reads the schedules the tick fires and the run log, so the window it enforces cannot drift
from the scheduler's own rows (docs/plans/FLASH_NEXT_ENGINE_PLAN.md F3a). Assertions are
about this test's own rows, so a shared database's other schedules and runs never decide
them; the window rule itself is unit-tested (`nightly_window_reason`)."""

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
from jbrain.workflow.scheduler import (
    executing_runs,
    nightly_window_reason,
    quiet_window_guard,
    schedule_windows,
)
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


async def _schedule(
    maker: async_sessionmaker, *, interval: int, next_run_at: datetime, enabled: bool = True
) -> str:
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
            text(
                "INSERT INTO app.triggers (id, on_schedule_id, pipeline, enabled)"
                " VALUES (:id, :s, :p, :en)"
            ),
            {"id": tid, "s": sid, "p": pipeline, "en": enabled},
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


async def test_a_nightly_fire_is_read_with_its_pipeline_label(maker: async_sessionmaker) -> None:
    fire = FAR + timedelta(minutes=15)
    pipeline = await _schedule(maker, interval=86_400, next_run_at=fire)
    try:
        mine = [w for w in await schedule_windows(maker) if w.label == pipeline]
        assert len(mine) == 1 and mine[0].next_run_at == fire
        reason = nightly_window_reason(mine, FAR)
        assert reason is not None and "fires in 15 min" in reason
    finally:
        await _drop(maker, pipeline)


async def test_a_schedule_whose_trigger_is_disabled_is_not_read(
    maker: async_sessionmaker,
) -> None:
    pipeline = await _schedule(
        maker, interval=86_400, next_run_at=FAR + timedelta(minutes=5), enabled=False
    )
    try:
        assert all(w.label != pipeline for w in await schedule_windows(maker))
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
    moment = FAR - timedelta(hours=3)
    try:
        # A queued step whose run_after has come is a run between its steps: it counts.
        assert "quiet_window_run" in await executing_runs(maker, moment)
        # Deferred to later (a precondition waiting on its model) it does not.
        async with scoped_session(maker, queue.SYSTEM_CTX) as s:
            await s.execute(
                text("UPDATE app.jobs SET run_after = :later WHERE id = :id"),
                {"id": job_id, "later": moment + timedelta(hours=1)},
            )
        assert "quiet_window_run" not in await executing_runs(maker, moment)
        async with scoped_session(maker, queue.SYSTEM_CTX) as s:
            await s.execute(
                text("UPDATE app.jobs SET status = 'running' WHERE id = :id"), {"id": job_id}
            )
        assert "quiet_window_run" in await executing_runs(maker, moment)
        assert await quiet_window_guard(maker, moment) is not None
    finally:
        # Run history is append-only for the app role (no DELETE grant on runs/run_steps), so
        # the cleanup finishes the job and the run instead: neither then reads as executing.
        async with scoped_session(maker, queue.SYSTEM_CTX) as s:
            await s.execute(
                text("UPDATE app.jobs SET status = 'done', finished_at = now() WHERE id = :id"),
                {"id": job_id},
            )
            await s.execute(
                text("UPDATE app.runs SET status = 'done', ended_at = now() WHERE id = :r"),
                {"r": run_id},
            )
