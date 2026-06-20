"""Owner-only runtime control for the on-box image service (Wave G5).

The image sibling of api/llm_settings: report the ComfyUI service's state (its
catalog models, what's provisioned on disk, and the real VRAM headroom it
reports) and free its memory on demand. Owner-gated at the router — host memory
control is never a capability-token action.

Read-only on provisioning: downloading weights stays the deliberate
scripts/comfyui-setup.sh step. The only runtime action here is `free` (unload),
because ComfyUI loads a model on the generation that needs it, not on command.
"""

from typing import Annotated, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jbrain.api.deps import SettingsDep, owner_only
from jbrain.config import Settings
from jbrain.image_gen import catalog, weights
from jbrain.image_gen.gateway import ComfyUiGatewayClient, ComfyUiGatewayError

# The compose service name this surface controls (the `comfyui` profile service).
COMFYUI_SERVICE = "comfyui"

router = APIRouter(prefix="/settings/image", dependencies=[Depends(owner_only)])


def get_comfyui_gateway(request: Request) -> ComfyUiGatewayClient | None:
    """The management client, or None when image hosting isn't configured (main.py
    wires it only when comfyui_url is set, mirroring the image_gen client)."""
    return cast("ComfyUiGatewayClient | None", request.app.state.comfyui_gateway)


GatewayDep = Annotated["ComfyUiGatewayClient | None", Depends(get_comfyui_gateway)]


class ImageModelInfo(BaseModel):
    """A catalog image model for the settings drawer: what it is, whether it's
    offered for routing, its nominal vs. real on-disk size, and its RAM estimate."""

    id: str
    label: str
    kind: str
    enabled: bool
    recommended: bool
    size_gb: float
    # Measured on-disk size of the provisioned files, or null when not on this box.
    disk_gb: float | None
    # Resident unified-memory footprint estimate (the RAM-budget reservation).
    vram_gb: float
    note: str


class ImageMemory(BaseModel):
    """Real VRAM/unified-memory gauge straight from ComfyUI's /system_stats — the
    true headroom for loading an image model beside a local LLM. None when the
    service is unreachable or didn't report it."""

    total_gb: float
    free_gb: float


class ImageSettingsOut(BaseModel):
    # Off by default; the drawer still lists the catalog so the operator sees what
    # they could provision via scripts/comfyui-setup.sh.
    enabled: bool
    # Best-effort live reachability of the ComfyUI service.
    reachable: bool
    models: list[ImageModelInfo]
    memory: ImageMemory | None = None


def _enabled(settings: Settings) -> bool:
    # The URL is the functional gate (main.py wires the clients on it).
    return bool(settings.comfyui_url)


def _model_info(settings: Settings, m: catalog.ImageModel) -> ImageModelInfo:
    enabled = _enabled(settings) and m.id in settings.comfyui_models
    disk = weights.weights_size_gb(settings.comfyui_models_dir, m) if _enabled(settings) else None
    return ImageModelInfo(
        id=m.id,
        label=m.label,
        kind=m.kind,
        enabled=enabled,
        recommended=m.recommended,
        size_gb=m.size_gb,
        disk_gb=disk,
        vram_gb=m.vram_gb,
        note=m.note,
    )


async def _snapshot(settings: Settings, gateway: ComfyUiGatewayClient | None) -> ImageSettingsOut:
    status = await gateway.status() if (gateway and _enabled(settings)) else None
    memory = (
        ImageMemory(total_gb=status.vram_total_gb, free_gb=status.vram_free_gb)
        if status and status.vram_total_gb is not None and status.vram_free_gb is not None
        else None
    )
    return ImageSettingsOut(
        enabled=_enabled(settings),
        reachable=bool(status and status.reachable),
        models=[_model_info(settings, m) for m in catalog.CATALOG],
        memory=memory,
    )


@router.get("")
async def read_image_settings(settings: SettingsDep, gateway: GatewayDep) -> ImageSettingsOut:
    return await _snapshot(settings, gateway)


@router.post("/free")
async def free_image_memory(settings: SettingsDep, gateway: GatewayDep) -> ImageSettingsOut:
    """Unload cached models and free the service's memory. 409 when image hosting
    is off; 502 if ComfyUI rejects or can't be reached."""
    if not (gateway and _enabled(settings)):
        raise HTTPException(status_code=409, detail="image hosting is not enabled")
    try:
        await gateway.free()
    except ComfyUiGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"comfyui free failed: {exc}") from exc
    return await _snapshot(settings, gateway)


class ServiceActionOut(BaseModel):
    service: str
    action: str  # "start" | "stop"


def _supervisor(request: Request) -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, request.app.state.supervisor_client)


def _sup_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.supervisor_token}"}


async def _toggle_service(request: Request, settings: Settings, action: str) -> ServiceActionOut:
    """Proxy a start/stop of the comfyui compose service to the supervisor (the
    only holder of the Docker socket). 409 when image hosting is off; 404 when the
    service was never provisioned (no container to toggle)."""
    if not _enabled(settings):
        raise HTTPException(status_code=409, detail="image hosting is not enabled")
    resp = await _supervisor(request).post(
        f"/{action}", json={"service": COMFYUI_SERVICE}, headers=_sup_headers(settings)
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="comfyui service is not provisioned")
    resp.raise_for_status()
    return ServiceActionOut(service=COMFYUI_SERVICE, action=action)


@router.post("/service/start", status_code=202)
async def start_image_service(request: Request, settings: SettingsDep) -> ServiceActionOut:
    """Start the (provisioned, stopped) ComfyUI service via the supervisor."""
    return await _toggle_service(request, settings, "start")


@router.post("/service/stop", status_code=202)
async def stop_image_service(request: Request, settings: SettingsDep) -> ServiceActionOut:
    """Stop the ComfyUI service via the supervisor (frees its memory by halting it)."""
    return await _toggle_service(request, settings, "stop")
