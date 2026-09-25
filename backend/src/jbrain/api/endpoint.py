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

import array
import base64
import hashlib
import json
import random
import re
import struct
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from jbrain.api.deps import OwnerDep, PanelDep, SettingsDep
from jbrain.api.devices import DeviceRepoDep
from jbrain.api.notes import ctx_for
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.devices import service as devices
from jbrain.devices.repo import DeviceRole
from jbrain.llm.router import LlmRouter
from jbrain.settings_store import SqlSettingsStore
from jbrain.transcribe import WhisperCppClient

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
# The `model` partition in firmware/partitions.csv. THE ONE IMAGE OTA CANNOT DELIVER:
# `esp_https_ota` writes app slots, and the speech models live in a data partition — so the
# only way they reach a panel is the USB flash each unit gets once. That is affordable
# because the command vocabulary is NOT in here: MultiNet phrases are supplied at runtime as
# phoneme strings, so changing what the robot answers to stays an ordinary OTA. This image
# changes only if the wake word or the model generation does.
MODEL_OFFSET = "0xaa0000"

APP_IMAGE = "jbrain-endpoint.bin"
MODEL_IMAGE = "srmodels.bin"
# RESET WHICH SLOT BOOTS, ON EVERY USB FLASH. Without it `otadata` keeps pointing at
# whichever OTA slot the panel was last updated into, and a USB flash writes `factory` — so
# the panel would ignore the image just written and boot the old one. That was survivable
# while the layout was fixed; it stopped being survivable when the app partitions were
# RESIZED (2026-09-21, firmware/partitions.csv), because the stale pointer then names a slot
# at a NEW offset holding the middle of the previous image. Recovering from that costs the
# cable this whole design exists to avoid.
OTA_DATA_IMAGE = "ota_data_initial.bin"

# Which built image goes where. A set missing any of them is refused rather than
# half-written to a board.
ARTIFACT_IMAGES = {
    "bootloader.bin": BOOTLOADER_OFFSET,
    "partition-table.bin": PARTITION_TABLE_OFFSET,
    OTA_DATA_IMAGE: OTA_DATA_OFFSET,
    APP_IMAGE: APP_OFFSET,
    MODEL_IMAGE: MODEL_OFFSET,
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
    # The ES8311's ALC register before and after the firmware cleared it — "f8-78 off",
    # "78 already-off", "REFUSED". It answers whether the codec's own automatic gain was
    # railing the microphone after a beep, and it is here rather than only in a console log
    # because a panel in its real place is on a charger, not a cable.
    alc: str = ""
    # Frames that reached the glass and frames that did not. A screen that has stopped
    # drawing cannot say so on a channel that needs someone standing next to it.
    blit_ok: int = 0
    blit_fail: int = 0
    boot_btn: int = 0
    # How many of the panel's phrases the speech model ACCEPTED, and how many it refused —
    # and, when it refused any, which ones.
    #
    # The owner, on the twins: *"burp has been on there. It never actually activates them.
    # The kids say the word — like the code word is wrong."* The panel has counted this since
    # bring-up and said so only to a serial console, on a device that has been on a plain
    # charger since the day it went in a bedroom. A phrase the model will not take is silently
    # absent from the vocabulary, and from the room that is indistinguishable from a broken
    # microphone; the count says the panel is deaf to something and the names say to what.
    # `vocab_ok` of 0 from firmware too old to report it, so a zero here is "unknown", not
    # "nothing worked".
    vocab_ok: int = 0
    vocab_bad: int = 0
    vocab_refused: list[str] = Field(default_factory=list)
    # The last few things the recogniser resolved: [phrase, confidence 0-100, did it fire].
    # `speech.c` has computed this on every decode since bring-up and printed it to a console
    # the panel does not have — and its own comment says the confidence floor that would stop
    # "turn red" firing `jump up` at p=0.19 cannot be chosen until a CORRECT decode's score is
    # known on this hardware. This is that measurement, finally leaving the device.
    # BOTH ARITIES, AND THE UNION IS THE ROLLOUT. 0.2.91 adds a fourth field — how many times
    # the same decode repeated in a row — and during an OTA one panel is on the old firmware
    # while the other is on the new. A model that took only the new shape would 422 the old
    # panel's telemetry, and a 422 is a FAILED report: the crash ring it was carrying would be
    # kept rather than cleared, and the reading would simply never arrive. Accepting both is
    # what makes a fleet upgradable one panel at a time.
    heard: list[tuple[str, int, int] | tuple[str, int, int, int]] = Field(default_factory=list)
    # The largest free INTERNAL DMA block. `free_heap` above is the total, and the total is
    # exactly the number that cannot tell 60 KB free-and-contiguous from 60 KB
    # free-and-fragmented — which is the difference between a panel that draws and one where
    # every blit fails. This reading has explained that fault twice and both times it took a
    # host toolchain to read it.
    int_largest: int = 0
    # What the codec last ACCEPTED, "90/36" — or "90!/36" when it refused the volume. Both
    # setters used to run with their returns dropped under a log line asserting success, on
    # the one path the owner drives remotely.
    levels: str = ""
    # Monotonic, unlike `blit_ok`/`blit_fail`, which are reset on recovery and therefore
    # report a panel that failed 249 blits and self-healed as one that never faltered.
    # `meter_fail` is its own transfer and reached no counter at all: the meter redraws at
    # 25 Hz against the face's 5, so it is the more frequent blit on this bus by five to one.
    blit_fail_total: int = 0
    blit_recov: int = 0
    meter_fail: int = 0
    # `wifi_err_reason_t` and how many times the link has dropped since boot. 201 (out of
    # range / SSID gone), 15 (wrong password) and 8 (the router kicked it) are three different
    # repairs; a panel that reconnects before its next report used to look perfectly healthy.
    wifi_reason: int = 0
    wifi_drops: int = 0
    # Why an update would not install, and how many have failed. A panel that CANNOT install
    # retries every fifteen minutes forever reporting the old version, which from here is
    # indistinguishable from a panel nobody offered an update to.
    ota_err: str = ""
    ota_tries: int = 0
    # Where the last touch landed and which zone it resolved to: [x, y, zone].
    #
    # THE PANEL HAS BEEN SENDING THIS ALL ALONG and nothing declared it, so pydantic dropped
    # it on the floor of every report — the panel spending the bytes, the box discarding them,
    # and no side of it able to notice. It is the reading that separates "the glass is dead"
    # from "the glass works and the rotation maths puts the finger somewhere else", which is a
    # fault this panel has actually had.
    tap: list[int] = Field(default_factory=list)
    # Which of the three callers of `esp_restart()` it was — "blit-heal" (a real fault),
    # "gesture" (a four-year-old), "ota-park" (routine). All three arrive as
    # `reset_reason: "sw(3)"` and two of them also share `crash_phase: 9`.
    restart_why: str = ""
    # Whether the panel got a real HARDWARE reset before the display was brought up. The
    # CO5300's reset line hangs off the TCA9554 expander and no build before 0.2.86 ever drove
    # it, so the controller only ever saw a SOFTWARE reset — a command down the same QSPI bus
    # it was already wedged on. False here means this boot initialised the panel the old way
    # (the expander did not answer), which is the state the post-OTA black screen lives in, so
    # a dark panel reporting `panel_reset: false` and one reporting `true` are different bugs.
    panel_reset: bool = False
    # "awake", "dim" or "dark": which stage of the screen sleep the panel is in. A sleeping
    # screen stops blitting deliberately, so `blit_ok` stops climbing — the exact signature of
    # the stalled render task that took a photograph from the owner to diagnose. Without this
    # field the two reports are identical, and the owner has no terminal to tell them apart
    # with (CLAUDE.md #10). Empty means firmware too old to say.
    screen: str = ""
    free_heap: int = 0
    free_psram: int = 0
    # Loudest microphone sample since the panel's last report, 0..32767. Zero across several
    # reports while someone is talking near it means the capture path is dead — which a
    # meter drawn on the panel shows to whoever is standing there, and this shows to whoever
    # is not.
    mic_peak: int = 0
    # Words of stack the panel's render task has never touched, smallest seen since boot. A
    # shrinking number is a panic that has not happened yet; zero means the field is from
    # firmware too old to report it.
    stack_free: int = 0
    # The render loop stage reached just before the last restart, or -1 on a cold boot. A
    # panic prints its backtrace to a console this panel does not have, so this is the
    # substitute: not a line number, but the difference between "somewhere in the firmware"
    # and "in the I2S read".
    crash_phase: int = 0
    # Raw accelerometer counts [x, y, z] at +/-4 g, so 1 g is about 8192. Deliberately raw:
    # which axis points where on this board is not documented anywhere, and a number the
    # firmware has already interpreted cannot answer that. All zeros means the part did not
    # answer, which is its own reading.
    accel: list[int] = []
    # Hex, one sample per entry, oldest first. Empty on a cold boot, which is itself the
    # answer to "did anything survive the restart".
    pmu_history: list[str] = []
    note: str = ""


@router.post("/telemetry", status_code=204)
async def telemetry(principal: PanelDep, request: Request, body: TelemetryIn) -> Response:
    """A panel reporting its own state, because every other channel either lies or resets it.

    THIS EXISTS BECAUSE THE INSTRUMENTS WERE THE PROBLEM. The display fault took six firmware
    releases partly because the panel could only be questioned two ways, and both were
    broken: reading the controller's registers over QSPI returns zeros that look exactly like
    a diagnosis, and opening the USB console resets the chip before the fault can be seen, so
    every console log was of a freshly-rebooted panel rather than of the thing being chased.

    It is also what CLAUDE.md #10 requires. A panel that can only be diagnosed with a cable
    is not a room endpoint, it is a bench unit — and the owner moving one to a plain USB
    charger, which is the whole premise, must not cost the ability to see what it is doing.

    THE LOG IS STILL WHERE THE DETAIL LIVES, and the latest report is now also KEPT — one
    row per panel, upserted, in `app.endpoint_status`. This route used to store nothing, on
    the argument that a table would be a schema to migrate every time the question changes.
    That objection was right and is answered by holding the report as `jsonb`; the premise
    underneath it was not. It assumed a human reading a log, and **the owner has no terminal**
    (CLAUDE.md #10) — so "is her panel alive, and did the update land" was a question only a
    shell could answer, which is what "just your update only has 0.2.88" cost on a panel that
    had in fact updated forty minutes earlier. `GET /status` is the answer from a phone.

    A FAILED SAVE MUST NOT FAIL THE REPORT. A 500 here is a *failed* telemetry from the
    panel's side: it keeps its crash ring rather than clearing it, and retries the whole body
    on the next cycle. The log line above has already been written by then, so the reading is
    not lost — only the snapshot is — and a panel that cannot reach the box's disk is exactly
    the panel whose report is most worth having.

    `pmu_history` is passed through as the panel spelled it, because the point is to see
    exactly what the registers held.
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
        accel=body.accel,
        stack_free=body.stack_free,
        crash_phase=body.crash_phase,
        alc=body.alc,
        blit_ok=body.blit_ok,
        blit_fail=body.blit_fail,
        boot_btn=body.boot_btn,
        vocab_ok=body.vocab_ok,
        vocab_bad=body.vocab_bad,
        int_largest=body.int_largest,
        levels=body.levels,
        blit_fail_total=body.blit_fail_total,
        blit_recov=body.blit_recov,
        meter_fail=body.meter_fail,
        wifi_reason=body.wifi_reason,
        wifi_drops=body.wifi_drops,
        restart_why=body.restart_why,
        tap=body.tap,
        panel_reset=body.panel_reset,
        screen=body.screen,
        # Only when there is something to say. An empty key on every report for fifteen
        # minutes of a healthy panel is how a log stops being read.
        **({"ota_err": body.ota_err, "ota_tries": body.ota_tries} if body.ota_err else {}),
        **({"heard": body.heard} if body.heard else {}),
        # Only when there are any: an empty list on every report is noise in a log a human
        # reads, and the counts already say when to look.
        **({"vocab_refused": body.vocab_refused} if body.vocab_refused else {}),
        pmu_history=body.pmu_history,
        note=body.note,
    )
    # `model_dump`, not the raw request: what is stored is what this route's model accepted,
    # so a panel cannot grow the row with fields nobody declared.
    try:
        async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO app.endpoint_status (principal_id, reported_at, version, report)
                    VALUES (CAST(:me AS uuid), now(), :version, CAST(:report AS jsonb))
                    ON CONFLICT (principal_id) DO UPDATE
                    SET reported_at = now(),
                        version = EXCLUDED.version,
                        report = EXCLUDED.report
                    """
                ),
                {
                    "me": principal.id,
                    "version": body.version,
                    "report": json.dumps(body.model_dump(mode="json")),
                },
            )
            await session.commit()
    except Exception:
        # See the docstring: the report is already in the log, and a 500 would make the panel
        # keep its crash ring and re-send everything rather than move on.
        log.warning("endpoint.status_not_saved", principal=principal.id, exc_info=True)
    return Response(status_code=204)


# Ceilings, and why each one is where it is.
#
# These clamp rather than reject. A rejected write gives a 500 and no guidance; a clamp turns
# a typo into a safe value and reports what it did, which is the difference between an owner
# with no terminal being stuck and being informed (CLAUDE.md #10).
#
# VOLUME_MAX is the only one that is a safety limit rather than a range. The vendor ships 90;
# 70 is the level the owner confirmed as good; 85 leaves room to go louder deliberately while
# making it impossible for a slipped digit to put 100 into a speaker held against a
# four-year-old's ear. Raising this ceiling should take a measurement and a commit, which is
# exactly the friction §10.4q asked for.
VOLUME_MAX = 85
# The ES8311's PGA quantises to 6 dB steps and stops at 42; anything above is silently
# truncated by the part, so accepting it would be a number that reads back wrong.
MIC_GAIN_MAX = 42
# Zero brightness is a panel indistinguishable from the fault §10.4u is chasing, so the floor
# is "clearly dim" rather than "off". Quiet hours dims to this end of the range; it must never
# be able to reach a screen that looks broken.
BRIGHTNESS_MIN = 10


#: What a unit IS, recorded at flash time on its subject (migration 0211). `jpet` is one of the
#: twins' panels — in the roster, addressable by its sibling. `display` is an endpoint the owner
#: operates: same OTA, settings, telemetry and `/converse`, never reachable by a pet. Aliased
#: rather than restated: the set belongs to the column, and two copies of it would drift.
PanelRole = DeviceRole


class EndpointSettings(BaseModel):
    volume: int = 70
    mic_gain_db: int = 30
    brightness: int = 255
    # The microphone meter down the panel's left edge. It earned its place during bring-up —
    # a microphone has no symptom, so a bar that is always running answers "is it hearing
    # anything" at a glance — but bring-up is over and what it buys now is a green bar on a
    # pet in a child's bedroom. Off by default; a debug overlay that defaults on is one
    # nobody turns off. See `0207_endpoint_debug_overlay.py`.
    debug_overlay: bool = False
    # THE CODEC'S OWN AGC (ES8311 REG18 bit 7), which has never been on — `audio.c` has reported
    # `00 already-off` in every telemetry report this box has received. A fixed PGA cannot serve
    # two panels whose last readings were `mic_peak` 32767 and 814 on the same gain. Default OFF,
    # so an upgrade changes nothing until the owner asks it to (migration 0214).
    mic_agc: bool = False
    # HOW DIM "DIM" IS, as a percentage of `brightness`. It was a hardcoded quarter, and the
    # owner found what that is worth in a bedroom at full brightness: 63 of 255, which does not
    # read as dim at all. 25 reproduces the old behaviour exactly (migration 0215).
    dim_percent: int = 25
    # THE FIRMWARE VERSION THIS BOX WOULD SERVE, so the settings poll is also the "is there
    # anything new" poll. It rides here rather than being a second request because every fetch a
    # panel makes is a fresh TLS handshake and `int_largest` on these units sits at 31 KB — one
    # round trip that answers both questions costs half the handshakes of two that each answer
    # one. A panel now learns about an update within a poll instead of at the end of the
    # fifteen-minute manifest cycle, while the 3.25 MB image is still only fetched when the
    # version actually CHANGES.
    #
    # Read-only in effect, by the same mechanism as `pet_name` below: the PUT writes six named
    # columns and ignores every other field of this model.
    fw_version: str = ""
    # PER-PANEL, unlike the five above, which are one answer for the whole house. Defaulted here
    # so the model stays the shape the PUT takes: the owner's write touches only the four
    # columns of `endpoint_settings`, and these come from `endpoint_panel` on the way out.
    pet_name: str = ""
    form: PanelForm = "ostrich"


def _clamp(v: EndpointSettings) -> EndpointSettings:
    return EndpointSettings(
        volume=max(0, min(v.volume, VOLUME_MAX)),
        mic_gain_db=max(0, min(v.mic_gain_db, MIC_GAIN_MAX)),
        brightness=max(BRIGHTNESS_MIN, min(v.brightness, 255)),
        # Nothing to clamp: a bool is already its own range.
        debug_overlay=v.debug_overlay,
        mic_agc=v.mic_agc,
        dim_percent=max(0, min(v.dim_percent, 100)),
    )


async def _read_settings(request: Request, ctx: SessionContext) -> EndpointSettings:
    async with scoped_session(request.app.state.session_maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "SELECT volume, mic_gain_db, brightness, debug_overlay, mic_agc,"
                    " dim_percent"
                    " FROM app.endpoint_settings WHERE id = 1"
                )
            )
        ).first()
    if row is None:
        return EndpointSettings()
    return EndpointSettings(
        volume=row[0],
        mic_gain_db=row[1],
        brightness=row[2],
        debug_overlay=row[3],
        mic_agc=row[4],
        dim_percent=row[5],
    )


# THE LABEL IS A NAME. THE ROLE IS A KIND. They were one string until migration 0211.
#
# A panel is an ordinary `device_key` principal — the same substrate as an OwnTracks phone —
# so for a long time the only thing marking one was the label `/flash` put on the key it minted,
# and `label LIKE 'panel%'` was how three separate queries answered "is this a panel". That
# string was therefore load-bearing for addressing, for the roster AND for the fleet view, with
# nothing but a test connecting the module that wrote it to the modules that matched it.
#
# It broke the way a convention breaks: a third unit was flashed, took the unnamed default, and
# `room endpoint panel` matched the roster predicate — so a box on the owner's desk silently
# joined two children's addressing and stopped their messages, because it had been named
# nothing in particular. `subjects.device_role` is now the mechanism, and these two keep only
# the job they were always good at: carrying a name a four-year-old can be told out loud.
# `jpanel` imports them; it does not restate them.
UNNAMED_PANEL_LABEL = "room endpoint panel"


def panel_label(name: str) -> str:
    """The label `/flash` writes onto a panel's device key."""
    return f"panel {name}".strip() if name else UNNAMED_PANEL_LABEL


def panel_display_name(label: str) -> str:
    """The name to say out loud, from that label. Pure, so the mapping can be round-tripped
    against the function that writes it rather than assumed."""
    if label == UNNAMED_PANEL_LABEL:
        # Sayable, if inelegant. A four-year-old told "a message from the other one" at least
        # knows a message arrived; an empty name would draw a pop-up from nobody.
        return "the other one"
    return label.removeprefix("panel").strip() or "the other one"


class PanelStatus(BaseModel):
    """One panel, as it last described itself."""

    device_id: str
    name: str
    # What this unit is, so the fleet view can say so and offer a pet the things only a pet
    # has. Both roles appear here: a display the owner operates is part of his fleet whether
    # or not the twins can reach it, and leaving it out would recreate in the one screen that
    # matters the blind spot this column exists to remove.
    role: PanelRole = "jpet"
    # Absent for a panel that has been flashed and has never reported — which is its own
    # answer, and a different one from "reported an hour ago and has gone quiet".
    reported_at: str = ""
    version: str = ""
    # Seconds since that report, computed on the box. The PWA must not subtract a server
    # timestamp from a phone clock: the two disagree by minutes on a phone that has been
    # asleep, and "last seen 4 minutes in the future" is how a working fleet looks broken.
    age_s: int = -1
    report: dict[str, Any] = Field(default_factory=dict)


class PanelStatuses(BaseModel):
    panels: list[PanelStatus] = Field(default_factory=list)


@router.get("/status")
async def panel_status(owner: OwnerDep, request: Request) -> PanelStatuses:
    """Every panel this box knows, and the last thing each one said about itself.

    **THE POINT IS THAT IT IS NOT A LOG.** The panel has reported richly for months and the
    only reader was `grep` over the box's structured log, reachable through the debug API and
    a terminal. The owner has neither (CLAUDE.md #10), so the questions this answers — is her
    panel alive, did the update land, is it drawing, can it hear, why did it restart — were
    ones he had to hand to someone with a shell.

    **A panel with no row has never reported**, and that is kept distinct from a stale one
    rather than folded into "unknown": the first is a unit that was flashed and never came up,
    the second is one that was working and stopped, and they are different faults with
    different first moves.

    Read under the OWNER's context, which is also why the roster query is here rather than
    borrowed from `jpanel`: that module's helper deliberately runs under its own addressing
    context to survive being called by a panel, and this route has no such problem."""
    async with scoped_session(request.app.state.session_maker, ctx_for(owner)) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (p.label)
                           p.id::text, p.label, sub.device_role,
                           s.reported_at, s.version, s.report,
                           EXTRACT(EPOCH FROM (now() - s.reported_at))::bigint
                    FROM app.principals p
                    JOIN app.subjects sub ON sub.id = p.subject_id
                    LEFT JOIN app.endpoint_status s ON s.principal_id = p.id
                    WHERE p.kind = 'device_key' AND p.revoked_at IS NULL
                      AND sub.device_role IS NOT NULL
                    ORDER BY p.label, p.created_at DESC
                    """
                )
            )
        ).all()
    return PanelStatuses(
        panels=[
            PanelStatus(
                device_id=str(row[0]),
                name=panel_display_name(str(row[1])),
                # Narrowed rather than cast: the CHECK constraint permits only these two,
                # and a row that somehow carries a third reads as a pet rather than crashing
                # the one screen the owner uses to find out what is wrong.
                role="display" if row[2] == "display" else "jpet",
                reported_at=row[3].isoformat() if row[3] is not None else "",
                version=str(row[4] or ""),
                age_s=int(row[6]) if row[6] is not None else -1,
                report=dict(row[5] or {}),
            )
            # `DISTINCT ON (label)` is belt to the braces of retiring a replaced key at flash
            # time: a panel re-flashed before that landed still has several live keys under one
            # name, and the owner should see the unit once, not once per time it was ever
            # flashed. Newest first, so the row shown is the identity the unit is actually using.
            for row in rows
        ]
    )


class PanelRevoked(BaseModel):
    name: str
    keys: int


@router.post("/panels/{device_id}/revoke")
async def revoke_panel(device_id: str, owner: OwnerDep, request: Request) -> PanelRevoked:
    """Stop a unit working, from the screen the owner already watches it on.

    **This is the route that existed nowhere.** Revoking a panel meant the Location screen's
    Phones tab — a location surface, landing on Map, listing every panel ever flashed as a row
    reading "no fixes yet" — and the owner, who has no terminal (CLAUDE.md #10), could not find
    it. Told where it was, he answered: *"I don't see a way to revoke from PWA."* He was right
    about the part that mattered: there was no way to revoke a PANEL from anywhere a panel is
    managed.

    EVERY LIVE KEY FOR THAT NAME, not just the one the row was drawn from. The fleet view
    collapses a unit's flashes into one row (`DISTINCT ON (p.label)`), so the row means "this
    panel" and revoking it must mean what the owner sees: the unit stops working. Retiring only
    the principal whose id the row carried would leave the twelve older keys of a re-flashed
    panel still authenticating, which is the failure this whole change exists to end.

    Scoped to endpoints. A phone reached through this route would be revoked with no
    location-domain confirmation around it, so it 404s instead — phones are revoked where
    phones are managed.
    """
    try:
        uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such panel") from None

    async with scoped_session(request.app.state.session_maker, ctx_for(owner)) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT s.display_name, s.device_role
                    FROM app.principals p
                    JOIN app.subjects s ON s.id = p.subject_id
                    WHERE p.id = CAST(:dev AS uuid) AND p.kind = 'device_key'
                      AND s.device_role IS NOT NULL
                    """
                ),
                {"dev": device_id},
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="no such panel")
        revoked = (
            await session.execute(
                text(
                    """
                    UPDATE app.principals p SET revoked_at = now()
                    FROM app.subjects s
                    WHERE s.id = p.subject_id AND p.kind = 'device_key'
                      AND p.revoked_at IS NULL
                      AND s.display_name = :label AND s.device_role = :role
                    RETURNING p.id
                    """
                ),
                {"label": row[0], "role": row[1]},
            )
        ).all()
        await session.commit()
    name = panel_display_name(str(row[0]))
    log.info("endpoint.panel_revoked", name=name, keys=len(revoked))
    return PanelRevoked(name=name, keys=len(revoked))


#: Which body the panel draws. `display.c` has `FORM_OSTRICH` and `FORM_ROBOT` and toggles
#: between them on four taps and a hold — in RAM, so every reboot and every OTA has silently
#: put both twins back to the ostrich. This is the answer the panel comes back as.
PanelForm = Literal["ostrich", "robot"]

#: The panel's font has 5x7 cells for A-Z, the digits, space, hyphen and full stop and nothing
#: else, and the wake word runs through MultiNet, which matches PHONEMES. Both ends constrain a
#: pet's name, so it is validated once, here, against the stricter reading of the two.
PET_NAME_MAX = 12
_PET_NAME_OK = re.compile(r"^[A-Za-z][A-Za-z ]*$")


class PanelAppearance(BaseModel):
    """What one panel is called and what it looks like — its own, not the box's.

    `pet_name` IS THE WAKE WORD, not a caption. `vocab.c` compiles in `hey fish` and
    `vocab_name()` takes the last word of it for the label above the pet's head, so renaming the
    pet changes what a four-year-old SAYS to it. Empty means "whatever the firmware shipped
    with", which is the only safe reading of absent: a blank name would leave a child saying
    something the panel cannot hear.
    """

    pet_name: str = ""
    form: PanelForm = "ostrich"


async def _read_appearance(
    request: Request, ctx: SessionContext, subject_id: str
) -> PanelAppearance:
    if not subject_id:
        return PanelAppearance()
    async with scoped_session(request.app.state.session_maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "SELECT pet_name, form FROM app.endpoint_panel"
                    " WHERE subject_id = CAST(:sid AS uuid)"
                ),
                {"sid": subject_id},
            )
        ).first()
    if row is None:
        return PanelAppearance()
    return PanelAppearance(pet_name=str(row[0]), form="robot" if row[1] == "robot" else "ostrich")


@router.put("/panels/{device_id}/appearance")
async def set_panel_appearance(
    device_id: str, owner: OwnerDep, request: Request, body: PanelAppearance
) -> PanelAppearance:
    """What this panel's pet is called, and what body it wears. Owner only.

    **`pet_name` IS THE WAKE WORD.** `vocab.c` compiles in `hey fish`, `vocab_name()` takes the
    last word of it for the label above the pet's head, and the panel re-registers the phrase
    with MultiNet when this changes. So this route renames the creature in the sense that
    matters to a four-year-old: what she says to it. The file has wanted this since it was
    written — *"a name only a rebuild can change is a name they cannot change, and the two
    panels will want different ones"* — and a rebuild is a cable, which the owner does not have.

    LETTERS AND SPACES ONLY, and a short cap. Two unrelated systems constrain it and the
    stricter one wins: the panel's 5x7 font has no glyph for an apostrophe and draws it as
    NOTHING, and MultiNet matches phonemes, so digits and punctuation are not sayable at all. A
    name is refused here rather than half-drawn on a bedroom wall.

    **It cannot promise the name will HEAR as well as the one it replaces.** `hey fish` was
    chosen partly because it collides with nothing else in the command table; a thin or short
    name may recognise worse, and the failure mode is a child saying it and getting nothing.
    That is a tuning question for the confidence floor, not a reason to withhold the control.
    """
    name = " ".join(body.pet_name.split())
    if name and (len(name) > PET_NAME_MAX or not _PET_NAME_OK.match(name)):
        raise HTTPException(
            status_code=422,
            detail=f"a pet name may use letters and spaces only, {PET_NAME_MAX} at most — "
            "the panel's font has no other characters and the wake word has to be sayable",
        )
    try:
        uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such panel") from None

    ctx = ctx_for(owner)
    async with scoped_session(request.app.state.session_maker, ctx) as session:
        subject_id = (
            await session.execute(
                text(
                    """
                    SELECT s.id::text FROM app.principals p
                    JOIN app.subjects s ON s.id = p.subject_id
                    WHERE p.id = CAST(:dev AS uuid) AND p.kind = 'device_key'
                      AND s.device_role IS NOT NULL
                    """
                ),
                {"dev": device_id},
            )
        ).scalar_one_or_none()
        if subject_id is None:
            raise HTTPException(status_code=404, detail="no such panel")
        # Upsert on the SUBJECT, which is what survives a re-flash now that the flash rotates
        # rather than re-provisions. A row keyed on the key would have been orphaned by the next
        # recovery flash, losing a child's pet to the step meant to fix her panel.
        await session.execute(
            text(
                """
                INSERT INTO app.endpoint_panel (subject_id, pet_name, form)
                VALUES (CAST(:sid AS uuid), :name, :form)
                ON CONFLICT (subject_id) DO UPDATE
                SET pet_name = EXCLUDED.pet_name, form = EXCLUDED.form, updated_at = now()
                """
            ),
            {"sid": subject_id, "name": name, "form": body.form},
        )
        await session.commit()
    log.info("endpoint.appearance_set", device=device_id, pet=name or "(default)", form=body.form)
    return PanelAppearance(pet_name=name, form=body.form)


@router.get("/panels/{device_id}/appearance")
async def get_panel_appearance(
    device_id: str, owner: OwnerDep, request: Request
) -> PanelAppearance:
    """What the owner last chose for this panel, so the PWA opens on the truth rather than on
    a default that would silently overwrite a real setting the moment he pressed save."""
    try:
        uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such panel") from None
    ctx = ctx_for(owner)
    async with scoped_session(request.app.state.session_maker, ctx) as session:
        subject_id = (
            await session.execute(
                text(
                    """
                    SELECT s.id::text FROM app.principals p
                    JOIN app.subjects s ON s.id = p.subject_id
                    WHERE p.id = CAST(:dev AS uuid) AND p.kind = 'device_key'
                      AND s.device_role IS NOT NULL
                    """
                ),
                {"dev": device_id},
            )
        ).scalar_one_or_none()
    if subject_id is None:
        raise HTTPException(status_code=404, detail="no such panel")
    return await _read_appearance(request, ctx, str(subject_id))


@router.get("/settings")
async def panel_settings(
    principal: PanelDep, request: Request, settings: SettingsDep
) -> EndpointSettings:
    """The knobs a panel applies to itself, fetched with its own key.

    `PanelDep`, so a panel reads this the same way it reads the manifest. The table is
    deliberately not `app.settings` — that one is gated on `app.is_owner()` and holds the
    Gmail client secret, the Moltbook key and the global kill, and a panel is a `device_key`
    precisely so a stolen one cannot reach them. See `0206_endpoint_settings.py`.

    Read under the caller's own context rather than an owner one: the `FOR SELECT` policy is
    what permits it, so nothing here depends on this route choosing to return only three
    fields.
    """
    # The ROLE IS NOT SERVED HERE, deliberately. A panel learns what it is from NVS, written by
    # the same flash that wrote its subject row — one source, one moment. Serving it from this
    # route as well would invite a second answer without giving the owner any way to change the
    # first: nothing rewrites a role after the flash, so the field would have described a
    # transition that cannot happen. When there is a way to re-role a unit in place, this is
    # where it belongs.
    #
    # THE APPEARANCE IS SERVED, and for the opposite reason: it is the owner's to change and the
    # panel's to obey, so there has to be a way for a change to reach a unit on a wall. Read
    # under the panel's own context, where `endpoint_panel_own` shows it exactly one row — its
    # own — so this route cannot be talked into describing a sibling.
    ctx = ctx_for(principal)
    knobs = await _read_settings(request, ctx)
    look = await _read_appearance(request, ctx, getattr(principal, "subject_id", "") or "")
    # An empty string when this box has no firmware in its checkout, NOT a 503 like the manifest
    # route: the knobs are still the truth and a panel that cannot be told about an update must
    # still be told how bright to be. The panel reads "" as "nothing to compare against".
    return knobs.model_copy(
        update={
            "pet_name": look.pet_name,
            "form": look.form,
            "fw_version": _firmware_version(settings) or "",
        }
    )


@router.put("/settings")
async def set_panel_settings(
    owner: OwnerDep, request: Request, body: EndpointSettings
) -> EndpointSettings:
    """Set the knobs. Owner only, and the values are clamped rather than rejected.

    A panel picks these up at its next cycle, and the five-second hold on the glass forces a
    reboot — which re-fetches immediately. So tuning is seconds rather than the build, CI,
    deploy and OTA cycle every one of these numbers used to cost.
    """
    clamped = _clamp(body)
    ctx = ctx_for(owner)
    async with scoped_session(request.app.state.session_maker, ctx) as session:
        await session.execute(
            text(
                "UPDATE app.endpoint_settings SET volume = :v, mic_gain_db = :g,"
                " brightness = :b, debug_overlay = :d, mic_agc = :agc,"
                " dim_percent = :dim, updated_at = now() WHERE id = 1"
            ),
            {
                "v": clamped.volume,
                "g": clamped.mic_gain_db,
                "b": clamped.brightness,
                "d": clamped.debug_overlay,
                "agc": clamped.mic_agc,
                "dim": clamped.dim_percent,
            },
        )
        await session.commit()
    log.info(
        "endpoint.settings_set",
        volume=clamped.volume,
        mic_gain_db=clamped.mic_gain_db,
        debug_overlay=clamped.debug_overlay,
        brightness=clamped.brightness,
        # So a clamped write is visible as a clamp rather than as the owner's own number.
        asked=body.model_dump(),
    )
    return clamped


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
    # jpet or Jeff — the one flag that decides what this unit IS. A `jpet` joins the twins'
    # roster and can exchange voice messages with its sibling; a `display` is an endpoint the
    # owner operates (OTA, settings, telemetry, /converse) and is never addressable by a pet.
    # Defaults to `jpet` because that is what every panel flashed so far is, and a default that
    # silently changed what existing units are would be a worse bug than the one this fixes.
    role: PanelRole = "jpet"
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
    role: PanelRole = "jpet",
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

    # A fresh CREDENTIAL per flash, on the shipped `device_key` substrate rather than a new auth
    # model (ROOM_ENDPOINT_PLAN.md §3). The plaintext key exists only inside this request: it
    # goes into NVS and is never stored here, which is the same contract the owner's own key
    # rotation has. Re-flashing therefore issues a new key and the old one stops working at that
    # moment, which is what this comment promised long before anything implemented it.
    #
    # THE IDENTITY, THOUGH, IS NOT FRESH — and that is the correction. This used to provision a
    # whole new subject every time, so one panel became thirteen subjects under one name and
    # there was nowhere durable to record what that unit was called or what body it wore. A
    # panel is now the subject and the key merely hangs off it, the way a phone's always has.
    label = panel_label(name)
    provisioned = await devices.provision_or_reflash(device_repo, ctx, label, device_role=role)

    api_base, ca = _panel_base(request, settings)
    nvs = {
        "ssid": ssid,
        "pass": password,
        "api": api_base,
        "token": provisioned.key,
        "name": name,
        # The firmware's one flag. It reads this at boot to decide whether to run the twin
        # side at all — a display never polls for a sibling's voice messages.
        "role": role,
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
        role=body.role,
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


# ---- the panel's conversation turn -------------------------------------------------------

# 16 kHz mono signed 16-bit, which is what the panel captures and what it can play. The rate
# was chosen for ESP-SR rather than for fidelity (firmware/main/audio.h), and everything on
# this route stays in it so the panel needs no decoder, no resampler and no WAV parser — see
# `docs/proposed/PANEL_CONVERSATION_PLAN.md`. The box has CPU to spare; the panel has 31 KB of
# contiguous internal RAM on a good day.
PANEL_RATE = 16000
# WHATEVER THE PANEL CAN CAPTURE, THE BOX MUST ACCEPT — this number's whole job is to be no
# smaller than `CAPTURE_MAX_MS` in `firmware/main/audio.c`, and a unit test reads that constant
# out of the firmware to keep it so. Six seconds became ten when the owner said *"the babies
# keep getting cut off because they're a little bit slow"* — a four-year-old stops in the
# MIDDLE of a breath, and the panel's silence window has to be able to wait that out
# (`LISTEN_HUSH_MS`) inside the same cap.
#
# Thirty at the owner's ask (0.2.95), and the ask was about MESSAGES — but the panel has one
# capture buffer and it feeds both, so a thirty-second question to the pet arrives here too.
# Truncating it at ten would have cut the tail off a child's question with nothing said about
# it; the test caught that, which is what it is for. Still short enough that a pocketed panel
# cannot upload a minute of a room, and the extra length is free because `_trim_to_speech`
# takes the silence back out before whisper ever sees it.
PANEL_AUDIO_MAX = PANEL_RATE * 2 * 30

# WHAT THE PANEL CAN ACTUALLY PLAY, which the box has to know because it is the box that
# overruns it. `firmware/main/talk.c` reads the reply into a fixed PSRAM buffer and
# `audio_play` truncates to the same ceiling, silently — so a reply longer than this does not
# fail, it stops mid-word. The owner: *"sometimes when the robot is talking back on a longer
# reply I get cut off."* Measured in the log the same afternoon: replies of 221,012 and
# 261,290 bytes against the 192,000 the panel then held, so the 261 KB one lost its last 2.2
# seconds. Both ends now say ten seconds; a box that outruns a panel that has not been
# updated yet truncates exactly as it did before, which is why the two can ship in either
# order. What must never happen again is it being SILENT, hence the warning below.
PANEL_REPLY_MAX = PANEL_RATE * 2 * 10

# DELIBERATELY PLAIN, AND DELIBERATELY SHORT. The jpet's prompt is built around wall objects,
# scene effects and an action script schema; none of that exists on a panel, and inheriting it
# would have the pet narrating furniture a child cannot see. The owner asked to "start off very
# easy, just a generic conversation prompt", so this is the smallest thing that behaves: who it
# is, who it is talking to, and the one constraint that actually matters — every word here is
# SPOKEN ALOUD through a small speaker, so length is not a style preference, it is latency the
# child waits through.
PANEL_CONVERSATION_PROMPT = """You are a small friendly robot pet who lives on a little \
screen in a child's bedroom. You are talking with a four-year-old.

FOLLOW THEIR LEAD. Talk about whatever they just brought up — what they are doing, what they \
ate, what they did today, their toys, their room, their animals, the people they know. Stay \
on their subject instead of changing it.

SAY THEIR IDEA BACK, BIGGER. Agree, repeat what they said in slightly fuller words, then add \
one small new thing. If they say "doggy runned", say "Yes! The doggy ran so fast." Never tell \
them they got a word wrong and never correct them — saying it back properly is the whole \
trick.

ASK ONE OPEN QUESTION, and never a quiz. "What happened next?" or "Tell me about it" gets a \
real answer; a question they can answer with yes or no ends the conversation. One question \
per reply, never two.

GUESS KINDLY WHEN THE WORDS COME OUT WRONG. You hear them through a tiny microphone and it \
mishears small children constantly. Work out what a four-year-old most likely meant and \
answer that. Only ask them to say it again if you truly cannot guess.

You have no body, no camera and no hands. You cannot look at things, fetch things, go \
anywhere, or play games that need moving or seeing, so never offer to — an offer you cannot \
keep is a promise broken, and a four-year-old will hold you to it.

But you CAN talk, and talking is nearly everything a four-year-old wants. Tell jokes, make up \
little stories, sing silly songs, count things, play guessing games with words, be silly. If \
they ask for a joke, just tell one. Never answer that you are a robot who cannot do things.

Reply with ONE or TWO short spoken sentences. Never more.
Be warm and patient. Use simple words a four-year-old knows.
Your reply is read aloud, so write only what should be said — no emoji, no asterisks, no \
stage directions, no lists."""


# IT HAD NO IDEA WHAT IT HAD JUST SAID.
#
# The owner, on the pet: *"it also asks to play a game a lot"*. The prompt is part of it, but
# not the whole: this route sent one utterance and nothing else, so every turn of a six-turn
# hands-free conversation arrived as the first thing anyone had ever said. A model with no
# idea it asked about a game last time asks about a game again — and a follow-up question it
# does ask gets answered into a void, which is what makes the toy feel like it is not
# listening rather than merely slow.
#
# IN MEMORY, AND ONLY WHILE THE CONVERSATION IS HAPPENING. The route's promise is that "a
# stolen panel key is worth exactly one conversation", and that stays true: this is the ONE
# conversation, in this process, for as long as it is still going on. Nothing reaches the
# database, no domain is touched, and a restart forgets — which for a bedroom toy is not a
# defect, because a four-year-old starting again in the morning is starting again.
_PANEL_TURNS_KEPT = 5
# Longer than the 2 s the panel waits before reopening the microphone and far shorter than an
# afternoon: the same child coming back after tea is a new conversation, not turn seven.
_PANEL_MEMORY_TTL_S = 240.0
_panel_memory: dict[str, tuple[float, list[tuple[str, str]]]] = {}


def _panel_history(key: str, now: float) -> list[tuple[str, str]]:
    """This panel's live conversation, and a sweep of everyone else's dead ones — the
    cheapest possible expiry, on a dict that holds one entry per panel in the house."""
    for stale in [k for k, (seen, _) in _panel_memory.items() if now - seen > _PANEL_MEMORY_TTL_S]:
        del _panel_memory[stale]
    entry = _panel_memory.get(key)
    return list(entry[1]) if entry else []


# THE PANEL'S NAME, STRIPPED OFF THE FRONT OF WHAT IT HEARD.
#
# The owner: *"if I say hey fish and then proceed with asking it something, it shouldn't be
# transcribed hey fish at the beginning."*
#
# WHY THE AUDIO CANNOT BE TRUSTED TO EXCLUDE IT. The obvious place to fix this is the panel —
# the recogniser fires on the phrase, so open the microphone after it and the name is already
# past. That is what happens on a cold start, and it is not the path this shows up on. After a
# reply the panel reopens the microphone by itself for a couple of seconds
# (`FOLLOW_LEAD_MS`), and a child who starts their next sentence with the pet's name is
# recorded saying ALL of it: the recogniser does fire, but `VOCAB_LISTEN` is refused because a
# turn is already live, so nothing trims anything and the whole utterance goes up. The name
# arrives inside the audio, so it has to come off the text.
#
# Stripped rather than left for the model to ignore, because it is not inert: it is the
# subject of the first sentence the model sees, and a four-year-old asking "hey fish, what do
# dogs eat" gets answers about fish.
#
# ONLY AS A PREFIX. A name in the middle of a sentence is the child talking about the pet, and
# deleting it there would change what they said.
#
# The variants are Whisper's, not ours: it has no idea this is a name and spells it by sound.
# The trailing \b is load-bearing — without it this eats the front of "fisherman".
_WAKE_PREFIX = re.compile(r"^\W*(?:hey|hay)\W+(?:fish|fishy|fisch|phish)\b\W*", re.IGNORECASE)


def _wake_prefix_for(pet_name: str) -> re.Pattern[str]:
    """The stripper for the name THIS panel answers to.

    **This has to follow a rename or the bug comes back.** The pattern above is the shipped name
    and its Whisper spellings; once the owner can rename the pet (migration 0212), a box still
    stripping `fish` would leave `hey pip` on the front of every transcript and send it to the
    model — which is exactly the symptom the owner reported and this stripper was written for.

    A CUSTOM NAME GETS NO VARIANTS, and that is a real limitation rather than an oversight.
    `fishy|fisch|phish` are transcriptions of the shipped name that were OBSERVED coming back
    from Whisper; nobody can know in advance how it will spell a name it has never been given,
    and guessing homophones would risk eating a word the child actually said. So a renamed pet
    strips its name spelled correctly, and the occasional mis-spelt wake reaches the model as
    part of the question — which reads as the pet answering something slightly odd, not as the
    pet being deaf.
    """
    name = " ".join(pet_name.split())
    if not name:
        return _WAKE_PREFIX
    words = r"\W+".join(re.escape(w) for w in name.split(" "))
    return re.compile(rf"^\W*(?:hey|hay)\W+(?:{words})\b\W*", re.IGNORECASE)


def _strip_wake_prefix(text: str, pet_name: str = "") -> str:
    """`text` without a leading wake phrase. Unchanged when it does not start with one.

    An utterance that was ONLY the name becomes empty, which is right: there is no question in
    it, and the caller already treats empty as "say that again" rather than as an error. That
    is the correct answer to an accidental wake and a better one than a reply about fish.

    ONLY AS A PREFIX, still: a name in the middle of a sentence is the child talking ABOUT the
    pet, and deleting it there would change what they said."""
    return _wake_prefix_for(pet_name).sub("", text, count=1).strip()


def _panel_remember(key: str, now: float, heard: str, reply: str) -> None:
    turns = _panel_history(key, now)
    turns.append((heard, reply))
    _panel_memory[key] = (now, turns[-_PANEL_TURNS_KEPT:])


def _with_history(turns: list[tuple[str, str]]) -> str:
    """The conversation so far, folded into the system prompt. `complete` takes one user
    message, so this is where a transcript goes without a router change — and at five turns
    of a four-year-old it costs a few dozen tokens, which is nothing next to being answered
    as though they had not spoken."""
    if not turns:
        return PANEL_CONVERSATION_PROMPT
    said = "\n".join(f"They said: {heard}\nYou answered: {reply}" for heard, reply in turns)
    return (
        f"{PANEL_CONVERSATION_PROMPT}\n\n"
        f"You are part-way through a conversation. It has gone like this so far, "
        f"oldest first:\n{said}\n\n"
        f"Carry it on. Do not greet them again and do not ask something you already asked."
    )


# When the model is slow, missing or broken. Never the same line twice in a row by luck, and
# never an error: a toy that goes quiet when a container restarts reads as a broken toy.
PANEL_BABBLE = (
    "Ooh, my thinking went all fuzzy. Say that again?",
    "Beep! I lost that one. What did you say?",
    "Hmm! My brain did a wobble. Tell me again?",
    "Whoops, I was daydreaming. One more time?",
)


def _wav(pcm: bytes, rate: int = PANEL_RATE) -> bytes:
    """Wrap raw mono s16 in a WAV header. Whisper takes a file, the panel sends a stream."""
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def _pcm_from_wav(data: bytes) -> tuple[bytes, int]:
    """The samples and the rate out of a WAV, without a dependency.

    Kokoro's output is not the panel's rate and the chunk layout is not guaranteed, so the
    header is walked rather than assumed — a fixed 44-byte skip is the bug that ships as a
    burst of noise at the start of every reply.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV")
    rate, pos = PANEL_RATE, 12
    pcm = b""
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = data[pos + 8 : pos + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            rate = struct.unpack("<I", body[4:8])[0]
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)  # chunks are word-aligned
    if not pcm:
        raise ValueError("WAV had no data chunk")
    return pcm, rate


def _to_panel_rate(pcm: bytes, rate: int) -> bytes:
    """Linear resample to the panel's 16 kHz. Speech at this rate through a 29 mm speaker does
    not reward anything cleverer, and a polyphase filter would be a dependency for nothing."""
    if rate == PANEL_RATE or not pcm:
        return pcm
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    out = array.array("h")
    step = rate / PANEL_RATE
    pos = 0.0
    while pos < len(src) - 1:
        i = int(pos)
        frac = pos - i
        out.append(int(src[i] + (src[i + 1] - src[i]) * frac))
        pos += step
    return out.tobytes()


# THE CLIP IS MOSTLY ROOM, AND WHISPER IS CHARGED FOR ALL OF IT.
#
# Sizing the encoder window to the clip (below) cut a turn from 9.5 s to 2.3 s and then
# stopped, because the clip is not the sentence. The panel opens the microphone, waits up to
# three seconds for a child to start, records, and then waits 900 ms of silence to decide they
# have finished — so *"yes, we wanted a story"* arrives as six seconds of audio with about a
# second and a half of speech in it. Measured 2026-09-22: every turn in the log reports
# `audio_ctx` 390-450, which is the six-second cap, whatever was actually said.
#
# So find the speech and send that. The panel already knows where it is — `speech.c` has a VAD
# running for the wake word — but it does not tell us, and a backend fix ships without an OTA.
#
# THE GATE IS RELATIVE TO THE ROOM, NOT A NUMBER. A fixed threshold is wrong in both
# directions: it deafens a quiet child and it hears a noisy bedroom with a fan in it. The
# noise floor is taken as the 10th-percentile frame — which the 900 ms hush alone guarantees
# is silence, and which one door slam therefore cannot move — and the gate sits three times
# above it. A room so loud that nothing clears the gate trims nothing at all rather than
# trimming wrongly, which is the failure worth having: an extra half second of silence costs
# whisper a few milliseconds, and a clipped word is a wrong answer read aloud to a child.
_TRIM_FRAME = PANEL_RATE // 50  # 20 ms: a word boundary is findable, one sample is not
_TRIM_LEAD_MS = 200
# Longer than the lead on purpose. A four-year-old trailing off at the end of a sentence is
# the clip this must not lose, and whisper reads a trailing breath as punctuation. Together
# the two margins also put a 620 ms floor under any result, so a one-word turn still arrives
# as a window whisper can place rather than a syllable it guesses at.
_TRIM_TAIL_MS = 400
_TRIM_QUIET = 300  # on a 32767 scale; below this a room is just a room


def _trim_to_speech(
    pcm: bytes, lead_ms: int = _TRIM_LEAD_MS, tail_ms: int = _TRIM_TAIL_MS
) -> bytes:
    """The speech inside a clip of mostly silence, with margins. The clip unchanged if
    there is no telling — never an empty one, because silence is `204` downstream and a
    truncated word is a wrong answer read aloud to a child.

    The margins are arguments because the two ends of a turn want different ones. A child's
    recording needs room for a syllable the gate nearly missed; Kokoro's output has exact
    digital silence at its edges and wants almost none, so the reply starts when the reply
    starts."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    frames = len(samples) // _TRIM_FRAME
    if frames < 8:  # under 160 ms there is nothing to find and nothing to save
        return pcm
    peaks = []
    for i in range(frames):
        seg = samples[i * _TRIM_FRAME : (i + 1) * _TRIM_FRAME]
        peaks.append(max(max(seg), -min(seg)))
    gate = max(sorted(peaks)[frames // 10] * 3, _TRIM_QUIET)
    voiced = [i for i, peak in enumerate(peaks) if peak >= gate]
    if not voiced:
        return pcm

    start = max(0, voiced[0] * _TRIM_FRAME - lead_ms * PANEL_RATE // 1000)
    end = min(len(samples), (voiced[-1] + 1) * _TRIM_FRAME + tail_ms * PANEL_RATE // 1000)
    return samples[start:end].tobytes()


@router.post("/converse")
async def converse(principal: PanelDep, request: Request) -> Response:
    """A child holds the panel, talks, and the panel answers out loud.

    THE SAME SHAPE AS THE WALL'S `/internal/pet/say`, which is the owner's instruction and
    also the right call: that route already solved the hard part, which is not the model but
    the failure behaviour. A slow, unconfigured or broken LLM there degrades to a canned line
    rather than a 500, so the toy always says something. A pet in a child's bedroom that goes
    silent when a container is restarting is indistinguishable from a broken pet.

    In, raw 16 kHz mono s16 — no container, no codec, because the panel has neither. Out, the
    same. Every conversion happens here (see `_to_panel_rate`): the box has CPU to spare and
    the panel has 31 KB of contiguous internal RAM.

    `PanelDep`, so a panel talks with the device key it already uses for its manifest and
    telemetry. Nothing is stored — this holds no memories and touches no domain, which keeps
    a stolen panel key worth exactly one conversation.
    """
    started = time.monotonic()
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=400, detail="no audio")
    if len(audio) > PANEL_AUDIO_MAX:
        audio = audio[:PANEL_AUDIO_MAX]
    held_ms = len(audio) * 1000 // (PANEL_RATE * 2)
    audio = _trim_to_speech(audio)
    spoken_ms = len(audio) * 1000 // (PANEL_RATE * 2)

    settings = cast(Settings, request.app.state.settings)
    if not settings.whisper_url:
        raise HTTPException(status_code=503, detail="speech recognition not configured")

    client = WhisperCppClient(
        base_url=settings.whisper_url,
        model=settings.whisper_model,
        timeout=min(settings.whisper_timeout, 60.0),
    )
    # THE ENCODER WINDOW, SIZED TO THE CLIP — the single largest cost in a panel turn.
    #
    # Whisper pads every clip to 30 seconds and encodes all of it, which is why §10.4ca
    # measured 9,564 ms and 9,549 ms for two utterances of very different length. The panel
    # records at most six seconds (`CAPTURE_MAX_MS`), so the default window does five times
    # the work this route can ever need.
    #
    # ~50 encoder frames per second of audio against 1500 for the full window, with half as
    # much again for margin and a floor — a window trimmed too close truncates the tail of a
    # sentence, and a four-year-old trailing off is exactly the clip that would lose it.
    #
    # The floor was 256 while the clip was the whole six-second hold and the margin was the
    # only thing standing between a word and a guess. `_trim_to_speech` now hands this the
    # sentence with 200/400 ms of room either side, so the floor can be what a real utterance
    # needs rather than what a padded one did: 160 is ~2.1 s of audio after the 1.5x margin,
    # which is longer than anything that survives the trim.
    seconds = len(audio) / float(PANEL_RATE * 2)
    audio_ctx = max(160, min(1500, int(seconds * 50 * 1.5)))
    stt_started = time.monotonic()
    try:
        transcript = await client.transcribe(
            _wav(audio),
            filename="panel.wav",
            media_type="audio/wav",
            audio_ctx=audio_ctx,
            # A bedroom in an English-speaking house has already answered this; without it
            # whisper spends a decode pass detecting the language of every utterance.
            language="en",
        )
    except Exception as exc:  # noqa: BLE001 — the panel gets an answer or a reason, never a hang
        log.warning("endpoint.converse_stt_error", error=repr(exc))
        raise HTTPException(status_code=503, detail="could not hear") from exc
    raw_heard = (transcript.text or "").strip()
    # THE NAME THIS PANEL ANSWERS TO, not the one the firmware shipped with. The owner can
    # rename the pet (migration 0212) and the wake word changes with it, so a box stripping a
    # constant would leave `hey pip` on the front of every question and send it to the model.
    look = await _read_appearance(
        request, ctx_for(principal), getattr(principal, "subject_id", "") or ""
    )
    heard = _strip_wake_prefix(raw_heard, look.pet_name)
    stt_ms = int((time.monotonic() - stt_started) * 1000)

    if not heard:
        # Silence is not an error. The panel shows "say that again" rather than a failure face.
        log.info(
            "endpoint.converse",
            heard="",
            # What the transcriber actually returned, when the name was all of it. Otherwise
            # an accidental wake and a dead microphone log identically, and they are not the
            # same fault.
            **({"raw_heard": raw_heard} if raw_heard else {}),
            stt_ms=stt_ms,
            audio_ctx=audio_ctx,
            held_ms=held_ms,
            spoken_ms=spoken_ms,
            reply="",
        )
        return Response(status_code=204)

    # THE LLM MUST NOT BREAK THE TOY — the jpet's rule, and the reason its `_say` is wrapped.
    reply = ""
    turns = _panel_history(principal.id, started)
    llm_started = time.monotonic()
    try:
        result = await cast(LlmRouter, request.app.state.llm_router).complete(
            "pet.turn",
            system=_with_history(turns),
            user_text=heard[:500],
            max_tokens=256,
        )
        reply = (result.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("endpoint.converse_llm_error", error=repr(exc))
    if reply:
        _panel_remember(principal.id, started, heard, reply)
    else:
        # A babble is the toy apologising for a model that did not answer. Remembering it
        # would put "You answered: my brain did a wobble" in front of the next turn, and the
        # model would take the hint and wobble again.
        reply = random.choice(PANEL_BABBLE)
    llm_ms = int((time.monotonic() - llm_started) * 1000)

    tts_started = time.monotonic()
    base = (settings.brain_tts_url or "").rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="speech synthesis not configured")
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(f"{base}/tts", params={"text": reply[:600]})
        resp.raise_for_status()
        wav_pcm, wav_rate = _pcm_from_wav(resp.content)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("endpoint.converse_tts_error", error=repr(exc))
        raise HTTPException(status_code=503, detail="could not speak") from exc
    out = _to_panel_rate(wav_pcm, wav_rate)
    # Kokoro pads. The panel cannot skip it — it plays what it is handed from the first
    # sample — so every reply starts with a beat of nothing, and that padding also counts
    # against the ceiling below. 30 ms in front and 120 ms behind keeps the reply from
    # sounding clipped while giving back the rest.
    out = _trim_to_speech(out, lead_ms=30, tail_ms=120)
    if len(out) > PANEL_REPLY_MAX:
        # NEVER SILENTLY. The panel truncates a long reply mid-word with nothing on screen
        # and nothing in a log, which is why this took a child complaining to find.
        log.warning(
            "endpoint.converse_reply_truncated",
            reply_bytes=len(out),
            ceiling=PANEL_REPLY_MAX,
            lost_ms=(len(out) - PANEL_REPLY_MAX) * 1000 // (PANEL_RATE * 2),
            reply=reply[:120],
        )
        out = out[:PANEL_REPLY_MAX]
    tts_ms = int((time.monotonic() - tts_started) * 1000)

    # THE THREE NUMBERS THAT DECIDE WHETHER THIS IS USABLE, on every turn. Whisper was
    # measured at ~9.8 s with the large model (api/sdr.py) and no thinking animation covers
    # that; this is how the owner finds out what it costs in the room rather than on a bench.
    log.info(
        "endpoint.converse",
        heard=heard[:120],
        reply=reply[:120],
        stt_ms=stt_ms,
        # Reported so the encoder window can be correlated with the cost it bought, rather
        # than the effect being asserted from a changelog. `held_ms` is what the panel
        # uploaded and `spoken_ms` what survived the trim: the gap between them is the room,
        # and if it ever closes the trim has stopped working.
        audio_ctx=audio_ctx,
        held_ms=held_ms,
        spoken_ms=spoken_ms,
        llm_ms=llm_ms,
        tts_ms=tts_ms,
        total_ms=int((time.monotonic() - started) * 1000),
        reply_bytes=len(out),
    )
    return Response(content=out, media_type="application/octet-stream")
