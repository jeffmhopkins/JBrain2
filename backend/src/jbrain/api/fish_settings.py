"""Owner-only runtime control for the on-box fish-identification service (Wave F4).

The fish sibling of api/image_settings: report the fishial service's state (its
catalog models, what's provisioned on disk, and whether the model is currently
loaded) and free its memory on demand. Owner-gated at the router — host memory
control is never a capability-token action.

The defining difference from the image service: the fish model is load → use →
unload per identification, so there is no resident model to "load" on command and
the gateway reports only reachability + a transient `loaded` flag (no VRAM totals).
The only runtime action here is `free` (force-unload now); provisioning the weights
stays the deliberate scripts/fish-id-setup.sh step.
"""

from typing import Annotated, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jbrain.api.deps import SettingsDep, owner_only
from jbrain.config import Settings
from jbrain.fish_id import catalog, weights
from jbrain.fish_id.gateway import FishIdGatewayClient, FishIdGatewayError

# The compose service name this surface controls (the `fish-id` profile service).
FISH_SERVICE = "fish-id"

router = APIRouter(prefix="/settings/fish", dependencies=[Depends(owner_only)])


def get_fish_gateway(request: Request) -> FishIdGatewayClient | None:
    """The management client, or None when fish hosting isn't configured (main.py
    wires it only when fish_id_url is set, mirroring the fish_id client)."""
    return cast("FishIdGatewayClient | None", request.app.state.fish_id_gateway)


GatewayDep = Annotated["FishIdGatewayClient | None", Depends(get_fish_gateway)]


class FishModelInfo(BaseModel):
    """A catalog fish model for the settings drawer: what it is, whether it's offered,
    its nominal vs. real on-disk size, and its peak-footprint estimate."""

    id: str
    label: str
    arch: str
    enabled: bool
    recommended: bool
    size_gb: float
    # Measured on-disk size of the provisioned files, or null when not on this box.
    disk_gb: float | None
    # Peak unified-memory footprint estimate (drawn only while an identification runs).
    footprint_gb: float
    species_count: int
    note: str


class FishSettingsOut(BaseModel):
    # Off by default; the drawer still lists the catalog so the operator sees what
    # they could provision via scripts/fish-id-setup.sh.
    enabled: bool
    # Best-effort live reachability of the fishial service.
    reachable: bool
    # The model is TRANSIENTLY loaded right now (mid-identification) — drives the
    # dashed transient segment on the memory bar. False/idle is the normal state.
    loaded: bool
    models: list[FishModelInfo]


def _enabled(settings: Settings) -> bool:
    # The URL is the functional gate (main.py wires the clients on it).
    return bool(settings.fish_id_url)


def _model_info(settings: Settings, m: catalog.FishModel) -> FishModelInfo:
    enabled = _enabled(settings) and m.id in settings.fish_id_models
    disk = weights.weights_size_gb(settings.fish_id_models_dir, m) if _enabled(settings) else None
    return FishModelInfo(
        id=m.id,
        label=m.label,
        arch=m.arch,
        enabled=enabled,
        recommended=m.recommended,
        size_gb=m.size_gb,
        disk_gb=disk,
        footprint_gb=m.footprint_gb,
        species_count=m.species_count,
        note=m.note,
    )


async def _snapshot(settings: Settings, gateway: FishIdGatewayClient | None) -> FishSettingsOut:
    status = await gateway.status() if (gateway and _enabled(settings)) else None
    return FishSettingsOut(
        enabled=_enabled(settings),
        reachable=bool(status and status.reachable),
        loaded=bool(status and status.loaded),
        models=[_model_info(settings, m) for m in catalog.CATALOG],
    )


@router.get("")
async def read_fish_settings(settings: SettingsDep, gateway: GatewayDep) -> FishSettingsOut:
    return await _snapshot(settings, gateway)


@router.post("/free")
async def free_fish_memory(settings: SettingsDep, gateway: GatewayDep) -> FishSettingsOut:
    """Force-unload the model and free its memory now. 409 when fish hosting is off;
    502 if the service rejects or can't be reached. (The identify path already frees
    after every call — this is the manual escape hatch.)"""
    if not (gateway and _enabled(settings)):
        raise HTTPException(status_code=409, detail="fish hosting is not enabled")
    try:
        await gateway.free()
    except FishIdGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"fish free failed: {exc}") from exc
    return await _snapshot(settings, gateway)


class ServiceActionOut(BaseModel):
    service: str
    action: str  # "start" | "stop"


def _supervisor(request: Request) -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, request.app.state.supervisor_client)


def _sup_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.supervisor_token}"}


async def _toggle_service(request: Request, settings: Settings, action: str) -> ServiceActionOut:
    """Proxy a start/stop of the fish-id compose service to the supervisor (the only
    holder of the Docker socket). 409 when fish hosting is off; 404 when the service
    was never provisioned (no container to toggle)."""
    if not _enabled(settings):
        raise HTTPException(status_code=409, detail="fish hosting is not enabled")
    resp = await _supervisor(request).post(
        f"/{action}", json={"service": FISH_SERVICE}, headers=_sup_headers(settings)
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="fish-id service is not provisioned")
    resp.raise_for_status()
    return ServiceActionOut(service=FISH_SERVICE, action=action)


@router.post("/service/start", status_code=202)
async def start_fish_service(request: Request, settings: SettingsDep) -> ServiceActionOut:
    """Start the (provisioned, stopped) fish-id service via the supervisor."""
    return await _toggle_service(request, settings, "start")


@router.post("/service/stop", status_code=202)
async def stop_fish_service(request: Request, settings: SettingsDep) -> ServiceActionOut:
    """Stop the fish-id service via the supervisor (frees its memory by halting it)."""
    return await _toggle_service(request, settings, "stop")
