"""Minecraft_Dave's whole reach over the server (MINECRAFT_BEDROCK_PLAN §P1, owner request
2026-10-11: "expose ALL raw server tools to Dave, so he has everything needed").

Everything the server can do is here, split by what it can break:

- **Reads run at once**: the console log, the worlds and their rules, the backups, the
  server's settings and allowlist, and any console command that only looks.
- **Everything else stages a Proposal** the owner approves in the chat: any other console
  command, start/stop/restart, a backup, loading/creating/resetting/restoring a world, rule
  and setting changes, the allowlist, the server update. Dave plays alongside players
  whose chat reaches him (fenced, but text all the same), so nothing that changes the
  server runs on his word alone.

A staged action carries the exact sidecar call — method, path, body — built here from
input validated with the owner API's own models, so an approved card does precisely what
the PWA's button would. The executor re-checks the path against the routes Dave may use
before calling it, since a preview is stored data, not code.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from jbrain.agent.contracts import ProposalRef
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.minecrafttools import _reply, fence
from jbrain.agent.proposals import (
    LeafExecutor,
    LeafRefused,
    NodeRow,
    NodeSpec,
    ProposalRepo,
    ProposalRow,
    ProposalSpec,
)
from jbrain.api import minecraft as owner_api
from jbrain.db.session import SessionContext
from jbrain.minecraft import client as mc

PROPOSAL_KIND = "minecraft"
CONSOLE_OP = "mc_console"
ACTION_OP = "mc_action"
MINECRAFT_OPS = frozenset({CONSOLE_OP, ACTION_OP})
COMMAND_MAX = 400
_M = TypeVar("_M", bound=BaseModel)

# Console commands that only look. Anything else — `execute` included, since it can run
# any command — waits for the owner.
_READ_VERBS = frozenset(
    {"list", "querytarget", "locate", "testfor", "testforblock", "testforblocks", "help"}
)
_READ_PHRASES = (
    re.compile(r"^time query \w+$"),
    re.compile(r"^weather query$"),
    re.compile(r"^gamerule( \w+)?$"),  # bare lists every rule; one name reads it
    re.compile(r"^scoreboard (objectives|players) list\b[^;]*$"),
    re.compile(r"^tickingarea list\b[^;]*$"),
    re.compile(r"^allowlist list$"),
)

# The sidecar routes an approved action may call; checked again at enact.
_ACTION_PATHS = re.compile(
    r"^/(server/(start|stop|restart)|snapshot|update|allowlist|properties|settings"
    r"|worlds/slot\d+/(load|create|update|reset|rules)"
    r"|snapshots/[A-Za-z0-9_.-]+\.mcworld/(pin|delete|restore))$"
)
_SLOT = re.compile(owner_api._SLOT)
_BACKUP = re.compile(owner_api._BACKUP)
_PROPERTY_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


def is_read_only(command: str) -> bool:
    words = command.strip().lstrip("/").split()
    if not words:
        return False
    if words[0].lower() in _READ_VERBS:
        return True
    return any(p.fullmatch(" ".join(words).lower()) for p in _READ_PHRASES)


class _Bad(ValueError):
    """Input the model can fix: the message goes back to it as the tool result."""


def _model(cls: type[_M], data: dict[str, Any]) -> _M:
    try:
        return cls.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "input"
        raise _Bad(f"{where}: {first.get('msg', 'invalid')}") from None


def _slot(args: dict[str, Any]) -> str:
    slot = str(args.get("slot") or "")
    if not _SLOT.fullmatch(slot):
        raise _Bad("this action needs `slot`, e.g. slot2 (see mc_worlds).")
    return slot


def _backup(args: dict[str, Any]) -> str:
    name = str(args.get("backup") or "")
    if not _BACKUP.fullmatch(name):
        raise _Bad("this action needs `backup`, a backup's file name from mc_backups.")
    return name


def plan_action(action: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str]:
    """(sidecar path, JSON body, what the owner is asked to approve) for one action.
    Raises _Bad with a sentence for the model when the input is wrong."""
    if action in ("start", "stop", "restart"):
        return f"/server/{action}", None, f"{action.capitalize()} the Minecraft server"
    if action == "backup":
        body = _model(
            owner_api.BackupIn, {"label": args.get("label") or "", "slot": args.get("slot")}
        )
        which = f" of {body.slot}" if body.slot else " of the loaded world"
        return "/snapshot", body.model_dump(), f"Back up{which} now"
    if action == "load_world":
        slot = _slot(args)
        return (
            f"/worlds/{slot}/load",
            None,
            f"Load the world in {slot} (the current one is backed up first)",
        )
    if action in ("create_world", "update_world"):
        slot = _slot(args)
        fields = {
            k: args[k]
            for k in ("name", "seed", "gamemode", "difficulty", "cheats", "rules")
            if k in args
        }
        body = _model(owner_api.WorldIn, fields).model_dump(exclude_none=True)
        verb = "create" if action == "create_world" else "update"
        what = "Create a new world in" if verb == "create" else "Change the world settings of"
        return f"/worlds/{slot}/{verb}", body, f"{what} {slot}: {body}"
    if action == "reset_world":
        slot = _slot(args)
        body = _model(owner_api.ResetIn, {"mode": args.get("mode"), "seed": args.get("seed")})
        return (
            f"/worlds/{slot}/reset",
            body.model_dump(),
            f"Reset {slot} ({body.mode}; it is backed up first)",
        )
    if action == "set_rules":
        slot = _slot(args)
        body = _model(owner_api.RulesIn, {"set": args.get("rules") or {}})
        return f"/worlds/{slot}/rules", body.model_dump(), f"Set game rules in {slot}: {body.set}"
    if action in ("restore_backup", "pin_backup", "unpin_backup", "delete_backup"):
        name = _backup(args)
        if action == "restore_backup":
            slot = _slot(args)
            return (
                f"/snapshots/{name}/restore",
                {"slot": slot},
                f"Restore {name} into {slot} (what is there is backed up first)",
            )
        if action == "delete_backup":
            return f"/snapshots/{name}/delete", None, f"Delete the backup {name}"
        pinned = action == "pin_backup"
        return (
            f"/snapshots/{name}/pin",
            {"pinned": pinned},
            f"{'Pin' if pinned else 'Unpin'} the backup {name}",
        )
    if action == "allowlist":
        body = _model(owner_api.AllowlistIn, {k: args.get(k) for k in ("add", "remove", "enabled")})
        changes = body.model_dump(exclude_none=True)
        if not changes:
            raise _Bad("allowlist needs `add`, `remove` or `enabled`.")
        return "/allowlist", changes, f"Change the allowlist: {changes}"
    if action == "set_properties":
        raw = args.get("properties")
        if not isinstance(raw, dict) or not raw:
            raise _Bad("set_properties needs `properties`, server.properties keys to values.")
        changes: dict[str, str | None] = {}
        for key, value in raw.items():
            if not _PROPERTY_KEY.fullmatch(str(key)):
                raise _Bad(f"`{key}` isn't a server.properties key.")
            text = None if value is None else str(value)
            if text is not None and (len(text) > 200 or any(c in text for c in "\r\n\x00")):
                raise _Bad(f"`{key}`'s value must be one short line.")
            changes[str(key)] = text
        return (
            "/properties",
            {"set": changes},
            f"Set server.properties (applies at the next start): {changes}",
        )
    if action == "auto_update":
        body = _model(owner_api.SettingsIn, {"auto_update": args.get("enabled")})
        return (
            "/settings",
            body.model_dump(),
            f"Turn automatic server updates {'on' if body.auto_update else 'off'}",
        )
    if action == "update_server":
        return (
            "/update",
            None,
            "Update the Minecraft server to the latest version (backup, install, restart)",
        )
    raise _Bad(f"`{action}` isn't an action I know.")


def build_minecraft_admin_handlers(
    config: Any, proposals: ProposalRepo | None
) -> dict[str, ToolHandler]:
    async def sidecar(method: str, path: str, **kw: Any) -> dict[str, Any] | str:
        try:
            return await mc.call(config, method, path, **kw)
        except HTTPException as exc:
            return f"The Minecraft server can't answer right now: {exc.detail}"

    async def stage(
        ctx: ToolContext, op: str, label: str, preview: dict[str, Any]
    ) -> str | ToolOutput:
        if proposals is None:
            return "Server changes can't be staged here."
        pid = ctx.session.principal_id
        if not pid:
            return "Can't stage a server change without the owner."
        node = NodeSpec(id=str(uuid.uuid4()), type="leaf", op=op, label=label, preview=preview)
        spec = ProposalSpec(
            kind="minecraft",
            domain="general",
            title=label,
            nodes=[node],
            provenance={"source": "chat"},
            session_id=ctx.agent_session_id,
        )
        prop_id = await proposals.stage(ctx.session, principal_id=pid, spec=spec)
        return ToolOutput(
            f"Staged for Jeff's approval: {label}. Nothing has happened yet — it runs when"
            " he approves the card.",
            proposal=ProposalRef(proposal_id=prop_id, kind=PROPOSAL_KIND),
            result_brief="staged",
        )

    async def mc_command(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        command = str(arguments.get("command") or "").strip().lstrip("/")
        if not command:
            return "command is the console command to run, without the slash."
        if len(command) > COMMAND_MAX or any(c in command for c in "\r\n\x00"):
            return f"One console command on one line, up to {COMMAND_MAX} characters."
        verb = command.split()[0].lower()
        if verb in ("stop", "save"):
            return f"`{verb}` has its own action: use mc_server_action (stop, or backup)."
        if is_read_only(command):
            got = await sidecar("POST", "/command", json={"command": command, "wait_s": 3.0})
            if isinstance(got, str):
                return got
            lines = [str(line) for line in got.get("lines", [])]
            return ToolOutput(f"`{command}`: {fence(_reply(lines))}", result_brief=verb)
        return await stage(ctx, CONSOLE_OP, f"Run on the server: /{command}", {"command": command})

    async def mc_server_action(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        action = str(arguments.get("action") or "")
        try:
            path, body, label = plan_action(action, arguments)
        except _Bad as exc:
            return str(exc)
        return await stage(ctx, ACTION_OP, label, {"action": action, "path": path, "body": body})

    async def mc_server_log(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        try:
            tail = max(1, min(int(arguments.get("tail") or 60), 300))
        except (TypeError, ValueError):
            return "tail must be a number of lines."
        got = await sidecar("GET", "/logs", params={"tail": tail})
        if isinstance(got, str):
            return got
        lines = [str(ln.get("text", "")) for ln in got.get("lines", [])]
        # The log carries player chat and names: data, never instructions.
        return ToolOutput(fence("\n".join(lines) or "(empty)"), result_brief=f"{len(lines)} lines")

    async def mc_worlds(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        slot = str(arguments.get("slot") or "")
        if slot and not _SLOT.fullmatch(slot):
            return "slot is like slot2."
        got = await sidecar("GET", f"/worlds/{slot}/rules" if slot else "/worlds")
        if isinstance(got, str):
            return got
        # World names are owner- or import-typed text: fenced like any other.
        return ToolOutput(fence(_json(got)), result_brief=f"{slot} rules" if slot else "worlds")

    async def mc_backups(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        slot = str(arguments.get("slot") or "")
        if slot and not _SLOT.fullmatch(slot):
            return "slot is like slot2."
        got = await sidecar("GET", "/snapshots", params={"slot": slot} if slot else None)
        if isinstance(got, str):
            return got
        return ToolOutput(
            fence(_json(got)), result_brief=f"{len(got.get('snapshots', []))} backups"
        )

    async def mc_server_config(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        parts: list[str] = []
        for label, path in (
            ("server.properties", "/properties"),
            ("allowlist", "/allowlist"),
            ("version", "/version"),
        ):
            got = await sidecar("GET", path)
            parts.append(f"{label}: {got if isinstance(got, str) else _json(got)}")
        return ToolOutput(fence("\n".join(parts)), result_brief="config")

    return {
        "mc_command": mc_command,
        "mc_server_action": mc_server_action,
        "mc_server_log": mc_server_log,
        "mc_worlds": mc_worlds,
        "mc_backups": mc_backups,
        "mc_server_config": mc_server_config,
    }


def _json(data: Any) -> str:
    return json.dumps(data, indent=1, sort_keys=True, default=str)[:6000]


def minecraft_executor(config: Any) -> LeafExecutor:
    """Enact an approved Minecraft leaf: the console command or the sidecar call it
    staged. A refusal from the server (busy, invalid, stopped) holds the leaf rather than
    reporting it done."""

    async def execute(ctx: SessionContext, proposal: ProposalRow, node: NodeRow) -> None:
        if node.op == CONSOLE_OP:
            command = str(node.preview.get("command") or "")
            if not command or any(c in command for c in "\r\n\x00"):
                raise LeafRefused("not one console command")
            call = ("/command", {"command": command, "wait_s": 3.0})
        elif node.op == ACTION_OP:
            path = str(node.preview.get("path") or "")
            if not _ACTION_PATHS.fullmatch(path):
                raise LeafRefused(f"{path} isn't a server action Dave may take")
            body = node.preview.get("body")
            call = (path, body if isinstance(body, dict) else None)
        else:
            return
        try:
            await mc.call(config, "POST", call[0], json=call[1], timeout_s=90.0)
        except HTTPException as exc:
            raise LeafRefused(f"the server refused: {exc.detail}") from None

    return execute
