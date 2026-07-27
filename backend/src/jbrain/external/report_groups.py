"""Owner-named folders the Research Library sorts reports into, and the move that files a
report into one.

The Reports-tab sibling of the Tasks surface's `task_groups` (migration 0149). Folders are
OWNER-ONLY metadata (`app.report_groups`, RLS `is_owner()`), so every call flows through an
owner `SessionContext` — never jerv's narrowed corpus scope. A report's membership lives on
`research_reports.group_id` (NULL = the trailing "Ungrouped" section); moving a report is a
plain UPDATE of that opaque column under the full-owner context, the same trusted executor
that owns the report delete. Deleting a folder SET NULLs its reports (they fall back to
Ungrouped), never dropping them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session


@dataclass(frozen=True)
class ReportGroup:
    """One report folder — a name + display `position` (report membership lives on the
    report row's `group_id`, like a task group)."""

    id: str
    name: str
    position: int


def _row(row: object) -> ReportGroup:
    return ReportGroup(id=str(row.id), name=row.name, position=int(row.position))  # type: ignore[attr-defined]


async def list_report_groups(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext
) -> list[ReportGroup]:
    """The owner's folders, in display order (position, then creation). Owner-only (RLS)."""
    async with scoped_session(maker, ctx) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, name, position FROM app.report_groups"
                    " ORDER BY position, created_at"
                )
            )
        ).all()
    return [_row(r) for r in rows]


async def create_report_group(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, *, name: str
) -> ReportGroup:
    """Append a new folder past the current tail (its `position` = max + 1, or 0 when
    empty), so folders keep the order they were created in."""
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.report_groups (principal_id, name, position)"
                    " VALUES (cast(:pid AS uuid), :name,"
                    "  coalesce((SELECT max(position) + 1 FROM app.report_groups), 0))"
                    " RETURNING id, name, position"
                ),
                {"pid": ctx.principal_id, "name": name},
            )
        ).one()
    return _row(row)


async def rename_report_group(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, group_id: str, *, name: str
) -> ReportGroup | None:
    """Rename a folder; None when the id isn't the owner's (RLS hides it)."""
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "UPDATE app.report_groups SET name = :name, updated_at = now()"
                    " WHERE id = cast(:gid AS uuid) RETURNING id, name, position"
                ),
                {"gid": group_id, "name": name},
            )
        ).first()
    return _row(row) if row is not None else None


async def delete_report_group(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, group_id: str
) -> None:
    """Drop a folder. Its reports are SET NULL by the FK (they fall to Ungrouped), never
    deleted — losing a folder must not lose the owner's reports. Idempotent."""
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text("DELETE FROM app.report_groups WHERE id = cast(:gid AS uuid)"),
            {"gid": group_id},
        )


async def report_group_exists(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, group_id: str
) -> bool:
    """Whether `group_id` is a folder the owner owns — the move endpoint's guard so a report
    is never filed into a stranger's / non-existent folder. A non-uuid id is a clean False."""
    try:
        uuid.UUID(group_id)
    except ValueError:
        return False
    async with scoped_session(maker, ctx) as session:
        found = (
            await session.execute(
                text("SELECT 1 FROM app.report_groups WHERE id = cast(:gid AS uuid)"),
                {"gid": group_id},
            )
        ).first()
    return found is not None


async def set_report_group(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    report_id: str,
    group_id: str | None,
) -> bool:
    """File `report_id` into `group_id` (NULL = Ungrouped). Runs under the OWNER's context —
    the trusted executor, never jerv — so it passes the report's `external`-domain firewall
    while writing owner-only membership. Returns True when a report row was actually updated
    (idempotent: an already-gone report is a harmless no-op). The caller validates that the
    report is in scope and the folder is the owner's before calling."""
    async with scoped_session(maker, ctx) as session:
        updated = (
            await session.execute(
                text(
                    "UPDATE app.research_reports SET group_id = cast(:gid AS uuid)"
                    " WHERE id = cast(:rid AS uuid) RETURNING id"
                ),
                {"gid": group_id, "rid": report_id},
            )
        ).first()
    return updated is not None
