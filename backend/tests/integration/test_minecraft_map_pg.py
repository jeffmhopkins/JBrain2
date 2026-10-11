"""`mc_show_map` against real Postgres: the travel-log reads behind the map card (§P3).

The card's defaults come from the owner-RLS'd travel log and saved places, so their SQL is
pinned here rather than faked: the newest death in the asked world (another world's, or
another player's, never), a death the pack reported without a position placed at the
last sample before it, the player's last spot in a dimension, and a saved place by name.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import minecraftmap as mm
from jbrain.agent.loop import ToolContext
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

TABLES = ("mc_places", "mc_chat_player", "mc_player_track", "mc_player_events")
T0 = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
WORLDS = {
    "active": "Castle Hill",
    "slots": [
        {
            "id": "slot2",
            "name": "Castle Hill",
            "folder": "Castle Hill",
            "exists": True,
            "active": True,
        },
    ],
}


class FakeSettings:
    async def minecraft_gamertag(self, _ctx: object) -> str | None:
        return "BlockyFox"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, SessionContext(principal_kind="owner")) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
        await s.execute(text("DELETE FROM app.agent_sessions WHERE agent = 'minecraft_dave'"))
    await engine.dispose()


@pytest.fixture(autouse=True)
def sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        return WORLDS

    monkeypatch.setattr(mc, "call", call)


async def _chat(maker: async_sessionmaker) -> ToolContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    owner = SessionContext(principal_id=str(pid), principal_kind="owner")
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


async def _seed(maker: async_sessionmaker, owner: SessionContext) -> None:
    boot = str(uuid.uuid4())
    track = [
        # (world, player, minutes after T0, dim, x, z)
        ("Castle Hill", "name:blockyfox", 0, "overworld", -140.0, 90.0),
        ("Castle Hill", "name:blockyfox", 50, "nether", 38.0, -55.0),
        ("Castle Hill", "name:blockyfox", 70, "overworld", -138.0, 92.0),
        ("Other World", "name:blockyfox", 90, "overworld", 5000.0, 5000.0),
    ]
    events = [
        # A death with no position: placed at the sample before it.
        ("Castle Hill", "name:blockyfox", 55, "death", None, None, None, {"cause": "lava"}),
        ("Castle Hill", "name:steve42", 80, "death", "overworld", 1.0, 1.0, {"cause": "drowning"}),
        ("Other World", "name:blockyfox", 95, "death", "overworld", 9.0, 9.0, {}),
    ]
    async with scoped_session(maker, owner) as s:
        for i, (world, key, mins, dim, x, z) in enumerate(track):
            await s.execute(
                text(
                    "INSERT INTO app.mc_player_track"
                    " (world, xuid, gamertag, boot_id, sample_id, at, dim, x, y, z)"
                    " VALUES (:w, :k, 'BlockyFox', :b, :i, :at, :d, :x, 70, :z)"
                ),
                {
                    "w": world,
                    "k": key,
                    "b": boot,
                    "i": i,
                    "at": T0 + timedelta(minutes=mins),
                    "d": dim,
                    "x": x,
                    "z": z,
                },
            )
        for i, (world, key, mins, kind, dim, x, z, detail) in enumerate(events):
            await s.execute(
                text(
                    "INSERT INTO app.mc_player_events"
                    " (world, xuid, gamertag, boot_id, event_id, at, kind, dim, x, z, detail)"
                    " VALUES (:w, :k, 'p', :b, :i, :at, :kind, :d, :x, :z, CAST(:det AS jsonb))"
                ),
                {
                    "w": world,
                    "k": key,
                    "b": boot,
                    "i": i,
                    "at": T0 + timedelta(minutes=mins),
                    "kind": kind,
                    "d": dim,
                    "x": x,
                    "z": z,
                    "det": json.dumps(detail),
                },
            )
        await s.execute(
            text(
                "INSERT INTO app.mc_places (xuid, gamertag, world, name, x, y, z, dim)"
                " VALUES ('name:blockyfox', 'BlockyFox', 'Castle Hill', 'Blaze farm',"
                " 20, 72, -70, 'nether')"
            )
        )


async def test_the_card_reads_the_asked_worlds_log(maker: async_sessionmaker) -> None:
    ctx = await _chat(maker)
    await _seed(maker, ctx.session)
    tools = mm.build_minecraft_map_handlers(maker, object(), FakeSettings())  # type: ignore[arg-type]

    async def show(args: dict[str, Any]) -> Any:
        return await tools["mc_show_map"](args, ctx)

    out = await show({"focus": "death"})
    assert out.view is not None, out
    d = out.view.data
    # This world's newest death of this player: the lava, not Steve's or the other world's.
    assert d["dim"] == "nether" and d["focus"]["cause"] == "lava"
    assert (d["focus"]["x"], d["focus"]["z"]) == (38, -55)
    assert d["focus"]["at"] == round((T0 + timedelta(minutes=55)).timestamp())
    assert d["overworld"] == {"x": 304, "z": -440}

    out = await show({"focus": "player"})
    assert (out.view.data["focus"]["x"], out.view.data["dim"]) == (-138, "overworld")
    out = await show({"dimension": "nether"})
    assert out.view.data["center"] == {"x": 38, "z": -55}
    out = await show({"focus": "place", "label": "blaze FARM"})
    f = out.view.data["focus"]
    assert (f["label"], f["x"], f["y"], f["z"], out.view.data["dim"]) == (
        "Blaze farm",
        20,
        72,
        -70,
        "nether",
    )
