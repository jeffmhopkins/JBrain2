"""Authentication flows over an abstract repository.

The repository protocol keeps these security-critical flows unit-testable at
100% coverage with a fake; the SQL implementation is exercised against real
Postgres in the integration suite.
"""

from dataclasses import dataclass
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


class AuthRepo(Protocol):
    async def find_active_principal_by_key_hash(self, key_hash: str) -> PrincipalInfo | None: ...

    async def find_active_device_principal_by_key_hash(
        self, key_hash: str
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


async def logout(repo: AuthRepo, token: str) -> None:
    if token:
        await repo.revoke_session(keys.hash_token(token))


async def rotate_owner_key(repo: AuthRepo) -> str:
    """Create (or replace) the owner principal; returns the new key exactly once.

    Revoking the previous owner principal also orphans its sessions, so a
    stolen-key scenario is fully recoverable from shell access.
    """
    key = keys.generate_owner_key()
    await repo.revoke_principals_of_kind("owner")
    await repo.create_principal("owner", keys.hash_key(key), "owner")
    return key
