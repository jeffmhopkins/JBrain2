"""GrowBot's access: the phone ticket and the robots' device keys (docs/plans/GROWBOT_PLAN.md
§2 "Access", wave G1). The model lane is `api/growbot_llm.py`.

Three routers over one repo (`jbrain.growbot.repo`):

- `router` — the owner (`OwnerDep`): `POST /growbot/ticket` mints a one-time, 120 s ticket and
  the `:8444` link that spends it; `POST`/`GET`/`DELETE /growbot/robots` mint or rotate a
  robot's device key (shown once), list the robots, and forget one.
- `debug_router` — a debug token (`DebugDep`, mounted only with debug access on): the same
  four, because G1 has no GUI and the owner runs the box with no terminal (CLAUDE.md #10).
- `internal_router` — the GrowBot sidecar (`growbot.token`, mounted under `/internal`, which
  Caddy never routes): redeem a ticket, verify a chip's key.

The sidecar's checks run under the system owner context, the wall's `/internal/pet` shape:
the token has been checked, and the tables are owner-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, OwnerDep, SettingsDep
from jbrain.api.internal_auth import require_growbot
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.growbot.repo import CODE_RE, GrowbotRepo

router = APIRouter(prefix="/growbot", tags=["growbot"])
debug_router = APIRouter(prefix="/debug/growbot")
internal_router = APIRouter(prefix="/growbot", dependencies=[Depends(require_growbot)])

# The LAN site Caddy serves the creature on (deploy/proxy-lan-conf.sh): the box's own LAN
# name over its `tls internal` CA, on a port of its own so the app is its own origin.
GROWBOT_PORT = 8444
_SYSTEM_OWNER = SessionContext(principal_kind="owner")
_DEBUG_OWNER = SessionContext(principal_id="debug-console", principal_kind="owner")
_CODE = r"^gb-[A-Za-z0-9]{1,32}$"

Code = Annotated[str, Path(pattern=_CODE)]


def _repo(request: Request) -> GrowbotRepo:
    return cast(GrowbotRepo, request.app.state.growbot_repo)


def _owner_ctx(owner: OwnerDep) -> SessionContext:
    return SessionContext(principal_id=owner.id, principal_kind="owner")


class TicketIn(BaseModel):
    # The robot the phone should drive; the link opens the creature on it. Not bound to the
    # ticket: a redeemed session may drive any of the owner's robots, all of which are theirs.
    code: str | None = Field(default=None, pattern=_CODE)


class TicketOut(BaseModel):
    ticket: str
    url: str
    expires_at: datetime


class RobotIn(BaseModel):
    code: str = Field(pattern=_CODE)


class RobotKeyOut(BaseModel):
    code: str
    key: str


class RobotOut(BaseModel):
    code: str
    created_at: datetime
    rotated_at: datetime | None


def _lan_host(settings: Settings) -> str:
    host = urlsplit(settings.lan_addr.strip()).hostname or ""
    if not host:
        # No LAN site, so Caddy serves no :8444 either: a link would open nothing.
        raise HTTPException(
            status_code=409, detail="GrowBot needs the box's LAN site (JBRAIN_LAN_ADDR)"
        )
    return host


async def _ticket(
    request: Request, settings: Settings, ctx: SessionContext, body: TicketIn | None
) -> TicketOut:
    host = _lan_host(settings)
    minted = await _repo(request).mint_ticket(ctx)
    url = f"https://{host}:{GROWBOT_PORT}/open?ticket={quote(minted.ticket, safe='')}"
    if body is not None and body.code:
        url += f"&id={quote(body.code, safe='')}"
    return TicketOut(ticket=minted.ticket, url=url, expires_at=minted.expires_at)


async def _put_robot(request: Request, ctx: SessionContext, body: RobotIn) -> RobotKeyOut:
    return RobotKeyOut(code=body.code, key=await _repo(request).put_robot(ctx, body.code))


async def _list_robots(request: Request, ctx: SessionContext) -> list[RobotOut]:
    robots = await _repo(request).list_robots(ctx)
    return [RobotOut(code=r.code, created_at=r.created_at, rotated_at=r.rotated_at) for r in robots]


async def _delete_robot(request: Request, ctx: SessionContext, code: str) -> Response:
    if not await _repo(request).delete_robot(ctx, code):
        raise HTTPException(status_code=404, detail="no such robot")
    return Response(status_code=204)


@router.post("/ticket")
async def mint_ticket(
    request: Request, owner: OwnerDep, settings: SettingsDep, body: TicketIn | None = None
) -> TicketOut:
    """A one-time ticket that opens GrowBot on a phone, and the link that spends it."""
    return await _ticket(request, settings, _owner_ctx(owner), body)


@router.post("/robots")
async def put_robot(request: Request, owner: OwnerDep, body: RobotIn) -> RobotKeyOut:
    """Mint `code`'s device key, or rotate it (the old key stops working). Shown only here."""
    return await _put_robot(request, _owner_ctx(owner), body)


@router.get("/robots")
async def list_robots(request: Request, owner: OwnerDep) -> list[RobotOut]:
    return await _list_robots(request, _owner_ctx(owner))


@router.delete("/robots/{code}", status_code=204)
async def delete_robot(request: Request, owner: OwnerDep, code: Code) -> Response:
    return await _delete_robot(request, _owner_ctx(owner), code)


@debug_router.post("/ticket")
async def debug_mint_ticket(
    request: Request, _p: DebugDep, settings: SettingsDep, body: TicketIn | None = None
) -> TicketOut:
    request.state.debug_detail = "growbot ticket"
    return await _ticket(request, settings, _DEBUG_OWNER, body)


@debug_router.post("/robots")
async def debug_put_robot(request: Request, _p: DebugDep, body: RobotIn) -> RobotKeyOut:
    request.state.debug_detail = f"growbot robot {body.code}"
    return await _put_robot(request, _DEBUG_OWNER, body)


@debug_router.get("/robots")
async def debug_list_robots(request: Request, _p: DebugDep) -> list[RobotOut]:
    request.state.debug_detail = "growbot robots"
    return await _list_robots(request, _DEBUG_OWNER)


@debug_router.delete("/robots/{code}", status_code=204)
async def debug_delete_robot(request: Request, _p: DebugDep, code: Code) -> Response:
    request.state.debug_detail = f"growbot forget {code}"
    return await _delete_robot(request, _DEBUG_OWNER, code)


# Lenient bodies: any JSON object short of a right answer is a 401, never a 422 the sidecar
# would have to tell apart from a refusal. (Only a body that is not JSON at all is FastAPI's
# 422, and the sidecar never sends one.)
class RedeemIn(BaseModel):
    ticket: str = ""


class VerifyIn(BaseModel):
    code: str = ""
    key: str = ""


class OkOut(BaseModel):
    ok: bool


def _refused() -> HTTPException:
    return HTTPException(status_code=401, detail="unauthorized")


# Far past any real ticket or key (43 characters each), short enough to hash for nothing.
_MAX_SECRET = 256


@internal_router.post("/ticket/redeem")
async def redeem_ticket(request: Request, body: RedeemIn) -> OkOut:
    """Spend a phone's ticket: 200 the first time within its 120 s, 401 ever after."""
    if len(body.ticket) > _MAX_SECRET:
        raise _refused()
    if not await _repo(request).redeem_ticket(_SYSTEM_OWNER, body.ticket):
        raise _refused()
    return OkOut(ok=True)


@internal_router.post("/robot/verify")
async def verify_robot(request: Request, body: VerifyIn) -> OkOut:
    """Check the device key a chip presented on the relay for its pairing code."""
    if not CODE_RE.fullmatch(body.code) or len(body.key) > _MAX_SECRET:
        raise _refused()
    if not await _repo(request).verify_robot(_SYSTEM_OWNER, body.code, body.key):
        raise _refused()
    return OkOut(ok=True)
