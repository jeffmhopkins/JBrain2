"""Flashing a room-endpoint panel from the PWA, because the owner has no terminal.

The owner runs this box remotely (`CLAUDE.md` #10). A panel they plug into its USB port
is therefore only reachable through a button — there is no shell to run `esptool` in, and
the debug console's `host.read` scope reports memory and processes, not device nodes. So
the whole flashing path is an API surface, and the most useful thing it does is the least
impressive one: `GET /endpoint/ports` answers "can the box see the panel at all", which is
otherwise unanswerable from where the owner sits.

WHAT CROSSES WHICH BOUNDARY. The sidecar (`deploy/endpoint`) owns the device and knows
nothing else: it never fetches firmware and has no route off the box. This module owns
everything secret — the Wi-Fi credentials, the per-unit device token, the box's own Caddy
root — and passes them per request, so the published firmware artifact stays generic and
credential-free and no GitHub credential is needed on the box at all.

Owner-only, with one exception that has to exist: `GET /endpoint/firmware` is also
reachable by a `device_key` principal, because that is the manifest a flashed panel polls.
Reaching it is ALSO the panel's health signal — the firmware marks a new image good only
once it has fetched this, since the only unrecoverable state is one an OTA cannot reach
(firmware/README.md). It returns a version and a URL and nothing else; a panel is not
allowed to learn anything about this box beyond what firmware it should be running.

Rules: no LLM (#1 n/a). Firmware images go through `BlobStore` (#2). The manifest's
metadata lives in `app.settings`, whose owner-only RLS this inherits — no new table, so no
new isolation test to add (#3); the store's own docstring is explicit that a new key is a
constant rather than a migration.
"""

from __future__ import annotations

import base64
import io
import zipfile
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from jbrain.api.deps import OwnerDep, PrincipalDep, SettingsDep
from jbrain.api.devices import DeviceRepoDep
from jbrain.api.notes import BlobStoreDep, ctx_for
from jbrain.devices import service as devices
from jbrain.settings_store import SqlSettingsStore

router = APIRouter(prefix="/endpoint", tags=["endpoint"])

# The manifest, as `app.settings` holds it: {"version": str, "images": {offset: sha256}}.
FIRMWARE_KEY = "endpoint_firmware"

# Where each image is written, matching `firmware/partitions.csv`. Hardcoded rather than
# taken from the request because these offsets are the layout of a device the owner cannot
# recover without a cable: a caller-supplied otadata offset is not a feature.
BOOTLOADER_OFFSET = "0x0"
PARTITION_TABLE_OFFSET = "0x8000"
OTA_DATA_OFFSET = "0xf000"
APP_OFFSET = "0x20000"
NVS_OFFSET = "0x9000"
NVS_SIZE = "0x6000"

# Which file in the CI artifact goes where. The artifact is what
# `.github/workflows/firmware.yml` publishes; a zip missing any of these is rejected with
# the names it did contain, since the alternative is a flash that half-writes a board.
ARTIFACT_IMAGES = {
    "bootloader.bin": BOOTLOADER_OFFSET,
    "partition-table.bin": PARTITION_TABLE_OFFSET,
    "jbrain-endpoint.bin": APP_OFFSET,
}

# A whole firmware set is ~1 MB. The cap is about a zip bomb, not about firmware.
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024

SIDECAR_TIMEOUT_S = 600.0

# Caddy's internal-CA root, as the read-only `caddy_data` mount exposes it. A panel needs
# it to validate https://jbrain.local, and reading it here is what stops the owner needing
# the `docker cp` that docs/runbooks/LOCAL_ACCESS.md otherwise requires (CLAUDE.md #10).
CADDY_ROOT_PATH = "/data/caddy/caddy/pki/authorities/local/root.crt"


def _lan_ca() -> str:
    """The box's own root certificate, or "" when this box has no LAN site.

    Empty is a real state rather than a failure: a box reached only through the tunnel has
    a publicly-trusted certificate and no internal CA to distribute. The panel is told so
    by getting no `ca` key, and a flash against a tunnel URL still works.
    """
    try:
        with open(CADDY_ROOT_PATH, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _store(request: Request) -> SqlSettingsStore:
    return SqlSettingsStore(request.app.state.session_maker)


def _sidecar(settings: SettingsDep) -> str:
    base = settings.endpoint_url.strip().rstrip("/")
    if not base:
        # Not an error the owner caused. The flasher is an opt-in compose profile, and
        # saying so is more use than a 500 on a box that simply never enabled it.
        raise HTTPException(
            status_code=503,
            detail="No panel flasher on this box — the `endpoint` compose profile is off.",
        )
    return base


class PortOut(BaseModel):
    device: str
    label: str
    is_espressif: bool


class PortsOut(BaseModel):
    ports: list[PortOut]
    # Separate from an empty list because they mean different things to the owner: no
    # flasher is a configuration answer, no ports is a "did you plug it in" answer.
    flasher: bool = True


@router.get("/ports")
async def list_ports(_owner: OwnerDep, settings: SettingsDep) -> PortsOut:
    """What the box can see on USB right now.

    The first thing worth running after plugging a panel in, and the only way to tell a
    panel that is not enumerating from a container that cannot see it.
    """
    base = _sidecar(settings)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{base}/ports")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"flasher unreachable: {exc}") from exc
    return PortsOut(ports=[PortOut(**p) for p in resp.json().get("ports", [])])


class FirmwareOut(BaseModel):
    version: str
    url: str


@router.get("/firmware")
async def firmware_manifest(principal: PrincipalDep, request: Request) -> FirmwareOut:
    """What a panel should be running — the one route a flashed panel itself calls.

    Reaching this is the health signal the firmware's rollback gate turns on, so it is
    deliberately cheap and says nothing about this box beyond a version and a URL.
    """
    if principal.kind not in ("owner", "device_key"):
        raise HTTPException(status_code=403, detail="not permitted")
    stored = await _store(request).get(ctx_for(principal), FIRMWARE_KEY)
    if not stored:
        raise HTTPException(status_code=404, detail="no firmware uploaded yet")
    return FirmwareOut(
        version=str(stored["version"]), url=f"{_public_base(request)}/endpoint/firmware/bin"
    )


def _public_base(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api"


@router.post("/firmware")
async def upload_firmware(
    owner: OwnerDep,
    request: Request,
    blobs: BlobStoreDep,
    version: Annotated[str, Field(min_length=1, max_length=48)],
    artifact: UploadFile,
) -> FirmwareOut:
    """Take the CI artifact zip and keep it, so a flash and an OTA serve the same bytes.

    The owner downloads `endpoint-firmware` from the `firmware` workflow run and picks it
    here. That is one manual hop, and it buys something worth more than automating it: no
    GitHub credential on this box, and no path by which this box fetches and then executes
    something from the internet.
    """
    raw = await artifact.read(MAX_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="artifact too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="not a zip archive") from exc

    # Match on the base name: the workflow publishes them flat, but a zip downloaded from
    # the Actions UI can carry a directory prefix depending on how it was made.
    found: dict[str, bytes] = {}
    for info in zf.infolist():
        name = info.filename.rsplit("/", 1)[-1]
        if name in ARTIFACT_IMAGES and name not in found:
            found[name] = zf.read(info)

    missing = sorted(set(ARTIFACT_IMAGES) - set(found))
    if missing:
        present = sorted({i.filename.rsplit("/", 1)[-1] for i in zf.infolist()})
        raise HTTPException(
            status_code=400,
            detail=f"artifact is missing {missing}; it contains {present}",
        )

    images = {ARTIFACT_IMAGES[name]: await blobs.put(data) for name, data in found.items()}
    stored = {"version": version, "images": images}
    await _store(request).upsert(ctx_for(owner), FIRMWARE_KEY, stored)
    return FirmwareOut(version=version, url=f"{_public_base(request)}/endpoint/firmware/bin")


class FlashIn(BaseModel):
    port: str
    ssid: str
    password: str
    # Which twin's panel this is. Carried into NVS so a log line names a unit rather than
    # a serial port that changes between plugs.
    name: str = ""
    erase: bool = False


@router.post("/flash")
async def flash_panel(
    owner: OwnerDep,
    request: Request,
    settings: SettingsDep,
    blobs: BlobStoreDep,
    device_repo: DeviceRepoDep,
    body: FlashIn,
) -> StreamingResponse:
    """Write the stored firmware plus this unit's own config to a panel.

    Streams the flasher's log straight through: it takes tens of seconds, and an owner
    watching a PWA has no other signal that anything is happening.
    """
    base = _sidecar(settings)
    stored = await _store(request).get(ctx_for(owner), FIRMWARE_KEY)
    if not stored:
        raise HTTPException(status_code=409, detail="upload a firmware artifact first")

    images = [
        {"offset": offset, "b64": base64.b64encode(await blobs.get(sha)).decode()}
        for offset, sha in sorted(stored["images"].items(), key=lambda kv: int(kv[0], 16))
    ]

    # A fresh device identity per flash, on the shipped `device_key` substrate rather than
    # a new auth model (ROOM_ENDPOINT_PLAN.md §3). The plaintext key exists only inside
    # this request: it goes into NVS and is never stored here, which is the same contract
    # the owner's own key rotation has. Re-flashing a panel therefore issues a NEW
    # identity — correct, because a re-flash is how a unit is handed over or recovered,
    # and the old key should stop working at that moment.
    label = f"panel {body.name}".strip() if body.name else "room endpoint panel"
    provisioned = await devices.provision_device(device_repo, ctx_for(owner), label)

    nvs = {
        "ssid": body.ssid,
        "pass": body.password,
        "api": _public_base(request),
        "token": provisioned.key,
        "name": body.name,
    }
    ca = _lan_ca()
    if ca:
        nvs["ca"] = ca

    payload: dict[str, Any] = {
        "port": body.port,
        "images": images,
        "nvs": nvs,
        "nvs_offset": NVS_OFFSET,
        "nvs_size": NVS_SIZE,
        "erase": body.erase,
    }

    async def stream() -> AsyncIterator[bytes]:
        async with (
            httpx.AsyncClient(timeout=SIDECAR_TIMEOUT_S) as client,
            client.stream("POST", f"{base}/flash", json=payload) as resp,
        ):
            async for chunk in resp.aiter_bytes():
                yield chunk

    return StreamingResponse(
        stream(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
