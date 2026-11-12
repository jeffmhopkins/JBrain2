"""The GrowBot call log's routes (migration 0260, `jbrain.growbot.calls`).

- `internal_router` — the sidecar (`growbot.token`): `POST /internal/growbot/calls` records a
  transcription or a spoken line after it ran. The model lane writes its own rows.
- `debug_router` — a debug token: `GET /api/debug/growbot/calls` lists recent rows, newest
  first, and `GET /api/debug/growbot/calls/summary` splits their latency by kind and stage,
  with picture turns apart from text-only ones. The owner has no terminal (CLAUDE.md #10),
  so this is how the log is read off the box.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep
from jbrain.api.growbot_llm import log_call
from jbrain.api.internal_auth import require_growbot
from jbrain.db.session import SessionContext
from jbrain.growbot.calls import CallLog, CallRecord, LoggedCall

internal_router = APIRouter(prefix="/growbot", dependencies=[Depends(require_growbot)])
debug_router = APIRouter(prefix="/debug/growbot")

_DEBUG_OWNER = SessionContext(principal_id="debug-console", principal_kind="owner")
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# The stages a summary reports, in the order a turn passes through them.
_STAGES = (
    "lock_ms",
    "admit_ms",
    "first_byte_ms",
    "first_token_ms",
    "engine_prefill_ms",
    "engine_decode_ms",
    "upstream_ms",
    "audio_s",
)


_DetailValue = Annotated[str, Field(max_length=500)] | int | float | bool | None


def _finite(value: object) -> bool:
    """json.dumps writes NaN as `NaN`, which jsonb refuses, so one would lose the whole row.
    (Refusing it in the model instead makes FastAPI's 422 fail to serialise the input.)"""
    return not isinstance(value, float) or math.isfinite(value)


def _calls(request: Request) -> CallLog:
    return cast(CallLog, request.app.state.growbot_calls)


class SidecarCallIn(BaseModel):
    kind: Literal["stt", "tts"]
    ok: bool
    total_ms: int = Field(ge=0, le=3_600_000)
    persona: str | None = Field(default=None, pattern=_UUID)
    robot: str = Field(default="", pattern=r"^(gb-[A-Za-z0-9]{1,32})?$")
    model: str = Field(default="", max_length=80)
    error: str = Field(default="", max_length=500)
    # The transcript (stt) or the line spoken (tts).
    text: str = Field(default="", max_length=8000)
    # stt: the clip's length and the prompt the app gave whisper; tts: the voice.
    detail: dict[str, _DetailValue] = Field(default_factory=dict)
    timings: dict[str, int | float] = Field(default_factory=dict)


@internal_router.post("/calls", status_code=204)
async def record_sidecar_call(request: Request, body: SidecarCallIn) -> Response:
    if len(body.detail) > 20 or len(body.timings) > 20:
        return Response(status_code=400)
    log_call(
        request,
        CallRecord(
            kind=body.kind,
            ok=body.ok,
            total_ms=body.total_ms,
            persona_id=body.persona,
            robot=body.robot,
            model=body.model,
            error=body.error,
            request={k: v for k, v in body.detail.items() if len(k) <= 40 and _finite(v)},
            reply=body.text,
            timings={k: v for k, v in body.timings.items() if len(k) <= 40 and _finite(v)},
        ),
    )
    return Response(status_code=204)


class CallOut(BaseModel):
    id: int
    created_at: datetime
    kind: str
    ok: bool
    total_ms: int
    persona_id: str | None
    robot: str
    model: str
    error: str
    images: int
    image_bytes: int
    prompt_tokens: int | None
    cached_tokens: int | None
    completion_tokens: int | None
    timings: dict[str, Any]
    reply: str
    reasoning: str
    # The full request (messages, pictures as their size) only with `full=true`.
    request: dict[str, Any] | None = None


def _out(call: LoggedCall, full: bool) -> CallOut:
    rec = call.record
    return CallOut(
        id=call.id,
        created_at=call.created_at,
        kind=rec.kind,
        ok=rec.ok,
        total_ms=rec.total_ms,
        persona_id=rec.persona_id,
        robot=rec.robot,
        model=rec.model,
        error=rec.error,
        images=rec.images,
        image_bytes=rec.image_bytes,
        prompt_tokens=rec.prompt_tokens,
        cached_tokens=rec.cached_tokens,
        completion_tokens=rec.completion_tokens,
        timings=rec.timings,
        reply=rec.reply,
        reasoning=rec.reasoning,
        request=rec.request if full or rec.kind != "llm" else None,
    )


def _since(minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


@debug_router.get("/calls")
async def debug_list_calls(
    request: Request,
    _p: DebugDep,
    minutes: Annotated[int, Query(ge=1, le=10_080)] = 60,
    kind: Literal["llm", "stt", "tts"] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    full: bool = False,
) -> list[CallOut]:
    calls = await _calls(request).recent(
        _DEBUG_OWNER, since=_since(minutes), kind=kind, limit=limit
    )
    return [_out(c, full) for c in calls]


def _spread(values: list[float]) -> dict[str, float | int]:
    values = sorted(values)
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50": round(statistics.median(values), 1),
        "p90": round(values[min(len(values) - 1, int(len(values) * 0.9))], 1),
        "max": round(values[-1], 1),
    }


def summarize(calls: list[LoggedCall]) -> dict[str, Any]:
    """Each group's count, failures, total and per-stage spread; model turns split by whether
    they carried a picture, since a frame's prefill is the suspect."""
    groups: dict[str, list[CallRecord]] = {}
    for call in calls:
        rec = call.record
        name = rec.kind if rec.kind != "llm" else ("llm_image" if rec.images else "llm_text")
        groups.setdefault(name, []).append(rec)
    out: dict[str, Any] = {}
    for name, recs in sorted(groups.items()):
        stages: dict[str, Any] = {}
        for stage in _STAGES:
            got = [
                float(r.timings[stage])
                for r in recs
                if isinstance(r.timings.get(stage), (int, float))
            ]
            if got:
                stages[stage] = _spread(got)
        prompt = [float(r.prompt_tokens) for r in recs if r.prompt_tokens is not None]
        cached = [float(r.cached_tokens) for r in recs if r.cached_tokens is not None]
        out[name] = {
            "failed": sum(1 for r in recs if not r.ok),
            "total_ms": _spread([float(r.total_ms) for r in recs]),
            "stages": stages,
            "prompt_tokens": _spread(prompt),
            "cached_tokens": _spread(cached),
            "image_bytes": _spread([float(r.image_bytes) for r in recs if r.images]),
        }
    return out


@debug_router.get("/calls/summary")
async def debug_calls_summary(
    request: Request,
    _p: DebugDep,
    minutes: Annotated[int, Query(ge=1, le=10_080)] = 60,
) -> dict[str, Any]:
    calls = await _calls(request).recent(_DEBUG_OWNER, since=_since(minutes), limit=5000)
    return {"minutes": minutes, "calls": len(calls), "groups": summarize(calls)}
