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

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import DebugDep, SettingsDep
from jbrain.api.minecraft import WORLD_JOB_TIMEOUT_S
from jbrain.config import Settings
from jbrain.minecraft import client as mc
from jbrain.minecraft import markers, scans, timeline, travel, visited

router = APIRouter(prefix="/debug/minecraft")

SERVICE = mc.SERVICE
SIDECAR_TIMEOUT_S = mc.SIDECAR_TIMEOUT_S
SNAPSHOT_TIMEOUT_S = mc.SNAPSHOT_TIMEOUT_S
_sidecar = mc.call
_container = mc.container
_lifecycle = mc.lifecycle


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


# Read-only views of the world slots and the travel log (§3a, M2/M3, T1), so setting up
# and debugging them doesn't need the owner's phone. Changing the owner's worlds stays
# owner-only; the token can only make, reset and empty its own test worlds (§SW4), and
# flip the Power Pack switch (below) until the PWA has it.
_SLOT = r"^slot\d+$"


@router.get("/worlds")
async def minecraft_worlds(request: Request, settings: SettingsDep, _p: DebugDep) -> dict[str, Any]:
    request.state.debug_detail = "minecraft worlds"
    return await _sidecar(settings, "GET", "/worlds")


@router.get("/worlds/{slot}/rules")
async def minecraft_world_rules(
    slot: Annotated[str, Path(pattern=_SLOT)],
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
) -> dict[str, Any]:
    request.state.debug_detail = f"minecraft rules {slot}"
    return await _sidecar(settings, "GET", f"/worlds/{slot}/rules")


class PowerPackIn(BaseModel):
    on: bool


@router.put("/worlds/{slot}/power-pack")
async def minecraft_power_pack(
    slot: Annotated[str, Path(pattern=_SLOT)],
    body: PowerPackIn,
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
) -> dict[str, Any]:
    """The world's "Power Pack keeps inventory" switch (§M10) — the one owner-world
    setting this token may change, because until the PWA has its toggle this is how the
    owner, with no terminal, turns it on or off. It changes a game rule, never the
    world's contents, and off puts keepInventory back to the owner's choice (or
    vanilla's off)."""
    request.state.debug_detail = f"minecraft power pack {slot} on={body.on}"
    return await _sidecar(settings, "POST", f"/worlds/{slot}/power-pack", json={"on": body.on})


@router.put("/worlds/{slot}/power-pack-transport")
async def minecraft_power_pack_transport(
    slot: Annotated[str, Path(pattern=_SLOT)],
    body: PowerPackIn,
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
) -> dict[str, Any]:
    """The world's "Power Pack teleport" switch (§M10), on by default — beside the
    keep-inventory switch for the same reason: until the PWA has its toggle, this is how
    the owner, with no terminal, turns transport on request off or on. It changes no
    rule and nothing in the world; off, the pack refuses every trip."""
    request.state.debug_detail = f"minecraft power pack transport {slot} on={body.on}"
    return await _sidecar(
        settings, "POST", f"/worlds/{slot}/power-pack-transport", json={"on": body.on}
    )


class TestWorldIn(BaseModel):
    seed: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=40)
    wreck: bool = True


class TestResetIn(BaseModel):
    mode: Literal["same_seed", "new_seed", "empty"]
    seed: str | None = Field(default=None, max_length=64)
    wreck: bool = True


async def _slot(settings: Settings, slot: str) -> dict[str, Any]:
    worlds = await _sidecar(settings, "GET", "/worlds")
    rec = next((s for s in worlds.get("slots", []) if s.get("id") == slot), None)
    if rec is None:
        raise HTTPException(404, f"no such slot: {slot}")
    return rec


async def _nobody_on(settings: Settings, what: str) -> None:
    # A load or a reset of the loaded world restarts the server, which would put the
    # owner's family out of the game for a test.
    status = await _sidecar(settings, "GET", "/status")
    names = [str(p.get("name", "")) for p in status.get("players") or []]
    if names:
        raise HTTPException(409, f"{', '.join(names)} on the server — not {what} now")


@router.post("/test-worlds/{slot}")
async def minecraft_make_test_world(
    slot: Annotated[str, Path(pattern=_SLOT)],
    body: TestWorldIn,
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
) -> dict[str, Any]:
    """Make a test world from `seed` in an empty slot, marked `test`. It's generated
    (and its crash site built, while `wreck`) the first time it loads."""
    request.state.debug_detail = f"minecraft test world {slot} seed {body.seed}"
    rec = await _slot(settings, slot)
    if rec.get("exists") or rec.get("name") or rec.get("seed"):
        raise HTTPException(409, f"{slot} holds {rec.get('name') or 'a world'} — not a free slot")
    return await _sidecar(
        settings,
        "POST",
        f"/worlds/{slot}/create",
        json={
            "name": body.name or f"Test {body.seed}"[:40],
            "seed": body.seed,
            "wreck": body.wreck,
            "test": True,
        },
    )


@router.post("/test-worlds/{slot}/reset")
async def minecraft_reset_test_world(
    slot: Annotated[str, Path(pattern=_SLOT)],
    body: TestResetIn,
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
) -> dict[str, Any]:
    """Regenerate a test world (same or new seed) or empty its slot. Only a world this
    API made, and the owner hasn't since taken over, can be reset here."""
    request.state.debug_detail = f"minecraft reset test world {slot} {body.mode}"
    rec = await _slot(settings, slot)
    if not rec.get("test"):
        raise HTTPException(409, f"{rec.get('name') or slot} isn't a test world — owner-only")
    if rec.get("active"):
        await _nobody_on(settings, "resetting the loaded world")
    return await _sidecar(
        settings,
        "POST",
        f"/worlds/{slot}/reset",
        json={"mode": body.mode, "seed": body.seed, "wreck": body.wreck, "test": True},
        timeout_s=WORLD_JOB_TIMEOUT_S,
    )


@router.post("/worlds/{slot}/load")
async def minecraft_load_world(
    slot: Annotated[str, Path(pattern=_SLOT)],
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
) -> dict[str, Any]:
    """Load a slot's world (the loaded one is backed up first): a test world to check
    it, then the owner's own back. Refused while anyone is playing, and for an empty
    slot. Each load backs up the world it leaves, and automatic backups push out the
    oldest, so a sweep loads its test worlds back to back and the owner's once, at the
    end."""
    request.state.debug_detail = f"minecraft load {slot}"
    rec = await _slot(settings, slot)
    if not rec.get("name"):
        raise HTTPException(409, f"{slot} is empty — make a test world there first")
    await _nobody_on(settings, "switching worlds")
    return await _sidecar(settings, "POST", f"/worlds/{slot}/load", timeout_s=WORLD_JOB_TIMEOUT_S)


@router.get("/travel")
async def minecraft_travel(request: Request, _p: DebugDep) -> dict[str, Any]:
    """Counts per world and player — samples, chunks explored, deaths, first and last
    sample — enough to see the travel log recording, without handing out trails."""
    request.state.debug_detail = "minecraft travel summary"
    maker = cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)
    return {"players": await travel.summary(maker)}


@router.get("/markers")
async def minecraft_markers(request: Request, _p: DebugDep) -> dict[str, Any]:
    """The beacons and gates the session drain holds per world (§M26) — counts and the
    newest report, enough to see the pack's lines arriving, never where they are (the
    travel log's rule)."""
    request.state.debug_detail = "minecraft beacon and gate counts"
    maker = cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)
    return {"worlds": await markers.summary(maker)}


@router.get("/scans")
async def minecraft_scans(request: Request, _p: DebugDep) -> dict[str, Any]:
    """The in-game deep-scan limit (§M17) per world, player and Minecraft day: scans used
    and left, and how many results are kept for free repeats — counts, never the results,
    the same rule as `/travel`."""
    request.state.debug_detail = "minecraft scan counts"
    maker = cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)
    return {"per_day": scans.SCANS_PER_DAY, "players": await scans.summary(maker)}


@router.get("/timeline")
async def minecraft_timeline(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    start: Annotated[float, Query(alias="from")],
    end: Annotated[float, Query(alias="to")],
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
) -> dict[str, Any]:
    """What the map's timeline would send for a range — per player the samples read, the
    points kept and the segments, the events by kind, the session count — as counts, the
    same rule as `/travel`: no trail leaves over a token."""
    request.state.debug_detail = f"minecraft timeline counts {slot}"
    rec = await mc.slot_record(settings, slot)
    maker = cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)
    try:
        return await timeline.counts(maker, str(rec["folder"]), start, end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/map/info")
async def minecraft_map_info(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=r"^(overworld|nether|the_end)$")] = "overworld",
) -> dict[str, Any]:
    request.state.debug_detail = f"minecraft map info {slot} {dim}"
    return await _sidecar(settings, "GET", "/map/info", params={"slot": slot, "dim": dim})


@router.get("/map/point")
async def minecraft_map_point(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    x: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    z: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=r"^(overworld|nether|the_end)$")] = "overworld",
) -> dict[str, Any]:
    """The biome and surface height at one column, explored or from the seed's survey —
    Minecraft_Dave's `mc_what_is_at`."""
    request.state.debug_detail = f"minecraft map point {slot} {dim} {x},{z}"
    params = {"slot": slot, "dim": dim, "x": x, "z": z}
    return await _sidecar(settings, "GET", "/map/point", params=params)


@router.get("/map/find")
async def minecraft_map_find(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    x: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    z: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    blocks: Annotated[str, Query(pattern=r"^[a-z0-9_:,]{2,400}$")],
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=r"^(overworld|nether|the_end)$")] = "overworld",
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    radius: Annotated[int, Query(ge=16, le=16384)] = 2048,
) -> dict[str, Any]:
    """The nearest blocks of the named kinds in explored ground — Minecraft_Dave's
    `mc_find_block`."""
    request.state.debug_detail = f"minecraft map find {slot} {dim} {x},{z} {blocks}"
    params = {
        "slot": slot,
        "dim": dim,
        "x": x,
        "z": z,
        "blocks": blocks,
        "limit": limit,
        "radius": radius,
    }
    return await _sidecar(settings, "GET", "/map/find", params=params)


@router.get("/map/items")
async def minecraft_map_items(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    x: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    z: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    items: Annotated[str, Query(pattern=r"^[a-z0-9_:,]{2,600}$")],
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=r"^(overworld|nether|the_end)$")] = "overworld",
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    radius: Annotated[int | None, Query(ge=1, le=60_000_000)] = None,
    flush: bool = False,
) -> dict[str, Any]:
    """The nearest containers holding the named items, from the world's BlockEntity
    records — Minecraft_Dave's `mc_find_item` without the ground look. Also the index's
    own counts (records, unreadable, unopened loot) for checking the format on a real
    world. `flush` first has the running world write itself out."""
    request.state.debug_detail = f"minecraft map items {slot} {dim} {x},{z} {items}"
    params: dict[str, Any] = {
        "slot": slot,
        "dim": dim,
        "x": x,
        "z": z,
        "items": items,
        "limit": limit,
        "flush": int(flush),
    }
    if radius is not None:
        params["radius"] = radius
    return await _sidecar(settings, "GET", "/map/items", params=params)


@router.get("/map/villagers")
async def minecraft_map_villagers(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    x: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    z: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=r"^(overworld|nether|the_end)$")] = "overworld",
    profession: Annotated[str, Query(pattern=r"^[a-z0-9_]{0,40}$")] = "",
    sells: Annotated[str, Query(pattern=r"^[a-z0-9_:]{0,64}$")] = "",
    enchant: Annotated[str, Query(pattern=r"^[a-z0-9_]{0,40}$")] = "",
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    radius: Annotated[int | None, Query(ge=1, le=60_000_000)] = None,
    flush: bool = False,
) -> dict[str, Any]:
    """The nearest villagers with a job or selling an item or enchantment, from the
    world's actor records — Minecraft_Dave's `mc_find_villager` with no scan charged
    (§M4e). Only the trades each offers now. Also the entity index's own counts
    (records, unreadable, unplaced, overflow) for the box check on a real world."""
    request.state.debug_detail = f"minecraft map villagers {slot} {dim} {x},{z}"
    params: dict[str, Any] = {
        "slot": slot,
        "dim": dim,
        "x": x,
        "z": z,
        "profession": profession,
        "sells": sells,
        "enchant": enchant,
        "limit": limit,
        "flush": int(flush),
    }
    if radius is not None:
        params["radius"] = radius
    return await _sidecar(settings, "GET", "/map/villagers", params=params)


@router.get("/map/pets")
async def minecraft_map_pets(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    x: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    z: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=r"^(overworld|nether|the_end)$")] = "overworld",
    owner: Annotated[str, Query(pattern=r"^(-?\d{1,20})?$")] = "",
    owner_name: Annotated[str, Query(max_length=16)] = "",
    kind: Annotated[str, Query(pattern=r"^[a-z0-9_]{0,40}$")] = "",
    name: Annotated[str, Query(max_length=64)] = "",
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    radius: Annotated[int | None, Query(ge=1, le=60_000_000)] = None,
    everywhere: bool = False,
    flush: bool = False,
) -> dict[str, Any]:
    """Tamed pets (one player's by `owner` id or `owner_name` gamertag) or any mob named
    `name`, from the world's actor records — Minecraft_Dave's `mc_find_pet` with no scan
    charged (§M4e). For the box check that a pet's `OwnerNew` is its tamer's id."""
    request.state.debug_detail = f"minecraft map pets {slot} {dim} {x},{z}"
    params: dict[str, Any] = {
        "slot": slot,
        "dim": dim,
        "x": x,
        "z": z,
        "owner": owner,
        "owner_name": owner_name,
        "kind": kind,
        "name": name,
        "limit": limit,
        "everywhere": int(everywhere),
        "flush": int(flush),
    }
    if radius is not None:
        params["radius"] = radius
    return await _sidecar(settings, "GET", "/map/pets", params=params)


@router.get("/map/portals")
async def minecraft_map_portals(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=r"^(overworld|nether|the_end)?$")] = "",
    check: bool = True,
) -> dict[str, Any]:
    """The Nether portals the world recorded, the ones the Tricorder points through
    (§M4e). `check` leaves out a record whose portal block is gone; `check=false` lists
    them all, for the box check that a broken portal is caught."""
    request.state.debug_detail = f"minecraft map portals {slot} {dim or 'all'}"
    params: dict[str, Any] = {"slot": slot, "check": int(check)}
    if dim:
        params["dim"] = dim
    return await _sidecar(settings, "GET", "/map/portals", params=params)


@router.get("/map/entities")
async def minecraft_map_entities(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=r"^(overworld|nether|the_end)$")] = "overworld",
) -> dict[str, Any]:
    """What the map's Villagers and Pets layers get (`/api/minecraft/map/entities`), as
    counts and each village's centre, size and jobs — for the box check of the 48-block
    grouping against a real village — without every villager's row."""
    request.state.debug_detail = f"minecraft map entities {slot} {dim}"
    got = await _sidecar(settings, "GET", "/map/entities", params={"slot": slot, "dim": dim})
    return {
        "dimension": got.get("dimension"),
        "villages": got.get("villages", []),
        "villagers_listed": len(got.get("villagers", [])),
        "villagers_truncated": got.get("villagers_truncated", False),
        "pets": len(got.get("pets", [])),
        "pets_owner_unknown": sum(
            1 for p in got.get("pets", []) if p.get("owner") and not p.get("owner_name")
        ),
        "named": len(got.get("named", [])),
        "traders": got.get("traders", 0),
        "live": got.get("live"),
        "as_of": got.get("as_of"),
        "age_s": got.get("age_s"),
        "index": {k: got.get(k) for k in _INDEX_COUNTS},
    }


_INDEX_COUNTS = (
    "villagers_indexed",
    "pets_indexed",
    "named_indexed",
    "records",
    "unreadable_records",
    "unplaced_records",
    "overflow",
)


@router.get("/map/nearby")
async def minecraft_map_nearby(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    x: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    z: Annotated[int, Query(ge=-30_000_000, le=30_000_000)],
    dim: Annotated[str, Query(pattern=r"^(overworld|nether|the_end)$")] = "overworld",
) -> dict[str, Any]:
    """The nearest structure of each kind, by the running server's own `locate` —
    Minecraft_Dave's `mc_nearby`. Read-only: `locate` generates nothing."""
    request.state.debug_detail = f"minecraft map nearby {dim} {x},{z}"
    params = {"dim": dim, "x": x, "z": z}
    return await _sidecar(settings, "GET", "/map/nearby", params=params)


@router.get("/map/structures")
async def minecraft_map_structures(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    dim: Annotated[str, Query(pattern=r"^(overworld|nether|the_end)$")] = "overworld",
) -> dict[str, Any]:
    """The world's saved structure sweep — what the map's Structures overlay draws — and
    whether a sweep is running, done or failed."""
    request.state.debug_detail = f"minecraft map structures {slot} {dim}"
    return await _sidecar(settings, "GET", "/map/structures", params={"slot": slot, "dim": dim})


@router.post("/map/structures/sweep", status_code=202)
async def minecraft_map_sweep(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
) -> dict[str, Any]:
    """Sweep the loaded world for structures again (read-only `locate`s, in the
    background); a 409 unless it is the loaded, running world."""
    request.state.debug_detail = f"minecraft map sweep {slot}"
    return await _sidecar(settings, "POST", "/map/structures/sweep", json={"slot": slot})


@router.get("/map/tile/{dim}/{zoom}/{tx}/{tz}.png")
async def minecraft_map_tile(
    dim: Annotated[str, Path(pattern=r"^(overworld|nether|the_end)$")],
    zoom: Annotated[int, Path(ge=0, le=4)],
    tx: Annotated[int, Path(ge=-visited.TILE_LIMIT, le=visited.TILE_LIMIT)],
    tz: Annotated[int, Path(ge=-visited.TILE_LIMIT, le=visited.TILE_LIMIT)],
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    slot: Annotated[str, Query(pattern=_SLOT)] = "slot1",
    layer: Annotated[str, Query(pattern=r"^(surface|biomes)$")] = "surface",
) -> Response:
    """A map tile picture — a rendering, not the world's files, so it doesn't break the
    no-world-download rule above."""
    request.state.debug_detail = f"minecraft map tile {slot} {dim} {zoom}/{tx}/{tz} {layer}"
    maker = cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)
    seen = await visited.for_slot(maker, settings, slot, dim, zoom, tx, tz)
    png = await mc.image(
        settings,
        f"/map/tile/{dim}/{zoom}/{tx}/{tz}.png",
        params={"slot": slot, "layer": layer, **seen},
    )
    return Response(png, media_type="image/png")
