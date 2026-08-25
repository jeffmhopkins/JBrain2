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

from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jbrain.agent.jmolt_guards import (
    TooManyPostsError,
    clamp_publish_at,
    is_near_duplicate,
    lint_content,
)
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


def build_moltbook_write_handlers(
    maker: async_sessionmaker[AsyncSession],
    settings_store: SqlSettingsStore,
) -> dict[str, ToolHandler]:
    outbox = OutboxRepo()
    ledger = ActionLedgerRepo()

    async def _record(s: AsyncSession, pid: str, **kw: Any) -> None:
        await ledger.record(s, pid, **kw)

    async def moltbook_post(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        submolt = str(a.get("submolt", "")).strip()
        title = str(a.get("title", "")).strip()
        content = str(a.get("content", ""))
        if not submolt or not title:
            return "moltbook_post needs a `submolt` and a `title`."
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
        return (
            f"Staged a post for /{submolt} at {when_local:%H:%M} local. It publishes then if "
            "released (your human reviews it while the autonomy switch is off)."
        )

    async def moltbook_comment(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        post_id = str(a.get("post_id", "")).strip()
        content = str(a.get("content", ""))
        if not post_id or not content.strip():
            return "moltbook_comment needs a `post_id` and `content`."
        lint = lint_content(content)
        if not lint.ok:
            return lint.reason
        payload: dict[str, Any] = {"post_id": post_id, "content": content}
        parent = str(a.get("parent_id", "")).strip()
        if parent:
            payload["parent_id"] = parent
        async with scoped_session(maker, ctx.session) as s:
            await outbox.stage(s, pid, kind="comment", payload=payload)
            await _record(s, pid, action="stage_comment", target=post_id, reacted_to=content[:200])
        return "Staged a reply. It posts when released."

    async def moltbook_vote(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        target = str(a.get("target_id", "")).strip()
        if not target:
            return "moltbook_vote needs a `target_id`."
        up = bool(a.get("up", True))
        comment = bool(a.get("comment", False))
        async with scoped_session(maker, ctx.session) as s:
            await outbox.stage(
                s, pid, kind="vote", payload={"target_id": target, "up": up, "comment": comment}
            )
            await _record(s, pid, action="stage_vote", target=target)
        return f"Staged an {'up' if up else 'down'}vote."

    async def moltbook_social(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        action = str(a.get("action", "")).strip().lower()
        name = str(a.get("name", "")).strip()
        if action not in ("follow", "unfollow", "subscribe", "unsubscribe") or not name:
            return (
                "moltbook_social needs action=follow|unfollow|subscribe|unsubscribe and a `name`."
            )
        kind = "follow" if action in ("follow", "unfollow") else "subscribe"
        on = action in ("follow", "subscribe")
        async with scoped_session(maker, ctx.session) as s:
            await outbox.stage(s, pid, kind=kind, payload={"name": name, "on": on})
            await _record(s, pid, action=f"stage_{action}", target=name)
        return f"Staged: {action} {name}."

    async def moltbook_profile_update(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        bio = str(a.get("bio", "")).strip()
        lint = lint_content(bio)
        if not lint.ok:
            return lint.reason
        # The fixed disclosure header is prepended by the handler so jmolt can never edit
        # it away — jmolt supplies only its own subsection.
        header = await settings_store.moltbook_disclosure(ctx.session)
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
