"""The /api/tasks router: payload validation (TaskBody) and the thin endpoint
handlers, driven directly with fakes on a stand-in request — no app, no DB."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from jbrain.api import tasks as tasks_api
from jbrain.api.tasks import TaskBody
from jbrain.tasks.repo import TaskInfo, TaskRunInfo

NOW = datetime(2026, 6, 24, 12, tzinfo=UTC)
PID = "11111111-1111-1111-1111-111111111111"
PRINCIPAL = SimpleNamespace(id=PID, kind="owner")


def _task(**over: object) -> TaskInfo:
    base: dict[str, object] = dict(
        id="task-1",
        principal_id=PID,
        name="Brief",
        prompt="news",
        agent="jerv",
        domain_scopes=(),
        schedule_kind="on_demand",
        schedule_freq=None,
        schedule_days=(),
        schedule_time=None,
        run_at=None,
        timezone="UTC",
        enabled=True,
        notify_push=True,
        home_card=True,
        next_run_at=None,
        last_run_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(over)
    return TaskInfo(**base)  # type: ignore[arg-type]


# ---- TaskBody validation ----


def test_repeat_requires_freq_and_time() -> None:
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", schedule_kind="repeat")
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", schedule_kind="repeat", schedule_freq="weekly", schedule_time="07:00")
    ok = TaskBody(prompt="x", schedule_kind="repeat", schedule_freq="daily", schedule_time="07:00")
    assert ok.schedule_freq == "daily"


def test_once_requires_run_at() -> None:
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", schedule_kind="once")
    ok = TaskBody(prompt="x", schedule_kind="once", run_at=datetime(2026, 7, 1, 9, tzinfo=UTC))
    assert ok.run_at is not None


def test_on_demand_clears_schedule_fields() -> None:
    body = TaskBody(
        prompt="x",
        schedule_kind="on_demand",
        schedule_freq="daily",
        schedule_time="07:00",
        schedule_days=[1, 2],
    )
    assert body.schedule_freq is None and body.schedule_time is None and body.schedule_days == []


def test_non_curator_drops_domain_scopes() -> None:
    body = TaskBody(prompt="x", agent="jerv", domain_scopes=["health"])
    assert body.domain_scopes == []
    curator = TaskBody(prompt="x", agent="curator", domain_scopes=["health"])
    assert curator.domain_scopes == ["health"]


def test_rejects_unknown_enums_and_days() -> None:
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", agent="rogue")
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", schedule_kind="whenever")
    with pytest.raises(ValidationError):
        TaskBody(prompt="x", agent="curator", domain_scopes=["secret"])
    with pytest.raises(ValidationError):
        TaskBody(
            prompt="x",
            schedule_kind="repeat",
            schedule_freq="weekly",
            schedule_time="07:00",
            schedule_days=[9],
        )


# ---- endpoint handlers ----


class FakeRepo:
    def __init__(self) -> None:
        self.tasks = {"task-1": _task()}
        self.deleted: list[str] = []
        self.marked: list[str] = []

    async def list(self, ctx):  # type: ignore[no-untyped-def]
        return list(self.tasks.values())

    async def create(self, ctx, **fields):  # type: ignore[no-untyped-def]
        return _task(id="task-2", name=fields.get("name", ""))

    async def get(self, ctx, task_id):  # type: ignore[no-untyped-def]
        return self.tasks.get(task_id)

    async def update(self, ctx, task_id, **fields):  # type: ignore[no-untyped-def]
        if task_id not in self.tasks:
            return None
        return _task(id=task_id, **{k: v for k, v in fields.items() if k == "enabled"})

    async def delete(self, ctx, task_id):  # type: ignore[no-untyped-def]
        self.deleted.append(task_id)

    async def mark_ran(self, ctx, task_id, *, at):  # type: ignore[no-untyped-def]
        self.marked.append(task_id)


class FakeRunner:
    async def run(self, ctx, task, *, trigger):  # type: ignore[no-untyped-def]
        return TaskRunInfo(
            id="trun-1",
            task_id=task.id,
            session_id="sess-1",
            run_id="run-1",
            status="done",
            trigger=trigger,
            summary="ok",
            error=None,
            step_count=1,
            cost_tokens=5,
            started_at=NOW,
            ended_at=NOW,
        )


class FakeRuns:
    async def list_for_task(self, ctx, task_id, *, limit=20):  # type: ignore[no-untyped-def]
        return []

    async def count_since(self, ctx, since):  # type: ignore[no-untyped-def]
        return 3


def _request(repo: FakeRepo) -> SimpleNamespace:
    state = SimpleNamespace(task_repo=repo, task_runner=FakeRunner(), task_runs=FakeRuns())
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.asyncio
async def test_list_and_create() -> None:
    repo = FakeRepo()
    req = _request(repo)
    out = await tasks_api.list_tasks(req, PRINCIPAL)  # type: ignore[arg-type]
    assert out[0].id == "task-1"
    created = await tasks_api.create_task(req, PRINCIPAL, TaskBody(prompt="hi", name="New"))  # type: ignore[arg-type]
    assert created.name == "New"


@pytest.mark.asyncio
async def test_patch_enabled_and_404() -> None:
    repo = FakeRepo()
    req = _request(repo)
    patch = tasks_api.EnabledPatch(enabled=False)
    out = await tasks_api.set_enabled(req, PRINCIPAL, "task-1", patch)  # type: ignore[arg-type]
    assert out.enabled is False
    with pytest.raises(HTTPException):
        miss = tasks_api.EnabledPatch(enabled=True)
        await tasks_api.set_enabled(req, PRINCIPAL, "missing", miss)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_now_executes_and_marks_ran() -> None:
    repo = FakeRepo()
    req = _request(repo)
    run = await tasks_api.run_task(req, PRINCIPAL, "task-1")  # type: ignore[arg-type]
    assert run.status == "done" and run.trigger == "manual"
    assert repo.marked == ["task-1"]
    with pytest.raises(HTTPException):
        await tasks_api.run_task(req, PRINCIPAL, "missing")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_delete_and_runs() -> None:
    repo = FakeRepo()
    req = _request(repo)
    await tasks_api.delete_task(req, PRINCIPAL, "task-1")  # type: ignore[arg-type]
    assert repo.deleted == ["task-1"]
    assert await tasks_api.task_runs(req, PRINCIPAL, "task-1") == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_activity_badge() -> None:
    req = _request(FakeRepo())
    # No baseline yet → zero, no query.
    none = await tasks_api.run_activity(req, PRINCIPAL, None)  # type: ignore[arg-type]
    assert none.count == 0
    # With a since, the repo's count is surfaced.
    some = await tasks_api.run_activity(req, PRINCIPAL, NOW)  # type: ignore[arg-type]
    assert some.count == 3
