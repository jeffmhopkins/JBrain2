"""The /api/tasks router: payload validation (TaskBody) and the thin endpoint
handlers, driven directly with fakes on a stand-in request — no app, no DB."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from jbrain.api import tasks as tasks_api
from jbrain.api.tasks import GroupBody, ReorderBody, TaskBody
from jbrain.tasks.repo import TaskGroupInfo, TaskInfo, TaskRunInfo

NOW = datetime(2026, 6, 24, 12, tzinfo=UTC)
PID = "11111111-1111-1111-1111-111111111111"
PRINCIPAL = SimpleNamespace(id=PID, kind="owner")


def _task(**over: object) -> TaskInfo:
    base: dict[str, object] = dict(
        id="task-1",
        principal_id=PID,
        group_id=None,
        position=0,
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

    async def reorder(self, ctx, *, group_id, task_ids):  # type: ignore[no-untyped-def]
        self.reordered = (group_id, list(task_ids))
        return [_task(id=tid, group_id=group_id, position=i) for i, tid in enumerate(task_ids)]


class FakeGroups:
    def __init__(self) -> None:
        self.groups = {"g1": TaskGroupInfo(id="g1", name="Money", position=0)}
        self.deleted: list[str] = []

    async def list(self, ctx):  # type: ignore[no-untyped-def]
        return list(self.groups.values())

    async def create(self, ctx, *, name):  # type: ignore[no-untyped-def]
        return TaskGroupInfo(id="g2", name=name, position=1)

    async def rename(self, ctx, group_id, *, name):  # type: ignore[no-untyped-def]
        if group_id not in self.groups:
            return None
        return TaskGroupInfo(id=group_id, name=name, position=0)

    async def delete(self, ctx, group_id):  # type: ignore[no-untyped-def]
        self.deleted.append(group_id)


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


def _run(task_id: str) -> TaskRunInfo:
    return TaskRunInfo(
        id="lr",
        task_id=task_id,
        session_id="sess-latest",
        run_id=None,
        status="done",
        trigger="schedule",
        summary="latest",
        error=None,
        step_count=2,
        cost_tokens=0,
        started_at=NOW,
        ended_at=NOW,
    )


class FakeRuns:
    async def list_for_task(self, ctx, task_id, *, limit=20):  # type: ignore[no-untyped-def]
        return []

    async def latest_per_task(self, ctx, task_ids):  # type: ignore[no-untyped-def]
        return {tid: _run(tid) for tid in task_ids}


def _request(repo: FakeRepo, groups: "FakeGroups | None" = None) -> SimpleNamespace:
    state = SimpleNamespace(
        task_repo=repo,
        task_runner=FakeRunner(),
        task_runs=FakeRuns(),
        task_groups=groups or FakeGroups(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.asyncio
async def test_list_and_create() -> None:
    repo = FakeRepo()
    req = _request(repo)
    out = await tasks_api.list_tasks(req, PRINCIPAL)  # type: ignore[arg-type]
    assert out[0].id == "task-1"
    # The latest run is embedded so the card's band renders without a per-card fetch.
    assert out[0].latest_run is not None and out[0].latest_run.session_id == "sess-latest"
    created = await tasks_api.create_task(req, PRINCIPAL, TaskBody(prompt="hi", name="New"))  # type: ignore[arg-type]
    assert created.name == "New"
    assert created.latest_run is None  # a brand-new task has never run


@pytest.mark.asyncio
async def test_patch_enabled_and_404() -> None:
    repo = FakeRepo()
    req = _request(repo)
    patch = tasks_api.EnabledPatch(enabled=False)
    out = await tasks_api.set_enabled(req, PRINCIPAL, "task-1", patch)  # type: ignore[arg-type]
    assert out.enabled is False
    assert out.latest_run is not None  # a toggle preserves the embedded latest run
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


# ---- groups + reorder (Direction B) ----


@pytest.mark.asyncio
async def test_group_crud() -> None:
    groups = FakeGroups()
    req = _request(FakeRepo(), groups)
    listed = await tasks_api.list_task_groups(req, PRINCIPAL)  # type: ignore[arg-type]
    assert [g.name for g in listed] == ["Money"]
    created = await tasks_api.create_task_group(req, PRINCIPAL, GroupBody(name="  Health  "))  # type: ignore[arg-type]
    assert created.name == "Health"  # the name is trimmed at the edge
    renamed = await tasks_api.rename_task_group(req, PRINCIPAL, "g1", GroupBody(name="Finance"))  # type: ignore[arg-type]
    assert renamed.name == "Finance"
    with pytest.raises(HTTPException):
        await tasks_api.rename_task_group(req, PRINCIPAL, "missing", GroupBody(name="x"))  # type: ignore[arg-type]
    await tasks_api.delete_task_group(req, PRINCIPAL, "g1")  # type: ignore[arg-type]
    assert groups.deleted == ["g1"]


@pytest.mark.asyncio
async def test_reorder_sets_group_and_position() -> None:
    repo = FakeRepo()
    req = _request(repo)
    body = ReorderBody(group_id="g1", task_ids=["task-3", "task-1"])
    out = await tasks_api.reorder_tasks(req, PRINCIPAL, body)  # type: ignore[arg-type]
    assert repo.reordered == ("g1", ["task-3", "task-1"])
    # Order returned reflects the sent sequence, each stamped with its list index.
    assert [(t.id, t.group_id, t.position) for t in out] == [
        ("task-3", "g1", 0),
        ("task-1", "g1", 1),
    ]

    # A NULL target moves tasks back to the Ungrouped bucket.
    back = ReorderBody(group_id=None, task_ids=["task-1"])
    ungrouped = await tasks_api.reorder_tasks(req, PRINCIPAL, back)  # type: ignore[arg-type]
    assert ungrouped[0].group_id is None
