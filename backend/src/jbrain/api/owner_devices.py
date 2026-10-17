"""The owner tunnel's phones: the bound session an enrolled phone mints, and the owner's
list of phones with rename and revoke (docs/plans/OWNER_TUNNEL_PLAN.md §3.4)."""

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.deps import AuthRepoDep, OwnerDep, SettingsDep
from jbrain.api.session_cookie import set_session_cookie
from jbrain.api.ws_reauth import kick
from jbrain.auth import service
from jbrain.auth.service import BOUND_SESSION_IDLE, PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.owner_tunnel.devices import OwnerDeviceInfo, OwnerDeviceRepo
from jbrain.owner_tunnel.guard import STATE_KEY, kick_device

router = APIRouter(prefix="/owner-devices")


def get_owner_device_repo(request: Request) -> OwnerDeviceRepo:
    return cast(OwnerDeviceRepo, request.app.state.owner_device_repo)


OwnerDeviceRepoDep = Annotated[OwnerDeviceRepo, Depends(get_owner_device_repo)]


def _ctx(owner: PrincipalInfo) -> SessionContext:
    return SessionContext(principal_id=owner.id, principal_kind="owner")


class DeviceOut(BaseModel):
    id: str
    label: str
    enrolled_at: datetime
    last_seen_at: datetime | None
    last_ingress: str | None
    revoked: bool

    @classmethod
    def of(cls, d: OwnerDeviceInfo) -> "DeviceOut":
        return cls(
            id=d.id,
            label=d.label,
            enrolled_at=d.enrolled_at,
            last_seen_at=d.last_seen_at,
            last_ingress=d.last_ingress,
            revoked=d.revoked_at is not None,
        )


class RenameIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)


@router.post("/session", status_code=204)
async def mint_session(
    request: Request, response: Response, repo: AuthRepoDep, settings: SettingsDep
) -> None:
    """The app's sign-in: an owner session bound to the phone this request came from.
    Only reachable through the owner site, where the guard has already matched the phone's
    verified key to an enrolled, unrevoked device."""
    device = cast(OwnerDeviceInfo | None, request.scope.get("state", {}).get(STATE_KEY))
    if device is None:
        raise HTTPException(status_code=403, detail="only from an enrolled phone")
    token = await service.mint_device_owner_session(repo, device.id, f"phone: {device.label}")
    if token is None:
        raise HTTPException(status_code=409, detail="no owner")
    set_session_cookie(
        response,
        settings,
        token,
        samesite="lax",
        max_age=int(BOUND_SESSION_IDLE.total_seconds()),
    )


@router.get("")
async def list_devices(owner: OwnerDep, devices: OwnerDeviceRepoDep) -> list[DeviceOut]:
    return [DeviceOut.of(d) for d in await devices.list_all(_ctx(owner))]


@router.patch("/{device_id}", status_code=204)
async def rename_device(
    device_id: str, body: RenameIn, owner: OwnerDep, devices: OwnerDeviceRepoDep
) -> None:
    label = body.label.strip()
    if not label:
        raise HTTPException(status_code=422, detail="label is blank")
    # The column's limit is in bytes, so a label of wide characters can pass the field's.
    if len(label.encode()) > 80:
        raise HTTPException(status_code=422, detail="label is too long")
    if not await devices.rename(_ctx(owner), device_id, label):
        raise HTTPException(status_code=404, detail="no such device")


@router.post("/{device_id}/revoke", status_code=204)
async def revoke_device(
    device_id: str, request: Request, owner: OwnerDep, devices: OwnerDeviceRepoDep
) -> None:
    if not await devices.revoke(_ctx(owner), device_id):
        raise HTTPException(status_code=404, detail="no such device")
    # Everything the phone has open ends now rather than at the next tick: its owner-site
    # requests and streams (kick_device), and sockets holding its sessions (kick).
    kick_device(request.app, device_id)
    kick(request.app, owner.id)
