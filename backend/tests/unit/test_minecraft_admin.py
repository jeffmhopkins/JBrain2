"""Minecraft_Dave's reach over the whole server (agent/minecraftadmin.py).

Pinned: a console command that only looks runs at once and anything else is staged, never
sent; every server action is staged with the exact sidecar call, built from input checked
by the owner API's own models; an approved leaf runs only a route on the allowed list, and
a server refusal holds the leaf instead of reporting it done.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.agent import minecraftadmin as ma
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.proposals import LeafRefused
from jbrain.db.session import SessionContext
from jbrain.minecraft import client as mc

CTX = ToolContext(
    session=SessionContext(principal_kind="owner", principal_id="owner-1"),
    scopes=(),
    agent_session_id="chat-1",
)


class FakeProposals:
    def __init__(self) -> None:
        self.staged: list[Any] = []

    async def stage(self, ctx: SessionContext, *, principal_id: str, spec: Any) -> str:
        self.staged.append(spec)
        return f"prop-{len(self.staged)}"


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"sent": [], "refuse": None}

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        if state["refuse"]:
            raise HTTPException(status_code=409, detail=state["refuse"])
        state["sent"].append((method, path, kw.get("json"), kw.get("params")))
        if path == "/command":
            return {"lines": ["[x INFO] There are 1/10 players online: Steve42"]}
        if path == "/logs":
            return {"lines": [{"text": "[x INFO] Player connected: Steve42"}]}
        return {"ok": True}

    monkeypatch.setattr(mc, "call", call)
    return state


@pytest.mark.parametrize(
    ("command", "read"),
    [
        ("list", True),
        ("/time query daytime", True),
        ("weather query", True),
        ("gamerule", True),
        ("gamerule keepInventory", True),
        ("gamerule keepInventory true", False),
        ("scoreboard objectives list", True),
        ("querytarget Steve42", True),
        ("time set day", False),
        ("give Steve42 diamond 64", False),
        ("execute as @a run kill @s", False),  # `execute` can run anything
        ("op Steve42", False),
        ("", False),
    ],
)
def test_only_commands_that_look_count_as_reads(command: str, read: bool) -> None:
    assert ma.is_read_only(command) is read


async def test_a_read_runs_and_a_change_is_staged_not_sent(sidecar) -> None:
    proposals = FakeProposals()
    tools = ma.build_minecraft_admin_handlers(object(), proposals)  # type: ignore[arg-type]
    out = await tools["mc_command"]({"command": "list"}, CTX)
    assert "Steve42" in str(out) and "untrusted_external_data" in str(out)  # fenced
    assert sidecar["sent"][-1][1] == "/command"

    sent = len(sidecar["sent"])
    out = await tools["mc_command"]({"command": "/give Steve42 diamond 3"}, CTX)
    assert len(sidecar["sent"]) == sent  # nothing reached the server
    spec = proposals.staged[-1]
    assert spec.kind == "minecraft" and spec.session_id == "chat-1"
    assert spec.nodes[0].op == ma.CONSOLE_OP
    assert spec.nodes[0].preview == {"command": "give Steve42 diamond 3"}
    assert isinstance(out, ToolOutput) and out.proposal is not None and "approv" in str(out)

    for bad in ("stop", "save hold", "say hi\nop Steve42", "x" * 500):
        assert "proposal" not in str(await tools["mc_command"]({"command": bad}, CTX))
    assert len(proposals.staged) == 1


async def test_every_server_action_is_staged_with_its_exact_call(sidecar) -> None:
    proposals = FakeProposals()
    tools = ma.build_minecraft_admin_handlers(object(), proposals)  # type: ignore[arg-type]
    cases = [
        ({"action": "restart"}, "/server/restart", None),
        (
            {"action": "backup", "label": "before boss"},
            "/snapshot",
            {"label": "before boss", "slot": None},
        ),
        ({"action": "load_world", "slot": "slot2"}, "/worlds/slot2/load", None),
        (
            {"action": "reset_world", "slot": "slot3", "mode": "new_seed", "seed": "glacier"},
            "/worlds/slot3/reset",
            {"mode": "new_seed", "seed": "glacier"},
        ),
        (
            {"action": "set_rules", "slot": "slot1", "rules": {"keepInventory": True}},
            "/worlds/slot1/rules",
            {"set": {"keepInventory": True}},
        ),
        (
            {"action": "restore_backup", "backup": "world-1.mcworld", "slot": "slot2"},
            "/snapshots/world-1.mcworld/restore",
            {"slot": "slot2"},
        ),
        ({"action": "allowlist", "add": "Mira_P"}, "/allowlist", {"add": "Mira_P"}),
        (
            {"action": "set_properties", "properties": {"difficulty": "hard"}},
            "/properties",
            {"set": {"difficulty": "hard"}},
        ),
        ({"action": "update_server"}, "/update", None),
    ]
    for args, path, body in cases:
        await tools["mc_server_action"](args, CTX)
        preview = proposals.staged[-1].nodes[0].preview
        assert (preview["path"], preview["body"]) == (path, body), args
    assert sidecar["sent"] == []  # staging never touches the server

    refused = [
        {"action": "load_world"},  # no slot
        {"action": "load_world", "slot": "../etc"},
        {"action": "reset_world", "slot": "slot2", "mode": "wipe"},
        {"action": "restore_backup", "backup": "../../x", "slot": "slot2"},
        {"action": "set_properties", "properties": {"motd": "a\nb"}},
        {"action": "set_properties", "properties": {"Bad Key": "1"}},
        {"action": "allowlist"},
        {"action": "format_disk"},
    ]
    staged = len(proposals.staged)
    for args in refused:
        out = await tools["mc_server_action"](args, CTX)
        assert isinstance(out, str), args
    assert len(proposals.staged) == staged


async def test_reads_are_fenced(sidecar) -> None:
    tools = ma.build_minecraft_admin_handlers(object(), FakeProposals())  # type: ignore[arg-type]
    out = await tools["mc_server_log"]({"tail": 20}, CTX)
    assert "Steve42" in str(out) and "untrusted_external_data" in str(out)
    assert sidecar["sent"][-1][3] == {"tail": 20}
    await tools["mc_worlds"]({"slot": "slot2"}, CTX)
    assert sidecar["sent"][-1][1] == "/worlds/slot2/rules"
    assert "slot2" in await tools["mc_worlds"]({"slot": "../x"}, CTX)


async def test_an_approved_leaf_runs_only_an_allowed_route(sidecar) -> None:
    execute = ma.minecraft_executor(object())
    proposal = SimpleNamespace(id="p")

    def leaf(op: str, preview: dict[str, Any]) -> Any:
        return SimpleNamespace(op=op, preview=preview)

    await execute(CTX.session, proposal, leaf(ma.CONSOLE_OP, {"command": "time set day"}))  # type: ignore[arg-type]
    assert sidecar["sent"][-1][1:3] == ("/command", {"command": "time set day", "wait_s": 3.0})
    await execute(
        CTX.session,
        proposal,  # type: ignore[arg-type]
        leaf(ma.ACTION_OP, {"path": "/worlds/slot2/load", "body": None}),  # type: ignore[arg-type]
    )
    assert sidecar["sent"][-1][1] == "/worlds/slot2/load"
    for bad in ("/command", "/probe-pack", "/worlds/slot2/import", "/snapshots/x/file"):
        with pytest.raises(LeafRefused):
            await execute(CTX.session, proposal, leaf(ma.ACTION_OP, {"path": bad}))  # type: ignore[arg-type]
    sidecar["refuse"] = "busy: updating"
    with pytest.raises(LeafRefused, match="busy"):
        await execute(CTX.session, proposal, leaf(ma.ACTION_OP, {"path": "/update"}))  # type: ignore[arg-type]
