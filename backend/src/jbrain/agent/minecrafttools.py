"""Minecraft_Dave's tools (docs/plans/MINECRAFT_BEDROCK_PLAN.md §P1).

Everything is about ONE player at a time — the chat's player (`mc_chat_player`), which
starts as the owner's gamertag setting and changes only through `mc_player`. Goals, the
progress log and memory are that player's; the server and world tools read the box's
Bedrock server through its sidecar.

**Writes run without approval, but only with a player defined** (owner decision): a chat
with no player refuses them and says how to set one, rather than guessing whose data to
touch. Memory has no whole-document write at all — add, replace one line, remove one line
— and a replaced or removed line is kept as history (`superseded_at`), never deleted.

Gamertags, world names and player-typed log lines are player-authored, so they reach
the model inside the untrusted-data fence the persona prompt declares inert.

`web`-classed like the archivist's memory: only an allowlist that names them reaches
them, so curator's wildcard never absorbs a Minecraft tool.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import HTTPException
from sqlalchemy import text

from jbrain.agent.briefs import FEED_TAG, neutralize_boundary
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import sessions as mc_sessions
from jbrain.minecraft.sessions import check_gamertag

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.settings_store import SqlSettingsStore

log = structlog.get_logger(__name__)


# Memory caps (plan §P1): a working set of facts, not a diary — the log is the diary.
MEMORY_MAX_LINES = 60
MEMORY_MAX_CHARS = 6000
MEMORY_LINE_MAX = 300
LOG_MAX = 500
GOAL_TITLE_MAX = 120

# Bedrock structure and biome ids: lowercase words, optionally namespaced.
_GAME_ID = re.compile(r"^(minecraft:)?[a-z_]{2,40}$")
_GOAL_STATUSES = ("open", "done", "abandoned")
_COORD_MAX = 30_000_000


def fence(body: str) -> str:
    """Wrap player-authored text in the data boundary the persona prompt declares inert,
    defanging any boundary tag inside it first so it cannot close the envelope."""
    return f'<{FEED_TAG} source="minecraft-players">\n{neutralize_boundary(body)}\n</{FEED_TAG}>'


def name_key(gamertag: str) -> str:
    # The fallback key `sessions.player_key` uses, case-folded so "Steve42" and "steve42"
    # are one player until the server reports their xuid.
    return f"name:{gamertag.casefold()}"


def check_memory_op(current: list[str], op: str, line: int, new_text: str) -> str:
    """Why this memory edit can't be applied, or "" — pure, checked before any write."""
    if op not in ("add", "replace", "remove"):
        return f"'{op}' isn't a memory edit — use add, replace or remove."
    if op in ("add", "replace"):
        if not new_text:
            return "text is required — the fact to remember."
        if len(new_text) > MEMORY_LINE_MAX:
            return (
                f"that line is {len(new_text)} characters, over the {MEMORY_LINE_MAX} limit."
                " One fact per line — shorten it or split it."
            )
    if op in ("replace", "remove") and not 1 <= line <= len(current):
        return (
            f"there is no line {line} — this player's memory has {len(current)} line(s)."
            if current
            else "this player's memory is empty, so there is nothing to change. Use add."
        )
    after = list(current)
    if op == "add":
        if new_text.casefold() in (c.casefold() for c in current):
            return "that's already remembered."
        after.append(new_text)
    elif op == "replace":
        after[line - 1] = new_text
    else:
        after.pop(line - 1)
    if len(after) > MEMORY_MAX_LINES:
        return (
            f"that would make {len(after)} lines, over the {MEMORY_MAX_LINES} limit."
            " Consolidate: replace or remove a line instead of adding one."
        )
    if sum(len(a) for a in after) > MEMORY_MAX_CHARS:
        return (
            f"that would put this player's memory over {MEMORY_MAX_CHARS} characters."
            " Consolidate older lines first."
        )
    return ""


def _reply(lines: list[str]) -> str:
    """A console reply's text, its log-line prefixes dropped."""
    return " ".join(line.split("] ", 1)[-1] for line in lines).strip() or "(no answer)"


def _int(raw: Any, default: int, low: int, high: int) -> int | None:
    """A model-supplied count clamped to range; None when it isn't a number at all."""
    if raw in (None, ""):
        return default
    try:
        return max(low, min(int(raw), high))
    except (TypeError, ValueError):
        return None


def _when(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


class Player:
    __slots__ = ("key", "gamertag")

    def __init__(self, key: str, gamertag: str) -> None:
        self.key, self.gamertag = key, gamertag


async def resolve_player(s: AsyncSession, gamertag: str) -> Player:
    """A gamertag → the key its data lives under. A player the server has seen is keyed
    by xuid; one it hasn't is keyed by name until it has, and the first time an xuid is
    known any name-keyed goals, log or memory move onto it, so nothing written before a
    player's first join is stranded."""
    row = (
        await s.execute(
            text(
                "SELECT xuid, gamertag FROM app.mc_player_sessions"
                " WHERE lower(gamertag) = lower(:g) AND xuid NOT LIKE 'name:%'"
                " ORDER BY started_at DESC LIMIT 1"
            ),
            {"g": gamertag},
        )
    ).first()
    if row is None:
        return Player(name_key(gamertag), gamertag)
    key = {"x": row.xuid, "n": name_key(gamertag)}
    for table in ("mc_goals", "mc_goal_log", "mc_chat_player"):
        await s.execute(text(f"UPDATE app.{table} SET xuid = :x WHERE xuid = :n"), key)
    # Name-keyed memory lines go after any the xuid already holds, so positions can't clash.
    await s.execute(
        text(
            "UPDATE app.mc_player_memory SET xuid = :x, seq = seq + (SELECT coalesce(max(seq), 0)"
            " FROM app.mc_player_memory WHERE xuid = :x) WHERE xuid = :n"
        ),
        key,
    )
    return Player(row.xuid, row.gamertag)


async def _current_lines(s: AsyncSession, key: str) -> list[tuple[int, str]]:
    rows = await s.execute(
        text(
            "SELECT seq, text FROM app.mc_player_memory"
            " WHERE xuid = :k AND superseded_at IS NULL ORDER BY seq"
        ),
        {"k": key},
    )
    return [(r.seq, r.text) for r in rows]


async def _goals(s: AsyncSession, key: str) -> list[Any]:
    # Numbered by creation over ALL of a player's goals, so "goal 3" never moves when an
    # earlier one is finished.
    rows = await s.execute(
        text(
            "SELECT id, title, notes, status, world, created_at, finished_at"
            " FROM app.mc_goals WHERE xuid = :k ORDER BY created_at, id"
        ),
        {"k": key},
    )
    return list(rows)


async def player_context(s: AsyncSession, key: str) -> str:
    """The block a Minecraft_Dave chat starts with: the player's memory and open goals,
    numbered the way the tools address them. Empty when there is nothing yet."""
    lines = await _current_lines(s, key)
    goals = [(i, g) for i, g in enumerate(await _goals(s, key), start=1) if g.status == "open"]
    parts: list[str] = []
    if lines:
        parts.append(
            "Memory:\n" + "\n".join(f"{i}. {t}" for i, (_, t) in enumerate(lines, start=1))
        )
    if goals:
        parts.append("Open goals:\n" + "\n".join(f"{i}. {g.title}" for i, g in goals))
    return fence("\n\n".join(parts)) if parts else ""


async def bind_chat(s: AsyncSession, agent_session_id: str | None, player: Player) -> None:
    if not agent_session_id:
        return
    # Binding to a different player drops the chat's intro snapshot, so the next turn
    # starts from that player's memory and goals.
    await s.execute(
        text(
            "INSERT INTO app.mc_chat_player (agent_session_id, xuid, gamertag)"
            " VALUES (:sid, :k, :g) ON CONFLICT (agent_session_id) DO UPDATE"
            " SET xuid = excluded.xuid, gamertag = excluded.gamertag, set_at = now(),"
            " intro = CASE WHEN app.mc_chat_player.xuid = excluded.xuid"
            " THEN app.mc_chat_player.intro END"
        ),
        {"sid": agent_session_id, "k": player.key, "g": player.gamertag},
    )


async def chat_player_for(
    s: AsyncSession,
    settings_store: SqlSettingsStore | None,
    session_ctx: SessionContext,
    agent_session_id: str | None,
) -> Player | None:
    """A chat's player: the one bound to it, else the owner's gamertag — bound on first
    use, so changing the setting later doesn't swap the player under an open chat."""
    if agent_session_id:
        row = (
            await s.execute(
                text("SELECT xuid, gamertag FROM app.mc_chat_player WHERE agent_session_id = :sid"),
                {"sid": agent_session_id},
            )
        ).first()
        if row is not None:
            # A chat bound to an xuid stays with that account: a gamertag can later belong
            # to someone else, and re-resolving by name would swap players mid-chat. Only
            # a name-keyed binding (not seen on the server yet) is looked up again.
            if not row.xuid.startswith("name:"):
                return Player(row.xuid, row.gamertag)
            return await resolve_player(s, row.gamertag)
    raw = await settings_store.minecraft_gamertag(session_ctx) if settings_store else None
    tag = check_gamertag(raw) if raw else None
    if tag is None:
        return None
    player = await resolve_player(s, tag)
    await bind_chat(s, agent_session_id, player)
    return player


async def chat_intro(
    maker: async_sessionmaker[AsyncSession],
    settings_store: SqlSettingsStore | None,
    session_ctx: SessionContext,
    agent_session_id: str,
) -> str:
    """What a Minecraft_Dave turn's system prompt ends with: whose chat it is, and that
    player's memory and open goals as they were when the chat started (plan §P1, "read at
    the start of a chat").

    Snapshotted on first use and reused for the chat's life, so the system prompt stays
    byte-identical turn to turn and the local engine's prefix cache holds. Edits made
    during the chat reach the model through its own tool results; switching players
    takes a fresh snapshot."""
    async with scoped_session(maker, session_ctx) as s:
        player = await chat_player_for(s, settings_store, session_ctx, agent_session_id)
        if player is None:
            return (
                "## This chat\nNo player is set for this chat and the owner hasn't set a"
                " gamertag. Before writing any goal, log entry or memory, ask who it's for"
                " and call mc_player."
            )
        saved = (
            await s.execute(
                text("SELECT intro FROM app.mc_chat_player WHERE agent_session_id = :sid"),
                {"sid": agent_session_id},
            )
        ).scalar()
        if saved:
            return str(saved)
        context = await player_context(s, player.key)
        about = f"## This chat\nThis chat is about the player {fence(player.gamertag)}."
        intro = f"{about}\n\n{context}" if context else f"{about} Nothing is remembered yet."
        await s.execute(
            text("UPDATE app.mc_chat_player SET intro = :i WHERE agent_session_id = :sid"),
            {"i": intro, "sid": agent_session_id},
        )
    return intro


def build_minecraft_handlers(
    maker: async_sessionmaker[AsyncSession],
    config: Any,
    settings_store: SqlSettingsStore | None,
) -> dict[str, ToolHandler]:
    """Minecraft_Dave's tools, bound to the app's sessionmaker, its config (the sidecar's
    address and token) and the settings store (the owner's gamertag). Each DB handler runs
    under `ctx.session`, so the owner-only RLS on the Minecraft tables is the gate."""

    async def chat_player(s: AsyncSession, ctx: ToolContext) -> Player | None:
        return await chat_player_for(s, settings_store, ctx.session, ctx.agent_session_id)

    async def bind(s: AsyncSession, ctx: ToolContext, player: Player) -> None:
        await bind_chat(s, ctx.agent_session_id, player)

    no_player = (
        "This chat has no Minecraft player yet, so nothing was written. Ask which player"
        " it's for and call mc_player with their gamertag — or the owner can set theirs on"
        " the Minecraft screen."
    )

    async def sidecar(method: str, path: str, **kw: Any) -> dict[str, Any] | str:
        """The sidecar's answer, or a sentence saying why there isn't one."""
        try:
            return await mc.call(config, method, path, **kw)
        except HTTPException as exc:
            return f"The Minecraft server can't answer right now: {exc.detail}"

    async def console(command: str) -> list[str] | str:
        got = await sidecar("POST", "/command", json={"command": command, "wait_s": 3.0})
        if isinstance(got, str):
            return got
        return [str(line) for line in got.get("lines", [])]

    # --- the player ------------------------------------------------------------------

    async def mc_player(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        raw = str(arguments.get("gamertag") or "").strip()
        async with scoped_session(maker, ctx.session) as s:
            if not raw:
                player = await chat_player(s, ctx)
                if player is None:
                    return ToolOutput(no_player, result_brief="no player")
                return ToolOutput(
                    "This chat is about " + fence(player.gamertag), result_brief=player.gamertag
                )
            tag = check_gamertag(raw)
            if tag is None:
                return "That isn't a gamertag (letters, digits and spaces, up to 16)."
            player = await resolve_player(s, tag)
            await bind(s, ctx, player)
            seen = not player.key.startswith("name:")
        note = "" if seen else " They haven't joined this server yet; their data is kept by name."
        return ToolOutput(
            f"This chat is now about {fence(player.gamertag)}.{note}",
            result_brief=f"→ {player.gamertag}",
        )

    # --- server, players, history -----------------------------------------------------

    async def mc_server_status(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        status = await sidecar("GET", "/status")
        if isinstance(status, str):
            return status
        lines = [
            f"Server: {status.get('state')}, Bedrock {status.get('version') or '?'}",
            f"LAN address: {status.get('lan_ip')}:19132",
        ]
        if status.get("uptime_s"):
            lines.append(f"Up for {_duration(float(status['uptime_s']))}")
        worlds = await sidecar("GET", "/worlds")
        if isinstance(worlds, dict):
            for w in worlds.get("slots", []):
                if w.get("exists"):
                    mark = " (loaded)" if w.get("active") else ""
                    lines.append(
                        f"World {w['id']}: {fence(str(w.get('name')))}{mark},"
                        f" {w.get('gamemode')}, {w.get('difficulty')}"
                    )
        online = [p.get("name", "") for p in status.get("players", [])]
        lines.append("Online: " + (fence(", ".join(online)) if online else "nobody"))
        return ToolOutput("\n".join(lines), result_brief=f"{status.get('state')}, {len(online)} on")

    async def mc_players(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        status = await sidecar("GET", "/status")
        online = (
            {
                mc_sessions.player_key(str(p.get("xuid", "")), str(p.get("name", ""))): float(
                    p.get("joined_at") or 0
                )
                for p in status.get("players", [])
            }
            if isinstance(status, dict)
            else {}
        )
        rows = await mc_sessions.players(maker, online, ctx=ctx.session)
        if not rows:
            return ToolOutput("Nobody has played on this server yet.", result_brief="none yet")
        body = "\n".join(
            f"{r['gamertag']}: {'online now, ' if r['online'] else ''}"
            f"{_duration(r['total_seconds'])} over {r['sessions']} session(s),"
            f" last seen {_when(datetime.fromtimestamp(r['last_seen'], UTC))}"
            for r in rows
        )
        return ToolOutput(fence(body), result_brief=f"{len(rows)} player(s)")

    async def mc_play_history(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        days = _int(arguments.get("days"), 14, 1, 90)
        if days is None:
            return "days must be a number of days."
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            rows = (
                await s.execute(
                    text(
                        "SELECT started_at, coalesce(ended_at, last_seen_at) AS ended"
                        " FROM app.mc_player_sessions"
                        " WHERE xuid = :k AND started_at >= :since ORDER BY started_at"
                    ),
                    {"k": player.key, "since": datetime.now(UTC) - timedelta(days=days)},
                )
            ).all()
        if not rows:
            return ToolOutput(
                f"No play sessions for {fence(player.gamertag)} in the last {days} days.",
                result_brief="none",
            )
        body = "\n".join(
            f"{_when(r.started_at)} for {_duration((r.ended - r.started_at).total_seconds())}"
            for r in rows
        )
        return ToolOutput(
            f"{fence(player.gamertag)}, last {days} days:\n{body}",
            result_brief=f"{len(rows)} session(s)",
        )

    # --- the world -------------------------------------------------------------------

    async def mc_world_info(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        out: list[str] = []
        for label, command in (
            ("Day", "time query day"),
            (
                "Time of day (ticks, 0 = sunrise, 6000 = noon, 18000 = midnight)",
                "time query daytime",
            ),
            ("Weather", "weather query"),
        ):
            got = await console(command)
            if isinstance(got, str):
                return got
            out.append(f"{label}: {_reply(got)}")
        # The console prints whatever arrives in the wait window — a join line with a
        # gamertag, an add-on's event line — so the reply is fenced like any player text.
        return ToolOutput(fence("\n".join(out)), result_brief="time + weather")

    async def mc_locate(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        kind = str(arguments.get("kind") or "")
        what = str(arguments.get("id") or "").strip().lower()
        if kind not in ("structure", "biome"):
            return "kind must be structure or biome."
        if not _GAME_ID.fullmatch(what):
            return "That isn't a Bedrock id — e.g. mansion, village, ancient_city, cherry_grove."
        if kind == "biome" and not what.startswith("minecraft:"):
            what = f"minecraft:{what}"  # biomes need the namespace; structures don't (M0b)
        x, z = arguments.get("x"), arguments.get("z")
        origin = "the coordinates given"
        if (x is None) != (z is None):
            return "Give both x and z, or neither (to search from the player's last position)."
        if x is None or z is None:
            # Only a point in the LOADED world means anything to its generator: the trail
            # also holds every other slot the player has walked.
            worlds = await sidecar("GET", "/worlds")
            loaded = worlds.get("active") if isinstance(worlds, dict) else None
            async with scoped_session(maker, ctx.session) as s:
                player = await chat_player(s, ctx)
                last = (
                    (
                        await s.execute(
                            text(
                                "SELECT x, z FROM app.mc_player_track WHERE xuid = :k"
                                " AND world = :w AND dim = 'overworld'"
                                " ORDER BY at DESC LIMIT 1"
                            ),
                            {"k": player.key, "w": loaded},
                        )
                    ).first()
                    if player is not None and loaded
                    else None
                )
            if last is not None:
                x, z, origin = last.x, last.z, "the player's last known position"
            else:
                x, z, origin = 0, 0, "world spawn (0, 0)"
        try:
            xi, zi = int(float(x)), int(float(z))
        except (TypeError, ValueError):
            return "x and z must be numbers."
        if abs(xi) > _COORD_MAX or abs(zi) > _COORD_MAX:
            return "Those coordinates are outside the world."
        got = await console(f"execute positioned {xi} 64 {zi} run locate {kind} {what}")
        if isinstance(got, str):
            return got
        return ToolOutput(
            f"Searching from {origin} ({xi}, {zi}): {fence(_reply(got))}", result_brief=what
        )

    # --- goals and the progress log ---------------------------------------------------

    async def mc_goals(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        want = str(arguments.get("status") or "open")
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            goals = await _goals(s, player.key)
        shown = [(i, g) for i, g in enumerate(goals, start=1) if want == "all" or g.status == want]
        if not shown:
            return ToolOutput(
                f"No {'' if want == 'all' else want + ' '}goals for {fence(player.gamertag)}.",
                result_brief="none",
            )
        body = "\n".join(
            f"{i}. [{g.status}] {g.title}" + (f" — {g.notes}" if g.notes else "") for i, g in shown
        )
        return ToolOutput(
            f"Goals for {fence(player.gamertag)}:\n{fence(body)}",
            result_brief=f"{len(shown)} goal(s)",
        )

    async def mc_goal_create(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        title = " ".join(str(arguments.get("title") or "").split())
        notes = " ".join(str(arguments.get("notes") or "").split())
        if not title or len(title) > GOAL_TITLE_MAX:
            return f"A goal needs a title of 1–{GOAL_TITLE_MAX} characters."
        if len(notes) > LOG_MAX:
            return f"Notes are capped at {LOG_MAX} characters."
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            await s.execute(
                text(
                    "INSERT INTO app.mc_goals (xuid, gamertag, title, notes)"
                    " VALUES (:k, :g, :t, :n)"
                ),
                {"k": player.key, "g": player.gamertag, "t": title, "n": notes},
            )
            number = len(await _goals(s, player.key))
        return ToolOutput(
            f"Added goal {number} for {fence(player.gamertag)}: {fence(title)}",
            result_brief=f"goal {number}",
        )

    async def mc_goal_update(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        try:
            number = int(arguments.get("goal") or 0)
        except (TypeError, ValueError):
            return "goal must be a goal number from mc_goals."
        status = arguments.get("status")
        title = " ".join(str(arguments.get("title") or "").split())
        if status is not None and status not in _GOAL_STATUSES:
            return "status must be open, done or abandoned."
        if title and len(title) > GOAL_TITLE_MAX:
            return f"A title is at most {GOAL_TITLE_MAX} characters."
        if status is None and not title:
            return "Nothing to change — pass a status or a new title."
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            goals = await _goals(s, player.key)
            if not 1 <= number <= len(goals):
                return f"There is no goal {number} — {fence(player.gamertag)} has {len(goals)}."
            goal = goals[number - 1]
            await s.execute(
                text(
                    "UPDATE app.mc_goals SET title = coalesce(:t, title),"
                    " status = coalesce(:st, status),"
                    " finished_at = CASE WHEN coalesce(:st, status) = 'open' THEN NULL"
                    "   WHEN :st IS NOT NULL AND :st <> status THEN now() ELSE finished_at END"
                    " WHERE id = :id"
                ),
                {"t": title or None, "st": status, "id": goal.id},
            )
        change = ", ".join(
            x
            for x in (f"now {status}" if status else "", f"renamed “{title}”" if title else "")
            if x
        )
        return ToolOutput(
            f"Goal {number} ({fence(goal.title)}): {change}.", result_brief=f"goal {number}"
        )

    async def mc_log(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        entry = " ".join(str(arguments.get("text") or "").split())
        if not entry or len(entry) > LOG_MAX:
            return f"A log entry is 1–{LOG_MAX} characters."
        source = "dave" if arguments.get("by_dave") else "owner"
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            goal_id = None
            if arguments.get("goal") not in (None, "", 0):
                goals = await _goals(s, player.key)
                try:
                    number = int(arguments["goal"])
                except (TypeError, ValueError):
                    return "goal must be a goal number from mc_goals."
                if not 1 <= number <= len(goals):
                    return f"There is no goal {number} — {fence(player.gamertag)} has {len(goals)}."
                goal_id = goals[number - 1].id
            await s.execute(
                text(
                    "INSERT INTO app.mc_goal_log (xuid, gamertag, goal_id, text, source)"
                    " VALUES (:k, :g, :gid, :t, :src)"
                ),
                {"k": player.key, "g": player.gamertag, "gid": goal_id, "t": entry, "src": source},
            )
        return ToolOutput(
            f"Logged for {fence(player.gamertag)}: {fence(entry)}", result_brief="logged"
        )

    async def mc_log_read(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        limit = _int(arguments.get("limit"), 20, 1, 100)
        if limit is None:
            return "limit must be a number of entries."
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            goals = await _goals(s, player.key)
            number_of = {g.id: i for i, g in enumerate(goals, start=1)}
            params: dict[str, Any] = {"k": player.key, "n": limit}
            only = ""
            if arguments.get("goal") not in (None, "", 0):
                try:
                    number = int(arguments["goal"])
                except (TypeError, ValueError):
                    return "goal must be a goal number from mc_goals."
                if not 1 <= number <= len(goals):
                    return f"There is no goal {number} — {fence(player.gamertag)} has {len(goals)}."
                only, params["gid"] = " AND goal_id = :gid", goals[number - 1].id
            rows = (
                await s.execute(
                    text(
                        "SELECT at, text, source, goal_id FROM app.mc_goal_log"
                        f" WHERE xuid = :k{only} ORDER BY at DESC LIMIT :n"
                    ),
                    params,
                )
            ).all()
        if not rows:
            return ToolOutput(f"{fence(player.gamertag)}'s log is empty.", result_brief="empty")
        body = "\n".join(
            f"{_when(r.at)} [{r.source}]"
            + (f" (goal {number_of[r.goal_id]})" if r.goal_id in number_of else "")
            + f" {r.text}"
            for r in reversed(rows)
        )
        return ToolOutput(
            f"Progress log for {fence(player.gamertag)}, oldest first:\n{fence(body)}",
            result_brief=f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}",
        )

    # --- memory: line-level only, history kept -----------------------------------------

    async def mc_memory_read(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            lines = await _current_lines(s, player.key)
        if not lines:
            return ToolOutput(
                f"Nothing remembered about {fence(player.gamertag)} yet.", result_brief="empty"
            )
        body = "\n".join(f"{i}. {t}" for i, (_, t) in enumerate(lines, start=1))
        return ToolOutput(
            f"Memory for {fence(player.gamertag)}:\n{fence(body)}",
            result_brief=f"{len(lines)} line(s)",
        )

    def memory_edit(op: str) -> Callable[[dict, ToolContext], Awaitable[str | ToolOutput]]:
        async def run(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
            new_text = " ".join(str(arguments.get("text") or "").split())
            old_text = " ".join(str(arguments.get("old_text") or "").split())
            try:
                line = int(arguments.get("line") or 0)
            except (TypeError, ValueError):
                return "line must be a line number from mc_memory_read."
            async with scoped_session(maker, ctx.session) as s:
                player = await chat_player(s, ctx)
                if player is None:
                    return no_player
                current = await _current_lines(s, player.key)
                refusal = check_memory_op([t for _, t in current], op, line, new_text)
                # replace and remove name the line's CURRENT text as well as its number:
                # numbers shift after a remove, and a stale one must never hit another line.
                expect = new_text if op == "remove" else old_text
                if op == "replace" and not old_text and not refusal:
                    refusal = "old_text is required — the line's current wording, verbatim."
                if (
                    op != "add"
                    and not refusal
                    and current[line - 1][1].casefold() != expect.casefold()
                ):
                    refusal = (
                        f"line {line} reads {fence(current[line - 1][1])}, not what you passed."
                        " Read the memory again and use that line's current number and text."
                    )
                if refusal:
                    return ToolOutput(f"Nothing changed: {refusal}", result_brief="refused")
                if op in ("replace", "remove"):
                    seq, before = current[line - 1]
                    await s.execute(
                        text(
                            "UPDATE app.mc_player_memory SET superseded_at = now()"
                            " WHERE xuid = :k AND seq = :seq AND superseded_at IS NULL"
                        ),
                        {"k": player.key, "seq": seq},
                    )
                else:
                    seq = max((q for q, _ in current), default=0) + 1
                    before = ""
                if op in ("add", "replace"):
                    await s.execute(
                        text(
                            "INSERT INTO app.mc_player_memory (xuid, seq, text, source)"
                            " VALUES (:k, :seq, :t, 'owner')"
                        ),
                        {"k": player.key, "seq": seq, "t": new_text},
                    )
                count = len(current) + (1 if op == "add" else -1 if op == "remove" else 0)
            # The receipt quotes before and after, so the model's account of its own memory
            # stays grounded (the archivist's lesson).
            receipt = {
                "add": f"Added line {count}: {fence(new_text)}.",
                "replace": f"Line {line} was {fence(before)}, now {fence(new_text)}"
                " (the old text is kept).",
                "remove": f"Removed line {line}, {fence(before)} (kept in history; lines after"
                " it move up one).",
            }[op]
            return ToolOutput(
                f"{receipt} {fence(player.gamertag)}'s memory now has {count} line(s).",
                result_brief=f"{op} · {count} line(s)",
            )

        return run

    handlers: dict[str, ToolHandler] = {
        "mc_player": mc_player,
        "mc_server_status": mc_server_status,
        "mc_players": mc_players,
        "mc_play_history": mc_play_history,
        "mc_world_info": mc_world_info,
        "mc_locate": mc_locate,
        "mc_goals": mc_goals,
        "mc_goal_create": mc_goal_create,
        "mc_goal_update": mc_goal_update,
        "mc_log": mc_log,
        "mc_log_read": mc_log_read,
        "mc_memory_read": mc_memory_read,
        "mc_memory_add": memory_edit("add"),
        "mc_memory_replace": memory_edit("replace"),
        "mc_memory_remove": memory_edit("remove"),
    }
    return {name: _guarded(name, h) for name, h in handlers.items()}


def _guarded(name: str, handler: ToolHandler) -> ToolHandler:
    """Every failure comes back as a sentence the model can act on, never the loop's
    generic internal error — and says plainly that nothing was written."""

    async def run(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        try:
            return await handler(arguments, ctx)
        except Exception as exc:  # noqa: BLE001 — a failed Minecraft tool is an observation
            log.warning("minecraft.tool_failed", tool=name, error=repr(exc))
            return f"{name} failed (internal) — nothing was changed. Say so; don't guess."

    return run
