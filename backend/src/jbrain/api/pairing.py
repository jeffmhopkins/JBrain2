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
from jbrain.locations.pairing import PairingRepo, build_owntracks_config
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


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)


class RedeemOut(BaseModel):
    config: dict[str, Any]
    dashboard_url: str


@router.post("/pairing/codes", status_code=201)
async def mint_code(body: MintRequest, owner: OwnerDep, repo: PairingRepoDep) -> MintOut:
    code, expires_at = await repo.mint_code(
        _owner_ctx(owner), label=body.label, monitoring=body.monitoring
    )
    return MintOut(code=code, expires_at=expires_at)


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
