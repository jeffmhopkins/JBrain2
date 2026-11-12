"""A GrowBot body's calibration and the PWA's direct leg moves (the Body tab's Legs pane), so
nothing upstream's own tester page did needs the original app.

- `router` — the owner: `GET`/`PUT /growbot/robots/{code}/cal` read and save the robot's
  calibration (trims, left/right swap, leg and IMU signs, walk gain; a `PUT` saves the keys it
  names, over those saved before), and `POST /growbot/robots/{code}/act` sends the chip a
  keyframe move, its canned wiggle, or a stop, through the sidecar's relay. Each move is what
  upstream's tester sends to check a trim or a leg, so it goes straight to the chip, past any
  persona riding it, as there.
- `debug_router` — the same reads and writes through the debug token (CLAUDE.md #10).
- `internal_router` — the sidecar (`growbot.token`): the calibration the creature page copies
  into its `gb_walk_cal`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, OwnerDep
from jbrain.api.growbot import Code, _sidecar
from jbrain.api.internal_auth import require_growbot
from jbrain.db.session import SessionContext
from jbrain.growbot.body import BodyRepo

router = APIRouter(prefix="/growbot", tags=["growbot"])
debug_router = APIRouter(prefix="/debug/growbot")
internal_router = APIRouter(prefix="/growbot", dependencies=[Depends(require_growbot)])

_SYSTEM_OWNER = SessionContext(principal_kind="owner")
_DEBUG_OWNER = SessionContext(principal_id="debug-console", principal_kind="owner")

Sign = Literal[-1, 1]
# Upstream's tester trims by eye within ±30°: past that the horn is on the wrong spline tooth.
Trim = Annotated[int, Field(ge=-30, le=30)]


class Cal(BaseModel):
    """`gb_walk_cal` as upstream's creature reads it (`calNow()`), field names and all. The
    defaults are upstream's body truth (`v3/body.json` `walk.cal`) for the stock 85 mm body."""

    L_OFF: Trim = 0
    R_OFF: Trim = 0
    SWAP: Literal[0, 1] = 0
    L_SIGN: Sign = -1
    R_SIGN: Sign = 1
    IMU_SIGN: tuple[Sign, Sign, Sign] = (1, -1, 1)
    gain: float = Field(default=1.0, ge=0.0, le=2.0)


class CalOut(BaseModel):
    # Unsaved keys read as the stock body's, though each phone keeps its own for them.
    cal: Cal
    # False until the owner saves a key: the robot's phones still use what they each hold.
    saved: bool


class Step(BaseModel):
    l: int = Field(ge=0, le=180)  # noqa: E741 (the wire's own name)
    r: int = Field(ge=0, le=180)
    ms: int = Field(ge=50, le=2000)


class ActIn(BaseModel):
    # `act` plays `steps` (in the robot's own left/right); `wiggle` is the chip's one canned
    # routine; `stop` clears the queue and lets the legs go limp.
    kind: Literal["act", "wiggle", "stop"]
    steps: list[Step] = Field(default_factory=list, max_length=12)


class ActOut(BaseModel):
    sent: bool


class InternalCalOut(BaseModel):
    cal: dict[str, Any] | None


def _repo(request: Request) -> BodyRepo:
    return cast(BodyRepo, request.app.state.growbot_body)


def _owner_ctx(owner: OwnerDep) -> SessionContext:
    return SessionContext(principal_id=owner.id, principal_kind="owner")


def _no_robot() -> HTTPException:
    return HTTPException(status_code=404, detail="no such robot")


async def _read(request: Request, ctx: SessionContext, code: str) -> CalOut:
    found, stored = await _repo(request).get_cal(ctx, code)
    if not found:
        raise _no_robot()
    known = _known(stored or {})
    if not known:
        return CalOut(cal=Cal(), saved=False)
    # A row written by an older shape still reads: what fits is kept, the rest is default.
    return CalOut(cal=Cal.model_validate(known), saved=True)


def _known(stored: dict[str, Any]) -> dict[str, Any]:
    """The stored keys that still read, as stored: an older shape's misfits are dropped."""
    out: dict[str, Any] = {}
    for key, value in stored.items():
        if key in Cal.model_fields:
            try:
                Cal.model_validate({key: value})
            except ValueError:
                continue
            out[key] = value
    return out


async def _write(request: Request, ctx: SessionContext, code: str, cal: Cal) -> CalOut:
    # Only the keys the PWA sent are the box's: the rest stay each phone's own, so a first
    # change to one setting never wipes a trim a phone set in upstream's tester.
    found, stored = await _repo(request).get_cal(ctx, code)
    if not found:
        raise _no_robot()
    merged = {**_known(stored or {}), **cal.model_dump(mode="json", exclude_unset=True)}
    if not await _repo(request).set_cal(ctx, code, merged):
        raise _no_robot()
    return CalOut(cal=Cal.model_validate(merged), saved=True)


def _frame(body: ActIn, swap: bool) -> dict[str, Any]:
    if body.kind == "stop":
        return {"t": "stop"}
    if body.kind == "wiggle":
        return {"t": "routine", "name": "wiggle"}
    if not body.steps:
        raise HTTPException(status_code=422, detail="a move needs at least one step")
    # The swap is the robot's wiring, applied on the wire as the phone's relay-transport does,
    # so "left" here is the robot's left whichever port its left servo is plugged into.
    steps = [
        {"l": s.r, "r": s.l, "ms": s.ms} if swap else {"l": s.l, "r": s.r, "ms": s.ms}
        for s in body.steps
    ]
    return {"t": "act", "steps": steps, "mode": "replace"}


async def _act(request: Request, ctx: SessionContext, code: str, body: ActIn) -> ActOut:
    cal = await _read(request, ctx, code)
    frame = _frame(body, swap=bool(cal.saved and cal.cal.SWAP))
    resp = await _sidecar(request, "POST", "/internal/robots/act", {"code": code, "frame": frame})
    if resp is None or resp.status_code != 200:
        raise HTTPException(status_code=503, detail="the GrowBot service is not answering")
    if not resp.json().get("sent"):
        raise HTTPException(status_code=409, detail="the robot is not on the relay")
    return ActOut(sent=True)


@router.get("/robots/{code}/cal")
async def get_cal(request: Request, owner: OwnerDep, code: Code) -> CalOut:
    return await _read(request, _owner_ctx(owner), code)


@router.put("/robots/{code}/cal")
async def put_cal(request: Request, owner: OwnerDep, code: Code, body: Cal) -> CalOut:
    """Save the robot's calibration. A creature wearing it picks it up within seconds and
    uses it from its next move."""
    return await _write(request, _owner_ctx(owner), code, body)


@router.post("/robots/{code}/act")
async def act(request: Request, owner: OwnerDep, code: Code, body: ActIn) -> ActOut:
    """Move the legs now: 409 when the robot is not on the relay."""
    return await _act(request, _owner_ctx(owner), code, body)


@debug_router.get("/robots/{code}/cal")
async def debug_get_cal(request: Request, _p: DebugDep, code: Code) -> CalOut:
    request.state.debug_detail = f"growbot cal {code}"
    return await _read(request, _DEBUG_OWNER, code)


@debug_router.put("/robots/{code}/cal")
async def debug_put_cal(request: Request, _p: DebugDep, code: Code, body: Cal) -> CalOut:
    request.state.debug_detail = f"growbot set cal {code}"
    return await _write(request, _DEBUG_OWNER, code, body)


@internal_router.get("/robots/{code}/cal")
async def internal_cal(request: Request, code: Code) -> InternalCalOut:
    """What the creature page copies into `gb_walk_cal`, key by key: only the keys saved from
    the PWA, so the phone keeps its own for the rest; null when none were."""
    found, stored = await _repo(request).get_cal(_SYSTEM_OWNER, code)
    if not found:
        raise _no_robot()
    return InternalCalOut(cal=_known(stored or {}) or None)
