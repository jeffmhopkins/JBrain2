"""Owner-only proxy to the supervisor container.

The supervisor is never exposed through Caddy; this proxy is the single
authenticated path from the outside world to host control, and it forwards
only the supervisor's fixed command set.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jbrain import ops_metrics
from jbrain.agent.prompt_capture import prompt_for
from jbrain.agent.runlog import RunLogReader
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.api.deps import PrincipalDep, SettingsDep, owner_only
from jbrain.api.settings import get_settings_store
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.db.stats import database_stats
from jbrain.storage import BackupShelf, BlobStore
from jbrain.tasks.schedule import FREQS
from jbrain.usage import usage_summary
from jbrain.vitals_ring import VitalsRing
from jbrain.workflow import scheduler
from jbrain.workflow.automations import AutomationsReader
from jbrain.workflow.registry import ActionRegistry

# The schedule kinds the owner can set on a sweep: the legacy fixed `interval` plus
# the task-style spec kinds (mirrors jbrain.tasks.schedule.KINDS + interval).
_SCHEDULE_KINDS = frozenset({"interval", "on_demand", "once", "repeat"})

router = APIRouter(prefix="/ops", dependencies=[Depends(owner_only)])


def _owner_ctx(principal: PrincipalDep) -> SessionContext:
    """The owner session every Automations read/mutation runs under. The RLS
    policies on triggers/schedules/pipelines/actions/runs are the real gate; this
    just carries the authenticated owner identity into the scoped session."""
    return SessionContext(principal_id=principal.id, principal_kind=principal.kind)


def _transcript(request: Request) -> AgentTranscript:
    """The stored-conversation reader. The vitals detail falls back to it for any turn
    with no in-process live handle — a sub-agent, or one that has already settled."""
    return cast(AgentTranscript, request.app.state.agent_transcript)


def _automations_reader(request: Request) -> AutomationsReader:
    return cast(AutomationsReader, request.app.state.automations_reader)


# ---- Automations operator surface (the "Workflow" screen) ------------------
# Read the live engine config + the run log as the "when -> do" cards and the
# action Catalog; mutate the enable flag on a trigger or a schedule. All owner-only
# (the router dep) and RLS-scoped (the reader's sessions). The run-now control is
# the shipped POST /ops/triggers/{id}/run above — reused, not re-implemented.


class StepOut(BaseModel):
    action: str
    cost_class: str
    description: str
    known: bool


class RecentRunOut(BaseModel):
    id: str
    status: str
    started_at: datetime
    duration_ms: int | None
    last_error: str | None


class AutomationOut(BaseModel):
    trigger_id: str
    kind: str  # on_event | schedule
    group: str  # event | reconcile | nightly
    pipeline: str
    enabled: bool
    manual: bool
    steps: list[StepOut]
    recent_runs: list[RecentRunOut]
    on_event: str | None
    schedule_id: str | None
    interval_seconds: int | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    schedule_kind: str | None
    schedule_freq: str | None
    schedule_days: list[int]
    schedule_time: str | None
    run_at: datetime | None
    timezone: str | None


class ActionOut(BaseModel):
    name: str
    cost_class: str
    domain_optional: bool
    mutating: bool
    description: str
    seeded: bool


class AutomationsOut(BaseModel):
    automations: list[AutomationOut]
    actions: list[ActionOut]


class EnabledPatch(BaseModel):
    enabled: bool


class ScheduleBody(BaseModel):
    """Replace a schedule's timing spec — the owner setting a sweep's cadence the way
    a task is scheduled (day/time/repeat). Cross-validated by kind, mirroring
    api.tasks.TaskBody; the DB CHECKs are the backstop. `interval` keeps the legacy
    fixed cadence (the only kind that can express a sub-day reconciler)."""

    schedule_kind: str = "interval"
    interval_seconds: int | None = None
    schedule_freq: str | None = None
    schedule_days: list[int] = Field(default_factory=list)
    schedule_time: str | None = None
    run_at: datetime | None = None
    timezone: str = "UTC"

    @field_validator("schedule_kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in _SCHEDULE_KINDS:
            raise ValueError("unknown schedule kind")
        return v

    @field_validator("schedule_days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("schedule_days must be 0..6 (Sun..Sat)")
        return sorted(set(v))

    @model_validator(mode="after")
    def _coherent(self) -> "ScheduleBody":
        if self.schedule_kind == "interval":
            if self.interval_seconds is None or self.interval_seconds <= 0:
                raise ValueError("interval needs a positive interval_seconds")
            # An interval row carries no wall-clock spec — clear it so the stored row
            # is honest and the tick never reads a stale day/time.
            self.schedule_freq = None
            self.schedule_time = None
            self.run_at = None
            self.schedule_days = []
        elif self.schedule_kind == "repeat":
            self.interval_seconds = None
            if self.schedule_freq not in FREQS:
                raise ValueError("repeat needs a freq of daily|weekdays|weekly")
            if not self.schedule_time:
                raise ValueError("repeat needs a time (HH:MM)")
            if self.schedule_freq == "weekly" and not self.schedule_days:
                raise ValueError("weekly needs at least one day")
        elif self.schedule_kind == "once":
            self.interval_seconds = None
            if self.run_at is None:
                raise ValueError("once needs a run_at instant")
        else:  # on_demand
            self.interval_seconds = None
            self.schedule_freq = None
            self.schedule_time = None
            self.run_at = None
            self.schedule_days = []
        return self


@router.get("/automations")
async def list_automations(request: Request, principal: PrincipalDep) -> AutomationsOut:
    """Every trigger as a "when X -> run Y" card: its kind (on_event | schedule),
    what fires it, the pipeline's ordered steps (cost class + description), enabled
    + manual flags, a recent-run summary — plus the action Catalog. Owner-only,
    RLS-scoped; reflects live DB state (no hardcoded automation list)."""
    view = await _automations_reader(request).load(_owner_ctx(principal))
    return AutomationsOut(
        automations=[
            AutomationOut(
                trigger_id=a.trigger_id,
                kind=a.kind,
                group=a.group,
                pipeline=a.pipeline,
                enabled=a.enabled,
                manual=a.manual,
                steps=[StepOut(**vars(s)) for s in a.steps],
                recent_runs=[RecentRunOut(**vars(r)) for r in a.recent_runs],
                on_event=a.on_event,
                schedule_id=a.schedule_id,
                interval_seconds=a.interval_seconds,
                next_run_at=a.next_run_at,
                last_run_at=a.last_run_at,
                schedule_kind=a.schedule_kind,
                schedule_freq=a.schedule_freq,
                schedule_days=a.schedule_days,
                schedule_time=a.schedule_time,
                run_at=a.run_at,
                timezone=a.timezone,
            )
            for a in view.automations
        ],
        actions=[ActionOut(**vars(act)) for act in view.actions],
    )


@router.get("/actions")
async def list_actions(request: Request, principal: PrincipalDep) -> list[ActionOut]:
    """The action Catalog on its own: every registered action with cost class,
    blast-radius flags, description, and whether it is seeded in app.actions."""
    view = await _automations_reader(request).load(_owner_ctx(principal))
    return [ActionOut(**vars(act)) for act in view.actions]


@router.patch("/triggers/{trigger_id}")
async def patch_trigger(
    trigger_id: str, body: EnabledPatch, request: Request, principal: PrincipalDep
) -> dict[str, object]:
    """Enable/disable a trigger — a real mutation on engine config (e.g. disabling
    note.created->ingest is a deliberate emergency stop). Owner-only, RLS-scoped,
    audited via the existing structured logging. A 404 if no such trigger is in
    scope (the RLS UPDATE policy is the firewall, not this code)."""
    ok = await _automations_reader(request).set_trigger_enabled(
        _owner_ctx(principal), trigger_id, body.enabled
    )
    if not ok:
        raise HTTPException(status_code=404, detail="no such trigger")
    return {"trigger_id": trigger_id, "enabled": body.enabled}


@router.patch("/schedules/{schedule_id}")
async def patch_schedule(
    schedule_id: str, body: EnabledPatch, request: Request, principal: PrincipalDep
) -> dict[str, object]:
    """Enable/disable a schedule (a disabled schedule stops the scheduler tick from
    firing it). Same owner-only, RLS-scoped contract as patch_trigger; 404 on an
    unknown id."""
    ok = await _automations_reader(request).set_schedule_enabled(
        _owner_ctx(principal), schedule_id, body.enabled
    )
    if not ok:
        raise HTTPException(status_code=404, detail="no such schedule")
    return {"schedule_id": schedule_id, "enabled": body.enabled}


@router.put("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str, body: ScheduleBody, request: Request, principal: PrincipalDep
) -> dict[str, object]:
    """Set a schedule's timing spec (kind/freq/days/time/run_at) — the owner editing
    a sweep's cadence like a task. The repo recomputes `next_run_at` from the spec so
    the editor and the scheduler agree on the next fire. Owner-only, RLS-scoped; 404
    on an unknown id. Enable/disable stays on the narrow PATCH; this owns the cadence
    while leaving the toggle untouched."""
    ok = await _automations_reader(request).update_schedule(
        _owner_ctx(principal),
        schedule_id,
        schedule_kind=body.schedule_kind,
        interval_seconds=body.interval_seconds,
        schedule_freq=body.schedule_freq,
        schedule_days=body.schedule_days,
        schedule_time=body.schedule_time,
        run_at=body.run_at,
        timezone=body.timezone,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="no such schedule")
    return {"schedule_id": schedule_id, "schedule_kind": body.schedule_kind}


@router.post("/triggers/{trigger_id}/run", status_code=202)
async def run_trigger(trigger_id: str, request: Request) -> dict[str, object]:
    """Fire a manual trigger now: enqueue its pipeline's action(s) immediately so a
    sweep is runnable from Ops without a service restart (Phase-5 Track B, E4).

    Owner-only (the router dependency). Returns the enqueued job ids — the audit
    handle for the run-log surface. A re-fire is safe: the enqueued handlers keep
    their own dedup and write-once semantics, so a second click never double-writes.
    """
    maker = cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)
    registry = cast(ActionRegistry, request.app.state.action_registry)
    try:
        fired = await scheduler.fire_trigger(maker, registry, trigger_id, require_manual=True)
    except scheduler.ScheduleResolutionError as exc:
        # An unknown/disabled trigger or an unresolvable pipeline is a 404, not a
        # server error: the operator named a trigger that can't be fired.
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {
        "trigger_id": fired.trigger_id,
        "pipeline": fired.pipeline,
        "job_ids": fired.job_ids,
    }


def _client(request: Request) -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, request.app.state.supervisor_client)


def _headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.supervisor_token}"}


@router.get("/status")
async def status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get("/status", headers=_headers(settings))
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


class RestartRequest(BaseModel):
    service: str


@router.post("/restart", status_code=202)
async def restart(
    body: RestartRequest, request: Request, settings: SettingsDep
) -> dict[str, object]:
    resp = await _client(request).post(
        "/restart", json={"service": body.service}, headers=_headers(settings)
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="unknown service")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


# Start/stop a single existing container — the per-container power controls next to
# restart. Both proxy the supervisor's fixed-command gateway (docker start/stop on the
# existing container); an unknown/never-created service 404s.
async def _lifecycle(
    action: str, service: str, request: Request, settings: Settings
) -> dict[str, object]:
    resp = await _client(request).post(
        f"/{action}", json={"service": service}, headers=_headers(settings)
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="unknown service")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.post("/start", status_code=202)
async def start_service(
    body: RestartRequest, request: Request, settings: SettingsDep
) -> dict[str, object]:
    return await _lifecycle("start", body.service, request, settings)


@router.post("/stop", status_code=202)
async def stop_service(
    body: RestartRequest, request: Request, settings: SettingsDep
) -> dict[str, object]:
    return await _lifecycle("stop", body.service, request, settings)


@router.get("/metrics")
async def metrics(
    request: Request, principal: PrincipalDep, settings: SettingsDep
) -> dict[str, object]:
    resp = await _client(request).get("/metrics", headers=_headers(settings))
    resp.raise_for_status()
    merged = cast(dict[str, object], resp.json())

    # DB/blob stats are best-effort: host metrics still render if the
    # database is mid-restart.
    try:
        maker = cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)
        ctx = SessionContext(principal_id=principal.id, principal_kind=principal.kind)
        db = await database_stats(maker, ctx)
        merged["db"] = {
            "db_size_bytes": db.db_size_bytes,
            "note_count": db.note_count,
            "attachment_count": db.attachment_count,
            "attachment_bytes": db.attachment_bytes,
        }
    except Exception:  # noqa: BLE001
        merged["db"] = None

    try:
        blobs = cast(BlobStore, request.app.state.blob_store)
        count, total = blobs.usage()
        merged["blobs"] = {"file_count": count, "total_bytes": total}
    except Exception:  # noqa: BLE001
        merged["blobs"] = None

    # Per-process RSS (via the supervisor's `docker top`) for the memory-breakdown
    # card — biggest first, argv clipped. Best-effort: an older supervisor without
    # /processes (or a hiccup) just leaves the list empty and the card falls back
    # to the per-container `containers` it already has.
    try:
        procs_resp = await _client(request).get("/processes", headers=_headers(settings))
        procs_resp.raise_for_status()
        procs = cast(list[dict[str, object]], procs_resp.json().get("processes", []))
        for p in procs:
            p["command"] = str(p.get("command", ""))[:_PROCESS_CMD_MAX]
        merged["processes"] = sorted(
            procs, key=lambda p: cast(int, p.get("rss_bytes", 0)), reverse=True
        )
    except Exception:  # noqa: BLE001
        merged["processes"] = []

    return merged


# A llama-server argv is long; the model path that distinguishes the co-resident
# models sits near the front, so a generous head is enough for the card's row.
_PROCESS_CMD_MAX = 200


# --- Top-bar vitals (the always-on GPU reading) -----------------------------
# The top bar shows GPU busy % continuously, so this is deliberately NOT the
# /metrics route above: that one runs `docker stats` + `docker top` across every
# container and queries the DB, which is fine once a screen but ruinous once a
# second. These two read one small /sys file and nothing else.

# The top bar's refresh cadence. One second is the point of the surface — the
# owner watches it to see the box respond as a turn runs.
_VITALS_PERIOD_S = 1.0


def _gpu_busy(request: Request) -> float | None:
    """The gauge — from the ONE sampler, never a fresh sysfs read.

    Every GPU figure the API serves comes through here: the top bar's stream, its probe,
    and the roster's reading. They used to take their own `read_gpu_busy_percent()` at
    their own instant, which is how two surfaces showing "the GPU" could disagree — and,
    because that read goes through the amdgpu driver and can stall under heavy allocation,
    it also put a blocking device read on the request path once per frame per client.

    Reading the ring makes both problems structural rather than managed: there is exactly
    one sysfs read per second in the process (vitals_ring.sample_loop, on a thread with a
    timeout), and every surface answers from the same sample, so they cannot disagree.

    Returns None when no sampler has run or its newest sample has gone stale — the
    surfaces already distinguish "no reading" from "no gauge"."""
    ring = cast(VitalsRing | None, getattr(request.app.state, "vitals_ring", None))
    return ring.latest() if ring is not None else None


@router.get("/vitals")
async def vitals(request: Request) -> dict[str, object]:
    """One host-vitals reading. Also the PWA's PROBE: the top-bar meter calls this
    once and only opens the stream below if it succeeds, so a family member (whom
    the router's owner_only rejects) never opens an EventSource that would retry
    against a 403 forever."""
    return {"gpu_busy_percent": _gpu_busy(request)}


async def vitals_frames(
    disconnected: Callable[[], Awaitable[bool]], gauge: Callable[[], float | None]
) -> AsyncIterator[bytes]:
    """SSE frames of host vitals, one per `_VITALS_PERIOD_S`, until the client goes.

    Every frame is sent whether or not the value moved: the constant tick doubles as
    the liveness signal that tells the meter its reading is still current, so a
    stalled stream can go stale rather than freeze on a stale number.

    The disconnect check is a parameter rather than a captured `Request` so the loop
    can be driven directly in tests — `TestClient` never delivers an ASGI
    `http.disconnect`, so a route-local closure over `request.is_disconnected` would
    spin forever with no way to stop it."""
    while not await disconnected():
        payload = json.dumps({"gpu_busy_percent": gauge()})
        yield f"data: {payload}\n\n".encode()
        await asyncio.sleep(_VITALS_PERIOD_S)


class CallStampOut(BaseModel):
    """What a run was asked to do, resolved at turn start (migration 0166). Every field
    is optional: a run whose driver doesn't stamp, or one predating the column, renders
    whatever it has rather than being hidden. The VERBATIM PROMPT is absent by design —
    it is assembled per model call and stored nowhere."""

    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    context_window: int | None = None
    vision: bool | None = None
    persona: str | None = None
    # None is the registry wildcard ("every in-scope tool"), which is not the same
    # thing as the empty list ("no tools") — the surface words them differently.
    tools: list[str] | None = None
    user_message: str | None = None
    user_message_truncated: bool | None = None
    label: str | None = None


class LiveTurnOut(BaseModel):
    id: str
    kind: str
    status: str
    name: str
    started_at: datetime
    # Null while the turn is still running; set once it settles. The roster splits on it.
    ended_at: datetime | None
    elapsed_ms: int
    step_count: int
    cost_tokens: int
    progress_note: str | None
    parent_run_id: str | None
    session_id: str | None
    domain_code: str | None
    ran_as: str
    prompt_version: str | None
    trigger_pipeline: str | None
    call: CallStampOut | None


class LiveTurnsOut(BaseModel):
    turns: list[LiveTurnOut]
    # The gauge alongside the roster, so the surface can say "the GPU is busy but no
    # turn is running" in one breath — that happens legitimately, because GPU busy
    # covers the whole box, image generation included.
    gpu_busy_percent: float | None


@router.get("/turns")
async def live_turns(request: Request, principal: PrincipalDep, seconds: int = 0) -> LiveTurnsOut:
    """The turns on the box, oldest first, with the call each was set up with.

    `seconds` widens the roster from "running right now" (0, the default) to "ran inside
    this window", matching the graph's 1/5/15-minute ranges. Capped at the graph's own
    ceiling so a hand-written query cannot ask for the whole run log through a route
    that returns every field of every row.

    Read from the runs table rather than the in-process live-turn registry: that
    registry holds only parent /chat turns, so a deep-research fan would collapse to one
    row and a workflow run would not appear at all."""
    reader = cast(RunLogReader, request.app.state.run_reader)
    ctx = SessionContext(principal_id=principal.id, principal_kind=principal.kind)
    window = min(max(seconds, 0), _MAX_HISTORY_SECONDS)
    since = datetime.now(tz=UTC) - timedelta(seconds=window) if window > 0 else None
    rows = await reader.list_live(ctx, since=since)
    return LiveTurnsOut(
        turns=[
            LiveTurnOut(
                **{k: v for k, v in vars(row).items() if k != "call_stamp"},
                call=CallStampOut(**row.call_stamp) if row.call_stamp else None,
            )
            for row in rows
        ],
        gpu_busy_percent=_gpu_busy(request),
    )


class TurnStepOut(BaseModel):
    idx: int
    kind: str
    name: str
    # None while the step is still in flight — a live tool call, not a failed one.
    ok: bool | None
    cost_tokens: int
    error: str | None


class TurnOutputOut(BaseModel):
    """A turn's raw output, and where it was read from.

    Two sources by necessity. A live PARENT /chat turn has an in-process render
    accumulator, which is the only way to see a turn mid-answer. Everything else — a
    sub-agent (no live handle), or any settled turn — reads its stored transcript. The
    `live` flag says which, so the surface can label a streaming turn honestly instead
    of implying a finished answer is still arriving."""

    live: bool
    answer: str
    reasoning: str
    steps: list[TurnStepOut]


class PromptMessageOut(BaseModel):
    role: str
    content: str


class CapturedPromptOut(BaseModel):
    """The assembled prompt this run last sent to the model.

    Kept in memory for the life of the process (agent/prompt_capture.py), so it is
    absent for a run that predates a restart, was evicted by a newer one, or never
    reached a model call. `truncated` says the text was clipped, so a long prompt reads
    as clipped rather than as a short one."""

    system: str
    messages: list[PromptMessageOut]
    tools: list[str]
    round_index: int
    truncated: bool
    system_chars: int
    message_chars: int


class TurnDetailOut(BaseModel):
    steps: list[TurnStepOut]
    output: TurnOutputOut | None
    prompt: CapturedPromptOut | None


def _steps_from_snapshot(tools: list[dict[str, Any]]) -> list[TurnStepOut]:
    return [
        TurnStepOut(
            idx=i,
            kind=str(tool.get("name") or "step"),
            name=str(tool.get("summary") or tool.get("name") or ""),
            ok=cast(bool | None, tool.get("ok")),
            cost_tokens=0,
            error=None,
        )
        for i, tool in enumerate(tools)
    ]


@router.get("/turns/{run_id}")
async def turn_detail(run_id: str, request: Request, principal: PrincipalDep) -> TurnDetailOut:
    """One running turn's step trail and raw output, for the vitals detail's level 2.

    The step trail comes from run_steps, which every run kind writes. The output comes
    from the live accumulator when this run is a parent /chat turn still in flight, and
    from the stored transcript otherwise — a sub-agent never registers a live handle,
    so without the transcript fallback a fan's children would all read as silent."""
    ctx = SessionContext(principal_id=principal.id, principal_kind=principal.kind)
    reader = cast(RunLogReader, request.app.state.run_reader)
    detail = await reader.load(ctx, run_id)
    steps = [
        TurnStepOut(
            idx=s.idx, kind=s.kind, name=s.name, ok=s.ok, cost_tokens=s.cost_tokens, error=s.error
        )
        for s in (detail.steps if detail else [])
    ]

    captured = prompt_for(run_id)
    prompt = (
        CapturedPromptOut(
            system=captured.system,
            messages=[PromptMessageOut(**m) for m in captured.messages],
            tools=captured.tools,
            round_index=captured.round_index,
            truncated=captured.truncated,
            system_chars=captured.system_chars,
            message_chars=captured.message_chars,
        )
        if captured is not None
        else None
    )

    live = request.app.state.live_turns.get(run_id)
    acc = getattr(live, "acc", None) if live is not None else None
    if live is not None and acc is not None and not live.done:
        snapshot = cast(dict[str, Any], acc.render_snapshot())
        # run_steps rows are written on completion, so the trail would end at the last
        # finished step and never show the one actually running. The live accumulator
        # knows about that one, so its tail past the persisted count is appended —
        # keeping cost for the finished steps and truth for the in-flight one.
        snapshot_steps = _steps_from_snapshot(
            cast(list[dict[str, Any]], snapshot.get("tools") or [])
        )
        trail = steps + [
            TurnStepOut(**{**vars(s), "idx": len(steps) + i})
            for i, s in enumerate(snapshot_steps[len(steps) :])
        ]
        return TurnDetailOut(
            steps=trail,
            prompt=prompt,
            output=TurnOutputOut(
                live=True,
                answer=str(snapshot.get("content") or ""),
                reasoning=str(snapshot.get("reasoning") or ""),
                steps=_steps_from_snapshot(cast(list[dict[str, Any]], snapshot.get("tools") or [])),
            ),
        )

    # No live handle. The transcript's last assistant turn is only THIS run's output if
    # this run has finished — a /chat turn registers its live handle several awaits
    # after its row exists, so a roster row opened in that window would otherwise show
    # the PREVIOUS exchange's answer under this turn's header, badged as if it were its
    # own. A running turn we cannot see is reported as no output rather than as someone
    # else's; the screen words that by the run's status.
    settled = detail is not None and detail.status != "running"
    session_id = detail.session_id if detail is not None and settled else None
    if session_id:
        turns = await _transcript(request).load(ctx, session_id)
        answers = [t for t in turns if t.role == "assistant"]
        if answers:
            latest = answers[-1]
            return TurnDetailOut(
                steps=steps,
                prompt=prompt,
                output=TurnOutputOut(
                    live=False,
                    answer=latest.content,
                    reasoning=latest.reasoning or "",
                    steps=[],
                ),
            )
    return TurnDetailOut(steps=steps, output=None, prompt=prompt)


class VitalsSampleOut(BaseModel):
    at_ms: int
    gpu: float | None


class VitalsHistoryOut(BaseModel):
    samples: list[VitalsSampleOut]


# The widest window the detail graph offers. A request past this is clamped rather than
# rejected — the ring simply has no more to give.
_MAX_HISTORY_SECONDS = 900


@router.get("/vitals/history")
async def vitals_history(request: Request, seconds: int = _MAX_HISTORY_SECONDS) -> VitalsHistoryOut:
    """The GPU load already recorded, so the detail graph opens with a past instead of
    filling from empty after a reload. Sampled once a second in-process (vitals_ring)."""
    ring = cast(VitalsRing, request.app.state.vitals_ring)
    window = max(1, min(seconds, _MAX_HISTORY_SECONDS))
    return VitalsHistoryOut(
        samples=[VitalsSampleOut(**cast(dict[str, Any], s)) for s in ring.since(window)]
    )


class ClientVitalsReport(BaseModel):
    """What the BROWSER saw of the vitals stream.

    Reported by the client because the box cannot see any of it. A connection the browser
    declines to open — because it has hit its per-origin cap, say — leaves no trace here at
    all, which is exactly the state that had the top bar blank while the box was serving
    97% perfectly well. Free-form and bounded: this is a diagnostic, not an API, and it must
    never fail a request over an unexpected key."""

    model_config = ConfigDict(extra="allow")


@router.post("/client-vitals", status_code=204)
async def report_client_vitals(body: ClientVitalsReport, request: Request) -> None:
    """Record the browser's view of its own vitals stream. Owner-only (the router's dep),
    last-report-wins, in memory — it answers "what is the meter doing right now", and a
    restart losing it costs nothing."""
    request.app.state.client_vitals = {
        "at": datetime.now(tz=UTC).isoformat(),
        **body.model_dump(),
    }


@router.get("/vitals/stream")
async def vitals_stream(request: Request) -> StreamingResponse:
    """Host vitals as SSE at 1 Hz, for the top bar's GPU reading.

    The headers are load-bearing, not decoration. An intermediary that BUFFERS a
    `text/event-stream` turns this into a socket that connects, reports itself healthy,
    and delivers nothing — a failure EventSource has no error event for, so the meter sat
    blank while the same data read fine over ordinary fetches. `no-cache` and
    `X-Accel-Buffering: no` are the two asks that make a proxy pass frames straight
    through; the client's own silence watchdog is the backstop for one that ignores
    them."""
    return StreamingResponse(
        vitals_frames(request.is_disconnected, lambda: _gpu_busy(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# The history ranges the Ops graph offers. Spans up to RAW_QUERY_MAX read raw
# 30s samples (downsampled); wider spans read the hourly rollup (jbrain.ops_metrics).
_HISTORY_RANGES: dict[str, timedelta] = {
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "2d": timedelta(days=2),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "1y": timedelta(days=365),
}


@router.get("/metrics/history")
async def metrics_history(
    request: Request, principal: PrincipalDep, range: str = "24h"
) -> dict[str, object]:
    """Downsampled host-metrics time series for the Ops graph (owner-only,
    RLS-scoped). `range` selects the window; the resolution (raw vs hourly rollup)
    and bucket width are chosen server-side so the payload stays small."""
    window = _HISTORY_RANGES.get(range)
    if window is None:
        raise HTTPException(status_code=400, detail=f"unknown range: {range}")
    maker = cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)
    ctx = SessionContext(principal_id=principal.id, principal_kind=principal.kind)
    return await ops_metrics.history(maker, ctx, since=datetime.now(tz=UTC) - window)


@router.get("/llm-usage")
async def llm_usage(
    request: Request, principal: PrincipalDep, settings: SettingsDep
) -> dict[str, object]:
    """The AI usage card: today/month/all-time totals, per-task breakdown, last 30
    days — costs estimated at query time from the config price table
    (docs/reference/ANALYSIS.md "Token accounting" / "Cost estimates"). The
    today/month buckets roll over at the owner's local midnight, not UTC's."""
    maker = cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)
    ctx = SessionContext(principal_id=principal.id, principal_kind=principal.kind)
    tz_name = await get_settings_store(request).owner_timezone(ctx)
    return await usage_summary(maker, ctx, settings.llm_prices, tz_name=tz_name)


@router.post("/update", status_code=202)
async def start_update(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).post("/update", headers=_headers(settings))
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="update already running")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/update/status")
async def update_status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get(
        "/update/status", params={"tail": 80}, headers=_headers(settings)
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


# --- Per-service rebuild (the PWA per-container "Rebuild" button) --------
# Rebuild ONE service (compose build + up -d) via a supervisor one-shot, to apply a
# code/Dockerfile change already on the box (e.g. a newly-baked tts-stt voice)
# without a full system update. Shares the one-shot mutual-exclusion guard.


class RebuildRequest(BaseModel):
    service: str


@router.post("/rebuild", status_code=202)
async def start_rebuild(
    body: RebuildRequest, request: Request, settings: SettingsDep
) -> dict[str, object]:
    resp = await _client(request).post(
        "/rebuild", json={"service": body.service}, headers=_headers(settings)
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="unknown service")
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="another operation is running")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/rebuild/status")
async def rebuild_status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get(
        "/rebuild/status", params={"tail": 80}, headers=_headers(settings)
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


# --- Local-model download (the PWA "Download" action) -------------------
# Installing an on-box model no longer rides a full system update. This triggers
# the supervisor's provision one-shot — deploy/local-models-sync.sh alone (download
# weights + re-stamp llama-swap + restart the gateway), with no git pull or rebuild.
# It shares the one-shot mutual-exclusion guard, so it 409s during an update/export.


@router.post("/local-provision", status_code=202)
async def start_local_provision(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).post("/provision", headers=_headers(settings))
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="another operation is running")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/local-provision/status")
async def local_provision_status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get(
        "/provision/status", params={"tail": 80}, headers=_headers(settings)
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


# --- Data export/import -------------------------------------------------
# Heavy lifting happens in supervisor-launched one-shots (they have docker;
# the api deliberately has neither superuser DB access nor pg_dump). The api
# proxies start/status and moves archive bytes via the shared backups mount.


def _shelf(request: Request) -> BackupShelf:
    return cast(BackupShelf, request.app.state.backup_shelf)


@router.post("/export", status_code=202)
async def start_export(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).post("/export", headers=_headers(settings))
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="another operation is running")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/export/status")
async def export_status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get(
        "/export/status", params={"tail": 80}, headers=_headers(settings)
    )
    resp.raise_for_status()
    status = cast(dict[str, object], resp.json())
    # A finished export's filename comes from the shelf, not the log text.
    status["filename"] = (
        _shelf(request).latest_export()
        if status.get("state") == "exited" and status.get("exit_code") == 0
        else None
    )
    return status


@router.get("/export/file/{name}")
async def download_export(name: str, request: Request) -> FileResponse:
    try:
        path = _shelf(request).export_path(name)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such export") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such export")
    return FileResponse(path, media_type="application/x-tar", filename=name)


@router.post("/import/upload", status_code=201)
async def upload_import(request: Request, file: UploadFile) -> dict[str, str]:
    async def chunks() -> AsyncIterator[bytes]:
        while data := await file.read(1 << 20):
            yield data

    name = await _shelf(request).save_import(chunks())
    return {"archive": name}


class ImportStartRequest(BaseModel):
    archive: str


@router.post("/import/start", status_code=202)
async def start_import(
    body: ImportStartRequest, request: Request, settings: SettingsDep
) -> dict[str, object]:
    resp = await _client(request).post(
        "/import", json={"archive": body.archive}, headers=_headers(settings)
    )
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="another operation is running")
    if resp.status_code == 400:
        raise HTTPException(status_code=400, detail="bad archive name")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/import/status")
async def import_status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get(
        "/import/status", params={"tail": 80}, headers=_headers(settings)
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


# Reset is a supervisor one-shot for the same reason export/import are: the
# api's RLS-scoped role deliberately cannot TRUNCATE (least privilege — RLS
# does not bind TRUNCATE), so erasing content data takes superuser psql that
# only a supervisor-launched container holds.


@router.post("/reset", status_code=202)
async def start_reset(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).post("/reset", headers=_headers(settings))
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="another operation is running")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/reset/status")
async def reset_status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get(
        "/reset/status", params={"tail": 80}, headers=_headers(settings)
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/logs/{service}")
async def logs(
    service: str,
    request: Request,
    settings: SettingsDep,
    tail: int = 200,
) -> PlainTextResponse:
    resp = await _client(request).get(
        f"/logs/{service}", params={"tail": tail}, headers=_headers(settings)
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="unknown service")
    resp.raise_for_status()
    return PlainTextResponse(resp.text)


@router.get("/logs/{service}/stream")
async def logs_stream(service: str, request: Request, settings: SettingsDep) -> StreamingResponse:
    client = _client(request)

    async def relay() -> AsyncIterator[bytes]:
        async with client.stream(
            "GET", f"/logs/{service}/stream", headers=_headers(settings), timeout=None
        ) as upstream:
            if upstream.status_code != 200:
                return
            async for chunk in upstream.aiter_bytes():
                yield chunk

    return StreamingResponse(relay(), media_type="text/event-stream")
