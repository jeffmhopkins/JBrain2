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
import re
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask

from jbrain.api.deps import OwnerDep, SettingsDep, owner_only
from jbrain.db.session import SessionContext
from jbrain.minecraft import (
    changelog,
    goals,
    livescan,
    markers,
    places,
    sessions,
    shares,
    spawnpoints,
    targets,
    timeline,
    travel,
    visited,
)
from jbrain.minecraft import client as mc

router = APIRouter(prefix="/minecraft", dependencies=[Depends(owner_only)])


def _changelog(request: Request) -> changelog.Changelog:
    return cast(changelog.Changelog, request.app.state.minecraft_changelog)


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


# `server-udp-ports` is `[public-ip:]external:internal`; the address, when there is one,
# is what joining players are told to send their game traffic to.
_ANNOUNCED = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):")


def _announced_ip(udp_ports: object) -> str | None:
    m = _ANNOUNCED.match(str(udp_ports or ""))
    return m.group(1) if m else None


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
        # What the internet sees the box at, and what the server tells players it is: the
        # PWA flags them differing, since internet play breaks until they match again.
        "public_ip": raw.get("public_ip"),
        "announced_ip": _announced_ip(props.get("server-udp-ports")),
        "port": int(props.get("server-port", 19132)),
        "uptime_s": raw.get("uptime_s"),
        "players": raw.get("players") or [],
        "update": raw.get("update") or {"state": "idle"},
        "auto_update": bool(raw.get("auto_update", False)),
        # Whether a world the box makes starts at the crash site (plan §SW3); on unless off.
        "spawn_wreck": bool(raw.get("spawn_wreck", True)),
        # A world operation in flight (what, phase, started_at), so every device sees it.
        "job": raw.get("job"),
        # server.properties keys saved since the last start ("gamemode", "allow-list", …):
        # what a restart will change, the same on every device.
        "pending_restart": raw.get("pending_restart") or [],
        # How the last world operation ended: a load, reset, restore or import that ran
        # past the sidecar's wait answers {"accepted": true}, and this is its outcome.
        "last_job": raw.get("last_job"),
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
    auto_update: bool | None = None
    spawn_wreck: bool | None = None


@router.put("/settings")
async def put_settings(body: SettingsIn, settings: SettingsDep) -> dict[str, Any]:
    return await mc.call(settings, "POST", "/settings", json=body.model_dump(exclude_none=True))


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


# --- Goals and their progress (§M24; GUI gate docs/mocks/minecraft-goals/, B, look-only).
# Read-only: players change goals by asking Dave in game, never from here.


@router.get("/goals")
async def player_goals(request: Request, settings: SettingsDep) -> dict[str, Any]:
    """Every player's goals and latest journal, from the database, each goal with the
    world it's in (a goal from before goals recorded one is the loaded world's) and
    whether that's the loaded world. The sidecar only names the worlds: down, the goals
    still answer, with `loaded` unknown (None)."""
    try:
        view: dict[str, Any] | str = await mc.call(settings, "GET", "/worlds")
    except HTTPException as exc:
        view = str(exc.detail)
    active, by_folder = goals.world_names(view)
    players = await goals.read_goals(_maker(request))
    for p in players:
        for g in p["goals"]:
            folder = g["world"] or active
            g["world"] = folder
            g["world_name"] = (by_folder.get(folder) or {}).get("name") if folder else None
            g["loaded"] = None if active is None else folder == active
    return {"world": active, "players": players}


@router.get("/goals/progress")
async def goal_progress(
    request: Request,
    settings: SettingsDep,
    xuid: Annotated[str, Query(min_length=1, max_length=64)],
    flush: bool = False,
) -> dict[str, Any]:
    """Count one player's open counted goals now, all at once (one carry, one stock count
    a dimension): the app path of `mc_goal_progress`, which never spends a deep scan.
    `flush` is **Count again**: the running world saves first (at most every 30 s)."""

    async def sidecar(method: str, path: str, **kw: Any) -> dict[str, Any] | str:
        try:
            return await mc.call(settings, method, path, **kw)
        except HTTPException as exc:
            return str(exc.detail)

    try:
        return await goals.count_progress(_maker(request), sidecar, xuid, flush=flush)
    except goals.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# --- Worlds and backups (MINECRAFT_BEDROCK_PLAN §M2/§M3) -----------------------------
# Every world operation is the sidecar's, under its lifecycle lock: a load, import,
# reset or restore backs up what it would replace first, and refuses (409) while an
# update or another operation owns the server.

_SLOT = r"^slot\d+$"
_BACKUP = r"^[A-Za-z0-9_.-]+\.mcworld$"


@router.get("/worlds")
async def worlds(settings: SettingsDep) -> dict[str, Any]:
    return await mc.call(settings, "GET", "/worlds")


# The sidecar answers a world operation within ~20 s, as 202 if it is still running, so
# no request nears Cloudflare's ~100 s cut-off; this only bounds a wedged sidecar.
WORLD_JOB_TIMEOUT_S = 60.0


class WorldIn(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    seed: str | None = Field(default=None, max_length=64)
    rules: dict[str, bool | int | str] | None = None
    gamemode: Literal["survival", "creative", "adventure"] | None = None
    difficulty: Literal["peaceful", "easy", "normal", "hard"] | None = None
    cheats: bool | None = None
    # The crash site at spawn (§SW3), for a create: unset follows the server's default.
    wreck: bool | None = None


@router.post("/worlds/{slot}/load")
async def load_world(slot: Annotated[str, Path(pattern=_SLOT)], settings: SettingsDep):
    return await mc.call(settings, "POST", f"/worlds/{slot}/load", timeout_s=WORLD_JOB_TIMEOUT_S)


class CreateIn(WorldIn):
    # The Power Pack teleport switch (§M10) is a create-time choice and its own route after,
    # so a PATCH never carries it. Unset leaves the sidecar's default, on.
    power_pack_transport: bool | None = None


@router.post("/worlds/{slot}/create")
async def create_world(
    slot: Annotated[str, Path(pattern=_SLOT)], body: CreateIn, settings: SettingsDep
) -> dict[str, Any]:
    return await mc.call(
        settings, "POST", f"/worlds/{slot}/create", json=body.model_dump(exclude_none=True)
    )


@router.patch("/worlds/{slot}")
async def update_world(
    slot: Annotated[str, Path(pattern=_SLOT)], body: WorldIn, settings: SettingsDep
) -> dict[str, Any]:
    return await mc.call(
        settings, "POST", f"/worlds/{slot}/update", json=body.model_dump(exclude_none=True)
    )


class ResetIn(BaseModel):
    mode: Literal["same_seed", "new_seed", "empty"]
    seed: str | None = Field(default=None, max_length=64)
    # A regenerated world's crash site (§SW3); unset follows the server's default.
    wreck: bool | None = None


@router.post("/worlds/{slot}/reset")
async def reset_world(
    slot: Annotated[str, Path(pattern=_SLOT)], body: ResetIn, settings: SettingsDep
) -> dict[str, Any]:
    return await mc.call(
        settings,
        "POST",
        f"/worlds/{slot}/reset",
        # An unset crash site is left out, so the box applies its own default.
        json=body.model_dump(exclude={"wreck"} if body.wreck is None else None),
        timeout_s=WORLD_JOB_TIMEOUT_S,
    )


@router.post("/worlds/{slot}/import")
async def import_world(
    slot: Annotated[str, Path(pattern=_SLOT)],
    request: Request,
    settings: SettingsDep,
    name: str = "",
) -> dict[str, Any]:
    """The PWA uploads the .mcworld as the raw request body; it streams straight on."""
    length = int(request.headers.get("content-length") or 0)
    if length <= 0:
        raise HTTPException(status_code=411, detail="upload needs a Content-Length")
    return await mc.upload(
        settings, f"/worlds/{slot}/import", request.stream(), length, {"name": name}
    )


@router.get("/backups")
async def backups(settings: SettingsDep, slot: str = "") -> dict[str, Any]:
    return await mc.call(settings, "GET", "/snapshots", params={"slot": slot} if slot else None)


class BackupIn(BaseModel):
    label: str = Field(default="", max_length=40)
    slot: str | None = Field(default=None, pattern=_SLOT)


@router.post("/backups")
async def back_up_now(body: BackupIn, settings: SettingsDep) -> dict[str, Any]:
    return await mc.call(
        settings, "POST", "/snapshot", json=body.model_dump(), timeout_s=mc.SNAPSHOT_TIMEOUT_S
    )


class PinIn(BaseModel):
    pinned: bool


@router.post("/backups/{name}/pin")
async def pin_backup(
    name: Annotated[str, Path(pattern=_BACKUP)], body: PinIn, settings: SettingsDep
) -> dict[str, Any]:
    return await mc.call(settings, "POST", f"/snapshots/{name}/pin", json=body.model_dump())


@router.delete("/backups/{name}")
async def delete_backup(
    name: Annotated[str, Path(pattern=_BACKUP)], settings: SettingsDep
) -> dict[str, Any]:
    return await mc.call(settings, "POST", f"/snapshots/{name}/delete")


class RestoreIn(BaseModel):
    slot: str = Field(pattern=_SLOT)


@router.post("/backups/{name}/restore")
async def restore_backup(
    name: Annotated[str, Path(pattern=_BACKUP)], body: RestoreIn, settings: SettingsDep
) -> dict[str, Any]:
    return await mc.call(
        settings,
        "POST",
        f"/snapshots/{name}/restore",
        json=body.model_dump(),
        timeout_s=WORLD_JOB_TIMEOUT_S,
    )


@router.get("/backups/{name}/file")
async def download_backup(
    name: Annotated[str, Path(pattern=_BACKUP)], settings: SettingsDep
) -> StreamingResponse:
    """The owner's download — the only copy of a Minecraft backup that leaves the box
    (they're kept apart from the box backups), so the sidecar records it per world."""
    client, resp = await mc.download(settings, f"/snapshots/{name}/file")

    async def _close() -> None:
        await resp.aclose()
        await client.aclose()

    return StreamingResponse(
        resp.aiter_bytes(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": resp.headers.get("content-length", ""),
        },
        background=BackgroundTask(_close),
    )


@router.get("/allowlist")
async def allowlist(settings: SettingsDep) -> dict[str, Any]:
    return await mc.call(settings, "GET", "/allowlist")


class AllowlistIn(BaseModel):
    add: str | None = Field(default=None, max_length=32)
    remove: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None


@router.post("/allowlist")
async def change_allowlist(body: AllowlistIn, settings: SettingsDep) -> dict[str, Any]:
    return await mc.call(settings, "POST", "/allowlist", json=body.model_dump(exclude_none=True))


# Server-wide settings: a fixed set, not the debug surface's free-form overrides.
SERVER_KEYS = {
    "server_name": "server-name",
    "max_players": "max-players",
    "view_distance": "view-distance",
}


class ServerSettingsIn(BaseModel):
    server_name: str | None = Field(default=None, min_length=1, max_length=40)
    max_players: int | None = Field(default=None, ge=1, le=30)
    view_distance: int | None = Field(default=None, ge=5, le=32)


@router.get("/server-settings")
async def server_settings(settings: SettingsDep) -> dict[str, Any]:
    eff = (await mc.call(settings, "GET", "/properties"))["effective"]
    out: dict[str, Any] = {k: eff.get(v) for k, v in SERVER_KEYS.items()}
    for key in ("max_players", "view_distance"):  # server.properties holds text
        try:
            out[key] = int(out[key]) if out[key] is not None else None
        except ValueError:
            out[key] = None
    return out


@router.put("/server-settings")
async def put_server_settings(body: ServerSettingsIn, settings: SettingsDep) -> dict[str, Any]:
    changes = {SERVER_KEYS[k]: str(v) for k, v in body.model_dump(exclude_none=True).items()}
    await mc.call(settings, "POST", "/properties", json={"set": changes})
    return {**(await server_settings(settings)), "applies": "next restart"}


@router.get("/worlds/new-seed")
async def new_seed(settings: SettingsDep) -> dict[str, Any]:
    """A random seed for the create-world form; re-rollable, shown before creating."""
    return await mc.call(settings, "GET", "/worlds/new-seed")


@router.get("/worlds/{slot}/rules")
async def world_rules(
    slot: Annotated[str, Path(pattern=_SLOT)], settings: SettingsDep
) -> dict[str, Any]:
    return await mc.call(settings, "GET", f"/worlds/{slot}/rules")


class RulesIn(BaseModel):
    # rule id -> true/false, a number, or a one-word choice; validated by the sidecar
    # against the server's own rule list.
    set: dict[str, bool | int | str]


@router.put("/worlds/{slot}/rules")
async def set_world_rules(
    slot: Annotated[str, Path(pattern=_SLOT)], body: RulesIn, settings: SettingsDep
) -> dict[str, Any]:
    return await mc.call(settings, "POST", f"/worlds/{slot}/rules", json=body.model_dump())


class PowerPackIn(BaseModel):
    on: bool


@router.put("/worlds/{slot}/power-pack")
async def set_power_pack(
    slot: Annotated[str, Path(pattern=_SLOT)], body: PowerPackIn, settings: SettingsDep
) -> dict[str, Any]:
    """The world's "Power Pack keeps inventory" switch (plan §M10), off by default. On,
    the world's keepInventory is held on (the rules view marks it managed and refuses a
    change to it) and the jbrain pack drops for anyone who dies without both a Power Pack
    and their own Tricorder. Applies at once to the loaded world, else on its next load."""
    return await mc.call(settings, "POST", f"/worlds/{slot}/power-pack", json=body.model_dump())


@router.put("/worlds/{slot}/power-pack-transport")
async def set_power_pack_transport(
    slot: Annotated[str, Path(pattern=_SLOT)], body: PowerPackIn, settings: SettingsDep
) -> dict[str, Any]:
    """The world's "Power Pack teleport" switch (plan §M10, transport on request), on by
    default. Off, the jbrain pack refuses every trip Dave offers in that world. Applies at
    once to the loaded world, else on its next load."""
    return await mc.call(
        settings, "POST", f"/worlds/{slot}/power-pack-transport", json=body.model_dump()
    )


# --- the map (MINECRAFT_BEDROCK_PLAN.md §M8) ----------------------------------------
_DIM = r"^(overworld|nether|the_end)$"
# The map's base layers: explored ground in its real block colours, or the biome atlas.
_LAYER = r"^(surface|biomes)$"
# A block coordinate inside the world border.
_COORD = 30_000_000


@router.get("/map/info")
async def map_info(
    settings: SettingsDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=_DIM)] = "overworld",
) -> dict[str, Any]:
    """What a viewer needs before asking for tiles: the generated extent and the tile
    geometry."""
    return await mc.call(settings, "GET", "/map/info", params={"slot": slot, "dim": dim})


@router.get("/map/tile/{dim}/{zoom}/{tx}/{tz}.png")
async def map_tile(
    dim: Annotated[str, Path(pattern=_DIM)],
    zoom: Annotated[int, Path(ge=0, le=4)],
    tx: int,
    tz: int,
    request: Request,
    settings: SettingsDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    layer: Annotated[str, Query(pattern=_LAYER)] = "surface",
) -> Response:
    """One 256×256 map tile of a base layer, rendered on the box from the world's own
    files and the ground the travel log saw players cross. Cached briefly: a tile changes
    only as the world is explored."""
    seen = await visited.for_slot(_maker(request), settings, slot, dim, zoom, tx, tz)
    png = await mc.image(
        settings,
        f"/map/tile/{dim}/{zoom}/{tx}/{tz}.png",
        params={"slot": slot, "layer": layer, **seen},
    )
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})


@router.get("/map/point")
async def map_point(
    settings: SettingsDep,
    x: Annotated[int, Query(ge=-_COORD, le=_COORD)],
    z: Annotated[int, Query(ge=-_COORD, le=_COORD)],
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=_DIM)] = "overworld",
) -> dict[str, Any]:
    """What a tapped spot is: its biome and surface y, and whether that is explored ground
    or the satellite's survey (the seed's prediction, y an estimate)."""
    params = {"slot": slot, "dim": dim, "x": x, "z": z}
    return await mc.call(settings, "GET", "/map/point", params=params)


@router.get("/map/players")
async def map_players(
    request: Request,
    settings: SettingsDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
) -> dict[str, Any]:
    """Each player's latest position in one world, from the travel log, and whether they
    are on it right now. Only the loaded, running world has anyone on; any other world
    shows where each player last was."""
    rec = await mc.slot_record(settings, slot)
    return await players_view(request, settings, slot, rec)


async def players_view(
    request: Request,
    settings: Any,
    slot: str,
    rec: dict[str, Any],
    ctx: SessionContext | None = None,
) -> dict[str, Any]:
    """`/map/players` for a slot record already in hand; a map share passes its own
    pinned `ctx` so the travel log is read under the share's RLS, not the owner's."""
    online: set[str] = set()
    running = False
    if rec.get("active"):
        try:
            status = await mc.call(settings, "GET", "/status")
        except HTTPException:
            status = {}
        running = status.get("state") == "running"
        if running:
            online = {
                sessions.player_key(str(p.get("xuid", "")), str(p.get("name", "")))
                for p in status.get("players") or []
            }
    folder = str(rec["folder"])
    if ctx is None:
        players = await travel.latest_positions(_maker(request), folder)
        locks = await targets.for_world(_maker(request), folder)
        scans = await livescan.for_world(_maker(request), folder)
    else:
        players = await travel.latest_positions(_maker(request), folder, ctx)
        locks = await targets.for_world(_maker(request), folder, ctx)
        scans = await livescan.for_world(_maker(request), folder, ctx)
    for p in players:
        p["online"] = p["xuid"] in online
        # Where Dave has their Tricorder pointed, if anywhere (plan §M21).
        p["target"] = locks.get(targets.key(str(p["gamertag"])))
        # And the live scan that points it now, kept while they're offline (§M25).
        got = scans.get(livescan.key(str(p["gamertag"])))
        p["scan"] = (
            None
            if got is None
            else {k: got[k] for k in ("label", "state", "found", "at", "set_at")}
        )
    return {"slot": slot, "world": rec["folder"], "live": running, "players": players}


@router.get("/map/structures")
async def map_structures(
    settings: SettingsDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=_DIM)] = "overworld",
) -> dict[str, Any]:
    """The world's saved structure sweep in one dimension (the server's own `locate`, so
    exact), with the sweep's state. Answers while the server is stopped; a stale sweep of
    the loaded, running world refreshes itself in the background."""
    params = {"slot": slot, "dim": dim}
    return await mc.call(settings, "GET", "/map/structures", params=params)


@router.get("/map/markers")
async def map_markers(
    request: Request,
    settings: SettingsDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str | None, Query(pattern=_DIM)] = None,
) -> dict[str, Any]:
    """The world's No-Spawn Beacons and wormhole gates (§M26), in one dimension or all:
    the pack's own records, kept by the session drain, so they answer with the server
    stopped or the world unloaded, as of `as_of` (when the pack last reported them). With
    them, every player's respawn point and its recent changes (§M8, beds), in every
    dimension: the owner's only, as this route is."""
    rec = await mc.slot_record(settings, slot)
    folder = str(rec["folder"])
    got = await markers.for_world(_maker(request), folder, dim)
    beds = await spawnpoints.for_world(_maker(request), folder)
    return {"slot": slot, "world": rec["folder"], **got, **beds}


@router.get("/map/places")
async def map_places(
    request: Request,
    settings: SettingsDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str | None, Query(pattern=_DIM)] = None,
) -> dict[str, Any]:
    """Every player's saved places in the slot's world (one dimension or all), each with
    the beacons that cover it and the gates aimed at its block (GUI gate
    `docs/mocks/minecraft-map-layers/`, places A). Owner-only: never on a share link."""
    rec = await mc.slot_record(settings, slot)
    got = await places.for_world(_maker(request), str(rec["folder"]), dim)
    return {"slot": slot, "world": rec["folder"], **got}


_ID = r"^(-?\d{1,20}|at:-?\d{1,2}:-?\d{1,9}:-?\d{1,4}:-?\d{1,9})$"


@router.get("/map/entities")
async def map_entities(
    settings: SettingsDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=_DIM)] = "overworld",
    x0: Annotated[int | None, Query(ge=-_COORD, le=_COORD)] = None,
    z0: Annotated[int | None, Query(ge=-_COORD, le=_COORD)] = None,
    x1: Annotated[int | None, Query(ge=-_COORD, le=_COORD)] = None,
    z1: Annotated[int | None, Query(ge=-_COORD, le=_COORD)] = None,
) -> dict[str, Any]:
    """The villagers (grouped into villages), pets and named mobs in one dimension, or in
    a box, from the world's saved files (§M4e's index, on the map). Answers with the
    server stopped; never forces a save. Owner-only like the beacons and gates."""
    corners = (x0, z0, x1, z1)
    params: dict[str, Any] = {"slot": slot, "dim": dim}
    if any(v is not None for v in corners):
        if any(v is None for v in corners):
            raise HTTPException(status_code=422, detail="a box needs x0, z0, x1 and z1")
        params.update(x0=x0, z0=z0, x1=x1, z1=z1)
    return await mc.call(settings, "GET", "/map/entities", params=params)


@router.get("/map/villager")
async def map_villager(
    settings: SettingsDep,
    vid: Annotated[str, Query(alias="id", pattern=_ID)],
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
) -> dict[str, Any]:
    """One villager's card, by the id `/map/entities` gave it: every trade it offers now,
    by level. `found` is null when the saved world no longer has it."""
    return await mc.call(settings, "GET", "/map/villagers", params={"slot": slot, "id": vid})


@router.post("/map/structures/sweep", status_code=202)
async def sweep_structures(
    settings: SettingsDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
) -> dict[str, Any]:
    """Search the loaded world for structures again, now. A 409 when it isn't the loaded,
    running world: `locate` asks the server, so only that world can answer."""
    return await mc.call(settings, "POST", "/map/structures/sweep", json={"slot": slot})


_PLAYERS = r"^[^,]{1,64}(,[^,]{1,64}){0,15}$"
_PARTS = r"^(trails|events|sessions|explored)(,(trails|events|sessions|explored)){0,3}$"


@router.get("/timeline")
async def map_timeline(
    request: Request,
    settings: SettingsDep,
    start: Annotated[float, Query(alias="from")],
    end: Annotated[float, Query(alias="to")],
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    players: Annotated[str | None, Query(pattern=_PLAYERS)] = None,
    include: Annotated[str, Query(pattern=_PARTS)] = "trails,events,sessions",
    dim: Annotated[str, Query(pattern=_DIM)] = "overworld",
) -> dict[str, Any]:
    """One world's travel log over [from, to] (epoch seconds) for the map's timeline (plan
    §M8a): each player's trail cut to at most 2,000 points, never across a session, a
    dimension or a respawn; the joins, leaves, deaths (with cause and killer), respawns and
    dimension changes in range; every session of the world, newest first; and, asked for
    with `include=explored`, the fog of war in `dim` with each chunk's first-seen time.
    `players` (comma-separated keys) narrows trails, events and fog to those players."""
    rec = await mc.slot_record(settings, slot)
    try:
        got = await timeline.timeline(
            _maker(request),
            str(rec["folder"]),
            start,
            end,
            players=[p for p in (players or "").split(",") if p],
            include=frozenset(include.split(",")),
            dim=dim,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"slot": slot, **got}


# --- share links (a no-login view of one world's map) --------------------------------
# The public read side is `api/minecraft_share.py`; these are the owner's mint, list and
# revoke. A link opens on one player but shows every player's position and history in
# its world, until revoked.


class MintMapShareIn(BaseModel):
    slot: str = Field(pattern=_SLOT)
    xuid: str = Field(min_length=1, max_length=64)


class MapShareOut(BaseModel):
    id: str
    slot: str
    xuid: str
    gamertag: str
    created_at: float
    last_viewed_at: float | None
    view_count: int


class MintMapShareOut(MapShareOut):
    # The bearer secret, shown exactly once; only its hash is kept.
    token: str


def _share_out(link: shares.ShareLink) -> MapShareOut:
    return MapShareOut(
        id=link.id,
        slot=link.slot,
        xuid=link.xuid,
        gamertag=link.gamertag,
        created_at=link.created_at.timestamp(),
        last_viewed_at=link.last_viewed_at.timestamp() if link.last_viewed_at else None,
        view_count=link.view_count,
    )


def _owner(owner: Any) -> SessionContext:
    return SessionContext(principal_id=owner.id, principal_kind="owner")


@router.post("/shares", status_code=201)
async def mint_map_share(
    body: MintMapShareIn, owner: OwnerDep, request: Request, settings: SettingsDep
) -> MintMapShareOut:
    """A share link for one world, opened on one of its players. 404 when the slot holds
    no world or the player has never been seen in it."""
    rec = await mc.slot_record(settings, body.slot)
    folder = str(rec["folder"])
    seen = await travel.latest_positions(_maker(request), folder)
    player = next((p for p in seen if p["xuid"] == body.xuid), None)
    if player is None:
        raise HTTPException(status_code=404, detail="that player has never been in this world")
    secret, link = await shares.mint_share(
        _maker(request),
        _owner(owner),
        slot=body.slot,
        world=folder,
        xuid=body.xuid,
        gamertag=str(player["gamertag"]),
    )
    return MintMapShareOut(**_share_out(link).model_dump(), token=secret)


@router.get("/shares")
async def list_map_shares(owner: OwnerDep, request: Request) -> list[MapShareOut]:
    return [_share_out(s) for s in await shares.list_shares(_maker(request), _owner(owner))]


@router.delete("/shares/{share_id}", status_code=204)
async def revoke_map_share(share_id: str, owner: OwnerDep, request: Request) -> None:
    if not await shares.revoke_share(_maker(request), _owner(owner), share_id):
        raise HTTPException(status_code=404, detail="unknown share link")
