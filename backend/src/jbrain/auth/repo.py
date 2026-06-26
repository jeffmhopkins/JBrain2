"""SQL implementation of the auth repository.

Runs under the 'login'/'bootstrap' auth contexts: RLS policies on principals
and device_sessions only open up for these GUC values, so this module is the
sole code path that can touch credentials before a principal context exists.
"""

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.auth.service import CapabilityToken, PrincipalInfo
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
            return _principal_info(row)

    async def find_active_device_principal_by_key_hash(self, key_hash: str) -> PrincipalInfo | None:
        """Look up a device key, kind-filtered in SQL so an owner or capability key
        can never authenticate on the device path (no kind confusion)."""
        async with scoped_session(self._maker, _LOGIN) as session:
            row = (
                await session.execute(
                    select(Principal).where(
                        Principal.key_hash == key_hash,
                        Principal.kind == "device_key",
                        Principal.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _principal_info(row)

    async def find_active_device_principal_by_id(self, principal_id: str) -> PrincipalInfo | None:
        """Resolve a device principal id (an MQTT topic's owner segment) to its
        subject. The consumer trusts that segment because the broker ACL only lets a
        device publish under its own id; this kind-filtered, revocation-filtered read
        turns the id into the subject a fix is pinned to (and drops a malformed id)."""
        try:
            pid = uuid.UUID(principal_id)
        except ValueError:
            return None
        async with scoped_session(self._maker, _LOGIN) as session:
            row = (
                await session.execute(
                    select(Principal).where(
                        Principal.id == pid,
                        Principal.kind == "device_key",
                        Principal.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _principal_info(row)

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
                        # A time-boxed principal (a jcode share link) stops authenticating
                        # the moment it lapses — the cookie can't outlive the share's
                        # expiry. Owner/device principals have NULL expiry, so unaffected.
                        or_(Principal.expires_at.is_(None), Principal.expires_at > func.now()),
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
            return _principal_info(row)

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

    async def create_principal(
        self, kind: str, key_hash: str, label: str, subject_id: str | None = None
    ) -> None:
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            session.add(
                Principal(
                    kind=kind,
                    key_hash=key_hash,
                    label=label,
                    subject_id=uuid.UUID(subject_id) if subject_id else None,
                )
            )

    async def create_capability(
        self, key_hash: str, label: str, expires_at: datetime | None
    ) -> CapabilityToken:
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            row = Principal(
                kind="capability_token", key_hash=key_hash, label=label, expires_at=expires_at
            )
            session.add(row)
            await session.flush()
            return _capability_token(row)

    async def find_active_capability_by_key_hash(self, key_hash: str) -> PrincipalInfo | None:
        """Resolve a debug-console bearer key, kind-filtered so an owner or device
        key presented here never authenticates. Enforces revocation AND a live
        expiry, and stamps last_used_at on the hit so the owner's list shows
        liveness. An unknown / revoked / lapsed / wrong-kind key returns None."""
        async with scoped_session(self._maker, _LOGIN) as session:
            row = (
                await session.execute(
                    select(Principal).where(
                        Principal.key_hash == key_hash,
                        Principal.kind == "capability_token",
                        Principal.revoked_at.is_(None),
                        Principal.suspended_at.is_(None),
                        or_(Principal.expires_at.is_(None), Principal.expires_at > func.now()),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            info = _principal_info(row)
        # The principals UPDATE policy admits only owner/bootstrap (the 'login'
        # context may read credentials but not write them), so the liveness stamp
        # runs under bootstrap — the same context that mints/revokes the token.
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            await session.execute(
                update(Principal).where(Principal.id == row.id).values(last_used_at=text("now()"))
            )
        return info

    async def list_capabilities(self) -> list[CapabilityToken]:
        async with scoped_session(self._maker, _LOGIN) as session:
            rows = (
                await session.execute(
                    select(Principal)
                    .where(Principal.kind == "capability_token")
                    .order_by(Principal.created_at.desc())
                )
            ).scalars()
            return [_capability_token(row) for row in rows]

    async def revoke_capability(self, capability_id: str) -> bool:
        try:
            cid = uuid.UUID(capability_id)
        except ValueError:
            return False
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            result = await session.execute(
                update(Principal)
                .where(
                    Principal.id == cid,
                    Principal.kind == "capability_token",
                    Principal.revoked_at.is_(None),
                )
                .values(revoked_at=text("now()"))
            )
            return (cast("CursorResult[Any]", result).rowcount or 0) > 0

    async def suspend_capability(self, capability_id: str) -> bool:
        """Pause a live token (set suspended_at) so it stops authenticating. No-op
        on an unknown / revoked / already-suspended token (reports no row changed)."""
        try:
            cid = uuid.UUID(capability_id)
        except ValueError:
            return False
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            result = await session.execute(
                update(Principal)
                .where(
                    Principal.id == cid,
                    Principal.kind == "capability_token",
                    Principal.revoked_at.is_(None),
                    Principal.suspended_at.is_(None),
                )
                .values(suspended_at=text("now()"))
            )
            return (cast("CursorResult[Any]", result).rowcount or 0) > 0

    async def resume_capability(self, capability_id: str) -> bool:
        """Clear a suspension so a paused token authenticates again. Owner-only — a
        suspended token cannot reach this path itself. No-op on an unknown / revoked
        / not-suspended token (a revoked token stays dead: the revoked_at filter)."""
        try:
            cid = uuid.UUID(capability_id)
        except ValueError:
            return False
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            result = await session.execute(
                update(Principal)
                .where(
                    Principal.id == cid,
                    Principal.kind == "capability_token",
                    Principal.revoked_at.is_(None),
                    Principal.suspended_at.is_not(None),
                )
                .values(suspended_at=None)
            )
            return (cast("CursorResult[Any]", result).rowcount or 0) > 0

    async def create_jcode_share(
        self, key_hash: str, label: str, session_id: str, expires_at: datetime
    ) -> CapabilityToken:
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            row = Principal(
                kind="jcode_share_link",
                key_hash=key_hash,
                label=label,
                jcode_session_id=session_id,
                expires_at=expires_at,
            )
            session.add(row)
            await session.flush()
            return _capability_token(row)

    async def find_active_jcode_share_by_key_hash(self, key_hash: str) -> PrincipalInfo | None:
        """Resolve a share-link secret, kind-filtered so an owner / device / debug key
        presented here never authenticates. Enforces revocation AND a live expiry, and
        stamps last_used_at. The returned PrincipalInfo carries jcode_session_id, the
        scope the access gate checks. Unknown / revoked / lapsed / wrong-kind → None."""
        async with scoped_session(self._maker, _LOGIN) as session:
            row = (
                await session.execute(
                    select(Principal).where(
                        Principal.key_hash == key_hash,
                        Principal.kind == "jcode_share_link",
                        Principal.revoked_at.is_(None),
                        or_(Principal.expires_at.is_(None), Principal.expires_at > func.now()),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            info = _principal_info(row)
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            await session.execute(
                update(Principal).where(Principal.id == row.id).values(last_used_at=text("now()"))
            )
        return info

    async def list_jcode_shares(self, session_id: str) -> list[CapabilityToken]:
        """The non-revoked share links for one session, newest first (owner's list)."""
        async with scoped_session(self._maker, _LOGIN) as session:
            rows = (
                await session.execute(
                    select(Principal)
                    .where(
                        Principal.kind == "jcode_share_link",
                        Principal.jcode_session_id == session_id,
                        Principal.revoked_at.is_(None),
                    )
                    .order_by(Principal.created_at.desc())
                )
            ).scalars()
            return [_capability_token(row) for row in rows]

    async def revoke_jcode_share(self, share_id: str, session_id: str) -> bool:
        """Revoke a share link — scoped to its session, so the owner can't revoke a
        share of a session they didn't name (defence in depth). No-op (False) on an
        unknown / already-revoked / wrong-session id."""
        try:
            pid = uuid.UUID(share_id)
        except ValueError:
            return False
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            result = await session.execute(
                update(Principal)
                .where(
                    Principal.id == pid,
                    Principal.kind == "jcode_share_link",
                    Principal.jcode_session_id == session_id,
                    Principal.revoked_at.is_(None),
                )
                .values(revoked_at=text("now()"))
            )
            return (cast("CursorResult[Any]", result).rowcount or 0) > 0


def _capability_token(row: Principal) -> CapabilityToken:
    return CapabilityToken(
        id=str(row.id),
        label=row.label,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        suspended_at=row.suspended_at,
    )


def _principal_info(row: Principal) -> PrincipalInfo:
    return PrincipalInfo(
        id=str(row.id),
        kind=row.kind,
        label=row.label,
        subject_id=str(row.subject_id) if row.subject_id is not None else "",
        jcode_session_id=row.jcode_session_id or "",
    )
