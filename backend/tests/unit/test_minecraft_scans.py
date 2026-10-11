"""The deep-scan limit (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M17) through the real tool
handlers, with the sidecar faked and the ledger in memory.

The contract: in game, two deep scans (`mc_locate`, `mc_nearby`, `mc_find_block`) per
player per Minecraft day, then the canon recharge line and no search; a new day (the
world's own counter) brings two more; the same search from about the same place is free;
the owner's PWA chats are unlimited; nothing is shared between players or worlds; a search
that didn't run costs nothing; and an unreadable day refuses a server search but counts an
offline block search against the world's last known day.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.agent import minecrafttools as mt
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.minecraft import client as mc
from jbrain.minecraft import scans
from jbrain.minecraft.asker import Asker, Spot, asking

OWNER = SessionContext(principal_kind="owner")
PWA = ToolContext(session=OWNER, scopes=(), agent_session_id="pwa")
DOOR = ToolContext(session=OWNER, scopes=(), agent_session_id="door")
STEVE = Asker(key="111", gamertag="Steve 42", world="slot2", at=Spot(0, 64, 0, "overworld"))
MIRA = Asker(key="222", gamertag="Mira_P", world="slot2", at=Spot(0, 64, 0, "overworld"))
DAY = "time query day"
NEARBY = {"nearby": [{"structure": "village", "x": 200, "z": 152, "distance": 251}]}
FOUND = {
    "searched": ["minecraft:diamond_ore"],
    "radius": 2048,
    "found": [{"block": "minecraft:diamond_ore", "x": 12, "y": -58, "z": 18, "distance": 22}],
}


class MemoryBook:
    """`ScanBook` in a dict, with the SQL book's semantics: a write drops the world's
    other days, and a reservation past the limit spends nothing."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.writes = 0

    async def last_day(self, world: str) -> int | None:
        mine = [(r["at"], k[2]) for k, r in self.rows.items() if k[0] == world]
        return max(mine)[1] if mine else None

    async def today(self, world: str, xuid: str, day: int) -> tuple[int, list[dict[str, Any]]]:
        row = self.rows.get((world, xuid, day))
        return (0, []) if row is None else (row["used"], list(row["results"]))

    async def reserve(self, world: str, xuid: str, gamertag: str, day: int) -> int | None:
        for key in [k for k in self.rows if k[0] == world and k[2] != day]:
            del self.rows[key]
        self.writes += 1
        row = self.rows.setdefault(
            (world, xuid, day), {"used": 0, "results": [], "at": self.writes}
        )
        if row["used"] >= scans.SCANS_PER_DAY:
            return None
        row["used"] += 1
        row["at"] = self.writes
        return row["used"]

    async def refund(self, world: str, xuid: str, day: int) -> None:
        row = self.rows[(world, xuid, day)]
        row["used"] = max(row["used"] - 1, 0)

    async def remember(self, world: str, xuid: str, day: int, result: dict[str, Any]) -> None:
        self.rows[(world, xuid, day)]["results"].append(result)

    def used(self, asker: Asker, day: int) -> int:
        row = self.rows.get((asker.world, asker.key, day))
        return 0 if row is None else row["used"]


@pytest.fixture
def book() -> MemoryBook:
    return MemoryBook()


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "sent": [],
        "replies": {
            DAY: ["[x INFO] Day is 7"],
            "/worlds": {"active": "slot2", "slots": [{"id": "slot2", "active": True}]},
            "/map/nearby": NEARBY,
            "/map/find": FOUND,
        },
        "down": set(),
    }

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        what = kw["json"]["command"] if path == "/command" else path
        if what in state["down"]:
            raise HTTPException(status_code=503, detail="the server is stopped")
        state["sent"].append(what)
        if path == "/command":
            return {"lines": state["replies"].get(what, ["[x INFO] ok"])}
        return state["replies"].get(path, {})

    monkeypatch.setattr(mc, "call", call)
    return state


def _tools(book: MemoryBook) -> dict[str, Any]:
    return mt.build_minecraft_handlers(
        object(),  # type: ignore[arg-type]
        object(),
        None,
        scan_book=lambda _ctx: book,
    )


def searches(sidecar: dict[str, Any]) -> list[str]:
    return [s for s in sidecar["sent"] if s in ("/map/nearby", "/map/find") or "locate" in s]


async def ask(tools: dict[str, Any], who: Asker, tool: str, args: dict[str, Any]) -> str:
    with asking(DOOR.agent_session_id or "", who):
        return await tools[tool](args, DOOR)


def at(asker: Asker, x: int, z: int) -> Asker:
    return replace(asker, at=Spot(x, 64, z, "overworld"))


async def test_two_scans_then_the_recharge_line_and_no_search(book, sidecar) -> None:
    tools = _tools(book)
    first = await ask(tools, STEVE, "mc_nearby", {})
    assert "village at (200, 152)" in first and "1 scan left today" in first
    second = await ask(tools, at(STEVE, 1000, 0), "mc_find_block", {"blocks": ["diamond_ore"]})
    assert "diamond ore at x=12" in second and "0 scans left today" in second
    assert len(searches(sidecar)) == 2
    for tool, args in (
        ("mc_nearby", {}),
        ("mc_find_block", {"blocks": ["gold_ore"]}),
        ("mc_locate", {"kind": "structure", "id": "mansion"}),
    ):
        out = await ask(tools, at(STEVE, 5000, 5000), tool, args)
        assert isinstance(out, ToolOutput) and out.result_brief == "out of scans"
        assert scans.RECHARGE in out and "nothing was searched" in out
    assert len(searches(sidecar)) == 2  # refused before any search
    assert book.used(STEVE, 7) == 2


async def test_a_new_day_brings_two_more_and_forgets_yesterday(book, sidecar) -> None:
    tools = _tools(book)
    await ask(tools, STEVE, "mc_nearby", {})
    await ask(tools, at(STEVE, 1000, 0), "mc_nearby", {})
    assert scans.RECHARGE in await ask(tools, at(STEVE, 2000, 0), "mc_nearby", {})
    sidecar["replies"][DAY] = ["[x INFO] Day is 8"]  # they slept
    out = await ask(tools, STEVE, "mc_nearby", {})
    assert "1 scan left today" in out and "earlier today" not in out  # yesterday's is gone
    assert {k[2] for k in book.rows} == {8}  # old days pruned on write


async def test_a_repeat_from_about_the_same_place_is_free(book, sidecar) -> None:
    tools = _tools(book)
    await ask(tools, STEVE, "mc_find_block", {"blocks": ["diamond_ore", "gold_ore"]})
    sent = len(searches(sidecar))
    # Same block set in another order, a short walk away: answered from the first scan.
    again = await ask(
        tools, at(STEVE, 150, -150), "mc_find_block", {"blocks": ["gold_ore", "diamond_ore"]}
    )
    assert isinstance(again, ToolOutput) and again.result_brief == "repeat, free"
    assert "diamond ore at x=12" in again and "earlier today" in again
    assert "1 scan left today" in again
    assert len(searches(sidecar)) == sent and book.used(STEVE, 7) == 1
    # Not a repeat: farther than REPEAT_RADIUS, or a different search.
    far = scans.REPEAT_RADIUS + 50
    out = await ask(
        tools, at(STEVE, far, 0), "mc_find_block", {"blocks": ["diamond_ore", "gold_ore"]}
    )
    assert "0 scans left today" in out
    assert scans.RECHARGE in await ask(tools, STEVE, "mc_find_block", {"blocks": ["diamond_ore"]})
    # Out of scans, a repeat is still free.
    assert "earlier today" in await ask(
        tools, STEVE, "mc_find_block", {"blocks": ["gold_ore", "diamond_ore"]}
    )


async def test_the_owners_pwa_chats_are_unlimited(book, sidecar) -> None:
    tools = _tools(book)
    for i in range(5):
        out = await tools["mc_nearby"]({"x": i * 1000, "z": 0}, PWA)
        assert "village" in out and "left today" not in out
    assert len(searches(sidecar)) == 5
    assert DAY not in sidecar["sent"] and book.rows == {}


async def test_each_player_has_their_own_scans_in_each_world(book, sidecar) -> None:
    tools = _tools(book)
    await ask(tools, STEVE, "mc_nearby", {})
    await ask(tools, at(STEVE, 1000, 0), "mc_nearby", {})
    assert scans.RECHARGE in await ask(tools, at(STEVE, 3000, 0), "mc_nearby", {})
    # Mira asks Steve's exact search: no shared repeat, and her own two.
    out = await ask(tools, MIRA, "mc_nearby", {})
    assert "1 scan left today" in out and "earlier today" not in out
    # Steve in another world folder starts fresh there.
    other_world = replace(STEVE, world="slot3")
    assert "1 scan left today" in await ask(tools, other_world, "mc_nearby", {})


async def test_a_search_that_didnt_run_costs_nothing(book, sidecar) -> None:
    tools = _tools(book)
    locate = "execute positioned 0 64 0 run locate structure mansion"
    sidecar["replies"][locate] = ["[x INFO] Unable to find a structure of type mansion"]
    out = await ask(tools, STEVE, "mc_locate", {"kind": "structure", "id": "mansion"})
    assert "left today" not in out and book.used(STEVE, 7) == 0
    sidecar["down"].add("/map/nearby")
    out = await ask(tools, STEVE, "mc_nearby", {})
    assert "can't answer" in out and book.used(STEVE, 7) == 0
    sidecar["replies"]["/map/find"] = {"searched": [], "suggestions": ["minecraft:gold_ore"]}
    out = await ask(tools, STEVE, "mc_find_block", {"blocks": ["diamond_ore"]})
    assert "minecraft:gold_ore" in out and book.used(STEVE, 7) == 0
    sidecar["replies"][locate] = [
        "[x INFO] The nearest mansion is at block 2312, ~, -1544 (2876 blocks away)"
    ]
    out = await ask(tools, STEVE, "mc_locate", {"kind": "structure", "id": "mansion"})
    assert "2876 blocks away" in out and "1 scan left today" in out


async def test_a_failing_search_is_refunded_and_still_raises(book) -> None:
    async def boom() -> tuple[str, bool]:
        raise RuntimeError("sidecar fell over")

    search = scans.Search("mc_nearby", "r2000", "overworld", 0, 0)
    with pytest.raises(RuntimeError):
        await scans.deep_scan(book, STEVE, 7, search, boom)
    assert book.used(STEVE, 7) == 0


async def test_two_calls_at_once_cannot_both_spend_the_last_scan(book, sidecar) -> None:
    tools = _tools(book)
    await ask(tools, STEVE, "mc_nearby", {})
    with asking(DOOR.agent_session_id or "", STEVE):
        outs = await asyncio.gather(
            tools["mc_nearby"]({"x": 3000, "z": 0}, DOOR),
            tools["mc_nearby"]({"x": -3000, "z": 0}, DOOR),
        )
    assert sorted(o.result_brief.endswith("left today") for o in outs) == [False, True]
    assert sum(scans.RECHARGE in o for o in outs) == 1 and book.used(STEVE, 7) == 2


async def test_an_unreadable_day(book, sidecar) -> None:
    """`mc_nearby` asks the running server anyway, so it refuses cleanly; `mc_find_block`
    reads the world's files and counts against the world's last known day."""
    tools = _tools(book)
    sidecar["replies"][DAY] = ["[x INFO] Unknown command: time"]
    out = await ask(tools, STEVE, "mc_nearby", {})
    assert "didn't say what day" in out and searches(sidecar) == [] and book.rows == {}
    # Never seen a day in this world: its own bucket.
    out = await ask(tools, STEVE, "mc_find_block", {"blocks": ["diamond_ore"]})
    assert "1 scan left today" in out and book.used(STEVE, scans.UNKNOWN_DAY) == 1
    # Once a day has been read, an unreadable one counts against it.
    sidecar["replies"][DAY] = ["[x INFO] Day is 12"]
    await ask(tools, MIRA, "mc_find_block", {"blocks": ["gold_ore"]})
    sidecar["down"].add(DAY)  # the server is stopped: the console can't answer
    out = await ask(tools, MIRA, "mc_find_block", {"blocks": ["iron_ore"]})
    assert "0 scans left today" in out and book.used(MIRA, 12) == 2


async def test_world_info_says_how_many_scans_are_left_in_game_only(book, sidecar) -> None:
    tools = _tools(book)
    await ask(tools, STEVE, "mc_nearby", {})
    out = await ask(tools, STEVE, "mc_world_info", {})
    assert "1 scan left today (of 2)" in out
    assert "left today" not in await tools["mc_world_info"]({}, PWA)


def test_the_day_parse_and_the_count_wording() -> None:
    assert scans.parse_day(["[2026 INFO] Day is 42"]) == 42
    assert scans.parse_day(["[x] Player joined: Day is fun"]) is None
    assert scans.parse_day("The Minecraft server can't answer right now") is None
    assert scans.parse_day([]) is None
    assert [scans.scans_left(n) for n in (0, 1, 2)] == [
        "0 scans left today",
        "1 scan left today",
        "2 scans left today",
    ]
    assert scans.RECHARGE == "…the deep scan needs to recharge, crew. Ask me again tomorrow."
