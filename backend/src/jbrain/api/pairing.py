"""Pairing: the owner mints a one-time code; a device redeems it for its config.

`POST /api/pairing/codes` is OwnerDep-gated. `POST /api/pairing/redeem` is
unauthenticated (the device has no principal yet) and IP-rate-limited against code
brute-force; an invalid/expired/used code is a flat 400 (no oracle on *why*).
"""

from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import OwnerDep
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.locations.pairing import (
    PairingRepo,
    build_owntracks_config,
    build_pairing_payload,
)
from jbrain.locations.ratelimit import TokenBucket

router = APIRouter()

# OwnTracks monitoring modes: -1 Quiet, 0 Manual, 1 Significant, 2 Move.
_MIN_MODE, _MAX_MODE = -1, 2


def get_pairing_repo(request: Request) -> PairingRepo:
    return cast(PairingRepo, request.app.state.pairing_repo)


def get_pairing_limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.pairing_rate_limiter)


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


PairingRepoDep = Annotated[PairingRepo, Depends(get_pairing_repo)]
PairingLimiterDep = Annotated[TokenBucket, Depends(get_pairing_limiter)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _owner_ctx(owner: PrincipalInfo) -> SessionContext:
    return SessionContext(principal_id=owner.id, principal_kind="owner")


class MintRequest(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    monitoring: int = Field(default=1, ge=_MIN_MODE, le=_MAX_MODE)


class MintOut(BaseModel):
    code: str
    expires_at: datetime
    # The self-contained string to share (copy or QR): embeds the server URL + the
    # code, so the app needs nothing baked in — it pulls the server from this.
    payload: str


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)


class RedeemOut(BaseModel):
    config: dict[str, Any]
    dashboard_url: str


def _public_base(request: Request, settings: Settings) -> str:
    """The server's public base URL to embed in the pairing payload. The configured
    `dashboard_url` is the source of truth; if unset, fall back to the origin the
    owner's browser actually hit (it minted from the real public host)."""
    if settings.dashboard_url:
        return settings.dashboard_url.rstrip("/")
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    host = request.headers.get("host", request.url.netloc)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{host}".rstrip("/")


@router.post("/pairing/codes", status_code=201)
async def mint_code(
    body: MintRequest,
    request: Request,
    owner: OwnerDep,
    repo: PairingRepoDep,
    settings: SettingsDep,
) -> MintOut:
    code, expires_at = await repo.mint_code(
        _owner_ctx(owner), label=body.label, monitoring=body.monitoring
    )
    payload = build_pairing_payload(_public_base(request, settings), code)
    return MintOut(code=code, expires_at=expires_at, payload=payload)


@router.post("/pairing/redeem")
async def redeem(
    body: RedeemRequest,
    request: Request,
    repo: PairingRepoDep,
    limiter: PairingLimiterDep,
    settings: SettingsDep,
) -> RedeemOut:
    client = request.client.host if request.client else "unknown"
    if not limiter.allow(client):
        raise HTTPException(status_code=429, detail="rate limited")
    device = await repo.redeem(body.code)
    if device is None:
        raise HTTPException(status_code=400, detail="invalid or expired code")
    config = build_owntracks_config(
        device, broker_host=settings.mqtt_public_host, broker_port=settings.mqtt_public_port
    )
    return RedeemOut(config=config, dashboard_url=settings.dashboard_url)
