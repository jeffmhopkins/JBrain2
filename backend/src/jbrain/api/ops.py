"""Owner-only proxy to the supervisor container.

The supervisor is never exposed through Caddy; this proxy is the single
authenticated path from the outside world to host control, and it forwards
only the supervisor's fixed command set.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from jbrain import ops_metrics
from jbrain.api.deps import PrincipalDep, SettingsDep, owner_only
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.db.stats import database_stats
from jbrain.storage import BackupShelf, BlobStore
from jbrain.tasks.schedule import FREQS
from jbrain.usage import usage_summary
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
    """The AI usage card: today/month totals, per-task breakdown, last 30
    days — costs estimated at query time from the config price table
    (docs/reference/ANALYSIS.md "Token accounting" / "Cost estimates")."""
    maker = cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)
    ctx = SessionContext(principal_id=principal.id, principal_kind=principal.kind)
    return await usage_summary(maker, ctx, settings.llm_prices)


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
