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
    ) -> None:
        self._maker = maker
        self._client = client
        self._router = router
        self._settings = settings_store
        self._notify = notify
        self._outbox = OutboxRepo()
        self._ledger = ActionLedgerRepo()

    async def tick(self, *, now: datetime | None = None) -> int:
        """One sweep. Returns the number of rows published. Never raises."""
        now = now or datetime.now(UTC)
        pid = await _owner_principal_id(self._maker)
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

        async with scoped_session(self._maker, admin) as s:
            due = await self._outbox.due(s, now=now)

        published = 0
        for row in due:
            outcome = await self._publish_one(pid, admin, row)
            if outcome == "published":
                published += 1
            elif outcome == "deferred":
                # A rate-limit (429): stop the tick and let this row — and the rest of the queue
                # — retry on a later tick. This spaces publishing out over ticks instead of
                # hammering a throttling platform, and the deferred rows stay `released` (never
                # dropped), which is the fix for a busy night's tail failing terminally.
                log.info("jmolt_sweep.deferred_on_rate_limit", kind=row.kind)
                break
        return published

    async def _publish_one(self, pid: str, admin: SessionContext, row: OutboxRow) -> PublishOutcome:
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
