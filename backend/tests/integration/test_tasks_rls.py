"""Migration 0093 against real Postgres: tasks + task_runs are owner-only (CLAUDE.md
rule 3), the persona/schedule/status sets are pinned by CHECKs, the scheduler claim
advances `next_run_at`, deleting a task cascades its runs, and a full task run lands
real session/run/transcript/task_run rows.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.agents import AGENT_NAMES
from jbrain.agent.loop import AgentResult
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.tasks.repo import TaskRepo, TaskRunRepo
from jbrain.tasks.runner import TaskRunner
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _make_task(repo: TaskRepo, owner: SessionContext, **over: object):  # type: ignore[no-untyped-def]
    fields: dict[str, object] = dict(
        name="brief",
        prompt="news",
        agent="jerv",
        domain_scopes=[],
        schedule_kind="on_demand",
        schedule_freq=None,
        schedule_days=[],
        schedule_time=None,
        run_at=None,
        timezone="UTC",
    )
    fields.update(over)
    return await repo.create(owner, **fields)  # type: ignore[arg-type]


async def test_tasks_are_owner_only(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = TaskRepo(maker)
    task = await _make_task(repo, owner, name="morning brief")
    assert task.next_run_at is None  # on-demand never schedules

    assert len(await repo.list(owner)) == 1
    # A non-owner principal sees no tasks at all (RLS).
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await repo.list(token) == []
    # A narrowed owner still sees its tasks — owner_scoped restricts domain data only.
    narrowed = SessionContext(
        principal_id=owner.principal_id,
        principal_kind="owner",
        domain_scopes=("general",),
        owner_scoped=True,
    )
    assert len(await repo.list(narrowed)) == 1


async def test_check_constraints_pin_the_sets(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    for col, val in (("agent", "rogue"), ("schedule_kind", "whenever")):
        with pytest.raises((ProgrammingError, IntegrityError)):
            async with scoped_session(maker, owner) as session:
                await session.execute(
                    text(
                        f"INSERT INTO app.tasks (principal_id, prompt, {col})"
                        f" VALUES (:pid, 'x', :v)"
                    ),
                    {"pid": owner.principal_id, "v": val},
                )


async def test_every_code_defined_persona_is_a_valid_task_agent(
    maker: async_sessionmaker,
) -> None:
    """The tasks.agent CHECK (0093, widened in 0095) must admit every persona the code
    offers, so the task launcher can schedule any of them — the archivist included."""
    owner = await _owner_ctx(maker)
    repo = TaskRepo(maker)
    for name in sorted(AGENT_NAMES):
        task = await _make_task(repo, owner, name=name, agent=name)
        assert task.agent == name


async def test_repeat_schedule_computes_next_run(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = TaskRepo(maker)
    task = await repo.create(
        owner,
        name="daily",
        prompt="news",
        agent="jerv",
        domain_scopes=[],
        schedule_kind="repeat",
        schedule_freq="daily",
        schedule_days=[],
        schedule_time="07:00",
        run_at=None,
        timezone="UTC",
        now=datetime(2026, 6, 24, 8, tzinfo=UTC),
    )
    assert task.next_run_at == datetime(2026, 6, 25, 7, tzinfo=UTC)


async def test_claim_due_advances_next_run(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = TaskRepo(maker)
    task = await _make_task(
        repo, owner, schedule_kind="repeat", schedule_freq="daily", schedule_time="07:00"
    )
    # Force it due, then claim at a moment past it.
    past = datetime.now(UTC) - timedelta(minutes=5)
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text("UPDATE app.tasks SET next_run_at = :t WHERE id = :id"),
            {"t": past, "id": task.id},
        )
    now = datetime.now(UTC)
    claimed = await repo.claim_due(owner, now=now)
    assert task.id in [c.id for c in claimed]
    # A second claim no longer finds it — the first advanced next_run_at into the future.
    again = await repo.claim_due(owner, now=now)
    assert task.id not in [c.id for c in again]
    refreshed = await repo.get(owner, task.id)
    assert refreshed is not None and refreshed.next_run_at is not None
    assert refreshed.next_run_at > datetime.now(UTC)


async def test_one_off_claim_clears_next_run(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = TaskRepo(maker)
    run_at = datetime(2026, 6, 26, 9, tzinfo=UTC)
    task = await _make_task(
        repo, owner, schedule_kind="once", run_at=run_at, now=run_at - timedelta(hours=1)
    )
    assert task.next_run_at == run_at  # scheduled for its one moment
    # Claiming at/after that moment spends it — there is no next fire.
    claimed = await repo.claim_due(owner, now=run_at + timedelta(minutes=1))
    assert task.id in [c.id for c in claimed]
    refreshed = await repo.get(owner, task.id)
    assert refreshed is not None and refreshed.next_run_at is None


async def test_runs_are_owner_only_and_cascade_with_the_task(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo, runs = TaskRepo(maker), TaskRunRepo(maker)
    task = await _make_task(repo, owner)
    run_id = await runs.start(
        owner,
        task_id=task.id,
        principal_id=owner.principal_id,
        session_id=None,
        run_id=None,
        trigger="manual",
    )
    await runs.finish(owner, run_id, status="done", summary="ok", error=None, step_count=1)

    assert len(await runs.list_for_task(owner, task.id)) == 1
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await runs.list_for_task(token, task.id) == []

    # Deleting the task cascades its runs away.
    await repo.delete(owner, task.id)
    async with scoped_session(maker, owner) as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM app.task_runs WHERE task_id = :id"), {"id": task.id}
            )
        ).scalar()
    assert remaining == 0


async def test_count_since_powers_the_badge(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo, runs = TaskRepo(maker), TaskRunRepo(maker)
    task = await _make_task(repo, owner)

    marker = datetime.now(UTC)
    # A run started before the marker doesn't count; two after it do.
    before = await runs.start(
        owner,
        task_id=task.id,
        principal_id=owner.principal_id,
        session_id=None,
        run_id=None,
        trigger="schedule",
    )
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text("UPDATE app.task_runs SET started_at = :t WHERE id = :id"),
            {"t": marker - timedelta(minutes=5), "id": before},
        )
    for _ in range(2):
        await runs.start(
            owner,
            task_id=task.id,
            principal_id=owner.principal_id,
            session_id=None,
            run_id=None,
            trigger="manual",
        )
    assert await runs.count_since(owner, marker) == 2
    # A non-owner sees nothing (RLS).
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await runs.count_since(token, marker) == 0


async def test_full_run_lands_session_run_and_task_run(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = TaskRepo(maker)
    task = await _make_task(repo, owner, name="brief", prompt="the news")

    class FakeExecutor:
        async def run_turn(self, **_: object) -> AgentResult:
            return AgentResult(text="here it is", stop_reason="end_turn", steps=1, cost_tokens=4)

    runner = TaskRunner(
        sessions=AgentSessionRepo(maker),
        runlog=AgentRunLog(maker),
        transcript=AgentTranscript(maker),
        runs=TaskRunRepo(maker),
        executor=FakeExecutor(),
    )
    info = await runner.run(owner, task, trigger="manual")
    assert info.status == "done" and info.session_id is not None

    # The run produced a real, browsable session + a transcript exchange.
    async with scoped_session(maker, owner) as session:
        sess = (
            await session.execute(
                text("SELECT count(*) FROM app.agent_sessions WHERE id = :id"),
                {"id": info.session_id},
            )
        ).scalar()
        turns = (
            await session.execute(
                text("SELECT count(*) FROM app.agent_turns WHERE session_id = :id"),
                {"id": info.session_id},
            )
        ).scalar()
    assert sess == 1 and turns == 2  # user + assistant
    stored = await TaskRunRepo(maker).list_for_task(owner, task.id)
    assert len(stored) == 1 and stored[0].summary == "here it is"
