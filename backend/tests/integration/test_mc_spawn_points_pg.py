"""Migration 0246 against real Postgres: each player's respawn point (§M8, beds).

The contract, case by case:
- the session drain turns the pack's `spawnpoint` events into rows, one per change: a line
  that repeats the latest adds nothing, and re-reading the same events adds nothing;
- `read` gives each player's current point (with when they got that place, across a bed
  read as `other` while it was broken) and the recent changes plus the one before them;
- a world reset or import clears its rows and no other world's;
- the table is the owner's alone: any other session, a map share link pinned to that very
  world included, reads nothing and writes nothing (review X3).
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

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, mc_share_context, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import sessions, shares, spawnpoints
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

TABLES = ("mc_spawn_points", "mc_player_events", "mc_share_links")
EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)
T0 = 1_791_640_000.0
DAY = 86_400.0
FOX = "2535400000000001"
NOW = datetime.fromtimestamp(T0 + 10 * DAY, UTC)


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
    """`GET /events?after=` over a scripted list."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

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

    def point(
        self,
        at: float,
        spawn: str,
        *,
        name: str = "BlockyFox",
        xuid: str = FOX,
        world: str = "castle",
        **place: Any,
    ) -> None:
        self.add("spawnpoint", at, world=world, name=name, xuid=xuid, spawn=spawn, **place)

    async def call(self, _settings: Any, _m: str, path: str, **kw: Any) -> dict[str, Any]:
        if path != "/events":
            raise HTTPException(status_code=404, detail="no")
        after = int(kw["params"]["after"])
        return {"boot_id": "b", "events": [e for e in self.events if e["id"] > after]}


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> FakeSidecar:
    fake = FakeSidecar()
    monkeypatch.setattr(mc, "call", fake.call)
    return fake


BED = {"dim": "overworld", "x": 70, "y": 64, "z": 40}
ANCHOR = {"dim": "nether", "x": -20, "y": 71, "z": 40}


async def _drain(maker: async_sessionmaker) -> None:
    await sessions.SessionDrain(maker, SimpleNamespace()).tick()


async def _count(maker: async_sessionmaker, ctx: SessionContext = OWNER) -> int:
    async with scoped_session(maker, ctx) as s:
        return (await s.execute(text("SELECT count(*) FROM app.mc_spawn_points"))).scalar_one()


async def test_one_row_per_change_and_the_current_point_with_when_it_was_set(
    maker, sidecar
) -> None:
    sidecar.point(T0, "none")
    sidecar.point(T0 + DAY, "bed", **BED)  # slept
    sidecar.point(T0 + DAY + 10, "bed", **BED)  # a join's resend: no change
    sidecar.point(T0 + 9 * DAY, "other", **BED)  # broken under the remembered point
    sidecar.point(T0 + 9 * DAY + 60, "bed", **BED)  # put back
    sidecar.point(T0 + DAY, "anchor", name="Mira_P", xuid="m", **ANCHOR)
    sidecar.point(T0, "none", name="Steve42", xuid="")  # no xuid: keyed by name
    sidecar.point(T0, "sofa", name="Steve42", xuid="", **BED)  # not a kind
    sidecar.point(T0, "bed", name="Steve42", xuid="", dim="the_moon", x=1, y=2, z=3)
    sidecar.point(T0, "bed", name="Steve42", xuid="", dim="overworld", x=1)  # not whole
    sidecar.point(T0, "bed", name="", xuid="", **BED)  # nobody's
    await _drain(maker)
    assert await _count(maker) == 6
    await _drain(maker)  # a fresh drain (an api restart) reads them all again
    assert await _count(maker) == 6

    got = await spawnpoints.for_world(maker, "castle", now=NOW)
    by = {p["gamertag"]: p for p in got["spawns"]}
    assert [p["gamertag"] for p in got["spawns"]] == ["BlockyFox", "Mira_P", "Steve42"]
    fox = by["BlockyFox"]
    assert (fox["kind"], fox["dim"], fox["x"], fox["z"], fox["xuid"]) == (
        "bed",
        "overworld",
        70,
        40,
        FOX,
    )
    # Set when they first slept there: the broken spell didn't move the point.
    assert (fox["set_at"], fox["at"]) == (T0 + DAY, T0 + 9 * DAY + 60)
    assert (by["Mira_P"]["kind"], by["Mira_P"]["dim"]) == ("anchor", "nether")
    steve = by["Steve42"]
    assert (steve["xuid"], steve["kind"], steve["dim"], steve["x"]) == (
        "name:Steve42",
        "none",
        None,
        None,
    )
    # The last eight days, and the point in effect before them, per player.
    assert [(r["at"], r["kind"]) for r in got["spawn_log"] if r["xuid"] == FOX] == [
        (T0 + DAY, "bed"),
        (T0 + 9 * DAY, "other"),
        (T0 + 9 * DAY + 60, "bed"),
    ]
    assert [r["kind"] for r in got["spawn_log"] if r["xuid"] == "m"] == ["anchor"]

    async with scoped_session(maker, OWNER) as s:
        mine = await spawnpoints.current(s, "castle", FOX)
        mira = await spawnpoints.current_by_name(s, "castle", "mira_p")
        nobody = await spawnpoints.current_by_name(s, "castle", "Nobody")
    assert (mine.kind, mine.x) == ("bed", 70)
    assert (mira.gamertag, mira.kind, mira.dim) == ("Mira_P", "anchor", "nether")
    assert nobody is None


async def test_a_replaced_world_loses_its_spawn_points(maker, sidecar) -> None:
    sidecar.point(T0, "bed", **BED)
    sidecar.point(T0, "bed", world="other", **BED)
    sidecar.add("world_replaced", T0 + 10)
    sidecar.point(T0 + 20, "none")  # the new world's first
    await _drain(maker)
    got = await spawnpoints.for_world(maker, "castle", now=NOW)
    assert [(p["kind"], p["set_at"]) for p in got["spawns"]] == [("none", T0 + 20)]
    assert len((await spawnpoints.for_world(maker, "other", now=NOW))["spawns"]) == 1


async def _share_link(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    owner = SessionContext(principal_id=str(pid), principal_kind="owner")
    _, link = await shares.mint_share(
        maker, owner, slot="slot2", world="castle", xuid=FOX, gamertag="BlockyFox"
    )
    return str(link.id)


@pytest.mark.parametrize("who", ["EVERY_SCOPE", "UNSCOPED", "SHARE"])
async def test_a_non_owner_sees_nothing_and_cannot_write(maker, sidecar, who) -> None:
    sidecar.point(T0, "bed", **BED)
    await _drain(maker)
    assert await _count(maker) == 1
    ctx = (
        mc_share_context(await _share_link(maker))
        if who == "SHARE"
        else {"EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[who]
    )
    assert await _count(maker, ctx) == 0
    assert await spawnpoints.for_world(maker, "castle", ctx, NOW) == {
        "spawns": [],
        "spawn_log": [],
    }
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, ctx) as s:
            await s.execute(
                text(
                    "INSERT INTO app.mc_spawn_points (world, xuid, gamertag, boot_id,"
                    " event_id, at, kind) VALUES ('castle', 'x', 'x', 'b', 99, now(), 'none')"
                )
            )
    async with scoped_session(maker, ctx) as s:
        await s.execute(text("DELETE FROM app.mc_spawn_points"))  # sees nothing to remove
        await s.execute(text("UPDATE app.mc_spawn_points SET kind = 'none'"))
    assert await _count(maker) == 1
    async with scoped_session(maker, OWNER) as s:
        kind = (await s.execute(text("SELECT kind FROM app.mc_spawn_points"))).scalar_one()
    assert kind == "bed"


async def test_the_table_refuses_a_place_that_doesnt_fit_its_kind(maker) -> None:
    bad = [
        "('w', 'k', 'g', 'b', 1, now(), 'none', 'overworld', 1, 2, 3)",
        "('w', 'k', 'g', 'b', 2, now(), 'bed', NULL, NULL, NULL, NULL)",
        "('w', 'k', 'g', 'b', 3, now(), 'bed', 'overworld', 1, NULL, 3)",
        "('w', 'k', 'g', 'b', 4, now(), 'sofa', 'overworld', 1, 2, 3)",
        "('w', 'k', 'g', 'b', 5, now(), 'bed', 'the_moon', 1, 2, 3)",
    ]
    for row in bad:
        with pytest.raises(DBAPIError):
            async with scoped_session(maker, OWNER) as s:
                await s.execute(
                    text(
                        "INSERT INTO app.mc_spawn_points (world, xuid, gamertag, boot_id,"
                        f" event_id, at, kind, dim, x, y, z) VALUES {row}"
                    )
                )


async def test_unknown_keeps_a_known_points_kind_and_a_new_one_is_other(maker, sidecar) -> None:
    """The pack couldn't read the blocks (a join far from home): the same point as the
    player's latest row is no change; a new point, or a first one, is stored as `other`;
    `unknown` itself is never stored or served."""
    sidecar.point(T0, "bed", **BED)
    sidecar.point(T0 + 10, "unknown", **BED)  # rejoined far from home: still one row
    await _drain(maker)
    got = await spawnpoints.for_world(maker, "castle", now=NOW)
    assert [(p["kind"], p["at"]) for p in got["spawns"]] == [("bed", T0)]
    assert await _count(maker) == 1

    sidecar.point(T0 + 20, "unknown", dim="overworld", x=500, y=70, z=500)  # a new point
    sidecar.point(T0 + 30, "unknown", dim="overworld", x=500, y=70, z=500)  # and again
    sidecar.point(T0, "unknown", name="Mira_P", xuid="m", **ANCHOR)  # nothing before it
    await _drain(maker)
    got = await spawnpoints.for_world(maker, "castle", now=NOW)
    by = {p["gamertag"]: p for p in got["spawns"]}
    assert (by["BlockyFox"]["kind"], by["BlockyFox"]["x"]) == ("other", 500)
    assert (by["Mira_P"]["kind"], by["Mira_P"]["dim"]) == ("other", "nether")
    assert await _count(maker) == 3
    assert all(r["kind"] != "unknown" for r in got["spawn_log"])
    async with scoped_session(maker, OWNER) as s:
        kinds = (await s.execute(text("SELECT DISTINCT kind FROM app.mc_spawn_points"))).all()
        mine = await spawnpoints.current(s, "castle", FOX)
    assert {k.kind for k in kinds} == {"bed", "other"}
    assert mine.kind == "other"
