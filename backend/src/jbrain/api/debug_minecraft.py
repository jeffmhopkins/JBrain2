"""`/api/debug/minecraft/*` — the assistant's handle on the Minecraft server.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §3a. The owner has no terminal, so setting up and
debugging the Bedrock server runs through a debug token: lifecycle through the
supervisor (the only holder of the docker socket), everything else through the
sidecar's own control surface (`deploy/minecraft/server.py`).

"Full control" here means the GAME server, never the box: there is no route to a shell,
`docker exec`, or a file path. The console refuses `stop` and `save …`, which have
routes of their own that do them safely, and a world copy never leaves the box over a
token — snapshots are listed, not downloaded, the same rule as `/debug/backup`.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, SettingsDep

router = APIRouter(prefix="/debug/minecraft")

SERVICE = "minecraft"
# A snapshot copies the whole world under `save hold`; a small world takes seconds, but
# the first one after a long session can be slow, and a timeout here would only hide
# whether the hold was released.
SNAPSHOT_TIMEOUT_S = 180.0
SIDECAR_TIMEOUT_S = 30.0
# Tests swap in an httpx.MockTransport; None is the real network.
_transport: httpx.AsyncBaseTransport | None = None


def _supervisor(request: Request) -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, request.app.state.supervisor_client)


def _base(settings: Any) -> str:
    base = str(settings.minecraft_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="No Minecraft server on this box.")
    return base


async def _sidecar(
    settings: Any,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: float = SIDECAR_TIMEOUT_S,
) -> dict[str, Any]:
    """One call to the wrapper, with its refusals passed through as what they are.

    An unreachable sidecar is a 503 naming the likely cause, because "connection
    refused" on a stopped container reads like a bug when it is a state."""
    token = str(settings.minecraft_token or "")
    if not token:
        # Sending "Bearer " with nothing after it is a malformed header that httpx
        # rejects before any request — the cryptic LocalProtocolError the first deploy
        # hit. Say what is actually missing.
        raise HTTPException(
            status_code=503,
            detail="MINECRAFT_TOKEN is not set for the api — run an update to mint it",
        )
    try:
        async with httpx.AsyncClient(
            base_url=_base(settings),
            timeout=timeout_s,
            transport=_transport,
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            resp = await client.request(method, path, json=json, params=params)
    except httpx.TransportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Minecraft sidecar unreachable ({type(exc).__name__}) — is it stopped?",
        ) from exc
    if resp.status_code in (401, 503):
        # The sidecar's own auth refusal — a token mismatch is a deploy fault, not a state.
        raise HTTPException(
            status_code=502, detail=f"Minecraft sidecar refused the api: {_detail(resp)}"
        )
    if resp.status_code in (400, 404, 409):
        raise HTTPException(status_code=resp.status_code, detail=_detail(resp))
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json())


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text))
    except ValueError:
        return resp.text


async def _container(request: Request, settings: Any) -> dict[str, Any] | None:
    resp = await _supervisor(request).get(
        "/status", headers={"Authorization": f"Bearer {settings.supervisor_token}"}
    )
    resp.raise_for_status()
    for c in resp.json().get("containers", []):
        if c.get("service") == SERVICE:
            return cast(dict[str, Any], c)
    return None


async def _lifecycle(request: Request, settings: Any, action: str) -> None:
    resp = await _supervisor(request).post(
        f"/{action}",
        json={"service": SERVICE},
        headers={"Authorization": f"Bearer {settings.supervisor_token}"},
    )
    if resp.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="the minecraft container does not exist yet — run /debug/update",
        )
    resp.raise_for_status()


@router.get("")
async def minecraft_status(request: Request, settings: SettingsDep, _p: DebugDep) -> dict[str, Any]:
    """Everything worth knowing in one call: the container as docker sees it, and the
    game server as the wrapper sees it (state, version, players, effective
    properties). The two disagree in useful ways — a running container whose server
    is still downloading, or a stopped one — so both are returned, never merged."""
    request.state.debug_detail = "minecraft status"
    container = await _container(request, settings)
    server: dict[str, Any] | None = None
    server_error: str | None = None
    if container and container.get("state") == "running":
        try:
            server = await _sidecar(settings, "GET", "/status")
        except HTTPException as exc:
            server_error = str(exc.detail)
    return {"container": container, "server": server, "server_error": server_error}


@router.post("/start", status_code=202)
async def minecraft_start(request: Request, settings: SettingsDep, _p: DebugDep) -> dict[str, str]:
    request.state.debug_detail = "minecraft start"
    await _lifecycle(request, settings, "start")
    return {"service": SERVICE, "action": "start"}


@router.post("/stop", status_code=202)
async def minecraft_stop(request: Request, settings: SettingsDep, _p: DebugDep) -> dict[str, str]:
    """Stop the container. Docker's stop honours the service's `stop_grace_period`, and
    the wrapper turns the SIGTERM into a console `stop` that saves the world first."""
    request.state.debug_detail = "minecraft stop"
    await _lifecycle(request, settings, "stop")
    return {"service": SERVICE, "action": "stop"}


@router.post("/restart", status_code=202)
async def minecraft_restart(
    request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, str]:
    """Stop, then start — NOT the supervisor's `/restart`, whose docker restart passes a
    fixed 10 s timeout and can kill the server mid-save; a stop honours the 60 s grace
    period the compose service sets. Property edits (`PUT /properties`) apply here."""
    request.state.debug_detail = "minecraft restart"
    await _lifecycle(request, settings, "stop")
    await _lifecycle(request, settings, "start")
    return {"service": SERVICE, "action": "restart"}


@router.get("/logs")
async def minecraft_logs(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    tail: Annotated[int, Query(ge=1, le=5000)] = 200,
) -> dict[str, Any]:
    """The game server's console as the wrapper recorded it (with sequence numbers, so a
    poll can ask "what is new since"). For a container that will not start, read
    `/debug/logs/minecraft` instead — that is docker's log, which outlives the wrapper."""
    request.state.debug_detail = f"minecraft logs (tail {tail})"
    return await _sidecar(settings, "GET", "/logs", params={"tail": tail})


class ConsoleIn(BaseModel):
    command: str = Field(min_length=1, max_length=2000)
    # How long to collect the reply; a `locate` can take several seconds.
    wait_s: float = Field(default=3.0, ge=0.5, le=20.0)


@router.post("/console")
async def minecraft_console(
    body: ConsoleIn, request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, Any]:
    """Run one console command and return what the server printed in reply.

    Game-scoped: anything the BDS console accepts, except `stop` and `save …`, which
    the wrapper refuses because each has a safer route (`/stop`, `/snapshot`)."""
    request.state.debug_detail = f"minecraft console: {body.command[:120]}"
    return await _sidecar(
        settings,
        "POST",
        "/command",
        json={"command": body.command, "wait_s": body.wait_s},
        timeout_s=body.wait_s + SIDECAR_TIMEOUT_S,
    )


class SnapshotIn(BaseModel):
    label: str = Field(default="", max_length=40)


@router.post("/snapshot")
async def minecraft_snapshot(
    body: SnapshotIn, request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, Any]:
    """A hot backup of the running world (`save hold` → `save query` → copy → `save
    resume`), kept on the box as a `.mcworld`. Returns its name and size, never its
    bytes."""
    request.state.debug_detail = f"minecraft snapshot {body.label}".strip()
    return await _sidecar(
        settings, "POST", "/snapshot", json={"label": body.label}, timeout_s=SNAPSHOT_TIMEOUT_S
    )


@router.get("/snapshots")
async def minecraft_snapshots(
    request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, Any]:
    request.state.debug_detail = "minecraft snapshots"
    return await _sidecar(settings, "GET", "/snapshots")


@router.get("/properties")
async def minecraft_properties(
    request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, Any]:
    request.state.debug_detail = "minecraft properties"
    return await _sidecar(settings, "GET", "/properties")


class PropertiesIn(BaseModel):
    # server.properties keys -> value; null removes an override. Applies on restart.
    set: dict[str, str | None]


@router.put("/properties")
async def minecraft_set_properties(
    body: PropertiesIn, request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, Any]:
    """Override server.properties keys on top of the compose defaults (for example
    `transport` to try `nethernet`, or `allow-list`). Ports are pinned and refused.
    Takes effect at the next `/restart`."""
    request.state.debug_detail = f"minecraft properties {sorted(body.set)}"
    return await _sidecar(settings, "POST", "/properties", json={"set": body.set})


@router.get("/version")
async def minecraft_version(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    refresh: bool = False,
) -> dict[str, Any]:
    """Running BDS version against Mojang's latest (cached for hours by the wrapper;
    `refresh=true` asks Mojang now)."""
    request.state.debug_detail = f"minecraft version (refresh={refresh})"
    return await _sidecar(settings, "GET", "/version", params={"refresh": int(refresh)})


@router.post("/update", status_code=202)
async def minecraft_update(request: Request, settings: SettingsDep, _p: DebugDep) -> dict[str, Any]:
    """Back up, install the latest BDS, restart — the card's Update server. Runs in the
    background; poll `GET /minecraft` (its `server.update`). A failed backup installs
    nothing. 409 while one is already running."""
    request.state.debug_detail = "minecraft update server"
    return await _sidecar(settings, "POST", "/update")


class ProbePackIn(BaseModel):
    install: bool = True


@router.post("/probe-pack")
async def minecraft_probe_pack(
    body: ProbePackIn, request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, Any]:
    """Install (or remove) the bundled M0 probe behavior pack in the active world and
    restart the server so it loads. Its findings are `[jbrain-probe]` lines in
    `GET /minecraft/logs`. The pack is shipped in the image; nothing is uploaded."""
    request.state.debug_detail = f"minecraft probe pack install={body.install}"
    return await _sidecar(
        settings, "POST", "/probe-pack", json={"install": body.install}, timeout_s=120
    )
