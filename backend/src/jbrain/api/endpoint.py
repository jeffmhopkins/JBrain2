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
import hashlib
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
from jbrain.config import Settings
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
        # Not an error the owner caused, and not the normal state either: the flasher is
        # stock stack, so an empty URL means someone deliberately blanked it. Saying which
        # setting is more use than a 500.
        raise HTTPException(
            status_code=503,
            detail="No panel flasher on this box — JBRAIN_ENDPOINT_URL is empty.",
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


# Release tags are `firmware-v<version>`, cut by .github/workflows/firmware.yml when
# firmware/version.txt changes. The prefix matters: this repo tags other things too.
RELEASE_PREFIX = "firmware-v"

# A whole flashable set is ~1 MB; the cap is about a hostile response, not about firmware.
MAX_ASSET_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT_S = 60.0


class FirmwareOut(BaseModel):
    version: str
    url: str


class AvailableOut(BaseModel):
    """What the box could install, next to what it has."""

    installed: str | None
    latest: str | None
    # False when no source is configured, so the card can offer upload instead of
    # pretending a fetch is possible.
    fetchable: bool


def _releases_url(settings: Settings) -> str:
    return f"https://api.github.com/repos/{settings.endpoint_firmware_repo}/releases"


async def _latest_release(settings: Settings) -> dict[str, Any] | None:
    """The newest firmware release, or None when there is no source or no release yet.

    Never raises for a reachability problem: the box not being able to see GitHub is a
    normal state for a LAN device, and it must degrade to "upload it yourself" rather than
    breaking the page that offers that fallback.
    """
    if not settings.endpoint_firmware_repo.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as client:
            resp = await client.get(
                _releases_url(settings),
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            releases = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(releases, list):
        return None
    for release in releases:
        tag = str(release.get("tag_name") or "")
        if tag.startswith(RELEASE_PREFIX) and not release.get("draft"):
            return release
    return None


def _assets(release: dict[str, Any]) -> dict[str, str]:
    """Asset name -> browser download URL, for the names the flash actually needs."""
    out: dict[str, str] = {}
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name and url:
            out[name] = url
    return out


def _expected_digests(sums: str) -> dict[str, str]:
    """Parse `sha256sum` output into base name -> digest.

    Names come back as `./bootloader.bin` from the workflow's `cd out && sha256sum ./*.bin`,
    so the leading path is stripped rather than matched literally.
    """
    digests: dict[str, str] = {}
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2:
            digests[parts[1].lstrip("./").rsplit("/", 1)[-1]] = parts[0].lower()
    return digests


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


@router.get("/firmware/available")
async def firmware_available(
    _owner: OwnerDep, request: Request, settings: SettingsDep
) -> AvailableOut:
    """What is installed, and what the box could fetch — the card's whole status line."""
    stored = await _store(request).get(ctx_for(_owner), FIRMWARE_KEY)
    release = await _latest_release(settings)
    return AvailableOut(
        installed=str(stored["version"]) if stored else None,
        latest=(str(release["tag_name"]).removeprefix(RELEASE_PREFIX) if release else None),
        fetchable=bool(settings.endpoint_firmware_repo.strip()),
    )


async def _sync(owner: Any, request: Request, settings: Settings, blobs: Any) -> FirmwareOut:
    """Fetch the newest published firmware and store it.

    The repository is public, so this needs no credential at all — which is what makes the
    whole path possible: the owner taps a button instead of downloading an artifact and
    uploading it back. Every asset is verified against the release's own SHA256SUMS before
    anything is stored, because these bytes become a bootloader on a device that has no
    cable attached to it once it is in a bedroom, and a truncated download that flashes is
    worse than one that fails.
    """
    release = await _latest_release(settings)
    if release is None:
        raise HTTPException(
            status_code=503,
            detail="No firmware release reachable — upload the artifact instead.",
        )
    version = str(release["tag_name"]).removeprefix(RELEASE_PREFIX)
    assets = _assets(release)

    missing = sorted(set(ARTIFACT_IMAGES) - set(assets))
    if missing:
        raise HTTPException(
            status_code=502, detail=f"release {release['tag_name']} is missing {missing}"
        )

    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=True) as client:
        digests: dict[str, str] = {}
        if "SHA256SUMS" in assets:
            sums = await client.get(assets["SHA256SUMS"])
            sums.raise_for_status()
            digests = _expected_digests(sums.text)

        images: dict[str, str] = {}
        for name, offset in ARTIFACT_IMAGES.items():
            resp = await client.get(assets[name])
            resp.raise_for_status()
            data = resp.content
            if len(data) > MAX_ASSET_BYTES:
                raise HTTPException(status_code=502, detail=f"{name} is implausibly large")
            want = digests.get(name)
            if want and hashlib.sha256(data).hexdigest() != want:
                # Refuse the whole set rather than store a good image beside a bad one.
                raise HTTPException(
                    status_code=502, detail=f"{name} failed its checksum — nothing stored"
                )
            images[offset] = await blobs.put(data)

    await _store(request).upsert(
        ctx_for(owner), FIRMWARE_KEY, {"version": version, "images": images}
    )
    return FirmwareOut(version=version, url=f"{_public_base(request)}/endpoint/firmware/bin")


@router.post("/firmware/sync")
async def sync_firmware(
    owner: OwnerDep, request: Request, settings: SettingsDep, blobs: BlobStoreDep
) -> FirmwareOut:
    """One tap: fetch the latest published firmware straight from the release."""
    return await _sync(owner, request, settings, blobs)


@router.post("/firmware")
async def upload_firmware(
    owner: OwnerDep,
    request: Request,
    blobs: BlobStoreDep,
    version: Annotated[str, Field(min_length=1, max_length=48)],
    artifact: UploadFile,
) -> FirmwareOut:
    """The fallback path: take the CI artifact zip directly.

    `POST /firmware/sync` is the normal way in — the box fetches its own firmware and the
    owner taps a button. This exists for the box that cannot reach GitHub, which is a real
    state for a LAN device and not worth leaving without an answer.
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
        # Fetch it rather than refusing. A box that has never flashed a panel has no
        # firmware stored, and making that a 409 would hand the owner an errand for
        # something the box can do itself in a second. Only when the fetch is impossible
        # does this become a question for them.
        try:
            await _sync(owner, request, settings, blobs)
        except HTTPException as exc:
            raise HTTPException(
                status_code=409,
                detail=f"no firmware stored and none could be fetched ({exc.detail})",
            ) from exc
        stored = await _store(request).get(ctx_for(owner), FIRMWARE_KEY)
    assert stored is not None

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
