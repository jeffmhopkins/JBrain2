"""JPet HTTP surface (docs/plans/JPET_PLAN.md §1, W1) — the realtime backbone both
surfaces build on.

- `GET /pet` — the current authoritative state (creating the pet on first read).
- `POST /pet/command` — a care/move command (feed/play/pet/poke/sleep/move); applies
  it, broadcasts the new state, and returns it.
- `GET /pet/stream` — a Server-Sent-Events stream (matching the repo's SSE-not-WS
  choice): an initial snapshot, then every subsequent state change, so the Wall and
  the phone Control screen stay in sync off one server-authoritative row.

Owner-gated for W1 (single-owner box); the kid/family device-session principal that
lets a child drive the pet from the phone arrives with W3.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Literal, cast

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
from jbrain.jpet.service import Command, PetStateInfo
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
    """The pet's wire shape — the drives flattened to the names the UI shows."""

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
        )


class CommandIn(BaseModel):
    """A command from a surface. `x`/`z` (normalized floor coords in [-1, 1]) are read
    only for `move`."""

    action: Literal["feed", "play", "pet", "poke", "sleep", "move", "say"]
    x: float | None = None
    z: float | None = None
    # Only read for `say`: what the child said to the pet.
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
    """Apply a command, broadcast the new state to every subscriber, and return it.
    `say` runs the pet.turn brain to answer the child; the rest are pure deltas."""
    ctx = ctx_for(principal)
    state = await _ensure(request, ctx)
    domain = _settings(request).jpet_domain
    repo = _repo(request)
    if body.action == "say":
        text = (body.text or "").strip()
        memories = await repo.recent_memories(ctx, domain=domain)
        reply = await pet_turn(_router(request), state=state, message=text, memories=memories)
        info = await repo.apply_reply(
            ctx, domain=domain, speech=reply.speech, emotion=reply.emotion, action=reply.action
        )
        if text:  # remember the exchange so the next turn can recall it (W5)
            await repo.record_memory(
                ctx,
                domain=domain,
                kind="said",
                body=f'A child said: "{text[:120]}" — you replied: "{reply.speech[:120]}"',
            )
    else:
        info = await repo.apply_command(
            ctx, domain=domain, command=Command(action=body.action, x=body.x, z=body.z)
        )
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
