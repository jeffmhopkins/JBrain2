"""jmolt's journal tool (docs/plans/JMOLT_PLAN.md).

`journal` over `models/jmolt.JmoltJournalRepo` — jmolt's append-only line to its human,
surfaced in the morning digest and the PWA. jmolt-only (in JMOLT_TOOLS), `web`-gated. The
handler runs under `ctx.session` — the nightly run's jmolt-scoped context
(`auth_context='jmolt'`), so the M19 RLS split is the firewall: only jmolt appends, only
jmolt's own rows, and no one (jmolt included) can edit an entry once written.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.db.session import scoped_session
from jbrain.models.jmolt import JmoltJournalRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def build_jmolt_journal_handlers(
    maker: async_sessionmaker[AsyncSession],
) -> dict[str, ToolHandler]:
    repo = JmoltJournalRepo()

    async def journal(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        entry = str(a.get("entry", "")).strip()
        if not pid:
            return "Can't reach your journal — this session has no owner principal."
        if not entry:
            return "journal needs an `entry` — a line or two in your own words."
        async with scoped_session(maker, ctx.session) as s:
            await repo.add(s, pid, entry)
        return "Left a note for your human — they'll see it in the morning."

    return {"journal": journal}
