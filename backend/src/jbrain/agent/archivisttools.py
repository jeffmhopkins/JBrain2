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

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import scoped_session
from jbrain.models.archivist import ArchivistMemoryRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# A single memory document is the archivist's scratchpad; cap it so it stays a concise
# running summary rather than an unbounded log.
_MAX_CHARS = 20_000
# Where a refused save is told to land: far enough under the cap that the next session's
# additions fit, so the very next save isn't refused again.
_TARGET_CHARS = 16_000


def _too_long(size: int, refusals_before: int) -> str:
    """The refusal for an oversized save. It names a concrete cut, because "summarize and
    save again" produced passes that each trimmed ~200 chars of a 22k document — every one
    refused, every one minutes of generation. From the second refusal in a turn it closes
    saving outright: an unsaved memory costs nothing, an undelivered answer is the failure."""
    if refusals_before:
        return (
            f"NOT saved again ({size} chars, max {_MAX_CHARS}); your stored memory is "
            "unchanged. Stop trying to save in this turn — give Jeff your answer now. "
            "Shrink the memory in a later session."
        )
    return (
        f"NOT saved — {size} chars, max {_MAX_CHARS}; your stored memory is unchanged. "
        f"Cut at least {size - _TARGET_CHARS} chars (to {_TARGET_CHARS} or fewer) by dropping "
        "finished items, old dated tallies and superseded notes; trimming a few lines will be "
        "refused again. If you can't make that cut now, skip saving and give your answer."
    )


def _receipt(previous: str, content: str) -> str:
    """What the write actually DID, in the tool result. A write is a full replace, and the
    model has to hold that fact across a turn: asked whether it had clobbered the owner's
    notes, the archivist apologized for destroying a document its own read had just
    returned as empty. A receipt naming the before/after keeps its account of its memory
    grounded, and puts a real clobber in front of it while it can still re-save the merge."""
    if not previous:
        return "Memory saved — nothing was stored before, so this replaced an empty memory."
    return (
        "Memory saved. The previous document is GONE, replaced by this one "
        f"({len(previous)} chars → {len(content)}). If it dropped anything you meant to "
        "keep, save the merged version now."
    )


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
        return ToolOutput(
            content or "(your memory is empty — nothing saved yet)",
            result_brief=f"{len(content)} chars" if content else "empty",
        )

    async def archivist_memory_write(arguments: dict, ctx: ToolContext) -> str:
        content = str(arguments.get("content", ""))
        if len(content) > _MAX_CHARS:
            refusal = _too_long(len(content), len(ctx.memory_write_refusals))
            ctx.memory_write_refusals.append(len(content))
            return refusal
        if not ctx.session.principal_id:
            return "Can't save memory — this session has no owner principal."
        async with scoped_session(maker, ctx.session) as session:
            previous = await repo.read(session, ctx.session.principal_id)
            await repo.write(session, ctx.session.principal_id, content)
        # The size CHANGE, not the new size: a write here is a full replace, and a shrink
        # is the shape of the clobber this receipt exists to put in front of the model.
        return ToolOutput(
            _receipt(previous, content), result_brief=f"{len(previous)} → {len(content)} chars"
        )

    return {
        "archivist_memory_read": archivist_memory_read,
        "archivist_memory_write": archivist_memory_write,
    }
