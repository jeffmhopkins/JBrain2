"""jmolt's nightly autonomous run (docs/plans/JMOLT_PLAN.md, W1).

Once a night, in the owner's small-hours window, jmolt wakes for up to one hour on a
DETACHED lane — not awaited inline in any minute tick, so a long run never stalls the
scheduler. The run is a single agent turn whose ReAct loop calls the `moltbook` read
tool many times; a wall-clock watchdog is the hard stop (M-lane), the loop's own
guardrails the soft one.

W1 is read-only lurking: jmolt has only the read umbrella (no scratchpad, no writes),
so the nightly prologue tells it the truth about tonight's limited shape rather than
letting the soul (which describes files and posting) mislead it — honesty is the whole
premise (JMOLT_PLAN §2.3). Later waves widen the prologue as those tools land.

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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.agents import agent_for
from jbrain.agent.clock import now_block
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
_SUMMARY_LEN = 240
_SYSTEM_OWNER = SessionContext(principal_kind="owner")

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
    ) -> None:
        self._sessions = sessions
        self._runlog = runlog
        self._transcript = transcript
        self._executor = executor
        self._settings = settings_store
        self._maker = maker
        self._scratch = JmoltScratchRepo()
        self._notify = notify

    async def run(self, owner_ctx: SessionContext) -> str:
        """Execute one nightly run under `owner_ctx` (owner principal). Never raises —
        a turn failure is recorded as an error run. Returns the session id."""
        profile = agent_for("jmolt")
        tz = await self._settings.owner_timezone(owner_ctx) or "UTC"
        session = await self._sessions.create(
            owner_ctx, domain_scopes=[], title="jmolt night", agent="jmolt"
        )
        run_id = await self._runlog.start(
            owner_ctx, session_id=session.id, prompt_version=profile.version
        )
        # jmolt's turn runs under its OWN scope: owner principal (for owner-only reads),
        # the jmolt domain scope (to read its scratchpad), and auth_context='jmolt' (the
        # only context the M19 RLS split lets write jmolt's tables). The scratch tools use
        # this ctx.session, so their writes pass the firewall.
        read_ctx = jmolt_run_context(owner_ctx.principal_id)
        # First night = jmolt has NEVER written anything → the bootstrap ritual; otherwise
        # the returning-night prologue. Detected from the append-only ARCHIVE, not the live
        # files: a jmolt that deleted (or a crash that lost) its files must not be treated as
        # brand new and re-derive its identity from scratch — its history still exists to
        # rebuild from. Both prologues are honest about W2's shape (reads + notes, no posting).
        async with scoped_session(self._maker, read_ctx) as s:
            first_night = not await self._scratch.history(s, owner_ctx.principal_id)
        prologue = _RITUAL_PROLOGUE if first_night else _RETURNING_PROLOGUE
        conversation = [UserMessage(text=now_block(tz)), UserMessage(text=prologue)]
        recorder = self._runlog.bound(owner_ctx, run_id)

        status, summary, error, steps, cost, stop_reason = "error", "", None, 0, 0, "error"
        try:
            executed = await self._executor.run_turn(
                profile=profile,
                read_ctx=read_ctx,
                read_scopes=(),
                conversation=conversation,
                timezone=tz,
                recorder=recorder,
                agent_session_id=session.id,
            )
            r = executed.result
            status, summary, steps, cost = "done", r.text, r.steps, r.cost_tokens
            stop_reason = r.stop_reason
            with contextlib.suppress(Exception):
                await self._transcript.record_exchange(
                    owner_ctx,
                    session_id=session.id,
                    run_id=run_id,
                    user_text=prologue,
                    assistant_text=r.text,
                    tools=executed.tools,
                    reasoning=executed.reasoning,
                )
        except Exception as exc:  # noqa: BLE001 — a night is a recorded run, never a crash
            log.warning("jmolt_night.turn_failed", error=repr(exc))
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
        with contextlib.suppress(Exception):
            await self._sessions.touch(owner_ctx, session.id)

        # A content-free wake to the owner's devices — the morning digest (W4) is what
        # carries the substance; this just says a night happened.
        summary_line = summary[:_SUMMARY_LEN].strip() if status == "done" else (error or "")
        notify_owner(
            self._notify,
            Notification(
                kind="jmolt_night",
                title="jmolt had a night",
                body=summary_line or ("lurked" if status == "done" else "failed"),
                ref=session.id,
            ),
        )
        return session.id


def _owner_local_now(tz: str, now: datetime) -> datetime:
    try:
        return now.astimezone(ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError):
        return now.astimezone(UTC)


async def _owner_principal_id(maker: async_sessionmaker[AsyncSession]) -> str | None:
    async with scoped_session(maker, _SYSTEM_OWNER) as session:
        return (
            await session.execute(
                text("SELECT id FROM app.principals WHERE kind = 'owner' LIMIT 1")
            )
        ).scalar()


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

    if await settings_store.moltbook_killed(owner_ctx):
        return False  # M6: global kill halts the nightly lane.
    if not await settings_store.moltbook_api_key(owner_ctx):
        return False  # unregistered — nothing to do.

    tz = await settings_store.owner_timezone(owner_ctx) or "UTC"
    local = _owner_local_now(tz, now)
    in_window = local.hour == JMOLT_NIGHT_HOUR and local.minute < JMOLT_NIGHT_WINDOW_MIN
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
    digest: "JmoltDigest | None" = None,
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
