"""Migration 0093 against real Postgres: tasks + task_runs are owner-only (CLAUDE.md
rule 3), the persona/schedule/status sets are pinned by CHECKs, the scheduler claim
advances `next_run_at`, deleting a task cascades its runs, and a full task run lands
real session/run/transcript/task_run rows.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.agents import NON_OWNER_PERSONAS, OWNER_AGENTS
from jbrain.agent.loop import AgentResult
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.tasks.repo import TaskGroupRepo, TaskRepo, TaskRunRepo
from jbrain.tasks.runner import ExecutedTurn, TaskRunner
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


async def test_every_owner_persona_is_a_valid_task_agent(
    maker: async_sessionmaker,
) -> None:
    """The tasks.agent CHECK (0093, widened in 0095) must admit every OWNER-selectable
    persona, so the task launcher can schedule any of them — the archivist included. The
    non-owner intake persona is excluded by design (proven rejected below)."""
    owner = await _owner_ctx(maker)
    repo = TaskRepo(maker)
    for name in sorted(OWNER_AGENTS):
        task = await _make_task(repo, owner, name=name, agent=name)
        assert task.agent == name


async def test_non_owner_persona_is_rejected_as_a_task_agent(
    maker: async_sessionmaker,
) -> None:
    """The intake persona can never be scheduled as a task: the tasks.agent CHECK excludes
    it, failing closed at the DB even if the API's OWNER_AGENTS gate were bypassed (§5)."""
    owner = await _owner_ctx(maker)
    repo = TaskRepo(maker)
    for name in sorted(NON_OWNER_PERSONAS):
        with pytest.raises((ProgrammingError, IntegrityError)):
            await _make_task(repo, owner, name=name, agent=name)


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


async def test_latest_per_task_returns_the_newest_run(maker: async_sessionmaker) -> None:
    """The card-band query: one row per task — its most recent FINISHED run — and
    nothing for a task that has never run, whose only run is still in flight, or for a
    non-owner principal (RLS). An in-flight newer run does not replace the last
    completed result (the band must not update on start)."""
    owner = await _owner_ctx(maker)
    repo, runs = TaskRepo(maker), TaskRunRepo(maker)
    t1 = await _make_task(repo, owner, name="one")
    t2 = await _make_task(repo, owner, name="two")  # never runs → absent from the result
    t3 = await _make_task(repo, owner, name="three")  # only run is in flight → absent

    async def _start(task_id: str) -> str:
        return await runs.start(
            owner,
            task_id=task_id,
            principal_id=owner.principal_id,
            session_id=None,
            run_id=None,
            trigger="manual",
        )

    older = await _start(t1.id)
    newer = await _start(t1.id)
    await runs.finish(owner, newer, status="done", summary="fresh", step_count=3)
    # Age the first run so DISTINCT ON unambiguously picks the newer one.
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text("UPDATE app.task_runs SET started_at = :t, ended_at = :t WHERE id = :id"),
            {"t": datetime.now(UTC) - timedelta(hours=1), "id": older},
        )
    await _start(t3.id)  # in flight, never finished

    latest = await runs.latest_per_task(owner, [t1.id, t2.id, t3.id])
    assert set(latest) == {t1.id}  # t2 never ran; t3's only run is still running
    assert latest[t1.id].id == newer and latest[t1.id].summary == "fresh"

    # A freshly started run on t1 must NOT supplant the last finished one until it ends.
    in_flight = await _start(t1.id)
    again = await runs.latest_per_task(owner, [t1.id])
    assert again[t1.id].id == newer  # still the completed run, not the in-flight one
    await runs.finish(owner, in_flight, status="done", summary="newest", step_count=1)
    done = await runs.latest_per_task(owner, [t1.id])
    assert done[t1.id].id == in_flight and done[t1.id].summary == "newest"

    # Empty input short-circuits; a non-owner sees nothing (RLS).
    assert await runs.latest_per_task(owner, []) == {}
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await runs.latest_per_task(token, [t1.id]) == {}


async def test_task_groups_are_owner_only(maker: async_sessionmaker) -> None:
    """Migration 0136: task_groups is owner-only metadata (CLAUDE.md rule 3), like the
    tasks it buckets. A capability token sees none; a narrowed owner still sees its own."""
    owner = await _owner_ctx(maker)
    groups = TaskGroupRepo(maker)
    g = await groups.create(owner, name="Money")
    assert g.position == 0  # first group anchors the order
    g2 = await groups.create(owner, name="Household")
    assert g2.position == 1  # each new group appends

    assert [x.name for x in await groups.list(owner)] == ["Money", "Household"]
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    assert await groups.list(token) == []
    narrowed = SessionContext(
        principal_id=owner.principal_id,
        principal_kind="owner",
        domain_scopes=("general",),
        owner_scoped=True,
    )
    assert len(await groups.list(narrowed)) == 2

    renamed = await groups.rename(owner, g.id, name="Finance")
    assert renamed is not None and renamed.name == "Finance"


async def test_reorder_sets_group_and_position(maker: async_sessionmaker) -> None:
    """`reorder` writes the authoritative membership + order for one group's list — the
    single call behind both a within-group drag and a "Move to…". A new task appends to
    the Ungrouped bucket; reordering into a real group stamps 0-based positions."""
    owner = await _owner_ctx(maker)
    repo, groups = TaskRepo(maker), TaskGroupRepo(maker)
    t1 = await _make_task(repo, owner, name="one")
    t2 = await _make_task(repo, owner, name="two")
    # New tasks land ungrouped, each appended one past the current Ungrouped tail
    # (absolute positions accrue across the shared test DB — only the delta is fixed).
    assert t1.group_id is None and t2.group_id is None
    assert t2.position == t1.position + 1

    g = await groups.create(owner, name="Money")
    moved = await repo.reorder(owner, group_id=g.id, task_ids=[t2.id, t1.id])
    assert [(m.id, m.group_id, m.position) for m in moved] == [
        (t2.id, g.id, 0),
        (t1.id, g.id, 1),
    ]
    # The list reflects the new order within the group.
    by_id = {t.id: t for t in await repo.list(owner)}
    assert by_id[t2.id].group_id == g.id and by_id[t2.id].position == 0

    # Reordering into a non-existent group is a no-op (nothing moves).
    assert await repo.reorder(owner, group_id=str(uuid.uuid4()), task_ids=[t1.id]) == []


async def test_deleting_a_group_ungroups_its_tasks(maker: async_sessionmaker) -> None:
    """Deleting a bucket must never lose tasks: the FK SET NULLs them back to Ungrouped
    (migration 0136), unlike deleting the task itself which cascades its runs."""
    owner = await _owner_ctx(maker)
    repo, groups = TaskRepo(maker), TaskGroupRepo(maker)
    task = await _make_task(repo, owner, name="filed")
    g = await groups.create(owner, name="Money")
    await repo.reorder(owner, group_id=g.id, task_ids=[task.id])
    assert (await repo.get(owner, task.id)).group_id == g.id  # type: ignore[union-attr]

    await groups.delete(owner, g.id)
    survivor = await repo.get(owner, task.id)
    assert survivor is not None and survivor.group_id is None  # ungrouped, not deleted


async def test_full_run_lands_session_run_and_task_run(maker: async_sessionmaker) -> None:
    owner = await _owner_ctx(maker)
    repo = TaskRepo(maker)
    task = await _make_task(repo, owner, name="brief", prompt="the news")

    class FakeExecutor:
        async def run_turn(self, **_: object) -> ExecutedTurn:
            return ExecutedTurn(
                result=AgentResult(
                    text="here it is", stop_reason="end_turn", steps=1, cost_tokens=4
                ),
                tools=[{"id": "t1", "name": "search", "ok": True}],
                reasoning="thinking it through",
            )

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
        # The assistant turn persists the tool steps + reasoning, so reopening the
        # session this task produced replays its "Worked" / "Thought for Ns" disclosures.
        assistant = (
            await session.execute(
                text(
                    "SELECT tools, reasoning FROM app.agent_turns"
                    " WHERE session_id = :id AND role = 'assistant'"
                ),
                {"id": info.session_id},
            )
        ).one()
    assert sess == 1 and turns == 2  # user + assistant
    assert assistant[0] == [{"id": "t1", "name": "search", "ok": True}]
    assert assistant[1] == "thinking it through"
    stored = await TaskRunRepo(maker).list_for_task(owner, task.id)
    assert len(stored) == 1 and stored[0].summary == "here it is"
