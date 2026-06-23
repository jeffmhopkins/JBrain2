"""Capability-token (debug console) auth flows with the repo faked.

The SQL repo's expiry/revocation filtering is exercised against real Postgres in
test_capability_pg; here we pin the service-level contract: mint returns the secret
once, authenticate is kind-filtered and honours expiry + revocation, and the
management list/revoke behave."""

import pytest

from jbrain.auth import keys
from jbrain.auth import service as auth_service
from tests.unit.fakes import FakeAuthRepo


@pytest.mark.asyncio
async def test_mint_returns_secret_and_record() -> None:
    repo = FakeAuthRepo()
    key, record = await auth_service.mint_capability(repo, "claude debug", ttl_hours=24)
    assert key  # the secret, shown once
    assert record.label == "claude debug"
    assert record.expires_at is not None and record.revoked_at is None
    # Stored as a plain SHA-256 (the session-token path), never the paper-key hash.
    stored = repo.principals[0]
    assert stored.kind == "capability_token"
    assert stored.key_hash == keys.hash_token(key)


@pytest.mark.asyncio
async def test_authenticate_resolves_live_token_and_stamps_use() -> None:
    repo = FakeAuthRepo()
    key, record = await auth_service.mint_capability(repo, "debug", ttl_hours=24)
    principal = await auth_service.authenticate_capability(repo, key)
    assert principal is not None
    assert principal.id == record.id and principal.kind == "capability_token"
    # last_used_at is stamped on the hit so the owner list shows liveness.
    assert repo.principals[0].last_used_at is not None


@pytest.mark.asyncio
async def test_authenticate_rejects_empty_unknown_and_wrong_kind() -> None:
    repo = FakeAuthRepo()
    # An owner key minted on the owner path must NOT authenticate as a capability.
    owner_key = await auth_service.rotate_owner_key(repo)
    assert await auth_service.authenticate_capability(repo, "") is None
    assert await auth_service.authenticate_capability(repo, "nope") is None
    assert await auth_service.authenticate_capability(repo, owner_key) is None


@pytest.mark.asyncio
async def test_authenticate_rejects_expired_token() -> None:
    repo = FakeAuthRepo()
    key, _ = await auth_service.mint_capability(repo, "debug", ttl_hours=-1)  # already lapsed
    assert await auth_service.authenticate_capability(repo, key) is None


@pytest.mark.asyncio
async def test_revoke_blocks_future_auth_and_is_idempotent() -> None:
    repo = FakeAuthRepo()
    key, record = await auth_service.mint_capability(repo, "debug", ttl_hours=24)
    assert await repo.revoke_capability(record.id) is True
    assert await auth_service.authenticate_capability(repo, key) is None
    # A second revoke (or an unknown id) reports no row changed.
    assert await repo.revoke_capability(record.id) is False
    assert await repo.revoke_capability("00000000-0000-0000-0000-000000000000") is False


@pytest.mark.asyncio
async def test_list_capabilities_reflects_state() -> None:
    repo = FakeAuthRepo()
    _, a = await auth_service.mint_capability(repo, "a", ttl_hours=24)
    _, b = await auth_service.mint_capability(repo, "b", ttl_hours=24)
    await repo.revoke_capability(a.id)
    listed = {t.id: t for t in await repo.list_capabilities()}
    assert set(listed) == {a.id, b.id}
    assert listed[a.id].revoked_at is not None
    assert listed[b.id].revoked_at is None


@pytest.mark.asyncio
async def test_suspend_blocks_auth_then_resume_restores_it() -> None:
    repo = FakeAuthRepo()
    key, record = await auth_service.mint_capability(repo, "debug", ttl_hours=24)
    assert await auth_service.authenticate_capability(repo, key) is not None

    # Suspend freezes the token: it no longer authenticates, and the list shows it.
    assert await repo.suspend_capability(record.id) is True
    assert await auth_service.authenticate_capability(repo, key) is None
    assert (await repo.list_capabilities())[0].suspended_at is not None
    # Idempotent: a second suspend changes nothing.
    assert await repo.suspend_capability(record.id) is False

    # Resume clears the pause and the same key works again.
    assert await repo.resume_capability(record.id) is True
    assert await auth_service.authenticate_capability(repo, key) is not None
    assert (await repo.list_capabilities())[0].suspended_at is None
    # Resuming an already-active token is a no-op.
    assert await repo.resume_capability(record.id) is False


@pytest.mark.asyncio
async def test_revoked_token_cannot_be_suspended_or_resumed() -> None:
    repo = FakeAuthRepo()
    _, record = await auth_service.mint_capability(repo, "debug", ttl_hours=24)
    assert await repo.revoke_capability(record.id) is True
    # A revoked token is permanently dead: neither lifecycle op touches it.
    assert await repo.suspend_capability(record.id) is False
    assert await repo.resume_capability(record.id) is False
