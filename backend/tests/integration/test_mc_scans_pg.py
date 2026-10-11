"""Migration 0229 against real Postgres: the deep-scan limit's ledger (`app.mc_scans`).

The contract (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M17):
- in game, two deep scans per player per world per Minecraft day, then refused without a
  search, and the count survives (it is a table, not process memory);
- a repeat answers from the stored result; a new day replaces the world's old ones;
- the SQL reservation never lets a third scan through, however the calls interleave;
- the debug summary is counts only;
- nobody but the owner can read or write `mc_scans`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import minecrafttools as mt
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import scans
from jbrain.minecraft.asker import Asker, Spot, asking
from tests.conftest import docker_available
from tests.integration.test_rls import UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

OWNER = SessionContext(principal_kind="owner")
EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)
DOOR = ToolContext(session=OWNER, scopes=(), agent_session_id="door")
STEVE = Asker(key="111", gamertag="Steve 42", world="slot2", at=Spot(0, 64, 0, "overworld"))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        await s.execute(text("DELETE FROM app.mc_scans"))
    await engine.dispose()


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"day": "Day is 7", "searches": 0}

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        if path == "/command":
            return {"lines": [f"[x INFO] {state['day']}"]}
        if path == "/map/nearby":
            state["searches"] += 1
            return {"nearby": [{"structure": "village", "x": 200, "z": 152, "distance": 251}]}
        return {}

    monkeypatch.setattr(mc, "call", call)
    return state


async def _nearby(maker: async_sessionmaker, who: Asker, x: int = 0) -> str:
    tools = mt.build_minecraft_handlers(maker, object(), None)
    with asking("door", who):
        return await tools["mc_nearby"]({"x": x, "z": 0}, DOOR)


async def test_the_limit_holds_in_the_table(maker, sidecar) -> None:
    assert "1 scan left today" in await _nearby(maker, STEVE)
    assert "earlier today" in await _nearby(maker, STEVE, x=100)  # a repeat, from the row
    assert "0 scans left today" in await _nearby(maker, STEVE, x=1000)
    assert scans.RECHARGE in await _nearby(maker, STEVE, x=5000)
    assert sidecar["searches"] == 2
    book = scans.SqlScanBook(maker, OWNER)
    used, results = await book.today("slot2", "111", 7)
    assert used == 2 and [(r["tool"], r["x"]) for r in results] == [
        ("mc_nearby", 0),
        ("mc_nearby", 1000),
    ]
    assert await book.last_day("slot2") == 7 and await book.last_day("slot9") is None
    # Another player, and the same player in another world, have their own.
    assert "1 scan left today" in await _nearby(maker, Asker("222", "Mira_P", "slot2", STEVE.at))
    other = Asker("111", "Steve 42", "slot3", STEVE.at)
    assert "1 scan left today" in await _nearby(maker, other)
    # They slept: day 8 brings two more and the world's day 7 rows are pruned on write.
    sidecar["day"] = "Day is 8"
    assert "1 scan left today" in await _nearby(maker, STEVE, x=5000)
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(text("SELECT world, xuid, mc_day, used FROM app.mc_scans ORDER BY 1"))
        ).all()
    assert [tuple(r) for r in rows] == [("slot2", "111", 8, 1), ("slot3", "111", 7, 1)]


async def test_the_reservation_never_spends_a_third_scan(maker) -> None:
    book = scans.SqlScanBook(maker, OWNER)
    got = await asyncio.gather(*(book.reserve("slot2", "111", "Steve 42", 7) for _ in range(5)))
    assert sorted(g for g in got if g is not None) == [1, 2] and got.count(None) == 3
    await book.refund("slot2", "111", 7)
    assert (await book.today("slot2", "111", 7))[0] == 1
    await book.remember("slot2", "111", 7, {"tool": "mc_nearby", "text": 'a "quoted" find'})
    assert (await book.today("slot2", "111", 7))[1][0]["text"] == 'a "quoted" find'


async def test_the_debug_summary_is_counts_only(maker) -> None:
    book = scans.SqlScanBook(maker, OWNER)
    await book.reserve("slot2", "111", "Steve 42", 7)
    await book.remember("slot2", "111", 7, {"tool": "mc_nearby", "text": "village at 1, 2"})
    [row] = await scans.summary(maker)
    assert {k: row[k] for k in ("world", "xuid", "gamertag", "day", "used", "left")} == {
        "world": "slot2",
        "xuid": "111",
        "gamertag": "Steve 42",
        "day": 7,
        "used": 1,
        "left": 1,
    }
    assert row["stored_results"] == 1 and "village" not in str(row)


@pytest.mark.parametrize("ctx_name", ["EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_sees_no_scans_and_cannot_write_one(maker, ctx_name) -> None:
    await scans.SqlScanBook(maker, OWNER).reserve("slot2", "111", "Steve 42", 7)
    other = {"EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]
    async with scoped_session(maker, other) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.mc_scans"))).scalar() == 0
        # An update or delete a non-owner sends reaches no row.
        await s.execute(text("UPDATE app.mc_scans SET used = 0"))
        await s.execute(text("DELETE FROM app.mc_scans"))
    assert (await scans.SqlScanBook(maker, OWNER).today("slot2", "111", 7))[0] == 1
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, other) as s:
            await s.execute(
                text(
                    "INSERT INTO app.mc_scans (world, xuid, gamertag, mc_day, used)"
                    " VALUES ('w', 'x', 'x', 1, 0)"
                )
            )
