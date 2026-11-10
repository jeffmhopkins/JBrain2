"""GrowBot's access: the phone ticket and the robots' device keys (docs/plans/GROWBOT_PLAN.md
§2 "Access", wave G1). The model lane is `api/growbot_llm.py`.

Three routers over one repo (`jbrain.growbot.repo`):

- `router` — the owner (`OwnerDep`): `POST /growbot/ticket` mints a one-time, 120 s ticket and
  the `:8444` link that spends it; `POST`/`GET`/`DELETE /growbot/robots` mint or rotate a
  robot's device key (shown once), list the robots (with whether each is on the relay now),
  and forget one; `POST /growbot/sessions/reset` signs every phone out. Flashing, revoking and
  the status card are `api/growbot_flash.py` (G2).
- `debug_router` — a debug token (`DebugDep`, mounted only with debug access on): the same
  five, because the owner runs the box with no terminal (CLAUDE.md #10).
- `internal_router` — the GrowBot sidecar (`growbot.token`, mounted under `/internal`, which
  Caddy never routes): redeem a ticket, verify a chip's key.

The sidecar's checks run under the system owner context, the wall's `/internal/pet` shape:
the token has been checked, and the tables are owner-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from urllib.parse import quote, urlsplit

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, OwnerDep, SettingsDep
from jbrain.api.internal_auth import require_growbot
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.growbot.repo import CODE_RE, GrowbotRepo, RobotInfo
from jbrain.storage import InternalSecrets

log = structlog.get_logger()

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
    name: str = ""
    created_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None = None
    last_seen_at: datetime | None = None
    # On the relay right now, as the growbot sidecar reports it (False when it is down).
    connected: bool = False


def robot_out(info: RobotInfo, connected: set[str]) -> RobotOut:
    return RobotOut(
        code=info.code,
        name=info.name,
        created_at=info.created_at,
        rotated_at=info.rotated_at,
        revoked_at=info.revoked_at,
        last_seen_at=info.last_seen_at,
        connected=info.code in connected,
    )


# The growbot sidecar's /internal routes (growbot/server/serve.py), with growbot.token as the
# bearer. Tests swap in an httpx.MockTransport; None is the real network.
SIDECAR_TIMEOUT_S = 5.0
_transport: httpx.AsyncBaseTransport | None = None


async def _sidecar(
    request: Request, method: str, path: str, body: dict[str, str] | None = None
) -> httpx.Response | None:
    base = str(getattr(request.app.state.settings, "growbot_url", "") or "").rstrip("/")
    secrets = cast(InternalSecrets, request.app.state.growbot_secrets)
    token = secrets.read("growbot")
    if not base or not token:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=SIDECAR_TIMEOUT_S, transport=_transport, trust_env=False
        ) as client:
            return await client.request(
                method,
                base + path,
                json=body,
                headers={"Authorization": f"Bearer {token.decode()}"},
            )
    except httpx.HTTPError:
        return None


async def relay_robots(request: Request) -> set[str] | None:
    """The codes on the relay now, or None when the growbot sidecar is down or refuses."""
    resp = await _sidecar(request, "GET", "/internal/robots")
    if resp is None or resp.status_code != 200:
        return None
    try:
        rows = resp.json().get("robots")
    except (ValueError, AttributeError):
        return None
    if not isinstance(rows, list):
        return None
    return {str(r["code"]) for r in rows if isinstance(r, dict) and isinstance(r.get("code"), str)}


async def drop_robot(request: Request, code: str) -> bool:
    """Close `code`'s relay socket. Best effort: its key is already gone, so a robot the
    sidecar could not drop now is refused at its next hello."""
    resp = await _sidecar(request, "POST", "/internal/robots/drop", {"code": code})
    ok = resp is not None and resp.status_code == 200
    if not ok:
        log.warning("growbot.drop_failed", code=code)
    return ok


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
    code = body.code if body is not None and body.code else ""
    # The ticket itself names the robot: the sidecar signs the code redeem answers into the
    # phone's session, so editing `id` on the link reaches no other robot.
    minted = await _repo(request).mint_ticket(ctx, code)
    url = f"https://{host}:{GROWBOT_PORT}/open?ticket={quote(minted.ticket, safe='')}"
    if code:
        url += f"&id={quote(code, safe='')}"
    return TicketOut(ticket=minted.ticket, url=url, expires_at=minted.expires_at)


async def _put_robot(request: Request, ctx: SessionContext, body: RobotIn) -> RobotKeyOut:
    key = await _repo(request).put_robot(ctx, body.code)
    # A robot on the relay under the old key would stay connected on a key nothing holds.
    await drop_robot(request, body.code)
    return RobotKeyOut(code=body.code, key=key)


async def _list_robots(request: Request, ctx: SessionContext) -> list[RobotOut]:
    robots = await _repo(request).list_robots(ctx)
    connected = await relay_robots(request) if robots else set()
    return [robot_out(r, connected or set()) for r in robots]


async def _delete_robot(request: Request, ctx: SessionContext, code: str) -> Response:
    if not await _repo(request).delete_robot(ctx, code):
        raise HTTPException(status_code=404, detail="no such robot")
    await drop_robot(request, code)
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


# The sidecar keys every `gb_session` from this file too (growbot/server/serve.py `Sessions`),
# so re-minting it signs every phone out at once: the owner's way to end a lost phone's session
# without a terminal (CLAUDE.md #10).
SESSION_EPOCH = "growbot-session"


def _sign_out_phones(request: Request) -> Response:
    try:
        cast(InternalSecrets, request.app.state.growbot_secrets).rotate(SESSION_EPOCH)
    except OSError as exc:
        log.error("growbot.sign_out_failed", error=repr(exc))
        raise HTTPException(status_code=503, detail="could not sign the phones out") from exc
    log.info("growbot.phones_signed_out")
    return Response(status_code=204)


@router.post("/sessions/reset", status_code=204)
async def sign_out_phones(request: Request, owner: OwnerDep) -> Response:
    """End every phone's GrowBot session; each opens again from the PWA with a new ticket."""
    return _sign_out_phones(request)


@debug_router.post("/sessions/reset", status_code=204)
async def debug_sign_out_phones(request: Request, _p: DebugDep) -> Response:
    request.state.debug_detail = "growbot sign out phones"
    return _sign_out_phones(request)


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


class RedeemedOut(BaseModel):
    ok: bool
    # The robot the ticket was minted for, '' for none: the only robot the phone may join.
    code: str


def _refused() -> HTTPException:
    return HTTPException(status_code=401, detail="unauthorized")


# Far past any real ticket or key (43 characters each), short enough to hash for nothing.
_MAX_SECRET = 256


@internal_router.post("/ticket/redeem")
async def redeem_ticket(request: Request, body: RedeemIn) -> RedeemedOut:
    """Spend a phone's ticket: 200 with its robot the first time within its 120 s, 401 ever
    after."""
    if len(body.ticket) > _MAX_SECRET:
        raise _refused()
    code = await _repo(request).redeem_ticket(_SYSTEM_OWNER, body.ticket)
    if code is None:
        raise _refused()
    return RedeemedOut(ok=True, code=code)


@internal_router.post("/robot/verify")
async def verify_robot(request: Request, body: VerifyIn) -> OkOut:
    """Check the device key a chip presented on the relay for its pairing code."""
    if not CODE_RE.fullmatch(body.code) or len(body.key) > _MAX_SECRET:
        raise _refused()
    if not await _repo(request).verify_robot(_SYSTEM_OWNER, body.code, body.key):
        raise _refused()
    return OkOut(ok=True)
