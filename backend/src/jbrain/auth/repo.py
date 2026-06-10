"""SQL implementation of the auth repository.

Runs under the 'login'/'bootstrap' auth contexts: RLS policies on principals
and device_sessions only open up for these GUC values, so this module is the
sole code path that can touch credentials before a principal context exists.
"""

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models import DeviceSession, Principal

_LOGIN = SessionContext(auth_context="login")
_BOOTSTRAP = SessionContext(auth_context="bootstrap")


class SqlAuthRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def find_active_principal_by_key_hash(self, key_hash: str) -> PrincipalInfo | None:
        async with scoped_session(self._maker, _LOGIN) as session:
            row = (
                await session.execute(
                    select(Principal).where(
                        Principal.key_hash == key_hash, Principal.revoked_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return PrincipalInfo(id=str(row.id), kind=row.kind, label=row.label)

    async def create_session(self, principal_id: str, token_hash: str, label: str) -> None:
        async with scoped_session(self._maker, _LOGIN) as session:
            session.add(
                DeviceSession(principal_id=principal_id, token_hash=token_hash, label=label)
            )

    async def find_principal_by_session_token_hash(self, token_hash: str) -> PrincipalInfo | None:
        async with scoped_session(self._maker, _LOGIN) as session:
            row = (
                await session.execute(
                    select(Principal)
                    .join(DeviceSession, DeviceSession.principal_id == Principal.id)
                    .where(
                        DeviceSession.token_hash == token_hash,
                        DeviceSession.revoked_at.is_(None),
                        Principal.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            await session.execute(
                update(DeviceSession)
                .where(DeviceSession.token_hash == token_hash)
                .values(last_seen_at=text("now()"))
            )
            return PrincipalInfo(id=str(row.id), kind=row.kind, label=row.label)

    async def revoke_session(self, token_hash: str) -> None:
        async with scoped_session(self._maker, _LOGIN) as session:
            await session.execute(
                update(DeviceSession)
                .where(DeviceSession.token_hash == token_hash, DeviceSession.revoked_at.is_(None))
                .values(revoked_at=text("now()"))
            )

    async def revoke_principals_of_kind(self, kind: str) -> None:
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            await session.execute(
                update(Principal)
                .where(Principal.kind == kind, Principal.revoked_at.is_(None))
                .values(revoked_at=text("now()"))
            )

    async def create_principal(self, kind: str, key_hash: str, label: str) -> None:
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            session.add(Principal(kind=kind, key_hash=key_hash, label=label))
