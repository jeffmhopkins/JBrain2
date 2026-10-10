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
    receipt = await tools["mc_memory_replace"]({"line": 1, "text": "Base at 120 70 -15"}, ctx)
    assert "was “Base at 100 64 -20”" in receipt
    assert "reads" in await tools["mc_memory_remove"]({"line": 2, "text": "wrong text"}, ctx)
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
        f" VALUES ('{ctx.agent_session_id}', 'x', 'x')",
    }
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, other) as s:
            await s.execute(text(inserts[table]))
