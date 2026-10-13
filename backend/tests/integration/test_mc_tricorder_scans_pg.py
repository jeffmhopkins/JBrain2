"""Migration 0244 against real Postgres: the Tricorder's live scan the map and Dave show (§M25).

The contract, case by case:
- the session drain turns the pack's `scan` lines into one row per player per world —
  `set` starts it empty, `found` / `none` say what the last scan saw and when, `stop`
  drops it — and never touches the player's waypoint row under it;
- a `scans` snapshot replaces a world's set (a cut one only patches): a scan still on keeps
  what it last found, a new or changed one starts empty;
- a world reset or import clears its rows and no other world's;
- Dave's status read and the map card answer from the rows;
- the owner reads every world's; a map share link reads only its own world's, and nothing
  once revoked; anyone else reads nothing and nobody but the owner writes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import minecrafttools as mt
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, mc_share_context, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import livescan, sessions, shares, targets
from jbrain.minecraft.asker import Asker, Spot, asking
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

TABLES = ("mc_tricorder_scans", "mc_tricorder_targets", "mc_share_links", "mc_player_track")
EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)
T0 = 1_791_640_000.0
DIAMOND = ["minecraft:diamond_ore", "minecraft:deepslate_diamond_ore"]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
    await engine.dispose()


class FakeSidecar:
    """`GET /events?after=` over a scripted list, `/worlds` and `/status` for the tool."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.online: list[str] = ["BlockyFox"]

    def add(self, kind: str, at: float, world: str = "castle", **extra: Any) -> None:
        self.events.append(
            {
                "id": len(self.events) + 1,
                "at": at,
                "kind": kind,
                "name": "",
                "xuid": "",
                "world": world,
                **extra,
            }
        )

    async def call(self, _settings: Any, _m: str, path: str, **kw: Any) -> dict[str, Any]:
        if path == "/worlds":
            return {"active": "castle"}
        if path == "/status":
            return {"state": "running", "players": [{"name": n} for n in self.online]}
        if path != "/events":
            raise HTTPException(status_code=404, detail=path)
        after = int(kw["params"]["after"])
        return {"boot_id": "b", "events": [e for e in self.events if e["id"] > after]}


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> FakeSidecar:
    fake = FakeSidecar()
    monkeypatch.setattr(mc, "call", fake.call)
    return fake


def scan(op: str, player: str = "BlockyFox", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"op": op, "player": player}
    if op != "stop":
        body.update({"label": "diamond", "ids": DIAMOND})
    return {**body, **extra}


def found(x: int, y: int, z: int, player: str = "BlockyFox") -> dict[str, Any]:
    return scan("found", player, dim="overworld", x=x, y=y, z=z)


async def _drain(maker: async_sessionmaker) -> None:
    await sessions.SessionDrain(maker, SimpleNamespace()).tick()


async def test_set_found_none_and_stop_keep_one_row_and_leave_the_waypoint(maker, sidecar) -> None:
    async with scoped_session(maker, OWNER) as s:
        await targets.set_target(s, "castle", "BlockyFox", Spot(9, None, 9, "overworld"), "Farm")
    sidecar.add("scan", T0, **scan("set"))
    await _drain(maker)
    got = (await livescan.for_world(maker, "castle"))["blockyfox"]
    assert (got["state"], got["found"], got["at"], got["set_at"]) == ("set", None, None, T0)
    assert (got["gamertag"], got["label"], got["ids"]) == ("BlockyFox", "diamond", DIAMOND)

    sidecar.add("scan", T0 + 10, **found(41, -58, -79))
    sidecar.add("scan", T0 + 70, **found(41, -58, -79))  # the minute's heartbeat
    await _drain(maker)
    got = (await livescan.for_world(maker, "castle"))["blockyfox"]
    assert got["state"] == "found" and got["at"] == T0 + 70 and got["set_at"] == T0
    assert got["found"] == {"dim": "overworld", "x": 41, "y": -58, "z": -79}

    sidecar.add("scan", T0 + 80, **scan("none"))
    await _drain(maker)
    got = (await livescan.for_world(maker, "castle"))["blockyfox"]
    assert (got["state"], got["found"], got["at"]) == ("none", None, T0 + 80)
    # The waypoint under the scan is untouched, so it comes back when the scan stops.
    assert (await targets.for_world(maker, "castle"))["blockyfox"]["label"] == "Farm"

    sidecar.add("scan", T0 + 90, **scan("stop", player="blockyfox"))
    await _drain(maker)
    assert await livescan.for_world(maker, "castle") == {}
    assert (await targets.for_world(maker, "castle"))["blockyfox"]["label"] == "Farm"


async def test_a_found_heard_before_its_set_still_makes_the_row(maker, sidecar) -> None:
    sidecar.add("scan", T0, **found(1, 2, 3, player="Mira P"))
    await _drain(maker)
    got = (await livescan.for_world(maker, "castle"))["mira p"]
    assert got["state"] == "found" and got["set_at"] is None


async def test_a_snapshot_replaces_the_worlds_scans_keeping_what_still_runs(maker, sidecar) -> None:
    sidecar.add("scan", T0, **found(41, -58, -79))
    sidecar.add("scan", T0, **found(5, 60, 5, player="Mira"))
    sidecar.add("scan", T0, **found(1, 1, 1, player="Gone"))
    sidecar.add("scan", T0, world="other", **scan("set", player="Elsewhere"))
    sidecar.add(
        "scans",
        T0 + 100,
        all=[
            {"player": "BlockyFox", "label": "diamond", "ids": DIAMOND},
            {"player": "Mira", "label": "oak log", "ids": ["minecraft:oak_log"]},
            {"player": "New", "label": "iron", "ids": ["minecraft:iron_ore"]},
        ],
        truncated=False,
    )
    await _drain(maker)
    got = await livescan.for_world(maker, "castle")
    assert sorted(got) == ["blockyfox", "mira", "new"]
    # Still the same scan: what it last found stands until the first scan after the load.
    assert got["blockyfox"]["found"] == {"dim": "overworld", "x": 41, "y": -58, "z": -79}
    # Scanning for something else now: it starts empty.
    assert (got["mira"]["label"], got["mira"]["state"], got["mira"]["found"]) == (
        "oak log",
        "set",
        None,
    )
    assert got["new"]["state"] == "set"
    assert list(await livescan.for_world(maker, "other")) == ["elsewhere"]

    sidecar.add("scans", T0 + 200, all=[{"player": "New", "label": "iron"}], truncated=True)
    await _drain(maker)
    assert sorted(await livescan.for_world(maker, "castle")) == ["blockyfox", "mira", "new"]


async def test_a_replaced_world_loses_its_scans_and_no_other_worlds(maker, sidecar) -> None:
    sidecar.add("scan", T0, **found(41, -58, -79))
    sidecar.add("scan", T0, world="other", **scan("set"))
    sidecar.add("world_replaced", T0 + 10)
    sidecar.add("scan", T0 + 20, **scan("set", player="Mira"))  # the new world's first
    await _drain(maker)
    assert list(await livescan.for_world(maker, "castle")) == ["mira"]
    assert list(await livescan.for_world(maker, "other")) == ["blockyfox"]


async def test_replaying_the_same_events_ends_in_the_same_rows(maker, sidecar) -> None:
    sidecar.add("scan", T0, **scan("set"))
    sidecar.add("scan", T0 + 5, **found(41, -58, -79))
    await _drain(maker)
    first = await livescan.for_world(maker, "castle")
    async with scoped_session(maker, OWNER) as s:
        for e in sidecar.events:
            await sessions.SessionDrain.apply(s, "b", e)
    assert await livescan.for_world(maker, "castle") == first


async def _track(maker: async_sessionmaker, x: float, y: float, z: float) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_player_track"
                " (xuid, gamertag, world, boot_id, sample_id, dim, x, y, z, at)"
                " VALUES ('111', 'BlockyFox', 'castle', 'b', 1, 'overworld', :x, :y, :z, now())"
            ),
            {"x": x, "y": y, "z": z},
        )


async def test_dave_reads_the_scan_from_the_rows_the_asker_only_their_own(maker, sidecar) -> None:
    sidecar.add("scan", T0, **found(41, -58, -79))
    sidecar.add("scan", T0, **found(1, 1, 1, player="Mira"))
    await _drain(maker)
    await _track(maker, 32.0, -52.0, -70.0)
    tool = mt.build_minecraft_handlers(maker, SimpleNamespace(), None)["mc_tricorder_scan"]
    app = ToolContext(session=OWNER, scopes=())
    out = await tool({"gamertag": "BlockyFox", "status": True}, app)
    assert isinstance(out, ToolOutput) and out.result_brief == "diamond · found one"
    assert "found one 13 blocks north-east of them, at y -58: 6 blocks below them" in out
    assert "from y -64 to -36 up and down" in out
    sidecar.online = []
    out = await tool({"gamertag": "BlockyFox", "status": True}, app)
    assert isinstance(out, ToolOutput) and out.result_brief == "diamond · offline"

    asker = Asker("111", "BlockyFox", "castle", Spot(32, -52, -70, "overworld"))
    door = ToolContext(session=OWNER, scopes=(), agent_session_id="door-1")
    with asking("door-1", asker):
        other = await tool({"gamertag": "Mira", "status": True}, door)
        mine = await tool({"status": True}, door)
    assert isinstance(other, ToolOutput) and other.result_brief == "refused"
    assert isinstance(mine, ToolOutput) and "6 blocks below them" in mine


async def test_a_share_sees_its_worlds_scans_and_writes_none(maker, sidecar) -> None:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    owner = SessionContext(principal_id=str(pid), principal_kind="owner")
    sidecar.add("scan", T0, **found(41, -58, -79))
    sidecar.add("scan", T0, world="other", **scan("set"))
    await _drain(maker)
    _, link = await shares.mint_share(
        maker, owner, slot="slot2", world="castle", xuid="111", gamertag="BlockyFox"
    )
    share = mc_share_context(link.id)
    assert (await livescan.for_world(maker, "castle", share))["blockyfox"]["found"]["x"] == 41
    assert await livescan.for_world(maker, "other", share) == {}
    assert await livescan.for_world(maker, "castle", mc_share_context("")) == {}
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, share) as s:
            await livescan.apply(
                s, {"kind": "scan", "world": "castle", **scan("set", player="Mallory")}, _dt()
            )
    async with scoped_session(maker, share) as s:
        await livescan.apply(s, {"kind": "scan", "world": "castle", **scan("stop")}, _dt())
    assert "blockyfox" in await livescan.for_world(maker, "castle")
    await shares.revoke_share(maker, owner, link.id)
    assert await livescan.for_world(maker, "castle", share) == {}


def _dt() -> datetime:
    return datetime.fromtimestamp(T0 + 1000, UTC)


@pytest.mark.parametrize("ctx_name", ["EVERY_SCOPE", "UNSCOPED", "MEMBER"])
async def test_a_non_owner_sees_nothing_and_cannot_write(maker, sidecar, ctx_name) -> None:
    ctx = {
        "EVERY_SCOPE": EVERY_SCOPE,
        "UNSCOPED": UNSCOPED,
        "MEMBER": SessionContext(principal_kind="member"),
    }[ctx_name]
    sidecar.add("scan", T0, **found(41, -58, -79))
    await _drain(maker)
    async with scoped_session(maker, ctx) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.mc_tricorder_scans"))).scalar() == 0
    assert await livescan.for_world(maker, "castle", ctx) == {}
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, ctx) as s:
            await s.execute(
                text(
                    "INSERT INTO app.mc_tricorder_scans (world, player, gamertag, label, state)"
                    " VALUES ('w', 'x', 'x', 'stone', 'set')"
                )
            )
    async with scoped_session(maker, ctx) as s:
        await s.execute(text("DELETE FROM app.mc_tricorder_scans"))  # sees nothing
    assert "blockyfox" in await livescan.for_world(maker, "castle")
