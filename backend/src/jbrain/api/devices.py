"""Owner-only device management: provision / rotate / revoke / list (Phase 7).

The plaintext key is returned exactly once, on provision and rotate. Every route
is `OwnerDep`-gated, and the service runs under the owner's `SessionContext`, so
the `subjects`/`principals` RLS owner-only write policies are the real barrier.
"""

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import OwnerDep
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.devices import service
from jbrain.devices.repo import DeviceInfo, DeviceRepo

router = APIRouter()


def get_device_repo(request: Request) -> DeviceRepo:
    return cast(DeviceRepo, request.app.state.device_repo)


DeviceRepoDep = Annotated[DeviceRepo, Depends(get_device_repo)]


def _owner_ctx(owner: PrincipalInfo) -> SessionContext:
    return SessionContext(principal_id=owner.id, principal_kind="owner")


class DeviceOut(BaseModel):
    id: str
    label: str
    created_at: datetime
    revoked: bool


class ProvisionRequest(BaseModel):
    label: str = Field(min_length=1, max_length=128)


class ProvisionedOut(BaseModel):
    device: DeviceOut
    # Shown once. Configure OwnTracks (HTTP mode) with this as the Basic password.
    key: str


class RotatedOut(BaseModel):
    key: str


def _device_out(d: DeviceInfo) -> DeviceOut:
    return DeviceOut(id=d.id, label=d.label, created_at=d.created_at, revoked=d.revoked)


@router.post("/devices", status_code=201)
async def create_device(
    body: ProvisionRequest, owner: OwnerDep, repo: DeviceRepoDep
) -> ProvisionedOut:
    provisioned = await service.provision_device(repo, _owner_ctx(owner), body.label)
    return ProvisionedOut(device=_device_out(provisioned.device), key=provisioned.key)


@router.get("/devices")
async def list_devices(owner: OwnerDep, repo: DeviceRepoDep) -> list[DeviceOut]:
    return [_device_out(d) for d in await repo.list(_owner_ctx(owner))]


@router.post("/devices/{device_id}/rotate")
async def rotate_device(device_id: str, owner: OwnerDep, repo: DeviceRepoDep) -> RotatedOut:
    key = await service.rotate_device_key(repo, _owner_ctx(owner), device_id)
    if key is None:
        raise HTTPException(status_code=404, detail="device not found")
    return RotatedOut(key=key)


@router.post("/devices/{device_id}/revoke", status_code=204)
async def revoke_device(device_id: str, owner: OwnerDep, repo: DeviceRepoDep) -> None:
    if not await service.revoke_device(repo, _owner_ctx(owner), device_id):
        raise HTTPException(status_code=404, detail="device not found")
