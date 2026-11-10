"""Flashing a GrowBot from the PWA, and the robots' status card (docs/plans/GROWBOT_PLAN.md §3,
wave G2).

The owner plugs the robot's Pico 2 W into the box and presses a button; there is no terminal
(CLAUDE.md #10). One flash is three phases, each shown as it happens:

- **load** — MicroPython onto the board (`/pico/load` on the endpoint sidecar: into its USB
  bootloader if it is running, then picotool). Skipped when MicroPython is already on it and
  no reinstall was asked for.
- **copy** — read the board's pairing code (`/pico/identify`), and only then mint its device
  key, so a wrong board is refused before any key changes; then the firmware and a generated
  `secrets.py` (`/pico/provision`).
- **join** — the robot says `WIFI_OK`, and the relay stamps its `last_seen_at` once its hello
  verifies, which is the proof it reached the box.

A rekey is identify, mint, `/pico/rekey` (swap the key line in place) and join.

WHAT CROSSES WHICH BOUNDARY. As for the panels (`api/endpoint.py`): the sidecar never fetches
and keeps nothing. This module reads the firmware off the read-only checkout mount and checks
`SHA256SUMS`, holds the Wi-Fi passwords and the minted key only in this request and the job's
task (never in the job's log, which is redacted), and refuses to flash a robot that would dial
the box unverified (no readable LAN root, no flash).

One job at a time, in-process (`app.state.growbot_flash`): the api runs one uvicorn worker, and
a box has one USB bus worth of robots on a desk. The finished job is kept until the next one
starts, so the PWA can come back to it.

Rules: no LLM (#1 n/a). The firmware is a file in this box's own checkout, an infrastructure
mount like the panels' (#2). The robots table is owner-only under RLS (migration 0254/0255, #3).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from jbrain.api import endpoint as endpoint_api
from jbrain.api import growbot
from jbrain.api.deps import DebugDep, OwnerDep, SettingsDep
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.growbot import provision
from jbrain.growbot.repo import CODE_RE, NAME_MAX, GrowbotRepo, RobotInfo
from jbrain.homenet import adguard
from jbrain.settings_store import ENDPOINT_WIFI_KEY

log = structlog.get_logger()

router = APIRouter(prefix="/growbot", tags=["growbot"])
debug_router = APIRouter(prefix="/debug/growbot")

MICROPYTHON_VERSION = "1.28.0"
UF2_NAME = "RPI_PICO2_W-20260406-v1.28.0.uf2"
PICO_MICROPYTHON_PID = "0005"
RP2350_BOOT_PID = "000f"
LOG_MAX = 400
JOIN_WAIT_S = 60.0
JOIN_POLL_S = 2.0
WATCH_S = 120
_DEBUG_OWNER = SessionContext(principal_id="debug-console", principal_kind="owner")
# The endpoint sidecar's streams: quiet stretches (a Wi-Fi join, picotool's verify) are
# normal, a minute of nothing at all is not.
_STREAM_TIMEOUT = httpx.Timeout(10.0, read=180.0)
# Tests swap in an httpx.MockTransport; None is the real network.
_transport: httpx.AsyncBaseTransport | None = None

Phase = Literal["wait", "run", "done", "fail", "skip"]
FailCode = Literal[
    "load_failed",
    "not_bootsel",
    "usb_gone",
    "wrong_robot",
    "copy_failed",
    "wifi_failed",
    "box_unreached",
    "timeout",
    "internal",
]
FAIL_CODES: frozenset[str] = frozenset(FailCode.__args__)  # type: ignore[attr-defined]

# What the owner can do about each failure. The PWA has its own copy; this one is what the
# debug API and the job's `fail.message` carry.
RECOVERY: dict[str, str] = {
    "load_failed": "MicroPython did not load. Unplug the robot, plug it back in and try again.",
    "not_bootsel": (
        "The board did not enter its bootloader. Hold BOOTSEL while you plug it in, then try again."
    ),
    "usb_gone": "The robot dropped off USB. Check the cable is a data cable and try again.",
    "wrong_robot": (
        "That is a different robot from the one you picked. Plug in the right one, or add this"
        " one as a new robot."
    ),
    "copy_failed": "Copying to the robot failed. Unplug it, plug it back in and try again.",
    "wifi_failed": (
        "It could not join any of its networks. Check the password, and that the network is"
        " 2.4 GHz."
    ),
    "box_unreached": (
        "It joined Wi-Fi but never reached the box. Check the box's address, and that the robot"
        " and the box are on the same network."
    ),
    "timeout": "The robot went quiet. Unplug it, plug it back in and try again.",
    "internal": "Something went wrong on the box. Try again; the log says what.",
}


# -- the job ---------------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    mode: str
    name: str
    box_host: str
    started_at: datetime
    state: str = "running"
    code: str | None = None
    port: str | None = None
    phases: dict[str, str] = field(
        default_factory=lambda: {"load": "wait", "copy": "wait", "join": "wait"}
    )
    log: list[str] = field(default_factory=list)
    fail: dict[str, str] | None = None
    finished_at: datetime | None = None
    # Strings that must never reach the log (the minted key, the Wi-Fi passwords). Kept on
    # the job only while it runs; never serialised.
    secrets: list[str] = field(default_factory=list, repr=False)

    def say(self, line: str) -> None:
        for secret in self.secrets:
            if secret:
                line = line.replace(secret, "[redacted]")
        self.log.append(line[:512])
        if len(self.log) > LOG_MAX:
            del self.log[: len(self.log) - LOG_MAX]

    def phase(self, name: str, state: Phase) -> None:
        self.phases[name] = state

    def running_phase(self) -> str:
        for name in ("join", "copy", "load"):
            if self.phases[name] == "run":
                return name
        return "copy"

    def out(self) -> JobOut:
        # Validated rather than constructed: the job keeps plain strings, and the model is
        # what holds them to the contract's literals.
        return JobOut.model_validate(
            {
                "id": self.id,
                "mode": self.mode,
                "state": self.state,
                "name": self.name,
                "code": self.code,
                "port": self.port,
                "box_host": self.box_host,
                "phases": self.phases,
                "log": list(self.log),
                "fail": self.fail,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }
        )


class _Failed(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code if code in FAIL_CODES else "internal"
        self.detail = detail


@dataclass
class FlashState:
    job: Job | None = None
    tasks: set[asyncio.Task[None]] = field(default_factory=set)


def _state(request: Request) -> FlashState:
    state = getattr(request.app.state, "growbot_flash", None)
    if not isinstance(state, FlashState):
        state = FlashState()
        request.app.state.growbot_flash = state
    return state


# -- shapes ----------------------------------------------------------------------------------


class PhasesOut(BaseModel):
    # `copy` on the wire; pydantic's BaseModel already has a `copy` attribute.
    model_config = ConfigDict(populate_by_name=True)
    load: Phase
    copy_: Phase = Field(alias="copy")
    join: Phase


class FailOut(BaseModel):
    code: FailCode
    message: str


class JobOut(BaseModel):
    id: str
    mode: Literal["flash", "rekey"]
    state: Literal["running", "ok", "fail"]
    name: str
    code: str | None
    port: str | None
    box_host: str
    phases: PhasesOut
    log: list[str]
    fail: FailOut | None
    started_at: datetime
    finished_at: datetime | None


class JobStartOut(BaseModel):
    job: JobOut


class JobGetOut(BaseModel):
    job: JobOut | None


class NetworkIn(BaseModel):
    ssid: str = Field(min_length=1, max_length=64)
    # Omitted only for a network the box already remembers.
    password: str | None = Field(default=None, max_length=128)
    remember: bool = False


class FlashIn(BaseModel):
    mode: Literal["flash", "rekey"]
    target: str = Field(pattern=r"^(new|gb-[A-Za-z0-9]{1,32})$")
    name: str | None = Field(default=None, max_length=NAME_MAX)
    reinstall: bool = False
    networks: list[NetworkIn] | None = Field(default=None, max_length=provision.MAX_NETWORKS)
    box_host: str | None = Field(default=None, max_length=64)


class UsbOut(BaseModel):
    kind: Literal["boot", "micropython"]
    port: str | None
    label: str


class NetworkOut(BaseModel):
    ssid: str
    remembered: bool = True


class MicroPythonOut(BaseModel):
    version: str
    ready: bool


class SetupOut(BaseModel):
    flasher: bool
    usb: list[UsbOut]
    networks: list[NetworkOut]
    box_host: str | None
    lan: bool
    ca: bool
    micropython: MicroPythonOut
    firmware_ready: bool


class StatusOut(BaseModel):
    service: Literal["up", "down"]
    connected: int
    robots: list[growbot.RobotOut]


# -- the firmware on the mount ---------------------------------------------------------------


def _firmware_dir(settings: Settings) -> Path:
    return Path(settings.growbot_firmware_dir)


def _sums(settings: Settings) -> dict[str, str]:
    try:
        text = (_firmware_dir(settings) / "SHA256SUMS").read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1].lstrip("*")] = parts[0].lower()
    return out


def _checked(settings: Settings, name: str, sums: dict[str, str]) -> bytes | None:
    """One file off the mount if SHA256SUMS lists it and it matches, else None. A file the
    sums do not list is refused too: a half-updated checkout must not flash."""
    want = sums.get(name)
    if want is None:
        return None
    try:
        data = (_firmware_dir(settings) / name).read_bytes()
    except OSError:
        return None
    return data if hashlib.sha256(data).hexdigest() == want else None


def _readiness(settings: Settings) -> tuple[bool, bool]:
    sums = _sums(settings)
    uf2 = _checked(settings, UF2_NAME, sums) is not None
    files = all(_checked(settings, src, sums) is not None for src, _ in provision.FILE_MAP)
    return uf2, files


def _firmware(settings: Settings) -> tuple[bytes, list[tuple[str, bytes]]]:
    sums = _sums(settings)
    uf2 = _checked(settings, UF2_NAME, sums)
    if uf2 is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"This box has no MicroPython {MICROPYTHON_VERSION} to load (growbot/firmware/"
                f"{UF2_NAME} is missing or does not match SHA256SUMS). Run Ops -> Update,"
                " then try again."
            ),
        )
    files: list[tuple[str, bytes]] = []
    for src, dest in provision.FILE_MAP:
        data = _checked(settings, src, sums)
        if data is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"This box's GrowBot firmware is incomplete (growbot/firmware/{src} is"
                    " missing or does not match SHA256SUMS). Run Ops -> Update, then try again."
                ),
            )
        files.append((dest, data))
    return uf2, files


# -- the endpoint sidecar --------------------------------------------------------------------


def _client(timeout: float | httpx.Timeout = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transport, trust_env=False)


async def _ports(base: str) -> dict[str, Any]:
    try:
        async with _client() as client:
            resp = await client.get(f"{base}/ports")
            resp.raise_for_status()
            found = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"The robot flasher is not answering ({type(exc).__name__}). Try again in"
            " a minute; Ops shows whether the endpoint service is running.",
        ) from exc
    return found if isinstance(found, dict) else {}


def _usb(found: dict[str, Any]) -> list[UsbOut]:
    usb: list[UsbOut] = []
    for p in found.get("ports") or []:
        if isinstance(p, dict) and p.get("is_pico") and p.get("pid") == PICO_MICROPYTHON_PID:
            usb.append(UsbOut(kind="micropython", port=str(p["device"]), label=str(p["label"])))
    for d in found.get("pico_boot") or []:
        if isinstance(d, dict):
            usb.append(UsbOut(kind="boot", port=None, label=str(d.get("label") or "")))
    return usb


async def _lines(base: str, path: str, payload: dict[str, Any]) -> AsyncIterator[str]:
    """A sidecar stream's lines. A refusal before the stream (a 4xx) is an internal fault:
    every input was validated here first."""
    try:
        async with (
            _client(_STREAM_TIMEOUT) as client,
            client.stream("POST", f"{base}{path}", json=payload) as resp,
        ):
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")[:300]
                raise _Failed("internal", f"the flasher refused {path}: {body}")
            async for line in resp.aiter_lines():
                yield line
    except httpx.HTTPError as exc:
        raise _Failed("usb_gone", f"the flasher stopped answering ({type(exc).__name__})") from exc


async def _drive(
    job: Job,
    base: str,
    path: str,
    payload: dict[str, Any],
    on_line: Callable[[str], None] | None = None,
) -> str:
    """Run one sidecar stream into the job's log; returns its final `OK…` line."""
    async for line in _lines(base, path, payload):
        if line.startswith("FAILED: "):
            code, _, detail = line[len("FAILED: ") :].partition(": ")
            job.say(line)
            raise _Failed(code, detail)
        job.say(line)
        if line.startswith("port="):
            job.port = line[len("port=") :]
        if on_line is not None:
            on_line(line)
        if line == "OK" or line.startswith("OK "):
            return line
    raise _Failed("internal", f"the flasher's {path} ended without a result")


async def _identify(job: Job, base: str, port: str) -> str:
    try:
        async with _client(60.0) as client:
            resp = await client.post(f"{base}/pico/identify", json={"port": port})
            found = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _Failed("usb_gone", f"identify failed ({type(exc).__name__})") from exc
    if resp.status_code != 200 or not isinstance(found, dict):
        detail = str(found.get("error") if isinstance(found, dict) else found)
        code = str(found.get("code") or "usb_gone") if isinstance(found, dict) else "usb_gone"
        raise _Failed(code, detail)
    code = str(found.get("code") or "")
    if not CODE_RE.fullmatch(code):
        raise _Failed("internal", f"the board gave no pairing code ({code!r})")
    job.say(
        f"PAIRING CODE: {code} (MicroPython {found.get('micropython')}, {found.get('machine')})"
    )
    return code


# -- Wi-Fi -----------------------------------------------------------------------------------


def _wifi_store(request: Request) -> Any:
    return endpoint_api._store(request)


def _remembered(row: Any) -> list[tuple[str, str]]:
    """The remembered networks in order: the list, then the panels' single pair if it is not
    already in it (the row is shared with the panel flasher, which reads only that pair)."""
    if not isinstance(row, dict):
        return []
    out: list[tuple[str, str]] = []
    for n in row.get("networks") or []:
        if isinstance(n, dict) and n.get("ssid") and n.get("password"):
            out.append((str(n["ssid"]), str(n["password"])))
    top = (str(row.get("ssid") or ""), str(row.get("password") or ""))
    if top[0] and top[1] and all(s != top[0] for s, _ in out):
        out.append(top)
    return out


def _remember(row: Any, keep: list[tuple[str, str]]) -> dict[str, Any]:
    """The row with `keep` added or updated; the top pair is filled from the first network
    when missing, so the panels' remembered_wifi keeps working."""
    base = dict(row) if isinstance(row, dict) else {}
    networks = [
        dict(n) for n in base.get("networks") or [] if isinstance(n, dict) and n.get("ssid")
    ]
    for ssid, password in keep:
        for n in networks:
            if n["ssid"] == ssid:
                n["password"] = password
                break
        else:
            networks.append({"ssid": ssid, "password": password})
    base["networks"] = networks[-provision.MAX_NETWORKS :]
    if not base.get("ssid") and networks:
        base["ssid"], base["password"] = networks[0]["ssid"], networks[0]["password"]
    return base


# -- the address the robot dials ------------------------------------------------------------


async def _adguard_host(request: Request, settings: Settings) -> str | None:
    try:
        status = await adguard.for_request(request, settings).call("GET", "/control/status")
    except HTTPException:
        return None
    return provision.pick_box_host(
        status.get("dns_addresses") if isinstance(status, dict) else None
    )


def _sni(settings: Settings) -> str:
    return urlsplit(settings.lan_addr.strip()).hostname or ""


# -- setup -----------------------------------------------------------------------------------


async def _setup(request: Request, settings: Settings, ctx: SessionContext) -> SetupOut:
    usb: list[UsbOut] = []
    flasher = bool(settings.endpoint_url.strip())
    if flasher:
        try:
            usb = _usb(await _ports(endpoint_api._sidecar(settings)))
        except HTTPException:
            flasher = False
    row = await _wifi_store(request).get(ctx, ENDPOINT_WIFI_KEY)
    uf2_ready, files_ready = _readiness(settings)
    return SetupOut(
        flasher=flasher,
        usb=usb,
        networks=[NetworkOut(ssid=s) for s, _ in _remembered(row)],
        box_host=await _adguard_host(request, settings),
        lan=bool(_sni(settings)),
        ca=bool(endpoint_api._lan_ca()),
        micropython=MicroPythonOut(version=MICROPYTHON_VERSION, ready=uf2_ready),
        firmware_ready=files_ready,
    )


# -- starting a job --------------------------------------------------------------------------


@dataclass
class _Plan:
    mode: str
    target: str
    name: str | None
    base: str
    tty: str | None
    need_load: bool
    box_host: str
    sni: str
    ca: str
    networks: list[tuple[str, str]]
    uf2: bytes
    files: list[tuple[str, bytes]]
    existing: dict[str, RobotInfo]


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


async def _plan(request: Request, settings: Settings, ctx: SessionContext, body: FlashIn) -> _Plan:
    """Every refusal that can be made before a board is touched, in the contract's order."""
    flash = body.mode == "flash"
    base = endpoint_api._sidecar(settings)
    uf2, files = _firmware(settings) if flash else (b"", [])
    job = _state(request).job
    if job is not None and job.state == "running":
        raise _conflict("A robot is being flashed right now. Wait for it to finish.")
    sni = growbot._lan_host(settings)
    ca = endpoint_api._lan_ca()
    if flash and not ca:
        raise _conflict(
            "The box's LAN certificate cannot be read, and a robot without it would dial the"
            " box unverified. Run Ops -> Update to restart the proxy, then try again."
        )
    box_host = await _adguard_host(request, settings)
    if box_host is None and body.box_host:
        box_host = provision.pick_box_host([body.box_host.strip()])
        if box_host is None:
            raise _unprocessable("The box's address must be its private IPv4 LAN address.")
    if flash and box_host is None:
        raise _conflict(
            "The box does not know its own LAN address. Type it in (for example 192.168.1.20)"
            " and try again."
        )

    found = await _ports(base)
    usb = _usb(found)
    ttys = [u.port for u in usb if u.kind == "micropython" and u.port]
    boots = [
        d
        for d in found.get("pico_boot") or []
        if isinstance(d, dict) and d.get("pid") == RP2350_BOOT_PID
    ]
    if len(ttys) + len(boots) > 1:
        raise _conflict("More than one Pico is plugged into the box. Unplug all but the robot.")
    if not ttys and not boots:
        if found.get("pico_boot"):
            raise _conflict(
                "That is an RP2040 Pico. GrowBot needs a Pico 2 W; plug that in instead."
            )
        raise _conflict("No robot is plugged into the box. Plug its USB cable into the box.")
    tty = ttys[0] if ttys else None
    if not flash and tty is None:
        raise _conflict(
            "That board has no MicroPython yet, so there is no key to change. Add it as a new"
            " robot instead."
        )

    existing = {r.code: r for r in await growbot._repo(request).list_robots(ctx)}
    if body.target != "new" and body.target not in existing:
        raise HTTPException(status_code=404, detail="There is no robot with that code on this box.")
    name = (body.name or "").strip() or None
    if flash and body.target == "new" and not name:
        raise _unprocessable("Give the new robot a name.")

    networks: list[tuple[str, str]] = []
    if flash:
        if not body.networks:
            raise _unprocessable("Give the robot at least one Wi-Fi network.")
        store = _wifi_store(request)
        row = await store.get(ctx, ENDPOINT_WIFI_KEY)
        known = dict(_remembered(row))
        keep: list[tuple[str, str]] = []
        for n in body.networks:
            password = n.password
            if password is None:
                if n.ssid not in known:
                    raise _unprocessable(f"The box does not remember {n.ssid}. Type its password.")
                password = known[n.ssid]
            try:
                provision.validate_network(n.ssid, password)
            except provision.ProvisionError as exc:
                raise _unprocessable(str(exc)) from None
            if any(s == n.ssid for s, _ in networks):
                raise _unprocessable(f"{n.ssid} is listed twice.")
            networks.append((n.ssid, password))
            if n.remember and n.password is not None:
                keep.append((n.ssid, password))
        if keep:
            # Before the flash, like the panels': a flash that fails part-way still leaves
            # the owner able to retry without retyping.
            await store.upsert(ctx, ENDPOINT_WIFI_KEY, _remember(row, keep))

    return _Plan(
        mode=body.mode,
        target=body.target,
        name=name,
        base=base,
        tty=tty,
        need_load=flash and (tty is None or body.reinstall),
        box_host=box_host or "",
        sni=sni,
        ca=ca,
        networks=networks,
        uf2=uf2,
        files=files,
        existing=existing,
    )


async def _start(
    request: Request, settings: Settings, ctx: SessionContext, body: FlashIn
) -> JobStartOut:
    plan = await _plan(request, settings, ctx, body)
    existing = plan.existing.get(plan.target)
    job = Job(
        id=uuid.uuid4().hex,
        mode=plan.mode,
        name=plan.name or (existing.name if existing else "") or "",
        box_host=plan.box_host,
        started_at=datetime.now(UTC),
        port=plan.tty,
    )
    job.secrets = [p for _, p in plan.networks]
    state = _state(request)
    state.job = job
    repo = growbot._repo(request)
    task = asyncio.create_task(_run(job, plan, repo, ctx))
    state.tasks.add(task)  # hold a ref so the task is not collected mid-flight
    task.add_done_callback(state.tasks.discard)
    return JobStartOut(job=job.out())


# -- running a job ---------------------------------------------------------------------------


async def _run(job: Job, plan: _Plan, repo: GrowbotRepo, ctx: SessionContext) -> None:
    try:
        if plan.mode == "flash":
            await _flash(job, plan, repo, ctx)
        else:
            await _rekey(job, plan, repo, ctx)
        job.state = "ok"
        job.say("OK: the robot is paired and on the box.")
    except _Failed as exc:
        _fail(job, exc.code, exc.detail)
    except Exception as exc:  # noqa: BLE001 - a job must end in a state, never hang running
        log.exception("growbot.flash_crashed", job=job.id)
        _fail(job, "internal", type(exc).__name__)
    finally:
        job.finished_at = datetime.now(UTC)
        job.secrets = []


def _fail(job: Job, code: str, detail: str) -> None:
    job.phase(job.running_phase(), "fail")
    job.state = "fail"
    job.fail = {"code": code, "message": RECOVERY[code]}
    if detail:
        job.say(f"FAILED: {code}: {detail}")


async def _load(job: Job, plan: _Plan) -> str:
    if not plan.need_load:
        job.phase("load", "skip")
        job.say(f"MicroPython is already on {plan.tty}.")
        return cast(str, plan.tty)
    job.phase("load", "run")
    job.say(f"Loading MicroPython {MICROPYTHON_VERSION}.")
    final = await _drive(
        job,
        plan.base,
        "/pico/load",
        {
            "port": plan.tty,
            "uf2_b64": base64.b64encode(plan.uf2).decode(),
            "sha256": hashlib.sha256(plan.uf2).hexdigest(),
        },
    )
    port = final[len("OK port=") :] if final.startswith("OK port=") else ""
    if not port:
        raise _Failed("usb_gone", "MicroPython loaded but no port came back")
    job.port = port
    job.phase("load", "done")
    return port


async def _mint(job: Job, plan: _Plan, repo: GrowbotRepo, ctx: SessionContext, code: str) -> str:
    """The key, minted only now that the board has said which robot it is."""
    if plan.target != "new" and code != plan.target:
        raise _Failed("wrong_robot", f"the board is {code}, not {plan.target}")
    known = plan.existing.get(code)
    if plan.target == "new" and known is not None:
        job.say(f"{code} is already paired as {known.name or code}; this reflashes it.")
    key = await repo.put_robot(ctx, code, plan.name)
    job.secrets.append(key)
    return key


def _copied(job: Job) -> Callable[[str], None]:
    def on_line(line: str) -> None:
        if line in ("copied", "key written"):
            job.phase("copy", "done")
            job.phase("join", "run")

    return on_line


async def _flash(job: Job, plan: _Plan, repo: GrowbotRepo, ctx: SessionContext) -> None:
    port = await _load(job, plan)
    job.phase("copy", "run")
    code = await _identify(job, plan.base, port)
    job.code = code
    key = await _mint(job, plan, repo, ctx, code)
    try:
        secrets = provision.render_secrets(
            plan.box_host, growbot.GROWBOT_PORT, plan.sni, key, plan.ca, plan.networks
        )
    except provision.ProvisionError as exc:
        raise _Failed("internal", str(exc)) from None
    payload = {
        "port": job.port or port,
        "files": [{"name": n, "b64": base64.b64encode(d).decode()} for n, d in plan.files],
        "secrets_b64": base64.b64encode(secrets.encode()).decode(),
        "watch_s": WATCH_S,
    }
    await _drive(job, plan.base, "/pico/provision", payload, _copied(job))
    await _join(job, repo, ctx, code)


async def _rekey(job: Job, plan: _Plan, repo: GrowbotRepo, ctx: SessionContext) -> None:
    job.phase("load", "skip")
    job.phase("copy", "run")
    port = cast(str, plan.tty)
    code = await _identify(job, plan.base, port)
    job.code = code
    key = await _mint(job, plan, repo, ctx, code)
    await _drive(
        job, plan.base, "/pico/rekey", {"port": job.port or port, "key": key}, _copied(job)
    )
    await _join(job, repo, ctx, code)


async def _join(job: Job, repo: GrowbotRepo, ctx: SessionContext, code: str) -> None:
    """Joined Wi-Fi is not paired: the relay stamps last_seen_at once the robot's hello with
    its new key verifies, and that is the only proof it reached the box."""
    job.phase("copy", "done")
    job.phase("join", "run")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + JOIN_WAIT_S
    while True:
        robots = {r.code: r for r in await repo.list_robots(ctx)}
        seen = robots[code].last_seen_at if code in robots else None
        if seen is not None and seen >= job.started_at:
            job.phase("join", "done")
            return
        if loop.time() >= deadline:
            raise _Failed("box_unreached", "the robot never said hello to the relay")
        await asyncio.sleep(JOIN_POLL_S)


# -- revoke and status -----------------------------------------------------------------------


async def _revoke(request: Request, ctx: SessionContext, code: str) -> growbot.RobotOut:
    info = await growbot._repo(request).revoke_robot(ctx, code)
    if info is None:
        raise HTTPException(status_code=404, detail="There is no robot with that code on this box.")
    await growbot.drop_robot(request, code)
    return growbot.robot_out(info, set())


async def _status(request: Request, ctx: SessionContext) -> StatusOut:
    robots = await growbot._repo(request).list_robots(ctx)
    connected = await growbot.relay_robots(request)
    rows = [growbot.robot_out(r, connected or set()) for r in robots]
    return StatusOut(
        service="down" if connected is None else "up",
        connected=sum(1 for r in rows if r.connected),
        robots=rows,
    )


# -- routes ----------------------------------------------------------------------------------


def _code(code: str) -> str:
    if not CODE_RE.fullmatch(code):
        raise HTTPException(status_code=422, detail="That is not a pairing code.")
    return code


@router.get("/status")
async def growbot_status(request: Request, owner: OwnerDep) -> StatusOut:
    """The GrowBot card: whether the relay is up, and each robot with whether it is on it."""
    return await _status(request, growbot._owner_ctx(owner))


@router.get("/flash/setup")
async def flash_setup(request: Request, owner: OwnerDep, settings: SettingsDep) -> SetupOut:
    """What the Add sheet needs before a flash: what is on USB, the remembered networks (never
    their passwords), the box's address, and whether the box has what a flash needs."""
    return await _setup(request, settings, growbot._owner_ctx(owner))


@router.post("/flash", status_code=202)
async def start_flash(
    request: Request, owner: OwnerDep, settings: SettingsDep, body: FlashIn
) -> JobStartOut:
    """Flash or rekey the robot on the box's USB; poll GET /growbot/flash for its progress."""
    return await _start(request, settings, growbot._owner_ctx(owner), body)


@router.get("/flash")
async def get_flash(request: Request, _owner: OwnerDep) -> JobGetOut:
    job = _state(request).job
    return JobGetOut(job=job.out() if job else None)


@router.post("/robots/{code}/revoke")
async def revoke_robot(request: Request, owner: OwnerDep, code: str) -> growbot.RobotOut:
    """Revoke a robot's key (it stays listed, and a reflash brings it back) and drop it off
    the relay now."""
    return await _revoke(request, growbot._owner_ctx(owner), _code(code))


@debug_router.post("/flash", status_code=202)
async def debug_start_flash(
    request: Request, _p: DebugDep, settings: SettingsDep, body: FlashIn
) -> JobStartOut:
    request.state.debug_detail = f"growbot flash {body.mode} {body.target}"
    return await _start(request, settings, _DEBUG_OWNER, body)


@debug_router.get("/flash")
async def debug_get_flash(request: Request, _p: DebugDep) -> JobGetOut:
    request.state.debug_detail = "growbot flash job"
    job = _state(request).job
    return JobGetOut(job=job.out() if job else None)


@debug_router.post("/robots/{code}/revoke")
async def debug_revoke_robot(request: Request, _p: DebugDep, code: str) -> growbot.RobotOut:
    request.state.debug_detail = f"growbot revoke {code}"
    return await _revoke(request, _DEBUG_OWNER, _code(code))
