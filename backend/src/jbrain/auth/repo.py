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

from jbrain.auth.service import CapabilityToken, ExternalSession, PrincipalInfo
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

    async def has_active_capability(self) -> bool:
        """True while any debug-console token is live (unrevoked, unsuspended, unexpired) —
        the signal that an owner-authorized debug session is open. Used to switch on verbose
        diagnostics (the wall's per-clip TTS trace) for the session's duration, without an
        env flag. Same liveness predicate as the auth path, minus the key match + stamp."""
        async with scoped_session(self._maker, _LOGIN) as session:
            row = (
                await session.execute(
                    select(Principal.id)
                    .where(
                        Principal.kind == "capability_token",
                        Principal.revoked_at.is_(None),
                        Principal.suspended_at.is_(None),
                        or_(Principal.expires_at.is_(None), Principal.expires_at > func.now()),
                    )
                    .limit(1)
                )
            ).first()
            return row is not None

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

    async def consume_jcode_share(self, key_hash: str) -> PrincipalInfo | None:
        """Atomically claim a share link for single use: stamp ``redeemed_at`` in ONE
        conditional UPDATE that only matches an active, not-yet-redeemed link, and return
        its principal — or None if it was unknown / revoked / lapsed / already claimed.

        The ``redeemed_at IS NULL`` guard in the WHERE clause is the single-use gate: two
        concurrent redeems both pass an earlier read, but only the first UPDATE matches a
        row, so exactly one browser binds the link and the loser gets None. Runs under
        bootstrap (the principals UPDATE policy admits only owner/bootstrap), the same
        context that mints/revokes a share."""
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            row = (
                await session.execute(
                    update(Principal)
                    .where(
                        Principal.key_hash == key_hash,
                        Principal.kind == "jcode_share_link",
                        Principal.revoked_at.is_(None),
                        Principal.redeemed_at.is_(None),
                        or_(Principal.expires_at.is_(None), Principal.expires_at > func.now()),
                    )
                    .values(redeemed_at=text("now()"))
                    .returning(Principal)
                )
            ).scalar_one_or_none()
            return _principal_info(row) if row is not None else None

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

    async def create_external_llm(
        self, key_hash: str, label: str, expires_at: datetime | None
    ) -> ExternalSession:
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            row = Principal(
                kind="external_llm", key_hash=key_hash, label=label, expires_at=expires_at
            )
            session.add(row)
            await session.flush()
            return _external_session(row)

    async def find_active_external_llm_by_key_hash(self, key_hash: str) -> PrincipalInfo | None:
        """Resolve an external-LLM bearer secret, kind-filtered so no other credential
        authenticates here. Enforces revocation, the suspend (off) toggle, AND a live
        expiry, and stamps last_used_at. Unknown / revoked / suspended / lapsed / wrong-
        kind → None."""
        async with scoped_session(self._maker, _LOGIN) as session:
            row = (
                await session.execute(
                    select(Principal).where(
                        Principal.key_hash == key_hash,
                        Principal.kind == "external_llm",
                        Principal.revoked_at.is_(None),
                        Principal.suspended_at.is_(None),
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

    async def list_external_llm(self) -> list[ExternalSession]:
        async with scoped_session(self._maker, _LOGIN) as session:
            rows = (
                await session.execute(
                    select(Principal)
                    .where(Principal.kind == "external_llm", Principal.revoked_at.is_(None))
                    .order_by(Principal.created_at.desc())
                )
            ).scalars()
            return [_external_session(row) for row in rows]

    async def set_external_llm_enabled(self, session_id: str, enabled: bool) -> bool:
        """Flip the on/off toggle: enabled clears suspended_at, disabled sets it. No-op on
        an unknown / revoked id (reports no row changed)."""
        try:
            pid = uuid.UUID(session_id)
        except ValueError:
            return False
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            result = await session.execute(
                update(Principal)
                .where(
                    Principal.id == pid,
                    Principal.kind == "external_llm",
                    Principal.revoked_at.is_(None),
                )
                .values(suspended_at=None if enabled else text("now()"))
            )
            return (cast("CursorResult[Any]", result).rowcount or 0) > 0

    async def revoke_external_llm(self, session_id: str) -> bool:
        try:
            pid = uuid.UUID(session_id)
        except ValueError:
            return False
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            result = await session.execute(
                update(Principal)
                .where(
                    Principal.id == pid,
                    Principal.kind == "external_llm",
                    Principal.revoked_at.is_(None),
                )
                .values(revoked_at=text("now()"))
            )
            return (cast("CursorResult[Any]", result).rowcount or 0) > 0

    async def add_external_usage(self, session_id: str, in_tokens: int, out_tokens: int) -> None:
        """Add one call's token usage to the cumulative counters (and bump the request
        count + last_used_at). Best-effort metering: a bad id simply matches no row."""
        try:
            pid = uuid.UUID(session_id)
        except ValueError:
            return
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            await session.execute(
                update(Principal)
                .where(Principal.id == pid, Principal.kind == "external_llm")
                .values(
                    ext_in_tokens=Principal.ext_in_tokens + in_tokens,
                    ext_out_tokens=Principal.ext_out_tokens + out_tokens,
                    ext_requests=Principal.ext_requests + 1,
                    last_used_at=text("now()"),
                )
            )


def _capability_token(row: Principal) -> CapabilityToken:
    return CapabilityToken(
        id=str(row.id),
        label=row.label,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        suspended_at=row.suspended_at,
        redeemed_at=row.redeemed_at,
    )


def _external_session(row: Principal) -> ExternalSession:
    return ExternalSession(
        id=str(row.id),
        label=row.label,
        enabled=row.suspended_at is None,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        in_tokens=row.ext_in_tokens,
        out_tokens=row.ext_out_tokens,
        requests=row.ext_requests,
    )


def _principal_info(row: Principal) -> PrincipalInfo:
    return PrincipalInfo(
        id=str(row.id),
        kind=row.kind,
        label=row.label,
        subject_id=str(row.subject_id) if row.subject_id is not None else "",
        jcode_session_id=row.jcode_session_id or "",
    )
