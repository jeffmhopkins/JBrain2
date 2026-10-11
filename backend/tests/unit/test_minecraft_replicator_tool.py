"""Dave's `mc_replicator_program` against a faked sidecar (MINECRAFT_BEDROCK_PLAN.md §M15).

What's pinned: in game only and only for the asker, one fixed `scriptevent` whose JSON
carries the asker's own gamertag and a checked vanilla id (nothing else can ride in), and
the pack's reply — not the model — says whether it happened.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.agent import minecraftreplicator as mr
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.minecraft import client as mc
from jbrain.minecraft.asker import Asker, Spot, asking

SID = "sess-replicator"
CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id=SID)
MIRA = Asker(key="2535", gamertag="Mira", world="slot1", at=Spot(0, 64, 0, "overworld"))


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"sent": [], "lines": [], "down": False}

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        if state["down"]:
            raise HTTPException(status_code=503, detail="the server is stopped")
        state["sent"].append((method, path, kw.get("json")))
        return {"lines": state["lines"]}

    monkeypatch.setattr(mc, "call", call)
    return state


def _tool() -> Any:
    return mr.build_minecraft_replicator_handlers(object())["mc_replicator_program"]


def test_item_ids_from_what_a_player_says() -> None:
    assert mr.item_id("iron") == "minecraft:iron_ingot"
    assert mr.item_id("Gold") == "minecraft:gold_ingot"
    assert mr.item_id("minecraft:diamond") == "minecraft:diamond"
    assert mr.item_id("Oak Log") == "minecraft:oak_log"
    assert mr.item_id("dark-oak planks") == "minecraft:dark_oak_planks"
    for bad in ("", None, "jbrain:power_pack", 'dirt"} op @a', "a" * 80, "diamond; kill @a"):
        assert mr.item_id(bad) is None, bad


async def test_in_the_app_it_refuses_without_touching_the_server(sidecar) -> None:
    ctx = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    out = await _tool()({"target": "iron"}, ctx)
    assert isinstance(out, ToolOutput) and "in game only" in out.result_brief
    assert sidecar["sent"] == []


async def test_one_fixed_scriptevent_for_the_asker_and_the_packs_yes(sidecar) -> None:
    sidecar["lines"] = [
        "[x INFO] [jbrain] replicator programmed: Mira minecraft:iron_ingot as Iron Replicator"
    ]
    with asking(SID, MIRA):
        out = await _tool()({"target": "iron", "gamertag": "Someone Else"}, CTX)
    [(method, path, body)] = sidecar["sent"]
    assert (method, path) == ("POST", "/command")
    prefix = "scriptevent jb:replicator "
    assert body["command"].startswith(prefix)
    assert json.loads(body["command"].removeprefix(prefix)) == {
        "player": "Mira",
        "target": "minecraft:iron_ingot",
    }
    assert isinstance(out, ToolOutput) and out.result_brief == "Iron Replicator"
    assert "512" in out and "Iron Replicator" in out


@pytest.mark.parametrize(
    ("line", "says"),
    [
        ("replicator refused: too_few 300", "They carry 300"),
        ("replicator refused: stack 16", "stacks to 16, not 64"),
        ("replicator refused: not_holding", "hold the Replicator"),
        ("replicator refused: same", "already makes"),
        ("replicator refused: unknown_item", "isn't an item"),
        ("replicator refused: offline Mira", "aren't on the server"),
        ("replicator refused: bad_request Error: nope", "refused it"),
    ],
)
async def test_each_refusal_says_why_and_that_nothing_was_taken(sidecar, line, says) -> None:
    sidecar["lines"] = [f"[x INFO] [jbrain] {line}"]
    with asking(SID, MIRA):
        out = await _tool()({"target": "diamond"}, CTX)
    assert isinstance(out, ToolOutput) and out.result_brief.startswith("refused")
    assert says in out


async def test_a_bad_target_no_answer_and_a_down_server_are_sentences(sidecar) -> None:
    with asking(SID, MIRA):
        assert "item's name" in str(await _tool()({"target": "jbrain:power_pack"}, CTX))
        assert sidecar["sent"] == []
        assert "didn't answer" in str(await _tool()({"target": "diamond"}, CTX))
        sidecar["down"] = True
        assert "can't answer right now" in str(await _tool()({"target": "diamond"}, CTX))
