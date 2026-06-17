"""Device provisioning over an RLS-scoped session.

A "device" is a `Subject(kind='device')` with a bound `Principal(kind='device_key')`
that authenticates OwnTracks ingest (Phase 7). Provisioning, rotation, and
revocation are owner-only: every method runs under the caller's owner
`SessionContext`, so the `subjects`/`principals` RLS policies (owner-only writes)
are the enforcement — not application politeness.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session


@dataclass(frozen=True)
class DeviceInfo:
    """A provisioned device. `id` is the device's subject id (its stable identity);
    `revoked` is True once it has no active key."""

    id: str
    label: str
    created_at: datetime
    revoked: bool


class DeviceRepo(Protocol):
    async def provision(self, ctx: SessionContext, *, label: str, key_hash: str) -> DeviceInfo: ...

    async def list(self, ctx: SessionContext) -> Sequence[DeviceInfo]: ...

    async def rotate(self, ctx: SessionContext, device_id: str, key_hash: str) -> bool: ...

    async def revoke(self, ctx: SessionContext, device_id: str) -> bool: ...


class SqlDeviceRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def provision(self, ctx: SessionContext, *, label: str, key_hash: str) -> DeviceInfo:
        sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
        async with scoped_session(self._maker, ctx) as session:
            created_at = (
                await session.execute(
                    text(
                        "INSERT INTO app.subjects (id, display_name, kind)"
                        " VALUES (:sid, :label, 'device') RETURNING created_at"
                    ),
                    {"sid": sid, "label": label},
                )
            ).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO app.principals (id, kind, subject_id, key_hash, label)"
                    " VALUES (:pid, 'device_key', :sid, :kh, :label)"
                ),
                {"pid": pid, "sid": sid, "kh": key_hash, "label": label},
            )
        return DeviceInfo(id=sid, label=label, created_at=created_at, revoked=False)

    async def list(self, ctx: SessionContext) -> Sequence[DeviceInfo]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT s.id, s.display_name, s.created_at,"
                        " bool_or(p.id IS NOT NULL) AS has_active_key"
                        " FROM app.subjects s"
                        " LEFT JOIN app.principals p"
                        "   ON p.subject_id = s.id AND p.kind = 'device_key'"
                        "   AND p.revoked_at IS NULL"
                        " WHERE s.kind = 'device'"
                        " GROUP BY s.id, s.display_name, s.created_at"
                        " ORDER BY s.created_at DESC"
                    )
                )
            ).all()
        return [
            DeviceInfo(
                id=str(r.id),
                label=r.display_name,
                created_at=r.created_at,
                revoked=not r.has_active_key,
            )
            for r in rows
        ]

    async def rotate(self, ctx: SessionContext, device_id: str, key_hash: str) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            if not await self._is_device(session, device_id):
                return False
            await self._revoke_keys(session, device_id)
            await session.execute(
                text(
                    "INSERT INTO app.principals (id, kind, subject_id, key_hash, label)"
                    " SELECT gen_random_uuid(), 'device_key', :sid, :kh, display_name"
                    " FROM app.subjects WHERE id = :sid"
                ),
                {"sid": device_id, "kh": key_hash},
            )
        return True

    async def revoke(self, ctx: SessionContext, device_id: str) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            if not await self._is_device(session, device_id):
                return False
            await self._revoke_keys(session, device_id)
        return True

    @staticmethod
    async def _is_device(session: AsyncSession, device_id: str) -> bool:
        return (
            await session.execute(
                text("SELECT 1 FROM app.subjects WHERE id = :sid AND kind = 'device'"),
                {"sid": device_id},
            )
        ).first() is not None

    @staticmethod
    async def _revoke_keys(session: AsyncSession, device_id: str) -> None:
        await session.execute(
            text(
                "UPDATE app.principals SET revoked_at = now()"
                " WHERE subject_id = :sid AND kind = 'device_key' AND revoked_at IS NULL"
            ),
            {"sid": device_id},
        )
