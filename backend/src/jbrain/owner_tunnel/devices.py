"""The phones enrolled for the owner tunnel (docs/plans/OWNER_TUNNEL_PLAN.md §3.4).

A phone is known by `key_hash`, the hash of its client key that Caddy verified and sends as
`X-JBrain-Device` (`keys.public_key_sha256`). Revoking one also revokes every owner session
bound to it, so nothing it held keeps working.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models import DeviceSession, OwnerDevice

_LOGIN = SessionContext(auth_context="login")
# A phone's last-seen time is stamped at most this often, not on every asset request.
_TOUCH_EVERY = timedelta(minutes=1)


@dataclass(frozen=True)
class OwnerDeviceInfo:
    id: str
    label: str
    key_hash: str
    enrolled_at: datetime
    last_seen_at: datetime | None
    last_ingress: str | None
    revoked_at: datetime | None


class OwnerDeviceRepo(Protocol):
    async def touch_active(self, key_hash: str, ingress: str) -> OwnerDeviceInfo | None:
        """The phone with this key if it is not revoked, stamping when it was last seen."""
        ...

    async def create(self, label: str, key_hash: str, public_key: bytes) -> OwnerDeviceInfo: ...

    async def list_all(self, ctx: SessionContext) -> list[OwnerDeviceInfo]: ...

    async def rename(self, ctx: SessionContext, device_id: str, label: str) -> bool: ...

    async def revoke(self, ctx: SessionContext, device_id: str) -> bool:
        """Revoke a phone and every session bound to it. False if there was none to revoke."""
        ...


def _info(row: OwnerDevice) -> OwnerDeviceInfo:
    return OwnerDeviceInfo(
        id=str(row.id),
        label=row.label,
        key_hash=row.key_hash,
        enrolled_at=row.enrolled_at,
        last_seen_at=row.last_seen_at,
        last_ingress=row.last_ingress,
        revoked_at=row.revoked_at,
    )


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class SqlOwnerDeviceRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def touch_active(self, key_hash: str, ingress: str) -> OwnerDeviceInfo | None:
        async with scoped_session(self._maker, _LOGIN) as session:
            row = (
                await session.execute(
                    select(OwnerDevice).where(
                        OwnerDevice.key_hash == key_hash, OwnerDevice.revoked_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            # Read before the stamp: the UPDATE expires the loaded row, and touching an
            # expired attribute afterwards would be a lazy load outside the async context.
            info = _info(row)
            await session.execute(
                update(OwnerDevice)
                .where(
                    OwnerDevice.id == row.id,
                    or_(
                        OwnerDevice.last_seen_at.is_(None),
                        OwnerDevice.last_seen_at < func.now() - _TOUCH_EVERY,
                        OwnerDevice.last_ingress.is_distinct_from(ingress),
                    ),
                )
                .values(last_seen_at=text("now()"), last_ingress=ingress)
            )
            return info

    async def create(self, label: str, key_hash: str, public_key: bytes) -> OwnerDeviceInfo:
        async with scoped_session(self._maker, _LOGIN) as session:
            row = OwnerDevice(label=label, key_hash=key_hash, public_key=public_key)
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _info(row)

    async def list_all(self, ctx: SessionContext) -> list[OwnerDeviceInfo]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(select(OwnerDevice).order_by(OwnerDevice.enrolled_at))
            ).scalars()
            return [_info(r) for r in rows]

    async def rename(self, ctx: SessionContext, device_id: str, label: str) -> bool:
        did = _uuid(device_id)
        if did is None:
            return False
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                update(OwnerDevice).where(OwnerDevice.id == did).values(label=label)
            )
            return bool(getattr(result, "rowcount", 0))

    async def revoke(self, ctx: SessionContext, device_id: str) -> bool:
        did = _uuid(device_id)
        if did is None:
            return False
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                update(OwnerDevice)
                .where(OwnerDevice.id == did, OwnerDevice.revoked_at.is_(None))
                .values(revoked_at=text("now()"))
            )
            if not getattr(result, "rowcount", 0):
                return False
            await session.execute(
                update(DeviceSession)
                .where(DeviceSession.device_id == did, DeviceSession.revoked_at.is_(None))
                .values(revoked_at=text("now()"))
            )
            return True
