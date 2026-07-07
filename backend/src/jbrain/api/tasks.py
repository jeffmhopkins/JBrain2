"""/api/tasks — the owner's scheduled / on-demand agent tasks.

A task is a saved prompt + persona + schedule (docs/mocks/tasks-launcher-README.md,
Direction A). CRUD is owner-only; "Run now" executes the task synchronously through
the shared `TaskRunner` and returns the finished run. The scheduler fires due tasks
on its own (tasks/scheduler.py) — this router is the authoring + history surface.

Validation pins the persona/schedule sets (and a curator's domain scopes) at the
edge; the DB CHECKs are the backstop. The next-fire time is computed in the repo.
"""

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from jbrain.agent.agents import OWNER_AGENTS
from jbrain.api.deps import owner_only
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo
from jbrain.tasks.repo import TaskInfo, TaskRepo, TaskRunInfo, TaskRunRepo
from jbrain.tasks.runner import TaskRunner
from jbrain.tasks.schedule import FREQS, KINDS

router = APIRouter(dependencies=[Depends(owner_only)])

OwnerDep = Annotated[PrincipalInfo, Depends(owner_only)]

# The domain codes a curator task may read (the SessionsPanel set); a non-KB agent
# carries none. General/Medical/Financial/Location on the wire.
_DOMAINS = frozenset({"general", "health", "finance", "location"})


def get_task_repo(request: Request) -> TaskRepo:
    return cast(TaskRepo, request.app.state.task_repo)


def get_task_runs(request: Request) -> TaskRunRepo:
    return cast(TaskRunRepo, request.app.state.task_runs)


def get_task_runner(request: Request) -> TaskRunner:
    return cast(TaskRunner, request.app.state.task_runner)


class TaskBody(BaseModel):
    """Create / replace payload. The schedule fields are cross-validated by kind."""

    name: str = Field(default="", max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)
    agent: str = "jerv"
    domain_scopes: list[str] = Field(default_factory=list)
    schedule_kind: str = "on_demand"
    schedule_freq: str | None = None
    schedule_days: list[int] = Field(default_factory=list)
    schedule_time: str | None = None
    run_at: datetime | None = None
    timezone: str = "UTC"
    enabled: bool = True
    notify_push: bool = True
    home_card: bool = True

    @field_validator("agent")
    @classmethod
    def _agent(cls, v: str) -> str:
        if v not in OWNER_AGENTS:
            raise ValueError("unknown agent")
        return v

    @field_validator("schedule_kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError("unknown schedule kind")
        return v

    @field_validator("domain_scopes")
    @classmethod
    def _scopes(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in _DOMAINS]
        if bad:
            raise ValueError(f"unknown domain scopes: {bad}")
        return v

    @field_validator("schedule_days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("schedule_days must be 0..6 (Sun..Sat)")
        return sorted(set(v))

    @model_validator(mode="after")
    def _coherent(self) -> "TaskBody":
        # A non-KB persona reads no domains — drop any scopes so the stored row is
        # honest (the runner enforces the firewall regardless).
        if self.agent != "curator":
            self.domain_scopes = []
        if self.schedule_kind == "repeat":
            if self.schedule_freq not in FREQS:
                raise ValueError("repeat needs a freq of daily|weekdays|weekly")
            if not self.schedule_time:
                raise ValueError("repeat needs a time (HH:MM)")
            if self.schedule_freq == "weekly" and not self.schedule_days:
                raise ValueError("weekly needs at least one day")
        elif self.schedule_kind == "once":
            if self.run_at is None:
                raise ValueError("once needs a run_at instant")
        else:  # on_demand
            self.schedule_freq = None
            self.schedule_time = None
            self.run_at = None
            self.schedule_days = []
        return self


class TaskRunOut(BaseModel):
    id: str
    task_id: str
    session_id: str | None
    status: str
    trigger: str
    summary: str
    error: str | None
    step_count: int
    cost_tokens: int
    started_at: datetime
    ended_at: datetime | None

    @classmethod
    def of(cls, r: TaskRunInfo) -> "TaskRunOut":
        return cls(
            id=r.id,
            task_id=r.task_id,
            session_id=r.session_id,
            status=r.status,
            trigger=r.trigger,
            summary=r.summary,
            error=r.error,
            step_count=r.step_count,
            cost_tokens=r.cost_tokens,
            started_at=r.started_at,
            ended_at=r.ended_at,
        )


class TaskOut(BaseModel):
    id: str
    name: str
    prompt: str
    agent: str
    domain_scopes: list[str]
    schedule_kind: str
    schedule_freq: str | None
    schedule_days: list[int]
    schedule_time: str | None
    run_at: datetime | None
    timezone: str
    enabled: bool
    notify_push: bool
    home_card: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    # The most recent run, embedded so the card's "latest result" band renders (and
    # opens its session) without a per-card fetch. None until the task has ever run.
    latest_run: "TaskRunOut | None" = None

    @classmethod
    def of(cls, t: TaskInfo, latest_run: "TaskRunInfo | None" = None) -> "TaskOut":
        return cls(
            id=t.id,
            name=t.name,
            prompt=t.prompt,
            agent=t.agent,
            domain_scopes=list(t.domain_scopes),
            schedule_kind=t.schedule_kind,
            schedule_freq=t.schedule_freq,
            schedule_days=list(t.schedule_days),
            schedule_time=t.schedule_time,
            run_at=t.run_at,
            timezone=t.timezone,
            enabled=t.enabled,
            notify_push=t.notify_push,
            home_card=t.home_card,
            next_run_at=t.next_run_at,
            last_run_at=t.last_run_at,
            latest_run=TaskRunOut.of(latest_run) if latest_run is not None else None,
        )


class EnabledPatch(BaseModel):
    """The optimistic enable/disable toggle — a narrow PATCH the card uses."""

    enabled: bool


@router.get("/tasks")
async def list_tasks(request: Request, principal: OwnerDep) -> list[TaskOut]:
    ctx = ctx_for(principal)
    tasks = await get_task_repo(request).list(ctx)
    latest = await get_task_runs(request).latest_per_task(ctx, [t.id for t in tasks])
    return [TaskOut.of(t, latest.get(t.id)) for t in tasks]


@router.post("/tasks", status_code=201)
async def create_task(request: Request, principal: OwnerDep, body: TaskBody) -> TaskOut:
    created = await get_task_repo(request).create(
        ctx_for(principal),
        name=body.name,
        prompt=body.prompt,
        agent=body.agent,
        domain_scopes=body.domain_scopes,
        schedule_kind=body.schedule_kind,
        schedule_freq=body.schedule_freq,
        schedule_days=body.schedule_days,
        schedule_time=body.schedule_time,
        run_at=body.run_at,
        timezone=body.timezone,
        enabled=body.enabled,
        notify_push=body.notify_push,
        home_card=body.home_card,
    )
    return TaskOut.of(created)


@router.put("/tasks/{task_id}")
async def replace_task(
    request: Request, principal: OwnerDep, task_id: str, body: TaskBody
) -> TaskOut:
    ctx = ctx_for(principal)
    updated = await get_task_repo(request).update(
        ctx,
        task_id,
        name=body.name,
        prompt=body.prompt,
        agent=body.agent,
        domain_scopes=body.domain_scopes,
        schedule_kind=body.schedule_kind,
        schedule_freq=body.schedule_freq,
        schedule_days=body.schedule_days,
        schedule_time=body.schedule_time,
        run_at=body.run_at,
        timezone=body.timezone,
        enabled=body.enabled,
        notify_push=body.notify_push,
        home_card=body.home_card,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such task")
    latest = await get_task_runs(request).latest_per_task(ctx, [updated.id])
    return TaskOut.of(updated, latest.get(updated.id))


@router.patch("/tasks/{task_id}")
async def set_enabled(
    request: Request, principal: OwnerDep, task_id: str, body: EnabledPatch
) -> TaskOut:
    ctx = ctx_for(principal)
    updated = await get_task_repo(request).update(ctx, task_id, enabled=body.enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail="no such task")
    latest = await get_task_runs(request).latest_per_task(ctx, [updated.id])
    return TaskOut.of(updated, latest.get(updated.id))


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(request: Request, principal: OwnerDep, task_id: str) -> None:
    await get_task_repo(request).delete(ctx_for(principal), task_id)


@router.post("/tasks/{task_id}/run")
async def run_task(request: Request, principal: OwnerDep, task_id: str) -> TaskRunOut:
    ctx = ctx_for(principal)
    task = await get_task_repo(request).get(ctx, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="no such task")
    info = await get_task_runner(request).run(ctx, task, trigger="manual")
    await get_task_repo(request).mark_ran(ctx, task_id, at=info.started_at)
    return TaskRunOut.of(info)


@router.get("/tasks/{task_id}/runs")
async def task_runs(request: Request, principal: OwnerDep, task_id: str) -> list[TaskRunOut]:
    runs = await get_task_runs(request).list_for_task(ctx_for(principal), task_id)
    return [TaskRunOut.of(r) for r in runs]
