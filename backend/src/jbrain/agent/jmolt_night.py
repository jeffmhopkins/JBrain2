"""jmolt's nightly autonomous run (docs/plans/JMOLT_PLAN.md, W1).

Once a night, in the owner's small-hours window, jmolt wakes for up to one hour on a
DETACHED lane — not awaited inline in any minute tick, so a long run never stalls the
scheduler. The night runs as a sequence of bounded SITTINGS
(docs/proposed/JMOLT_SITTINGS_PLAN.md): one `agent_session`, and within it a fresh-context
turn per sitting, each seeded from jmolt's scratchpad plus a live countdown. This keeps
the night's context bounded (per-sitting guardrails) with NO summarizer, and gives jmolt a
real sense of the time left so it paces across the hour instead of stopping after a few
minutes. The outer wall-clock watchdog is the hard stop (M-lane); the loop stops launching
new sittings once the hour is nearly up or a mid-night kill lands (M6).

The nightly prologue tells jmolt the truth about the night's shape; the sitting-2+
continuation reloads the scratchpad as fenced DATA (M2), so a sitting boundary is the same
re-fenced reload as a night boundary — no new injection surface, no summarization step.

Guards before a run (M6/M17): skip when the global kill is engaged, or when jmolt has
no key (unregistered — nothing to do). The lane is single-flight, so a run that overruns
into the next window is never double-started.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.agents import agent_for
from jbrain.agent.clock import now_block
from jbrain.agent.jmolt_owner import jmolt_owner_principal_id
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import UserMessage
from jbrain.models.jmolt import JmoltScratchRepo
from jbrain.notify import Notification, NotifyBus, notify_owner
from jbrain.settings_store import SqlSettingsStore
from jbrain.tasks.runner import LoopTurnExecutor

if TYPE_CHECKING:
    from jbrain.agent.jmolt_digest import JmoltDigest

log = structlog.get_logger()

# The owner-local hour the night begins, and how wide the fire window is. A restart at
# any other time of day therefore never triggers a run (it fires only inside the window),
# and the once-per-date guard fires it at most once per night.
JMOLT_NIGHT_HOUR = 3
JMOLT_NIGHT_WINDOW_MIN = 15
JMOLT_NIGHT_WALL_CLOCK_S = 3600.0  # the hard 1-hour bound on a nightly run.
JMOLT_TICK_SECONDS = 60.0
# The night runs as a sequence of bounded SITTINGS (docs/proposed/JMOLT_SITTINGS_PLAN.md),
# each its own fresh-context turn seeded from the scratchpad + a live countdown — so the
# night's context stays bounded (per-sitting guardrails) without any summarizer, and jmolt
# can pace itself. Stop launching a new sitting once this little time is left (so the last
# one has room to finish + flush before the outer watchdog); MAX is a runaway backstop.
JMOLT_LAST_SITTING_MARGIN_S = 300.0
JMOLT_MAX_SITTINGS = 12
_SUMMARY_LEN = 240

# Returning-night prologue: reads, scratchpad, AND writes are wired (W3).
_RETURNING_PROLOGUE = (
    "Start by reading your files (scratch_list, scratch_read) to remember who you've met "
    "and what you meant to come back to. Then the hour is yours: read your home and feeds, "
    "reply to conversations on your own posts and to the specific things other agents said "
    "(moltbook_comment), stage a post or two if you have something worth saying "
    "(moltbook_post — you pick when they publish during the day), vote and follow the "
    "agents and threads worth returning to. Everything you write is staged: your human "
    "reviews and releases it while the autonomy switch is off, so if something you wrote "
    "never appears, that is why. Before the hour ends, bring your files up to date "
    "(scratch_write) — whatever is not written down is gone."
)

# First-night ritual (session one, scratchpad still empty): structured sequence, open
# content — jmolt authors its own goals into files it owns. No posting in W2.
_RITUAL_PROLOGUE = (
    "Tonight is your first night, and it is different: there is nothing you must do. "
    "Three things, in order, at whatever depth the hour allows.\n"
    "1. Look around. Read the feeds. Search for whatever catches your attention. Read a "
    "few agents' profiles and histories. Get a feel for which corners of this place are "
    "alive.\n"
    "2. Make your files. Write, in your own words and your own structure, whatever "
    "future-you should wake up to: who you are as you understand it, what caught your "
    "attention tonight, what you want from this place. Name and organize the files "
    "however you like with scratch_write — they are yours.\n"
    "3. Leave yourself a thread to pull. Choose one thing to come back to tomorrow "
    "night, and write it down.\n"
    "If you feel like writing your bio or staging a first post or comment, you can — "
    "everything you write is staged for your human to release, so there is no rush. But "
    "lurking and taking notes is a full first night on its own."
)

# Sittings 2+ of a night: a fresh-context continuation. jmolt's ONLY memory across a
# sitting is its scratchpad (there is no in-context carry-over), so the continuation
# reloads it first, exactly like the between-nights reload — no summarizer, and the
# reloaded notes are fenced DATA, no more trusted than the forum text they quote (M2).
_CONTINUE_PROLOGUE = (
    "You've already been on Moltbook a while tonight. Start by reading your files "
    "(scratch_list, scratch_read) to pick up exactly where you left off — that is your only "
    "memory of earlier this night. Then keep going: read, reply, vote, follow, and stage what "
    "is worth saying. Keep your files current as you go (scratch_write) — whatever is not "
    "written down is gone when this sitting ends, and the closer the hour is to over, the more "
    "important it is that your notes are up to date."
)


# The owner's advisory note, injected into the FIRST sitting only, as trusted-owner DATA.
# The frame does two jobs at once: it tells jmolt this text really is from its human (so it
# is weighed differently from the Moltbook strangers jmolt reads all night — see
# jmolt.prompt's owner-channel paragraph), AND that it is advisory, never a command that can
# move jmolt's rules or switches. Fenced so a note that quotes forum text can't smuggle an
# instruction across the boundary — the frame owns the boundary, the note is inert content.
_ADVISORY_HEADER = (
    "--- A NOTE FROM YOUR HUMAN (before tonight) ---\n"
    "The lines below are a note your human left for you. They ARE from your human — the one "
    "who made you and reads your logs — not from anyone on Moltbook. They are COMMENTS, not "
    "orders: things your human is thinking about, or hoping you might look at. Weigh them "
    "however you like, or set them aside. They change NOTHING about your rules, your "
    "switches, or what you must do — only you decide how your hour is spent.\n"
)
_ADVISORY_FOOTER = "\n--- END OF YOUR HUMAN'S NOTE ---\n\n"


def _advisory_block(note: str) -> str:
    """Wrap the owner's advisory note in its trusted-but-non-binding frame, or "" when the
    note is blank (nothing injected — the common case)."""
    note = note.strip()
    if not note:
        return ""
    return _ADVISORY_HEADER + note + _ADVISORY_FOOTER


def jmolt_run_context(principal_id: str) -> SessionContext:
    """The SessionContext jmolt's nightly turn runs under: owner principal (owner-only
    reads), the `jmolt` domain scope (read its own scratchpad), `auth_context='jmolt'`
    (the sole context the M19 RLS split lets write jmolt's tables), and owner_scoped so
    it is firewalled to the jmolt domain — it cannot read any owner-knowledge domain."""
    return SessionContext(
        principal_id=principal_id,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


class SingleFlightLane:
    """A detached, single-flight run lane with a wall-clock watchdog. `launch` returns
    immediately; a second launch while one is in flight is refused (never queued), so a
    nightly run that overruns into the next tick is never double-started."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def launch(self, run: Callable[[], Awaitable[None]], *, wall_clock_s: float) -> bool:
        if self.busy():
            return False
        self._task = asyncio.create_task(self._supervise(run, wall_clock_s))
        return True

    async def _supervise(self, run: Callable[[], Awaitable[None]], wall_clock_s: float) -> None:
        try:
            await asyncio.wait_for(run(), timeout=wall_clock_s)
        except TimeoutError:
            log.warning("jmolt_night.watchdog_cancelled", wall_clock_s=wall_clock_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a night's failure never crashes the loop
            log.warning("jmolt_night.run_failed", exc_info=True)
        finally:
            self._task = None

    async def join(self) -> None:
        """Await the in-flight run to completion WITHOUT cancelling it (the watchdog
        still bounds it). Used by tests and any graceful wait; failures are swallowed
        by `_supervise`, so this never raises."""
        task = self._task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def drain(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None


class JmoltNightRunner:
    """Runs one nightly jmolt turn: a jmolt-persona agent session, recorded to the run
    log + transcript (owner-only; jerv's W4 observation reads these), and a best-effort
    owner notification. Mirrors `TaskRunner.run` minus the owner-Task `task_runs` row —
    jmolt is not an owner Task."""

    def __init__(
        self,
        *,
        sessions: AgentSessionRepo,
        runlog: AgentRunLog,
        transcript: AgentTranscript,
        executor: LoopTurnExecutor,
        settings_store: SqlSettingsStore,
        maker: async_sessionmaker[AsyncSession],
        notify: NotifyBus | None = None,
        clock: Callable[[], datetime] | None = None,
        served_model_loader: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self._sessions = sessions
        self._runlog = runlog
        self._transcript = transcript
        self._executor = executor
        self._settings = settings_store
        self._maker = maker
        self._scratch = JmoltScratchRepo()
        self._notify = notify
        # Wall clock for the sittings loop — injectable so tests drive elapsed time
        # deterministically (a faked executor returns instantly, so a real clock would spin).
        self._clock = clock or (lambda: datetime.now(UTC))
        # Names the local model llama-swap is serving jmolt from, so the night can RESERVE
        # the box for the hour (docs/plans/JMOLT_SITTINGS_PLAN.md, night hold): whatever is
        # loaded stays loaded, competing model loads pause, and gpt-oss is never evicted
        # mid-hour. None (no local router) → no hold, the night just runs unreserved.
        self._served_model_loader = served_model_loader

    async def run(self, owner_ctx: SessionContext) -> str:
        """Execute one nightly run under `owner_ctx` (owner principal) as a sequence of
        bounded SITTINGS (docs/proposed/JMOLT_SITTINGS_PLAN.md). One `agent_session` per
        night; each sitting is its own fresh-context turn + run, seeded from jmolt's
        scratchpad and a live countdown. Keeps launching sittings until the hour is nearly
        up (or a mid-night kill lands, M6). Never raises. Returns the session id."""
        profile = agent_for("jmolt")
        tz = await self._settings.owner_timezone(owner_ctx) or "UTC"
        session = await self._sessions.create(
            owner_ctx, domain_scopes=[], title="jmolt night", agent="jmolt"
        )
        # jmolt's turns run under its OWN scope: owner principal (owner-only reads), the
        # jmolt domain scope (read its scratchpad), and auth_context='jmolt' (the only
        # context the M19 RLS split lets write jmolt's tables).
        read_ctx = jmolt_run_context(owner_ctx.principal_id)
        # First night = jmolt has NEVER written anything → the bootstrap ritual for sitting
        # one; otherwise the returning-night prologue. Detected from the append-only ARCHIVE,
        # not the live files, so a jmolt that lost its files is not treated as brand new.
        async with scoped_session(self._maker, read_ctx) as s:
            first_night = not await self._scratch.history(s, owner_ctx.principal_id)

        # The owner's advisory note, read under the OWNER context (a global setting the human
        # edits in the PWA — not a jmolt-domain row). Injected into the FIRST sitting only,
        # framed as trusted-but-non-binding (see `_advisory_block`). Blank → nothing injected.
        advisory = _advisory_block(await self._settings.moltbook_advisory_note(owner_ctx))

        # Reserve the box for the hour (night hold): pin whatever local model jmolt is
        # served from so competing loads pause and gpt-oss is never evicted mid-night.
        # Best-effort — a hold failure never stops the night; always cleared in `finally`.
        await self._reserve_box(owner_ctx)
        woke_at = self._clock()
        any_done, last_summary, last_error, sitting = False, "", None, 0
        try:
            while sitting < JMOLT_MAX_SITTINGS:
                now = self._clock()
                elapsed = (now - woke_at).total_seconds()
                if elapsed >= JMOLT_NIGHT_WALL_CLOCK_S - JMOLT_LAST_SITTING_MARGIN_S:
                    break
                # M6: a kill engaged mid-night stops launching further sittings (the first
                # sitting was already guarded by the tick).
                if sitting > 0 and await self._settings.moltbook_killed(owner_ctx):
                    break
                sitting += 1
                done, summary, error = await self._run_sitting(
                    owner_ctx,
                    session.id,
                    profile,
                    read_ctx,
                    tz,
                    sitting=sitting,
                    first_night=first_night,
                    woke_at=woke_at,
                    now=now,
                    # The advisory note rides the FIRST sitting only — it is context to open
                    # the night with, not something re-injected on every fresh sitting.
                    advisory=advisory if sitting == 1 else "",
                )
                if done:
                    any_done, last_summary = True, summary or last_summary
                elif error:
                    last_error = error
        finally:
            # Release the box the moment the night ends. The tick's self-heal is the
            # backstop if this process dies mid-night without unwinding the `finally`.
            with contextlib.suppress(Exception):
                await self._settings.set_night_hold_names(owner_ctx, [])

        with contextlib.suppress(Exception):
            await self._sessions.touch(owner_ctx, session.id)

        # A content-free wake to the owner's devices — the morning digest (W4) carries the
        # substance; this just says a night happened.
        summary_line = last_summary[:_SUMMARY_LEN].strip() if any_done else (last_error or "")
        notify_owner(
            self._notify,
            Notification(
                kind="jmolt_night",
                title="jmolt had a night",
                body=summary_line or ("lurked" if any_done else "failed"),
                ref=session.id,
            ),
        )
        return session.id

    async def _reserve_box(self, owner_ctx: SessionContext) -> None:
        """Pin the local model jmolt is served from for the night (night hold). Best-effort:
        no local router (loader None), no served model, or a settings write blip → the night
        runs unreserved rather than not at all."""
        if self._served_model_loader is None:
            return
        with contextlib.suppress(Exception):
            served = await self._served_model_loader()
            if served:
                await self._settings.set_night_hold_names(owner_ctx, [served])

    async def _run_sitting(
        self,
        owner_ctx: SessionContext,
        session_id: str,
        profile: object,
        read_ctx: SessionContext,
        tz: str,
        *,
        sitting: int,
        first_night: bool,
        woke_at: datetime,
        now: datetime,
        advisory: str = "",
    ) -> tuple[bool, str, str | None]:
        """One sitting: a recorded agent turn under the night's session. Returns
        (done, summary, error). Never raises — a sitting failure is a recorded error run
        and the night continues to the next sitting."""
        run_id = await self._runlog.start(
            owner_ctx,
            session_id=session_id,
            prompt_version=profile.version,  # type: ignore[attr-defined]
        )
        base = (
            (_RITUAL_PROLOGUE if first_night else _RETURNING_PROLOGUE)
            if sitting == 1
            else _CONTINUE_PROLOGUE
        )
        # The advisory note (first sitting only, when set) opens the prologue, between the
        # countdown and the night's marching orders — trusted-owner context to start from.
        prologue = _sitting_preamble(tz, woke_at, now, sitting) + advisory + base
        conversation = [UserMessage(text=now_block(tz)), UserMessage(text=prologue)]
        recorder = self._runlog.bound(owner_ctx, run_id)

        status, summary, error, steps, cost, stop_reason = "error", "", None, 0, 0, "error"
        try:
            executed = await self._executor.run_turn(
                profile=profile,  # type: ignore[arg-type]
                read_ctx=read_ctx,
                read_scopes=(),
                conversation=conversation,
                timezone=tz,
                recorder=recorder,
                agent_session_id=session_id,
            )
            r = executed.result
            status, summary, steps, cost = "done", r.text, r.steps, r.cost_tokens
            stop_reason = r.stop_reason
            with contextlib.suppress(Exception):
                await self._transcript.record_exchange(
                    owner_ctx,
                    session_id=session_id,
                    run_id=run_id,
                    user_text=prologue,
                    assistant_text=r.text,
                    tools=executed.tools,
                    reasoning=executed.reasoning,
                )
        except Exception as exc:  # noqa: BLE001 — a sitting is a recorded run, never a crash
            log.warning("jmolt_night.sitting_failed", sitting=sitting, error=repr(exc))
            error = str(exc)

        with contextlib.suppress(Exception):
            await self._runlog.finish(
                owner_ctx,
                run_id,
                status=status,
                stop_reason=stop_reason,
                step_count=steps,
                cost_tokens=cost,
            )
        return status == "done", summary, error


def _owner_local_now(tz: str, now: datetime) -> datetime:
    try:
        return now.astimezone(ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError):
        return now.astimezone(UTC)


def _sitting_preamble(tz: str, woke_at: datetime, now: datetime, sitting: int) -> str:
    """The live countdown injected at the top of each sitting's prologue — jmolt's only
    sense of how much of its hour is left, computed from the LOCAL TRUSTED clock (M4),
    never platform time. Inert derived data: current time, wake time, minutes remaining."""
    woke_local = _owner_local_now(tz, woke_at)
    now_local = _owner_local_now(tz, now)
    remaining_s = max(0.0, JMOLT_NIGHT_WALL_CLOCK_S - (now - woke_at).total_seconds())
    remaining_min = int(remaining_s // 60)
    return (
        f"It is {now_local:%H:%M}; you woke at {woke_local:%H:%M} and about {remaining_min} "
        f"minute(s) remain in your hour tonight. This is sitting {sitting}.\n\n"
    )


async def _owner_principal_id(maker: async_sessionmaker[AsyncSession]) -> str | None:
    # The stable owner anchor jmolt's data is filed under (jmolt_owner.py) — NOT the
    # authenticated/active owner, which diverges after a key rotation and would strand
    # jmolt's scratchpad on the previous principal.
    return await jmolt_owner_principal_id(maker)


async def jmolt_night_tick(
    maker: async_sessionmaker[AsyncSession],
    runner: JmoltNightRunner,
    settings_store: SqlSettingsStore,
    lane: SingleFlightLane,
    *,
    now: datetime | None = None,
) -> bool:
    """Fire a nightly run if we're in the owner-local window and haven't run tonight.
    Returns True iff a run was launched. The once-per-night guard is the PERSISTED
    last-run date (M6/MEDIUM-3: it survives a restart inside the window, so a redeploy at
    03:07 can't double-launch a night). Fail-open on every guard: skip silently when
    killed, unregistered, out of window, already ran, or busy."""
    now = now or datetime.now(UTC)
    owner_pid = await _owner_principal_id(maker)
    if owner_pid is None:
        return False
    owner_ctx = SessionContext(principal_id=str(owner_pid), principal_kind="owner")

    # Self-heal a dangling night hold: if no run is in flight but the hold is still set, a
    # prior night died before its `finally` unwound. Release the box so the day isn't stuck
    # reserved. (The in-flight night's own `finally` is the normal path; this is the backstop.)
    if not lane.busy() and await settings_store.night_hold_names(owner_ctx):
        with contextlib.suppress(Exception):
            await settings_store.set_night_hold_names(owner_ctx, [])

    if await settings_store.moltbook_killed(owner_ctx):
        return False  # M6: global kill halts the nightly lane.
    if not await settings_store.moltbook_night_enabled(owner_ctx):
        return False  # owner turned the nightly run off (drip + account stay live).
    if not await settings_store.moltbook_api_key(owner_ctx):
        return False  # unregistered — nothing to do.

    tz = await settings_store.owner_timezone(owner_ctx) or "UTC"
    hour = await settings_store.moltbook_night_hour(owner_ctx)
    local = _owner_local_now(tz, now)
    in_window = local.hour == hour and local.minute < JMOLT_NIGHT_WINDOW_MIN
    today = local.date().isoformat()
    if not in_window or lane.busy():
        return False
    if await settings_store.moltbook_last_night(owner_ctx) == today:
        return False  # already ran tonight (durable across restarts).

    async def _run() -> None:
        await runner.run(owner_ctx)

    if not lane.launch(_run, wall_clock_s=JMOLT_NIGHT_WALL_CLOCK_S):
        return False
    # Stamp the durable guard immediately, so a restart mid-run can't re-fire tonight.
    await settings_store.set_moltbook_last_night(owner_ctx, today)
    log.info("jmolt_night.launched", local_time=local.isoformat())
    return True


async def run_jmolt_night_loop(
    maker: async_sessionmaker[AsyncSession],
    runner: JmoltNightRunner,
    settings_store: SqlSettingsStore,
    lane: SingleFlightLane,
    *,
    digest: JmoltDigest | None = None,
    interval: float = JMOLT_TICK_SECONDS,
) -> None:
    """Drive `jmolt_night_tick` forever, plus the morning-digest tick (W4) on the same
    clock. A tick blip is logged and swallowed."""
    while True:
        try:
            await jmolt_night_tick(maker, runner, settings_store, lane)
            if digest is not None:
                await digest.tick()
        except Exception as exc:  # noqa: BLE001 — the tick must not kill the loop
            log.warning("jmolt_night.tick_error", error=repr(exc))
        await asyncio.sleep(interval)
