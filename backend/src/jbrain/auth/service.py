"""Authentication flows over an abstract repository.

The repository protocol keeps these security-critical flows unit-testable at
100% coverage with a fake; the SQL implementation is exercised against real
Postgres in the integration suite.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from jbrain.auth import keys


class InvalidCredentials(Exception):
    pass


@dataclass(frozen=True)
class PrincipalInfo:
    id: str
    kind: str
    label: str
    # The subject a credential is bound to ("" for the owner, who has no subject).
    # Phase 7 device keys carry their device subject so a device session can pin
    # its row visibility to that subject (jbrain.db.session.device_context).
    subject_id: str = ""
    # The single code-mode session a jcode_share_link is scoped to ("" for every
    # other kind). The jcode access gate checks this against the route's session id
    # so a share grant can never reach another session.
    jcode_session_id: str = ""


@dataclass(frozen=True)
class CapabilityToken:
    """A debug-console capability token as the owner's management list sees it.
    Carries no secret — the key is shown exactly once, at mint."""

    id: str
    label: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    # Set while the token is paused (reversible); None when active. A suspended
    # token fails auth but the owner can resume it.
    suspended_at: datetime | None = None


class AuthRepo(Protocol):
    async def find_active_principal_by_key_hash(self, key_hash: str) -> PrincipalInfo | None: ...

    async def find_active_device_principal_by_key_hash(
        self, key_hash: str
    ) -> PrincipalInfo | None: ...

    async def find_active_device_principal_by_id(
        self, principal_id: str
    ) -> PrincipalInfo | None: ...

    async def create_session(self, principal_id: str, token_hash: str, label: str) -> None: ...

    async def find_principal_by_session_token_hash(
        self, token_hash: str
    ) -> PrincipalInfo | None: ...

    async def revoke_session(self, token_hash: str) -> None: ...

    async def revoke_principals_of_kind(self, kind: str) -> None: ...

    async def create_principal(
        self, kind: str, key_hash: str, label: str, subject_id: str | None = None
    ) -> None: ...

    async def create_capability(
        self, key_hash: str, label: str, expires_at: datetime | None
    ) -> CapabilityToken: ...

    async def find_active_capability_by_key_hash(self, key_hash: str) -> PrincipalInfo | None: ...

    async def list_capabilities(self) -> list[CapabilityToken]: ...

    async def revoke_capability(self, capability_id: str) -> bool: ...

    async def suspend_capability(self, capability_id: str) -> bool: ...

    async def resume_capability(self, capability_id: str) -> bool: ...

    async def create_jcode_share(
        self, key_hash: str, label: str, session_id: str, expires_at: datetime
    ) -> CapabilityToken: ...

    async def find_active_jcode_share_by_key_hash(self, key_hash: str) -> PrincipalInfo | None: ...

    async def list_jcode_shares(self, session_id: str) -> list[CapabilityToken]: ...

    async def revoke_jcode_share(self, share_id: str, session_id: str) -> bool: ...


async def login(repo: AuthRepo, owner_key: str, device_label: str) -> str:
    """Exchange an owner key for a device session token."""
    principal = await repo.find_active_principal_by_key_hash(keys.hash_key(owner_key))
    if principal is None:
        raise InvalidCredentials
    token = keys.generate_session_token()
    await repo.create_session(principal.id, keys.hash_token(token), device_label)
    return token


async def authenticate(repo: AuthRepo, token: str) -> PrincipalInfo | None:
    if not token:
        return None
    return await repo.find_principal_by_session_token_hash(keys.hash_token(token))


async def authenticate_device(repo: AuthRepo, key: str) -> PrincipalInfo | None:
    """Resolve an OwnTracks HTTP Basic password (the device key) to its principal.

    Kind-filtered in SQL (`find_active_device_principal_by_key_hash`) so an owner
    or capability key presented on the device path never authenticates — the
    device surface and the owner-cookie surface can never be conflated. Security
    rests on 256-bit key entropy + SHA-256 + the `revoked_at IS NULL` filter; an
    unknown/revoked/wrong-kind key returns None (the caller 401s, writing nothing).
    """
    if not key:
        return None
    return await repo.find_active_device_principal_by_key_hash(keys.hash_key(key))


async def logout(repo: AuthRepo, token: str) -> None:
    if token:
        await repo.revoke_session(keys.hash_token(token))


async def mint_dashboard_session(repo: AuthRepo, device_key: str) -> str | None:
    """Exchange a device key for a dashboard session token (the WebView cookie).

    The member dashboard authenticates the *device*, not the owner: the key is
    verified exactly like the MQTT / OwnTracks path — kind-filtered, saltless
    SHA-256, `revoked_at IS NULL` — then a session token is minted bound to that
    device principal (so the cookie carries the device's subject + view-scope).
    An owner or capability key never resolves through the device lookup, so this
    surface can mint a *member* session only, never owner authority (L4). Returns
    None on an unknown / revoked / wrong-kind key (the caller 401s)."""
    principal = await authenticate_device(repo, device_key)
    if principal is None:
        return None
    token = keys.generate_session_token()
    await repo.create_session(principal.id, keys.hash_token(token), principal.label)
    return token


async def mint_capability(
    repo: AuthRepo, label: str, ttl_hours: float
) -> tuple[str, CapabilityToken]:
    """Mint a debug-console capability token; returns the secret exactly once
    alongside its management record. The secret travels embedded in the debug
    payload, so it is hashed like a session token (plain SHA-256), never the
    paper-key path. A non-positive TTL is meaningless — the caller validates."""
    key = keys.generate_capability_key()
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    record = await repo.create_capability(keys.hash_token(key), label, expires_at)
    return key, record


async def authenticate_capability(repo: AuthRepo, key: str) -> PrincipalInfo | None:
    """Resolve a debug-console bearer key to its principal, or None.

    Kind-filtered in SQL so an owner or device key presented here never
    authenticates (no kind confusion, mirroring the device path). The repo also
    enforces `revoked_at IS NULL` AND a live `expires_at`, and stamps `last_used_at`
    — so a revoked or lapsed token fails closed and the caller 401s, writing nothing.
    """
    if not key:
        return None
    return await repo.find_active_capability_by_key_hash(keys.hash_token(key))


async def mint_jcode_share(
    repo: AuthRepo, session_id: str, label: str, ttl_hours: float
) -> tuple[str, CapabilityToken]:
    """Mint a jcode share link bound to ONE code-mode session; returns the secret
    exactly once alongside its management record. Same shape as a debug capability
    (256-bit key, SHA-256 hashed, time-boxed) but kind-tagged `jcode_share_link` and
    scoped to ``session_id`` so it can never reach another session. The caller
    validates the TTL (a non-positive box is meaningless)."""
    key = keys.generate_capability_key()
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    record = await repo.create_jcode_share(keys.hash_token(key), label, session_id, expires_at)
    return key, record


async def redeem_jcode_share(repo: AuthRepo, key: str) -> tuple[str, str] | None:
    """Exchange a share-link secret for ``(session_cookie_token, session_id)``, or None.

    Validates the secret in SQL (kind-filtered + revocation/expiry-enforced — a revoked,
    lapsed, or wrong-kind key returns None), then mints a session bound to the share
    principal so the browser carries the share's scope on every subsequent request —
    exactly as `mint_dashboard_session` does for a device key, but the resulting cookie
    reaches ONLY this one session's operational routes (the jcode access gate), never
    owner or member surfaces."""
    principal = (
        await repo.find_active_jcode_share_by_key_hash(keys.hash_token(key)) if key else None
    )
    if principal is None:
        return None
    token = keys.generate_session_token()
    await repo.create_session(principal.id, keys.hash_token(token), principal.label)
    return token, principal.jcode_session_id


async def rotate_owner_key(repo: AuthRepo) -> str:
    """Create (or replace) the owner principal; returns the new key exactly once.

    Revoking the previous owner principal also orphans its sessions, so a
    stolen-key scenario is fully recoverable from shell access.
    """
    key = keys.generate_owner_key()
    await repo.revoke_principals_of_kind("owner")
    await repo.create_principal("owner", keys.hash_key(key), "owner")
    return key
