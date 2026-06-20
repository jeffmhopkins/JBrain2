"""The owner family-membership repo (JBrain360 M7a).

Manages the `view_scope` rows for the single v1 family group, get-or-created by
name on first add. Owner-only: every call runs under the owner's context, and the
`family_group`/`view_scope` RLS (`is_full_owner`) is the real barrier.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

# The single v1 family group. (The schema allows many; the v1 UI curates one.)
_GROUP_NAME = "family"


@dataclass(frozen=True)
class FamilyMember:
    subject_id: str
    label: str
    added_at: datetime


class FamilyRepo(Protocol):
    async def members(self, ctx: SessionContext) -> list[FamilyMember]: ...
    async def add_member(self, ctx: SessionContext, subject_id: str) -> None: ...
    async def remove_member(self, ctx: SessionContext, subject_id: str) -> None: ...


class SqlFamilyRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def members(self, ctx: SessionContext) -> list[FamilyMember]:
        """The family roster: each member's subject, display label, and when added,
        newest first. Empty before any group/member exists."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT vs.member_subject_id::text AS sid,"
                        "   s.display_name AS label, vs.added_at"
                        " FROM app.view_scope vs"
                        " JOIN app.family_group g ON g.id = vs.group_id"
                        " JOIN app.subjects s ON s.id = vs.member_subject_id"
                        " WHERE g.name = :n"
                        " ORDER BY vs.added_at DESC"
                    ),
                    {"n": _GROUP_NAME},
                )
            ).all()
        return [FamilyMember(subject_id=r.sid, label=r.label, added_at=r.added_at) for r in rows]

    async def add_member(self, ctx: SessionContext, subject_id: str) -> None:
        """Add a subject to the family (idempotent), creating the group on first use.
        Owner-only by RLS — a non-owner context inserts nothing."""
        async with scoped_session(self._maker, ctx) as session:
            gid = await self._ensure_group(session)
            await session.execute(
                text(
                    "INSERT INTO app.view_scope (group_id, member_subject_id)"
                    " VALUES (cast(:g AS uuid), cast(:s AS uuid))"
                    " ON CONFLICT (group_id, member_subject_id) DO NOTHING"
                ),
                {"g": gid, "s": subject_id},
            )

    async def remove_member(self, ctx: SessionContext, subject_id: str) -> None:
        """Drop a subject from the family — the family-sees-family read path for it
        ends immediately (the next `viewer_may_see` returns false)."""
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "DELETE FROM app.view_scope vs"
                    " USING app.family_group g"
                    " WHERE vs.group_id = g.id AND g.name = :n"
                    "   AND vs.member_subject_id = cast(:s AS uuid)"
                ),
                {"n": _GROUP_NAME, "s": subject_id},
            )

    async def _ensure_group(self, session: AsyncSession) -> str:
        gid = (
            await session.execute(
                text("SELECT id::text FROM app.family_group WHERE name = :n LIMIT 1"),
                {"n": _GROUP_NAME},
            )
        ).scalar()
        if gid is not None:
            return gid
        return (
            await session.execute(
                text("INSERT INTO app.family_group (name) VALUES (:n) RETURNING id::text"),
                {"n": _GROUP_NAME},
            )
        ).scalar_one()
