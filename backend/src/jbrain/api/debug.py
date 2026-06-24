"""The owner debug console surface (docs/DEBUG_ACCESS.md).

Every route is gated by `DebugDep` — a live, revocable, time-boxed capability
token (and the JBRAIN_DEBUG_ACCESS_ENABLED flag). The surface is deliberately
narrow and read-leaning: run a prompt through the LLM adapter, run READ-ONLY SQL,
read container logs, and inspect/switch live LLM routing. There are no data-write
or owner-management routes here, and the capability-token lookup is physically
distinct from the owner-cookie path, so a debug token can never escalate.

This is an owner-authorized debugging aid for a TEST box: SQL runs under an owner
RLS context (full read, no domain firewall) but inside a READ-ONLY transaction, so
it can read anything yet write nothing.
"""

import asyncio
import base64
import datetime as dt
import decimal
import uuid
from typing import Annotated, Any, cast

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api import llm_settings
from jbrain.api.deps import AuthRepoDep, DebugDep, SettingsDep
from jbrain.api.llm_settings import LlmSettingsOut, LlmSettingsPut, LoadedModelsOut
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.imageprep import downscale_for_vision
from jbrain.ingest.ocr import (
    DESCRIPTION_MAX_TOKENS,
    DESCRIPTION_SYSTEM,
    OCR_MAX_TOKENS,
    OCR_SYSTEM,
)
from jbrain.llm import LlmImage
from jbrain.llm.errors import LlmError
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import DEFAULT_MAX_TOKENS
from jbrain.models.notes import Attachment
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import BlobStore

log = structlog.get_logger()

router = APIRouter(prefix="/debug")

# The owner authorized full read for this token, so SQL runs as an owner — but
# the transaction is forced read-only, so the firewall isn't needed to keep it
# from writing. A fixed synthetic principal id keeps the audit trail legible.
_OWNER_CTX = SessionContext(principal_id="debug-console", principal_kind="owner")

_MAX_SQL_ROWS = 2000
_READ_PREFIXES = ("select", "with", "explain", "show", "table", "values")


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


def _llm_router(request: Request) -> LlmRouter:
    return cast(LlmRouter, request.app.state.llm_router)


def _blobs(request: Request) -> BlobStore:
    return cast(BlobStore, request.app.state.blob_store)


def _store(request: Request) -> SqlSettingsStore:
    return cast(SqlSettingsStore, request.app.state.settings_store)


def _gateway(request: Request) -> Any:
    return request.app.state.local_gateway


def _supervisor(request: Request) -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, request.app.state.supervisor_client)


class WhoamiOut(BaseModel):
    id: str
    label: str
    kind: str
    # The fixed scope this surface grants, so the assistant knows what it can do.
    scopes: list[str]


@router.get("/whoami")
async def whoami(principal: DebugDep) -> WhoamiOut:
    return WhoamiOut(
        id=principal.id,
        label=principal.label,
        kind=principal.kind,
        scopes=["llm.complete", "sql.read", "logs.read", "llm.routing"],
    )


# --- Self-service token lifecycle (the console's kill switch) ----------------
# A capability token can de-escalate ITSELF — revoke (permanent) or suspend
# (reversible). Both are strictly safe: the only state change a token can make to
# its own grant is to weaken or end it, never extend it. Resume is deliberately
# absent here — a suspended token can no longer authenticate, so waking it back up
# is owner-only (api/debug_tokens.py). 204 even when already revoked/suspended so
# the console's button is idempotent.


@router.post("/revoke-self", status_code=204)
async def revoke_self(principal: DebugDep, repo: AuthRepoDep) -> None:
    """Permanently revoke the presenting token — the console's 'Revoke' button."""
    await repo.revoke_capability(principal.id)


@router.post("/suspend-self", status_code=204)
async def suspend_self(principal: DebugDep, repo: AuthRepoDep) -> None:
    """Pause the presenting token — the console's 'Suspend' button. The owner
    resumes it later from the PWA token list (a suspended token cannot itself)."""
    await repo.suspend_capability(principal.id)


# --- Live activity feed (the console's "watch what's happening" pane) --------


class ActivityEvent(BaseModel):
    seq: int
    ts: str
    method: str
    path: str
    status: int
    kind: str
    # A short, human-readable summary of the command — the SQL text, the prompt, the
    # routing change, the log target — so the console shows WHAT ran, not just the
    # route. Bodies are truncated; "" for routes with nothing to show (whoami).
    detail: str
    # Which console client issued the call (the console tags its own requests so it
    # can skip them in the feed); "" for an external caller (e.g. a curl session).
    client: str


class ActivityOut(BaseModel):
    events: list[ActivityEvent]
    last: int


@router.get("/activity")
async def activity(request: Request, _p: DebugDep, after: int | None = None) -> ActivityOut:
    """Poll the debug-activity ring for entries newer than `after` (every
    /api/debug/* call lands here), so the console can show live what's running —
    including commands an external assistant issues, not just this tab's."""
    return ActivityOut(**request.app.state.debug_activity.snapshot(after))


# --- Prompt iteration -------------------------------------------------------


class CompleteRequest(BaseModel):
    user_text: str = Field(min_length=1)
    system: str = ""
    # Route by a known task (so the live per-task override applies — the realistic
    # path for testing the model the owner actually routes a task to) OR by a raw
    # capability tier. Exactly one is used; task wins. Neither → the 'high' tier.
    task: str | None = None
    strength: str | None = None
    json_schema: dict[str, Any] | None = None
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=1, le=32768)


class CompleteOut(BaseModel):
    text: str
    parsed: Any | None
    # What actually served the call, after live routing overrides — so the
    # assistant sees which model produced the output it is iterating against.
    provider: str
    model: str
    reasoning_effort: str | None
    input_tokens: int
    output_tokens: int


async def _run_completion(router_: LlmRouter, body: CompleteRequest) -> CompleteOut:
    """The shared completion primitive behind both the sync and the async (job)
    routes — all egress stays on the adapter (non-neg #1)."""
    task = body.task or "debug.complete"
    strength = body.strength if body.task is None else None
    if body.task is None and body.strength is None:
        strength = "high"
    try:
        provider, model = await router_.effective_spec(task, strength)
        result = await router_.complete(
            task,
            system=body.system,
            user_text=body.user_text,
            json_schema=body.json_schema,
            max_tokens=body.max_tokens,
            strength=strength,
        )
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    effort = await router_.effective_reasoning_effort(task, strength)
    log.info("debug.complete", task=task, provider=provider, model=model)
    return CompleteOut(
        text=result.text,
        parsed=result.parsed,
        provider=provider,
        model=model,
        reasoning_effort=effort,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
    )


@router.post("/complete")
async def complete(body: CompleteRequest, request: Request, _p: DebugDep) -> CompleteOut:
    """Run one system+user prompt synchronously. Fine for quick calls; a slow model
    (a long, high-effort local extraction) can outlast a proxy's request timeout —
    use /complete-async + /jobs/{id} for those."""
    request.state.debug_detail = body.user_text
    return await _run_completion(_llm_router(request), body)


# --- Vision iteration -------------------------------------------------------
# Drive vision.ocr / vision.caption against an image ALREADY on the box (by
# attachment id) so the OCR/caption prompts can be iterated on the real vision
# model the same way /complete iterates text prompts. Reuses the llm.complete
# scope (vision IS a completion); image bytes flow through the storage
# abstraction (non-neg #2), egress through the adapter (non-neg #1). Read-only:
# the attachment lookup runs in the same owner read-only context as /sql.

# The shipped per-task defaults, applied when the caller passes no system override.
_VISION_DEFAULTS = {
    "vision.ocr": (OCR_SYSTEM, OCR_MAX_TOKENS, "Transcribe this image (file: {name})."),
    "vision.caption": (
        DESCRIPTION_SYSTEM,
        DESCRIPTION_MAX_TOKENS,
        "Describe this image (file: {name}).",
    ),
}


class VisionRequest(BaseModel):
    attachment_id: uuid.UUID
    # Which vision task to run — picks the routed model + the shipped default prompt.
    task: str = "vision.caption"
    # A prompt override to iterate against; empty falls back to the shipped prompt.
    system: str = ""
    # 0 means "use the task's shipped budget"; an explicit value overrides it.
    max_tokens: int = Field(default=0, ge=0, le=32768)


class VisionOut(BaseModel):
    text: str
    provider: str
    model: str
    task: str
    filename: str
    media_type: str


async def _run_vision(
    router_: LlmRouter, blobs: BlobStore, att: Attachment, body: VisionRequest
) -> VisionOut:
    """The vision primitive: load the attachment's bytes, downscale exactly as the
    ingest path does, and run the chosen vision task with an optional prompt
    override. Pure of the DB so it unit-tests with fakes; the route owns the lookup."""
    default = _VISION_DEFAULTS.get(body.task)
    if default is None:
        raise HTTPException(status_code=400, detail=f"unknown vision task: '{body.task}'")
    default_system, default_max, user_tmpl = default
    data, media_type = downscale_for_vision(await blobs.get(att.sha256), att.media_type)
    image = LlmImage(media_type=media_type, data=base64.b64encode(data).decode("ascii"))
    try:
        provider, model = await router_.effective_spec(body.task, "vision")
        result = await router_.complete(
            body.task,
            system=body.system or default_system,
            user_text=user_tmpl.format(name=att.filename),
            images=[image],
            max_tokens=body.max_tokens or default_max,
            strength="vision",
        )
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info("debug.vision", task=body.task, provider=provider, model=model, attachment=str(att.id))
    return VisionOut(
        text=result.text,
        provider=provider,
        model=model,
        task=body.task,
        filename=att.filename,
        media_type=att.media_type,
    )


@router.post("/vision")
async def vision(body: VisionRequest, request: Request, _p: DebugDep) -> VisionOut:
    """Run one vision task (OCR or caption) over an on-box attachment, optionally
    with a candidate system prompt — the image-layer twin of /complete."""
    request.state.debug_detail = f"{body.task} {body.attachment_id}"
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        att = (
            await session.execute(select(Attachment).where(Attachment.id == body.attachment_id))
        ).scalar_one_or_none()
        if att is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        return await _run_vision(_llm_router(request), _blobs(request), att, body)


# --- Async completion jobs (for slow models behind a short proxy timeout) ----
# A long local extraction can take minutes — longer than a Cloudflare Tunnel (or
# any proxy) will hold a request open. So the caller SUBMITS a job (returns at
# once) and POLLS /jobs/{id}; the model call runs in a background task on the box,
# never held open across the wire. The store is in-memory and best-effort — a
# process restart drops in-flight jobs, which is fine for a debug aid.

_MAX_JOBS = 256


class JobSubmitOut(BaseModel):
    job_id: str


class JobStatusOut(BaseModel):
    job_id: str
    status: str  # "pending" | "done" | "error"
    result: CompleteOut | None = None
    error: str | None = None


@router.post("/complete-async", status_code=202)
async def complete_async(body: CompleteRequest, request: Request, _p: DebugDep) -> JobSubmitOut:
    """Submit a completion as a background job; poll GET /jobs/{job_id} for the
    result. Lets the console/harness drive minutes-long calls through a proxy whose
    request timeout is far shorter than the model takes."""
    request.state.debug_detail = body.user_text
    jobs = request.app.state.debug_jobs
    tasks = request.app.state.debug_job_tasks
    router_ = _llm_router(request)
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "pending", "result": None, "error": None}
    # Keep the map bounded: drop the oldest already-finished jobs.
    if len(jobs) > _MAX_JOBS:
        for jid, val in list(jobs.items())[:-_MAX_JOBS]:
            if val["status"] != "pending":
                jobs.pop(jid, None)

    async def _run() -> None:
        try:
            out = await _run_completion(router_, body)
            jobs[job_id] = {"status": "done", "result": out, "error": None}
        except HTTPException as exc:
            jobs[job_id] = {"status": "error", "result": None, "error": str(exc.detail)}
        except Exception as exc:  # noqa: BLE001 - a debug job must surface, not crash the loop
            jobs[job_id] = {"status": "error", "result": None, "error": str(exc)}

    task = asyncio.create_task(_run())
    tasks.add(task)  # hold a ref so the task isn't GC'd mid-flight
    task.add_done_callback(tasks.discard)
    return JobSubmitOut(job_id=job_id)


@router.get("/jobs/{job_id}")
async def job_status(job_id: str, request: Request, _p: DebugDep) -> JobStatusOut:
    """Poll a submitted completion job — pending until the model returns, then the
    full result (or an error message)."""
    job = request.app.state.debug_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return JobStatusOut(
        job_id=job_id, status=job["status"], result=job["result"], error=job["error"]
    )


# --- Read-only SQL ----------------------------------------------------------


class SqlRequest(BaseModel):
    sql: str = Field(min_length=1)
    max_rows: int = Field(default=200, ge=1, le=_MAX_SQL_ROWS)


class SqlOut(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


def _jsonable(value: Any) -> Any:
    """Coerce a DB value to something JSON-serializable for the response."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, decimal.Decimal)):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _is_single_read(sql: str) -> bool:
    """A single read statement: one statement (trailing ';' tolerated) whose first
    keyword is a read verb. The READ-ONLY transaction is the real guard; this just
    rejects obvious misuse with a clean 400 instead of a Postgres error."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped or ";" in stripped:
        return False
    return stripped.split(None, 1)[0].lower() in _READ_PREFIXES


@router.post("/sql")
async def run_sql(body: SqlRequest, request: Request, _p: DebugDep) -> SqlOut:
    """Run one read-only SELECT under an owner RLS context inside a READ-ONLY
    transaction (so it reads everything but can write nothing). 400 on a non-read
    statement or a SQL error."""
    request.state.debug_detail = body.sql
    if not _is_single_read(body.sql):
        raise HTTPException(status_code=400, detail="only a single read-only statement is allowed")
    try:
        async with scoped_session(_maker(request), _OWNER_CTX) as session:
            # set_config (the GUC stamps) are reads, so flipping the txn read-only
            # here still precedes any data statement — writes now error in the engine.
            await session.execute(text("SET TRANSACTION READ ONLY"))
            result = await session.execute(text(body.sql))
            columns = list(result.keys())
            fetched = result.fetchmany(body.max_rows + 1)
    except DBAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc.orig)) from exc
    truncated = len(fetched) > body.max_rows
    rows = [[_jsonable(v) for v in row] for row in fetched[: body.max_rows]]
    log.info("debug.sql", row_count=len(rows), truncated=truncated)
    return SqlOut(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)


# --- Container logs (proxied to the supervisor) -----------------------------


@router.get("/logs/{service}", response_class=PlainTextResponse)
async def logs(
    service: str,
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    tail: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> PlainTextResponse:
    """Tail one container's logs by proxying to the supervisor (the single owner of
    docker access), mirroring the owner ops surface."""
    request.state.debug_detail = f"{service} (tail {tail})"
    resp = await _supervisor(request).get(
        f"/logs/{service}",
        params={"tail": tail},
        headers={"Authorization": f"Bearer {settings.supervisor_token}"},
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"unknown service: {service}")
    resp.raise_for_status()
    return PlainTextResponse(resp.text)


@router.get("/update/status")
async def update_status(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    tail: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, object]:
    """The most recent update one-shot's state + log tail (state, exit_code,
    log_tail), proxied from the supervisor. The updater runs OUTSIDE the compose
    project, so /debug/logs/<service> can't reach it — this is the read-only
    console's only window into why an update (and its local-model sync) failed.
    Mirrors the owner ops surface."""
    request.state.debug_detail = f"update (tail {tail})"
    resp = await _supervisor(request).get(
        "/update/status",
        params={"tail": tail},
        headers={"Authorization": f"Bearer {settings.supervisor_token}"},
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


# --- Live LLM routing (read / switch / load / unload) -----------------------


@router.get("/llm")
async def read_llm(request: Request, settings: SettingsDep, _p: DebugDep) -> LlmSettingsOut:
    return await llm_settings.snapshot(settings, _store(request), _OWNER_CTX, _gateway(request))


@router.put("/llm")
async def switch_llm(
    body: LlmSettingsPut, request: Request, settings: SettingsDep, _p: DebugDep
) -> LlmSettingsOut:
    """Switch which model serves each task, live — the 'choose which AI you're using'
    control. Shares validation with the owner settings screen."""
    request.state.debug_detail = ", ".join(f"{t}→{o.provider}" for t, o in body.tasks.items())
    return await llm_settings.apply_overrides(
        body, settings, _store(request), _OWNER_CTX, _gateway(request)
    )


@router.post("/llm/local-models/{model_id}/load")
async def load_model(
    model_id: str, request: Request, settings: SettingsDep, _p: DebugDep
) -> LoadedModelsOut:
    request.state.debug_detail = model_id
    return await llm_settings.gateway_load(model_id, settings, _gateway(request))


@router.post("/llm/local-models/{model_id}/unload")
async def unload_model(
    model_id: str, request: Request, settings: SettingsDep, _p: DebugDep
) -> LoadedModelsOut:
    request.state.debug_detail = model_id
    return await llm_settings.gateway_unload(model_id, settings, _gateway(request))
