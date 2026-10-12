"""Migration 0231 against real Postgres: the Tricorder targets the map draws.

The contract, case by case:
- a target is one row per player per world, replaced by the next and dropped by a clear;
- the track drain drops it, and clears the player's Tricorder in game, once a position
  sample taken after it was set reaches it — within the arrival reach across, and down
  at its depth when it has one; a sample from before it was set never counts;
- the owner reads every world's targets; a map share link reads only its own world's,
  and nothing once revoked; nobody else reads any, and a share writes none.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, mc_share_context, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import shares, targets, travel
from jbrain.minecraft.asker import Spot
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

TABLES = ("mc_tricorder_targets", "mc_share_links", "mc_player_track", "mc_player_explored")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
    await engine.dispose()


class Sidecar:
    """`/track` over a scripted list, and the console commands the drain sends."""

    def __init__(self) -> None:
        self.samples: list[dict[str, Any]] = []
        self.commands: list[str] = []

    def walk_to(self, x: float, y: float, z: float, *, ago: float = -1.0) -> None:
        """A sample of Steve42 at (x, y, z), `ago` seconds before now (negative: after)."""
        self.samples.append(
            {
                "id": len(self.samples) + 1,
                "at": time.time() - ago,
                "name": "Steve42",
                "xuid": "111",
                "world": "castle",
                "dim": "overworld",
                "x": x,
                "y": y,
                "z": z,
            }
        )

    async def call(self, _s: Any, _m: str, path: str, **kw: Any) -> dict[str, Any]:
        if path == "/command":
            self.commands.append(kw["json"]["command"])
            return {"lines": []}
        after = int(kw["params"]["after"])
        return {"boot_id": "b", "samples": [t for t in self.samples if t["id"] > after]}


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> Sidecar:
    fake = Sidecar()
    monkeypatch.setattr(mc, "call", fake.call)
    return fake


async def _set(maker: async_sessionmaker, spot: Spot, *, world: str = "castle") -> None:
    async with scoped_session(maker, OWNER) as s:
        await targets.set_target(s, world, "Steve42", spot, "Diamonds")


async def test_one_target_per_player_replaced_then_cleared(maker) -> None:
    book = targets.SqlTargetBook(maker, OWNER)
    await book.set("castle", "Steve42", Spot(1, None, 2, "overworld"), "Village")
    await book.set("castle", "steve42", Spot(12, -58, 30, "overworld"), "Diamonds")
    await book.set("other", "Steve42", Spot(5, None, 5, "nether"), "Fortress")
    got = await targets.for_world(maker, "castle")
    assert list(got) == ["steve42"]
    assert {k: got["steve42"][k] for k in ("dim", "x", "y", "z", "label")} == {
        "dim": "overworld",
        "x": 12,
        "y": -58,
        "z": 30,
        "label": "Diamonds",
    }
    await book.clear("castle", "STEVE42")
    assert await targets.for_world(maker, "castle") == {}
    assert list(await targets.for_world(maker, "other")) == ["steve42"]


async def test_reaching_a_target_drops_it_and_clears_the_tricorder(maker, sidecar) -> None:
    await _set(maker, Spot(100, None, 100, "overworld"))
    drain = travel.TrackDrain(maker, SimpleNamespace())
    # Near it before it was set, then on the way: neither arrives.
    sidecar.walk_to(100, 64, 100, ago=120)
    sidecar.walk_to(80, 64, 90)
    await drain.tick()
    assert "steve42" in await targets.for_world(maker, "castle")
    assert sidecar.commands == []
    # Within the reach of the block (the sample can trail where they stopped).
    sidecar.walk_to(106, 70, 97)
    await drain.tick()
    assert await targets.for_world(maker, "castle") == {}
    assert sidecar.commands == [
        "scriptevent jb:waypoint " + json.dumps({"player": "Steve42", "clear": True})
    ]


async def test_a_target_with_a_depth_needs_them_down_at_it(maker, sidecar) -> None:
    await _set(maker, Spot(0, -58, 0, "overworld"))
    drain = travel.TrackDrain(maker, SimpleNamespace())
    sidecar.walk_to(1, 64, 1)  # standing on the surface above the diamonds
    await drain.tick()
    assert "steve42" in await targets.for_world(maker, "castle")
    sidecar.walk_to(2, -55, -1)
    await drain.tick()
    assert await targets.for_world(maker, "castle") == {}


async def test_a_share_sees_its_worlds_targets_and_writes_none(maker) -> None:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    owner = SessionContext(principal_id=str(pid), principal_kind="owner")
    await _set(maker, Spot(1, None, 1, "overworld"))
    await _set(maker, Spot(9, None, 9, "overworld"), world="other")
    _, link = await shares.mint_share(
        maker, owner, slot="slot2", world="castle", xuid="111", gamertag="Steve42"
    )
    seen = await targets.for_world(maker, "castle", mc_share_context(link.id))
    assert seen["steve42"]["x"] == 1
    assert await targets.for_world(maker, "other", mc_share_context(link.id)) == {}
    assert await targets.for_world(maker, "castle", mc_share_context("")) == {}
    member = SessionContext(principal_kind="member")
    assert await targets.for_world(maker, "castle", member) == {}

    with pytest.raises(DBAPIError):
        async with scoped_session(maker, mc_share_context(link.id)) as s:
            await targets.set_target(s, "castle", "Mallory", Spot(0, None, 0, "overworld"), "x")
    async with scoped_session(maker, mc_share_context(link.id)) as s:
        await targets.clear_target(s, "castle", "Steve42")  # a no-op: the delete sees nothing
    assert "steve42" in await targets.for_world(maker, "castle")

    await shares.revoke_share(maker, owner, link.id)
    assert await targets.for_world(maker, "castle", mc_share_context(link.id)) == {}
