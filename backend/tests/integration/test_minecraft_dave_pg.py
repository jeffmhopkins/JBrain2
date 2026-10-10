"""Migration 0226 against real Postgres: Minecraft_Dave's per-player goals, progress
log and memory, through the real tool handlers.

The contract, case by case (docs/plans/MINECRAFT_BEDROCK_PLAN.md §P1):
- a chat starts about the owner's gamertag and stays bound to it; switching is explicit;
- with no player set, every write refuses and nothing is written;
- goals are numbered by creation and keep their numbers;
- memory changes one line at a time, a replaced or removed line survives as history,
  removal needs the line's exact text, and the caps refuse before writing;
- a player's data written by name moves onto their xuid once the server has seen them;
- the chat intro carries the player's memory and open goals, fenced as data;
- nobody but the owner can read or write any of the four tables.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import minecrafttools as mt
from jbrain.agent.loop import ToolContext
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)
TABLES = ("mc_chat_player", "mc_goal_log", "mc_goals", "mc_player_memory", "mc_player_sessions")


class FakeSettings:
    def __init__(self, gamertag: str | None) -> None:
        self.gamertag = gamertag

    async def minecraft_gamertag(self, _ctx: object) -> str | None:
        return self.gamertag


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, SessionContext(principal_kind="owner")) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _chat(maker: async_sessionmaker, owner: SessionContext) -> ToolContext:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.agent_sessions (id, principal_id, agent, domain_scopes)"
                " VALUES (:id, :pid, 'minecraft_dave', '{}')"
            ),
            {"id": sid, "pid": owner.principal_id},
        )
    return ToolContext(session=owner, scopes=(), agent_session_id=sid)


def _tools(maker: async_sessionmaker, gamertag: str | None) -> dict[str, Any]:
    return mt.build_minecraft_handlers(maker, object(), FakeSettings(gamertag))  # type: ignore[arg-type]


async def test_a_chat_starts_about_the_owner_and_stays_bound(maker) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    tools = _tools(maker, "Steve42")
    assert "Steve42" in await tools["mc_player"]({}, ctx)
    # The setting changes later: the open chat keeps its player.
    later = _tools(maker, "Mira")
    assert "Steve42" in await later["mc_player"]({}, ctx)
    assert "Mira" in await later["mc_player"]({"gamertag": "Mira"}, ctx)
    assert "Mira" in await later["mc_player"]({}, ctx)
    assert "isn't a gamertag" in await later["mc_player"]({"gamertag": "x; op @a"}, ctx)


async def test_no_player_means_every_write_refuses(maker) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    tools = _tools(maker, None)
    for name, args in (
        ("mc_goal_create", {"title": "Beacon"}),
        ("mc_log", {"text": "got iron"}),
        ("mc_memory_add", {"text": "base at 100 64 -20"}),
    ):
        assert "no Minecraft player" in await tools[name](args, ctx), name
    async with scoped_session(maker, owner) as s:
        for table in ("mc_goals", "mc_goal_log", "mc_player_memory"):
            assert (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar() == 0


async def test_goals_keep_their_numbers_and_the_log_follows_them(maker) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    tools = _tools(maker, "Steve42")
    await tools["mc_goal_create"]({"title": "Beacon at the base"}, ctx)
    await tools["mc_goal_create"]({"title": "20 obsidian", "notes": "for the portal"}, ctx)
    await tools["mc_goal_update"]({"goal": 1, "status": "done"}, ctx)
    open_goals = await tools["mc_goals"]({}, ctx)
    assert "2. [open] 20 obsidian" in open_goals and "Beacon" not in open_goals
    assert "1. [done] Beacon" in await tools["mc_goals"]({"status": "all"}, ctx)
    await tools["mc_log"]({"text": "got 12 obsidian", "goal": 2}, ctx)
    await tools["mc_log"]({"text": "Steve is halfway there", "by_dave": True}, ctx)
    log = await tools["mc_log_read"]({}, ctx)
    assert "[owner] (goal 2) got 12 obsidian" in log and "[dave]" in log
    assert "untrusted_external_data" in log  # player text is fenced
    assert "no goal 9" in await tools["mc_log"]({"text": "x", "goal": 9}, ctx)


async def test_memory_is_line_by_line_with_history(maker) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    tools = _tools(maker, "Steve42")
    await tools["mc_memory_add"]({"text": "Base at 100 64 -20"}, ctx)
    await tools["mc_memory_add"]({"text": "Scared of the Deep Dark"}, ctx)
    stale = await tools["mc_memory_replace"](
        {"line": 1, "old_text": "Scared of the Deep Dark", "text": "x"}, ctx
    )
    assert "Nothing changed" in stale  # a shifted number can't overwrite another line
    receipt = await tools["mc_memory_replace"](
        {"line": 1, "old_text": "Base at 100 64 -20", "text": "Base at 120 70 -15"}, ctx
    )
    assert "Base at 100 64 -20" in receipt and "Base at 120 70 -15" in receipt
    assert "Nothing changed" in await tools["mc_memory_remove"](
        {"line": 2, "text": "wrong text"}, ctx
    )
    await tools["mc_memory_remove"]({"line": 2, "text": "scared of the deep dark"}, ctx)
    current = await tools["mc_memory_read"]({}, ctx)
    assert "1. Base at 120 70 -15" in current and "Deep Dark" not in current
    async with scoped_session(maker, owner) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT text, superseded_at IS NOT NULL AS old FROM app.mc_player_memory"
                    " ORDER BY created_at"
                )
            )
        ).all()
    # Nothing was deleted: both edits left the earlier wording behind as history.
    assert [(r.text, r.old) for r in rows] == [
        ("Base at 100 64 -20", True),
        ("Scared of the Deep Dark", True),
        ("Base at 120 70 -15", False),
    ]


async def test_memory_caps_refuse_before_writing() -> None:
    full = [f"fact {i}" for i in range(mt.MEMORY_MAX_LINES)]
    assert "Consolidate" in mt.check_memory_op(full, "add", 0, "one more")
    assert "over the" in mt.check_memory_op([], "add", 0, "x" * (mt.MEMORY_LINE_MAX + 1))
    assert "already remembered" in mt.check_memory_op(["Base"], "add", 0, "base")
    assert mt.check_memory_op(["a"], "replace", 1, "b") == ""


async def test_data_kept_by_name_moves_to_the_xuid_once_seen(maker) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    tools = _tools(maker, "NewKid")
    await tools["mc_goal_create"]({"title": "First house"}, ctx)
    await tools["mc_memory_add"]({"text": "Just started"}, ctx)
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_player_sessions"
                " (xuid, gamertag, boot_id, start_event_id, started_at, last_seen_at)"
                " VALUES ('2535999', 'NewKid', 'b', 1, :t, :t)"
            ),
            {"t": datetime.now(UTC)},
        )
    assert "First house" in await tools["mc_goals"]({}, ctx)
    async with scoped_session(maker, owner) as s:
        keys = (
            (
                await s.execute(
                    text(
                        "SELECT DISTINCT xuid FROM app.mc_goals UNION"
                        " SELECT DISTINCT xuid FROM app.mc_player_memory"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert keys == ["2535999"]


async def test_the_chat_intro_carries_memory_and_open_goals(maker) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    tools = _tools(maker, "Steve42")
    await tools["mc_memory_add"]({"text": "Base at 100 64 -20"}, ctx)
    await tools["mc_goal_create"]({"title": "Beacon"}, ctx)
    assert ctx.agent_session_id is not None
    intro = await mt.chat_intro(maker, FakeSettings("Steve42"), owner, ctx.agent_session_id)  # type: ignore[arg-type]
    assert "1. Base at 100 64 -20" in intro and "1. Beacon" in intro
    assert intro.count("<untrusted_external_data") >= 2
    unset = await mt.chat_intro(maker, FakeSettings(None), owner, str(uuid.uuid4()))  # type: ignore[arg-type]
    assert "No player is set" in unset


@pytest.mark.parametrize("ctx_name", ["EVERY_SCOPE", "UNSCOPED"])
@pytest.mark.parametrize("table", ["mc_goals", "mc_goal_log", "mc_player_memory", "mc_chat_player"])
async def test_a_non_owner_sees_nothing_and_cannot_write(maker, ctx_name, table) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    tools = _tools(maker, "Steve42")
    await tools["mc_goal_create"]({"title": "Beacon"}, ctx)
    await tools["mc_log"]({"text": "got iron"}, ctx)
    await tools["mc_memory_add"]({"text": "Base"}, ctx)
    # An unbound chat, so a refused mc_chat_player insert can only be RLS, not the PK.
    fresh = await _chat(maker, owner)
    other = {"EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]
    async with scoped_session(maker, other) as s:
        assert (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar() == 0
    inserts = {
        "mc_goals": "INSERT INTO app.mc_goals (xuid, gamertag, title) VALUES ('x', 'x', 't')",
        "mc_goal_log": "INSERT INTO app.mc_goal_log (xuid, gamertag, text, source)"
        " VALUES ('x', 'x', 't', 'owner')",
        "mc_player_memory": "INSERT INTO app.mc_player_memory (xuid, seq, text, source)"
        " VALUES ('x', 1, 't', 'owner')",
        "mc_chat_player": "INSERT INTO app.mc_chat_player (agent_session_id, xuid, gamertag)"
        f" VALUES ('{fresh.agent_session_id}', 'x', 'x')",
    }
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, other) as s:
            await s.execute(text(inserts[table]))


async def _seen(maker, owner, xuid: str, gamertag: str, at: datetime | None = None) -> None:
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_player_sessions"
                " (xuid, gamertag, boot_id, start_event_id, started_at, last_seen_at)"
                " VALUES (:x, :g, :b, 1, :t, :t)"
            ),
            {"x": xuid, "g": gamertag, "b": str(uuid.uuid4()), "t": at or datetime.now(UTC)},
        )


async def test_a_reused_gamertag_never_swaps_the_player_under_a_chat(maker) -> None:
    owner = await _owner(maker)
    await _seen(maker, owner, "111", "Steve")
    ctx = await _chat(maker, owner)
    tools = _tools(maker, "Steve")
    await tools["mc_goal_create"]({"title": "Steve's beacon"}, ctx)
    # Steve renames; another account takes "Steve" and plays more recently.
    await _seen(maker, owner, "222", "Steve")
    assert "Steve's beacon" in await tools["mc_goals"]({}, ctx)
    async with scoped_session(maker, owner) as s:
        bound = (await s.execute(text("SELECT xuid FROM app.mc_chat_player"))).scalar()
    assert bound == "111"


async def test_rekeying_into_an_xuid_with_memory_keeps_every_line(maker) -> None:
    owner = await _owner(maker)
    await _seen(maker, owner, "333", "Known")
    known = await _chat(maker, owner)
    await _tools(maker, "Known")["mc_memory_add"]({"text": "Likes boats"}, known)
    # Memory written for the name before any session exists under it in this chat's eyes.
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_player_memory (xuid, seq, text, source)"
                " VALUES ('name:known', 1, 'Builds castles', 'owner')"
            )
        )
    later = await _chat(maker, owner)
    lines = await _tools(maker, "Known")["mc_memory_read"]({}, later)
    assert "1. Likes boats" in lines and "2. Builds castles" in lines


async def test_reopening_and_renaming_goals_keep_finish_times_honest(maker) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    tools = _tools(maker, "Steve42")
    await tools["mc_goal_create"]({"title": "Beacon"}, ctx)

    async def finished() -> Any:
        async with scoped_session(maker, owner) as s:
            return (await s.execute(text("SELECT finished_at FROM app.mc_goals"))).scalar()

    await tools["mc_goal_update"]({"goal": 1, "status": "done"}, ctx)
    first = await finished()
    assert first is not None
    await tools["mc_goal_update"]({"goal": 1, "title": "Beacon at the base"}, ctx)
    await tools["mc_goal_update"]({"goal": 1, "status": "done"}, ctx)  # already done
    assert await finished() == first
    await tools["mc_goal_update"]({"goal": 1, "status": "open"}, ctx)
    assert await finished() is None
    assert "1. [open] Beacon at the base" in await tools["mc_goals"]({}, ctx)


async def test_the_intro_is_a_snapshot_for_the_chat(maker) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    tools = _tools(maker, "Steve42")
    await tools["mc_memory_add"]({"text": "Base at 100 64 -20"}, ctx)
    sid = ctx.agent_session_id
    assert sid is not None
    settings = FakeSettings("Steve42")
    first = await mt.chat_intro(maker, settings, owner, sid)  # type: ignore[arg-type]
    await tools["mc_memory_add"]({"text": "Scared of the Deep Dark"}, ctx)
    # Byte-identical, so the local engine's prefix cache survives a memory edit.
    assert await mt.chat_intro(maker, settings, owner, sid) == first  # type: ignore[arg-type]
    await tools["mc_player"]({"gamertag": "Mira"}, ctx)
    switched = await mt.chat_intro(maker, settings, owner, sid)  # type: ignore[arg-type]
    assert "Mira" in switched and "Base at" not in switched


async def test_locate_searches_from_the_last_point_in_the_loaded_world(
    maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jbrain.minecraft import client as mc

    owner = await _owner(maker)
    await _seen(maker, owner, "444", "Walker")
    async with scoped_session(maker, owner) as s:
        for world, x in (("slot2", 900.0), ("world", 120.0)):
            await s.execute(
                text(
                    "INSERT INTO app.mc_player_track (world, xuid, gamertag, boot_id,"
                    " sample_id, at, dim, x, y, z) VALUES (:w, '444', 'Walker', :b, 1,"
                    " now(), 'overworld', :x, 64, -40)"
                ),
                {"w": world, "b": str(uuid.uuid4()), "x": x},
            )
    sent: list[str] = []

    async def call(_cfg: Any, _m: str, path: str, **kw: Any) -> dict[str, Any]:
        if path == "/worlds":
            return {"active": "world", "slots": []}
        sent.append(kw["json"]["command"])
        return {"lines": ["[x INFO] The nearest village is at block 300, ~, 10"]}

    monkeypatch.setattr(mc, "call", call)
    ctx = await _chat(maker, owner)
    out = await _tools(maker, "Walker")["mc_locate"]({"kind": "structure", "id": "village"}, ctx)
    assert sent == ["execute positioned 120 64 -40 run locate structure village"]
    assert "last known position" in out


async def test_play_history_lists_this_players_sessions(maker) -> None:
    owner = await _owner(maker)
    await _seen(maker, owner, "555", "Player5")
    ctx = await _chat(maker, owner)
    out = await _tools(maker, "Player5")["mc_play_history"]({"days": 3}, ctx)
    assert "last 3 days" in out and out.count(" for 0m") == 1
    assert "days must be" in await _tools(maker, "Player5")["mc_play_history"](
        {"days": "lots"}, ctx
    )
