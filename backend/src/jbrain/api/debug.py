"""The owner debug console surface (docs/runbooks/DEBUG_ACCESS.md).

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
import contextlib
import datetime as dt
import decimal
import json
import math
import re
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import box_events, media
from jbrain.agent.attachments import is_video_media_type
from jbrain.agent.chat_images import ImageTooLarge, UndecodableImage, image_dimensions
from jbrain.agent.grounding import (
    Convention,
    UnknownGroundingModel,
    convention_for,
    infer_convention,
    parse_grounding,
    to_pixels,
)
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.api import endpoint as endpoint_api
from jbrain.api import engine as engine_api
from jbrain.api import llm_settings, nudge, panel_ws
from jbrain.api import sdr as sdr_api
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
from jbrain.ingest.video import FRAME_CAPTION_TASK as VIDEO_FRAME_TASK
from jbrain.ingest.video import SUMMARY_TASK as VIDEO_SUMMARY_TASK
from jbrain.ingest.video import run_video_analysis, transcribe_audio_chunked
from jbrain.llm import LlmImage, kv_prefix, llama_swap_config, local_catalog, slot_roles
from jbrain.llm import engine as llm_engine
from jbrain.llm.errors import LlmError
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.router import LlmRouter
from jbrain.llm.slot_roles import SlotPin, SlotRole, role_for, slot_pin
from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    AssistantMessage,
    LlmMessage,
    LlmTool,
    LlmTurn,
    LlmVideo,
    ReasoningChunk,
    Sampling,
    TextChunk,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from jbrain.models.agent import TurnAttachment
from jbrain.models.notes import Attachment
from jbrain.models.telemetry import DeployHistoryRepo
from jbrain.sdr.resolve import for_purpose, refusal
from jbrain.sdr.roles import GENERAL, Radio
from jbrain.sdr.sweep import channels, reduce_csv, steady_channels, waterfall_png
from jbrain.sdr.tuner import MAX_MHZ, TUNABLE_MIN_MHZ, nodes_in, out_of_range
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import BlobStore
from jbrain.transcribe import WhisperCppClient
from jbrain.web.fetch import WebFetcher, WebFetchError
from jbrain.web.moltbook import scrub_secret

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


async def _radio(request: Request, settings: Any, want: str) -> str | None:
    """Which radio a debug call may open, or a 409 naming what the owner must fix.

    The debug console is a THIRD door onto the same radio, beside the PWA routes and
    jerv's tools, and a rule enforced at two of three doors is not enforced: a sweep or
    a capture from here could take the dongle the owner reserved for APRS. Same resolver
    as the other two, under the console's owner context."""
    choice = await for_purpose(
        _supervisor(request),
        settings.supervisor_token,
        _store(request),
        _OWNER_CTX,
        want,
        settings.sdr_url,
    )
    detail = refusal(choice)
    if detail is not None:
        raise HTTPException(status_code=409, detail=detail)
    return choice.serial


async def _rig(request: Request, serial: str | None) -> Radio:
    """What the owner has said about that radio's signal path — its pinned gain and any
    converter in front of it.

    The debug console is a third door onto the same hardware, so it has to honour the
    same settings: a sweep from here that forgot the converter would tune the raw
    shortwave frequency, power the tuner down, and measure an antenna the converter's
    output is not connected to — a picture of silence, confidently labelled."""
    if not serial:
        return Radio(serial="")
    stored = await _store(request).sdr_radios(_OWNER_CTX)
    return stored.get(serial) or Radio(serial=serial)


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
        scopes=[
            "llm.complete",
            "sql.read",
            "logs.read",
            "llm.routing",
            # The gateway surface: load/unload, the served `-c`, `-np`, launch flags via the
            # allowlist, KV-slot save/restore, props/slots/metrics, and prime. Listed because
            # this list is what an assistant reads to decide what it may attempt, and omitting
            # these read as "not permitted" — a session lost real time believing the flag sweep
            # it had been asked to run was out of scope, when every route was already open.
            "llm.gateway",
            "host.read",
            "host.metrics",
            "web.fetch",
            # Deploy: pull main, rebuild, restart (`POST /update`). Listed for the same
            # reason `llm.gateway` is — a capability missing from this list reads as one
            # the assistant may not use, and a session that believes it cannot deploy
            # waits on a human for something it was handed the means to do.
            "ops.update",
            # Freeing what `GET /disk` reports as reclaimable (`POST /disk/cleanup`): the
            # build cache, unused non-stack images and allowlisted orphan volumes. Listed
            # for the same reason as `ops.update`.
            "ops.disk_cleanup",
            # A panel's own console over USB (`GET /endpoint/console`). Listed for the
            # same reason as the two above: without it an assistant reads "cannot see the
            # device" and hands the owner an errand instead of looking.
            "endpoint.console",
            # Flashing a panel over USB with the remembered network (`POST
            # /endpoint/flash`). Listed for the same reason as the rest: a capability
            # missing from this list reads as one the assistant may not use.
            "endpoint.flash",
        ],
    )


class VersionOut(BaseModel):
    # The git commit the running server's image was built from (baked at build
    # time — see config.Settings.git_sha), and a friendlier `git describe` string.
    git_sha: str
    git_describe: str
    # When that image was built and when THIS process started — together they answer
    # "did the box actually restart onto the newly-published build?" (a stamped build
    # sitting behind a process that started before it means the deploy didn't recreate).
    build_time: str
    started_at: str | None


@router.get("/version")
async def version(request: Request, settings: SettingsDep, _p: DebugDep) -> VersionOut:
    """The exact source revision the running server was built from — baked into the
    image at build time, so an external assistant can confirm what is deployed
    instead of guessing whether a merge is live. `started_at` is when this process
    came up (a fresh image only takes effect once the container is recreated)."""
    started = getattr(request.app.state, "started_at", None)
    return VersionOut(
        git_sha=settings.git_sha,
        git_describe=settings.git_describe,
        build_time=settings.build_time,
        started_at=started.isoformat() if started is not None else None,
    )


class DeployRow(BaseModel):
    git_sha: str
    git_describe: str
    build_time: str
    # When this version was first seen running — the interval [deployed_at, next row)
    # is when it was live, so a record's timestamp maps to the build that produced it.
    deployed_at: str


class VersionHistoryOut(BaseModel):
    deploys: list[DeployRow]


@router.get("/version/history")
async def version_history(
    request: Request,
    _p: DebugDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> VersionHistoryOut:
    """The recorded history of deployed versions, newest first (app.deploy_history —
    one row per version change, written on boot). Lets an assistant answer *retro*-
    actively which build was live when an older run happened, not just what runs now.
    Read-only owner query, like /sql."""
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        rows = await DeployHistoryRepo().recent(session, limit)
    return VersionHistoryOut(
        deploys=[
            DeployRow(
                git_sha=r.git_sha,
                git_describe=r.git_describe,
                build_time=r.build_time,
                deployed_at=r.deployed_at.isoformat(),
            )
            for r in rows
        ]
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
    # Per-call sampling override, e.g. {"temperature": 0.1, "min_p": 0.0}. Merges over the
    # model's catalog defaults exactly as a prompt's `config: sampling:` block does.
    #
    # The only way to vary sampling from this box at all. It is catalog-static per model with no
    # settings key and no endpoint, so the whole class of failure that is NOT a launch flag —
    # degenerate repetition, a model that will not stop, malformed tool-call blocks, the `min_p`
    # trap the catalog itself warns about — could not be A/B'd against a live model. Per-request
    # and unpersisted: no reload, and nothing here can outlive the call that set it.
    sampling: dict[str, Any] | None = None
    # Run the turn through the STREAMING adapter path rather than the one-shot one. The two
    # are different code on this box — `converse_stream` is what a real chat turn takes, and
    # anything that only happens while a turn streams (the prefill fraction, first-token
    # latency, the reasoning channel arriving separately from the answer) is invisible to a
    # console that can only call the one-shot path. Not an SSE response: the frames are
    # buffered on the box and pulled from /jobs/{id}, because a stream held open across a
    # Cloudflare Tunnel dies at its request timeout exactly like a long completion does.
    stream: bool = False
    # The pooled-engine slot to run in. Omitted: the task's own role, except that a task
    # whose role is jerv's interactive slot runs in the workshop slot instead, so a console
    # probe never overwrites the persona prefix the owner's next chat turn reuses.
    slot_role: SlotRole | None = None


class StreamFrame(BaseModel):
    """One streamed part, stamped with when it arrived relative to the request going out.

    `at_ms` is the whole point. A transcript says what the model answered; the timings say
    where the wait WAS — a long first gap is prefill, an even cadence after it is generation,
    and the two have entirely different causes and fixes."""

    at_ms: int
    kind: str  # "text" | "reasoning" | "final"
    text: str


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
    # Streamed runs only. `ttft_ms` is the gap before the first part — the prefill wait,
    # and the reading this whole surface exists to make visible from a box with no terminal.
    ttft_ms: int | None = None
    frames: list[StreamFrame] | None = None


# A streamed debug run keeps every part it saw, but a chatty model can emit thousands. Past
# this the frames stop being recorded and the count is reported instead: the shape of a turn —
# where the gap was, how fast tokens came after it — is settled long before this.
_MAX_STREAM_FRAMES = 400


def _slot_pin(task: str, asked: SlotRole | None) -> SlotPin:
    """The `slot_role` keyword a console call passes, or nothing (the task's own role)."""
    if asked is None and role_for(task) == SlotRole.INTERACTIVE:
        return slot_pin(SlotRole.WORKSHOP)
    return slot_pin(asked)


async def _run_stream(
    router_: LlmRouter,
    body: CompleteRequest,
    task: str,
    strength: str | None,
    sampling: Sampling | None,
) -> tuple[LlmTurn, int | None, list[StreamFrame]]:
    """Drive `converse_stream` and buffer what comes back, with arrival times.

    Same adapter, same route resolution as `_run_completion` — the only difference is which
    of the router's two methods is called, which is exactly the difference worth being able
    to exercise from here."""
    started = time.perf_counter()
    ttft_ms: int | None = None
    frames: list[StreamFrame] = []
    final: LlmTurn | None = None

    def _stamp() -> int:
        return int((time.perf_counter() - started) * 1000)

    async for part in router_.converse_stream(
        task,
        system=body.system,
        messages=[UserMessage(text=body.user_text)],
        max_tokens=body.max_tokens,
        strength=strength,
        sampling=sampling,
        **_slot_pin(task, body.slot_role),
    ):
        at = _stamp()
        if ttft_ms is None:
            ttft_ms = at
        if len(frames) < _MAX_STREAM_FRAMES:
            if isinstance(part, TextChunk):
                frames.append(StreamFrame(at_ms=at, kind="text", text=part.text))
            elif isinstance(part, ReasoningChunk):
                frames.append(StreamFrame(at_ms=at, kind="reasoning", text=part.text))
        if isinstance(part, LlmTurn):
            final = part
            frames.append(StreamFrame(at_ms=at, kind="final", text=""))
    if final is None:
        raise HTTPException(status_code=502, detail="stream ended without a final turn")
    return final, ttft_ms, frames


async def _run_completion(router_: LlmRouter, body: CompleteRequest) -> CompleteOut:
    """The shared completion primitive behind both the sync and the async (job)
    routes — all egress stays on the adapter (non-neg #1)."""
    task = body.task or "debug.complete"
    strength = body.strength if body.task is None else None
    if body.task is None and body.strength is None:
        strength = "high"
    try:
        sampling = Sampling.from_mapping(body.sampling) if body.sampling else None
    except ValueError as exc:
        # 422, not a silent drop: a caller that believes it set a knob and did not would
        # misread the very comparison it ran this call to make.
        raise HTTPException(status_code=422, detail=f"bad sampling override: {exc}") from exc
    try:
        provider, model = await router_.effective_spec(task, strength)
        if body.stream:
            turn, ttft_ms, frames = await _run_stream(router_, body, task, strength, sampling)
            effort = await router_.effective_reasoning_effort(task, strength)
            log.info("debug.complete", task=task, provider=provider, model=model, stream=True)
            return CompleteOut(
                text=turn.text,
                # No schema pass on a streamed run: `json_schema` is a one-shot concern (the
                # router validates the whole body), and honouring it here would mean claiming
                # a parse this path never did.
                parsed=None,
                provider=provider,
                model=model,
                reasoning_effort=effort,
                input_tokens=turn.usage.input_tokens,
                output_tokens=turn.usage.output_tokens,
                ttft_ms=ttft_ms,
                frames=frames,
            )
        result = await router_.complete(
            task,
            system=body.system,
            user_text=body.user_text,
            json_schema=body.json_schema,
            max_tokens=body.max_tokens,
            strength=strength,
            sampling=sampling,
            **_slot_pin(task, body.slot_role),
        )
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    effort = await router_.effective_reasoning_effort(task, strength)
    log.info("debug.complete", task=task, provider=provider, model=model, stream=False)
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


# --- Tool-calling probe -----------------------------------------------------
# Send a CHOSEN set of tool schemas to a routed model and return the model's proposed
# tool calls — never executing a handler. Purpose-built to diagnose a model/gateway
# tool-calling failure remotely: vary `tools` (by name, from the live registry) and see
# which set errors. E.g. bisect "does gpt-oss crash at N tools?" by probing 15 vs 17
# names, or send the full set to reproduce a crash. Reuses the llm.complete scope (it IS
# a converse); no data write, and no tool handler ever runs — only schemas go to the model.


class ToolProbeRequest(BaseModel):
    user_text: str = Field(min_length=1)
    system: str = ""
    # Route like /complete: a known task (live overrides apply) or a raw strength tier.
    task: str = "agent.turn"
    strength: str | None = None
    # Registry tool NAMES to attach as schemas. Empty = no tools (a control run). Unknown
    # names 400 so a typo is obvious rather than silently probing the wrong set.
    tools: list[str] = Field(default_factory=list)
    # Inline tool schemas, appended after the registry ones. Each is {name, description,
    # input_schema}. This is the bisect knob: send a MUTATED copy of a real tool's schema
    # (strip a field, drop an enum, replace fancy punctuation) to find which construct the
    # gateway's tool-grammar builder chokes on — impossible with registry names alone.
    raw_tools: list[dict[str, Any]] = Field(default_factory=list)
    max_tokens: int = Field(default=2048, ge=1, le=32768)
    # The pooled-engine slot to run in. Omitted: the task's own role, except that a task
    # whose role is jerv's interactive slot runs in the workshop slot instead, so a console
    # probe never overwrites the persona prefix the owner's next chat turn reuses.
    slot_role: SlotRole | None = None


class ToolProbeOut(BaseModel):
    provider: str
    model: str
    tool_count: int
    # The model's PROPOSED calls (name + arguments), never executed. Empty when it answered
    # without calling a tool.
    tool_calls: list[dict[str, Any]]
    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    # Populated (with tool_calls empty) when the converse failed — e.g. the gateway crashed
    # on the tool payload ("local: HTTP 500"). Returned 200 so probes are easy to compare.
    error: str | None = None


@router.post("/tool-probe")
async def tool_probe(body: ToolProbeRequest, request: Request, _p: DebugDep) -> ToolProbeOut:
    """Probe tool-calling with a specified schema set (no handler runs). See the module note."""
    request.state.debug_detail = f"{len(body.tools)} tools: {','.join(body.tools[:24])}"
    registry = cast(ToolRegistry, request.app.state.agent_registry)
    unknown = [t for t in body.tools if t not in registry.names()]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown tools: {unknown}")
    llm_tools = [registry.get(name).as_llm_tool() for name in body.tools]
    try:
        llm_tools += [
            LlmTool(
                name=rt["name"],
                description=rt.get("description", ""),
                input_schema=rt.get("input_schema", {"type": "object", "properties": {}}),
            )
            for rt in body.raw_tools
        ]
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"raw_tools need a 'name' (and optional description/input_schema): {exc}",
        ) from exc
    router_ = _llm_router(request)
    provider, model = await router_.effective_spec(body.task, body.strength)
    log.info(
        "debug.tool_probe", task=body.task, provider=provider, model=model, tools=len(llm_tools)
    )
    try:
        turn = await router_.converse(
            body.task,
            system=body.system,
            messages=[UserMessage(text=body.user_text)],
            tools=llm_tools,
            max_tokens=body.max_tokens,
            strength=body.strength,
            **_slot_pin(body.task, body.slot_role),
        )
    except LlmError as exc:
        return ToolProbeOut(
            provider=provider,
            model=model,
            tool_count=len(llm_tools),
            tool_calls=[],
            text="",
            stop_reason="error",
            input_tokens=0,
            output_tokens=0,
            error=str(exc),
        )
    return ToolProbeOut(
        provider=provider,
        model=model,
        tool_count=len(llm_tools),
        tool_calls=[{"name": c.name, "arguments": c.arguments} for c in turn.tool_calls],
        text=turn.text,
        stop_reason=turn.stop_reason,
        input_tokens=turn.usage.input_tokens,
        output_tokens=turn.usage.output_tokens,
    )


# --- Multi-turn sitting replay ----------------------------------------------
# Drive a jmolt sitting PAST ITS FIRST MOVE by feeding back recorded tool results, so a
# prompt change can be measured against the decision it is supposed to affect.
#
# Why this exists. `/tool-probe` returns ONE proposed call, and jmolt's opening move is
# pinned by a sentence of the prologue ("Start by reading your files") — measured at 100%
# scratchpad across 160 probes regardless of any other wording. Every behaviour worth
# studying (the duplicate posts of 2026-08-29, the fourteen-minute night) happens on LATER
# turns, after tool results come back. Two pre-registered studies, 460 probes between them,
# could not reach any of it: one arm produced zero posts in EVERY condition including the
# control, which measures the harness rather than the hypothesis.
#
# The results fed back are the ones the night actually observed, pulled from
# `agent_turns.tools` — not invented stubs — so a replay reproduces a real sitting and a
# counterfactual differs from it by exactly one edit. `matched_recorded` per step is the
# measure: where the model stops following the night it actually had is the effect.
#
# It also takes inline `raw_tools`, which widens it past its origin: a tool surface still
# being designed has no registry entry, and a persona that resolves before it writes never
# reaches its write tool in one turn — so the call worth measuring is invisible until the
# tool ships, which is backwards. Stubs feed the first move so the second can be observed.
#
# Reuses the llm.complete scope (it IS a converse). NO HANDLER EVER RUNS: the only tool
# output that reaches the model is a string the caller supplied.


class ReplayStub(BaseModel):
    name: str = Field(min_length=1)
    result: str = ""
    is_error: bool = False


class ReplayRequest(BaseModel):
    user_text: str = Field(min_length=1)
    system: str = ""
    task: str = "agent.turn"
    strength: str | None = None
    tools: list[str] = Field(default_factory=list)
    # Inline tool schemas, appended after the registry ones — the same knob /tool-probe
    # carries, and here for a reason that surface does not cover: a multi-turn loop is the
    # only way to measure a tool a model reaches for on its SECOND move, and a tool being
    # designed does not exist in the registry yet. Without this, a proposed tool surface
    # can be measured for the shape of its first call and nothing else.
    raw_tools: list[dict[str, Any]] = Field(default_factory=list)
    # The night's observed tool results, in the order the sitting produced them. Matched to
    # the model's calls by NAME (FIFO per name) so a replay that reorders its reads still
    # continues; `matched_recorded` records whether the order held.
    stubs: list[ReplayStub] = Field(default_factory=list)
    # What a call with no recorded result gets back. The replay continues rather than
    # stopping, because where it goes AFTER leaving the recorded path is the interesting part.
    fallback_result: str = "(no recorded result for this call)"
    max_steps: int = Field(default=8, ge=1, le=24)
    max_tokens: int = Field(default=2048, ge=1, le=32768)
    # The pooled-engine slot to run in. Omitted: the task's own role, except that a task
    # whose role is jerv's interactive slot runs in the workshop slot instead, so a console
    # probe never overwrites the persona prefix the owner's next chat turn reuses.
    slot_role: SlotRole | None = None


class ReplayStep(BaseModel):
    index: int
    name: str
    arguments: dict[str, Any]
    matched_recorded: bool
    result_used: str
    stop_reason: str


class ReplayOut(BaseModel):
    provider: str
    model: str
    tool_count: int
    steps: list[ReplayStep]
    # Names in call order — the thing to compare across conditions.
    call_sequence: list[str]
    final_text: str
    stop_reason: str
    steps_taken: int
    input_tokens: int
    output_tokens: int
    error: str | None = None


@router.post("/replay")
async def replay(body: ReplayRequest, request: Request, _p: DebugDep) -> ReplayOut:
    """Replay a sitting multi-turn against recorded tool results. See the module note."""
    attached = len(body.tools) + len(body.raw_tools)
    request.state.debug_detail = f"{attached} tools, {body.max_steps} steps"
    registry = cast(ToolRegistry, request.app.state.agent_registry)
    unknown = [t for t in body.tools if t not in registry.names()]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown tools: {unknown}")
    llm_tools = [registry.get(name).as_llm_tool() for name in body.tools]
    try:
        llm_tools += [
            LlmTool(
                name=rt["name"],
                description=rt.get("description", ""),
                input_schema=rt.get("input_schema", {"type": "object", "properties": {}}),
            )
            for rt in body.raw_tools
        ]
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"raw_tools need a 'name' (and optional description/input_schema): {exc}",
        ) from exc
    router_ = _llm_router(request)
    provider, model = await router_.effective_spec(body.task, body.strength)

    # FIFO pool per tool name, plus the recorded ORDER, so a step can be scored both ways.
    pool: dict[str, list[ReplayStub]] = {}
    for stub in body.stubs:
        pool.setdefault(stub.name, []).append(stub)
    recorded_order = [stub.name for stub in body.stubs]

    messages: list[LlmMessage] = [UserMessage(text=body.user_text)]
    steps: list[ReplayStep] = []
    in_tokens = out_tokens = 0
    stop_reason, final_text = "end_turn", ""

    for i in range(body.max_steps):
        try:
            turn = await router_.converse(
                body.task,
                system=body.system,
                messages=messages,
                tools=llm_tools,
                max_tokens=body.max_tokens,
                strength=body.strength,
                **_slot_pin(body.task, body.slot_role),
            )
        except LlmError as exc:
            return ReplayOut(
                provider=provider,
                model=model,
                tool_count=len(llm_tools),
                steps=steps,
                call_sequence=[s.name for s in steps],
                final_text=final_text,
                stop_reason="error",
                steps_taken=len(steps),
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                error=str(exc),
            )
        in_tokens += turn.usage.input_tokens
        out_tokens += turn.usage.output_tokens
        final_text, stop_reason = turn.text, turn.stop_reason
        if not turn.tool_calls:
            break

        results: list[ToolResult] = []
        for call in turn.tool_calls:
            queued = pool.get(call.name) or []
            stub = queued.pop(0) if queued else None
            content = stub.result if stub is not None else body.fallback_result
            # "On the recorded path" means this call is the one the night made at this
            # position — not merely that a result of that name was still available.
            on_path = i < len(recorded_order) and recorded_order[i] == call.name
            steps.append(
                ReplayStep(
                    index=i,
                    name=call.name,
                    arguments=call.arguments,
                    matched_recorded=on_path,
                    result_used=content[:400],
                    stop_reason=turn.stop_reason,
                )
            )
            results.append(
                ToolResult(
                    tool_call_id=call.id,
                    content=content,
                    is_error=bool(stub and stub.is_error),
                )
            )
        messages.append(AssistantMessage(text=turn.text, tool_calls=turn.tool_calls))
        messages.append(ToolResultMessage(results=results))

    return ReplayOut(
        provider=provider,
        model=model,
        tool_count=len(llm_tools),
        steps=steps,
        call_sequence=[s.name for s in steps],
        final_text=final_text,
        stop_reason=stop_reason,
        steps_taken=len(steps),
        input_tokens=in_tokens,
        output_tokens=out_tokens,
    )


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


# --- Grounding probe (AGENT_CANVAS_PLAN W0) ---------------------------------
# Measures WHICH coordinate base the served vision model actually emits, because
# nothing upstream documents it for this checkpoint: the Qwen3-VL cookbook divides
# by 1000, the Qwen3-VL docs site describes a 0-1 range, and the Qwen3.8 model card
# says nothing at all. Guessing is not safe — a wrong base yields a confident box
# around the wrong thing — so this route renders the SAME model reply under BOTH
# bases and lets the owner see which one lands on the object. Exposed as an API,
# never a script, because the owner runs this box with no terminal (CLAUDE.md #10).

_GROUNDING_SYSTEM = (
    "You locate things in images. Reply with ONLY a JSON array, no prose, no code "
    'fence. Each element: {"bbox_2d": [x1, y1, x2, y2], "label": "<what it is>"}. '
    "Corners, not width/height. If the thing is not present, reply []."
)


class GroundingProbeRequest(BaseModel):
    attachment_id: uuid.UUID
    # What to locate, in the owner's words — "the water heater", "each face".
    target: str = Field(min_length=1)
    system: str = ""
    # Off by default: the point of the probe is to see what the model natively emits
    # at the resolution the chat path actually sends (which does NOT downscale).
    downscale: bool = False
    max_tokens: int = Field(default=1024, ge=1, le=32768)


class GroundingBoxOut(BaseModel):
    label: str
    raw: list[float]
    # The same box resolved under each candidate base, in original-image pixels.
    # Whichever one frames the object is the model's real convention.
    as_norm_1000: list[int]
    as_norm_1: list[int]


class GroundingProbeOut(BaseModel):
    provider: str
    model: str
    filename: str
    # EXIF-corrected, i.e. the axes the model actually saw. See chat_images.
    width: int
    height: int
    inferred: str
    pinned: str | None
    box_count: int
    boxes: list[GroundingBoxOut]
    text: str


@router.post("/grounding")
async def grounding_probe(
    body: GroundingProbeRequest, request: Request, _p: DebugDep
) -> GroundingProbeOut:
    """Ask the served vision model to locate `target` and report the boxes under both
    candidate coordinate bases. The base whose pixels frame the object is the one to
    pin in `agent/grounding.py`. See the module note above."""
    request.state.debug_detail = f"{body.target} {body.attachment_id}"
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        # Either table: a note attachment (`app.attachments`, what /vision reads) OR a
        # CHAT upload (`app.turn_attachments`). The canvas annotates chat uploads, so a
        # probe that only saw note attachments answered "attachment not found" for
        # exactly the images this feature exists to mark up.
        found = (
            await session.execute(select(Attachment).where(Attachment.id == body.attachment_id))
        ).scalar_one_or_none()
        if found is None:
            found = (
                await session.execute(
                    select(TurnAttachment).where(TurnAttachment.id == body.attachment_id)
                )
            ).scalar_one_or_none()
        if found is None:
            raise HTTPException(
                status_code=404,
                detail="no attachment with that id in app.attachments or app.turn_attachments",
            )
    att = found
    raw = await _blobs(request).get(att.sha256)
    if body.downscale:
        data, media_type = downscale_for_vision(raw, att.media_type)
    else:
        data, media_type = raw, att.media_type
    try:
        width, height = image_dimensions(data)
    except (UndecodableImage, ImageTooLarge) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    router_ = _llm_router(request)
    try:
        provider, model = await router_.effective_spec("agent.vision", "vision")
        result = await router_.complete(
            "agent.vision",
            system=body.system or _GROUNDING_SYSTEM,
            user_text=f"Locate {body.target}. Reply with the JSON array only.",
            images=[LlmImage(media_type=media_type, data=base64.b64encode(data).decode("ascii"))],
            max_tokens=body.max_tokens,
            strength="vision",
        )
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    boxes, _points = parse_grounding(result.text)
    flat = [v for b in boxes for v in (b.x1, b.y1, b.x2, b.y2)]
    inferred = infer_convention(flat) if flat else None
    try:
        pinned: str | None = str(convention_for(model))
    except UnknownGroundingModel:
        pinned = None

    def _px(convention: Convention) -> list[list[int]]:
        resolved = to_pixels(
            boxes, served_model=model, width=width, height=height, convention=convention
        )
        return [[b.x1, b.y1, b.x2, b.y2] for b in resolved]

    thousandths, unit = _px(Convention.NORM_1000), _px(Convention.NORM_1)
    log.info(
        "debug.grounding",
        provider=provider,
        model=model,
        boxes=len(boxes),
        inferred=str(inferred) if inferred else None,
    )
    return GroundingProbeOut(
        provider=provider,
        model=model,
        filename=att.filename,
        width=width,
        height=height,
        inferred=str(inferred) if inferred else "none",
        pinned=pinned,
        box_count=len(boxes),
        boxes=[
            GroundingBoxOut(
                label=b.label,
                raw=[b.x1, b.y1, b.x2, b.y2],
                as_norm_1000=thousandths[i],
                as_norm_1=unit[i],
            )
            for i, b in enumerate(boxes)
        ],
        text=result.text,
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


# --- Native video probe (NATIVE_VIDEO_PLAN V0) -------------------------------
# The measuring instrument for native video on Flash-Next: one clip already on the box, sent
# either natively (transcoded, one `input_video` part) or through today's frame pipeline, so
# tokens, latency and the two answers can be compared with a token and no terminal. The clip
# is capped at the native path's one minute whatever its length; the threshold that picks a
# path in V1 is not applied here, because measuring is the point.

NATIVE_VIDEO_MAX_SECONDS = 60.0
_VIDEO_DEFAULT_QUESTION = "Describe what happens in this video, in order, with timestamps."


class VideoProbeRequest(BaseModel):
    attachment_id: uuid.UUID
    mode: Literal["native", "frames"] = "native"
    # Native only: the frame pipeline runs its shipped prompts unchanged.
    question: str = ""
    system: str = ""
    max_tokens: int = Field(default=2048, ge=1, le=32768)
    # Native only: a `provider:model` spec for this one call (e.g. `local:qwen3.8-flash-next`),
    # so the probe can reach Flash-Next without re-routing `video.summarize` for everyone.
    spec: str | None = None


class VideoProbeOut(BaseModel):
    mode: str
    # The model that wrote `text`: the native call's, or in frames mode the summary step's.
    provider: str
    model: str
    text: str
    duration_s: float | None
    # What reached the model: the clipped length and the transcoded body (native only; the
    # frame pipeline makes many calls and sends stills).
    sent_seconds: float | None = None
    payload_bytes: int | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    transcode_ms: int | None = None
    elapsed_ms: int
    # Frames mode only: the vision route that captioned each still before the summary.
    caption_provider: str | None = None
    caption_model: str | None = None


async def _run_video(
    router_: LlmRouter, blobs: BlobStore, att: Attachment | TurnAttachment, body: VideoProbeRequest
) -> VideoProbeOut:
    """Run one clip through the chosen path. Pure of the DB so it unit-tests with fakes; the
    route owns the lookup."""
    if not is_video_media_type(att.media_type):
        raise HTTPException(status_code=400, detail=f"not a video: {att.media_type}")
    if not media.ffmpeg_available():
        raise HTTPException(status_code=400, detail="ffmpeg/ffprobe are not on the api's PATH")
    if body.mode == "frames" and body.spec:
        # The frame pipeline routes its own tasks; a spec silently ignored would mislabel the
        # comparison it exists to make.
        raise HTTPException(status_code=400, detail="spec applies to native mode only")
    raw = await blobs.get(att.sha256)
    if body.mode == "frames":
        return await _run_video_frames(router_, blobs, att, raw)
    with tempfile.TemporaryDirectory(prefix="jbrain-dbgvid-") as tmp:
        src = Path(tmp) / "in"
        src.write_bytes(raw)
        duration = await media.probe_duration_s(src)
        started = time.perf_counter()
        try:
            clip = await media.transcode_for_native_video(
                src, max_seconds=NATIVE_VIDEO_MAX_SECONDS, fps=slot_roles.VIDEO_FPS
            )
        except media.TranscodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        transcode_ms = round((time.perf_counter() - started) * 1000)
    sent = min(duration, NATIVE_VIDEO_MAX_SECONDS) if duration is not None else None
    part = LlmVideo(
        media_type=media.NATIVE_VIDEO_MEDIA_TYPE,
        data=base64.b64encode(clip).decode("ascii"),
        seconds=sent,
    )
    started = time.perf_counter()
    try:
        provider, model = await router_.effective_spec(VIDEO_SUMMARY_TASK, spec_override=body.spec)
        result = await router_.complete(
            VIDEO_SUMMARY_TASK,
            spec_override=body.spec,
            system=body.system,
            user_text=body.question or _VIDEO_DEFAULT_QUESTION,
            videos=[part],
            max_tokens=body.max_tokens,
        )
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    log.info(
        "debug.video",
        mode="native",
        provider=provider,
        model=model,
        attachment=str(att.id),
        payload_bytes=len(clip),
        prompt_tokens=result.usage.input_tokens,
        elapsed_ms=elapsed_ms,
    )
    return VideoProbeOut(
        mode="native",
        provider=provider,
        model=model,
        text=result.text,
        duration_s=duration,
        sent_seconds=sent,
        payload_bytes=len(clip),
        prompt_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        transcode_ms=transcode_ms,
        elapsed_ms=elapsed_ms,
    )


async def _run_video_frames(
    router_: LlmRouter, blobs: BlobStore, att: Attachment | TurnAttachment, raw: bytes
) -> VideoProbeOut:
    # Frames only, no whisper: the native path cannot hear either, so the comparison is
    # vision against vision. Frame thumbnails land in the blob store, content-addressed, as
    # they do for analyze_video.
    started = time.perf_counter()
    try:
        provider, model = await router_.effective_spec(VIDEO_SUMMARY_TASK)
        caption_provider, caption_model = await router_.effective_spec(VIDEO_FRAME_TASK)
        result = await run_video_analysis(
            raw,
            filename=att.filename,
            media_type=att.media_type,
            router=router_,
            blobs=blobs,
            sampler=media.sample_frames,
        )
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=400, detail="no frames could be read from that clip")
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    duration_ms = result.analysis.get("duration_ms")
    log.info("debug.video", mode="frames", provider=provider, model=model, elapsed_ms=elapsed_ms)
    return VideoProbeOut(
        mode="frames",
        provider=provider,
        model=model,
        text=result.summary,
        duration_s=duration_ms / 1000 if isinstance(duration_ms, int | float) else None,
        elapsed_ms=elapsed_ms,
        caption_provider=caption_provider,
        caption_model=caption_model,
    )


async def _video_attachment(
    request: Request, attachment_id: uuid.UUID
) -> Attachment | TurnAttachment:
    # Either table, as /grounding reads: a chat-attached clip is a turn attachment, and those
    # are the clips V2 sends natively.
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        found: Attachment | TurnAttachment | None = (
            await session.execute(select(Attachment).where(Attachment.id == attachment_id))
        ).scalar_one_or_none()
        if found is None:
            found = (
                await session.execute(
                    select(TurnAttachment).where(TurnAttachment.id == attachment_id)
                )
            ).scalar_one_or_none()
    if found is None:
        raise HTTPException(
            status_code=404,
            detail="no attachment with that id in app.attachments or app.turn_attachments",
        )
    return found


@router.post("/video")
async def video(body: VideoProbeRequest, request: Request, _p: DebugDep) -> VideoProbeOut:
    """Run one on-box clip natively or through the frame pipeline and report what it cost."""
    request.state.debug_detail = f"{body.mode} {body.attachment_id}"
    att = await _video_attachment(request, body.attachment_id)
    return await _run_video(_llm_router(request), _blobs(request), att, body)


# --- Async completion jobs (for slow models behind a short proxy timeout) ----
# A long local extraction can take minutes — longer than a Cloudflare Tunnel (or
# any proxy) will hold a request open. So the caller SUBMITS a job (returns at
# once) and POLLS /jobs/{id}; the model call runs in a background task on the box,
# never held open across the wire. The store is in-memory and best-effort — a
# process restart drops in-flight jobs, which is fine for a debug aid.


class SweepBinOut(BaseModel):
    hz: int
    mhz: float
    floor_db: float
    peak_db: float
    occupancy: float


class SweepGapOut(BaseModel):
    start_mhz: float
    stop_mhz: float
    khz: float


class SdrSweepOut(BaseModel):
    start_hz: int
    stop_hz: int
    bin_hz: int
    seconds: float
    rows: int
    bins: int
    floor_db: float
    revisit_s: float
    """Seconds between readings of one bin, measured off rtl_power's own timestamps —
    the scale `occupancy` is a fraction of. A 10 s transmission is six intervals at 1 s
    and rounding error at 60 s, and how often a bin is revisited depends on how many
    retune hops the span needs, which is not otherwise visible from the response."""
    complete: bool
    busy: list[SweepBinOut]
    steady: list[SweepBinOut]
    """Bins that never went quiet. A spur and a carrier held for the whole window are
    the same measurement, so these are reported rather than dropped — naming which is
    which needs a second look at the channel, not more arithmetic on this sweep."""
    uncovered: list[SweepGapOut]
    """Spans the sweep did not measure. rtl_power retunes in blocks and crops their
    edges, so a wide sweep has seams: this box's 144-148 run left a 342 kHz hole across
    live repeater channels. Without this the reader cannot tell quiet from unlooked-at."""
    png_base64: str
    csv_chars: int
    csv: str | None = None
    gain_db: float | None = None
    """The tuner gain these rows were MEASURED at, or None when the tuner was on its own
    loop. A survey's numbers are dBFS, and dBFS is comparable only against the same gain
    and the same bin width — the width was reported and the gain was not, so two runs
    taken at 10 and at 30 dB came back looking like the same instrument. The api cannot
    derive it: absent in the request means the sidecar's per-purpose default, and since
    the gain became a per-radio setting it can also mean whatever that radio stores."""
    tuner_bypassed: bool = False
    """True when the sweep ran below 24 MHz with no converter, where the tuner is
    powered down and there is no gain stage at all. A third answer, not a missing one:
    the levels are true dBFS with no gain to quote, rather than a moving reference."""
    upconverter_hz: int = 0
    """The converter offset the radio was tuned through, in Hz. Every frequency in this
    response — `start_hz`, `stop_hz`, every bin — is the owner's, never the tune."""
    """The raw rtl_power CSV, when asked for. Off by default because it is megabytes and
    dwarfs everything else here — but a calibration instrument that will not hand back
    its measurements is not one, and inferring a floor from PNG pixel brightness (which
    is what the absence of this forced) is not calibration."""


_MAX_JOBS = 256


class JobSubmitOut(BaseModel):
    job_id: str


class JobStatusOut(BaseModel):
    job_id: str
    status: str  # "pending" | "done" | "error"
    result: CompleteOut | SdrSweepOut | VideoProbeOut | None = None
    error: str | None = None


@router.post("/complete-async", status_code=202)
async def complete_async(body: CompleteRequest, request: Request, _p: DebugDep) -> JobSubmitOut:
    """Submit a completion as a background job; poll GET /jobs/{job_id} for the
    result. Lets the console/harness drive minutes-long calls through a proxy whose
    request timeout is far shorter than the model takes."""
    request.state.debug_detail = body.user_text
    router_ = _llm_router(request)
    return _submit_job(request, lambda: _run_completion(router_, body))


def _submit_job(
    request: Request, work: Callable[[], Awaitable[CompleteOut | VideoProbeOut]]
) -> JobSubmitOut:
    jobs = request.app.state.debug_jobs
    tasks = request.app.state.debug_job_tasks
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "pending", "result": None, "error": None}
    # Keep the map bounded: drop the oldest already-finished jobs.
    if len(jobs) > _MAX_JOBS:
        for jid, val in list(jobs.items())[:-_MAX_JOBS]:
            if val["status"] != "pending":
                jobs.pop(jid, None)

    async def _run() -> None:
        try:
            out = await work()
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


@router.post("/video-async", status_code=202)
async def video_async(body: VideoProbeRequest, request: Request, _p: DebugDep) -> JobSubmitOut:
    """/video as a background job; poll GET /jobs/{job_id}. A minute of video is a long
    prefill, and the frame pipeline makes two dozen calls — either outlasts a proxy timeout."""
    request.state.debug_detail = f"{body.mode} {body.attachment_id}"
    att = await _video_attachment(request, body.attachment_id)
    router_, blobs = _llm_router(request), _blobs(request)
    return _submit_job(request, lambda: _run_video(router_, blobs, att, body))


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


# G17 (docs/plans/JMOLT_HARDENING_PLAN.md). The console is read-only, which is not the same
# as confidential: `app.settings` holds the Moltbook bearer key and the Gmail client secret
# as plaintext jsonb, and `SELECT * FROM app.settings` returned them. The debug token is
# handed to a helper to look at a live box, and it should not also be a credential dump.
#
# Two shapes, because the secrets live in two shapes. A dedicated COLUMN named for a secret
# is redacted by name. And `app.settings` is one row per key, so a row whose key names a
# secret has its `value` redacted — the column there is called `value` and carries everything.
_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:api_key|secret|password|passwd|credential|bearer)s?(?:_|$)"
    r"|_key$|_token$|^token$|^key$",
    re.I,
)
_VALUE_COLUMNS = frozenset({"value", "val", "setting_value"})
_KEY_COLUMNS = frozenset({"key", "name", "setting", "setting_key"})
_REDACTED = "[redacted — a secret, not shown in the debug console]"


def _redact_row(columns: list[str], row: list[Any]) -> list[Any]:
    """Blank the secret-bearing cells of one result row."""
    lowered = [c.lower() for c in columns]
    named_key = next((i for i, c in enumerate(lowered) if c in _KEY_COLUMNS), None)
    row_names_a_secret = (
        named_key is not None
        and isinstance(row[named_key], str)
        and bool(_SECRET_NAME_RE.search(row[named_key]))
    )
    out: list[Any] = []
    for i, (name, value) in enumerate(zip(lowered, row, strict=True)):
        secret_by_column = bool(_SECRET_NAME_RE.search(name)) and i != named_key
        secret_by_row_key = row_names_a_secret and name in _VALUE_COLUMNS
        if secret_by_column or secret_by_row_key:
            out.append(_REDACTED)
        elif i == named_key:
            # The key column names the setting; it is what the console is FOR. It is also
            # full of strings starting "moltbook_", which the value scrubber would eat —
            # a console that renders every setting as `moltbook_[redacted]` reads as broken.
            out.append(value)
        else:
            # Belt and braces: a secret that reached a column nothing above names — an error
            # string, a jsonb blob, a joined view — still gets its recognisable shapes taken
            # out by the same scrubber the agent-facing paths use.
            out.append(scrub_secret(value) if isinstance(value, str) else value)
    return out


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
    rows = [_redact_row(columns, [_jsonable(v) for v in row]) for row in fetched[: body.max_rows]]
    log.info("debug.sql", row_count=len(rows), truncated=truncated)
    return SqlOut(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)


# --- Web fetch (exercise the live direct→reader→solver escalation) -----------


class FetchRequest(BaseModel):
    url: str
    offset: int = 0
    find: str = ""
    # Force a single recovery tier instead of the full escalation. "" = the normal
    # direct→reader→solver→tavily path; "tavily" = ONLY the hosted Tavily Extract tier
    # (the Settings "Test key" button uses this to verify a freshly pasted key against a
    # real walled URL with no terminal). The byparr solver has its own /solve route.
    tier: str = ""


class FetchOut(BaseModel):
    url: str  # the FINAL url (after redirects / the tier that served it)
    title: str
    text: str  # one window of the extracted text (capped like the agent sees it)
    total_chars: int
    links: int
    truncated: bool
    # Which leg of the ladder produced this — "direct" means nothing had to be recovered,
    # "reader"/"solver"/"tavily" name the tier that saved it. The whole point of the route is
    # to see the escalation work, and the page alone never shows it: before this the answer
    # lived only in `logs api`, which is a poor read on a phone.
    tier: str
    # True when NO tier could paint a JavaScript app — the page is real but was never
    # rendered, so an empty/tiny `text` here is an unread page, not an empty one.
    js_shell: bool


async def _run_tavily_tier(fetcher: WebFetcher, body: FetchRequest) -> Any:
    """Run a URL through ONLY the hosted Tavily Extract tier (the Settings "Test key" probe),
    raising a 400 that distinguishes the failure modes so the console reads clearly: the tier
    unwired (no base URL / provider), versus Tavily disabled / keyless / a genuine miss (a
    challenge-or-empty page). A bad scheme / private host raises via the SSRF guard."""
    if not fetcher.tavily_wired:
        raise HTTPException(
            status_code=400,
            detail="the Tavily tier is not configured (JBRAIN_TAVILY_URL is empty)",
        )
    try:
        result = await fetcher.tavily(body.url, offset=max(0, body.offset), find=body.find)
    except WebFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(
            status_code=400,
            detail="Tavily returned no usable page — it is disabled or keyless (check the "
            "Settings toggle/key), or it hit a challenge/empty page. See `logs api` for detail.",
        )
    return result


@router.post("/fetch")
async def fetch_url(body: FetchRequest, request: Request, _p: DebugDep) -> FetchOut:
    """Run a URL through jerv's WebFetcher — the SAME direct→reader→solver escalation the
    agent uses — and return the extracted page, or a 400 carrying the recoverable fetch
    error. The one debug route that drives the live web-fetch path end to end, so the
    bot-challenge detection and the solver fallback can be verified against a real walled URL
    after a deploy — `tier` names the leg that served it and `js_shell` marks a JavaScript app
    no tier could render, so the common checks need no log correlation at all."""
    request.state.debug_detail = f"{body.tier} {body.url}".strip() if body.tier else body.url
    fetcher = cast(WebFetcher, request.app.state.web_fetcher)
    if body.tier == "tavily":
        result = await _run_tavily_tier(fetcher, body)
    elif body.tier:
        raise HTTPException(
            status_code=400, detail=f"unknown tier '{body.tier}' (use '' or 'tavily')"
        )
    else:
        try:
            result = await fetcher.fetch(body.url, offset=max(0, body.offset), find=body.find)
        except WebFetchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info("debug.fetch", url=result.url, chars=result.total_chars, tier=body.tier or "auto")
    return FetchOut(
        url=result.url,
        title=result.title,
        text=result.text,
        total_chars=result.total_chars,
        links=len(result.links),
        truncated=result.truncated,
        tier=result.tier,
        js_shell=result.js_shell,
    )


@router.post("/solve")
async def solve_url(body: FetchRequest, request: Request, _p: DebugDep) -> FetchOut:
    """Run a URL through ONLY the challenge-solver tier (byparr), skipping the direct+reader
    legs — so the stealth browser can be exercised in isolation against a walled URL, without a
    doomed direct fetch first. A 400 distinguishes the failure modes so a probe reads clearly:
    the solver being unconfigured, versus byparr running but still getting a challenge / empty
    page (a genuine solve miss — pair with `logs byparr` for the browser-side detail). Shares
    the `web.fetch` scope (it is a narrower web fetch)."""
    request.state.debug_detail = f"solve {body.url}"
    fetcher = cast(WebFetcher, request.app.state.web_fetcher)
    if not fetcher.solver_enabled:
        raise HTTPException(
            status_code=400,
            detail="the challenge solver is not configured (JBRAIN_SOLVER_URL is empty)",
        )
    try:
        result = await fetcher.solve(body.url, offset=max(0, body.offset), find=body.find)
    except WebFetchError as exc:
        # A bad scheme/private host — the SSRF guard refusing the target, not a solve miss.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(
            status_code=400,
            detail="the solver returned no usable page — byparr is down, still challenged, "
            "or the page was empty. Check `logs byparr` for the browser-side detail.",
        )
    log.info("debug.solve", url=result.url, chars=result.total_chars)
    return FetchOut(
        url=result.url,
        title=result.title,
        text=result.text,
        total_chars=result.total_chars,
        links=len(result.links),
        truncated=result.truncated,
        tier=result.tier,
        js_shell=result.js_shell,
    )


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


async def _active_engine(request: Request) -> llm_engine.Engine:
    """The engine actually up — the EFFECTIVE one, which differs from the owner's selection
    after a Flash-Next fallback (FLASH_NEXT_ENGINE_PLAN §4d). A settings read that fails
    reads as the default rather than failing a log pull: the logs are most wanted exactly
    when something else on the box is broken."""
    try:
        return await _store(request).llm_local_engine_effective(_OWNER_CTX)
    except Exception:  # noqa: BLE001
        log.warning("debug.engine_unreadable", exc_info=True)
        return llm_engine.DEFAULT_ENGINE


def _jcode_log_services(engine: llm_engine.Engine) -> tuple[str, ...]:
    """The code-mode services, in the order most useful for debugging a turn: the control
    server, then the model gateway — the ACTIVE engine's container, since only one of
    `local-llm` / `flash-next` is ever up and the other's log is a stale run."""
    return ("jcode", llm_engine.SERVICE[engine])


@router.get("/jcode/logs", response_class=PlainTextResponse)
async def jcode_logs(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    tail: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> PlainTextResponse:
    """All code-mode logs in one pull — the control server and the model gateway, each
    tailed and labeled. A not-running service is noted, not fatal, so this works
    mid-bring-up. Saves round-trips when chasing a jcode turn failure."""
    request.state.debug_detail = f"jcode-system (tail {tail})"
    client = _supervisor(request)
    headers = {"Authorization": f"Bearer {settings.supervisor_token}"}
    sections: list[str] = []
    for service in _jcode_log_services(await _active_engine(request)):
        resp = await client.get(f"/logs/{service}", params={"tail": tail}, headers=headers)
        if resp.status_code == 404:
            body = "(service not running)"
        else:
            resp.raise_for_status()
            body = resp.text
        sections.append(f"===== {service} =====\n{body}")
    return PlainTextResponse("\n\n".join(sections))


# Which engine answered a gateway/upstream log read. Both engines carry the `local-llm`
# network alias and only one is ever up, so the gateway client reaches whichever is running
# with no per-engine URL — this header is how a reader knows which one that was.
_ENGINE_HEADER = "X-JBrain-Engine"


def _engine_hint(engine: llm_engine.Engine) -> str:
    service = llm_engine.SERVICE[engine]
    return (
        f" (active engine: {engine}; its container log is /debug/logs/{service}, and "
        "/debug/llm/engine says whether it is running)"
    )


@router.get("/llm/gateway-logs", response_class=PlainTextResponse)
async def gateway_logs(
    request: Request,
    _p: DebugDep,
    tail: Annotated[int, Query(ge=1, le=20000)] = 200,
) -> PlainTextResponse:
    """Tail llama-swap's buffered log — swap decisions, health checks, and the
    slot-acquired / slot-RELEASED account of a turn that answers whether a Stop actually
    halts decoding.

    What this does NOT contain, despite an earlier version of this docstring claiming it:
    llama-server's own output. llama-swap's only buffered route is `/logs`, and it carries
    the proxy's lines alone. For llama.cpp's own output — the per-buffer memory breakdown,
    the device report — use /debug/llm/upstream-logs, which reads the replay burst off
    `/logs/stream/*`. (That earlier docstring also claimed those streams carry no history;
    they do, which is what makes the sibling route possible.)

    `tail` reaches 20000 because a busy box turns over the buffer quickly and the old 2000
    cap could drop the window an operator was looking for. Sits beside /logs/{service}
    (the container's stdout via the supervisor). 502 if the gateway can't be reached.

    Engine-aware (FLASH_NEXT_ENGINE_PLAN §4d): both engines answer at the `local-llm` alias
    and only one is ever up, so this reads whichever is running. The `X-JBrain-Engine`
    header names the selected engine, and a 502 names that engine's container log."""
    engine = await _active_engine(request)
    request.state.debug_detail = f"gateway {engine} (tail {tail})"
    try:
        full = await _gateway(request).tail_logs()
    except LocalGatewayError as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway logs unavailable: {exc}{_engine_hint(engine)}"
        ) from exc
    return PlainTextResponse("\n".join(full.splitlines()[-tail:]), headers={_ENGINE_HEADER: engine})


@router.get("/llm/upstream-logs", response_class=PlainTextResponse)
async def upstream_logs(
    request: Request,
    _p: DebugDep,
    stream: Annotated[str, Query(pattern=r"^[A-Za-z0-9._-]+$")] = "upstream",
    tail: Annotated[int, Query(ge=1, le=20000)] = 400,
) -> PlainTextResponse:
    """llama-server's own stdout, which /llm/gateway-logs cannot show: the slot lifecycle,
    per-request prompt-eval throughput, context-checkpoint evictions, and the engine's
    account of why a load failed.

    It reads llama-swap's `/logs/stream/{stream}`, whose opening burst replays the buffered
    history before the stream goes live; the reader takes the burst and hangs up.

    MEASURED, and the reason this docstring no longer promises a memory breakdown: on the
    box's build the model LOADER prints nothing. A load shows as a ~1.4 s gap between
    `load_model: loading model` and `init: llama threadpool init` with no `llama_model_loader`,
    no `load_tensors`, and no `model buffer size` — not here, and not in the `local-llm`
    container log either. That output is simply not emitted at the default verbosity 3 (we
    pass no `-lv`), so the per-buffer split has no known reachable source on this build and
    should not be claimed to have one. The load's memory is measured by the device delta
    instead (`local_gateway._record_measured_footprint`), which needs no log at all.

    `stream` defaults to `upstream` (every model's output interleaved) and also accepts a
    served model id to isolate one model's load. Engine-aware like `gateway-logs`: it reads
    whichever engine is up, and the `X-JBrain-Engine` header names it. An empty body means the
    engine has printed nothing since llama-swap started — usually a box with no load since
    boot, not a fault.
    502 if the gateway can't be reached."""
    engine = await _active_engine(request)
    request.state.debug_detail = f"upstream {engine} {stream} (tail {tail})"
    try:
        full = await _gateway(request).tail_upstream_logs(stream)
    except LocalGatewayError as exc:
        raise HTTPException(
            status_code=502, detail=f"upstream logs unavailable: {exc}{_engine_hint(engine)}"
        ) from exc
    return PlainTextResponse("\n".join(full.splitlines()[-tail:]), headers={_ENGINE_HEADER: engine})


@router.post("/llm/drop-page-cache")
async def drop_page_cache(
    request: Request,
    _p: DebugDep,
    models: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    """Reclaim the page-cache copy of on-box model weights. `models` is a comma-separated
    list of catalog ids; omit it to sweep every model.

    The box serves with `--no-mmap`, so a load leaves the weights resident TWICE — once in
    GTT, once in the page cache the read filled — and unloading frees only the GTT copy.
    `host_metrics.read_memory_gb` counts page cache as used, so that residue shrinks the
    admission budget for every later load.

    MEASURED, and why this route exists: 29.19 GiB of stale gpt-oss-120b cache left host
    pages free at 86.2 GB, and qwen3-coder-next-q8 (needs ~95.5 GB) was refused for want of
    15.3 GB that nothing was actually using. Before this, the only way to reclaim it was the
    global `drop_caches` in deploy/update-inner.sh — host shell, which the owner running this
    box remotely does not have (CLAUDE.md #10).

    Safe while models are resident: `POSIX_FADV_DONTNEED` drops clean cache only, never the
    GTT copy llama-server serves from, and weights are read-only. `freed_gb` is MEASURED via
    `cachestat(2)`; a null per-model value means the kernel could not measure the drop (the
    syscall is unavailable — it is blocked by the container's seccomp profile on this box),
    not that nothing was freed.

    A RESIDENT file-backed model gets a range-aware drop (`local_weights
    .drop_weights_page_cache_except_mapped`, FLASH_NEXT_ENGINE_PLAN §3): Flash-Next serves its
    engram (PLE) table memory-mapped from disk, so that tensor's pages are the working set and
    stay, while the rest of its shards' cache — the residue of uploading the GPU weights — is
    dropped. It therefore reports less freed than its size, by design."""
    request.state.debug_detail = f"drop page cache ({models or 'all'})"
    ids = [m.strip() for m in models.split(",") if m.strip()] if models else None
    # In a thread: a resident Flash-Next's range-aware drop parses GGUF headers first.
    freed = await asyncio.to_thread(_gateway(request).drop_page_cache, ids)
    measured = [v for v in freed.values() if v is not None]
    return {
        "models": freed,
        "freed_gb": round(sum(measured), 2) if measured else None,
        "measured": bool(measured),
    }


@router.get("/client-vitals")
async def client_vitals(request: Request, _p: DebugDep) -> dict[str, object]:
    """The browser's own account of the top-bar vitals stream, as last reported.

    The one read that can tell a stream the box never sent from a stream the browser never
    received. `sinceLastFrameMs` is the number that matters: the route emits one frame a
    second, so anything above a few thousand means the meter is blind however healthy the
    socket claims to be. `{"reported": false}` means no client has opened the vitals detail
    since this process started — not that the meter is broken."""
    report = getattr(request.app.state, "client_vitals", None)
    if report is None:
        return {"reported": False}
    return {"reported": True, **report}


@router.get("/host/metrics")
async def host_metrics(request: Request, settings: SettingsDep, _p: DebugDep) -> dict[str, object]:
    """The host's live hardware telemetry, proxied from the supervisor (the only container
    that reads /sys): GPU busy %, APU package power, load average, memory/swap/disk, fan
    RPM, and per-container memory. The console's one physical read — pair it with a turn to
    watch the GPU gauge climb as the model decodes and, the question this answers, whether
    it FALLS when the turn is Stopped (a clean device release) or stays pegged (the gateway
    kept generating past the client disconnect). Mirrors the owner ops surface."""
    request.state.debug_detail = "host metrics"
    resp = await _supervisor(request).get(
        "/metrics", headers={"Authorization": f"Bearer {settings.supervisor_token}"}
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


# Kernel drivers that claim an RTL2832U dongle for DVB-T reception. Any of them
# bound to the device means userspace (librtlsdr) cannot open it — the blacklist
# step in the SDR plan's S0 exists precisely to keep them off it.
_DVB_DRIVERS: frozenset[str] = frozenset({"dvb_usb_rtl28xxu", "rtl2832", "rtl2830", "dvb_usb_v2"})


class SdrDeviceOut(BaseModel):
    name: str
    usb_id: str
    manufacturer: str | None
    product: str | None
    serial: str | None
    device_node: str | None
    drivers: list[str]
    claimed_by_dvb: bool


class SdrProbeOut(BaseModel):
    found: bool
    ready: bool
    summary: str
    next_step: str
    sysfs_readable: bool
    usb_device_count: int
    sdrs: list[SdrDeviceOut]
    # EVERY device on the bus, not just the SDR-shaped ones. Carried always because
    # the not-found verdict asks the reader to identify the dongle by its USB id,
    # and without the list that instruction is unactionable — the console has no
    # other way to see the bus, and the owner has no terminal (CLAUDE.md rule 10).
    devices: list[SdrDeviceOut]


async def _usb_scan(request: Request, settings: Any) -> dict[str, Any]:
    """The supervisor's raw USB scan. Raises `httpx.HTTPError` — the two callers want
    opposite things from a failure, so neither gets it swallowed here."""
    resp = await _supervisor(request).get(
        "/usb", headers={"Authorization": f"Bearer {settings.supervisor_token}"}
    )
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json())


def _sdr_verdict(payload: dict[str, Any]) -> SdrProbeOut:
    """Turn the supervisor's raw USB scan into an answer to one question: can this
    box drive an SDR yet, and if not, what is in the way?

    Kept separate from the route so it is testable without a supervisor."""
    readable = bool(payload.get("sysfs_readable"))
    devices = cast(list[dict[str, Any]], payload.get("devices") or [])
    raw_sdrs = cast(list[dict[str, Any]], payload.get("sdrs") or [])

    def _row(d: dict[str, Any]) -> SdrDeviceOut:
        return SdrDeviceOut(
            name=d["name"],
            usb_id=d["usb_id"],
            manufacturer=d.get("manufacturer"),
            product=d.get("product"),
            serial=d.get("serial"),
            device_node=d.get("device_node"),
            drivers=list(d.get("drivers") or []),
            claimed_by_dvb=bool(set(d.get("drivers") or []) & _DVB_DRIVERS),
        )

    sdrs = [_row(d) for d in raw_sdrs]
    every = [_row(d) for d in devices]

    if not readable:
        return SdrProbeOut(
            found=False,
            ready=False,
            summary="Cannot tell \u2014 the supervisor cannot read /sys/bus/usb.",
            next_step="Check that the supervisor container is up; this read needs no "
            "device passthrough, only a readable /sys.",
            sysfs_readable=False,
            usb_device_count=len(devices),
            sdrs=[],
            devices=every,
        )

    if not sdrs:
        return SdrProbeOut(
            found=False,
            ready=False,
            summary=f"No RTL-SDR found. The scan itself worked \u2014 {len(devices)} USB "
            f"device(s) enumerated.",
            next_step="Plug the dongle in (or re-seat it) and probe again. If it IS "
            "plugged in, it is one of the rows in `devices` below — report that row's "
            "usb_id so it can be added to the known-SDR table.",
            sysfs_readable=True,
            usb_device_count=len(devices),
            sdrs=[],
            devices=every,
        )

    claimed = [d for d in sdrs if d.claimed_by_dvb]
    named = sdrs[0].product or sdrs[0].usb_id
    # Every message below described sdrs[0] as if it were the whole picture, which read
    # as one radio the day a second was plugged in — the summary line quietly wrong on a
    # box whose owner has no terminal to check it against (CLAUDE.md #10). The LIST was
    # always right; only the prose was singular.
    more = " (and 1 other)" if len(sdrs) == 2 else f" (and {len(sdrs) - 1} others)"
    also = more if len(sdrs) > 1 else ""
    if claimed:
        holder = ", ".join(
            sorted({drv for d in claimed for drv in d.drivers if drv in _DVB_DRIVERS})
        )
        return SdrProbeOut(
            found=True,
            ready=False,
            summary=f"Found {named} ({sdrs[0].usb_id}){also}, but the kernel DVB "
            f"driver {holder} has claimed it.",
            next_step=f"Blacklist {holder} on the host and re-plug (or reboot). Until "
            "then librtlsdr cannot open the device.",
            sysfs_readable=True,
            usb_device_count=len(devices),
            sdrs=sdrs,
            devices=every,
        )

    other = sorted({drv for d in sdrs for drv in d.drivers})
    if other:
        return SdrProbeOut(
            found=True,
            ready=False,
            summary=f"Found {named} ({sdrs[0].usb_id}){also}, claimed by {', '.join(other)}.",
            next_step="Identify what bound that driver before passing the device through.",
            sysfs_readable=True,
            usb_device_count=len(devices),
            sdrs=sdrs,
            devices=every,
        )

    # Lead with the DIRECTORY, not the device node: devnum increments on every
    # re-plug, so a compose file pinning /dev/bus/usb/001/005 breaks the first time
    # the dongle is moved. Selecting by serial is what makes it stable — and what
    # lets a second dongle join later without ambiguity.
    node = sdrs[0].device_node or "an unknown node"
    serials = [d.serial for d in sdrs if d.serial]
    by_serial = f", selected by serial {' or '.join(serials)}" if serials else ""
    # With more than one attached, naming the serials is not decoration: `rtl_fm` and
    # `rtl_power` are invoked with no `-d`, so they take whichever librtlsdr enumerates
    # FIRST, and nothing here can say which that is. Two radios and a silent choice
    # between them is how APRS ends up on the wrong antenna with no other symptom.
    ambiguous = (
        " With more than one attached and no `-d` passed, which one a pipeline opens is"
        " librtlsdr's enumeration order, not a setting."
        if len(sdrs) > 1
        else ""
    )
    return SdrProbeOut(
        found=True,
        ready=True,
        summary=f"Found {named} ({sdrs[0].usb_id}){also}, unclaimed \u2014 userspace can open it.",
        next_step=f"Pass /dev/bus/usb into the sdr service{by_serial}. Do not pin the "
        f"per-device node ({node}) \u2014 it changes on every re-plug.{ambiguous}",
        sysfs_readable=True,
        usb_device_count=len(devices),
        sdrs=sdrs,
        devices=every,
    )


def _ca_read_failure() -> tuple[str, bool]:
    """Why Caddy's root is unreadable, and whether its directory can even be listed.

    Off the event loop because it touches a filesystem, and split out from the route for
    the same reason. The two facts together separate the only two causes that matter: a
    file that is not there yet (Caddy has never served the LAN site) from one that is
    there and denied (the api runs as a non-root user; Caddy writes that tree as root, and
    the directory holds the CA private key).
    """
    # The PUBLISHED copy first, because that is the one `_lan_ca` actually prefers. An
    # earlier version reported only the original path, so a box where the publisher was
    # working answered `ca_readable: true` beside a `ca_error` saying permission denied —
    # true of two different files, and confusing in exactly the moment this route is read.
    last = ""
    for path in (endpoint_api.CADDY_ROOT_PUBLISHED, endpoint_api.CADDY_ROOT_PATH):
        try:
            with open(path, encoding="utf-8") as fh:
                fh.read(1)
        except OSError as exc:
            last = str(exc)
            continue
        return "", True
    try:
        list(Path(endpoint_api.CADDY_ROOT_PATH).parent.iterdir())
        listable = True
    except OSError:
        listable = False
    return last, listable


# A flash writes ~1 MB over a serial link and may erase first. Generous but bounded: the
# sidecar holds the only device, and a request that has stopped making progress should end.
SIDECAR_FLASH_TIMEOUT_S = 600.0


class PanelFlashOut(BaseModel):
    """What the flasher did, as lines. `ok` false means the board was not written."""

    ok: bool
    port: str
    lines: list[str]
    detail: str = ""


class PanelFlashIn(BaseModel):
    # Omitted when exactly one panel is plugged in; required when two are, because a flash
    # ROTATES the unit's identity and doing that to the wrong twin's panel by inference is
    # not a thing this should be able to do.
    port: str = ""
    name: str = ""
    # Same flag the PWA offers, same default. The debug console is the fallback operator path
    # (CLAUDE.md #10), so a unit kind it cannot choose is a unit kind the owner cannot fix from
    # here when the PWA is the thing that is broken.
    role: endpoint_api.PanelRole = "jpet"
    erase: bool = False


@router.post("/endpoint/flash")
async def panel_flash(
    request: Request, settings: SettingsDep, _p: DebugDep, body: PanelFlashIn
) -> PanelFlashOut:
    """Flash a panel over USB from the debug console, using the remembered network.

    The owner runs this box remotely and a panel ends up on a bedroom wall. When one stops
    working, the fix is a re-flash — and until now that needed them at the PWA with the
    Wi-Fi password retyped, which is the errand CLAUDE.md #10 exists to remove. It is the
    same code path as the PWA's flash (`endpoint.build_flash`), so the two cannot drift.

    The credentials come from what the owner explicitly asked the box to remember, never
    from this request: a Wi-Fi password should not travel through a debug transcript, and
    a surface that accepted one would invite exactly that. No remembered network is a 409
    naming the one action that fixes it.

    It still mints a FRESH device key and revokes the old one, like every flash. That is
    the point of a re-flash as much as the firmware is.
    """
    request.state.debug_detail = f"flash panel {body.port or 'auto'} {body.name}".strip()
    base = settings.endpoint_url.strip().rstrip("/")
    if not base:
        return PanelFlashOut(ok=False, port=body.port, lines=[], detail="no panel flasher")

    ssid, password = await endpoint_api.remembered_wifi(request, _OWNER_CTX)
    if not ssid:
        return PanelFlashOut(
            ok=False,
            port=body.port,
            lines=[],
            detail=(
                "this box has not been asked to remember a network — flash once from the "
                "PWA with 'Remember this network' ticked, and re-flashes can happen here"
            ),
        )

    port = body.port
    try:
        async with httpx.AsyncClient(timeout=SIDECAR_FLASH_TIMEOUT_S) as client:
            if not port:
                resp = await client.get(f"{base}/ports")
                resp.raise_for_status()
                found = [p for p in resp.json().get("ports", []) if p.get("is_espressif")]
                if len(found) != 1:
                    return PanelFlashOut(
                        ok=False,
                        port="",
                        lines=[],
                        detail=f'{len(found)} panels visible; name one with "port"',
                    )
                port = str(found[0]["device"])

            payload = await endpoint_api.build_flash(
                request,
                settings,
                request.app.state.device_repo,
                _OWNER_CTX,
                port=port,
                ssid=ssid,
                password=password,
                name=body.name,
                role=body.role,
                erase=body.erase,
            )
            async with client.stream("POST", f"{base}/flash", json=payload) as stream:
                raw = b"".join([chunk async for chunk in stream.aiter_bytes()])
    except httpx.HTTPError as exc:
        return PanelFlashOut(ok=False, port=port, lines=[], detail=f"flasher: {exc}")

    lines = raw.decode("utf-8", "replace").splitlines()
    # The sidecar reports failure as a final `FAILED:` LINE, not a status code — the
    # response has already begun by the time esptool can fail.
    failed = any(x.startswith("FAILED:") for x in lines)
    return PanelFlashOut(ok=not failed and bool(lines), port=port, lines=lines)


class PanelAddressOut(BaseModel):
    """Where a panel would be told to find this box, and WHY that answer."""

    lan_addr: str
    ca_path: str
    ca_readable: bool
    ca_bytes: int
    # WHY the read failed, verbatim. "No such file" and "Permission denied" are entirely
    # different faults with entirely different fixes, and `_lan_ca` deliberately collapses
    # both to "" because a caller deciding an address does not care which.
    ca_error: str
    ca_parent_listable: bool
    panel_base: str
    pins_ca: bool
    on_the_lan: bool
    why: str


@router.get("/endpoint/address")
async def panel_address(request: Request, settings: SettingsDep, _p: DebugDep) -> PanelAddressOut:
    """Why a panel is, or is not, talking to this box over the LAN.

    A panel sits on the same network as the box, so sending its traffic out through the
    tunnel and back is latency bought for nothing — and worse, it makes the box's
    availability depend on the internet for a device three metres away. `_panel_base`
    exists to prevent that, and it takes the LAN branch only when BOTH halves hold: an
    address is configured AND Caddy's internal root is readable at the mount.

    When it falls back there is nothing to see. The manifest log records the address a
    panel was given but not which of the two halves was missing, and they have completely
    different fixes — one is a host `.env` value, the other is a certificate that Caddy
    mints only when it actually serves the LAN site. This says which.
    """
    request.state.debug_detail = "panel address decision"
    ca = endpoint_api._lan_ca()

    # Re-read deliberately rather than reusing the "" above: this surface exists to say
    # WHICH failure it was, and the api runs as a non-root user while Caddy writes that
    # tree as root — so "cannot read" is at least as likely to be a traversal denial on a
    # directory holding the CA private key as it is a missing file.
    ca_error, parent_listable = await asyncio.to_thread(_ca_read_failure)
    lan = settings.lan_addr.strip().rstrip("/")
    base, pinned = endpoint_api._panel_base(request, settings)

    if lan and ca:
        why = "LAN address configured and its root is readable, so a panel stays on the LAN"
    elif not lan and not ca:
        why = "no LAN address set and no internal root readable — the tunnel is all there is"
    elif not lan:
        why = (
            "Caddy's internal root is readable but JBRAIN_LAN_ADDR is empty, so nothing "
            "names the LAN site. Set it in the host .env and re-run Ops -> Update."
        )
    elif "Permission denied" in ca_error:
        why = (
            f"JBRAIN_LAN_ADDR is {lan!r} and the root EXISTS but this process cannot read "
            "it: the api runs as a non-root user and Caddy writes that tree as root, into a "
            "directory that also holds the CA private key. The proxy is supposed to publish "
            f"the public root to {endpoint_api.CADDY_ROOT_PUBLISHED} "
            "(deploy/proxy-publish-ca.sh) — if that file is absent, the proxy image predates "
            "it and needs an Ops -> Update."
        )
    else:
        why = (
            f"JBRAIN_LAN_ADDR is {lan!r} but no root is readable "
            f"({ca_error or 'no error reported'}). Caddy mints that root only once it "
            "actually serves the `tls internal` LAN site, so either the site is not "
            "configured in the proxy or the caddy_data mount is absent."
        )

    return PanelAddressOut(
        lan_addr=lan,
        ca_path=endpoint_api.CADDY_ROOT_PATH,
        ca_readable=bool(ca),
        ca_bytes=len(ca),
        ca_error=ca_error,
        ca_parent_listable=parent_listable,
        panel_base=base,
        pins_ca=bool(pinned),
        on_the_lan=bool(lan and ca),
        why=why,
    )


class PanelConsoleOut(BaseModel):
    """What one panel said, as lines. `ok` false means it could not be watched at all."""

    ok: bool
    port: str
    lines: list[str]
    detail: str = ""


@router.get("/endpoint/console")
async def panel_console(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    port: str = "",
    seconds: int = 25,
    reset: bool = True,
) -> PanelConsoleOut:
    """A panel's own console, collected and returned — the debug-token twin of the PWA's
    live console (`/endpoint/monitor`, owner-only).

    It exists because the owner had to be a relay. A flashed panel logs the reason an OTA
    failed to a console only the PWA could read, so diagnosing it meant asking them to open
    a screen, press a button and paste lines back — precisely the shape of errand CLAUDE.md
    #10 says not to hand someone who is operating this box remotely.

    Buffered rather than streamed: this surface returns JSON, and a bounded collection is
    what makes it answerable in one call. `reset` defaults TRUE here and false on the PWA
    route, and the difference is the caller — a console opened over a debug token is being
    read by something debugging, and waiting out a 15-minute poll to see a boot is not a
    reasonable use of the one window we have.
    """
    request.state.debug_detail = f"panel console {port or 'auto'} {seconds}s"
    base = settings.endpoint_url.strip().rstrip("/")
    if not base:
        return PanelConsoleOut(ok=False, port=port, lines=[], detail="no panel flasher on this box")

    try:
        async with httpx.AsyncClient(timeout=float(seconds) + 30.0) as client:
            if not port:
                # Convenience that matters on a box with two panels on one cable tray: an
                # explicit port always wins, but the common case is one plugged in.
                resp = await client.get(f"{base}/ports")
                resp.raise_for_status()
                found = [p for p in resp.json().get("ports", []) if p.get("is_espressif")]
                if len(found) != 1:
                    return PanelConsoleOut(
                        ok=False,
                        port="",
                        lines=[],
                        detail=f"{len(found)} panels visible; name one with ?port=",
                    )
                port = str(found[0]["device"])

            params = {"port": port, "seconds": str(seconds), "reset": "1" if reset else "0"}
            async with client.stream("GET", f"{base}/monitor", params=params) as stream:
                body = b"".join([chunk async for chunk in stream.aiter_bytes()])
    except httpx.HTTPError as exc:
        return PanelConsoleOut(ok=False, port=port, lines=[], detail=f"flasher: {exc}")

    lines = body.decode("utf-8", "replace").splitlines()
    failed = any(x.startswith("FAILED:") for x in lines)
    return PanelConsoleOut(ok=not failed, port=port, lines=lines)


@router.get("/sdr")
async def sdr_probe(request: Request, settings: SettingsDep, _p: DebugDep) -> SdrProbeOut:
    """Is the USB SDR dongle actually there, what exactly is it called, and is
    anything holding it?

    The S0 spike of `docs/plans/SDR_RADIO_PLAN.md`, and deliberately the cheapest
    possible one: enumerating and NAMING a USB device is a sysfs read, so this works
    with no device passthrough, no privileges, and no sdr container \u2014 it can answer
    "will this work?" before any of that exists. Proxied from the supervisor (the only
    container that reads /sys), like host metrics.

    The answer that matters is `ready`. A dongle found but claimed by the kernel's
    DVB-T driver is the expected first result on a stock Ubuntu box and is exactly what
    the blacklist step fixes; `next_step` says so rather than leaving the reader to
    know it."""
    request.state.debug_detail = "sdr usb probe"
    try:
        scan = await _usb_scan(request, settings)
    except httpx.HTTPError as unreachable:
        # MEASURED 2026-09-04: a USB port reset makes the bus read slow enough to time
        # out, and this route answered the owner with a 500 and a traceback — during the
        # exact minute they most needed to know what was on the bus. "Cannot tell" is a
        # real state this shape already carries; a 500 is not an answer at all.
        return SdrProbeOut(
            found=False,
            ready=False,
            summary=f"Cannot tell — the USB scan did not answer ({type(unreachable).__name__}).",
            next_step="Try again in a moment. A scan that times out is normal while a "
            "device is re-enumerating, which a reset causes on purpose.",
            sysfs_readable=False,
            usb_device_count=0,
            sdrs=[],
            devices=[],
        )
    return _sdr_verdict(scan)


class SdrCaptureOut(BaseModel):
    frequency_hz: int
    frequency_mhz: float
    mode: str
    seconds: float
    #: Loudest sample of the DEMODULATED AUDIO, 0..1 of full scale — **not a signal
    #: level** (F9). Named for what it is because the spectrum path now reports true
    #: dBFS per bin, and two numbers both called "peak" in the same surface, in
    #: different units, measuring different things, is how one gets read as the other.
    audio_peak: float
    heard_something: bool
    transcript: str | None
    transcript_error: str | None
    device_log: str


@router.post("/sdr/sweep", status_code=202)
async def sdr_sweep(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    # Bounded to what the RADIO reaches, so a request outside the capture plan arrives
    # here and gets a sentence rather than a 422 validation blob — which is the one
    # surface an owner with no terminal has (CLAUDE.md #10).
    start_mhz: Annotated[float, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)],
    stop_mhz: Annotated[float, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)],
    bin_khz: Annotated[float, Query(ge=0.1, le=100.0)] = 5.0,
    seconds: Annotated[float, Query(ge=1.0, le=900.0)] = 60.0,
    gain: Annotated[str | None, Query()] = None,
    channel_khz: Annotated[float, Query(ge=0.0, le=20_000.0)] = 0.0,
    include_csv: Annotated[bool, Query()] = False,
) -> JobSubmitOut:
    """Sweep a band and report what was busy in it. A BACKGROUND JOB — poll `/jobs/{id}`.

    Deferred because it has to be: a five-minute sweep will not survive the tunnel's
    ~100 s edge limit, and this is the shape `/complete-async` already established for
    exactly that reason.

    A measuring instrument, not an agent tool, and deliberately so. The detector's
    thresholds have to be calibrated against THIS box's noise floor and spur pattern —
    per-box facts nothing documents, which is the same reason `/grounding` exists for a
    vision model's coordinate base. Guessing them would ship a detector that reports the
    tuner's own artifacts as stations. Once a few real sweeps say what the floor looks
    like, the agent-facing tool can be designed against measurements instead of hopes.

    **This takes the radio** for the length of the sweep, as a real lease with the
    omnibox icon and Release — so it is refused, with a 409 naming the radio, when the
    one it would open is already held.

    `channel_khz` folds the busy bins onto a channel grid and sets how wide a
    neighbourhood `steady` judges a bin against — one number, because both answer "how
    wide is a signal here". 15 for 2m, 25 for 70cm and airband and marine, 200 for FM
    broadcast, thousands for a cellular carrier. It matters twice over off the narrowband
    bands: unfolded, a 200 kHz signal in a 5 kHz sweep reads as forty stations, and
    unsized, `steady` measures a wide carrier against a window sitting inside it and sees
    nothing. Zero leaves the bins alone and takes the narrowband default neighbourhood.

    `include_csv` returns the raw rows alongside the reduction. Off by default because it
    is megabytes; worth having because calibrating a detector against a summary the
    detector produced is circular, and the first round of this was done by reading
    brightness off the PNG.

    **Shortwave is surveyable now.** The floor that refused it was `rtl_power`'s, and B2
    moved the survey onto the engine that sets the ADC branch at runtime
    (`docs/archive/SDR_RECEIVER_CONVERGENCE_PLAN.md` A5/B2)."""
    request.state.debug_detail = f"sdr sweep {start_mhz}-{stop_mhz} MHz for {seconds}s"
    if not settings.sdr_url:
        raise HTTPException(status_code=503, detail="No SDR on this box (sdr_url unset).")
    # THE SAME QUESTION THE PICTURE ASKS, since B2 put the survey on the picture's
    # engine. `sweepable` was `rtl_power`'s question — it refused everything below
    # 24 MHz because the tool hardcodes the ADC's I branch and this board wires Q — and
    # the tool is gone, so what is left is whatever the capture plan covers.
    #
    # `_span` checks BOTH EDGES, which the sidecar cannot: it validates the sweep's
    # centre, so a 10-70 MHz request centres on 40 and passes every check while its
    # bottom half cannot be measured at all and comes back reported as quiet.
    # WHICH RADIO FIRST, because the capture plan depends on what is in front of it: a
    # converted shortwave span is the tuner doing ordinary work at VHF, not the ADC
    # branch its own edges imply.
    serial = await _radio(request, settings, GENERAL)
    rig = await _rig(request, serial)
    start_hz, stop_hz, _picture_bin, capture = sdr_api._span(  # noqa: SLF001
        None, start_mhz, stop_mhz, rig.upconverter_hz
    )

    body: dict[str, Any] = {
        "start_hz": start_hz,
        "stop_hz": stop_hz,
        # The width the SURVEY is written at, which is the caller's to choose and is NOT
        # the transform's. The sidecar folds one into the other and reports which it
        # used; nothing here assumes they are equal.
        "bin_hz": int(round(bin_khz * 1_000)),
        "seconds": seconds,
        # This call's gain if it named one — measuring the same band at two gains on
        # purpose is what this route is for — and otherwise the radio's standing choice.
        "gain": sdr_api._tuner_gain(gain, rig),  # noqa: SLF001
        "upconverter_hz": rig.upconverter_hz,
        # A sweep is a general use of the radio, so it may not take one reserved for a
        # service. Resolved BEFORE the job is queued, so a refusal is this request's 409
        # rather than an error the caller has to poll for.
        "serial": serial,
    }
    if capture is not None:
        # The capture the plan named, as the spectrum routes send it: the band table
        # lives in ONE place and the sidecar executes what it was handed.
        body["rate_hz"], body["bins"], body["hops"] = capture
    jobs = request.app.state.debug_jobs
    tasks = request.app.state.debug_job_tasks
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "pending", "result": None, "error": None}
    if len(jobs) > _MAX_JOBS:
        for jid, val in list(jobs.items())[:-_MAX_JOBS]:
            if val["status"] != "pending":
                jobs.pop(jid, None)

    base_url = cast(str, settings.sdr_url)

    async def _run() -> None:
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=seconds + 120) as client:
                resp = await client.post("/sweep", json=body)
            if resp.status_code != 200:
                detail = resp.json().get("detail", "") if resp.content else resp.text[:400]
                jobs[job_id] = {
                    "status": "error",
                    "result": None,
                    "error": f"sdr sidecar: {detail}",
                }
                return
            payload = resp.json()
            csv_text = str(payload.get("csv") or "")
            spacing = int(round(channel_khz * 1_000))
            reduced = reduce_csv(csv_text, channel_hz=spacing)
            busy = channels(reduced, spacing)
            out = SdrSweepOut(
                start_hz=reduced.start_hz or body["start_hz"],
                stop_hz=reduced.stop_hz or body["stop_hz"],
                bin_hz=reduced.bin_hz or body["bin_hz"],
                seconds=seconds,
                rows=reduced.rows,
                bins=reduced.bins,
                floor_db=reduced.floor_db,
                revisit_s=reduced.revisit_s,
                complete=bool(payload.get("complete")),
                busy=[
                    SweepBinOut(
                        hz=b.hz,
                        mhz=round(b.hz / 1_000_000, 6),
                        floor_db=b.floor_db,
                        peak_db=b.peak_db,
                        occupancy=b.occupancy,
                    )
                    # Bounded: a noisy sweep can light hundreds of bins, and a debug
                    # response that large is one nobody reads.
                    for b in busy[:200]
                ],
                steady=[
                    SweepBinOut(
                        hz=b.hz,
                        mhz=round(b.hz / 1_000_000, 6),
                        floor_db=b.floor_db,
                        peak_db=b.peak_db,
                        occupancy=b.occupancy,
                    )
                    for b in steady_channels(reduced, spacing)[:64]
                ],
                uncovered=[
                    SweepGapOut(
                        start_mhz=round(lo / 1_000_000, 6),
                        stop_mhz=round(hi / 1_000_000, 6),
                        khz=round((hi - lo) / 1_000, 1),
                    )
                    for lo, hi in reduced.uncovered[:64]
                ],
                png_base64=base64.b64encode(waterfall_png(reduced)).decode(),
                # The size is always here so a caller can tell an empty sweep from an
                # unparsed one without paying for the whole CSV.
                csv_chars=len(csv_text),
                csv=csv_text if include_csv else None,
                gain_db=(
                    float(payload["gain_db"])
                    if isinstance(payload.get("gain_db"), (int, float))
                    else None
                ),
                tuner_bypassed=payload.get("tuner_bypassed") is True,
                upconverter_hz=body["upconverter_hz"],
            )
            jobs[job_id] = {"status": "done", "result": out, "error": None}
        except Exception as exc:  # noqa: BLE001 - a debug job must surface, not crash the loop
            jobs[job_id] = {"status": "error", "result": None, "error": str(exc)}

    task = asyncio.create_task(_run())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return JobSubmitOut(job_id=job_id)


def _receivable(frequency_mhz: float, upconverter_hz: int = 0) -> None:
    """Refuse a frequency the radio would answer with a DIFFERENT one — the debug twin
    of `api/sdr.py`'s `_tunable`, and needed here for the same reason the owner routes
    need it: the `Query` bounds check the ENDS, and 14.4-24 MHz sits inside them and is
    reached by neither path. Below 24 MHz the sidecar tunes with `-E direct2`, and
    direct sampling folds the second Nyquist zone back onto the first, so 18.1 MHz is
    received as 10.7 (SDR_IQ_SPECTRUM_PLAN §8). A capture from there transcribes
    cleanly and names the wrong band.

    A converter takes that hole away rather than narrowing it, so the question is asked
    of the TUNE (`jbrain.sdr.tuner.out_of_range`)."""
    refusal = out_of_range(frequency_mhz, upconverter_hz / 1_000_000)
    if refusal:
        raise HTTPException(status_code=400, detail=refusal[0].upper() + refusal[1:])


@router.post("/sdr/capture")
async def sdr_capture(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    frequency_mhz: Annotated[float, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)],
    seconds: Annotated[float, Query(ge=0.5, le=120.0)] = 8.0,
    mode: Annotated[str, Query(pattern="^(fm|nfm|wbfm|am|usb|lsb)$")] = "fm",
    gain: Annotated[str | None, Query()] = None,
    transcribe: Annotated[bool, Query()] = True,
) -> SdrCaptureOut:
    """Tune the radio, record a few seconds, and (by default) run it through whisper.

    The end-to-end proof for the SDR plan's S0b-ii gate, driven from the debug console
    so it needs no terminal (CLAUDE.md #10). Frequency and mode are the ONLY inputs —
    never a URL or a host — and both are bounded here as well as in the sidecar, so the
    `stream.py` SSRF guard is neither used nor widened (plan §4.4).

    `peak` is the loudest sample as a fraction of full scale, and `heard_something` is
    the honest read on it: a dead antenna, a mistuned frequency and a working capture of
    silence all return the same duration, and only the level tells them apart. A
    transcript of an empty band is whisper hallucinating on noise, so judge the audio by
    `peak` first and the words second."""
    request.state.debug_detail = f"sdr capture {frequency_mhz} MHz {mode}"
    if not settings.sdr_url:
        raise HTTPException(status_code=503, detail="No SDR on this box (sdr_url unset).")

    serial = await _radio(request, settings, GENERAL)
    rig = await _rig(request, serial)
    _receivable(frequency_mhz, rig.upconverter_hz)
    freq_hz = int(round(frequency_mhz * 1_000_000))
    async with httpx.AsyncClient(base_url=settings.sdr_url, timeout=seconds + 60) as client:
        resp = await client.post(
            "/capture",
            json={
                # The OWNER's frequency. The offset below is what shifts the tune, and
                # the WAV that comes back is stamped with this one.
                "frequency_hz": freq_hz,
                "seconds": seconds,
                "mode": mode,
                "gain": sdr_api._tuner_gain(gain, rig),  # noqa: SLF001
                "upconverter_hz": rig.upconverter_hz,
                # A capture is a general use of the radio. The sidecar has accepted a
                # serial here since before radio roles existed; nothing had ever sent one.
                "serial": serial,
            },
        )
    if resp.status_code == 409:
        # The sidecar's OWN sentence, not a guess. Since sessions became per radio it
        # says which radio and what holds it — "the radio (77192819) is already logging
        # APRS" — and overwriting that with "another capture" was both wrong and
        # unactionable on the one surface the owner has when they cannot reach a
        # terminal (CLAUDE.md #10). The sibling doors already pass it through.
        raise HTTPException(status_code=409, detail=_sidecar_detail(resp, "The radio is busy."))
    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text[:400]) if resp.content else resp.text[:400]
        raise HTTPException(status_code=502, detail=f"sdr sidecar: {detail}")

    meta = cast(dict[str, Any], json.loads(resp.headers.get("X-Sdr-Meta") or "{}"))
    wav = resp.content

    transcript: str | None = None
    error: str | None = None
    if transcribe:
        # The whisper client is built per use from the setting, exactly as the video and
        # stream paths do in main.py — it is not held on app.state, and reading it from
        # there is how the first on-box capture came back untranscribed.
        if not settings.whisper_url:
            error = "no whisper gateway on this box (whisper_url unset)"
        else:
            try:
                result = await transcribe_audio_chunked(
                    WhisperCppClient(
                        settings.whisper_url,
                        settings.whisper_model,
                        timeout=settings.whisper_timeout,
                    ),
                    LocalGatewayClient(settings.whisper_url),
                    settings.whisper_model,
                    wav,
                    filename=f"sdr-{freq_hz}.wav",
                )
                transcript = (result or {}).get("text") or ""
            except Exception as exc:  # noqa: BLE001 - report, never sink the capture
                error = repr(exc)

    audio_peak = float(meta.get("audio_peak") or 0.0)
    return SdrCaptureOut(
        frequency_hz=freq_hz,
        frequency_mhz=frequency_mhz,
        mode=meta.get("mode") or mode,
        seconds=float(meta.get("seconds") or 0.0),
        audio_peak=audio_peak,
        # 1% of full scale is comfortably above a quiet noise floor and well below any
        # real signal — enough to tell "the radio produced audio" from "it produced zeros".
        heard_something=audio_peak > 0.01,
        transcript=transcript,
        transcript_error=error,
        device_log=str(meta.get("device_log") or ""),
    )


@router.post("/sdr/listen")
async def sdr_listen_debug(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    frequency_mhz: Annotated[float, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)],
    mode: Annotated[str, Query(pattern="^(fm|nfm|wbfm|am|usb|lsb)$")] = "wbfm",
) -> dict[str, Any]:
    """Take the radio and start listening — the debug twin of `POST /api/sdr/listen`.

    The owner surface is `OwnerDep` and a capability token is deliberately on a
    physically distinct path, so it cannot reach that route. Without this twin the
    radio is undrivable from a handed-over token, which matters because starting a
    session is what makes the composer's tuner icon appear at all: there would be no
    way to exercise the surface except through the agent."""
    request.state.debug_detail = f"sdr listen {frequency_mhz} MHz {mode}"
    _receivable(frequency_mhz)
    return await _sdr_post(
        settings,
        "/listen/start",
        {
            "frequency_hz": int(round(frequency_mhz * 1_000_000)),
            "mode": mode,
            "gain": None,
            "serial": await _radio(request, settings, GENERAL),
        },
    )


#: How long a USB port reset may take before the api stops waiting. MEASURED: the ioctl
#: outran the ordinary 30 s sidecar timeout on a device that was in trouble, which is
#: exactly the device anyone resets — the kernel waits on a port that may never answer,
#: and the wait is the operation rather than a hang. Generous, and still bounded: a
#: request held for ever is its own fault.
RESET_TIMEOUT_S = 120.0


@router.post("/sdr/reset")
async def sdr_reset_debug(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    serial: Annotated[str, Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
) -> dict[str, Any]:
    """Re-enumerate one dongle — the debug twin of `POST /api/sdr/radios/{serial}/reset`.

    The owner surface is `OwnerDep` and a capability token is on a physically distinct
    path, so it cannot reach that route. Without a twin, the one recovery for a radio
    that has stopped answering would be unreachable from a handed-over token — which is
    the situation it was written in: the dongle was already broken and nobody was home."""
    request.state.debug_detail = f"sdr reset {serial}"
    node = nodes_in(await _usb_scan(request, settings)).get(serial)
    if node is None:
        raise HTTPException(
            status_code=404,
            detail=f"No radio {serial} in the USB scan, so there is no device to reset.",
        )
    return await _sdr_post(
        settings, "/reset", {"serial": serial, "device_node": node}, wait_s=RESET_TIMEOUT_S
    )


#: How long a Soapy probe may take before the api stops waiting. It opens a device,
#: times a dozen USB buffers, retunes four times and then deliberately starves the
#: stream for a second — seconds of real work, where the 30 s default assumes a call
#: that either answers or has failed.
SOAPY_PROBE_TIMEOUT_S = 120.0


@router.post("/sdr/soapy-probe")
async def sdr_soapy_probe(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    frequency_mhz: Annotated[float, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)] = 10.0,
    rate_hz: Annotated[int, Query(ge=225_000, le=3_200_000)] = 256_000,
    bins: Annotated[int, Query(ge=16, le=8192)] = 1024,
    serial: Annotated[str | None, Query(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] = None,
) -> dict[str, Any]:
    """**Does this radio really behave the way the I/Q spectrum engine assumes?**

    The F0 spike of `docs/plans/SDR_IQ_SPECTRUM_PLAN.md`, run from the console because
    the owner has no terminal to run `SoapySDRUtil` from (CLAUDE.md #10). It drives
    `deploy/sdr/radio.py` against a real dongle and returns a VERDICT — `ok`, a one-line
    `summary`, and a `findings` list naming any claim that did not hold — with the
    evidence under it rather than instead of it.

    Seven claims, each of which the engine is already written against: SoapySDR
    enumerates the dongles; `serial=` opens the one it names (everything per-radio in
    this system depends on that); `direct_samp=2` reads back as 2, which is the whole
    shortwave story; `setFrequency` and `setSampleRate` work ON A LIVE STREAM with no
    rebuild, which is why pan and zoom stop blanking; `SOAPY_SDR_OVERFLOW` really is
    reported under induced backpressure, where `rtl_sdr` dropped samples silently; the
    achieved sample rate comes back off librtlsdr's divider unchanged; and — the one
    nothing else can answer — **`bufflen` actually took**, measured as a callback
    period, because librtlsdr replaces a bad value silently and `getStreamMTU` reports
    the value that was asked for rather than the one in use.

    It also captures one frame through `iq.py` and reports the peak bin against the
    frame's own median, so the default of 10 MHz is a WWV check: a carrier well clear of
    the floor there is the direct-sampling path working end to end.

    **TAKES A RADIO** through the same lease as every other holder, so it is refused
    with a 409 while something else has that dongle and released the moment it is done.
    `serial` picks which one — the point of the probe on a two-dongle box — and defaults
    to whichever the resolver would give a general job."""
    request.state.debug_detail = f"sdr soapy probe {frequency_mhz} MHz"
    _receivable(frequency_mhz)
    if serial is not None:
        # Checked against the scan for `sdr_reset_debug`'s reason: a serial that names
        # no device would otherwise reach the sidecar and come back as a driver-level
        # "could not open", which reads like a broken radio rather than a typo.
        if serial not in nodes_in(await _usb_scan(request, settings)):
            raise HTTPException(
                status_code=404,
                detail=f"No radio {serial} in the USB scan, so there is nothing to probe.",
            )
    else:
        serial = await _radio(request, settings, GENERAL)
    return await _sdr_post(
        settings,
        "/soapy/probe",
        {
            "serial": serial,
            "center_hz": int(round(frequency_mhz * 1_000_000)),
            "rate_hz": rate_hz,
            "bins": bins,
        },
        wait_s=SOAPY_PROBE_TIMEOUT_S,
    )


SPECTRUM_PROBE_TIMEOUT_S = 60.0


@router.post("/sdr/spectrum-probe")
async def sdr_spectrum_probe(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    section: Annotated[str | None, Query(max_length=48)] = None,
    start_mhz: Annotated[float | None, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)] = None,
    stop_mhz: Annotated[float | None, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)] = None,
    seconds: Annotated[float, Query(ge=1.0, le=15.0)] = 3.0,
    serial: Annotated[str | None, Query(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] = None,
) -> dict[str, Any]:
    """**Did the engine swap actually work on this radio?** F6's twin of `soapy-probe`.

    A live spectrum is an owner route behind a websocket, so until this existed the only
    way to see whether F6 worked on real hardware was for the owner to open the Radio
    tab and look. An owner with no terminal cannot be the test harness for their own box
    (CLAUDE.md #10), and "it renders" is not a measurement of a frame rate or a bin
    width anyway.

    It runs the REAL decision rather than a copy of it: `_span` picks the range, the
    width and the one-hop capture exactly as `POST /api/sdr/spectrum` does, so a bug in
    that choice shows up here instead of hiding behind a probe that chose differently.
    The sidecar then starts a genuine spectrum session, watches it for a few seconds and
    gives the radio back.

    Three claims it can fail, each invisible from anywhere else. **Which engine ran** —
    the sidecar drops to `rtl_power` at runtime when a radio will not open, and a silent
    downgrade is a waterfall quietly at a tenth of the rate it claims. **Whether
    `bin_hz` on the wire is exactly `rate / bins`** — a frame declaring a width the
    transform never used is the one failure nothing downstream can see. **What the frame
    rate really is** — `rtl_power` clamps its interval to a second in its own C, so a
    measured rate above that IS the ceiling being gone, and no static reading proves it.

    **TAKES A RADIO** for those seconds through the same lease as everything else: a 409
    names the holder, and the session is released even when the probe fails, because the
    owner has no terminal to free a radio a diagnostic walked away from."""
    request.state.debug_detail = f"sdr spectrum probe {section or f'{start_mhz}-{stop_mhz}'}"
    start_hz, stop_hz, chosen_bin, capture = sdr_api._span(section, start_mhz, stop_mhz)  # noqa: SLF001
    if serial is not None:
        if serial not in nodes_in(await _usb_scan(request, settings)):
            raise HTTPException(
                status_code=404,
                detail=f"No radio {serial} in the USB scan, so there is nothing to probe.",
            )
    else:
        serial = await _radio(request, settings, GENERAL)
    body: dict[str, Any] = {
        "start_hz": start_hz,
        "stop_hz": stop_hz,
        "bin_hz": chosen_bin,
        "seconds": seconds,
        "serial": serial,
    }
    if capture is not None:
        # Three since F11, and the hop count has to travel with the rest: a sidecar
        # reading it as absent would sweep a wide span at one tuning and draw the
        # wrong band confidently.
        body["rate_hz"], body["bins"], body["hops"] = capture
    return await _sdr_post(settings, "/spectrum/probe", body, wait_s=SPECTRUM_PROBE_TIMEOUT_S)


@router.post("/sdr/listen-probe")
async def sdr_listen_probe(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    mhz: Annotated[float, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)],
    mode: Annotated[str, Query(max_length=8, pattern=r"^[a-z]+$")] = "fm",
    seconds: Annotated[float, Query(ge=1.0, le=20.0)] = 5.0,
    serial: Annotated[str | None, Query(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] = None,
    gain: Annotated[str | None, Query(max_length=8, pattern=r"^[0-9.]+$")] = None,
    transcribe: Annotated[bool, Query()] = False,
    band: Annotated[bool, Query()] = False,
    retune_to: Annotated[float | None, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)] = None,
) -> dict[str, Any]:
    """**Does the numpy demodulator work on this radio?** The twin of `spectrum-probe`.

    Listening is an owner route and the audio is an MP3 stream, so before this the only
    way to know whether `demod.py` worked on real hardware was for the owner to press
    play and listen — and an owner with no terminal cannot be the test harness for
    their own box (CLAUDE.md #10).

    It answers what the synthetic-signal tests cannot: which engine actually ran (the
    listen pipeline falls back to `rtl_fm` at runtime and says nothing), whether the
    station landed where the offset tuning says it should, whether the audio is a
    signal rather than silence or a rail, and how many USB buffers the driver threw
    away — which on a waterfall is one row slightly wrong and on audio is a click.

    `transcribe` is the one that catches what the others cannot, and it exists because
    everything above measures LEVEL. An FM discriminator differentiates phase and is
    blind to amplitude, so a chain demodulating the wrong piece of spectrum emits noise
    at FULL SCALE — a healthy peak, a healthy RMS, a moving tape, a plausible row. The
    offset tuning was inverted for the life of this path and every one of those readings
    stayed green (2026-09-06). Words are the only evidence that separates a receiver
    tuned to a station from one tuned to nothing, so this hands the demodulated audio to
    whisper. Judge `ok` and `view.snr_db` first and the transcript second: whisper
    hallucinates fluently on noise, and a transcript that does not match what the
    station is known to be transmitting is not evidence of anything.

    It is off by default because it costs the audio a trip through the gateway, and
    because the sidecar only taps the PCM when someone asks.

    `band` adds what ELSE was on the air, measured from the very same capture: one
    radio, one buffer, the channel demodulated and the whole 2.4 MHz around it
    transformed. That reading used to be impossible without giving up the audio — a
    spectrum session and a listening session are one dongle apiece. Off by default
    because the wideband transform is real work (~11% of a core) that the sidecar
    skips entirely while nobody has asked for it.

    `retune_to` moves the session to another frequency HALFWAY through and reports what
    that cost. It is the only way to see A2's claim from outside: "the session id
    survived" was true of a full pipeline rebuild too, by design, so the evidence is
    `retune.stream_rebuilt` (`setupStream`'s handle either side — false means the stream
    was never torn down) and `retune.worst_gap_ms` against `median_gap_ms`, which is the
    gap in the SOUND, since the rows come off the same buffer as the audio.

    **TAKES A RADIO** for those seconds and releases it even on failure."""
    request.state.debug_detail = f"sdr listen probe {mhz} {mode}"
    if serial is not None:
        if serial not in nodes_in(await _usb_scan(request, settings)):
            raise HTTPException(
                status_code=404,
                detail=f"No radio {serial} in the USB scan, so there is nothing to probe.",
            )
    else:
        serial = await _radio(request, settings, GENERAL)
    # `gain` travels because it is the thing most likely to be WRONG on a quiet band,
    # and comparing two runs is the only way to tell a deaf receiver from a dead one.
    # Absent hands the tuner to its own AGC, which is what `rtl_fm` does by default.
    body = {
        "mhz": mhz,
        "mode": mode,
        "seconds": seconds,
        "serial": serial,
        "gain": gain,
        "audio": transcribe,
        "band": band,
        "retune_mhz": retune_to,
    }
    answer = await _sdr_post(settings, "/listen/probe", body, wait_s=seconds + 25.0)
    # Stripped whatever happens next: a quarter of a megabyte of base64 in a console
    # response buries the eight lines that are the verdict.
    wav_b64 = answer.pop("audio_wav_b64", None)
    if not transcribe:
        return answer
    if not wav_b64:
        answer["transcript_error"] = "the sidecar returned no audio to transcribe"
        return answer
    if not settings.whisper_url:
        answer["transcript_error"] = "no whisper gateway on this box (whisper_url unset)"
        return answer
    try:
        result = await transcribe_audio_chunked(
            WhisperCppClient(
                settings.whisper_url, settings.whisper_model, timeout=settings.whisper_timeout
            ),
            LocalGatewayClient(settings.whisper_url),
            settings.whisper_model,
            base64.b64decode(wav_b64),
            filename=f"sdr-probe-{int(mhz * 1_000_000)}.wav",
        )
        answer["transcript"] = (result or {}).get("text") or ""
    except Exception as exc:  # noqa: BLE001 - report, never sink the verdict
        answer["transcript_error"] = repr(exc)
    return answer


@router.post("/sdr/stop")
async def sdr_stop_debug(request: Request, settings: SettingsDep, _p: DebugDep) -> dict[str, Any]:
    """Release the radio. The composer's tuner icon disappears when it lands."""
    request.state.debug_detail = "sdr stop"
    return await _sdr_post(settings, "/listen/stop", {"session_id": None})


@router.get("/sdr/sessions")
async def sdr_sessions_debug(
    request: Request, settings: SettingsDep, _p: DebugDep
) -> sdr_api.SdrStatusOut:
    """Which radios are held, and WHICH ONE the composer icon is showing.

    The owner surface (`GET /api/sdr/status`) is `OwnerDep`, so a handed-over token
    could see the USB bus and start a session but never read back what the owner's own
    screen says about it — and after B7 that answer is a decision this api makes, not
    something the sidecar reports. It calls the same `status_of`, so it cannot drift
    from the icon: a second derivation would be the very thing B7 deleted."""
    request.state.debug_detail = "sdr sessions"
    # `recording_now` for the same reason: the tape deck is api state rather than
    # something /healthz reports, and a console that omits it would say the box is idle
    # while a recording is running.
    return await sdr_api.status_of(settings, sdr_api.recording_now(request))


def _sidecar_detail(resp: httpx.Response, fallback: str) -> str:
    """The sidecar's own refusal, or `fallback` if it did not send one.

    One helper because two debug routes flattened it separately, and the flattening only
    became visible once the refusal started carrying the radio's serial: "The radio is
    busy" tells an owner with no terminal nothing they can act on, while "the radio
    (77192819) is already logging APRS" names the thing to turn off."""
    try:
        return cast(str, resp.json().get("detail") or fallback)
    except ValueError:
        return fallback


async def _sdr_post(
    settings: Any, path: str, body: dict[str, Any], wait_s: float = 30.0
) -> dict[str, Any]:
    if not settings.sdr_url:
        raise HTTPException(status_code=503, detail="No SDR on this box (sdr_url unset).")
    try:
        async with httpx.AsyncClient(base_url=settings.sdr_url, timeout=wait_s) as client:
            resp = await client.post(path, json=body)
    except httpx.TimeoutException as slow:
        # NOT "it failed". MEASURED 2026-09-04: a `USBDEVFS_RESET` outran the 30 s
        # default and the owner got a 500 with a traceback for an operation that had in
        # fact HAPPENED — the device left the bus. Saying "the radio did not reset" would
        # have been worse than the traceback, because it is false. What a timeout here
        # licenses is "look again", and nothing more.
        raise HTTPException(
            status_code=504,
            detail=f"The radio has not answered yet ({type(slow).__name__}). Whatever was "
            f"asked for may still be happening — read the radio list again in a moment "
            f"rather than assuming it did not.",
        ) from slow
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail=_sidecar_detail(resp, "The radio is busy."))
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"sdr sidecar: {resp.text[:300]}")
    return cast(dict[str, Any], resp.json())


@router.post("/update", status_code=202)
async def start_update_debug(
    request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, object]:
    """**Pull main, rebuild, restart** — the Ops → Update button, reachable with a token.

    The console could already WATCH an update (`/update/status`) and not cause one, so
    every deploy needed the owner at the PWA. That is a real cost when the thing being
    deployed is the only way to answer a question about the hardware: a probe run costs
    a merge, a tap and a wait, and the tap has to happen on someone else's schedule.

    **It takes no ref, and that is the security property.** The supervisor's `/update`
    builds whatever `main` is; there is no branch, tag or sha to pass, so a token cannot
    choose the code it deploys — only ask for what a merged PR already put on `main`,
    which is exactly what the button does. Widening this to an arbitrary ref would turn
    a capability token into remote code execution on the box, and no amount of
    operational convenience is worth that trade.

    What it does grant is real and worth naming rather than burying: anyone holding a
    live token can restart this box's services and roll it to current `main`. `DebugDep`
    is uniform — there is no per-token scope on this surface — so it reaches every token
    ever minted, not just new ones. The mitigation is the one the surface already has:
    tokens are revocable, time-boxed and listed for the owner, so the answer to "who
    holds one" is to revoke rather than to reason about it.

    409 while an update is already running, which is the supervisor's own mutual
    exclusion over its one-shots rather than a rule restated here. Poll `/update/status`
    for the log tail, and `/version` to know the new build is actually serving — a
    restart is not the same event as a rebuild, and only `git_sha` tells them apart."""
    request.state.debug_detail = "update (pull main, rebuild, restart)"
    engine_api.refuse_while_switching(request, "update")
    resp = await _supervisor(request).post(
        "/update", headers={"Authorization": f"Bearer {settings.supervisor_token}"}
    )
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="update already running")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


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


@router.post("/backup", status_code=202)
async def start_backup_debug(
    request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, object]:
    """**Take a full backup** — the Data screen's "Back up everything", reachable with a
    token.

    It exists because the console could already CAUSE the irreversible thing and not the
    reversible one. `/update` deploys, and a deploy can carry a destructive migration; the
    snapshot that makes such a deploy survivable was the single step only the owner could
    perform, from a screen whose name had drifted out of the docs (it moved from Ops to
    its own Data launcher). So the safe half of "back up, then update" depended on finding
    a button, and the unsafe half did not. That asymmetry is the gap this closes.

    **It starts a backup; it cannot read one.** The archive is written on the box and
    stays there — this returns the one-shot's state, never its bytes, and there is
    deliberately no download route beside it. A token that could pull the archive would be
    a way to exfiltrate every note, fact and attachment in one request, which is a far
    larger grant than anything else on this surface and is not worth the convenience.
    Retrieving the file remains the owner's, from the Data screen, over his own session.

    409 while another one-shot is running — the supervisor's own mutual exclusion, since
    a backup racing an update would snapshot a half-migrated database. Poll
    `/backup/status` for the log tail and the filename it wrote."""
    request.state.debug_detail = "backup (full export)"
    engine_api.refuse_while_switching(request, "back up")
    resp = await _supervisor(request).post(
        "/export", headers={"Authorization": f"Bearer {settings.supervisor_token}"}
    )
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="another one-shot is already running")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/backup/status")
async def backup_status(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    tail: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, object]:
    """The most recent backup one-shot's state + log tail, proxied from the supervisor.

    `state: "done"` with `exit_code: 0` is the only thing that licenses a destructive
    deploy. Read it before pressing `/update` on a release carrying a data migration —
    "I started a backup" is not the same claim as "a backup finished"."""
    request.state.debug_detail = f"backup status (tail {tail})"
    resp = await _supervisor(request).get(
        "/export/status",
        params={"tail": tail},
        headers={"Authorization": f"Bearer {settings.supervisor_token}"},
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.post("/refresh", status_code=202)
async def start_refresh_debug(
    request: Request, settings: SettingsDep, _p: DebugDep, service: str
) -> dict[str, object]:
    """**Pull main and rebuild ONE service** — the fast path between nothing and
    `/update`.

    `/update` pulls and then rebuilds the world: it backs up, quiesces the stack,
    rebuilds every image, unloads every model and takes about ten minutes. That is the
    right price for shipping and the wrong one for asking the radio a question. The sdr
    sidecar is pure Python behind an apt-only image, so a one-line change to a
    measurement cost a full system update to try — and on a box whose owner has no
    terminal (CLAUDE.md #10) there was no cheaper route, which made every hardware
    question a ten-minute round trip.

    **It takes no ref, and that is the same security property `/update` has.** The inner
    script resets the source mirror to its tracked upstream, so a token can ask for what
    a merged PR already put on `main` and nothing else. `service` is validated by the
    supervisor against the live compose service set before it reaches a shell-quoted
    command, so it is a known token rather than caller-controlled text.

    **It is a PARTIAL deploy, deliberately.** The source mirror moves to `main` for every
    service while only the named one is rebuilt, so the api can be running older code
    than `src` describes until a full `/update` follows — and `/version` reports the
    api's build, not the mirror's, so it will not show the change. It also does not
    refresh the host helper files, so a `docker-compose.yml` or Dockerfile-path change
    still needs the full update. Use it to iterate, `/update` to ship.

    Rebuilding a service recreates its container, so anything the service was holding —
    an sdr lease, a live spectrum — ends. 409 while another one-shot is running; poll
    `/refresh/status` for the log tail."""
    request.state.debug_detail = f"refresh {service} (pull main, rebuild one service)"
    engine_api.refuse_while_switching(request, f"refresh {service}")
    resp = await _supervisor(request).post(
        "/refresh",
        json={"service": service},
        headers={"Authorization": f"Bearer {settings.supervisor_token}"},
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"no such service: {service}")
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="another one-shot is running")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/refresh/status")
async def refresh_status(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    tail: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, object]:
    """The most recent refresh one-shot's state + log tail. Like `/update/status`, this
    is the only window into it: the one-shot runs OUTSIDE the compose project, so
    `/debug/logs/<service>` cannot reach it."""
    request.state.debug_detail = f"refresh (tail {tail})"
    resp = await _supervisor(request).get(
        "/refresh/status",
        params={"tail": tail},
        headers={"Authorization": f"Bearer {settings.supervisor_token}"},
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/provision/status")
async def provision_status(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    tail: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, object]:
    """The most recent local-model DOWNLOAD one-shot's state + log tail (the PWA
    'Download' action, deploy/local-models-sync.sh). Like /update/status, the sync
    runs OUTSIDE the compose project, so /debug/logs/<service> can't reach it — this
    is the read-only console's window into WHY a model download failed (the verbose
    per-model hf output — repo, include globs, 404/auth/disk reason — streams here).
    Proxied from the supervisor; mirrors the owner ops surface."""
    request.state.debug_detail = f"provision (tail {tail})"
    resp = await _supervisor(request).get(
        "/provision/status",
        params={"tail": tail},
        headers={"Authorization": f"Bearer {settings.supervisor_token}"},
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


# --- Host metrics (proxied to the supervisor) -------------------------------
# "What is using the box's RAM?" The read-only meter (host_metrics) only knows
# MemTotal/MemAvailable — the unified-memory TOTAL, with no breakdown. The
# supervisor owns the docker socket, so it can attribute usage per container
# (the local-llm container's number includes the loaded model RSS, since the
# gateway runs --no-mmap). This proxies that, the same way /logs and
# /update/status do, so the console can answer the breakdown question directly.


class ContainerMem(BaseModel):
    service: str
    mem_bytes: int


class ProcessMem(BaseModel):
    service: str
    pid: int
    rss_bytes: int
    command: str


class HostMetricsOut(BaseModel):
    mem_total_bytes: int
    mem_available_bytes: int
    swap_total_bytes: int
    swap_free_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    load_1m: float
    load_5m: float
    load_15m: float
    uptime_seconds: int
    gpu_busy_percent: float | None
    fan_rpm: dict[str, int] | None
    apu_power_w: float | None
    # Per-compose-container RSS, biggest first — the breakdown the unified-memory
    # total can't show on its own.
    containers: list[ContainerMem]
    # Raw per-process RSS across all containers (via `docker top`), biggest first
    # — the actual processes behind each container's total, e.g. the local-llm
    # container's separate llama-server per loaded model.
    processes: list[ProcessMem]


# A long argv (a llama-server command line) would bloat the readout; the model
# path that distinguishes processes is near the front, so a generous head is enough.
# The llama-server command line reaches ~200 chars BEFORE its per-model flags begin, so a
# 200-char cap silently hid every catalog `extra_server_args` — --spec-type draft-mtp,
# --image-min-tokens, --mmproj. Reading this field to check which flags were served showed a
# command that looked flagless, which cost real debugging time chasing speculation that was
# in fact enabled. The whole point of surfacing the command is seeing the tail.
_CMD_MAX = 512


@router.get("/host")
async def host(request: Request, settings: SettingsDep, _p: DebugDep) -> HostMetricsOut:
    """Live host memory/swap/disk/load + per-container RSS AND raw per-process RSS,
    proxied from the supervisor (the single owner of docker access + /proc),
    biggest first. Mirrors the owner ops surface; lets the read-only console
    attribute the unified-memory total down to individual processes instead of
    guessing — the per-process list is what tells the 120B from the vision model."""
    request.state.debug_detail = "host metrics"
    client = _supervisor(request)
    headers = {"Authorization": f"Bearer {settings.supervisor_token}"}
    metrics = await client.get("/metrics", headers=headers)
    metrics.raise_for_status()
    data = cast(dict[str, Any], metrics.json())
    data["containers"] = sorted(
        data.get("containers", []), key=lambda c: c["mem_bytes"], reverse=True
    )
    procs_resp = await client.get("/processes", headers=headers)
    procs_resp.raise_for_status()
    procs = cast(dict[str, Any], procs_resp.json()).get("processes", [])
    for p in procs:
        p["command"] = str(p.get("command", ""))[:_CMD_MAX]
    data["processes"] = sorted(procs, key=lambda p: p["rss_bytes"], reverse=True)
    return HostMetricsOut(**data)


# A cold `/disk` runs `docker system df` (which walks every volume) and a `du` of the
# project tree in a helper container, so it can take a minute or more; the supervisor
# caches the result ~60 s, so a retry after a timeout reads the finished build.
_DISK_TIMEOUT = httpx.Timeout(240.0, connect=5.0)


@router.get("/disk")
async def disk(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    refresh: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    """Where the box's disk went, proxied from the supervisor (the only holder of the
    docker socket): filesystem totals, `docker system df` (images with reclaimable bytes,
    container writable layers, volumes, build cache), per-entry sizes of PROJECT_DIR
    one level deeper under the model and backup dirs, and `host_dirs` — the same `du`
    over a fixed allowlist of host folders (/home, /root, /var, ...), top 60 entries
    each, for space neither the project nor docker holds. Passed through as the supervisor
    shapes it — a partial build carries an `errors` list rather than failing.
    `?refresh=1` bypasses the supervisor's ~60 s cache."""
    request.state.debug_detail = "disk usage" + (" (refresh)" if refresh else "")
    resp = await _supervisor(request).get(
        "/disk",
        params={"refresh": "1"} if refresh else {},
        headers={"Authorization": f"Bearer {settings.supervisor_token}"},
        timeout=_DISK_TIMEOUT,
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


class DiskCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: list[Literal["build_cache", "unused_images", "orphan_volumes"]] = Field(min_length=1)
    # Safe by default: forgetting the flag only reports what WOULD be freed.
    dry_run: bool = True


@router.post("/disk/cleanup")
async def disk_cleanup(
    body: DiskCleanupRequest, request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, object]:
    """**Free what `/disk` reports as reclaimable**, proxied to the supervisor's
    `/disk/cleanup`. A dry run (the default) reports per action what it WOULD free and
    removes nothing; `dry_run: false` applies it and reports what was freed.

    The action set is fixed and each is narrow, decided on the supervisor where the
    docker socket is: `build_cache` (`docker builder prune --all`), `unused_images` (no
    container uses them AND nothing the stack could need names their repository — the
    compose file's images and build-arg bases with the box's `.env` overrides, every
    Dockerfile `FROM`, `jbrain2-*`, `jbrain-*`, `alpine`, `docker`; an unknown shared
    size keeps an image; no image at all when any of that cannot be read),
    `orphan_volumes` (only names on an explicit allowlist — `jbrain_llm_kv` — and only
    at ref count 0; never `blobs` or `db_data`). Nothing is forced, so the daemon's own
    in-use refusal still stands.

    Same trust as `POST /update`, which this surface already grants: a token that can
    roll the box to `main` can also drop caches the next build would have reused. It
    cannot name what to remove — only pick from three actions. 409 while an update or
    other one-shot runs (applying only) or while another cleanup runs, and while an
    apply runs the supervisor refuses every one-shot start. An applied cleanup
    invalidates the supervisor's `/disk` cache. An apply can outlast this call's
    timeout; it still finishes on the supervisor, so re-run the dry run to see what is
    left rather than re-applying."""
    request.state.debug_detail = (
        f"disk cleanup {'dry run' if body.dry_run else 'APPLY'}: {', '.join(body.actions)}"
    )
    resp = await _supervisor(request).post(
        "/disk/cleanup",
        json=body.model_dump(),
        headers={"Authorization": f"Bearer {settings.supervisor_token}"},
        timeout=_DISK_TIMEOUT,
    )
    if resp.status_code in (409, 503):
        raise HTTPException(
            status_code=resp.status_code,
            detail=_sidecar_detail(resp, "the supervisor refused the cleanup"),
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


@router.put("/llm/engine-effort/{engine}")
async def put_llm_engine_efforts(
    engine: str,
    body: llm_settings.EngineEffortsPut,
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
) -> LlmSettingsOut:
    """Set/clear per-engine reasoning levels (null clears) — the owner's batch route, same
    validation and one write."""
    # Validated first, so the audit line only ever names known tiers, tasks and levels.
    changes = llm_settings.validate_engine_efforts(engine, body)
    request.state.debug_detail = ", ".join(
        f"{scope}:{key}→{level}" for (_engine, scope, key), level in changes.items()
    )
    return await llm_settings.apply_engine_efforts(
        engine, body, settings, _store(request), _OWNER_CTX, _gateway(request)
    )


@router.post("/llm/local-models/{model_id}/load")
async def load_model(
    model_id: str, request: Request, settings: SettingsDep, _p: DebugDep
) -> LoadedModelsOut:
    request.state.debug_detail = model_id
    return await llm_settings.gateway_load(
        model_id,
        settings,
        _gateway(request),
        # This load used to reach the gateway with nothing having evicted to fit — one of
        # exactly two naked loads on the box, both of them here, on the surface the owner
        # reaches when the box is already in trouble.
        residency=getattr(request.app.state, "residency", None),
        registry=getattr(request.app.state, "agent_registry", None),
        settings_store=_store(request),
        kv_prefix=getattr(request.app.state, "kv_prefix", None),
        ctx=_OWNER_CTX,
    )


@router.post("/llm/local-models/{model_id}/unload")
async def unload_model(
    model_id: str, request: Request, settings: SettingsDep, _p: DebugDep
) -> LoadedModelsOut:
    request.state.debug_detail = model_id
    return await llm_settings.gateway_unload(model_id, settings, _gateway(request))


# --- Launch-flag experiments (remote, no terminal) ---------------------------------
# The owner runs this box remotely and cannot edit a catalog entry or a compose file
# (CLAUDE.md #10). These four endpoints exist so a llama-server LAUNCH FLAG can be tried,
# measured, and reverted entirely over the debug API — the loop that previously needed a
# code change, a release, and an Ops → Update per iteration.


class ExtraArgsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Empty/None clears the override — the recovery path when a flag broke the launch, and
    # the reason this is a settable list rather than a boolean per experiment.
    args: list[str] = Field(default_factory=list)


@router.put("/llm/local-models/{model_id}/extra-args")
async def set_extra_args(
    model_id: str, body: ExtraArgsIn, request: Request, settings: SettingsDep, _p: DebugDep
) -> LlmSettingsOut:
    """Set (or clear) EXTRA llama-server flags for one model, then re-stamp the gateway config
    and unload it so the next request relaunches with them.

    Only flags on `llm_settings.EXTRA_ARG_FLAGS` are accepted. That allowlist is the whole
    safety story: llama-server REFUSES TO START on an unknown flag, so an unrestricted argv
    here would let one call make a model permanently unloadable. Scoped per model, so a bad
    value can only affect the model it was set on, and clearing it is the same call with an
    empty list."""
    request.state.debug_detail = f"{model_id}: {' '.join(body.args) or '(clear)'}"
    return await llm_settings.set_local_extra_args(
        model_id, body.args, settings, _store(request), _OWNER_CTX, _gateway(request)
    )


class ContextWindowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # null clears the override back to the model's catalog default.
    context_window: int | None = None


@router.put("/llm/local-models/{model_id}/context-window")
async def set_context_window(
    model_id: str, body: ContextWindowIn, request: Request, settings: SettingsDep, _p: DebugDep
) -> LlmSettingsOut:
    """Set one model's served context window (llama-server `-c`), the PWA control mirrored here.

    It belongs on this surface because window and KV are the same decision: `--swa-full` doubles
    a model's KV, and halving the window pays for it exactly. Without this an assistant can turn
    the flag on remotely but not the knob that makes it affordable."""
    request.state.debug_detail = f"{model_id}: {body.context_window}"
    return await llm_settings.set_local_context_window_value(
        model_id, body.context_window, settings, _store(request), _OWNER_CTX, _gateway(request)
    )


@router.put("/llm/local-models/{model_id}/parallel-slots")
async def set_parallel_slots(
    model_id: str,
    body: llm_settings.ParallelSlotsIn,
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
) -> LlmSettingsOut:
    """Set one model's served slot count (llama-server `-np`), the PWA control mirrored here.

    The other half of the window knob: `-c` is slots × window on a standard model. Flash-Next
    serves a fixed shared pool, so for it this accepts only a no-op (null or 8) and answers 409
    otherwise, exactly as the owner route does."""
    request.state.debug_detail = f"{model_id}: {body.slots}"
    return await llm_settings.set_local_parallel_slots_value(
        model_id, body.slots, settings, _store(request), _OWNER_CTX, _gateway(request)
    )


@router.get("/llm/local-models/{model_id}/props")
async def model_props(
    model_id: str, request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, object]:
    """llama-server's `/props` for one model: `build_info` (the only build identity available
    over HTTP — and this box rebuilds llama.cpp on master by default, so it changes), the real
    `n_ctx`, and `total_slots`. REFUSES a model that is not already resident — reaching it
    would make the gateway load it outside the residency budget, the path that froze this
    host. Load it first (which evicts to make room), then read its props."""
    request.state.debug_detail = model_id
    windows = await _store(request).llm_local_context_windows(_OWNER_CTX)
    return await llm_settings.gateway_props(model_id, settings, _gateway(request), windows)


@router.get("/llm/local-models/{model_id}/slots")
async def model_slots(
    model_id: str, request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, object]:
    """llama-server's `/slots` for one RESIDENT model — per-slot state, and on a speculative
    build the `speculative` object that says whether drafting is actually running.

    This exists because `/props`'s `speculative.types` CANNOT answer that: the server builds
    it from a `task_params` it never populates, so it reads "none" on every build, and an
    entire investigation here concluded MTP was off from that field. The `--slots` flag the
    config always passes was added precisely so this endpoint would be available; only the
    route was missing."""
    request.state.debug_detail = model_id
    return await llm_settings.gateway_slots(model_id, settings, _gateway(request))


@router.get("/llm/local-models/{model_id}/metrics")
async def model_metrics(
    model_id: str, request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, object]:
    """llama-server's Prometheus `/metrics` for one RESIDENT model, with the speculative
    counters parsed out (`spec`: drafted, accepted, and the derived accept rate).

    The accept rate is the direct measure of whether MTP is earning its keep and whether
    `--spec-draft-n-max` is at the right depth. Without this route it could only be inferred
    from wall-clock timings, which is how the MTP work here spent a long time guessing."""
    request.state.debug_detail = model_id
    return await llm_settings.gateway_metrics(model_id, settings, _gateway(request))


@router.get("/llm/kv-prefix")
async def kv_prefix_state(
    request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, object]:
    """The jerv prompt cache's whole state — counters, per-model file/identity, disk usage,
    recent outcomes, and llama-server's own prompt-reuse counters.

    This is the route that answers "is the KV cache working?". Until it existed the box could
    not say: only the two SUCCESS paths wrote a box event, so a store humming along and a
    store that had not restored anything since boot both produced no rows at all, and the
    feature shipped silently inert twice on exactly that blindness. `counters` is the whole
    record (every outcome since process start); `models[].state` resolves the fingerprint a
    turn would ask for against what is on disk, naming the drifted component when they
    disagree; `reuse` is the server's cumulative cache-hit ratio, the one number that cannot
    be argued with.

    Read-only and load-free: it never admits, loads or evicts, so it is safe to poll."""
    return await llm_settings.kv_prefix_state(
        settings,
        _gateway(request),
        kv_prefix=getattr(request.app.state, "kv_prefix", None),
        registry=getattr(request.app.state, "agent_registry", None),
        settings_store=_store(request),
    )


@router.delete("/llm/kv-prefix")
async def kv_prefix_clear(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    model: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    """Delete the prompt cache's slot files — all of them, or one catalog model's (`model`).

    The no-terminal twin of `rm -rf .kvslots` (CLAUDE.md #10), for the same reason
    `drop-page-cache` exists: reclaiming this space needed host shell, which the owner running
    this box remotely does not have. Measured at 94% of the budget with nothing able to act.

    Safe while models are resident: this removes files on disk, never a live slot, so a
    conversation in flight keeps its KV. The cost of a wrong call is bounded at one re-prefill
    per deleted identity — the behaviour without this store at all — and the next prime writes
    the file back. Reach for `GET /llm/kv-prefix` first: `store.by_file` says what is there."""
    request.state.debug_detail = f"clear kv prefix ({model or 'all'})"
    return await llm_settings.kv_prefix_clear(
        settings, kv_prefix=getattr(request.app.state, "kv_prefix", None), model_id=model
    )


@router.put("/llm/auto-restore")
async def set_auto_restore(
    request: Request,
    _p: DebugDep,
    enabled: Annotated[bool, Query()],
) -> dict[str, object]:
    """Turn the end-of-turn restore on or off — the WarmKeeper's whole reason to exist.

    OFF, the keeper keeps nothing warm and the disk store carries the entire mechanism: a
    lost prefix waits for the next turn to notice it, which is the turn that then pays for
    the restore. ON, the box puts an evicted model back once a turn ends and the keeper
    re-primes it off-turn, so the owner's next message meets a warm slot.

    It already had an owner route (`PUT /api/settings/llm/auto-restore`), reachable only with
    an owner cookie — so an assistant holding a debug token could READ the flag on
    `GET /api/debug/llm`, measure exactly what it costs, and then not be able to act on the
    measurement. This is that gap closed; the two share one implementation.

    A SURPRISE control, not a safety one: every load, restore included, still goes through the
    device-memory guard. Applies to the next turn, with no restart."""
    request.state.debug_detail = f"auto restore {'on' if enabled else 'off'}"
    return await llm_settings.set_auto_restore_value(_store(request), _OWNER_CTX, enabled=enabled)


@router.put("/llm/kv-prefix/budget")
async def kv_prefix_budget(
    request: Request,
    _p: DebugDep,
    gb: Annotated[int, Query()],
) -> dict[str, object]:
    """Set the prompt cache's disk allowance in GiB (2..500, default 40) — role prefixes and
    conversation files share it, conversation files evicted first.

    It was a module constant whose own comment conceded the gap — "changing it is a release,
    there is no knob" — which on a box with no terminal meant no path at all. Stored and
    applied live; the owner's twin is the Settings field (`PUT /api/settings`)."""
    request.state.debug_detail = f"kv prefix budget {gb} GiB"
    return await llm_settings.set_kv_prefix_budget(
        _store(request),
        _OWNER_CTX,
        gb=gb,
        kv_prefix=getattr(request.app.state, "kv_prefix", None),
    )


@router.put("/llm/kv-prefix/conversations")
async def kv_prefix_conversations(
    request: Request,
    _p: DebugDep,
    enabled: Annotated[bool, Query()],
) -> dict[str, object]:
    """Turn the conversation cache on or off (FLASH_NEXT_ENGINE_PLAN F4c): the Flash-Next
    interactive slot saving each chat conversation as it moves on and restoring it when that
    conversation speaks again. The owner's twin is the Ops switch (`PUT /api/settings`).
    Applied live; off also deletes every saved conversation file."""
    request.state.debug_detail = f"kv conversation cache {'on' if enabled else 'off'}"
    return await llm_settings.set_kv_conversation_cache(
        _store(request),
        _OWNER_CTX,
        enabled=enabled,
        kv_prefix=getattr(request.app.state, "kv_prefix", None),
    )


@router.post("/llm/local-models/{model_id}/prime")
async def prime_model(
    model_id: str, request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, object]:
    """Run the REAL jerv prime against one model and time it — the instrument for every
    prefill experiment. Returns `elapsed_ms`, `input_tokens` and the tool count, so a cold
    prefill and a post-restore prefill are comparable numbers rather than a stopwatch guess.

    It primes through the same path `WarmKeeper` uses, so what it measures is what a real turn
    would pay, not a hand-built approximation that would drift from it."""
    request.state.debug_detail = model_id
    return await llm_settings.gateway_prime(
        model_id,
        settings,
        _gateway(request),
        residency=getattr(request.app.state, "residency", None),
        registry=getattr(request.app.state, "agent_registry", None),
        settings_store=_store(request),
        kv_prefix=getattr(request.app.state, "kv_prefix", None),
        ctx=_OWNER_CTX,
    )


# --- Local engine: Standard or Flash-Next (FLASH_NEXT_ENGINE_PLAN F3a) -------------------
# Thin wrappers over the owner switch (jbrain.api.engine), so the token reaches the SAME
# orchestration the PWA does — drain, one-engine stop/start, smoke test, auto-rollback, box
# event — and there is no second switching path to drift. The perplexity job below keeps its
# own small helpers: it is a supervisor one-shot, not a switch.


def _sup_headers(settings: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.supervisor_token}"}


async def _perplexity_running(request: Request, settings: Any) -> bool:
    resp = await _supervisor(request).get(
        "/perplexity/status", params={"tail": 1}, headers=_sup_headers(settings)
    )
    # A supervisor that predates the job has never run one.
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json()).get("state") == "running"


async def _oneshot_in_flight(request: Request, settings: Any) -> str | None:
    """The supervisor one-shot in flight ("update", "refresh", "perplexity", …), or None.
    An update or refresh recreates containers and a perplexity run holds the box, so an
    engine must not be started or stopped underneath any of them."""
    resp = await _supervisor(request).get("/oneshot", headers=_sup_headers(settings))
    if resp.status_code == 404:
        # A supervisor that predates the route: the perplexity read is the most it can say.
        return "perplexity" if await _perplexity_running(request, settings) else None
    resp.raise_for_status()
    running = cast(dict[str, Any], resp.json()).get("running")
    return str(running) if running else None


async def _unload_resident(request: Request, why: str) -> list[str]:
    """Unload every model the running gateway holds, through the client's own unload — the
    one chokepoint that discharges the reservation ledger and narrates to box events. A
    container stop would free the memory too, but leave the ledger charging for models that
    no longer exist. An unreachable gateway reports nothing resident, so there is nothing
    to release and nothing blocks."""
    gateway = _gateway(request)
    if gateway is None:
        return []
    released: list[str] = []
    try:
        with box_events.because(why):
            for served in sorted(await gateway.running()):
                await gateway.unload(served)
                released.append(served)
    except LocalGatewayError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"could not unload every resident model before {why} "
            f"(released: {', '.join(released) or 'none'}): {exc}. Nothing was stopped.",
        ) from exc
    return released


@router.get("/llm/engine")
async def read_engine(request: Request, _p: DebugDep) -> engine_api.EngineStateOut:
    """`GET /api/settings/llm/engine` for the token: desired + effective engine, both
    services' state, installed, one-shot, memory, admission, the nightly guard and the
    in-flight or last switch. 502 if the supervisor cannot be read."""
    request.state.debug_detail = "engine state"
    return await engine_api.engine_state(request, _OWNER_CTX)


@router.post("/llm/engine", status_code=202)
async def switch_engine(
    body: engine_api.EngineIn, request: Request, _p: DebugDep
) -> engine_api.SwitchStatusOut:
    """`POST /api/settings/llm/engine` for the token — the same orchestration, recorded with
    `source: "debug"`. 202 with the status; poll `GET /llm/engine` until `switch.stage` is
    `done`, `rolled_back` or `failed`."""
    request.state.debug_detail = f"engine → {body.engine}"
    return await engine_api.begin_switch(request, body, source="debug", ctx=_OWNER_CTX)


@router.post("/llm/engine/cancel", status_code=202)
async def cancel_engine_switch(request: Request, _p: DebugDep) -> engine_api.SwitchStatusOut:
    """`POST /api/settings/llm/engine/cancel` for the token: cancel a switch that is still
    draining (nothing stopped yet); 409 at any other stage."""
    request.state.debug_detail = "engine switch cancel"
    return engine_api.cancel_switch(request)


# --- Slot save/restore probe (F4's first check) ------------------------------------------
# The flash-next config renders --slot-save-path (the pool needs it for slot erase too), and
# from F4 the image builds the checkpoint-sidecar patch in, so a restore carries the slot's
# context checkpoints. `sidecar` says whether the save actually wrote one — the on-disk proof
# that the running build is the patched one — and `passed` is the F4 gate.
# Whether a saved-then-restored slot computes the SAME next token distribution as the slot
# it was saved from. Greedy token equality is too coarse (it can differ legitimately and
# agree by luck), so this returns the top-n log-probabilities side by side and the largest
# difference between them. Talks to llama-server directly through llama-swap's
# `/upstream/<model>/…` passthrough — only after checking the model is resident, because
# reaching that passthrough on a cold model makes llama-swap load it outside the budget.

# A fixed file name: each probe overwrites the last, so repeated runs never accumulate
# multi-GiB files in the `.kvslots` tree (the kv_prefix budget would evict them, but only
# after they had displaced real prefixes).
_SLOT_PROBE_FILE = "debug-slot-probe.bin"
# Long prompts prefill for minutes on this box; the read waits that long.
_SLOT_PROBE_TIMEOUT_S = 900.0
# Tests swap in an httpx.MockTransport here; production uses the default network stack.
_UPSTREAM_TRANSPORT: httpx.AsyncBaseTransport | None = None
# ~12 tokens a line on the tokenizers this box serves; numbered so no two lines are equal
# (a repeated line would let a cache reuse mask a restore that lost state).
_SYNTH_LINE = "Line {i}: the ledger records {i} quiet observations about river stones.\n"


class SlotProbeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Exactly one of these: the prompt itself, or roughly how many tokens to synthesize.
    prompt: str | None = Field(default=None, min_length=1, max_length=2_000_000)
    synth_tokens: int | None = Field(default=None, ge=16, le=262_144)
    # Served model name; omitted = the one model resident on the active engine.
    model: str | None = None
    # Slot ids; omitted = the last two slots, and then only on a model serving at least
    # three, so slot 0 is never picked by default. The probe OVERWRITES both slots' caches.
    slot_a: int | None = Field(default=None, ge=0, le=63)
    slot_b: int | None = Field(default=None, ge=0, le=63)
    n_probs: int = Field(default=10, ge=1, le=100)
    # The largest |probability difference| over shared top-n candidates that still counts as
    # the same distribution. In probability, not logprob: measured on Flash-Next (2026-10-04),
    # re-reading the SAME slot with no disk involved moves deep-tail candidates (p < 0.1%) by
    # up to ~1.3 nats while no probability moves by more than 0.0003, so a nats bound fails
    # every restore on noise. A restore that lost state moves the likely tokens, by far more.
    tolerance: float = Field(default=0.01, gt=0, le=1.0)


class SlotProbeRead(BaseModel):
    slot: int
    # Top-n next-token candidates: {"id", "token", "logprob"}, most likely first.
    top: list[dict[str, Any]]
    # llama-server's `timings` (prompt_n is how many tokens it actually evaluated — the
    # number that shows whether a restored slot re-prefilled) and `tokens_cached`.
    timings: dict[str, Any]
    tokens_cached: int | None


class SlotProbeDiff(BaseModel):
    # Largest |logprob difference| over the token ids both reads put in their top-n —
    # reported, not judged: it is dominated by deep-tail noise.
    max_abs_diff: float | None
    # Largest |probability difference| over the same candidates — what `tolerance` bounds.
    max_prob_diff: float | None = None
    shared: int
    top1_equal: bool


class SlotProbeOut(BaseModel):
    engine: llm_engine.Engine
    model: str
    slot_a: int
    slot_b: int
    prompt_chars: int
    cold: SlotProbeRead
    warm: SlotProbeRead
    restored: SlotProbeRead
    restored_vs_cold: SlotProbeDiff
    restored_vs_warm: SlotProbeDiff
    # False when the restored read re-evaluated the whole prompt (prompt_n ≥ the cold
    # read's) — the restore loaded state the server then threw away, which is what a
    # hybrid without its context checkpoints does. None when timings are missing.
    restore_effective: bool | None
    # The verdict F4 gates on: the restore was effective, both comparisons share most of
    # their top-n, the top token agrees, and every shared probability is within `tolerance`.
    tolerance: float
    within_tolerance: bool | None
    passed: bool
    # Whether the save wrote its checkpoint sidecar (`<file>.ckpt`) — the patched engine's
    # signature. None when this process cannot see the save directory.
    sidecar: bool | None
    # The disk prefix cache's restore gate this run recorded for a patch-gated model
    # (Flash-Next): `passed` only when `passed` AND `sidecar` held, keyed by the running
    # launch line and llama.cpp build; `failed` otherwise. None when nothing was recorded (a
    # model without the gate, or a save directory this process cannot see).
    restore_gate: str | None = None
    n_saved: int | None
    n_restored: int | None
    file_bytes: int | None
    save_ms: float | None
    restore_ms: float | None


def _synth_prompt(tokens: int) -> str:
    return "".join(_SYNTH_LINE.format(i=i) for i in range(max(1, tokens // 12)))


def _top_logprobs(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The first generated token's candidates, in both shapes llama-server has shipped:
    `top_logprobs` with log-probabilities (current), or `probs` with probabilities (older),
    normalised to log space so the two compare."""
    probs = body.get("completion_probabilities") or []
    if not probs or not isinstance(probs[0], dict):
        return []
    first = probs[0]
    if isinstance(first.get("top_logprobs"), list):
        return [
            {"id": c.get("id"), "token": c.get("token"), "logprob": float(c["logprob"])}
            for c in first["top_logprobs"]
            if isinstance(c, dict) and "logprob" in c
        ]
    out: list[dict[str, Any]] = []
    for c in first.get("probs") or []:
        if isinstance(c, dict) and c.get("prob", 0) > 0:
            out.append(
                {"id": c.get("id"), "token": c.get("tok_str"), "logprob": math.log(c["prob"])}
            )
    return out


def _diff(a: SlotProbeRead, b: SlotProbeRead) -> SlotProbeDiff:
    def key(c: dict[str, Any]) -> Any:
        return c["id"] if c.get("id") is not None else c.get("token")

    left = {key(c): c["logprob"] for c in a.top}
    right = {key(c): c["logprob"] for c in b.top}
    shared = left.keys() & right.keys()
    return SlotProbeDiff(
        max_abs_diff=max((abs(left[k] - right[k]) for k in shared), default=None),
        max_prob_diff=max(
            (abs(math.exp(left[k]) - math.exp(right[k])) for k in shared), default=None
        ),
        shared=len(shared),
        top1_equal=bool(a.top and b.top and key(a.top[0]) == key(b.top[0])),
    )


def _restore_effective(cold: SlotProbeRead, restored: SlotProbeRead) -> bool | None:
    full = cold.timings.get("prompt_n")
    again = restored.timings.get("prompt_n")
    if not isinstance(full, int | float) or not isinstance(again, int | float):
        return None
    return again < full


def _within(diff: SlotProbeDiff, tolerance: float, n_probs: int) -> bool | None:
    """Whether one comparison is the same distribution within `tolerance`: the top token
    agrees, at least half the top-n is shared (two reads that share one candidate prove
    nothing), and no shared candidate's probability moved further. None when nothing was
    comparable."""
    if diff.max_prob_diff is None:
        return None
    return (
        diff.top1_equal
        and diff.shared >= max(1, (n_probs + 1) // 2)
        and diff.max_prob_diff <= tolerance
    )


def _probe_sidecar_path(models_dir: str, served: str) -> Path | None:
    """Where the probe save's checkpoint sidecar lands, on the models volume the kv-prefix
    store also reads, or None when this process cannot see that folder."""
    model_id = local_catalog.id_for_served(served)
    if model_id is None:
        return None
    folder = Path(models_dir) / llama_swap_config.KVSLOT_DIR / model_id
    return folder / f"{_SLOT_PROBE_FILE}.ckpt" if folder.is_dir() else None


async def _record_restore_gate(
    request: Request,
    models_dir: str,
    gateway: LocalGatewayClient,
    engine: llm_engine.Engine,
    served: str,
    *,
    passed: bool,
    detail: dict[str, Any],
) -> str | None:
    """Write this run's verdict as the disk prefix cache's restore gate (kv_prefix: restores
    on a patch-gated model wait for a passing probe against the server running now). Keyed by
    the launch line and `/props` build_info, so a new image or launch line needs a new run."""
    model = local_catalog.get_by_served(served)
    if model is None or not model.kv_restore_needs_patch:
        return None
    line = llama_swap_config.launch_line(models_dir, served, engine)
    save_dir = None if line is None else kv_prefix.save_dir_for(line, models_dir)
    if line is None or save_dir is None:
        return None
    try:
        build = str((await gateway.props(served)).get("build_info") or "")
    except LocalGatewayError:
        return None
    verdict = "passed" if passed else "failed"
    record = {
        "fingerprint": kv_prefix.gate_fingerprint(line, build),
        "verdict": verdict,
        "model": served,
        "build_info": build,
        "at": dt.datetime.now(dt.UTC).isoformat(),
        **detail,
    }
    if not await asyncio.to_thread(kv_prefix.write_gate_verdict, save_dir, record):
        return None
    store = getattr(request.app.state, "kv_prefix", None)
    if store is not None:
        store.forget_gate(served)
    return verdict


def _needs_save_path(resp: httpx.Response) -> bool:
    # llama-server answers every slot action with 501 (ERROR_TYPE_NOT_SUPPORTED) when it was
    # started without --slot-save-path; the message names the flag on every build seen.
    return resp.status_code == 501 or "slot-save-path" in resp.text


@router.post("/llm/slot-probe")
async def slot_probe(
    body: SlotProbeIn, request: Request, settings: SettingsDep, _p: DebugDep
) -> SlotProbeOut:
    """**Slot save/restore probe** (FLASH_NEXT_ENGINE_PLAN F4's first check, moved out of F2):
    does a restored slot compute what the saved one did?

    On the active engine's resident model: erase slots A and B, prime A with the prompt
    (`n_predict: 1` — its next-token candidates are the COLD read), save A to a fixed file,
    restore that file into B, then ask A (WARM, a cache hit) and B (RESTORED) for the same
    next token with identical settings (`temperature 0`, `n_probs`). Returns the three top-n
    log-probability lists side by side, the largest difference over shared candidates,
    `n_saved` / `n_restored`, the file size (`n_written`), and each step's timings —
    `restored.timings.prompt_n` is how many tokens B re-evaluated, which is how a restore that
    lost its context checkpoints shows up (a hybrid re-prefills from zero).

    Not a byte-equal ubatch comparison: the cold read prefills in whatever ubatch split the
    server chooses for the whole prompt, so expect small differences, not zero. `tolerance`
    (default 0.01, in probability) bounds them: `within_tolerance` holds when, against both
    the cold and the warm read, the top token agrees, at least half the top-n is shared and no
    shared candidate's probability differs by more; `passed` adds `restore_effective` — F4's
    gate. `sidecar` says whether the save wrote the checkpoint sidecar, i.e. whether the
    patched engine is running.
    Any slot pair may be named, slot 0 included (Flash-Next's slots are role-pinned: name ones
    whose prefix you can afford to lose — 6 and 7 by default).

    **Overwrites both slots' caches** — pick slots no live workload is pinned to (on
    Flash-Next, not slot 0's persona). 409 when no model is resident (this never loads one),
    when the model serves fewer than two slots, or when the server runs without
    `--slot-save-path` (llama-server refuses every slot action then). 400 for a bad slot pair
    or neither/both of `prompt` and `synth_tokens`."""
    if (body.prompt is None) == (body.synth_tokens is None):
        raise HTTPException(status_code=400, detail="give exactly one of prompt, synth_tokens")
    gateway = _gateway(request)
    if gateway is None:
        raise HTTPException(status_code=409, detail="local hosting is off on this box")
    engine = await _active_engine(request)
    resident = sorted(await gateway.running())
    if body.model is not None:
        if body.model not in resident:
            raise HTTPException(
                status_code=409,
                detail=f"{body.model} is not resident; load it first (this probe never loads)",
            )
        served = body.model
    elif len(resident) == 1:
        served = resident[0]
    elif not resident:
        raise HTTPException(
            status_code=409, detail="no model is resident; load one first (this never loads)"
        )
    else:
        raise HTTPException(
            status_code=400, detail=f"several models resident ({', '.join(resident)}); name one"
        )
    request.state.debug_detail = f"slot probe {served} ({engine})"
    try:
        n_slots = len(await gateway.slots(served))
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"could not read /slots: {exc}") from exc
    named = body.slot_a is not None and body.slot_b is not None
    if n_slots < 2 or (n_slots < 3 and not named):
        raise HTTPException(
            status_code=409,
            detail=f"{served} serves {n_slots} slot(s); the probe needs two, and picks them "
            "itself only when there are three or more (it never defaults to slot 0, the "
            "persona slot) — name slot_a and slot_b to use slot 0 deliberately",
        )
    slot_a = body.slot_a if body.slot_a is not None else n_slots - 2
    slot_b = body.slot_b if body.slot_b is not None else n_slots - 1
    if slot_a == slot_b or max(slot_a, slot_b) >= n_slots:
        raise HTTPException(
            status_code=400,
            detail=f"slots must be two different ids below {n_slots} (got {slot_a}, {slot_b})",
        )
    prompt = body.prompt if body.prompt is not None else _synth_prompt(body.synth_tokens or 0)
    base = f"{settings.local_llm_url.rstrip('/').removesuffix('/v1')}/upstream/{served}"

    async with httpx.AsyncClient(
        timeout=_SLOT_PROBE_TIMEOUT_S, transport=_UPSTREAM_TRANSPORT
    ) as client:

        async def action(slot: int, verb: str, payload: dict[str, Any]) -> dict[str, Any]:
            resp = await client.post(f"{base}/slots/{slot}?action={verb}", json=payload)
            if resp.status_code >= 400:
                if _needs_save_path(resp):
                    raise HTTPException(
                        status_code=409,
                        detail=f"{served} runs without --slot-save-path, so llama-server "
                        "refuses slot save/restore; nothing was primed",
                    )
                raise HTTPException(
                    status_code=502,
                    detail=f"slot {verb} on {slot}: HTTP {resp.status_code} {resp.text[:300]}",
                )
            return cast(dict[str, Any], resp.json())

        async def read(slot: int) -> SlotProbeRead:
            resp = await client.post(
                f"{base}/completion",
                json={
                    "prompt": prompt,
                    "n_predict": 1,
                    "id_slot": slot,
                    "cache_prompt": True,
                    "n_probs": body.n_probs,
                    "temperature": 0,
                },
            )
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"completion on slot {slot}: HTTP {resp.status_code} {resp.text[:300]}",
                )
            got = cast(dict[str, Any], resp.json())
            return SlotProbeRead(
                slot=slot,
                top=_top_logprobs(got),
                timings=cast(dict[str, Any], got.get("timings") or {}),
                tokens_cached=got.get("tokens_cached"),
            )

        sidecar_path = _probe_sidecar_path(settings.local_models_dir, served)
        if sidecar_path is not None:
            # A previous run's sidecar would otherwise vouch for a build that writes none.
            with contextlib.suppress(OSError):
                sidecar_path.unlink(missing_ok=True)
        try:
            await action(slot_a, "erase", {})
            await action(slot_b, "erase", {})
            cold = await read(slot_a)
            saved = await action(slot_a, "save", {"filename": _SLOT_PROBE_FILE})
            restored_meta = await action(slot_b, "restore", {"filename": _SLOT_PROBE_FILE})
            warm = await read(slot_a)
            restored = await read(slot_b)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream: {exc}") from exc

    def num(meta: dict[str, Any], *path: str) -> Any:
        cur: Any = meta
        for part in path:
            cur = cur.get(part) if isinstance(cur, dict) else None
        return cur

    vs_cold = _diff(restored, cold)
    vs_warm = _diff(restored, warm)
    checks = (
        _within(vs_cold, body.tolerance, body.n_probs),
        _within(vs_warm, body.tolerance, body.n_probs),
    )
    within = None if None in checks else all(checks)
    effective = _restore_effective(cold, restored)
    passed = bool(effective) and within is True
    sidecar = None if sidecar_path is None else sidecar_path.exists()
    gate = await _record_restore_gate(
        request,
        settings.local_models_dir,
        gateway,
        engine,
        served,
        passed=passed and sidecar is True,
        detail={
            "within_tolerance": within,
            "restore_effective": effective,
            "sidecar": sidecar,
            "tolerance": body.tolerance,
            "max_abs_diff_vs_cold": vs_cold.max_abs_diff,
            "max_prob_diff_vs_cold": vs_cold.max_prob_diff,
            "slots": [slot_a, slot_b],
        },
    )
    return SlotProbeOut(
        engine=engine,
        model=served,
        slot_a=slot_a,
        slot_b=slot_b,
        prompt_chars=len(prompt),
        cold=cold,
        warm=warm,
        restored=restored,
        restored_vs_cold=vs_cold,
        restored_vs_warm=vs_warm,
        restore_effective=effective,
        tolerance=body.tolerance,
        within_tolerance=within,
        passed=passed,
        sidecar=sidecar,
        restore_gate=gate,
        n_saved=num(saved, "n_saved"),
        n_restored=num(restored_meta, "n_restored"),
        file_bytes=num(saved, "n_written"),
        save_ms=num(saved, "timings", "save_ms"),
        restore_ms=num(restored_meta, "timings", "restore_ms"),
    )


# --- Perplexity one-shot (F2 check 6) ------------------------------------------------------

_PPL_RE = re.compile(r"Final estimate: PPL = ([0-9.]+) \+/- ([0-9.]+)")


class PerplexityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 512-token chunks of WikiText-2; omitted = the supervisor's bounded default (100),
    # never the whole split. The supervisor enforces the same 1..200 bound.
    chunks: int | None = Field(default=None, ge=1, le=200)


def _flash_next_model_path(models_dir: str) -> str:
    """`/models/<id>/<first shard>` for the catalog's Flash-Next entry, resolved the way the
    gateway config resolves `-m` — so the run reads exactly the file serving would, and no
    shard name is hardcoded anywhere."""
    entries = [
        m for m in local_catalog.CATALOG if llm_engine.parse(m.engine) == llm_engine.FLASH_NEXT
    ]
    if not entries:
        raise HTTPException(status_code=409, detail="this build's catalog has no Flash-Next model")
    model = entries[0]
    try:
        rel = llama_swap_config.resolve_weight(models_dir, model.id, model.gguf_include)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"{model.id} weights are not on the box ({exc}); install them in the PWA",
        ) from exc
    return f"/models/{model.id}/{rel}"


@router.post("/llm/perplexity", status_code=202)
async def start_perplexity(
    body: PerplexityIn, request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, object]:
    """**WikiText-2 perplexity on Flash-Next** (FLASH_NEXT_ENGINE_PLAN F2, check 6) — the
    check that catches a bad conversion (the hyper-connection norm bug) against the #27742
    reference, without a shell.

    A FIXED supervisor job: `docker compose run --rm --no-deps flash-next llama-perplexity
    -m <catalog weights> -f /opt/jbrain/eval/wiki.test.raw -ngl 999 -ot
    per_layer_token_embd=CPU -c 512 --chunks N`. The only thing a caller chooses is the
    chunk count (1..200, default 100); the model path is resolved HERE from the catalog and
    validated again by the supervisor. There is no args field, because an argv is an exec.

    It STOPS the running engine for the duration and restarts that same one afterwards
    (the job's own trap, so it happens even when the run fails): this run loads the model
    a second time, and only a stopped gateway cannot have something loaded into it mid-run.
    Resident models are unloaded through the gateway first so the ledger is discharged.
    Known gap, accepted for a debug route: between that unload and the job's container
    stop, the warm keeper or a request can load a model again; the stop then frees it
    without the client's unload, so the ledger keeps a stale charge until its TTL sweep
    (no memory is at risk — the container is down). Local calls fail while it runs. The
    model container's output is in `/llm/perplexity/status`; `/logs/flash-next` shows the
    idle service container, not the run. Poll the status route.

    409 when the weights are absent, the flash-next container was never provisioned, or
    another one-shot (update, refresh, a previous run) is running."""
    engine_api.refuse_while_switching(request, "run perplexity")
    model_path = _flash_next_model_path(settings.local_models_dir)
    request.state.debug_detail = f"perplexity {model_path} (chunks {body.chunks or 'default'})"
    # Checked BEFORE unloading: refusing after would have evicted the owner's models for a
    # run that never starts.
    busy = await _oneshot_in_flight(request, settings)
    if busy is not None:
        raise HTTPException(status_code=409, detail=f"a supervisor one-shot ({busy}) is running")
    released = await _unload_resident(request, "a perplexity run needs the box")
    resp = await _supervisor(request).post(
        "/perplexity",
        json={"model_path": model_path, "chunks": body.chunks},
        headers=_sup_headers(settings),
    )
    if resp.status_code == 404:
        raise HTTPException(
            status_code=409,
            detail="the flash-next service is not provisioned; Ops → Update creates it",
        )
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="another one-shot is running")
    if resp.status_code == 400:
        raise HTTPException(
            status_code=409,
            detail=f"the supervisor refused the resolved path {model_path!r}",
        )
    resp.raise_for_status()
    return {**cast(dict[str, object], resp.json()), "model_path": model_path, "unloaded": released}


@router.get("/llm/perplexity/status")
async def perplexity_status(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    tail: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, object]:
    """The perplexity one-shot's state + log tail, with `ppl` / `ppl_error` parsed from
    llama-perplexity's `Final estimate` line once it has printed one (null until then)."""
    request.state.debug_detail = f"perplexity (tail {tail})"
    resp = await _supervisor(request).get(
        "/perplexity/status", params={"tail": tail}, headers=_sup_headers(settings)
    )
    resp.raise_for_status()
    data = cast(dict[str, object], resp.json())
    found = _PPL_RE.search(str(data.get("log_tail", "")))
    return {
        **data,
        "ppl": float(found.group(1)) if found else None,
        "ppl_error": float(found.group(2)) if found else None,
    }


# --- What the panels heard, and asking them to say so now -------------------


class HeardEntry(BaseModel):
    """One entry of a panel's decode ring, named rather than positional.

    The panel sends a tuple to keep the body small; nobody reading a diagnosis should have to
    remember that field four is the repeat count.
    """

    phrase: str
    prob: int
    fired: bool
    count: int = 1
    # The decoder's own phoneme string. Empty from a panel older than 0.3.10, which is not the
    # same as a decode that produced none — the field simply did not exist yet.
    raw: str = ""


class PanelHeard(BaseModel):
    label: str
    version: str
    reported_at: str
    # Newest first, exactly as the panel keeps it.
    heard: list[HeardEntry]


class PanelHeardOut(BaseModel):
    panels: list[PanelHeard]
    # What the box would have to raise for a fresh ring; echoed so a caller can tell a stale
    # answer from a current one without a second call.
    telemetry_seq: int


def _heard_entry(raw: object) -> HeardEntry | None:
    """One ring entry from the panel's positional tuple, across all three arities it has had.

    Three fields until 0.3.09, four with the repeat count, five with the raw phoneme string —
    and a fleet upgrades one panel at a time, so all three can be in the same answer.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    return HeardEntry(
        phrase=str(raw[0]),
        prob=int(raw[1]),
        fired=bool(raw[2]),
        count=int(raw[3]) if len(raw) > 3 else 1,
        raw=str(raw[4]) if len(raw) > 4 else "",
    )


@router.get("/endpoint/heard")
async def panel_heard(request: Request, _p: DebugDep) -> PanelHeardOut:
    """WHAT EACH PANEL LAST HEARD — the decode ring, from here, with no cable.

    This existed only as a `jsonb` column reachable by hand-written SQL, which meant the one
    surface for the question "is she being heard?" required knowing the shape of a positional
    tuple. `POST /endpoint/report-now` is the other half: raise the counter, wait a poll, read
    this.

    THE RING IS A SNAPSHOT AND THE LOG IS THE HISTORY. `app.endpoint_status` holds one
    upserted row per panel, so this answers "what did it hear most recently", not "what has it
    ever heard" — the telemetry log line carries every report and is where a question spanning
    more than one cycle belongs.
    """
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT pr.label, s.version, s.reported_at, s.report->'heard'
                    FROM app.endpoint_status s
                    JOIN app.principals pr ON pr.id = s.principal_id
                    ORDER BY s.reported_at DESC
                    """
                )
            )
        ).all()
        seq = (
            await session.execute(
                text("SELECT telemetry_seq FROM app.endpoint_settings WHERE id = 1")
            )
        ).scalar_one_or_none()
    panels = [
        PanelHeard(
            label=str(row[0]),
            version=str(row[1] or ""),
            reported_at=row[2].isoformat() if row[2] is not None else "",
            heard=[e for e in (_heard_entry(x) for x in (row[3] or [])) if e is not None],
        )
        for row in rows
    ]
    log.info("debug.panel_heard", panels=len(panels))
    return PanelHeardOut(panels=panels, telemetry_seq=int(seq or 0))


class PanelPath(BaseModel):
    """One of the panel's three ways of reaching this box."""

    name: str
    # The panel's own last reason — `connect`, `http-401`, an `esp_err_to_name` string. Empty
    # when this path has never failed, which `fails == 0` says independently.
    err: str = ""
    fails: int = 0
    # Seconds between that failure and the report carrying it. Read with `stale_s` below: a
    # small `ago_s` on a report that is itself an hour old describes an hour-old moment.
    ago_s: int = 0


#: The panel plays messages as 16-bit mono at 16 kHz — 32 bytes to the millisecond.
_MSG_BYTES_PER_MS = 32


class PanelMessage(BaseModel):
    """The last message this panel streamed, and whether it sounded for as long as it should."""

    bytes: int
    # How long the ring actually sounded.
    ms: int
    # How long that many bytes SHOULD have taken.
    expected_ms: int
    ok: int
    bad: int
    err: str = ""
    # How long the longest fetch waited for the speaker before it could start. Non-zero means this
    # panel raced its own press cue and the wait saved the message.
    waited_ms: int = 0
    # THE ANSWER, NOT THE INPUTS. `false` means the box served a message, the panel verified its
    # digest and acknowledged it played, and the speaker was quiet for nearly all of it — which is
    # indistinguishable, in every server-side record, from a message that played perfectly.
    #
    # A fifth of the expected time is the line: real playback overshoots slightly (the ring is
    # observed after it drains, not as the last sample leaves), and the failure this catches is
    # not a near miss — it is forty milliseconds against three thousand.
    heard: bool


class PanelReach(BaseModel):
    label: str
    version: str
    reported_at: str
    # HOW OLD THIS ANSWER IS, and on this route it is the headline rather than a footnote.
    #
    # Everything below arrived BY telemetry, which is a POST to this box — so a panel that cannot
    # reach us cannot tell us that, and its row goes stale instead of going red. On 2026-09-29 the
    # panel's last report was healthy and 90 minutes old while the panel sat silent; the report
    # said nothing was wrong because it predated everything that was.
    #
    # So a large `stale_s` IS the finding, and the fields under it describe the last moment the
    # panel could still speak, not the present.
    stale_s: int
    # The panel's own count of seconds since it last reached this box by any path, as of that
    # report. -1 means it had never reached us at all.
    box_quiet_s: int
    paths: list[PanelPath]
    # EVERY RED DASH THIS PANEL HAS DRAWN, and what caused the last one.
    #
    # The dash is the one failure a child actually SEES, and it is counted separately from the
    # paths above because one of its two causes is not a path failure at all. `dash_err` is:
    #   `net`     — the request failed, and the `talk` row above holds the reason.
    #   `timeout` — nothing failed. The answer just did not come back inside the panel's patience,
    #               so there is no row anywhere else, and the request may yet succeed. This is the
    #               case that used to leave no trace at all.
    dashes: int = 0
    dash_err: str = ""
    dash_ago_s: int = 0
    # WAS THE LAST MESSAGE ACTUALLY HEARD — computed here rather than left as two numbers,
    # because the finding is the RATIO and nobody reads a ratio off a page by dividing.
    #
    # The panel's ring is 16-bit mono at 16 kHz, so 32 bytes to the millisecond: the bytes say how
    # long the message SHOULD have taken and `msg_ms` says how long it did. `null` when the panel
    # has not streamed a message since boot, or is too old to say.
    last_message: PanelMessage | None = None
    # THE PANEL'S SINGLE SOCKET, from both ends. `socket` is the box's live view
    # (`panel_ws.snapshot()`): null means this panel holds no socket to this process right now —
    # either it is on HTTP fallback, reconnecting, or gone; `stale_s` says which is likelier.
    # The rest is what the panel last said about it: `ws` is "ws" / "http" / "down" (or "" from
    # firmware that predates the socket), `ws_err` the last REAL reason a connection failed.
    socket: dict[str, Any] | None = None
    ws: str = ""
    ws_err: str = ""
    ws_connects: int = 0
    ws_drops: int = 0
    # The internal-heap low-water mark since boot and the largest internal 8-bit block — the
    # pair that says whether a TLS session still fits.
    int_free: int = -1
    int_min: int = -1
    int_big: int = -1


class PanelReachOut(BaseModel):
    panels: list[PanelReach]
    # Echoed for the same reason `/endpoint/heard` echoes it: raise it, wait a poll, read again.
    telemetry_seq: int


def _int_or(value: object, absent: int) -> int:
    """An integer from a telemetry report, distinguishing ABSENT from a legitimate zero.

    `x or default` is the obvious spelling and it is wrong for every field whose healthy value
    is 0 — which, on this route, is most of them. Written once here rather than remembered at
    each call site."""
    if value is None:
        return absent
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return absent


def _panel_message(report: dict) -> "PanelMessage | None":
    """The last message's playback, or None from a panel that has not streamed one."""
    if not (report.get("msg_ok") or report.get("msg_bad")):
        return None
    got = _int_or(report.get("msg_bytes"), 0)
    ms = _int_or(report.get("msg_ms"), 0)
    expected = got // _MSG_BYTES_PER_MS
    return PanelMessage(
        bytes=got,
        ms=ms,
        expected_ms=expected,
        ok=_int_or(report.get("msg_ok"), 0),
        bad=_int_or(report.get("msg_bad"), 0),
        err=str(report.get("msg_err", "") or ""),
        waited_ms=_int_or(report.get("msg_waited_ms"), 0),
        # A panel that has only ever FAILED to stream one has no duration to judge, and calling
        # that "heard" would be the wrong way round.
        heard=bool(got) and ms * 5 >= expected,
    )


@router.get("/endpoint/reach")
async def panel_reach(request: Request, _p: DebugDep) -> PanelReachOut:
    """WHETHER EACH PANEL CAN REACH THIS BOX, AND WHY NOT — without a cable and without reading
    the access log by hand.

    THIS ROUTE EXISTS BECAUSE ANSWERING IT ONCE TOOK TWO THOUSAND LINES OF `GET /logs/api`. On
    2026-09-29 the owner pressed the pet on Lydian's panel, got the failure dash immediately, and
    asked why. The box's own evidence was entirely negative: no `POST /endpoint/converse` had
    arrived, no `GET /endpoint/settings` for an hour before that, nothing at all for the last
    thirty minutes. All true, all invisible except by paging back through the log and noticing
    which requests had STOPPED — the hardest thing to see in a log, because a request that never
    happened leaves no line to find.

    A REQUEST THAT DIES ON THE PANEL NEVER REACHES THIS BOX. That is the whole difficulty: the
    access log is a record of what worked, so the failures are exactly what is not in it. The
    panel knows the reason — `talk.c` logs `"connect failed"` — on a serial console that does not
    exist in a four-year-old's bedroom (CLAUDE.md #10). `reach.c` on the panel keeps those reasons
    across the outage and reports them when it can speak again; this route is where they land.

    READ `stale_s` FIRST. It is the only field here that does not depend on the panel being able
    to talk to us, and a panel that has gone silent shows a healthy row with an old timestamp
    rather than an unhealthy one.
    """
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT pr.label, s.version, s.reported_at, s.report, pr.id::text
                    FROM app.endpoint_status s
                    JOIN app.principals pr ON pr.id = s.principal_id
                    ORDER BY s.reported_at DESC
                    """
                )
            )
        ).all()
        seq = (
            await session.execute(
                text("SELECT telemetry_seq FROM app.endpoint_settings WHERE id = 1")
            )
        ).scalar_one_or_none()

    now = dt.datetime.now(dt.UTC)
    sockets = panel_ws.snapshot()
    panels: list[PanelReach] = []
    for row in rows:
        report = row[3] if isinstance(row[3], dict) else {}
        at = row[2]
        panels.append(
            PanelReach(
                label=str(row[0]),
                version=str(row[1] or ""),
                reported_at=at.isoformat() if at is not None else "",
                stale_s=int((now - at).total_seconds()) if at is not None else -1,
                # ABSENT IS -1; PRESENT AND ZERO IS ZERO, and the difference is the whole point
                # of the field. Written first as `report.get(..., -1) or -1`, which turns a
                # panel that reached the box THIS INSTANT — the healthiest possible answer, 0 —
                # into "never reached it", because 0 is falsy. That is precisely the confusion
                # `-1` was chosen to prevent, reintroduced in the reader rather than the writer.
                box_quiet_s=_int_or(report.get("box_quiet_s"), -1),
                dashes=_int_or(report.get("dashes"), 0),
                dash_err=str(report.get("dash_err", "") or ""),
                dash_ago_s=_int_or(report.get("dash_ago_s"), 0),
                last_message=_panel_message(report),
                socket=sockets.get(str(row[4])),
                ws=str(report.get("ws", "") or ""),
                ws_err=str(report.get("ws_err", "") or ""),
                ws_connects=_int_or(report.get("ws_connects"), 0),
                ws_drops=_int_or(report.get("ws_drops"), 0),
                int_free=_int_or(report.get("int_free"), -1),
                int_min=_int_or(report.get("int_min"), -1),
                int_big=_int_or(report.get("int_big"), -1),
                paths=[
                    PanelPath(
                        name=name,
                        err=str(report.get(f"{name}_err", "") or ""),
                        fails=_int_or(report.get(f"{name}_fails"), 0),
                        ago_s=_int_or(report.get(f"{name}_ago_s"), 0),
                    )
                    # `set` is every knob and the waiting count, `poll` is the message check on
                    # the panel's other task, `talk` is the conversation, and `send` is a child's
                    # recorded message going out — the one where a silent failure loses something
                    # that cannot be recovered. Listed even at zero:
                    # "the conversation has never failed" is an answer, and a route that omitted
                    # the healthy paths would make absence mean two things.
                    for name in ("set", "poll", "talk", "send")
                ],
            )
        )
    log.info("debug.panel_reach", panels=len(panels))
    return PanelReachOut(panels=panels, telemetry_seq=int(seq or 0))


class ReportNowOut(BaseModel):
    telemetry_seq: int
    detail: str


@router.post("/endpoint/report-now")
async def panel_report_now(request: Request, _p: DebugDep) -> ReportNowOut:
    """Ask every panel to post its telemetry on its next settings poll, ~3 s from now.

    Raises the counter the settings route serves. A panel adopts the value it sees on its
    first poll after boot and posts whenever it CHANGES, so this reaches each panel exactly
    once however many are listening and whatever order they poll in, and there is nothing
    here to clear afterwards.

    It does NOT shorten `CHECK_PERIOD_MS`: the fifteen-minute cycle is what makes a report at
    6-7 s of uptime mean "this panel just booted", and that diagnostic is worth more than the
    convenience this route provides.

    A panel that is off or offline simply never sees it — there is no queue and no retry,
    because a request to report a ring is worthless by the time the panel comes back with a
    ring that was wiped by the reboot.
    """
    async with scoped_session(_maker(request), _OWNER_CTX) as session:
        seq = (
            await session.execute(
                text(
                    "UPDATE app.endpoint_settings SET telemetry_seq = telemetry_seq + 1,"
                    " updated_at = now() WHERE id = 1 RETURNING telemetry_seq"
                )
            )
        ).scalar_one_or_none()
        await session.commit()
    if seq is None:
        raise HTTPException(status_code=404, detail="no endpoint settings row")
    log.info("debug.panel_report_now", telemetry_seq=int(seq))
    # AND NOW, RATHER THAN AT THE NEXT POLL. This route's whole value is "ask and then read",
    # and three seconds of it was the panel wondering rather than the box telling (`nudge.py`).
    woken = nudge.fire_all(why="report-now")
    return ReportNowOut(
        telemetry_seq=int(seq),
        detail=(
            f"nudged {woken} panel(s) to post now; unreachable ones post on their next "
            "settings poll (~3 s). Read /endpoint/heard after."
        ),
    )
