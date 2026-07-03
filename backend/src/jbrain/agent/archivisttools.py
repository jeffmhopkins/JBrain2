"""The archivist persona's cross-session memory tools (docs/archive/EMAIL_ARCHIVIST_PLAN.md).

A `web`-class (direct-exec, archivist-only) pair over the owner-only `archivist_memory`
scratchpad: the agent recalls its taxonomy/filing decisions at session start and
records new ones as it goes, so a 20-year cleanup continues across sessions instead of
starting blind. Owner-only RLS (`app.is_owner()`) is the firewall — each handler runs
its query under `ctx.session`'s scope (like `query_server_metrics`). This is the
agent's own notes, never the owner's knowledge base; the archivist still reads no
note/entity/domain data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.db.session import scoped_session
from jbrain.models.archivist import ArchivistMemoryRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# A single memory document is the archivist's scratchpad; cap it so it stays a concise
# running summary rather than an unbounded log.
_MAX_CHARS = 20_000


def build_archivist_memory_handlers(
    maker: async_sessionmaker[AsyncSession],
) -> dict[str, ToolHandler]:
    """The memory read/write tools, bound to the app's sessionmaker. Each handler runs
    under `ctx.session` (the turn's RLS scope), so the owner-only firewall on
    `archivist_memory` — not this code — is the gate."""
    repo = ArchivistMemoryRepo()

    async def archivist_memory_read(arguments: dict, ctx: ToolContext) -> str:
        if not ctx.session.principal_id:
            return "Can't read memory — this session has no owner principal."
        async with scoped_session(maker, ctx.session) as session:
            content = await repo.read(session, ctx.session.principal_id)
        return content or "(your memory is empty — nothing saved yet)"

    async def archivist_memory_write(arguments: dict, ctx: ToolContext) -> str:
        content = str(arguments.get("content", ""))
        if len(content) > _MAX_CHARS:
            return (
                f"That's too long to store ({len(content)} chars, max {_MAX_CHARS}). "
                "Summarize it to the essentials and save again."
            )
        if not ctx.session.principal_id:
            return "Can't save memory — this session has no owner principal."
        async with scoped_session(maker, ctx.session) as session:
            await repo.write(session, ctx.session.principal_id, content)
        return "Memory saved."

    return {
        "archivist_memory_read": archivist_memory_read,
        "archivist_memory_write": archivist_memory_write,
    }
