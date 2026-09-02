"""The verify path: a heard packet becomes a fired task, or a recorded refusal.

docs/plans/APRS_CONTROL_PLAN.md P4. This is the seam between the two trust tiers, and
the whole file is about keeping them apart:

**Nothing heard here is ever text a model reads.** The packet contributes exactly two
things — a word, matched against a closed set the owner configured, and a code, matched
against an HMAC. Neither reaches the LLM. The task's prompt is the owner's, written in
the editor, and it is identical whether the command came from the truck or a stranger's
transmitter. A packet becoming a prompt would be prompt injection with an antenna.

**The callsign filters and never authenticates.** It is plain bytes in a frame. Setting
one narrows who is worth listening to; the code is what decides.

**The window is checked HERE, at verify time.** Not as a task precondition — a
precondition defers with a retry, and a deferred gate command is a gate that opens hours
late for someone who is no longer there.

**Consume is a conditional UPDATE.** The counter moves past the matched value in the
same statement that checks it still held the value we verified against, so two copies of
one transmission — an echo, a digipeat, a second receiver — cannot fire twice.

**Every attempt is recorded before anything fires.** A refusal is the row worth having.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.notify import Notification, NotifyBus, notify_owner
from jbrain.sdr.command import (
    MAX_FAILURES,
    MIN_KEY_BYTES,
    Window,
    armed_at,
    key_from_text,
    parse_command,
    verify,
)
from jbrain.tasks.repo import TaskInfo
from jbrain.tasks.runner import PushPoke

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

log = structlog.get_logger(__name__)

# What is stored in `command_attempts.code`. A verified code is spent, and an unverified
# one is worthless, so this is not a secret — it is how the owner tells a fat-fingered
# key-in from a probe. Bounded because it came off the air.
MAX_CODE = 16


class Finds(Protocol):
    """`TaskRepo.get` — the gate needs one task, not a repository."""

    async def get(self, ctx: SessionContext, task_id: str) -> TaskInfo | None: ...


class Fires(Protocol):
    """`TaskRunner.run`. Narrow on purpose: a command fires a task through exactly the
    path the scheduler uses, and nothing about running it belongs here."""

    async def run(self, owner_ctx: SessionContext, task: TaskInfo, *, trigger: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class Heard:
    """One decoded frame, as the log stored it."""

    source: str
    info: str
    heard_at: datetime


@dataclass(frozen=True, slots=True)
class Attempt:
    """What the box did about one frame that looked like a command."""

    accepted: bool
    reason: str
    word: str
    task_id: str | None = None


def _plain(text: str) -> str:
    """Printable characters only — see `_record`."""
    return "".join(ch for ch in text if ch >= " ")


def _base_call(call: str) -> str:
    """A callsign without its SSID. `N0CALL-9` from the truck and `N0CALL-7` from the HT
    are the same operator, and an owner who typed the bare call means both."""
    return call.strip().upper().split("-", 1)[0]


def _callsign_allows(configured: str | None, heard: str) -> bool:
    if not configured:
        return True  # no filter set: the code alone decides
    want = configured.strip().upper()
    if "-" in want:
        return want == heard.strip().upper()
    return want == _base_call(heard)


class CommandGate:
    """Offers each heard frame to the owner's command tasks."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        repo: Finds,
        runner: Fires,
        notify: NotifyBus | None = None,
        push: PushPoke | None = None,
        push_tokens: Sequence[str] = (),
    ) -> None:
        self._maker = maker
        self._repo = repo
        self._runner = runner
        self._notify = notify
        self._push = push
        self._push_tokens = list(push_tokens)

    async def offer(self, heard: Heard) -> Attempt | None:
        """Consider one frame. None means it was not a command — which is almost every
        frame on a packet channel, and must stay silent rather than becoming a row."""
        parsed = parse_command(heard.info)
        if parsed is None:
            return None
        word, code = parsed
        row = await self._command_row(word)
        if row is None:
            # An unconfigured word is somebody else's traffic that happened to be two
            # words long. Logging those would bury the attempts that matter.
            return None
        attempt = await self._judge(row, heard, word, code)
        await self._record(attempt, heard, code)
        if attempt.accepted and attempt.task_id:
            await self._fire(attempt.task_id)
        await self._announce(attempt, heard, row)
        return attempt

    async def _command_row(self, word: str) -> dict[str, Any] | None:
        async with scoped_session(self._maker, SessionContext(principal_kind="owner")) as session:
            found = (
                await session.execute(
                    text(
                        "SELECT id, principal_id, command_callsign, command_key,"
                        " command_counter, command_failures, command_days, command_from,"
                        " command_until, command_once, timezone, name, enabled"
                        " FROM app.tasks"
                        " WHERE schedule_kind = 'on_command' AND command_word = :word"
                        " LIMIT 1"
                    ),
                    {"word": word},
                )
            ).mappings()
            first = found.first()
            return dict(first) if first else None

    async def _judge(self, row: dict[str, Any], heard: Heard, word: str, code: str) -> Attempt:
        """Everything that can refuse, in the order that costs least and leaks least."""
        task_id = str(row["id"])
        if not row["enabled"]:
            # A disabled command is off, not merely quiet: the owner's switch is the
            # outermost gate and nothing past it is consulted.
            return Attempt(False, "the command is disabled", word, task_id)
        if not _callsign_allows(row.get("command_callsign"), heard.source):
            return Attempt(False, "not from the allowed callsign", word, task_id)
        window = Window(
            days=tuple(row.get("command_days") or ()),
            start=row.get("command_from"),
            end=row.get("command_until"),
            timezone=row.get("timezone") or "UTC",
        )
        if not armed_at(window, heard.heard_at):
            # Refused, never queued — and deliberately not counted as a failure, so an
            # out-of-hours transmission cannot spend the owner's lockout budget.
            return Attempt(False, "outside the command's window", word, task_id)
        try:
            key = key_from_text(str(row["command_key"] or ""))
        except Exception:  # noqa: BLE001 — a corrupt key is a refusal, not a crash
            return Attempt(False, "the command's key is unreadable", word, task_id)
        # An EMPTY key does not raise: `hmac.new(b"", ...)` is valid, so without this the
        # codes become ones anyone who has read this repository can compute. `verify`
        # refuses a short key too; this is the same guard one layer out, where it can say
        # so in the attempt log.
        if len(key) < MIN_KEY_BYTES:
            return Attempt(False, "the command has no usable key", word, task_id)
        counter = int(row["command_counter"] or 0)
        failures = int(row["command_failures"] or 0)
        verdict = verify(key, counter, code, failures=failures)
        if not verdict.accepted:
            if not verdict.spent:
                # A SPENT code is the network repeating a transmission the owner already
                # made — 144.390 is digipeated, so one command arrives several times.
                # Counting those as guesses fires the lockout on success rather than on
                # attack, and leaves the owner refused at their own gate.
                await self._count_failure(task_id, heard.heard_at)
            return Attempt(False, verdict.reason, word, task_id)
        if not await self._consume(
            task_id,
            seen=counter,
            nxt=verdict.next_counter,
            at=heard.heard_at,
            once=bool(row.get("command_once")),
        ):
            # The row moved between verify and consume: the same transmission reached us
            # twice (a digipeat, a second receiver). The first one fired; this is a
            # replay however innocent, and a replay does nothing.
            return Attempt(False, "code already used", word, task_id)
        return Attempt(True, "verified", word, task_id)

    async def _consume(
        self, task_id: str, *, seen: int, nxt: int, at: datetime, once: bool = False
    ) -> bool:
        """Move the counter past the match, but only if it still held what we verified
        against. Returns whether this caller won the race.

        A one-shot command disarms HERE, in the same statement. Doing it afterwards, as a
        second write, leaves a window in which a duplicate of the same transmission —
        normal on a digipeated channel — finds a command that is still armed."""
        async with scoped_session(self._maker, SessionContext(principal_kind="owner")) as session:
            result = await session.execute(
                text(
                    "UPDATE app.tasks SET command_counter = :nxt, command_failures = 0,"
                    " command_last_at = :at"
                    + (", enabled = false" if once else "")
                    + " WHERE id = :id AND command_counter = :seen"
                ),
                {"id": task_id, "seen": seen, "nxt": nxt, "at": at},
            )
            await session.commit()
            return bool(cast("CursorResult[Any]", result).rowcount)

    async def _count_failure(self, task_id: str, at: datetime) -> None:
        async with scoped_session(self._maker, SessionContext(principal_kind="owner")) as session:
            await session.execute(
                text(
                    "UPDATE app.tasks SET command_failures = command_failures + 1,"
                    " command_last_at = :at WHERE id = :id"
                ),
                {"id": task_id, "at": at},
            )
            await session.commit()

    async def _record(self, attempt: Attempt, heard: Heard, code: str) -> None:
        """The durable record, written whatever happens next. A push can be missed; this
        is what the owner can still find on Tuesday."""
        try:
            async with scoped_session(
                self._maker, SessionContext(principal_kind="owner")
            ) as session:
                await session.execute(
                    text(
                        "INSERT INTO app.command_attempts"
                        " (heard_at, task_id, source, word, code, accepted, reason)"
                        " VALUES (:heard_at, :task_id, :source, :word, :code, :ok, :reason)"
                    ),
                    {
                        "heard_at": heard.heard_at,
                        "task_id": attempt.task_id,
                        # Scrubbed again here, not only where the frame entered. Postgres
                        # rejects a NUL in a text column and this INSERT swallows its own
                        # errors, so an unscrubbed byte does not fail loudly — it deletes
                        # the row silently, which is the one outcome this table exists to
                        # prevent. Cheap insurance on the evidence path.
                        "source": _plain(heard.source)[:16],
                        "word": _plain(attempt.word)[:16],
                        "code": _plain(code)[:MAX_CODE],
                        "ok": attempt.accepted,
                        "reason": attempt.reason[:120],
                    },
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — never let bookkeeping eat the command
            log.warning("command_gate.record_failed", error=repr(exc))

    async def _fire(self, task_id: str) -> None:
        """Run the task the owner attached to this word, exactly as the scheduler would."""
        owner = SessionContext(principal_kind="owner")
        task: TaskInfo | None = None
        with contextlib.suppress(Exception):
            task = await self._repo.get(owner, task_id)
        if task is None:
            log.warning("command_gate.task_missing", task_id=task_id)
            return
        ctx = SessionContext(principal_id=task.principal_id, principal_kind="owner")
        try:
            await self._runner.run(ctx, task, trigger="command")
        except Exception as exc:  # noqa: BLE001 — the runner records its own failures
            log.warning("command_gate.run_failed", error=repr(exc))

    async def _announce(self, attempt: Attempt, heard: Heard, row: dict[str, Any]) -> None:
        """Tell the owner. An accepted command always; a refusal too, EXCEPT while the
        command is already locked out — the lockout was itself announced, and repeating
        it for every further guess would let an attacker turn the owner's phone into the
        denial of service the lockout exists to prevent."""
        if not attempt.accepted and int(row.get("command_failures") or 0) >= MAX_FAILURES:
            return
        name = str(row.get("name") or attempt.word)
        notify_owner(
            self._notify,
            Notification(
                kind="radio_command",
                title=(f"Radio command: {name}" if attempt.accepted else f"Refused: {name}"),
                # The source callsign is shown because it is what the owner recognises,
                # framed as heard-not-verified everywhere it appears.
                body=f"{heard.source} — {attempt.reason}",
                ref=attempt.task_id or "",
            ),
        )
        if self._push is not None and self._push_tokens:
            # Best-effort and content-free, like the task runner's: a push only wakes the
            # PWA, which then fetches over its authenticated channel.
            with contextlib.suppress(Exception):
                await self._push.poke(self._push_tokens)


def heard_from_row(row: dict[str, Any]) -> Heard:
    """A `Heard` from what the log parsed, with a sane time when the frame carried none."""
    stamp = row.get("heard_at")
    when = (
        datetime.fromtimestamp(float(stamp), UTC)
        if isinstance(stamp, int | float)
        else (stamp if isinstance(stamp, datetime) else datetime.now(UTC))
    )
    return Heard(source=str(row.get("src") or ""), info=str(row.get("info") or ""), heard_at=when)
