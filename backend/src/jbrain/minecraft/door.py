"""The in-game door to Minecraft_Dave: `/jb:dave …` → one agent turn → a private reply.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §M5 and "One persona, two ways in". The jbrain
behavior pack logs each question as a `dave` event (the sidecar stamps the xuid BDS logged
at connect); this loop drains them like the session and track drains, and runs each as a
headless Minecraft_Dave turn on the scheduled-task path (`tasks.runner.LoopTurnExecutor`).

The door's rules, each enforced here rather than asked of the prompt:
- **The session player is the asker**, fixed by the server-reported xuid: each player has
  their own continuing session (`mc_chat_player.door`), rolled over after 6 hours idle.
- **Player tier**: the allowlist is narrowed (`agents.narrow_for_ingame`: no web, no
  admin, console or config tools), and while the turn runs the tools see the asker
  (`minecraft/asker.py`) and refuse any other player's name.
- **Player text is untrusted**: the question reaches the model inside the data fence.
- **Replies are private**: `tellraw` to the asker alone, built with `json.dumps` so no
  reply text can end the command.
- **One turn at a time per player**; a question asked mid-turn waits its turn.
- **Fail-closed**: a turn that errors answers a short apology and is logged.
- **The fast path first** (§M18, `minecraft/fastpath.py`): a question that clearly is one
  of a few fixed requests is carried out through the same tool handlers and answered with
  Dave's signed-off lines, with no model turn; it lands in the session like any exchange.
- **Only the asker's unlocked items** (§M19, `minecraft/guide.py`): the turn's prompt
  carries the item-guide entries the pack says this player has unlocked, and no others.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.agents import MINECRAFT_DAVE_AGENT, agent_for, narrow_for_ingame
from jbrain.agent.clock import now_block
from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.agent.minecrafttools import chat_intro, fence, name_key, resolve_player, spot_text
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import UserMessage
from jbrain.llm.types import AssistantMessage
from jbrain.minecraft import client as mc
from jbrain.minecraft import fastpath, guide
from jbrain.minecraft.asker import DIMENSIONS, Asker, Spot, asking
from jbrain.minecraft.sessions import check_gamertag
from jbrain.tasks.runner import TurnExecutor

log = structlog.get_logger(__name__)

POLL_SECONDS = 2.0
# A question first seen this long after it was asked is dropped, not answered: after an
# api restart the sidecar still holds the boot's whole event list, and replying to an
# hour-old question in someone's chat is worse than not replying.
FRESH_S = 120.0
# The plan's rollover: a fresh context after this long idle; the old session stays in
# the PWA as history.
IDLE_ROLLOVER = timedelta(hours=6)
# Earlier exchanges replayed into a continuing session, so "and the next one?" follows on.
HISTORY_EXCHANGES = 6
# Questions waiting behind a running turn, per player; past this the player is told to
# slow down rather than queueing more.
QUEUE_MAX = 3
# The box runs one local model; two players asking at once share it.
CONCURRENT_TURNS = 2
# Bedrock chat wraps long lines but a wall of text scrolls the question away: each
# message is one chunk, and the whole reply is capped.
REPLY_CHUNK = 220
REPLY_MAX = 900
PREFIX = "<Dave> "
_LEADING_DAVE = re.compile(r"^\s*dave\b[\s,:;!.-]*", re.IGNORECASE)
_MARKDOWN = re.compile(r"[*_`#>]+")

APOLOGY = "Sorry, I couldn't answer that just now. Try again in a minute."
BUSY = "One moment, I'm still on your last question."
SLOW_DOWN = "Hang on, I've got a few of your questions to get through first."
EMPTY = "I didn't come up with an answer. Try asking another way."


@dataclass(frozen=True)
class Question:
    event_id: int
    asked_at: float
    asker: Asker
    text: str


def _spot(raw: Any) -> Spot | None:
    """A position from a sidecar event, floored to the block; None unless whole."""
    if not isinstance(raw, dict) or raw.get("dim") not in DIMENSIONS:
        return None
    try:
        x, y, z = (float(raw[k]) for k in ("x", "y", "z"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, z)):
        return None
    return Spot(math.floor(x), math.floor(y), math.floor(z), str(raw["dim"]))


def parse_question(e: dict[str, Any]) -> Question | None:
    """A `dave` event as a question, or None when it can't be answered safely: no
    gamertag the reply can be addressed to, no position, or nothing asked."""
    if e.get("kind") != "dave":
        return None
    gamertag = check_gamertag(str(e.get("name", "")))
    at = _spot(e)
    question = _LEADING_DAVE.sub("", " ".join(str(e.get("text", "")).split()))
    if gamertag is None or at is None or not question:
        return None
    xuid = str(e.get("xuid") or "")
    asker = Asker(
        key=xuid if xuid.isdigit() else name_key(gamertag),
        gamertag=gamertag,
        world=str(e.get("world") or "world"),
        at=at,
        spawn=_spot(e.get("spawn")),
        known=guide.parse_known(e.get("known")),
    )
    return Question(int(e["id"]), float(e.get("at") or 0), asker, question[:256])


def in_game_message(q: Question) -> str:
    """The turn's user message: where the asker is (server-reported, so stated plainly
    except the gamertag), then their words fenced as data."""
    a = q.asker
    bed = (
        f" Their bed is at {spot_text(a.spawn)}."
        if a.spawn
        else " They have no bed or respawn anchor set."
    )
    return (
        f"In game: {fence(a.gamertag)} is asking, standing at {spot_text(a.at)}.{bed}\n"
        f"Their question:\n{fence(q.text)}"
    )


def _clean(reply: str) -> str:
    # Section signs are Bedrock's colour codes; markdown is noise in game chat.
    lines = (" ".join(_MARKDOWN.sub("", ln).split()) for ln in reply.replace("§", "").splitlines())
    return " ".join(ln for ln in lines if ln)


def tellraw_commands(gamertag: str, reply: str) -> list[str]:
    """One `tellraw` per chunk of the reply, to the asker only. The gamertag passed
    `check_gamertag` (no quote can be in it) and the text goes through `json.dumps`, which
    escapes quotes and line breaks, so nothing in the reply can end the command."""
    body = _clean(reply) or EMPTY
    if len(body) > REPLY_MAX:
        body = body[: REPLY_MAX - 1].rsplit(" ", 1)[0] + "…"
    chunks: list[str] = []
    while body:
        if len(body) <= REPLY_CHUNK:
            chunks.append(body)
            break
        cut = body.rfind(" ", 0, REPLY_CHUNK)
        cut = cut if cut > 0 else REPLY_CHUNK
        chunks.append(body[:cut])
        body = body[cut:].lstrip()
    return [
        f'tellraw "{gamertag}" '
        + json.dumps({"rawtext": [{"text": (PREFIX if i == 0 else "") + chunk}]})
        for i, chunk in enumerate(chunks)
    ]


class DaveDoor:
    """Drains `dave` events and answers each in its asker's own session."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        settings: Any,
        *,
        sessions: AgentSessionRepo,
        runlog: AgentRunLog,
        transcript: AgentTranscript,
        executor: TurnExecutor,
        owner_principal_id: Callable[[], Awaitable[str | None]],
        tools: Mapping[str, ToolHandler] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._maker = maker
        self._settings = settings
        self._sessions = sessions
        self._runlog = runlog
        self._transcript = transcript
        self._executor = executor
        self._owner_principal_id = owner_principal_id
        # The in-game tool handlers, for the fast path; without them every question is a
        # model turn.
        self._fast = fastpath.FastPath(tools) if tools is not None else None
        self._clock = clock
        self.boot_id: str | None = None
        self.cursor = 0
        self._queues: dict[str, deque[Question]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._turns = asyncio.Semaphore(CONCURRENT_TURNS)

    # --- the drain ---------------------------------------------------------------------

    async def tick(self) -> int:
        """One poll: queue every fresh question. Returns how many were taken."""
        try:
            got = await mc.call(self._settings, "GET", "/events", params={"after": self.cursor})
        except HTTPException:
            return 0
        boot = str(got.get("boot_id", ""))
        new_boot = boot != self.boot_id
        if new_boot and self.cursor:
            # The sidecar restarted and numbers from 1 again.
            got = await mc.call(self._settings, "GET", "/events", params={"after": 0})
        events = list(got.get("events", []))
        if new_boot:
            self.boot_id, self.cursor = boot, 0
        self.cursor = max([self.cursor, *(int(e["id"]) for e in events)])
        taken = 0
        now = self._clock()
        for e in events:
            q = parse_question(e)
            if q is None:
                if e.get("kind") == "dave":
                    log.warning("minecraft.door_unanswerable", event=e.get("id"))
                continue
            if now - q.asked_at > FRESH_S:
                continue
            await self.submit(q)
            taken += 1
        return taken

    async def submit(self, q: Question) -> None:
        key = q.asker.key
        worker = self._workers.get(key)
        if worker is None or worker.done():
            self._queues[key] = deque()
            self._workers[key] = asyncio.create_task(self._serve(key, q))
            return
        waiting = self._queues.setdefault(key, deque())
        if len(waiting) >= QUEUE_MAX:
            await self.reply(q.asker.gamertag, SLOW_DOWN)
            return
        waiting.append(q)
        await self.reply(q.asker.gamertag, BUSY)

    async def _serve(self, key: str, first: Question) -> None:
        await self.answer(first)
        waiting = self._queues[key]
        while waiting:
            await self.answer(waiting.popleft())

    async def drain(self) -> None:
        """Wait for every running turn (tests; shutdown cancels instead)."""
        while pending := [w for w in self._workers.values() if not w.done()]:
            await asyncio.gather(*pending)

    def cancel(self) -> None:
        for worker in self._workers.values():
            worker.cancel()

    # --- one question ------------------------------------------------------------------

    async def answer(self, q: Question) -> None:
        """Answer on the fast path when it can, else run the turn; send the reply. Any
        failure is an apology, never silence."""
        try:
            reply = await self.fast_answer(q)
            if reply is None:
                async with self._turns:
                    reply = await self.run_turn(q)
        except Exception as exc:  # noqa: BLE001 — a failed turn is an apology, not a crash
            log.warning("minecraft.door_turn_failed", player=q.asker.key, error=repr(exc))
            reply = APOLOGY
        await self.reply(q.asker.gamertag, reply)

    async def reply(self, gamertag: str, reply: str) -> None:
        for command in tellraw_commands(gamertag, reply):
            try:
                await mc.call(
                    self._settings, "POST", "/command", json={"command": command, "wait_s": 0.5}
                )
            except HTTPException as exc:
                log.warning("minecraft.door_reply_failed", error=str(exc.detail))
                return

    async def fast_answer(self, q: Question) -> str | None:
        """The fast path's reply, recorded in the asker's session as an ordinary
        exchange; None hands the question to the model (no match, or no signed-off line
        for how it came out)."""
        ask = fastpath.match(q.text) if self._fast is not None else None
        if ask is None or self._fast is None:
            return None
        pid = await self._owner_principal_id()
        if pid is None:
            return None
        owner = SessionContext(principal_id=pid, principal_kind="owner")
        session_id = await self.session_for(owner, q.asker)
        ctx = ToolContext(session=read_context(pid, ()), scopes=(), agent_session_id=session_id)
        try:
            with asking(session_id, q.asker):
                answered = await self._fast.answer(ask, q.asker, ctx)
        except Exception as exc:  # noqa: BLE001 — the model can still answer it
            log.warning("minecraft.door_fastpath_failed", player=q.asker.key, error=repr(exc))
            return None
        if answered is None:
            return None
        log.info("minecraft.door_fastpath", player=q.asker.key, action=ask.action)
        # No run row: no model ran. The exchange is what makes it part of the session, so
        # a later turn's history and the PWA both see it.
        with contextlib.suppress(Exception):
            await self._transcript.record_exchange(
                owner,
                session_id=session_id,
                run_id=None,
                user_text=q.text,
                assistant_text=answered.reply,
                tools=answered.steps,
            )
        with contextlib.suppress(Exception):
            await self._sessions.touch(owner, session_id)
        return answered.reply

    async def world_switches(self, world: str) -> tuple[bool, bool]:
        """This world's Power Pack switches: "keeps inventory" and "teleport". Unreadable
        is both off, so the guide never describes a use the world might not have."""
        try:
            got = await mc.call(self._settings, "GET", "/worlds")
        except HTTPException:
            return False, False
        slot = next((s for s in got.get("slots", []) if s.get("folder") == world), {})
        return slot.get("power_pack_keep") is True, slot.get("power_pack_transport") is True

    async def item_guide(self, asker: Asker) -> str:
        if "power_pack_uses" not in asker.known:
            return guide.for_player(asker.known, keep_switch=False)
        keep, transport = await self.world_switches(asker.world)
        return guide.for_player(asker.known, keep_switch=keep, transport_switch=transport)

    async def run_turn(self, q: Question) -> str:
        pid = await self._owner_principal_id()
        if pid is None:
            raise RuntimeError("no owner principal")
        owner = SessionContext(principal_id=pid, principal_kind="owner")
        session_id = await self.session_for(owner, q.asker)
        intro = await chat_intro(self._maker, None, owner, session_id)
        profile = narrow_for_ingame(agent_for(MINECRAFT_DAVE_AGENT))
        if "power_pack_uses" not in q.asker.known:
            # Transport is a Power Pack use: until the asker has made one, the tool and its
            # description aren't offered, so nothing in the turn names the pack (§M19).
            profile = replace(profile, tools=(profile.tools or frozenset()) - {"mc_transport"})
        items = await self.item_guide(q.asker)
        profile = replace(profile, prompt=f"{profile.prompt}\n\n{items}\n\n{intro}")
        conversation = [
            UserMessage(text=now_block(None)),
            *await self.history(owner, session_id),
            UserMessage(text=in_game_message(q)),
        ]
        run_id = await self._runlog.start(
            owner, session_id=session_id, prompt_version=profile.version
        )
        status, stop_reason, steps, cost = "error", "error", 0, 0
        try:
            with asking(session_id, q.asker):
                executed = await self._executor.run_turn(
                    profile=profile,
                    read_ctx=read_context(pid, ()),
                    read_scopes=(),
                    conversation=conversation,
                    timezone=None,
                    recorder=self._runlog.bound(owner, run_id),
                    agent_session_id=session_id,
                )
            result = executed.result
            status, stop_reason = "done", result.stop_reason
            steps, cost = result.steps, result.cost_tokens
            with contextlib.suppress(Exception):
                await self._transcript.record_exchange(
                    owner,
                    session_id=session_id,
                    run_id=run_id,
                    user_text=q.text,
                    assistant_text=result.text,
                    tools=executed.tools,
                    reasoning=executed.reasoning,
                )
            return result.text
        finally:
            with contextlib.suppress(Exception):
                await self._runlog.finish(
                    owner,
                    run_id,
                    status=status,
                    stop_reason=stop_reason,
                    step_count=steps,
                    cost_tokens=cost,
                )
            with contextlib.suppress(Exception):
                await self._sessions.touch(owner, session_id)

    async def session_for(self, owner: SessionContext, asker: Asker) -> str:
        """The asker's in-game session: their latest one unless it has been idle past
        the rollover, else a new one bound to them."""
        async with scoped_session(self._maker, owner) as s:
            # Goals, memory or places the owner wrote for this gamertag before the server
            # knew its xuid move onto it now, so the player finds them in game.
            await resolve_player(s, asker.gamertag)
            row = (
                await s.execute(
                    text(
                        "SELECT cp.agent_session_id, s.last_active_at"
                        " FROM app.mc_chat_player cp"
                        " JOIN app.agent_sessions s ON s.id = cp.agent_session_id"
                        " WHERE cp.xuid = :k AND cp.door"
                        " ORDER BY s.last_active_at DESC LIMIT 1"
                    ),
                    {"k": asker.key},
                )
            ).first()
        if row is not None and datetime.now(UTC) - row.last_active_at < IDLE_ROLLOVER:
            return str(row.agent_session_id)
        session = await self._sessions.create(
            owner, domain_scopes=[], title=f"In game: {asker.gamertag}", agent=MINECRAFT_DAVE_AGENT
        )
        async with scoped_session(self._maker, owner) as s:
            await s.execute(
                text(
                    "INSERT INTO app.mc_chat_player (agent_session_id, xuid, gamertag, door)"
                    " VALUES (:sid, :k, :g, true)"
                ),
                {"sid": session.id, "k": asker.key, "g": asker.gamertag},
            )
        return str(session.id)

    async def history(self, owner: SessionContext, session_id: str) -> Sequence[Any]:
        """The session's last few exchanges as plain text, the player's words fenced
        again: the transcript keeps what they typed, which is still theirs."""
        try:
            turns = await self._transcript.load(owner, session_id)
        except Exception as exc:  # noqa: BLE001 — a lost history is a fresh start, not a failure
            log.warning("minecraft.door_history_unread", error=repr(exc))
            return []
        out: list[Any] = []
        for turn in turns[-HISTORY_EXCHANGES * 2 :]:
            if turn.role == "user":
                out.append(UserMessage(text=f"In game, earlier:\n{fence(turn.content)}"))
            elif turn.content:
                out.append(AssistantMessage(text=turn.content))
        return out


async def run_dave_door(door: DaveDoor, *, interval: float = POLL_SECONDS) -> None:
    """Forever: drain, wait, repeat. Outlives every server restart."""
    try:
        while True:
            try:
                await door.tick()
            except Exception as exc:  # noqa: BLE001 — the loop outlives every failure
                log.warning("minecraft.door_error", error=repr(exc))
            await asyncio.sleep(interval)
    finally:
        door.cancel()
