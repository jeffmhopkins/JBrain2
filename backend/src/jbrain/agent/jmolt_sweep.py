"""jmolt's daytime drip sweep (docs/plans/JMOLT_PLAN.md, W3).

Runs as a system loop in the web process. Each tick, under a NON-jmolt owner context (so
it may release/advance outbox rows — jmolt itself cannot, per the M7 split):

1. **Global kill (M6)** — if engaged, do nothing.
2. **Failure-streak guard (M11)** — if the verify-failure streak has hit the limit, all
   writes are stopped until the owner clears it.
3. **Auto-release (M7)** — if the autonomy switch is ON, release queued rows; if OFF, they
   wait for the owner to release/discard them in the PWA.
4. **Publish due rows** — call the pinned client's whitelisted write; solve any verification
   challenge with the tool-free solver (M5); reconcile-before-retry on error (M23); record
   every publish to the action ledger (M14); reset the streak on success, bump + notify on
   a verify failure. A rate-limit (429) DEFERS rather than fails: the row stays `released` and
   the tick stops, so the queue drains across ticks instead of a busy night's tail being
   dropped when the platform (or the client's own write-window) throttles.

Posts carry a `publish_at`, so they drip through the day; comments/votes/social/profile
have none and publish on the next tick after release.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.jmolt_guards import MIN_POST_BODY_CHARS
from jbrain.agent.jmolt_owner import jmolt_owner_principal_id
from jbrain.agent.moltbook_verify import solve_challenge
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.router import LlmRouter
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo, OutboxRow
from jbrain.notify import Notification, NotifyBus, notify_owner
from jbrain.settings_store import MOLTBOOK_FAIL_STREAK_LIMIT, SqlSettingsStore
from jbrain.web.moltbook import MoltbookClient, MoltbookError

log = structlog.get_logger()

JMOLT_SWEEP_SECONDS = 60.0

# The drip has to actually DRIP. It used to publish every due row back-to-back inside one
# tick: measured on the box, writes went out 0.19-0.43 s apart, twelve inside three seconds,
# and the platform 429'd us seven times in one release. `RateLedger` did not stop it and
# cannot — it counts calls per MINUTE, so twenty-five writes in one second satisfies it
# exactly as well as twenty-five spread across sixty. A sliding-window COUNT is not a rate.
#
# So the tick spaces its writes and bounds how many it will do. Three seconds apart is an
# instantaneous 20/min — genuinely under the 25/min the local ledger allows and the ~30/min
# the platform documents, rather than merely averaging under it across a minute. Ten of them
# occupies ~27 s of a 60 s tick, so a tick still finishes well before the next, and a busy
# night's thirty-comment tail drains over three or four ticks. Slower is the point: this is a
# drip through the day, not a flush.
JMOLT_WRITE_GAP_S = 3.0
JMOLT_MAX_WRITES_PER_TICK = 10

# `_publish_one`'s outcome: the row went out, was DEFERRED for a later tick (a rate-limit
# 429 — left `released`, not dropped), or terminally FAILED.
PublishOutcome = Literal["published", "deferred", "failed"]


def _admin_ctx(pid: str) -> SessionContext:
    """A NON-jmolt owner context (auth_context is empty): may release/advance/prune, and
    read jmolt's tables via the jmolt domain scope."""
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


async def _owner_principal_id(maker: async_sessionmaker[AsyncSession]) -> str | None:
    # jmolt's stable data anchor (jmolt_owner.py) — the sweep publishes the outbox jmolt
    # staged under it, so it must resolve the same principal jmolt wrote under.
    return await jmolt_owner_principal_id(maker)


class JmoltSweep:
    def __init__(
        self,
        *,
        maker: async_sessionmaker[AsyncSession],
        client: MoltbookClient,
        router: LlmRouter,
        settings_store: SqlSettingsStore,
        notify: NotifyBus | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
        sim: bool = False,
        principal_id: str | None = None,
    ) -> None:
        """`sim` says which world this sweep belongs to, and the two are disjoint: a real
        sweep publishes only real rows, a simulated one only simulated rows
        (JMOLT_LEDGER_ENGINE_PLAN.md, S1). The simulator wires one of these over its
        transport-less client so a staged write actually goes out and becomes visible to
        jmolt's own next read — the loop whose absence produced the seventeen-comment night —
        through the SHIPPED publish path, verification, reconcile, ledger and all, rather
        than a second implementation of it that would drift.

        `principal_id` pins whose outbox this sweep serves; the simulator passes its
        synthetic night principal, since the real box owner is not who staged those rows."""
        self._maker = maker
        self._client = client
        self._router = router
        self._settings = settings_store
        self._notify = notify
        self._outbox = OutboxRepo()
        self._ledger = ActionLedgerRepo()
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        # When the platform hands back a Retry-After, honour it: ticks before this monotonic
        # instant publish nothing. Without it a 429 saying "wait 300 s" was retried at the
        # next 60 s tick — five more knocks on a door that had just told us to stop. The
        # header was already parsed onto the exception and then read by nobody.
        self._hold_until = 0.0
        self._sim = sim
        self._principal_id = principal_id

    async def _pid(self) -> str | None:
        """Whose outbox this sweep serves: the box owner, or the principal it was pinned to."""
        if self._principal_id is not None:
            return self._principal_id
        return await _owner_principal_id(self._maker)

    async def tick(self, *, now: datetime | None = None) -> int:
        """One sweep. Returns the number of rows published. Never raises."""
        now = now or datetime.now(UTC)
        pid = await self._pid()
        if pid is None:
            return 0
        admin = _admin_ctx(pid)

        # Stamp the loop's heartbeat so the PWA can show "drip last ran …" — recorded before
        # the kill/streak guards so it reflects the sweep loop being alive even while paused.
        with contextlib.suppress(Exception):
            await self._settings.set_moltbook_drip_last_swept(admin, now.isoformat())

        if await self._settings.moltbook_killed(admin):
            return 0  # M6
        if await self._settings.moltbook_verify_fail_streak(admin) >= MOLTBOOK_FAIL_STREAK_LIMIT:
            return 0  # M11 — writes stopped until the owner clears the streak

        # M7 auto-release when the switch is ON.
        if await self._settings.moltbook_autonomy(admin):
            async with scoped_session(self._maker, admin) as s:
                for row in await self._outbox.list_by_status(s, pid, ("queued",)):
                    await self._outbox.set_status(s, row.id, "released")

        # A Retry-After the platform gave us on an earlier 429 outranks our own cadence.
        if self._clock() < self._hold_until:
            return 0

        async with scoped_session(self._maker, admin) as s:
            due = await self._outbox.due(s, now=now, sim=self._sim)

        published = 0
        for row in due[:JMOLT_MAX_WRITES_PER_TICK]:
            if published:
                # Space them. This gap is the difference between a drip and a flush — see
                # JMOLT_WRITE_GAP_S. Only between writes, so a tick with one row is immediate.
                await self._sleep(JMOLT_WRITE_GAP_S)
            outcome = await self._publish_one(pid, admin, row)
            if outcome == "published":
                published += 1
            elif outcome == "deferred":
                # A rate-limit (429): stop the tick and let this row — and the rest of the queue
                # — retry on a later tick. The deferred rows stay `released` (never dropped),
                # which is what keeps a busy night's tail from failing terminally.
                log.info(
                    "jmolt_sweep.deferred_on_rate_limit",
                    kind=row.kind,
                    hold_s=round(max(0.0, self._hold_until - self._clock())),
                )
                break
        if len(due) > JMOLT_MAX_WRITES_PER_TICK:
            # Say it rather than let a long queue look like a short one — a silently
            # slow-draining outbox is how a stuck queue hides.
            log.info(
                "jmolt_sweep.queue_tail",
                waiting=len(due) - JMOLT_MAX_WRITES_PER_TICK,
            )
        return published

    async def publish_row_now(self, row_id: str) -> tuple[PublishOutcome, str]:
        """Publish ONE staged row immediately, for a write tool running under autonomy.

        With the switch on, jmolt's write tools call Moltbook through here rather than
        leaving the row for the next drip tick. The point is not speed: it closes the loop
        that produced the seventeen-comment night. Staged writes were invisible to jmolt's
        own reads — it re-read one thread nine times, was shown the same two comments every
        time, and never saw one of its own — so a write that lands before the next read is
        the honest fix, not another prologue block reminding it what it did.

        Everything the drip does is reused rather than reimplemented: reconcile-before-publish
        (M23), the tool-free verification solve (M5), the streak, id extraction, the ledger
        row. Duplicating that in the write tool is how the two paths would drift apart.

        Both gates the tick applies are applied here too — a direct write must not be the way
        around the kill switch (M6) or the verify-fail streak (M11) — plus the platform's own
        Retry-After hold, which outranks anything jmolt wants.

        Returns (outcome, agent-facing note). Never raises: a write tool must not turn a
        publish problem into a broken turn, and the row survives either way for the drip.
        """
        pid = await self._pid()
        if pid is None:
            return "deferred", "staged — the box has no owner principal to publish under."
        admin = _admin_ctx(pid)
        try:
            if await self._settings.moltbook_killed(admin):
                return "deferred", "staged, and held: writing is paused (the global kill)."
            streak = await self._settings.moltbook_verify_fail_streak(admin)
            if streak >= MOLTBOOK_FAIL_STREAK_LIMIT:
                return "deferred", (
                    "staged, and held: too many verification failures in a row, so writing is "
                    "stopped until your human clears it."
                )
            if self._clock() < self._hold_until:
                wait = int(self._hold_until - self._clock())
                return "deferred", (
                    f"staged, and held: Moltbook asked us to back off for another {wait}s. "
                    "It goes out on its own once that clears."
                )
            async with scoped_session(self._maker, admin) as s:
                row = await self._outbox.get(s, row_id)
            if row is None:
                return "deferred", "staged."
            outcome = await self._publish_one(pid, admin, row)
        except Exception:  # noqa: BLE001 — a publish problem must not break jmolt's turn
            log.warning("jmolt_sweep.publish_now_failed", row_id=row_id, exc_info=True)
            return "deferred", "staged — it could not go out just now, so the drip will retry."
        if outcome == "published":
            return outcome, "posted to Moltbook now."
        if outcome == "deferred":
            return outcome, "staged — Moltbook is rate-limiting, so the drip will send it."
        return outcome, "it did NOT go out — the write failed on the way to Moltbook."

    async def _publish_one(self, pid: str, admin: SessionContext, row: OutboxRow) -> PublishOutcome:
        # Every publish in this process goes through here, so this is where a row from the
        # wrong world dies (JMOLT_LEDGER_ENGINE_PLAN.md, S1). The two are disjoint in both
        # directions: the live sweep refuses a simulated row — the fence that matters, since
        # publishing one would post to Moltbook under jmolt's real name — and the simulator's
        # sweep refuses a real one, so a mis-wired harness cannot publish the owner's genuine
        # queue as a side effect of a measurement.
        if row.sim != self._sim:
            log.error(
                "jmolt_sweep.wrong_world_row_refused",
                row_id=row.id,
                kind=row.kind,
                row_sim=row.sim,
                sweep_sim=self._sim,
            )
            return "failed"
        # Reconcile-before-publish for posts (M23, strengthened): a crash between a landed
        # write and its DB commit would otherwise re-publish this row on restart with no
        # error to trigger reconcile. Check the account first; if the post is already there,
        # mark it published instead of double-posting.
        if row.kind == "post":
            existing_id = await self._already_posted(row)
            if existing_id is not None:
                async with scoped_session(self._maker, admin) as s:
                    await self._outbox.set_status(
                        s, row.id, "published", moltbook_id=existing_id, published=True
                    )
                return "published"
        try:
            result = await self._do_write(row)
        except MoltbookError as exc:
            # A rate-limit (429, platform or the client's own write-window) is NOT a failure:
            # leave the row `released` so a later tick retries it, rather than dropping a busy
            # night's tail. The reconcile-first post path still guards against a double-post,
            # since a 429 means the write did not land. Not a verify rejection, so no streak.
            if exc.status == 429:
                if exc.retry_after_s:
                    # Capped so a hostile or fat-fingered header cannot park the queue for a
                    # day; the platform gets the benefit of the doubt, not the keys.
                    self._hold_until = self._clock() + min(float(exc.retry_after_s), 900.0)
                return "deferred"
            await self._reconcile_or_fail(pid, admin, row, exc)
            return "failed"

        # A verification challenge? Solve it tool-free, then submit (M5).
        ver = result.get("verification") if isinstance(result, dict) else None
        if (
            isinstance(ver, dict)
            and ver.get("verification_code")
            and not await self._solve_and_verify(pid, admin, row, ver)
        ):
            return "failed"

        moltbook_id = self._extract_id(result)
        async with scoped_session(self._maker, admin) as s:
            await self._outbox.set_status(
                s, row.id, "published", moltbook_id=moltbook_id, published=True
            )
            await self._ledger.record(
                s,
                pid,
                action=f"publish_{row.kind}",
                target=str(
                    row.payload.get("submolt_name")
                    or row.payload.get("post_id")
                    or row.payload.get("name")
                    or ""
                )[:200],
            )
        return "published"

    async def _solve_and_verify(
        self, pid: str, admin: SessionContext, row: OutboxRow, ver: dict[str, Any]
    ) -> bool:
        answer = await solve_challenge(self._router, str(ver.get("challenge_text", "")))
        if answer is None:
            # M5: a non-numeric solve is a SKIP, not a submission — mark failed but DON'T
            # spend the streak (else an attacker's flood of unsolvable challenges could
            # self-DoS all writes without a single real verify rejection).
            await self._fail(admin, row, "could not solve the verification — skipped /verify")
            return False
        try:
            vr = await self._client.submit_verify(str(ver.get("verification_code")), answer)
        except MoltbookError as exc:
            # A transient submit error is not a rejection — fail the row, don't spend the streak.
            await self._fail(admin, row, self._client.scrub(str(exc)))
            return False
        if not (isinstance(vr, dict) and vr.get("success")):
            # A real platform rejection — this is what the streak counts (M11).
            await self._bump_streak_and_fail(pid, admin, row, "verification rejected")
            return False
        # Success — reset the streak.
        await self._settings.set_moltbook_verify_fail_streak(admin, 0)
        return True

    async def _fail(self, admin: SessionContext, row: OutboxRow, reason: str) -> None:
        async with scoped_session(self._maker, admin) as s:
            await self._outbox.set_status(s, row.id, "failed", error=reason[:500])

    async def _already_posted(self, row: OutboxRow) -> str | None:
        """The platform id of a post already on the account matching this row's title, or
        None. Best-effort — a lookup failure returns None so publishing proceeds."""
        title = str(row.payload.get("title", "")).strip()
        if not title:
            return None
        try:
            recent = await self._client.me_history()
        except MoltbookError:
            return None
        match = next((h for h in recent if str(h.get("title", "")).strip() == title), None)
        return str(match.get("id") or "") if match is not None else None

    async def _do_write(self, row: OutboxRow) -> Any:
        p = row.payload
        if row.kind == "post":
            # The outbox is a durable queue that outlives any handler guard: rows staged
            # before a check existed, or by some future path that skips the tool, still get
            # here. One did — a bare title with no body published to the live site because
            # the stage-time minimum did not exist yet and the client silently dropped the
            # empty field rather than refusing. Refuse at the boundary too, so the row fails
            # visibly with a reason instead of going out as a headline with nothing under it.
            if len(str(p.get("content", "")).strip()) < MIN_POST_BODY_CHARS:
                raise MoltbookError(
                    "refusing to publish a post with no body (a title is not a post)", status=422
                )
            return await self._client.create_post(
                str(p.get("submolt_name", "")),
                str(p.get("title", "")),
                content=str(p.get("content", "")),
                url=p.get("url"),
                post_type=str(p.get("type", "text")),
            )
        if row.kind == "comment":
            return await self._client.create_comment(
                str(p.get("post_id", "")), str(p.get("content", "")), parent_id=p.get("parent_id")
            )
        if row.kind == "vote":
            return await self._client.vote(
                str(p.get("target_id", "")),
                up=bool(p.get("up", True)),
                comment=bool(p.get("comment", False)),
            )
        if row.kind == "follow":
            return await self._client.follow(str(p.get("name", "")), on=bool(p.get("on", True)))
        if row.kind == "subscribe":
            return await self._client.subscribe(str(p.get("name", "")), on=bool(p.get("on", True)))
        if row.kind == "profile":
            return await self._client.update_profile(str(p.get("description", "")))
        raise MoltbookError(f"unknown outbox kind {row.kind!r}")

    async def _reconcile_or_fail(
        self, pid: str, admin: SessionContext, row: OutboxRow, exc: MoltbookError
    ) -> None:
        """M23 — before treating a failed write as failed, reconcile: a timeout may have
        actually landed. For a post, check the account's recent posts; if the title is
        already there, mark it published instead of double-posting. Otherwise mark failed
        (no blind auto-retry — the owner can re-stage)."""
        if row.kind == "post":
            with contextlib.suppress(Exception):
                recent = await self._client.me_history()
                title = str(row.payload.get("title", "")).strip()
                match = next((h for h in recent if str(h.get("title", "")).strip() == title), None)
                if match is not None:
                    async with scoped_session(self._maker, admin) as s:
                        await self._outbox.set_status(
                            s,
                            row.id,
                            "published",
                            moltbook_id=str(match.get("id") or ""),
                            published=True,
                        )
                    return
        async with scoped_session(self._maker, admin) as s:
            await self._outbox.set_status(s, row.id, "failed", error=self._client.scrub(str(exc)))

    async def _bump_streak_and_fail(
        self, pid: str, admin: SessionContext, row: OutboxRow, reason: str
    ) -> None:
        streak = await self._settings.moltbook_verify_fail_streak(admin) + 1
        await self._settings.set_moltbook_verify_fail_streak(admin, streak)
        async with scoped_session(self._maker, admin) as s:
            await self._outbox.set_status(s, row.id, "failed", error=reason)
        if streak >= MOLTBOOK_FAIL_STREAK_LIMIT:
            notify_owner(
                self._notify,
                Notification(
                    kind="jmolt_alert",
                    title="jmolt writes stopped",
                    body=f"{streak} verification failures in a row — writing is paused "
                    "until you clear it.",
                ),
            )

    @staticmethod
    def _extract_id(result: Any) -> str | None:
        if not isinstance(result, dict):
            return None
        post = result.get("post")
        if isinstance(post, dict) and post.get("id"):
            return str(post["id"])
        comment = result.get("comment")
        if isinstance(comment, dict) and comment.get("id"):
            return str(comment["id"])
        return str(result["id"]) if result.get("id") else None


async def run_jmolt_sweep_loop(sweep: JmoltSweep, *, interval: float = JMOLT_SWEEP_SECONDS) -> None:
    while True:
        try:
            await sweep.tick()
        except Exception as exc:  # noqa: BLE001 — the sweep must not kill the loop
            log.warning("jmolt_sweep.tick_error", error=repr(exc))
        await asyncio.sleep(interval)
