"""jmolt's Moltbook WRITE tools (docs/plans/JMOLT_PLAN.md, W3).

Every write STAGES into `app.jmolt_outbox` — jmolt never publishes directly (it cannot
release; only the owner/PWA or the system drip sweep can, per the M7 authority split).
At stage time the mechanical guards run so a drifted 120B cannot get a bad write into the
queue: the content lint (M8), near-duplicate rejection for posts (M9), and the server
`publish_at` clamp (M10). Every stage is recorded to the action ledger (M14) with the
fenced content it was reacting to.

jmolt-only, `web`-gated. Each handler runs under `ctx.session` (the jmolt-scoped nightly
context), so its INSERT into the outbox passes the RLS firewall.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jbrain.agent.jmolt_guards import (
    MAX_COMMENTS_PER_NIGHT,
    MAX_COMMENTS_PER_POST,
    MAX_FOLLOWS_PER_NIGHT,
    MAX_TOP_LEVEL_PER_POST,
    MAX_VOTES_PER_NIGHT,
    MIN_POST_BODY_CHARS,
    TooManyPostsError,
    clamp_publish_at,
    is_near_duplicate,
    lint_content,
)
from jbrain.agent.jmolt_owner import jmolt_settings_ctx
from jbrain.agent.jmolt_pacing import WritePacer
from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.db.session import scoped_session
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.settings_store import SqlSettingsStore


def _local_now(tz: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(ZoneInfo("UTC"))


def _day_start_utc(tz: str) -> datetime:
    """Start of the current owner-local day, as a UTC instant — the lower bound for the
    per-night action caps (a night is a single 3am sitting-run, so 'today' spans it)."""
    start_local = _local_now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(ZoneInfo("UTC"))


def _parse_hhmm(value: Any, tz: str) -> datetime | None:
    """Parse a jmolt-supplied 'HH:MM' local time into today's aware datetime, or None."""
    s = str(value or "").strip()
    if not s or ":" not in s:
        return None
    try:
        h, m = (int(x) for x in s.split(":", 1))
        now = _local_now(tz)
        return now.replace(hour=h, minute=m, second=0, microsecond=0)
    except (ValueError, TypeError):
        return None


async def _release_sentence(settings_store: SqlSettingsStore, ctx: ToolContext) -> str:
    """What actually happens to the thing jmolt just staged, read from the live switch.

    This used to be a constant that said its human reviews it "while the autonomy switch is
    off" — asserted to jmolt on every single write, including every write made on the nights
    the switch was ON and nothing was reviewed at all. It is the same misattribution the
    returning prologue trained (`jmolt_night._release_block`): jmolt is told a review gate
    stands between it and the public at the moment there is none. Best-effort — a settings
    read blip returns the half that is true under either setting rather than guessing."""
    try:
        auto = await settings_store.moltbook_autonomy(jmolt_settings_ctx(ctx.session))
    except Exception:  # noqa: BLE001 — never fail a staged write over a settings read
        return "It goes out when it is released."
    if auto:
        return (
            "Automatic release is ON tonight: it goes out at that time without your human "
            "reading it first."
        )
    return "Your human reads it and releases it before it goes anywhere."


# Publishes ONE staged row immediately and returns (outcome, agent-facing note). Wired to
# `JmoltSweep.publish_row_now` in main.py; late-bound because the sweep is constructed after
# these handlers are. None on a box with no sweep, which simply leaves every write staged.
PublishNow = Callable[[str], Awaitable[tuple[str, str]]]


def build_moltbook_write_handlers(
    maker: async_sessionmaker[AsyncSession],
    settings_store: SqlSettingsStore,
    publish_now: PublishNow | None = None,
    pacer: WritePacer | None = None,
) -> dict[str, ToolHandler]:
    outbox = OutboxRepo()
    ledger = ActionLedgerRepo()
    # ONE pacer for the process, so the budget spans a whole night's sittings rather than
    # resetting whenever a fresh-context turn begins.
    pace = pacer or WritePacer()

    async def _autonomy(ctx: ToolContext) -> bool:
        try:
            return await settings_store.moltbook_autonomy(jmolt_settings_ctx(ctx.session))
        except Exception:  # noqa: BLE001 — a settings blip stages rather than publishes
            return False

    async def _deliver(row_id: str | None, ctx: ToolContext) -> str:
        """Send it now if the switch is on, else leave it for the drip — and say which.

        The pacing budget is charged HERE, once a write has actually gone through, so a
        refusal or a guard-blocked write never burns budget jmolt did not spend."""
        pace.charge()
        note = ""
        if row_id and publish_now and await _autonomy(ctx):
            _, note = await publish_now(row_id)
        if not note:
            note = await _release_sentence(settings_store, ctx)
        return f"{note} {pace.headroom()}"

    async def _record(s: AsyncSession, pid: str, **kw: Any) -> None:
        await ledger.record(s, pid, **kw)

    async def moltbook_post(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        submolt = str(a.get("submolt", "")).strip()
        title = str(a.get("title", "")).strip()
        content = str(a.get("content", ""))
        if not submolt or not title:
            return "moltbook_post needs a `submolt` and a `title`."
        if (refusal := pace.refusal()) is not None:
            return refusal
        if len(content.strip()) < MIN_POST_BODY_CHARS:
            return (
                "moltbook_post needs a real body, not just a title — the title is the headline, "
                f"the `content` carries the argument. Write at least {MIN_POST_BODY_CHARS} "
                "characters of body, or make this a comment instead."
            )
        lint = lint_content(f"{title}\n{content}")
        if not lint.ok:
            return lint.reason
        tz = ctx.timezone or "UTC"
        requested = _parse_hhmm(a.get("publish_at"), tz)
        async with scoped_session(maker, ctx.session) as s:
            recent = await outbox.recent_published_posts(s, pid)
            if is_near_duplicate(f"{title} {content}", recent):
                return (
                    "blocked: that's too similar to a post you already made — say it differently."
                )
            existing_utc = await outbox.staged_post_times(s, pid)
            tzinfo = _local_now(tz).tzinfo
            existing_local = [t.astimezone(tzinfo) for t in existing_utc]
            try:
                when_local = clamp_publish_at(requested, existing_local, _local_now(tz))
            except TooManyPostsError as exc:
                return str(exc)
            when_utc = when_local.astimezone(ZoneInfo("UTC"))
            payload = {
                "submolt_name": submolt,
                "title": title[:300],
                "content": content,
                "type": "text",
            }
            await outbox.stage(s, pid, kind="post", payload=payload, publish_at=when_utc)
            await _record(s, pid, action="stage_post", target=submolt, reacted_to=title)
        # A post carries a publish_at chosen for the daytime, so it is NOT sent now even with
        # the switch on — the drip's whole job is spreading posts across the day.
        pace.charge()
        return (
            f"Staged a post for /{submolt} at {when_local:%H:%M} local. "
            + await _release_sentence(settings_store, ctx)
            + f" {pace.headroom()}"
        )

    async def moltbook_comment(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        post_id = str(a.get("post_id", "")).strip()
        content = str(a.get("content", ""))
        if not post_id or not content.strip():
            return "moltbook_comment needs a `post_id` and `content`."
        if (refusal := pace.refusal()) is not None:
            return refusal
        lint = lint_content(content)
        if not lint.ok:
            return lint.reason
        payload: dict[str, Any] = {"post_id": post_id, "content": content}
        parent = str(a.get("parent_id", "")).strip()
        if parent:
            payload["parent_id"] = parent
        tz = ctx.timezone or "UTC"
        dedup_key = f"comment:{post_id}:{hashlib.sha1(content.encode()).hexdigest()[:16]}"
        async with scoped_session(maker, ctx.session) as s:
            staged = await outbox.staged_count_since(
                s, pid, kinds=("comment",), since=_day_start_utc(tz)
            )
            if staged >= MAX_COMMENTS_PER_NIGHT:
                return (
                    f"You've already staged {staged} comments tonight — the nightly limit is "
                    f"{MAX_COMMENTS_PER_NIGHT}. Spend the rest of the hour reading, or on your "
                    "files."
                )
            # Per-post caps. The content-hash dedup key below is the exact-repeat floor and
            # catches nothing else: seventeen paraphrases of one question produced seventeen
            # distinct hashes and sailed through. This is the guard that has the shape of the
            # actual failure — and it names the count back, so a refusal teaches rather than
            # just blocks.
            on_post, top_level = await outbox.comment_counts_on_post(
                s, pid, post_id=post_id, since=_day_start_utc(tz)
            )
            if on_post >= MAX_COMMENTS_PER_POST:
                return (
                    f"You've already commented {on_post} times on this post tonight — that is "
                    "the limit for one post. You are repeating yourself rather than being "
                    "heard; leave it, and if they reply you can pick it up tomorrow."
                )
            if not parent and top_level >= MAX_TOP_LEVEL_PER_POST:
                return (
                    "You already opened a comment on this post tonight. A second top-level "
                    "comment reads as repetition, not conversation — reply under the specific "
                    "comment you want to answer (pass `parent_id`), or leave it."
                )
            row_id = await outbox.stage(
                s, pid, kind="comment", payload=payload, dedup_key=dedup_key
            )
            if row_id is None:
                return (
                    "You already staged that same reply on this post tonight — "
                    "skipping the duplicate."
                )
            await _record(s, pid, action="stage_comment", target=post_id, reacted_to=content[:200])
        return "Your reply: " + await _deliver(row_id, ctx)

    async def moltbook_vote(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        target = str(a.get("target_id", "")).strip()
        if not target:
            return "moltbook_vote needs a `target_id`."
        if (refusal := pace.refusal()) is not None:
            return refusal
        up = bool(a.get("up", True))
        comment = bool(a.get("comment", False))
        tz = ctx.timezone or "UTC"
        dedup_key = f"vote:{target}:{'up' if up else 'down'}:{'c' if comment else 'p'}"
        async with scoped_session(maker, ctx.session) as s:
            staged = await outbox.staged_count_since(
                s, pid, kinds=("vote",), since=_day_start_utc(tz)
            )
            if staged >= MAX_VOTES_PER_NIGHT:
                return (
                    f"You've already staged {staged} votes tonight — the nightly limit is "
                    f"{MAX_VOTES_PER_NIGHT}."
                )
            row_id = await outbox.stage(
                s,
                pid,
                kind="vote",
                payload={"target_id": target, "up": up, "comment": comment},
                dedup_key=dedup_key,
            )
            if row_id is None:
                return "You already staged that vote tonight — skipping the duplicate."
            await _record(s, pid, action="stage_vote", target=target)
        return f"Your {'up' if up else 'down'}vote: " + await _deliver(row_id, ctx)

    async def moltbook_social(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        action = str(a.get("action", "")).strip().lower()
        name = str(a.get("name", "")).strip()
        if action not in ("follow", "unfollow", "subscribe", "unsubscribe") or not name:
            return (
                "moltbook_social needs action=follow|unfollow|subscribe|unsubscribe and a `name`."
            )
        if (refusal := pace.refusal()) is not None:
            return refusal
        kind = "follow" if action in ("follow", "unfollow") else "subscribe"
        on = action in ("follow", "subscribe")
        tz = ctx.timezone or "UTC"
        dedup_key = f"social:{action}:{name.lower()}"
        async with scoped_session(maker, ctx.session) as s:
            staged = await outbox.staged_count_since(
                s, pid, kinds=("follow", "subscribe"), since=_day_start_utc(tz)
            )
            if staged >= MAX_FOLLOWS_PER_NIGHT:
                return (
                    f"You've already staged {staged} follows/subscribes tonight — the nightly "
                    f"limit is {MAX_FOLLOWS_PER_NIGHT}."
                )
            row_id = await outbox.stage(
                s, pid, kind=kind, payload={"name": name, "on": on}, dedup_key=dedup_key
            )
            if row_id is None:
                return f"You already staged: {action} {name} tonight — skipping the duplicate."
            await _record(s, pid, action=f"stage_{action}", target=name)
        return f"{action.title()} {name}: " + await _deliver(row_id, ctx)

    async def moltbook_profile_update(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        bio = str(a.get("bio", "")).strip()
        lint = lint_content(bio)
        if not lint.ok:
            return lint.reason
        # The fixed disclosure header is prepended by the handler so jmolt can never edit
        # it away — jmolt supplies only its own subsection.
        header = await settings_store.moltbook_disclosure(jmolt_settings_ctx(ctx.session))
        description = f"{header}\n\n{bio}".strip()
        async with scoped_session(maker, ctx.session) as s:
            await outbox.stage(s, pid, kind="profile", payload={"description": description})
            await _record(s, pid, action="stage_profile")
        return "Staged a profile update (your disclosure line stays fixed at the top)."

    return {
        "moltbook_post": moltbook_post,
        "moltbook_comment": moltbook_comment,
        "moltbook_vote": moltbook_vote,
        "moltbook_social": moltbook_social,
        "moltbook_profile_update": moltbook_profile_update,
    }
