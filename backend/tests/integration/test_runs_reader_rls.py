"""The RunLogReader against real Postgres: it reads the owner's run log with its
step tree, and (CLAUDE.md rule 3) a non-owner session reads an empty log even
through the reader — the RLS firewall, not the API, is the enforcement point."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain import queue
from jbrain.agent.runlog import AgentRunLog, RunLogReader
from jbrain.agent.session import AgentSessionRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.workflow.runlog import EnqueuedStep, PipelineRunLog
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _seed_run(maker: async_sessionmaker, owner: SessionContext) -> str:
    sessions = AgentSessionRepo(maker)
    info = await sessions.create(owner, domain_scopes=["general"], title="ask")
    log = AgentRunLog(maker)
    run_id = await log.start(owner, session_id=info.id, prompt_version="agent-system-v1")
    await log.step(owner, run_id, idx=0, kind="model", name="converse", ok=True, cost_tokens=15)
    await log.step(owner, run_id, idx=1, kind="tool", name="search", ok=False, cost_tokens=0)
    await log.finish(
        owner, run_id, status="error", stop_reason="step_error", step_count=2, cost_tokens=15
    )
    return run_id


async def test_reader_lists_and_loads_for_owner(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    run_id = await _seed_run(maker, owner)
    reader = RunLogReader(maker)

    listed = await reader.list_recent(owner)
    assert [r.id for r in listed] == [run_id]
    summary = listed[0]
    assert summary.status == "error"
    assert summary.duration_ms is not None  # finished run has an honest duration
    assert summary.last_error == "search"  # first not-ok step's name

    detail = await reader.load(owner, run_id)
    assert detail is not None
    assert detail.stop_reason == "step_error"
    assert [(s.idx, s.ok) for s in detail.steps] == [(0, True), (1, False)]
    assert detail.steps[1].error == "search"


async def _seed_sweep(maker: async_sessionmaker, owner: SessionContext, pipeline: str) -> str:
    """A finished 0-token reconcile-sweep pipeline run (the noise the filter drops)."""
    rid = str(uuid.uuid4())  # id has no DB default — the ORM writer generates it in Python.
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.runs (id, kind, pipeline, ran_as, status, step_count,"
                " cost_tokens, ended_at)"
                " VALUES (:id, 'pipeline', :p, 'system', 'done', 1, 0, now())"
            ),
            {"id": rid, "p": pipeline},
        )
    return rid


async def test_reader_filters_by_kind_and_sweeps(maker: async_sessionmaker) -> None:
    # The test DB is shared across this file, so assert on subsets/deltas around the
    # rows THIS test seeds, never absolute contents.
    owner = await _owner(maker)
    agent_run = await _seed_run(maker, owner)  # kind='agent'
    sweep_a = await _seed_sweep(maker, owner, "reconcile_pending_notes")
    sweep_b = await _seed_sweep(maker, owner, "geofence_sweep")
    reader = RunLogReader(maker)

    # kinds= narrows to agent turns; the reconcile sweeps (kind='pipeline') drop out.
    by_kind = await reader.list_recent(owner, kinds=["agent", "subagent"], limit=200)
    ids = {r.id for r in by_kind}
    assert agent_run in ids
    assert sweep_a not in ids and sweep_b not in ids
    assert all(r.kind in ("agent", "subagent") for r in by_kind)

    # exclude_sweeps drops the reconcile_* + geofence housekeeping, keeping the rest.
    kept = await reader.list_recent(owner, exclude_sweeps=True, limit=200)
    kept_ids = {r.id for r in kept}
    assert sweep_a not in kept_ids and sweep_b not in kept_ids
    assert agent_run in kept_ids

    # stats: the two sweeps count under pipeline; excluding them drops that bucket by 2.
    stats = await reader.stats(owner)
    assert stats.by_kind["agent"] >= 1 and stats.by_kind["pipeline"] >= 2
    swept = await reader.stats(owner, exclude_sweeps=True)
    assert swept.by_kind["pipeline"] == stats.by_kind["pipeline"] - 2


async def test_stats_is_owner_only(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    await _seed_run(maker, owner)
    reader = RunLogReader(maker)
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    # RLS firewall: a non-owner sees an empty log, so every aggregate is zero.
    blind = await reader.stats(token)
    assert blind.active == 0 and blind.failed_today == 0 and blind.tokens_today == 0
    assert blind.by_kind == {"agent": 0, "integration": 0, "pipeline": 0}


async def test_reader_is_owner_only(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    run_id = await _seed_run(maker, owner)
    reader = RunLogReader(maker)

    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await reader.list_recent(token) == []
    assert await reader.load(token, run_id) is None


async def test_reader_bad_uuid_is_none(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    reader = RunLogReader(maker)
    assert await reader.load(owner, "not-a-uuid") is None


async def _seed_pipeline(maker: async_sessionmaker, owner: SessionContext) -> tuple[str, list[str]]:
    """A pipeline run with two enqueued (status='queued') step jobs — the shape of a
    freshly-fired manual trigger before the single-threaded worker picks it up."""
    job_a = await queue.enqueue(maker, owner, "daily_inbox_triage", {"n": 1})
    job_b = await queue.enqueue(maker, owner, "daily_inbox_triage", {"n": 2})
    run_id = await PipelineRunLog(maker).record(
        owner,
        pipeline="daily_inbox_triage",
        trigger_id=None,
        ran_as="system",
        domain_code=None,
        principal_id=None,
        steps=[
            EnqueuedStep(kind="daily_inbox_triage", job_id=job_a),
            EnqueuedStep(kind="daily_inbox_triage", job_id=job_b),
        ],
    )
    return run_id, [job_a, job_b]


async def test_pipeline_run_reads_queued_until_a_step_starts(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    run_id, jobs = await _seed_pipeline(maker, owner)
    reader = RunLogReader(maker)

    # Stored 'running', but every step's job is still queued → derived 'queued'.
    assert (await reader.list_recent(owner))[0].status == "queued"
    detail = await reader.load(owner, run_id)
    assert detail is not None and detail.status == "queued"

    # Once the worker claims one step's job, the run is genuinely running.
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text("UPDATE app.jobs SET status = 'running' WHERE id = :id"), {"id": jobs[0]}
        )
    assert (await reader.list_recent(owner))[0].status == "running"
    assert (await reader.load(owner, run_id)).status == "running"  # type: ignore[union-attr]


async def test_queue_depth_counts_queued_jobs(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    reader = RunLogReader(maker)
    # Deltas, not absolutes: the test DB is shared, so other rows may already exist.
    base = await reader.queue_depth(owner)

    _, jobs = await _seed_pipeline(maker, owner)
    assert await reader.queue_depth(owner) == base + 2

    # A running job has left the queue; only the still-queued one is counted.
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text("UPDATE app.jobs SET status = 'running' WHERE id = :id"), {"id": jobs[0]}
        )
    assert await reader.queue_depth(owner) == base + 1
