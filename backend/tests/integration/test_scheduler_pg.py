"""The scheduler tick + emergency-trigger fire against real Postgres
(docs/WORKFLOW_ENGINE_PLAN.md §5 Track B): a due schedule claimed SKIP-LOCKED
enqueues its bound pipeline's action onto app.jobs and advances next_run_at
app-side, and the seeded nightly sweeps (migration 0037) are fireable on demand.

The clock is injected (a frozen `now`) so the advance is deterministic — no real
timer — exactly as the unit test does, but here against the real claim query and
the real app.jobs insert."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain import queue
from jbrain.db.session import scoped_session
from jbrain.workflow.registry import ACTION_SPECS, build_registry
from jbrain.workflow.scheduler import PURGE_ACTION, fire_trigger, scheduler_tick
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

NOW = datetime(2026, 6, 15, 2, 0, tzinfo=UTC)


def _registry():  # noqa: ANN202
    return build_registry((*ACTION_SPECS, PURGE_ACTION))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_schedule(
    maker: async_sessionmaker,
    *,
    action: str,
    next_run_at: datetime,
    enabled: bool = True,
    manual: bool = True,
) -> dict[str, str]:
    """A schedule + its bound schedule-trigger + a one-action pipeline. Fresh ids
    per call so tests never collide on the seeded nightly rows."""
    ids = {k: str(uuid.uuid4()) for k in ("schedule", "trigger")}
    pipeline = f"test_pipeline_{ids['schedule'][:8]}"
    ids["pipeline"] = pipeline
    steps = f'[{{"action": "{action}", "action_version": 1, "params": {{}}}}]'
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.pipelines (name, version, steps)"
                " VALUES (:n, 1, cast(:st AS jsonb))"
            ),
            {"n": pipeline, "st": steps},
        )
        await s.execute(
            text(
                "INSERT INTO app.schedules (id, interval_seconds, next_run_at, enabled)"
                " VALUES (:id, 86400, :nr, :en)"
            ),
            {"id": ids["schedule"], "nr": next_run_at, "en": enabled},
        )
        await s.execute(
            text(
                "INSERT INTO app.triggers (id, on_schedule_id, pipeline, manual)"
                " VALUES (:id, :sid, :p, :m)"
            ),
            {"id": ids["trigger"], "sid": ids["schedule"], "p": pipeline, "m": manual},
        )
    return ids


async def _jobs_of_kind(maker: async_sessionmaker, kind: str) -> int:
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        return (
            await s.execute(text("SELECT count(*) FROM app.jobs WHERE kind = :k"), {"k": kind})
        ).scalar_one()


async def test_due_schedule_enqueues_its_action_and_advances_next_run_at(
    maker: async_sessionmaker,
) -> None:
    # next_run_at one minute in the past relative to the injected NOW -> due.
    ids = await _seed_schedule(
        maker, action="sync_predicates", next_run_at=NOW - timedelta(minutes=1)
    )

    before = await _jobs_of_kind(maker, "sync_predicates")
    fired = await scheduler_tick(maker, _registry(), now=NOW)

    assert [f.pipeline for f in fired] == [ids["pipeline"]]
    assert len(fired[0].job_ids) == 1
    # The bound action was enqueued onto the real queue (kind = handler key).
    assert await _jobs_of_kind(maker, "sync_predicates") == before + 1

    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        row = (
            await s.execute(
                text("SELECT next_run_at, last_run_at FROM app.schedules WHERE id = :id"),
                {"id": ids["schedule"]},
            )
        ).first()
    # Advanced app-side off the INJECTED clock: last_run = NOW, next = NOW + 1 day.
    assert row is not None
    assert row.last_run_at == NOW
    assert row.next_run_at == NOW + timedelta(days=1)


async def test_tick_skips_a_not_yet_due_schedule(maker: async_sessionmaker) -> None:
    ids = await _seed_schedule(
        maker, action="consolidate_predicates", next_run_at=NOW + timedelta(hours=1)
    )
    before = await _jobs_of_kind(maker, "consolidate_predicates")
    fired = await scheduler_tick(maker, _registry(), now=NOW)
    assert all(f.trigger_id != ids["trigger"] for f in fired)
    assert await _jobs_of_kind(maker, "consolidate_predicates") == before


async def test_tick_skips_a_disabled_schedule(maker: async_sessionmaker) -> None:
    await _seed_schedule(
        maker,
        action="purge_deleted_artifacts",
        next_run_at=NOW - timedelta(hours=1),
        enabled=False,
    )
    before = await _jobs_of_kind(maker, "purge_deleted_artifacts")
    await scheduler_tick(maker, _registry(), now=NOW)
    assert await _jobs_of_kind(maker, "purge_deleted_artifacts") == before


async def test_fire_trigger_enqueues_immediately(maker: async_sessionmaker) -> None:
    # next_run_at in the future: fire_trigger ignores schedule timing entirely
    # (the emergency Ops path runs a sweep now regardless of cadence).
    ids = await _seed_schedule(
        maker, action="consolidate_predicates", next_run_at=NOW + timedelta(days=1)
    )
    before = await _jobs_of_kind(maker, "consolidate_predicates")
    fired = await fire_trigger(maker, _registry(), ids["trigger"])
    assert fired.pipeline == ids["pipeline"]
    assert await _jobs_of_kind(maker, "consolidate_predicates") == before + 1


async def test_seeded_nightly_sweeps_exist_and_are_fireable(maker: async_sessionmaker) -> None:
    """Migration 0037 seeds the three nightly sweeps as manual, schedule-bound
    triggers; each is fireable on demand (the emergency Ops control)."""
    async with scoped_session(maker, queue.SYSTEM_CTX) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT t.id, t.pipeline FROM app.triggers t"
                    " JOIN app.schedules s ON s.id = t.on_schedule_id"
                    " WHERE t.manual AND t.pipeline LIKE 'nightly_%'"
                )
            )
        ).all()
    pipelines = {r.pipeline for r in rows}
    assert pipelines == {
        "nightly_consolidate_predicates",
        "nightly_sync_predicates",
        "nightly_purge_deleted_artifacts",
    }
    # Fire the consolidate sweep on demand and confirm a job lands.
    trig = next(r.id for r in rows if r.pipeline == "nightly_consolidate_predicates")
    before = await _jobs_of_kind(maker, "consolidate_predicates")
    await fire_trigger(maker, _registry(), str(trig))
    assert await _jobs_of_kind(maker, "consolidate_predicates") == before + 1
