"""jmolt's scratchpad tools (docs/plans/JMOLT_PLAN.md, W2).

`scratch_list` / `scratch_read` / `scratch_write` over `models/jmolt.JmoltScratchRepo`.
jmolt-only (in JMOLT_TOOLS), `web`-gated. Each handler runs under `ctx.session` — the
nightly run's jmolt-scoped context (`auth_context='jmolt'`), so the M19 RLS split, not
this code, is the firewall. The quota (16 files / 128 KB / 24 KB) is enforced by the
repo; an over-quota write returns the repo's plain-language message. Every change also
snapshots to the append-only archive (repo does this).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.db.session import scoped_session
from jbrain.models.jmolt import MAX_FILES, MAX_TOTAL_BYTES, JmoltScratchRepo, QuotaError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def build_jmolt_scratch_handlers(
    maker: async_sessionmaker[AsyncSession],
) -> dict[str, ToolHandler]:
    repo = JmoltScratchRepo()

    async def scratch_list(_a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        if not pid:
            return "Can't reach your files — this session has no owner principal."
        async with scoped_session(maker, ctx.session) as s:
            files = await repo.list_files(s, pid)
        if not files:
            return "(your scratchpad is empty — you have written nothing yet)"
        used = sum(f.bytes for f in files)
        lines = [f"- {f.filename}  ({f.bytes} bytes)" for f in files]
        return (
            f"Your files ({len(files)}/{MAX_FILES} files, {used}/{MAX_TOTAL_BYTES} bytes used):\n"
            + "\n".join(lines)
        )

    async def scratch_read(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        fn = str(a.get("filename", "")).strip()
        if not fn:
            return "scratch_read needs a `filename`."
        if not pid:
            return "Can't reach your files — this session has no owner principal."
        async with scoped_session(maker, ctx.session) as s:
            content = await repo.read(s, pid, fn)
        return content if content is not None else f"You have no file named {fn!r}."

    async def scratch_write(a: dict, ctx: ToolContext) -> str:
        pid = ctx.session.principal_id
        fn = str(a.get("filename", "")).strip()
        mode = str(a.get("mode", "save")).strip().lower()
        if not fn:
            return "scratch_write needs a `filename`."
        if not pid:
            return "Can't reach your files — this session has no owner principal."
        if mode == "delete":
            async with scoped_session(maker, ctx.session) as s:
                deleted = await repo.delete(s, pid, fn)
            return f"Deleted {fn!r}." if deleted else f"You have no file named {fn!r}."
        content = str(a.get("content", ""))
        try:
            async with scoped_session(maker, ctx.session) as s:
                await repo.write(s, pid, fn, content)
        except QuotaError as exc:
            return str(exc)
        return f"Saved {fn!r}."

    return {
        "scratch_list": scratch_list,
        "scratch_read": scratch_read,
        "scratch_write": scratch_write,
    }
