"""JPet HTTP surface (docs/archive/JPET_V3_PLAN.md) — the realtime backbone both
surfaces build on.

- `GET /pet` — the current authoritative state (creating the pet on first read).
- `POST /pet/command` — a play command: a kid button (dance/chase/hide/…) that expands to
  a bounded action script, `say` (freeform → the hybrid keyword→LLM router → a script or
  colour), `color` (the phone palette), or a parent `move`. Applies it, broadcasts the new
  state, and returns it.
- `GET /pet/stream` — a Server-Sent-Events stream (matching the repo's SSE-not-WS
  choice): an initial snapshot, then every subsequent state change, so the Wall and
  the phone Control screen stay in sync off one server-authoritative row.

Owner-gated (single-owner box).
"""

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from jbrain.api.deps import PrincipalDep, owner_only
from jbrain.api.notes import ctx_for
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.jpet.brain import pet_turn, statue_voxels
from jbrain.jpet.broadcast import PetBroadcaster
from jbrain.jpet.intents import CHAT_BABBLE, canonical_color, chat_reply, classify, color_speech
from jbrain.jpet.repo import SqlJpetRepo
from jbrain.jpet.service import PetStateInfo, canned_script
from jbrain.llm.router import LlmRouter
from jbrain.locations.ratelimit import TokenBucket

log = structlog.get_logger()

router = APIRouter(prefix="/pet", dependencies=[Depends(owner_only)])

# Mounted under /internal (Caddy never routes /internal off-box), so it is reachable
# only on the docker network — e.g. by the on-box wall display. No auth:
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


# Ephemeral wall effects (never persisted): the "turn X <colour>" / "make X bigger" overrides.
# Bounds keep a kid from making a thing vanish or fill the room.
_SCALE_MIN, _SCALE_MAX = 0.4, 2.5
_GROW_STEP, _SHRINK_STEP = 1.25, 0.8


def _fresh_effects() -> dict[str, Any]:
    """A clean effects store — every override at its default (the reload / reset state)."""
    return {
        "colors": {},
        "scales": {},
        "pet_scale": 1.0,
        "pet_form": "robot",
        # Scene: "room" (default) or "field". `statue`/`statue_seq` carry the current
        # "build a statue of X" request to the wall (the seq bumps so a repeat re-triggers).
        "scene": "room",
        "statue": None,
        "statue_seq": 0,
    }


def _effects(request: Request) -> dict[str, Any]:
    """The in-memory effects store (colours/scales/pet scale/form), lazily created so a test app
    that didn't wire it still works. Cleared on the wall's reload via `/internal/pet/effects`."""
    fx = getattr(request.app.state, "pet_effects", None)
    if fx is None:
        fx = _fresh_effects()
        request.app.state.pet_effects = fx
    fx.setdefault("pet_form", "robot")  # tolerate a store created before forms existed
    return fx


def _clamp_scale(v: float) -> float:
    return max(_SCALE_MIN, min(_SCALE_MAX, v))


def _resized(cur: float, value: str) -> float:
    """New scale for a resize: huge/tiny jump to max/min, grow/shrink step, reset → 1."""
    if value == "reset":
        return 1.0
    if value == "huge":
        return _SCALE_MAX
    if value == "tiny":
        return _SCALE_MIN
    return _clamp_scale(cur * (_GROW_STEP if value == "grow" else _SHRINK_STEP))


def _apply_effect(fx: dict[str, Any], *, kind: str, target: str, value: str) -> None:
    """Fold a recolor/resize/form intent into the ephemeral store. Colour 'default' / a size
    'reset' drop the override (back to the object's built-in look/size)."""
    if kind == "form":
        fx["pet_form"] = value
    elif kind == "scene":
        fx["scene"] = value
    elif kind == "statue":
        # "Build a statue of X": jump to the field and hand the subject to the wall (which
        # fetches the voxel model). The bumped seq makes a repeat of the same subject re-trigger.
        fx["scene"] = "field"
        fx["statue"] = value
        fx["statue_seq"] = int(fx.get("statue_seq", 0)) + 1
    elif kind == "recolor":
        if value == "default":
            fx["colors"].pop(target, None)
        else:
            fx["colors"][target] = value
    elif target == "robot":  # resize the pet itself
        fx["pet_scale"] = _resized(fx["pet_scale"], value)
    elif value == "reset":  # resize a room thing — reset drops the override
        fx["scales"].pop(target, None)
    else:
        fx["scales"][target] = _resized(fx["scales"].get(target, 1.0), value)


class PetOut(BaseModel):
    """The pet's wire shape (v3 — no drive meters): durable state + the current command
    `script`, the room `objects` ({kind: [x, z]}), what it's carrying, and the light state.
    The wall runs the pet's continuous life and plays a script as an interrupt."""

    name: str
    domain: str
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
    color: str | None
    script: list[dict[str, Any]]
    carrying: str | None
    lights_on: bool
    objects: dict[str, list[float]]
    # Ephemeral "turn X <colour>" / "make X bigger" wall effects (docs — talk-box commands):
    # a per-object colour override, a per-object scale, and the robot's own scale. NEVER
    # persisted — held only in memory (`app.state.pet_effects`), overlaid here for the wall's
    # poll, and cleared when the wall reloads, so a fresh display starts back at the defaults.
    object_colors: dict[str, str] = {}
    object_scales: dict[str, float] = {}
    pet_scale: float = 1.0
    # Which creature the pet is drawn as: "robot" (default) or dog/cat/dragon/cow/pig/chicken.
    pet_form: str = "robot"
    # Scene ("room"/"field") + the current "build a statue of X" request (subject + a seq the
    # wall watches so a repeat re-triggers a fresh draft). All ephemeral wall effects.
    pet_scene: str = "room"
    statue_subject: str | None = None
    statue_seq: int = 0

    @classmethod
    def of(cls, info: PetStateInfo, effects: dict[str, Any] | None = None) -> "PetOut":
        fx = effects or {}
        return cls(
            name=info.name,
            domain=info.domain,
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
            color=info.color,
            script=list(info.script),
            carrying=info.carrying,
            lights_on=info.lights_on,
            objects={k: [v[0], v[1]] for k, v in info.objects.items()},
            object_colors=dict(fx.get("colors", {})),
            object_scales=dict(fx.get("scales", {})),
            pet_scale=float(fx.get("pet_scale", 1.0)),
            pet_form=str(fx.get("pet_form", "robot")),
            pet_scene=str(fx.get("scene", "room")),
            statue_subject=fx.get("statue"),
            statue_seq=int(fx.get("statue_seq", 0)),
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
    "jumprope",
    "music",
    "guitar",
    "sing",
    "fart",
    "burp",
    "say",
    "move",
    "color",
]


class CommandIn(BaseModel):
    """A command from a surface. `x`/`z` (normalized floor coords in [-1, 1]) are read only
    for `move`; `text` is the child's message for `say`, or the colour name for `color`."""

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
    return PetOut.of(await _ensure(request, ctx_for(principal)), _effects(request))


@router.post("/command")
async def send_command(request: Request, principal: PrincipalDep, body: CommandIn) -> PetOut:
    """Apply a command, broadcast the new state to every subscriber, and return it. `say`
    runs the hybrid talk router (keyword first, LLM only for open-ended); `color` recolours
    the robot; a play button runs its canned script; `move` sends the pet to a floor point."""
    ctx = ctx_for(principal)
    state = await _ensure(request, ctx)
    domain = _settings(request).jpet_domain
    repo = _repo(request)
    if body.action == "say":
        info = await _say(request, ctx, domain, state, (body.text or "").strip())
    elif body.action == "color":
        color = canonical_color(body.text or "") or "rainbow"
        info = await repo.set_color(ctx, domain=domain, color=color, speech=color_speech(color))
    elif body.action == "move":
        info = await repo.move_to(ctx, domain=domain, x=body.x or 0.0, z=body.z or 0.0)
    else:
        script = canned_script(body.action, objects=state.objects)
        info = await repo.run_script(ctx, domain=domain, script=script)
    if info is None:  # pragma: no cover — the ensure above guarantees a pet
        raise HTTPException(status_code=404, detail="no pet")
    _broadcaster(request).publish(info)
    return PetOut.of(info, _effects(request))


async def _say(
    request: Request,
    ctx: SessionContext,
    domain: str,
    state: PetStateInfo,
    text: str,
    *,
    remember: bool = True,
) -> PetStateInfo | None:
    """The hybrid talk→action router (docs/archive/JPET_V3_PLAN.md W3). A fast keyword
    classifier runs FIRST — "dance!", "chase the ball", "turn red" act immediately with no
    LLM, and common small talk ("how are you", "tell me a joke") gets a funny canned reply.
    Only genuinely open-ended input reaches the LLM, wrapped so a slow/unconfigured/failed
    model NEVER 500s: it degrades to a random funny babble + emote (never the same line), so
    the mic/text mode always holds a silly little conversation."""
    repo = _repo(request)
    intent = classify(text)
    if intent is not None and intent.kind == "color":
        info = await repo.set_color(ctx, domain=domain, color=intent.value, speech=intent.speech)
    elif intent is not None and intent.kind == "reset_all":
        # "Reset everything" — wipe every ephemeral effect AND the pet's own colour, in one go.
        request.app.state.pet_effects = _fresh_effects()
        info = await repo.set_color(ctx, domain=domain, color="default", speech=intent.speech)
    elif intent is not None and intent.kind in ("recolor", "resize", "form", "scene", "statue"):
        # An ephemeral wall effect — "turn the floor blue", "make the bed huge", "be a dragon",
        # "change scene to field", "build a statue of a cat". Fold it into the in-memory store
        # (the wall reads it on its next poll; a reload resets it) and give the pet a little
        # reaction so both surfaces show it did something.
        _apply_effect(
            _effects(request), kind=intent.kind, target=intent.target or "robot", value=intent.value
        )
        emote = "wiggle" if intent.kind == "resize" else "spin"  # a twirl sells a form/scene change
        script = canned_script(emote, objects=state.objects)
        info = await repo.run_script(ctx, domain=domain, script=script, speech=intent.speech)
    elif intent is not None:  # a recognised command action or a bit of small talk — no LLM
        script = canned_script(intent.value, objects=state.objects)
        info = await repo.run_script(ctx, domain=domain, script=script, speech=intent.speech)
    else:  # open-ended → the LLM, but it must never break the interaction
        try:
            memories = await repo.recent_memories(ctx, domain=domain)
            reply = await pet_turn(
                _router(request),
                state=state,
                message=text,
                memories=memories,
                objects=state.objects,
            )
            info = await repo.run_script(
                ctx, domain=domain, script=reply.script, speech=reply.speech, emotion=reply.emotion
            )
        except Exception as exc:  # noqa: BLE001 — the LLM must not break "say"
            log.warning("jpet.say_llm_error", error=repr(exc))
            # No model: match known small talk for a funny reply, else a random babble + emote.
            reply = chat_reply(text)
            emote, speech = reply or (
                random.choice(("wiggle", "nod", "wave", "beep", "spin")),
                random.choice(CHAT_BABBLE),
            )
            info = await repo.run_script(
                ctx,
                domain=domain,
                script=canned_script(emote, objects=state.objects),
                speech=speech,
            )
    if text and remember:  # remember the exchange so the next turn can recall it
        await repo.record_memory(
            ctx, domain=domain, kind="said", body=f'A child said: "{text[:140]}"'
        )
    return info


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
    """The current pet snapshot for the on-box wall display (deploy/wall). Read-only,
    under a system owner context; 404 until the drives tick has created the pet."""
    ctx = SessionContext(principal_kind="owner")
    info = await _repo(request).get_pet(ctx, domain=_settings(request).jpet_domain)
    if info is None:
        raise HTTPException(status_code=404, detail="pet not ready")
    return PetOut.of(info, _effects(request))


class SayIn(BaseModel):
    """A spoken command captured by the wall's wake-word listener ("robot, dance!")."""

    text: str


@internal_router.post("/say")
async def internal_say(request: Request, body: SayIn) -> PetOut:
    """Drive the pet from the on-box wall's voice listener (deploy/wall). Internal-only (Caddy
    never routes /internal off-box) and reached solely through the wall's same-origin proxy, so
    the exposure equals the wall itself: LAN-only, and the owner authorised that by deploying the
    wall. Kept safe by construction — the pet lives in the non-sensitive 'general' domain (no
    health/finance/location), the talk brain's persona is built only from in-scope pet state, and:
    it is RATE-LIMITED (an unauthenticated LAN caller can't flood the local LLM) and does NOT write
    the heard text into the pet's long-term memory (the authenticated phone path does that)."""
    limiter = cast(TokenBucket, request.app.state.pet_say_rate_limiter)
    if not limiter.allow("wall"):
        raise HTTPException(status_code=429, detail="too many pet commands; slow down")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="no text")
    ctx = SessionContext(principal_kind="owner")
    domain = _settings(request).jpet_domain
    state = await _repo(request).ensure_pet(ctx, name=_settings(request).jpet_name, domain=domain)
    info = await _say(request, ctx, domain, state, text[:500], remember=False)
    if info is None:  # pragma: no cover — ensure guarantees a pet
        raise HTTPException(status_code=404, detail="no pet")
    _broadcaster(request).publish(info)
    return PetOut.of(info, _effects(request))


class StatueIn(BaseModel):
    """The subject the wall wants sculpted for its field "build a statue of X"."""

    subject: str


@internal_router.post("/statue")
async def internal_statue(request: Request, body: StatueIn) -> dict[str, Any]:
    """Sculpt a voxel model of `subject` for the on-box wall's field build. Same LAN-only
    posture as /say (reached solely through the wall's same-origin proxy; the pet lives in the
    non-sensitive 'general' domain) and RATE-LIMITED so an unauthenticated caller can't spin the
    reasoning model in a loop. Returns the occupied voxels; the wall animates the block-by-block
    build. A model/parse failure is a clean 502 so the wall can say it couldn't imagine that one."""
    limiter = cast(TokenBucket, request.app.state.pet_say_rate_limiter)
    if not limiter.allow("wall"):
        raise HTTPException(status_code=429, detail="too many statue requests; slow down")
    subject = (body.subject or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="no subject")
    try:
        voxels = await statue_voxels(_router(request), subject=subject[:80])
    except Exception as exc:  # noqa: BLE001 — a failed/unconfigured model must be a clean error
        log.warning("jpet.statue_error", error=repr(exc))
        raise HTTPException(status_code=502, detail="could not imagine that statue") from exc
    return {
        "subject": subject[:80],
        "voxels": [{"x": v.x, "y": v.y, "z": v.z, "c": v.c} for v in voxels],
    }


@internal_router.post("/effects/clear")
async def internal_clear_effects(request: Request) -> dict[str, bool]:
    """Wipe the ephemeral colour/size overrides. The wall calls this on page load so a reload
    starts from the built-in defaults (the effects were never persisted). Internal-only."""
    request.app.state.pet_effects = _fresh_effects()
    return {"ok": True}


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
