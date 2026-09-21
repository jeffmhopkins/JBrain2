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
root — and passes them per request, which is what lets the built image stay generic: the
same bytes go to both twins' panels, and are safe to commit to this repository.

Owner-only, with one exception that has to exist: `GET /endpoint/firmware` is also
reachable by a `device_key` principal, because that is the manifest a flashed panel polls.
Reaching it is ALSO the panel's health signal — the firmware marks a new image good only
once it has fetched this, since the only unrecoverable state is one an OTA cannot reach
(firmware/README.md). It returns a version and a URL and nothing else; a panel is not
allowed to learn anything about this box beyond what firmware it should be running.

Rules: no LLM (#1 n/a). No new table and no new state at all — the firmware is a file in
this box's own checkout, so there is nothing to store and no isolation test to add (#3).
The one raw path this reads is an infrastructure mount, not owner data (#2, same shape as
the Caddy root below).
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from jbrain.api.deps import OwnerDep, PanelDep, SettingsDep
from jbrain.api.devices import DeviceRepoDep
from jbrain.api.notes import ctx_for
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.devices import service as devices
from jbrain.settings_store import SqlSettingsStore

log = structlog.get_logger()

router = APIRouter(prefix="/endpoint", tags=["endpoint"])

# Where each image is written, matching `firmware/partitions.csv`. Hardcoded rather than
# taken from the request because these offsets are the layout of a device the owner cannot
# recover without a cable: a caller-supplied otadata offset is not a feature.
BOOTLOADER_OFFSET = "0x0"
PARTITION_TABLE_OFFSET = "0x8000"
OTA_DATA_OFFSET = "0xf000"
APP_OFFSET = "0x20000"
NVS_OFFSET = "0x9000"
NVS_SIZE = "0x6000"

APP_IMAGE = "jbrain-endpoint.bin"

# Which built image goes where. A set missing any of them is refused rather than
# half-written to a board.
ARTIFACT_IMAGES = {
    "bootloader.bin": BOOTLOADER_OFFSET,
    "partition-table.bin": PARTITION_TABLE_OFFSET,
    APP_IMAGE: APP_OFFSET,
}

SIDECAR_TIMEOUT_S = 600.0

# The network a panel is put on, remembered so a re-flash does not need someone standing at
# the PWA with the password. THIS IS A SECRET AT REST and the only one this surface keeps —
# everything else here (the device token, the CA) is minted or read per request. It lives in
# `app.settings`, owner-only RLS, and it is written ONLY when the owner ticks the box that
# says so. The reason it is worth having: a panel on a bedroom wall that stops working is
# recovered by re-flashing it, and CLAUDE.md #10 means that must not require a phone and a
# retyped password (ROOM_ENDPOINT_PLAN.md §10.4k).
WIFI_KEY = "endpoint_wifi"


def _store(request: Request) -> SqlSettingsStore:
    return SqlSettingsStore(request.app.state.session_maker)


async def remembered_wifi(request: Request, ctx: SessionContext) -> tuple[str, str]:
    """The stored network, or ("", "") when the owner has never asked this box to keep one."""
    row = await _store(request).get(ctx, WIFI_KEY)
    if not isinstance(row, dict):
        return "", ""
    return str(row.get("ssid") or ""), str(row.get("password") or "")


# Caddy's internal-CA root, as the read-only `caddy_data` mount exposes it. A panel needs
# it to validate https://jbrain.local, and reading it here is what stops the owner needing
# the `docker cp` that docs/runbooks/LOCAL_ACCESS.md otherwise requires (CLAUDE.md #10).
# WHERE THE ROOT IS READ FROM, and why there are two paths.
#
# Caddy mints its internal CA as root, into a directory that also holds the CA PRIVATE
# KEY — so that directory is not world-traversable, correctly. This process runs as
# `appuser` (uid 1000) and cannot get to it: measured on the live box, `[Errno 13]
# Permission denied`, with the parent not even listable. The consequence was silent,
# because "" is also what a box with no LAN site returns: every panel was handed the
# PUBLIC hostname and routed its traffic out through Cloudflare and back from three
# metres away.
#
# So the proxy publishes the ROOT — the public half — to a readable path beside it
# (`deploy/proxy-publish-ca.sh`), and that is preferred here. The original path stays as a
# fallback for a box whose proxy has not been rebuilt yet, where this process might still
# be running as root and able to read it.
CADDY_ROOT_PUBLISHED = "/data/caddy/lan-root.crt"
CADDY_ROOT_PATH = "/data/caddy/caddy/pki/authorities/local/root.crt"


def _lan_ca() -> str:
    """The box's own root certificate, or "" when this box has no LAN site.

    Empty is a real state rather than a failure: a box reached only through the tunnel has
    a publicly-trusted certificate and no internal CA to distribute. The panel is told so
    by getting no `ca` key, and a flash against a tunnel URL still works.

    It is ALSO what an unreadable file returns, which is why `GET /api/debug/endpoint/address`
    exists — the two states are indistinguishable here and need completely different fixes.
    """
    for path in (CADDY_ROOT_PUBLISHED, CADDY_ROOT_PATH):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if "BEGIN CERTIFICATE" in text:
            return text
    return ""


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


class FirmwareOut(BaseModel):
    version: str
    url: str


# WHERE THE FIRMWARE COMES FROM: this box's own checkout, at `firmware/dist/`, mounted
# read-only at /firmware. Not from a GitHub release.
#
# It used to be a release, and the release was the whole problem. api.github.com for the
# release JSON, github.com for each asset, a signed CDN host after that, a SHA256SUMS
# parse, a copy into the BlobStore, and a "sync firmware" button the owner had to know to
# press first — three hosts this box otherwise never talks to, standing between a plugged-in
# board and the button next to it. The first real flash died on one of them: a DNS lookup
# returned no address and the owner got "Request failed: 500".
#
# None of it was needed. The box already pulls this entire repository from main on every
# update (deploy/update-inner.sh: `git fetch` + `reset --hard`), over a path that is proven
# to work here because everything else on the box arrives through it, and it builds the api
# image out of that same tree. So the flashable firmware is simply the firmware in the
# checkout the box is running. No second channel, no credential, no network at all at flash
# time, and nothing to press: bump `firmware/version.txt`, commit the rebuilt images, and a
# panel gets them on the next update the way every other part of this box does.
#
# The price is ~1 MB of built images in git per firmware version. That is the honest cost of
# a box needing no route to a CDN to flash a board plugged into its own USB port, and
# `.github/workflows/firmware.yml` proves the committed bytes are what this source builds.


def _dist(settings: Settings) -> Path:
    return Path(settings.firmware_dir) / "dist"


def _firmware_version(settings: Settings) -> str | None:
    """The version in the checkout, or None when there is no firmware mounted at all."""
    try:
        text = (Path(settings.firmware_dir) / "version.txt").read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def _expected_digests(settings: Settings) -> dict[str, str]:
    """`dist/SHA256SUMS` as image name -> digest, or empty when it is absent."""
    digests: dict[str, str] = {}
    try:
        text = (_dist(settings) / "SHA256SUMS").read_text(encoding="utf-8")
    except OSError:
        return digests
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            digests[parts[1].lstrip("*./").rsplit("/", 1)[-1]] = parts[0].lower()
    return digests


def _image(settings: Settings, name: str) -> bytes:
    """One built image off the mount, checked against `dist/SHA256SUMS`.

    The bytes come out of a git checkout rather than off a network, so this is not guarding
    against a hostile response — it guards a half-finished update or a stale `dist/` sitting
    beside a newer `version.txt`. Worth the two lines anyway: a truncated image that flashes
    is worse than one that refuses, because the board it lands on has no cable attached to
    it once it is in a bedroom.
    """
    try:
        data = (_dist(settings) / name).read_bytes()
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"This box has no firmware to flash — firmware/dist/{name} is missing from "
                "its checkout. Run Ops -> Update to pull the current main, then try again."
            ),
        ) from exc
    want = _expected_digests(settings).get(name)
    if want and hashlib.sha256(data).hexdigest() != want:
        raise HTTPException(
            status_code=500,
            detail=f"{name} does not match firmware/dist/SHA256SUMS — refusing to flash it",
        )
    return data


@router.get("/firmware")
async def firmware_manifest(
    principal: PanelDep, request: Request, settings: SettingsDep
) -> FirmwareOut:
    """What a panel should be running — the one route a flashed panel itself calls.

    Reaching this is the health signal the firmware's rollback gate turns on, so it is
    deliberately cheap and says nothing about this box beyond a version and a URL.

    `PanelDep`, not `PrincipalDep`: the latter reads the owner's session cookie and nothing
    else, so the `device_key` half of this route's contract was unreachable and every panel
    poll 401'd. See its docstring — the kind check that used to sit here was dead code.
    """
    version = _firmware_version(settings)
    if version is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "This box has no firmware in its checkout. Run Ops -> Update to pull the "
                "current main."
            ),
        )
    out = FirmwareOut(version=version, url=f"{_public_base(request)}/endpoint/firmware/bin")
    # The ONLY window onto a panel's update decision from where the owner sits. A panel that
    # polls happily and never updates is indistinguishable, in an access log, from one that
    # is correctly up to date — and the two have completely different fixes. The `url` is
    # here because it is DERIVED (from this request's own base), so it is the field most
    # able to be quietly wrong: a manifest the panel can read pointing at an image it cannot
    # fetch fails inside the firmware, where nothing on this box can see it.
    log.info(
        "endpoint.manifest_served",
        version=out.version,
        url=out.url,
        principal=principal.kind,
        host=request.headers.get("host", ""),
    )
    return out


@router.get("/firmware/bin")
async def firmware_image(principal: PanelDep, settings: SettingsDep) -> Response:
    """The app image itself: the URL the manifest hands a panel, and so the OTA download.

    This had no implementation before. The manifest advertised the path and nothing served
    it, so the first OTA any panel attempted would have 404'd — invisible until now only
    because no panel had ever got far enough to attempt one.
    """
    image = _image(settings, APP_IMAGE)
    log.info(
        "endpoint.image_served",
        bytes=len(image),
        version=_firmware_version(settings),
        principal=principal.kind,
    )
    return Response(
        content=image,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


def _public_base(request: Request) -> str:
    """The address the OWNER reached this box at, as a PANEL has to be able to use it.

    ALWAYS https, and the word "always" is doing real work. `request.base_url` reports the
    scheme of the hop that reached uvicorn, which is plain HTTP: Caddy runs in Cloudflare
    Tunnel mode, where its own site address is `http://<domain>` and TLS terminates at the
    edge, and uvicorn is not told to trust `X-Forwarded-Proto` from a container address.
    So the honest-looking answer is the wrong one, in two ways that both shipped:

    - `esp_https_ota` refuses a plain-HTTP URL outright, so EVERY over-the-air update
      failed instantly, silently, on the panel, forever (ROOM_ENDPOINT_PLAN.md §10.4h).
    - the same value is written into a panel's NVS at flash time as the address it calls
      home on, so a unit polled the box over http and put its bearer token on the wire in
      the clear.

    `X-Forwarded-Proto` is NOT consulted, which was the first attempt at this fix and was
    wrong in the same way for one hop further out. A forwarded scheme describes the hop
    that set it, not the client's connection: in tunnel mode Caddy receives the request
    from `cloudflared` over plain HTTP and says so, accurately, while the panel's actual
    connection to Cloudflare's edge was TLS all along. Believing that header re-emitted
    `http://` verbatim and the OTA stayed broken through a deploy.

    So: https, unconditionally. Every way into this box is TLS — the LAN site Caddy mints
    a certificate for, and the edge in front of the tunnel — and there is no supported
    deployment where handing a panel `http://` is right. Only the scheme is replaced;
    rewriting the host would send a panel somewhere nobody asked for.
    """
    return str(httpx.URL(str(request.base_url)).copy_with(scheme="https")).rstrip("/") + "/api"


def _panel_base(request: Request, settings: Settings) -> tuple[str, str]:
    """Where a panel should look for the box, and which certificate to trust there.

    NOT simply the owner's own address, which is what this used to be and was wrong in a
    way that had no symptom but silence. A panel sits on the same LAN as the box; handing
    it whatever host the owner's browser happened to be on sends every frame out through
    the tunnel and back — and worse, a panel told a PUBLIC hostname while being handed the
    box's INTERNAL root fails TLS on every request forever, having joined Wi-Fi perfectly.

    So the two travel together or not at all:

    - LAN address configured AND its root readable -> `https://jbrain.local/api` + that
      root. Pinning one certificate beats trusting ~150 public CAs, and it costs nothing
      here.
    - otherwise -> the owner's address + no root, and the firmware validates against the
      public bundle it now carries.
    """
    lan = settings.lan_addr.strip().rstrip("/")
    ca = _lan_ca()
    if lan and ca:
        return f"{lan}/api", ca
    return _public_base(request), ""


# A watch runs until it is stopped, so the ceiling is high and the sidecar enforces its
# own (monitor.MAX_SECONDS). This only has to outlast the request.
MONITOR_TIMEOUT_S = 960.0


class TelemetryIn(BaseModel):
    """What a panel says about itself, unprompted.

    Deliberately a flat bag of short strings and ints rather than a schema per diagnostic.
    What a panel needs to report changes with whatever is being chased that week, and a
    migration per question would mean the question does not get asked.
    """

    version: str
    uptime_ms: int
    reset_reason: str = ""
    free_heap: int = 0
    free_psram: int = 0
    # Loudest microphone sample since the panel's last report, 0..32767. Zero across several
    # reports while someone is talking near it means the capture path is dead — which a
    # meter drawn on the panel shows to whoever is standing there, and this shows to whoever
    # is not.
    mic_peak: int = 0
    # Hex, one sample per entry, oldest first. Empty on a cold boot, which is itself the
    # answer to "did anything survive the restart".
    pmu_history: list[str] = []
    note: str = ""


@router.post("/telemetry", status_code=204)
async def telemetry(principal: PanelDep, body: TelemetryIn) -> Response:
    """A panel reporting its own state, because every other channel either lies or resets it.

    THIS EXISTS BECAUSE THE INSTRUMENTS WERE THE PROBLEM. The display fault took six firmware
    releases partly because the panel could only be questioned two ways, and both were
    broken: reading the controller's registers over QSPI returns zeros that look exactly like
    a diagnosis, and opening the USB console resets the chip before the fault can be seen, so
    every console log was of a freshly-rebooted panel rather than of the thing being chased.

    It is also what CLAUDE.md #10 requires. A panel that can only be diagnosed with a cable
    is not a room endpoint, it is a bench unit — and the owner moving one to a plain USB
    charger, which is the whole premise, must not cost the ability to see what it is doing.

    Nothing is stored. These are a panel's own claims about itself, they are only ever read
    by a human looking at a log, and a table would be a schema to migrate every time the
    question changes. `pmu_history` is passed through as the panel spelled it, because the
    point is to see exactly what the registers held.
    """
    log.info(
        "endpoint.telemetry",
        principal=principal.kind,
        version=body.version,
        uptime_s=body.uptime_ms // 1000,
        reset_reason=body.reset_reason,
        free_heap=body.free_heap,
        free_psram=body.free_psram,
        mic_peak=body.mic_peak,
        pmu_history=body.pmu_history,
        note=body.note,
    )
    return Response(status_code=204)


@router.get("/monitor")
async def monitor_panel(
    _owner: OwnerDep, settings: SettingsDep, port: str, seconds: int = 120, reset: bool = False
) -> StreamingResponse:
    """Stream a panel's own console back to the owner.

    The counterpart to everything else here, which reports only what the BOX saw. A panel
    that polled the manifest and then quietly did not update looked, from this side,
    exactly like one that was correctly up to date — the reason was a log line on the
    panel that nobody could read (ROOM_ENDPOINT_PLAN.md §10.4d).

    Owner-only and `port`-checked by the sidecar against what is actually plugged in, the
    same as a flash: this opens a device node in a root container, so the path is never
    taken on trust from the request.
    """
    base = _sidecar(settings)
    query = {"port": port, "seconds": str(seconds), "reset": "1" if reset else "0"}

    async def stream() -> AsyncIterator[bytes]:
        async with (
            httpx.AsyncClient(timeout=MONITOR_TIMEOUT_S) as client,
            client.stream("GET", f"{base}/monitor", params=query) as resp,
        ):
            async for chunk in resp.aiter_bytes():
                yield chunk

    return StreamingResponse(
        stream(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class FlashIn(BaseModel):
    port: str
    ssid: str
    password: str
    # Which twin's panel this is. Carried into NVS so a log line names a unit rather than
    # a serial port that changes between plugs.
    name: str = ""
    erase: bool = False
    # Keep this network on the box so a later re-flash needs no phone and no retyped
    # password. Defaults OFF: it is the one secret this surface stores, and starting to
    # store it should be something the owner did rather than something that happened.
    remember: bool = False


async def build_flash(
    request: Request,
    settings: Settings,
    device_repo: Any,
    ctx: SessionContext,
    *,
    port: str,
    ssid: str,
    password: str,
    name: str,
    erase: bool,
) -> dict[str, Any]:
    """Everything a panel needs, assembled: the images, and the config that makes it a unit.

    Shared by the owner's PWA flash and the debug console's, so the two cannot drift. That
    matters more than the duplication it saves: a second copy of this would be a second
    place to forget the CA pairing rule below, and the failure mode there is a panel that
    joins Wi-Fi perfectly and is then silent forever.
    """
    images = [
        {"offset": offset, "b64": base64.b64encode(_image(settings, iname)).decode()}
        for iname, offset in sorted(ARTIFACT_IMAGES.items(), key=lambda kv: int(kv[1], 16))
    ]

    # A fresh device identity per flash, on the shipped `device_key` substrate rather than
    # a new auth model (ROOM_ENDPOINT_PLAN.md §3). The plaintext key exists only inside
    # this request: it goes into NVS and is never stored here, which is the same contract
    # the owner's own key rotation has. Re-flashing a panel therefore issues a NEW
    # identity — correct, because a re-flash is how a unit is handed over or recovered,
    # and the old key should stop working at that moment.
    label = f"panel {name}".strip() if name else "room endpoint panel"
    provisioned = await devices.provision_device(device_repo, ctx, label)

    api_base, ca = _panel_base(request, settings)
    nvs = {
        "ssid": ssid,
        "pass": password,
        "api": api_base,
        "token": provisioned.key,
        "name": name,
    }
    # Only when it is the right root for that address. An internal root beside a public
    # URL is worse than no root: it fails every handshake and looks like a network fault.
    if ca:
        nvs["ca"] = ca

    return {
        "port": port,
        "images": images,
        "nvs": nvs,
        "nvs_offset": NVS_OFFSET,
        "nvs_size": NVS_SIZE,
        "erase": erase,
    }


@router.post("/flash")
async def flash_panel(
    owner: OwnerDep,
    request: Request,
    settings: SettingsDep,
    device_repo: DeviceRepoDep,
    body: FlashIn,
) -> StreamingResponse:
    """Write this box's firmware plus this unit's own config to a panel.

    Streams the flasher's log straight through: it takes tens of seconds, and an owner
    watching a PWA has no other signal that anything is happening.
    """
    base = _sidecar(settings)
    ctx = ctx_for(owner)

    if body.remember:
        # Written BEFORE the flash, deliberately: a flash that fails half way still leaves
        # the owner able to retry from the debug console without their phone, which is the
        # situation this exists for.
        await _store(request).upsert(ctx, WIFI_KEY, {"ssid": body.ssid, "password": body.password})

    payload = await build_flash(
        request,
        settings,
        device_repo,
        ctx,
        port=body.port,
        ssid=body.ssid,
        password=body.password,
        name=body.name,
        erase=body.erase,
    )

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
