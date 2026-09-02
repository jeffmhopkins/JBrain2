"""/api/tasks — the owner's scheduled / on-demand agent tasks.

A task is a saved prompt + persona + schedule (docs/mocks/tasks-launcher-README.md,
Direction A). CRUD is owner-only; "Run now" executes the task synchronously through
the shared `TaskRunner` and returns the finished run. The scheduler fires due tasks
on its own (tasks/scheduler.py) — this router is the authoring + history surface.

Validation pins the persona/schedule sets (and a curator's domain scopes) at the
edge; the DB CHECKs are the backstop. The next-fire time is computed in the repo.
"""

from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from jbrain.agent.agents import OWNER_AGENTS
from jbrain.api.deps import owner_only
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo
from jbrain.sdr.command import MAX_FAILURES, key_to_text, new_key
from jbrain.tasks.repo import (
    TaskGroupInfo,
    TaskGroupRepo,
    TaskInfo,
    TaskRepo,
    TaskRunInfo,
    TaskRunRepo,
)
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


def get_task_groups(request: Request) -> TaskGroupRepo:
    return cast(TaskGroupRepo, request.app.state.task_groups)


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
    # The radio command (APRS_CONTROL_PLAN.md P4). There is deliberately NO key field:
    # the box generates the secret and shows it once from the rotate endpoint, so a key
    # never arrives over the wire and a weak one can never be chosen.
    command_word: str | None = Field(default=None, max_length=16)
    command_callsign: str | None = Field(default=None, max_length=16)
    command_days: list[int] = Field(default_factory=list)
    command_from: str | None = None
    command_until: str | None = None

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

    @field_validator("command_days")
    @classmethod
    def _command_days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("command_days must be 0..6 (Sun..Sat)")
        return sorted(set(v))

    @field_validator("command_word")
    @classmethod
    def _word(cls, v: str | None) -> str | None:
        """The word is what the owner keys into a radio head, so it is A-Z0-9 and
        upper-cased here rather than at every comparison. Rejecting the rest also keeps
        anything shaped like a control character out of a matched string."""
        if v is None:
            return None
        word = v.strip().upper()
        if not word:
            return None
        if not word.isalnum() or not word.isascii():
            raise ValueError("a command word is letters and digits only")
        return word

    @field_validator("command_callsign")
    @classmethod
    def _callsign(cls, v: str | None) -> str | None:
        if v is None:
            return None
        call = v.strip().upper()
        if not call:
            return None
        if not all(c.isalnum() or c == "-" for c in call) or not call.isascii():
            raise ValueError("a callsign is letters, digits and an optional -SSID")
        return call

    @field_validator("command_from", "command_until")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if not v:
            return None
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("times are HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("times are HH:MM")
        return f"{hour:02d}:{minute:02d}"

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
        elif self.schedule_kind == "on_command":
            if not self.command_word:
                raise ValueError("a radio command needs a word")
            # Half a range is no range, and a range with only one end would read as a
            # narrowing the box does not actually apply.
            if bool(self.command_from) != bool(self.command_until):
                raise ValueError("a window needs both a from and an until")
            # Firewalled scopes are WARNED, not refused (the decided mock,
            # docs/mocks/aprs/b-trigger-editor.html: "the scopes are the cap here").
            # Blocking them read like defence in depth and is not: location is precisely
            # the domain a radio command wants — "where am I due next" from the truck —
            # and the box never transmits, so a fired task cannot answer over the air.
            # The cap that matters is that only a VERIFIED command fires anything.
            self.schedule_freq = None
            self.schedule_time = None
            self.run_at = None
            self.schedule_days = []
        else:  # on_demand
            self.schedule_freq = None
            self.schedule_time = None
            self.run_at = None
            self.schedule_days = []
        if self.schedule_kind != "on_command":
            # A kind change leaves no armed command behind: the word is what the verify
            # path matches on, so clearing it is what actually disarms it.
            self.command_word = None
            self.command_callsign = None
            self.command_days = []
            self.command_from = None
            self.command_until = None
        return self


def _key_for_edit(existing: TaskInfo, body: TaskBody) -> dict[str, Any]:
    """What an edit does to the credential.

    Three cases, and the middle one is the point: an ordinary task becoming a command
    gets a fresh key; a command that stays a command KEEPS its key, because the owner
    edits the window or the callsign long after they set up the sender and re-keying the
    truck for a schedule tweak would make the feature unusable; and a command that stops
    being one loses its key entirely, so switching it back on cannot resurrect a
    credential someone may have copied in between.

    Whether a key exists is read off `schedule_kind` rather than the key itself — the DB
    CHECK guarantees an `on_command` row has one, and `TaskInfo` deliberately does not
    carry the secret.
    """
    if body.schedule_kind != "on_command":
        return {"command_key": None, "command_counter": 0, "command_failures": 0}
    if existing.schedule_kind == "on_command":
        return {}
    return {"command_key": key_to_text(new_key()), "command_counter": 0, "command_failures": 0}


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
    # The owner-named bucket (NULL = the trailing "Ungrouped" section) + the task's
    # 0-based rank within it; both are set by the reorder / move endpoints.
    group_id: str | None
    position: int
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
    command_word: str | None
    command_callsign: str | None
    command_days: list[int]
    command_from: str | None
    command_until: str | None
    # The counter and the failure tally are shown, the KEY never is: a rotate is the
    # only way it leaves the box. What the owner needs from this screen is whether the
    # command is being tried and whether it has locked itself.
    command_counter: int
    command_failures: int
    command_locked: bool
    command_last_at: datetime | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    # The most recent run, embedded so the card's "latest result" band renders (and
    # opens its session) without a per-card fetch. None until the task has ever run.
    latest_run: "TaskRunOut | None" = None

    @classmethod
    def of(cls, t: TaskInfo, latest_run: "TaskRunInfo | None" = None) -> "TaskOut":
        return cls(
            id=t.id,
            group_id=t.group_id,
            position=t.position,
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
            command_word=t.command_word,
            command_callsign=t.command_callsign,
            command_days=list(t.command_days),
            command_from=t.command_from,
            command_until=t.command_until,
            command_counter=t.command_counter,
            command_failures=t.command_failures,
            command_locked=t.command_failures >= MAX_FAILURES,
            command_last_at=t.command_last_at,
            next_run_at=t.next_run_at,
            last_run_at=t.last_run_at,
            latest_run=TaskRunOut.of(latest_run) if latest_run is not None else None,
        )


class EnabledPatch(BaseModel):
    """The optimistic enable/disable toggle — a narrow PATCH the card uses."""

    enabled: bool


class TaskGroupOut(BaseModel):
    id: str
    name: str
    position: int

    @classmethod
    def of(cls, g: TaskGroupInfo) -> "TaskGroupOut":
        return cls(id=g.id, name=g.name, position=g.position)


class GroupBody(BaseModel):
    """Create / rename payload for a task group."""

    name: str = Field(min_length=1, max_length=80)


class ReorderBody(BaseModel):
    """Authoritative membership + order for one group's list (Direction B). The client
    sends the destination group (NULL = Ungrouped) and its full ordered task ids — a
    within-group drag reorders, a "Move to…" appends the moved id to the destination."""

    group_id: str | None = None
    task_ids: list[str] = Field(default_factory=list, max_length=500)


@router.get("/tasks")
async def list_tasks(request: Request, principal: OwnerDep) -> list[TaskOut]:
    ctx = ctx_for(principal)
    tasks = await get_task_repo(request).list(ctx)
    latest = await get_task_runs(request).latest_per_task(ctx, [t.id for t in tasks])
    return [TaskOut.of(t, latest.get(t.id)) for t in tasks]


@router.get("/task-groups")
async def list_task_groups(request: Request, principal: OwnerDep) -> list[TaskGroupOut]:
    groups = await get_task_groups(request).list(ctx_for(principal))
    return [TaskGroupOut.of(g) for g in groups]


@router.post("/task-groups", status_code=201)
async def create_task_group(request: Request, principal: OwnerDep, body: GroupBody) -> TaskGroupOut:
    created = await get_task_groups(request).create(ctx_for(principal), name=body.name.strip())
    return TaskGroupOut.of(created)


@router.patch("/task-groups/{group_id}")
async def rename_task_group(
    request: Request, principal: OwnerDep, group_id: str, body: GroupBody
) -> TaskGroupOut:
    updated = await get_task_groups(request).rename(
        ctx_for(principal), group_id, name=body.name.strip()
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such group")
    return TaskGroupOut.of(updated)


@router.delete("/task-groups/{group_id}", status_code=204)
async def delete_task_group(request: Request, principal: OwnerDep, group_id: str) -> None:
    # Its tasks fall to Ungrouped (FK SET NULL); they are never deleted.
    await get_task_groups(request).delete(ctx_for(principal), group_id)


@router.post("/tasks/reorder")
async def reorder_tasks(request: Request, principal: OwnerDep, body: ReorderBody) -> list[TaskOut]:
    # A non-null target must name a group the owner owns; the repo returns [] otherwise.
    ctx = ctx_for(principal)
    moved = await get_task_repo(request).reorder(
        ctx, group_id=body.group_id, task_ids=body.task_ids
    )
    latest = await get_task_runs(request).latest_per_task(ctx, [t.id for t in moved])
    return [TaskOut.of(t, latest.get(t.id)) for t in moved]


@router.post("/tasks", status_code=201)
async def create_task(request: Request, principal: OwnerDep, body: TaskBody) -> TaskOut:
    # A command task gets its secret here, once, generated on the box. The owner reveals
    # it by rotating — which is also how they replace one they think is compromised.
    key = key_to_text(new_key()) if body.schedule_kind == "on_command" else None
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
        command_word=body.command_word,
        command_callsign=body.command_callsign,
        command_key=key,
        command_days=body.command_days,
        command_from=body.command_from,
        command_until=body.command_until,
    )
    return TaskOut.of(created)


@router.put("/tasks/{task_id}")
async def replace_task(
    request: Request, principal: OwnerDep, task_id: str, body: TaskBody
) -> TaskOut:
    ctx = ctx_for(principal)
    existing = await get_task_repo(request).get(ctx, task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="no such task")
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
        command_word=body.command_word,
        command_callsign=body.command_callsign,
        command_days=body.command_days,
        command_from=body.command_from,
        command_until=body.command_until,
        # An edit that turns an ordinary task INTO a command needs a key, and one that
        # already has a key keeps it — editing the window must not silently invalidate
        # the sender the owner set up months ago.
        **_key_for_edit(existing, body),
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


class CommandKeyOut(BaseModel):
    """A freshly generated key, returned EXACTLY once — the box keeps no other copy the
    owner can read back. Losing it means rotating again, which is the correct trade: a
    key a screen can re-display is a key a borrowed phone can read."""

    word: str
    key: str


@router.post("/tasks/{task_id}/command-key")
async def rotate_command_key(request: Request, principal: OwnerDep, task_id: str) -> CommandKeyOut:
    """Generate a new shared secret and show it once.

    This is both "set up the sender" and "revoke the old one": the previous key stops
    verifying the moment this returns. The counter goes back to zero with it, because a
    counter is meaningless against a different key."""
    ctx = ctx_for(principal)
    task = await get_task_repo(request).get(ctx, task_id)
    if task is None or task.schedule_kind != "on_command" or not task.command_word:
        raise HTTPException(status_code=404, detail="no such radio command")
    key = key_to_text(new_key())
    await get_task_repo(request).update(
        ctx, task_id, command_key=key, command_counter=0, command_failures=0
    )
    return CommandKeyOut(word=task.command_word, key=key)


@router.post("/tasks/{task_id}/command-unlock")
async def unlock_command(request: Request, principal: OwnerDep, task_id: str) -> TaskOut:
    """Clear the lockout after failed attempts, without touching the key.

    The owner runs this box remotely with no terminal (CLAUDE.md #10), so the way out of
    a lockout has to be a button. It is a separate act from rotating: most lockouts are a
    mis-keyed digit, and making the owner re-key their truck for one is how a safety
    feature becomes the reason the feature gets switched off."""
    ctx = ctx_for(principal)
    updated = await get_task_repo(request).update(ctx, task_id, command_failures=0)
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
