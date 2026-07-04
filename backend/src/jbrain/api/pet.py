"""JPet HTTP surface (docs/proposed/JPET_V2_PLAN.md) — the realtime backbone both
surfaces build on.

- `GET /pet` — the current authoritative state (creating the pet on first read).
- `POST /pet/command` — a play command: a kid button (dance/chase/hide/…) that expands
  to a bounded action script, `say` (freeform → the talk brain → a script), or a parent
  `move`. Applies it, broadcasts the new state, and returns it.
- `GET /pet/stream` — a Server-Sent-Events stream (matching the repo's SSE-not-WS
  choice): an initial snapshot, then every subsequent state change, so the Wall and
  the phone Control screen stay in sync off one server-authoritative row.

Owner-gated (single-owner box).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from jbrain.api.deps import PrincipalDep, owner_only
from jbrain.api.notes import ctx_for
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.jpet.brain import pet_turn
from jbrain.jpet.broadcast import PetBroadcaster
from jbrain.jpet.repo import SqlJpetRepo
from jbrain.jpet.service import PetStateInfo, canned_script
from jbrain.llm.router import LlmRouter

router = APIRouter(prefix="/pet", dependencies=[Depends(owner_only)])

# Mounted under /internal (Caddy never routes /internal off-box), so it is reachable
# only on the docker network — e.g. by the on-box server-brain wall display. No auth:
# it is a READ of the pet snapshot, the pet lives in the safe 'general' domain (no
# health/finance/location), and the display never mutates. See main.include_router.
internal_router = APIRouter(prefix="/pet")

# How long a quiet stream waits before emitting an SSE comment keepalive, so proxies
# and mobile radios don't drop an idle connection between ticks.
_KEEPALIVE_SECONDS = 15.0


def _repo(request: Request) -> SqlJpetRepo:
    return cast(SqlJpetRepo, request.app.state.jpet_repo)


def _broadcaster(request: Request) -> PetBroadcaster:
    return cast(PetBroadcaster, request.app.state.pet_broadcaster)


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _router(request: Request) -> LlmRouter:
    return cast(LlmRouter, request.app.state.llm_router)


class PetOut(BaseModel):
    """The pet's wire shape — the drives flattened to the names the UI shows, plus the
    v2 action `script`, the room `objects` ({kind: [x, z]}), what it's carrying, and the
    light state. Both surfaces render this row; the wall plays out the script."""

    name: str
    domain: str
    food: float
    energy: float
    fun: float
    love: float
    mood: str
    emotion: str
    speech: str | None
    asleep: bool
    pos_x: float
    pos_z: float
    target_x: float
    target_z: float
    facing: float
    action: str
    script: list[dict[str, Any]]
    carrying: str | None
    lights_on: bool
    objects: dict[str, list[float]]

    @classmethod
    def of(cls, info: PetStateInfo) -> "PetOut":
        return cls(
            name=info.name,
            domain=info.domain,
            food=info.drives.food,
            energy=info.drives.energy,
            fun=info.drives.fun,
            love=info.drives.love,
            mood=info.mood,
            emotion=info.emotion,
            speech=info.speech,
            asleep=info.asleep,
            pos_x=info.pos_x,
            pos_z=info.pos_z,
            target_x=info.target_x,
            target_z=info.target_z,
            facing=info.facing,
            action=info.action,
            script=list(info.script),
            carrying=info.carrying,
            lights_on=info.lights_on,
            objects={k: [v[0], v[1]] for k, v in info.objects.items()},
        )


# The kid play-buttons (each expands to a canned, bounded script) + `say` (freeform, runs
# the talk brain) + `move` (a parent affordance: send the pet to a raw floor point).
CommandAction = Literal[
    "dance",
    "spin",
    "jump",
    "wave",
    "wiggle",
    "chase",
    "hide",
    "beep",
    "come",
    "sleep",
    "wake",
    "eat",
    "lights",
    "say",
    "move",
]


class CommandIn(BaseModel):
    """A command from a surface. `x`/`z` (normalized floor coords in [-1, 1]) are read
    only for `move`; `text` only for `say`."""

    action: CommandAction
    x: float | None = None
    z: float | None = None
    text: str | None = None


async def _ensure(request: Request, ctx: SessionContext) -> PetStateInfo:
    s = _settings(request)
    return await _repo(request).ensure_pet(ctx, name=s.jpet_name, domain=s.jpet_domain)


@router.get("")
async def get_pet(request: Request, principal: PrincipalDep) -> PetOut:
    """The current pet state (created on first read)."""
    return PetOut.of(await _ensure(request, ctx_for(principal)))


@router.post("/command")
async def send_command(request: Request, principal: PrincipalDep, body: CommandIn) -> PetOut:
    """Apply a command, broadcast the new state to every subscriber, and return it. `say`
    runs the pet.turn brain to answer the child and act it out as a script; a play button
    runs its canned script; `move` sends the pet to a raw floor point."""
    ctx = ctx_for(principal)
    state = await _ensure(request, ctx)
    domain = _settings(request).jpet_domain
    repo = _repo(request)
    if body.action == "say":
        text = (body.text or "").strip()
        memories = await repo.recent_memories(ctx, domain=domain)
        reply = await pet_turn(
            _router(request), state=state, message=text, memories=memories, objects=state.objects
        )
        info = await repo.run_script(
            ctx, domain=domain, script=reply.script, speech=reply.speech, emotion=reply.emotion
        )
        if text:  # remember the exchange so the next turn can recall it
            await repo.record_memory(
                ctx,
                domain=domain,
                kind="said",
                body=f'A child said: "{text[:120]}" — you replied: "{reply.speech[:120]}"',
            )
    elif body.action == "move":
        info = await repo.move_to(ctx, domain=domain, x=body.x or 0.0, z=body.z or 0.0)
    else:
        script = canned_script(body.action, objects=state.objects)
        info = await repo.run_script(ctx, domain=domain, script=script)
    if info is None:  # pragma: no cover — the ensure above guarantees a pet
        raise HTTPException(status_code=404, detail="no pet")
    _broadcaster(request).publish(info)
    return PetOut.of(info)


@router.get("/stream")
async def stream_pet(request: Request, principal: PrincipalDep) -> StreamingResponse:
    """SSE stream: an initial snapshot then every state change. X-Accel-Buffering off
    so a proxy streams events instead of buffering."""
    initial = await _ensure(request, ctx_for(principal))
    return StreamingResponse(
        _events(_broadcaster(request), initial),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@internal_router.get("")
async def internal_get_pet(request: Request) -> PetOut:
    """The current pet snapshot for the on-box wall display (server-brain). Read-only,
    under a system owner context; 404 until the drives tick has created the pet."""
    ctx = SessionContext(principal_kind="owner")
    info = await _repo(request).get_pet(ctx, domain=_settings(request).jpet_domain)
    if info is None:
        raise HTTPException(status_code=404, detail="pet not ready")
    return PetOut.of(info)


async def _events(broadcaster: PetBroadcaster, initial: PetStateInfo) -> AsyncIterator[bytes]:
    """Yield the current snapshot, then each published state as an SSE `data:` frame,
    with a comment keepalive on an idle connection. Unsubscribes on disconnect."""
    queue = broadcaster.subscribe()
    try:
        yield f"data: {PetOut.of(initial).model_dump_json()}\n\n".encode()
        while True:
            try:
                state = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
            except TimeoutError:
                yield b": keepalive\n\n"
                continue
            yield f"data: {PetOut.of(state).model_dump_json()}\n\n".encode()
    finally:
        broadcaster.unsubscribe(queue)
