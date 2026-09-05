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
import datetime as dt
import decimal
import json
import re
import time
import uuid
from typing import Annotated, Any, cast

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from jbrain.ingest.video import transcribe_audio_chunked
from jbrain.llm import LlmImage
from jbrain.llm.errors import LlmError
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    AssistantMessage,
    LlmMessage,
    LlmTool,
    LlmTurn,
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
from jbrain.sdr.roles import GENERAL
from jbrain.sdr.sweep import channels, reduce_csv, steady_channels, waterfall_png
from jbrain.sdr.tuner import MAX_MHZ, TUNABLE_MIN_MHZ, nodes_in, out_of_range, sweepable
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
    # The night's observed tool results, in the order the sitting produced them. Matched to
    # the model's calls by NAME (FIFO per name) so a replay that reorders its reads still
    # continues; `matched_recorded` records whether the order held.
    stubs: list[ReplayStub] = Field(default_factory=list)
    # What a call with no recorded result gets back. The replay continues rather than
    # stopping, because where it goes AFTER leaving the recorded path is the interesting part.
    fallback_result: str = "(no recorded result for this call)"
    max_steps: int = Field(default=8, ge=1, le=24)
    max_tokens: int = Field(default=2048, ge=1, le=32768)


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
    request.state.debug_detail = f"{len(body.tools)} tools, {body.max_steps} steps"
    registry = cast(ToolRegistry, request.app.state.agent_registry)
    unknown = [t for t in body.tools if t not in registry.names()]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown tools: {unknown}")
    llm_tools = [registry.get(name).as_llm_tool() for name in body.tools]
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
    result: CompleteOut | SdrSweepOut | None = None
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


# The code-mode (jcode) services, in the order most useful for debugging a turn: the
# control server, then the model gateway.
_JCODE_LOG_SERVICES = ("jcode", "local-llm")


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
    for service in _JCODE_LOG_SERVICES:
        resp = await client.get(f"/logs/{service}", params={"tail": tail}, headers=headers)
        if resp.status_code == 404:
            body = "(service not running)"
        else:
            resp.raise_for_status()
            body = resp.text
        sections.append(f"===== {service} =====\n{body}")
    return PlainTextResponse("\n\n".join(sections))


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
    (the container's stdout via the supervisor). 502 if the gateway can't be reached."""
    request.state.debug_detail = f"gateway (tail {tail})"
    try:
        full = await _gateway(request).tail_logs()
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"gateway logs unavailable: {exc}") from exc
    return PlainTextResponse("\n".join(full.splitlines()[-tail:]))


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
    served model id to isolate one model's load. An empty body means the engine has printed
    nothing since llama-swap started — usually a box with no load since boot, not a fault.
    502 if the gateway can't be reached."""
    request.state.debug_detail = f"upstream {stream} (tail {tail})"
    try:
        full = await _gateway(request).tail_upstream_logs(stream)
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"upstream logs unavailable: {exc}") from exc
    return PlainTextResponse("\n".join(full.splitlines()[-tail:]))


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
    not that nothing was freed."""
    request.state.debug_detail = f"drop page cache ({models or 'all'})"
    ids = [m.strip() for m in models.split(",") if m.strip()] if models else None
    freed = _gateway(request).drop_page_cache(ids)
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
    # Bounded to what the RADIO reaches, not to what a sweep reaches — so a shortwave
    # request arrives here and gets `sweepable`'s sentence rather than a 422 validation
    # blob. The difference matters because shortwave is listenable and not sweepable,
    # and that is precisely the thing a bare bound cannot say.
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

    `include_csv` returns rtl_power's own numbers alongside the reduction. Off by
    default because it is megabytes; worth having because calibrating a detector against
    a summary the detector produced is circular, and the first round of this was done by
    reading brightness off the PNG."""
    request.state.debug_detail = f"sdr sweep {start_mhz}-{stop_mhz} MHz for {seconds}s"
    if not settings.sdr_url:
        raise HTTPException(status_code=503, detail="No SDR on this box (sdr_url unset).")
    # Both EDGES, because the sidecar validates the sweep's centre: a 10-70 MHz request
    # centres on 40 and passes every check while its bottom half cannot be measured at
    # all, and comes back reported as quiet.
    for edge in (start_mhz, stop_mhz):
        refusal = sweepable(edge)
        if refusal:
            raise HTTPException(status_code=400, detail=refusal)

    body = {
        "start_hz": int(round(start_mhz * 1_000_000)),
        "stop_hz": int(round(stop_mhz * 1_000_000)),
        "bin_hz": int(round(bin_khz * 1_000)),
        "seconds": seconds,
        "gain": gain,
        # A sweep is a general use of the radio, so it may not take one reserved for a
        # service. Resolved BEFORE the job is queued, so a refusal is this request's 409
        # rather than an error the caller has to poll for.
        "serial": await _radio(request, settings, GENERAL),
    }
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
            )
            jobs[job_id] = {"status": "done", "result": out, "error": None}
        except Exception as exc:  # noqa: BLE001 - a debug job must surface, not crash the loop
            jobs[job_id] = {"status": "error", "result": None, "error": str(exc)}

    task = asyncio.create_task(_run())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return JobSubmitOut(job_id=job_id)


def _receivable(frequency_mhz: float) -> None:
    """Refuse a frequency the radio would answer with a DIFFERENT one — the debug twin
    of `api/sdr.py`'s `_tunable`, and needed here for the same reason the owner routes
    need it: the `Query` bounds check the ENDS, and 14.4-24 MHz sits inside them and is
    reached by neither path. Below 24 MHz the sidecar tunes with `-E direct2`, and
    direct sampling folds the second Nyquist zone back onto the first, so 18.1 MHz is
    received as 10.7 (SDR_IQ_SPECTRUM_PLAN §8). A capture from there transcribes
    cleanly and names the wrong band."""
    refusal = out_of_range(frequency_mhz)
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
    _receivable(frequency_mhz)
    if not settings.sdr_url:
        raise HTTPException(status_code=503, detail="No SDR on this box (sdr_url unset).")

    freq_hz = int(round(frequency_mhz * 1_000_000))
    async with httpx.AsyncClient(base_url=settings.sdr_url, timeout=seconds + 60) as client:
        resp = await client.post(
            "/capture",
            json={
                "frequency_hz": freq_hz,
                "seconds": seconds,
                "mode": mode,
                "gain": gain,
                # A capture is a general use of the radio. The sidecar has accepted a
                # serial here since before radio roles existed; nothing had ever sent one.
                "serial": await _radio(request, settings, GENERAL),
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


@router.post("/sdr/stop")
async def sdr_stop_debug(request: Request, settings: SettingsDep, _p: DebugDep) -> dict[str, Any]:
    """Release the radio. The composer's tuner icon disappears when it lands."""
    request.state.debug_detail = "sdr stop"
    return await _sdr_post(settings, "/listen/stop", {"session_id": None})


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
    return await llm_settings.gateway_props(model_id, settings, _gateway(request))


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
    )
