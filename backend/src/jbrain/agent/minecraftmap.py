"""Minecraft_Dave's map card: `mc_show_map` (docs/plans/MINECRAFT_BEDROCK_PLAN.md §P3, the
GUI gate docs/mocks/minecraft-map/, DESIGN.md "`minecraft_map` tool-view").

The tool answers with the `minecraft_map` view: a live, pannable piece of the box's biome
map inline in the chat. The model fills only data slots — the world slot, the dimension,
the centre, the zoom, which overlays are on, and the one thing the card is about — and
never a URL: the app builds every tile request itself from the on-box tile route. That is
the carve-out the owner approved with the map's GUI gate, and why nothing here returns a
link, an image or markup.

PWA only. In game there is no screen to show a card on, so the in-game tier never lists
it (`agents.MINECRAFT_DAVE_INGAME_TOOLS`) and the handler refuses a door turn as well.

The card is the owner's, so it shows the owner's tier of everything (every structure the
server has located, explored or not). Fair play for players is a question for the in-game
share link (§M8), not for this card.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import text

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.minecrafttools import (
    _COORD_MAX,
    _DIMENSION_NAMES,
    _DIMENSIONS,
    Player,
    _guarded,
    chat_player_for,
    fence,
    place_name,
)
from jbrain.db.session import scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import livescan, spawnpoints, travel
from jbrain.minecraft.asker import asker_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.settings_store import SqlSettingsStore

VIEW = "minecraft_map"
# `gate` and `beacon` (§M26): the card draws that gate or beacon selected from the box's
# own records — the payload carries only where it is. `scan` (§M25): a player's live
# Tricorder scan, which the card reads from the live players route; the payload carries
# only whose it is and where they were. `bed` (§M8): a player's respawn point, a bed or a
# respawn anchor, from the drain's record of the pack's lines; `spawn` says which.
FOCUS_KINDS = (
    "death",
    "player",
    "place",
    "structure",
    "spot",
    "gate",
    "beacon",
    "scan",
    "bed",
)
# What a `bed` focus's point was when the pack last looked, as the card draws it.
_SPAWN_NOUNS = {"bed": "bed", "anchor": "respawn anchor", "other": "spawn point"}
OVERLAYS = ("trails", "deaths", "structures", "players", "beacons", "gates")
# The chat card's own default: the asker's trail and deaths over the located structures,
# the beacons' squares and the gates' lines. Live player dots are the full map's job; on a
# card about yesterday they would mislead.
DEFAULT_OVERLAYS = frozenset({"trails", "deaths", "structures", "beacons", "gates"})
# Map zoom as the tile route counts it: 0 is one pixel per block, 4 one per chunk (the
# furthest the box renders). Below 0 the app scales tiles up itself, to 8 px per block.
ZOOM_MIN, ZOOM_MAX = -3, 4
ZOOM_FOCUS = -1  # 2 px per block: a death or a doorway reads on a 220-px card
ZOOM_SCAN = -2  # 4 px per block: the scan's 33-block square is about 130 px
ZOOM_AREA = 1
SLOTS = 5
LABEL_MAX = 40
# How much of the travel log the card's trail covers: the hours leading up to a focus
# with a time. With none, `from`/`to` stay null and the card shows the last day.
TRAIL_BEFORE_S = 2 * 3600
TRAIL_AFTER_S = 120
NETHER_SCALE = 8

_SLOT_ID = re.compile(r"^slot([1-9])$")


def _clean_label(raw: Any) -> str:
    """A label as shown on the card: printable, spacing collapsed, cut to the limit."""
    label = " ".join("".join(c for c in str(raw or "") if c.isprintable()).split())
    return label[:LABEL_MAX].rstrip()


def _coord(raw: Any) -> int | None:
    """A model-supplied block coordinate, or None when it isn't one inside the world."""
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if abs(value) <= _COORD_MAX else None


def parse_slot(raw: Any) -> int | None | str:
    """The slot number asked for (1-5), None for "the loaded one", or why it isn't one."""
    if raw in (None, ""):
        return None
    m = _SLOT_ID.fullmatch(str(raw).strip().lower())
    try:
        n = int(m.group(1)) if m else int(raw)
    except (TypeError, ValueError):
        return "slot must be a world slot number, 1 to 5."
    return n if 1 <= n <= SLOTS else "slot must be a world slot number, 1 to 5."


def parse_zoom(raw: Any, default: int) -> int | str:
    """The zoom clamped to what the card can draw, or why it isn't a zoom."""
    if raw in (None, ""):
        return default
    try:
        return max(ZOOM_MIN, min(int(float(raw)), ZOOM_MAX))
    except (TypeError, ValueError):
        return "zoom must be a whole number from -3 (8 px per block) to 4 (1 px per chunk)."


def parse_overlays(raw: Any) -> dict[str, bool]:
    """Which overlays are on. Names outside the closed set are dropped, never passed on —
    the card reads only the four it knows."""
    if raw in (None, ""):
        chosen = DEFAULT_OVERLAYS
    else:
        items = raw if isinstance(raw, list) else str(raw).split(",")
        chosen = frozenset(str(i).strip().lower() for i in items) & frozenset(OVERLAYS)
    return {name: name in chosen for name in OVERLAYS}


def overworld_side(x: int, z: int) -> tuple[int, int]:
    """Where a Nether spot leads in the Overworld: eight blocks for every one."""
    return x * NETHER_SCALE, z * NETHER_SCALE


def map_view(
    *,
    slot: str,
    world: str,
    loaded: bool,
    dim: str,
    center: tuple[int, int],
    zoom: int,
    overlays: dict[str, bool],
    focus: dict[str, Any] | None,
    trail: dict[str, Any] | None,
) -> ViewPayload:
    """The data-only `minecraft_map` payload (DESIGN.md "`minecraft_map` tool-view"). No
    URL, no markup, no colour: the card builds its tile requests from `slot`, `dim` and
    the view, and maps `focus.kind` to its own glyph."""
    on_nether = focus is not None and dim == "nether"
    ox, oz = overworld_side(focus["x"], focus["z"]) if on_nether and focus else (0, 0)
    return ViewPayload(
        view=VIEW,
        surface="inline",
        data={
            "slot": slot,
            "world": world,
            "loaded": loaded,
            "dim": dim,
            "center": {"x": center[0], "z": center[1]},
            "zoom": zoom,
            "overlays": overlays,
            "focus": focus,
            "overworld": {"x": ox, "z": oz} if on_nether else None,
            "trail": trail,
        },
    )


def map_summary(view: ViewPayload) -> str:
    """What the model is told: the card is showing, what it is centred on, and the
    numbers to answer with — the coordinates are said in prose, the card is the picture.
    World names, labels and the death's cause are player- or game-authored, so fenced."""
    d = view.data
    dim_name = _DIMENSION_NAMES[d["dim"]]
    where = f"X {d['center']['x']}, Z {d['center']['z']}"
    lines = [f"The app is showing a map card of world slot {d['slot'][4:]}, {dim_name}."]
    facts = [f"World: {d['world']}"]
    focus = d["focus"]
    if focus:
        y = f", Y {focus['y']}" if focus.get("y") is not None else ""
        facts.append(f"Card is about: {focus['kind']} — {focus['label'] or '(no label)'}")
        facts.append(f"At: X {focus['x']}, Z {focus['z']}{y} in the {dim_name}")
        if focus.get("at"):
            when = datetime.fromtimestamp(focus["at"], UTC).strftime("%Y-%m-%d %H:%M UTC")
            facts.append(f"When: {when}")
        if focus.get("cause"):
            killer = f" (by {focus['killer']})" if focus.get("killer") else ""
            facts.append(f"Cause: {focus['cause']}{killer}")
    else:
        facts.append(f"Centred on: {where}")
    if d["overworld"]:
        facts.append(f"Overworld side: X {d['overworld']['x']}, Z {d['overworld']['z']}")
    if not d["loaded"]:
        lines.append("That world isn't the loaded one, so the map is as of when it was played.")
    if focus and focus["kind"] == "bed" and focus.get("spawn") == "other":
        lines.append(
            "No bed or respawn anchor was seen at that point when the pack last looked: the"
            " game keeps a spawn point after its bed is broken, until the next death."
        )
    if focus and focus["kind"] == "scan":
        lines.append(
            "The card draws their Tricorder's scan live: the square it searches and the block"
            " it last found. For the words, mc_tricorder_scan with status."
        )
    lines.append(fence("\n".join(facts)))
    lines.append(
        "Answer in words with the coordinates; the card already draws the spot, and "
        "tapping it opens the full map. Don't describe the card or link to it."
    )
    return "\n".join(lines)


async def latest_death(s: AsyncSession, key: str, world: str) -> Any:
    """The player's newest death in one world's travel log, with where it happened: the
    event's own position, else the last sample before it (a death the pack reported
    without one)."""
    return (
        await s.execute(
            text(
                "SELECT extract(epoch FROM e.at) AS at, e.detail,"
                " coalesce(e.x, t.x) AS x, e.y AS y, coalesce(e.z, t.z) AS z,"
                " coalesce(e.dim, t.dim) AS where_dim"
                " FROM app.mc_player_events e"
                " LEFT JOIN LATERAL (SELECT x, z, dim FROM app.mc_player_track"
                "   WHERE world = e.world AND xuid = e.xuid AND at <= e.at"
                "   ORDER BY at DESC LIMIT 1) t ON true"
                " WHERE e.world = :w AND e.xuid = :k AND e.kind = 'death'"
                " ORDER BY e.at DESC LIMIT 1"
            ),
            {"k": key, "w": world},
        )
    ).first()


async def last_position(s: AsyncSession, key: str, world: str, dim: str | None) -> Any:
    """The player's newest travel-log sample in one world, in `dim` when given."""
    where_dim = " AND dim = :d" if dim else ""
    return (
        await s.execute(
            text(
                "SELECT extract(epoch FROM at) AS at, dim, x, y, z FROM app.mc_player_track"
                f" WHERE xuid = :k AND world = :w{where_dim} ORDER BY at DESC LIMIT 1"
            ),
            {"k": key, "w": world, "d": dim},
        )
    ).first()


async def saved_place(s: AsyncSession, key: str, world: str, name: str) -> Any:
    return (
        await s.execute(
            text(
                "SELECT name, x, y, z, dim FROM app.mc_places"
                " WHERE xuid = :k AND world = :w AND lower(name) = lower(:n)"
            ),
            {"k": key, "w": world, "n": name},
        )
    ).first()


def build_minecraft_map_handlers(
    maker: async_sessionmaker[AsyncSession],
    config: Any,
    settings_store: SqlSettingsStore | None,
) -> dict[str, ToolHandler]:
    """`mc_show_map`, bound like the other Minecraft tools: the sidecar for the world
    slots, and the owner-RLS'd travel log and saved places under `ctx.session`."""

    async def worlds() -> dict[str, Any] | str:
        try:
            return await mc.call(config, "GET", "/worlds")
        except HTTPException as exc:
            return f"The Minecraft server can't answer right now: {exc.detail}"

    async def mc_show_map(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        if asker_for(ctx.agent_session_id) is not None:
            return ToolOutput(
                "There is no screen in game to show a map on. Give directions in words.",
                result_brief="refused in game",
            )
        # Everything the model typed is checked before anything is asked of the box.
        slot_n = parse_slot(arguments.get("slot"))
        if isinstance(slot_n, str):
            return slot_n
        raw_dim = str(arguments.get("dimension") or "").strip().lower() or None
        if raw_dim is not None and raw_dim not in _DIMENSIONS:
            return "dimension must be overworld, nether or the_end."
        kind = str(arguments.get("focus") or "").strip().lower() or None
        if kind is not None and kind not in FOCUS_KINDS:
            return "focus must be one of: " + ", ".join(FOCUS_KINDS) + "."
        label = _clean_label(arguments.get("label"))
        rx, rz = arguments.get("x"), arguments.get("z")
        if (rx is None) != (rz is None):
            return "Give both x and z, or neither."
        at_xz: tuple[int, int] | None = None
        if rx is not None:
            x, z = _coord(rx), _coord(rz)
            if x is None or z is None:
                return "x and z must be block coordinates inside the world."
            at_xz = (x, z)
        if kind in ("structure", "spot", "gate", "beacon") and at_xz is None:
            return f"A {kind} needs its x and z."
        if kind == "place" and at_xz is None and not place_name(label):
            return "Name the saved place in label (mc_places lists them), or give x and z."
        default_zoom = ZOOM_SCAN if kind == "scan" else ZOOM_FOCUS if kind or at_xz else ZOOM_AREA
        zoom = parse_zoom(arguments.get("zoom"), default_zoom)
        if isinstance(zoom, str):
            return zoom
        overlays = parse_overlays(arguments.get("show"))

        got = await worlds()
        if isinstance(got, str):
            return got
        slots = [r for r in got.get("slots", []) if isinstance(r, dict)]
        if slot_n is None:
            rec = next((r for r in slots if r.get("active")), None)
            if rec is None:
                return "No world is loaded. Say which slot (1-5) to map."
        else:
            rec = next((r for r in slots if r.get("id") == f"slot{slot_n}"), None)
            if rec is None or not rec.get("exists"):
                return f"Slot {slot_n} is empty — there's no world there to map."
        slot = str(rec["id"])
        folder = str(rec.get("folder") or "")
        world = str(rec.get("name") or folder or slot)

        dim = raw_dim or "overworld"
        focus: dict[str, Any] | None = None
        center: tuple[int, int] | None = at_xz
        moment: float | None = None
        if kind == "scan":
            return await scan_card(rec, slot, folder, world, label, zoom, overlays, ctx)
        if kind == "bed" and at_xz is None:
            return await bed_card(rec, slot, folder, world, label, zoom, overlays, ctx)
        async with scoped_session(maker, ctx.session) as s:
            player: Player | None = await chat_player_for(
                s, settings_store, ctx.session, ctx.agent_session_id
            )
            needs_player = at_xz is None and kind in ("death", "player", "place")
            if needs_player and player is None:
                return (
                    "This chat has no Minecraft player yet, so there's no death, position or"
                    " place to show. Give x and z, or set the player with mc_player."
                )
            if at_xz is None and kind == "death" and player is not None:
                row = await latest_death(s, player.key, folder)
                if row is None or row.x is None or row.z is None:
                    return ToolOutput(
                        f"No death of {fence(player.gamertag)} is in this world's travel log.",
                        result_brief="no death",
                    )
                detail = row.detail if isinstance(row.detail, dict) else json.loads(row.detail)
                dim = str(row.where_dim or "overworld")
                moment = float(row.at)
                focus = {
                    "kind": "death",
                    "label": label or f"{player.gamertag} died here",
                    "x": round(row.x),
                    "y": None if row.y is None else round(row.y),
                    "z": round(row.z),
                    "at": round(moment),
                    "cause": str(detail.get("cause") or "") or None,
                    "killer": str(detail.get("killer") or "") or None,
                }
            elif at_xz is None and kind == "player" and player is not None:
                row = await last_position(s, player.key, folder, raw_dim)
                if row is None:
                    return ToolOutput(
                        f"{fence(player.gamertag)} hasn't been seen in this world.",
                        result_brief="not seen",
                    )
                dim, moment = str(row.dim), float(row.at)
                focus = {
                    "kind": "player",
                    "label": label or player.gamertag,
                    "x": round(row.x),
                    "y": round(row.y),
                    "z": round(row.z),
                    "at": round(moment),
                }
            elif at_xz is None and kind == "place" and player is not None:
                row = await saved_place(s, player.key, folder, place_name(label))
                if row is None:
                    return ToolOutput(
                        f"There's no saved place called {fence(label)} in this world.",
                        result_brief="no such place",
                    )
                dim = str(row.dim)
                focus = {
                    "kind": "place",
                    "label": str(row.name),
                    "x": int(row.x),
                    "y": None if row.y is None else int(row.y),
                    "z": int(row.z),
                }
            elif kind is not None and at_xz is not None:
                focus = {"kind": kind, "label": label, "x": at_xz[0], "y": None, "z": at_xz[1]}
            if focus is not None:
                center = (focus["x"], focus["z"])
            elif center is None:
                row = (
                    await last_position(s, player.key, folder, dim) if player is not None else None
                )
                center = (round(row.x), round(row.z)) if row is not None else (0, 0)

        trail = None
        if player is not None:
            until = moment + TRAIL_AFTER_S if moment is not None else None
            trail = {
                "player": player.key,
                "gamertag": player.gamertag,
                "from": round(moment - TRAIL_BEFORE_S) if moment is not None else None,
                "to": round(until) if until is not None else None,
            }
        view = map_view(
            slot=slot,
            world=world,
            loaded=bool(rec.get("active")),
            dim=dim,
            center=center,
            zoom=zoom,
            overlays=overlays,
            focus=focus,
            trail=trail,
        )
        brief = f"{focus['kind'] if focus else 'map'} · {_DIMENSION_NAMES[dim]}"
        return ToolOutput(map_summary(view), view=view, result_brief=brief)

    async def scan_card(
        rec: dict[str, Any],
        slot: str,
        folder: str,
        world: str,
        label: str,
        zoom: int,
        overlays: dict[str, bool],
        ctx: ToolContext,
    ) -> str | ToolOutput:
        """The `scan` card: whose scan is the label's gamertag, else this chat's player.
        Centred on where they are; the card draws the square, the block and the words from
        the live route, so it stays true after the answer."""
        async with scoped_session(maker, ctx.session) as s:
            chat = await chat_player_for(s, settings_store, ctx.session, ctx.agent_session_id)
            scans = await livescan.read(s, folder)
        gamertag = label or (chat.gamertag if chat is not None else "")
        if not gamertag:
            return "Say whose scan in label (a gamertag), or set this chat's player with mc_player."
        scan = scans.get(livescan.key(gamertag))
        if scan is None:
            return ToolOutput(
                f"{fence(gamertag)}'s Tricorder isn't scanning in this world.",
                result_brief="not scanning",
            )
        name = str(scan["gamertag"] or gamertag)
        where = next(
            (
                p
                for p in await travel.latest_positions(maker, folder, ctx.session)
                if str(p["gamertag"]).casefold() == name.casefold()
            ),
            None,
        )
        if where is None:
            return ToolOutput(
                f"{fence(name)} hasn't been seen in this world, so there's nowhere to centre"
                " their scan.",
                result_brief="not seen",
            )
        dim = str(where["dim"])
        focus = {
            "kind": "scan",
            "label": name,
            "x": round(where["x"]),
            "y": round(where["y"]),
            "z": round(where["z"]),
            "at": None if scan["at"] is None else round(scan["at"]),
        }
        view = map_view(
            slot=slot,
            world=world,
            loaded=bool(rec.get("active")),
            dim=dim,
            center=(focus["x"], focus["z"]),
            zoom=zoom,
            overlays={**overlays, "players": True},
            focus=focus,
            trail={"player": where["xuid"], "gamertag": name, "from": None, "to": None},
        )
        return ToolOutput(
            map_summary(view), view=view, result_brief=f"scan · {_DIMENSION_NAMES[dim]}"
        )

    async def bed_card(
        rec: dict[str, Any],
        slot: str,
        folder: str,
        world: str,
        label: str,
        zoom: int,
        overlays: dict[str, bool],
        ctx: ToolContext,
    ) -> str | ToolOutput:
        """The `bed` card with no coordinates: whose is the label's gamertag, else this
        chat's player; the point is their current one as the pack last sent it."""
        async with scoped_session(maker, ctx.session) as s:
            chat = await chat_player_for(s, settings_store, ctx.session, ctx.agent_session_id)
            if label:
                row = await spawnpoints.current_by_name(s, folder, label)
            elif chat is not None:
                row = await spawnpoints.current(s, folder, chat.key)
            else:
                return (
                    "Say whose bed in label (a gamertag), or set this chat's player with mc_player."
                )
        who = label or (chat.gamertag if chat is not None else "")
        if row is None:
            return ToolOutput(
                f"The pack hasn't reported {fence(who)}'s respawn point in this world yet.",
                result_brief="not reported",
            )
        name = str(row.gamertag or who)
        if row.kind == "none":
            return ToolOutput(
                f"{fence(name)} has no bed or respawn anchor set, so they wake at the world spawn.",
                result_brief="no bed set",
            )
        dim = str(row.dim)
        focus = {
            "kind": "bed",
            "label": _clean_label(f"{name}'s {_SPAWN_NOUNS.get(row.kind, 'bed')}"),
            "x": int(row.x),
            "y": int(row.y),
            "z": int(row.z),
            "spawn": str(row.kind),
        }
        trail = (
            {"player": chat.key, "gamertag": chat.gamertag, "from": None, "to": None}
            if chat is not None and chat.gamertag.casefold() == name.casefold()
            else None
        )
        view = map_view(
            slot=slot,
            world=world,
            loaded=bool(rec.get("active")),
            dim=dim,
            center=(focus["x"], focus["z"]),
            zoom=zoom,
            overlays=overlays,
            focus=focus,
            trail=trail,
        )
        return ToolOutput(
            map_summary(view), view=view, result_brief=f"bed · {_DIMENSION_NAMES[dim]}"
        )

    return {"mc_show_map": _guarded("mc_show_map", mc_show_map)}
