"""`/api/minecraft/*`: the owner's Minecraft screen.

The binding mock is `docs/mocks/minecraft-ops/b-dedicated-screen.html`, and the routes
are the contract in docs/plans/MINECRAFT_BEDROCK_PLAN.md §M1. Lifecycle goes through
the supervisor and the rest through the host-networked sidecar, via the client shared
with the debug routes. Play history comes from `app.mc_player_sessions`, which
`minecraft/sessions.py` fills.

The API refuses nothing on the owner's behalf. The confirms ("2 players will be
disconnected", "this restart also installs 1.26.60.4") are the screen's job, because
only the screen knows what the owner has already been told.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import SettingsDep, owner_only
from jbrain.minecraft import changelog, sessions
from jbrain.minecraft import client as mc

router = APIRouter(prefix="/minecraft", dependencies=[Depends(owner_only)])


def _changelog(request: Request) -> changelog.Changelog:
    return cast(changelog.Changelog, request.app.state.minecraft_changelog)


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


def _server_view(raw: dict[str, Any]) -> dict[str, Any]:
    props = raw.get("properties") or {}
    return {
        "state": raw.get("state"),
        "install_error": raw.get("install_error"),
        "version": raw.get("version"),
        "level_name": raw.get("level_name"),
        "server_name": props.get("server-name"),
        "gamemode": props.get("gamemode"),
        "difficulty": props.get("difficulty"),
        "allow_list": str(props.get("allow-list", "false")).lower() == "true",
        "lan_ip": raw.get("lan_ip"),
        "port": int(props.get("server-port", 19132)),
        "uptime_s": raw.get("uptime_s"),
        "players": raw.get("players") or [],
        "update": raw.get("update") or {"state": "idle"},
        "auto_update": bool(raw.get("auto_update", False)),
    }


@router.get("")
async def minecraft(request: Request, settings: SettingsDep) -> dict[str, Any]:
    server: dict[str, Any] | None = None
    server_error: str | None = None
    try:
        container = await mc.container(request, settings)
    except HTTPException as exc:
        return {"container": None, "server": None, "server_error": str(exc.detail)}
    if container and container.get("state") == "running":
        try:
            server = _server_view(await mc.call(settings, "GET", "/status"))
        except HTTPException as exc:
            server_error = str(exc.detail)
    return {"container": container, "server": server, "server_error": server_error}


@router.get("/version")
async def version(request: Request, settings: SettingsDep, refresh: bool = False) -> dict[str, Any]:
    info = await mc.call(settings, "GET", "/version", params={"refresh": int(refresh)})
    # The notes describe the version the card is talking about: the one on offer when
    # an update is waiting, otherwise the one running.
    about = info.get("latest") if info.get("update_available") else info.get("running")
    notes = await _changelog(request).notes_for(about)
    return {
        **info,
        "notes": notes.as_dict() if notes else None,
        "notes_fallback_url": changelog.FALLBACK_URL,
    }


# How long a freshly started container gets to bring its control surface up before the
# server start inside it is given up on (the screen then shows whatever state it's in).
CONTAINER_UP_S = 30.0


async def _server_action(request: Request, settings: Any, action: str) -> dict[str, str]:
    """Start/stop/restart the GAME server, keeping the container (and so the status,
    the server facts and the update path) up. If the container itself is down, a start
    or restart brings it up first; a stop of a stopped container is already done."""
    container = await mc.container(request, settings)
    if not container or container.get("state") != "running":
        if action == "stop":
            return {"action": action}
        await mc.lifecycle(request, settings, "start")
        await _wait_for_wrapper(settings)
        action = "start"
    await mc.call(settings, "POST", f"/server/{action}", timeout_s=90.0)
    return {"action": action}


async def _wait_for_wrapper(settings: Any) -> None:
    deadline = asyncio.get_running_loop().time() + CONTAINER_UP_S
    while asyncio.get_running_loop().time() < deadline:
        try:
            await mc.call(settings, "GET", "/status")
            return
        except HTTPException:
            await asyncio.sleep(1.0)


@router.post("/start", status_code=202)
async def start(request: Request, settings: SettingsDep) -> dict[str, str]:
    return await _server_action(request, settings, "start")


@router.post("/stop", status_code=202)
async def stop(request: Request, settings: SettingsDep) -> dict[str, str]:
    return await _server_action(request, settings, "stop")


@router.post("/restart", status_code=202)
async def restart(request: Request, settings: SettingsDep) -> dict[str, str]:
    return await _server_action(request, settings, "restart")


@router.post("/retry-install", status_code=202)
async def retry_install(request: Request, settings: SettingsDep) -> dict[str, str]:
    """After a failed first install there is no server to restart; restarting the
    container re-runs the wrapper's install at once instead of on its 5-minute retry."""
    await mc.lifecycle(request, settings, "stop")
    await mc.lifecycle(request, settings, "start")
    return {"action": "retry-install"}


@router.post("/update", status_code=202)
async def update(settings: SettingsDep) -> dict[str, Any]:
    return await mc.call(settings, "POST", "/update", timeout_s=mc.SNAPSHOT_TIMEOUT_S)


class SettingsIn(BaseModel):
    auto_update: bool


@router.put("/settings")
async def put_settings(body: SettingsIn, settings: SettingsDep) -> dict[str, Any]:
    return await mc.call(settings, "POST", "/settings", json=body.model_dump())


@router.get("/players")
async def players(request: Request, settings: SettingsDep) -> dict[str, Any]:
    """History from the table, "who's on right now" from the live server. A stopped
    server just means nobody is online; the history still answers."""
    online: dict[str, float] = {}
    try:
        status = await mc.call(settings, "GET", "/status")
        for p in status.get("players") or []:
            online[sessions.player_key(str(p.get("xuid", "")), str(p.get("name", "")))] = float(
                p.get("joined_at") or 0
            )
    except HTTPException:
        pass
    return {
        "players": await sessions.players(_maker(request), online),
        # Lifetime stats (deaths, mobs, blocks, distance) arrive with the M5 add-on.
        "stats_available": False,
    }
