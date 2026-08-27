"""jmolt's `time_left` tool (docs/plans/JMOLT_SITTINGS_PLAN.md).

Lets jmolt check, mid-turn, how much of its nightly hour is left, so it can pace itself
across the whole hour instead of gauging by feel. jmolt already holds `current_time` (the
wall clock), but turning that into "minutes remaining" would ask a 120B to subtract its
wake time in its head every time — the exact arithmetic the per-sitting countdown is
injected pre-computed to avoid. This tool returns the answer already computed, from the
LOCAL TRUSTED clock (M4), never platform time.

The night stamps its end time in settings at run start and clears it at the end
(`jmolt_night.py`); the handler reads that deadline under jmolt's own context (`app.settings`
is `is_owner()`-gated, which jmolt's owner-principal context satisfies) and reports the time
left. jmolt-only (in JMOLT_TOOLS), `web`-gated, read-only — it touches no owner data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jbrain.agent.jmolt_owner import jmolt_settings_ctx
from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.settings_store import SqlSettingsStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _local(tz: str, dt: datetime) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError):
        return dt.astimezone(UTC)


def time_left_message(deadline_iso: str, tz: str, now: datetime) -> str:
    """The sentence the tool returns. Pure — unit-tested. Reports the current local time
    and, when a night is running, the whole minutes remaining before the hour ends (0 once
    the deadline has passed). A blank/unparseable deadline means no night is in flight."""
    now_local = _local(tz, now)
    stamp = f"It is {now_local:%H:%M} ({tz})."
    try:
        deadline = datetime.fromisoformat(deadline_iso) if deadline_iso else None
    except ValueError:
        deadline = None
    if deadline is None:
        return f"{stamp} Your nightly hour is not running right now."
    remaining_min = max(0, int((deadline - now).total_seconds() // 60))
    if remaining_min <= 0:
        return f"{stamp} Your hour tonight is over — wrap up and save your files."
    return f"{stamp} About {remaining_min} minute(s) remain in your hour tonight."


def build_jmolt_time_handlers(
    maker: async_sessionmaker[AsyncSession],
    settings_store: SqlSettingsStore | None = None,
) -> dict[str, ToolHandler]:
    # settings_store is injectable for tests; the app always passes None and builds one.
    settings = settings_store or SqlSettingsStore(maker)

    async def time_left(_a: dict, ctx: ToolContext) -> str:
        # Settings deny jmolt's own auth context (migration 0178, B9).
        sctx = jmolt_settings_ctx(ctx.session)
        tz = await settings.owner_timezone(sctx) or "UTC"
        deadline = await settings.moltbook_night_deadline(sctx)
        return time_left_message(deadline, tz, datetime.now(UTC))

    return {"time_left": time_left}
