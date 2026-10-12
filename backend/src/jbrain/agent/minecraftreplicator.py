"""Minecraft_Dave programs a Replicator: `mc_replicator_program`
(docs/plans/MINECRAFT_BEDROCK_PLAN.md §M15; canon: Bleepbloop_City `notes/lore.md`, "The
Replicator").

In game only, and only for the player asking: they hold the Replicator and carry the 8
full stacks Dave takes as its pattern. The tool sends one `scriptevent jb:replicator` and
the box's jbrain pack does everything else — it checks the held item, the stack size and
the 512, and it only ever touches the asker's own inventory. The model picks nothing but
the target's name; the pack's console reply is the only word on whether it happened.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.minecrafttools import _guarded, fence
from jbrain.minecraft import client as mc
from jbrain.minecraft.asker import asker_for

PATTERN = 512
# What a player says for a metal is its ingot: canon's "Iron Replicator" makes iron ingots.
_INGOTS = frozenset({"iron", "gold", "copper", "netherite"})
_ITEM_ID = re.compile(r"^minecraft:[a-z0-9_.]{1,64}$")
_REPLY = re.compile(r"replicator (programmed|refused): ?(.*)$")


def item_id(raw: Any) -> str | None:
    """A vanilla item id from what the model passed ("iron", "Oak Log",
    "minecraft:diamond"), or None when it can't be one."""
    name = re.sub(r"[\s-]+", "_", str(raw or "").strip().lower())
    if ":" not in name:
        name = f"minecraft:{name}"
    if name.removeprefix("minecraft:") in _INGOTS:
        name += "_ingot"
    return name if _ITEM_ID.match(name) else None


def _refusal(code: str, detail: str, target: str) -> str:
    """The pack's refusal, as a sentence the model can pass on. Nothing changed."""
    what = fence(target)
    return {
        "offline": "They aren't on the server right now, so nothing was programmed.",
        "not_holding": (
            "They need to hold the Replicator (blank or programmed) in their hand while they"
            " ask. Nothing was taken."
        ),
        "unknown_item": f"{what} isn't an item in the game. Nothing was taken.",
        "stack": (
            f"{what} stacks to {detail}, not 64, so it can't be a pattern: only things that"
            " stack to 64 can. Nothing was taken."
        ),
        "same": f"That Replicator already makes {what}. Nothing was taken.",
        "too_few": (
            f"They carry {detail} of {what}; the pattern takes 8 full stacks ({PATTERN})."
            " Nothing was taken."
        ),
    }.get(code, f"The Replicator add-on refused it: {fence(code + ' ' + detail)}")


def build_minecraft_replicator_handlers(config: Any) -> dict[str, ToolHandler]:
    """`mc_replicator_program`, bound to the sidecar's address and token in `config`."""

    async def mc_replicator_program(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        asker = asker_for(ctx.agent_session_id)
        if asker is None:
            return ToolOutput(
                "Programming a Replicator happens in game only: the player holds it, carries"
                " 8 full stacks of what it should make, and asks with /dave.",
                result_brief="in game only",
            )
        target = item_id(arguments.get("target"))
        if target is None:
            return "target is an item's name, like iron, diamond or oak_log."
        event = {"player": asker.gamertag, "target": target}
        # json.dumps escapes every quote and control character, so the message is one
        # argument on one console line.
        try:
            got = await mc.call(
                config,
                "POST",
                "/command",
                json={"command": f"scriptevent jb:replicator {json.dumps(event)}", "wait_s": 3.0},
            )
        except HTTPException as exc:
            return f"The Minecraft server can't answer right now: {exc.detail}"
        lines = [str(line) for line in got.get("lines", [])]
        found = next((m for line in lines if (m := _REPLY.search(line))), None)
        if found is None:
            return (
                "The server took the command, but the Replicator add-on didn't answer — the"
                " box's own jbrain pack may not have loaded in this world. Nothing is known"
                " to have changed."
            )
        outcome, rest = found.groups()
        if outcome == "refused":
            code, _, detail = rest.partition(" ")
            return ToolOutput(_refusal(code, detail, target), result_brief=f"refused · {code}")
        name = rest.rsplit(" as ", 1)[-1] if " as " in rest else "Replicator"
        return ToolOutput(
            f"Done: {fence(asker.gamertag)}'s Replicator is now the {fence(name)}. Dave took"
            f" the {PATTERN} {fence(target)} as its pattern; they're gone. Placed down, it"
            " makes one about every 10 seconds from any block in a chest or hopper on top,"
            " with fuel in one beside it, into one below, while someone is nearby.",
            result_brief=name,
        )

    return {"mc_replicator_program": _guarded("mc_replicator_program", mc_replicator_program)}
