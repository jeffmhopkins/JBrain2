"""jcode share-link auth: mint / redeem / scope, with the repo faked.

The SQL repo's expiry/revocation filtering runs against real Postgres in
test_jcode_share_pg; here we pin the security-critical service contract — mint binds
a time-boxed secret to one session, redeem exchanges a LIVE secret for a session
cookie carrying that scope (and rejects empty/unknown/expired/revoked/wrong-kind), a
lapsed share's cookie stops authenticating, and the access gate admits the owner or a
same-session share but 403s a cross-session share or any other kind.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from jbrain.api.deps import jcode_session_access
from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.auth.service import PrincipalInfo
from tests.unit.fakes import FakeAuthRepo


def _req(sid: str):
    """A minimal stand-in for the Starlette Request the gate reads path params off of."""
    return type("Req", (), {"path_params": {"sid": sid}})()


@pytest.mark.asyncio
async def test_mint_binds_a_timeboxed_secret_to_the_session() -> None:
    repo = FakeAuthRepo()
    key, record = await auth_service.mint_jcode_share(repo, "sess-a", "Sarah", ttl_hours=24)
    assert key  # the secret, shown once
    assert record.label == "Sarah"
    assert record.expires_at is not None and record.revoked_at is None
    stored = repo.principals[0]
    assert stored.kind == "jcode_share_link"
    assert stored.jcode_session_id == "sess-a"
    assert stored.key_hash == keys.hash_token(key)  # plain SHA-256, never the paper-key path


@pytest.mark.asyncio
async def test_redeem_live_secret_mints_a_scoped_session_cookie() -> None:
    repo = FakeAuthRepo()
    key, _ = await auth_service.mint_jcode_share(repo, "sess-a", "s", ttl_hours=24)
    redeemed = await auth_service.redeem_jcode_share(repo, key)
    assert redeemed is not None
    cookie_token, session_id = redeemed
    assert session_id == "sess-a"
    # The minted cookie authenticates to the share principal, carrying its session scope.
    principal = await auth_service.authenticate(repo, cookie_token)
    assert principal is not None
    assert principal.kind == "jcode_share_link"
    assert principal.jcode_session_id == "sess-a"


@pytest.mark.asyncio
async def test_redeem_rejects_empty_unknown_expired_revoked_and_wrong_kind() -> None:
    repo = FakeAuthRepo()
    assert await auth_service.redeem_jcode_share(repo, "") is None
    assert await auth_service.redeem_jcode_share(repo, "nope") is None

    # An owner key minted on the owner path must NOT redeem as a share.
    owner_key = await auth_service.rotate_owner_key(repo)
    assert await auth_service.redeem_jcode_share(repo, owner_key) is None

    lapsed, _ = await auth_service.mint_jcode_share(repo, "s", "x", ttl_hours=-1)
    assert await auth_service.redeem_jcode_share(repo, lapsed) is None

    key, record = await auth_service.mint_jcode_share(repo, "s", "x", ttl_hours=24)
    assert await repo.revoke_jcode_share(record.id, "s") is True
    assert await auth_service.redeem_jcode_share(repo, key) is None


@pytest.mark.asyncio
async def test_a_lapsed_shares_cookie_stops_authenticating() -> None:
    # Expiry must be enforced on EVERY request, not just at redeem — else a redeemed
    # cookie would outlive the share. Redeem while live, then lapse the principal.
    repo = FakeAuthRepo()
    key, _ = await auth_service.mint_jcode_share(repo, "s", "x", ttl_hours=24)
    redeemed = await auth_service.redeem_jcode_share(repo, key)
    assert redeemed is not None
    cookie_token, _ = redeemed
    assert await auth_service.authenticate(repo, cookie_token) is not None

    repo.principals[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await auth_service.authenticate(repo, cookie_token) is None


@pytest.mark.asyncio
async def test_revoking_a_share_kills_its_live_cookie() -> None:
    repo = FakeAuthRepo()
    key, record = await auth_service.mint_jcode_share(repo, "s", "x", ttl_hours=24)
    redeemed = await auth_service.redeem_jcode_share(repo, key)
    assert redeemed is not None
    cookie_token, _ = redeemed
    assert await auth_service.authenticate(repo, cookie_token) is not None
    assert await repo.revoke_jcode_share(record.id, "s") is True
    # Revocation fails the existing cookie closed on its next use.
    assert await auth_service.authenticate(repo, cookie_token) is None


@pytest.mark.asyncio
async def test_list_and_revoke_are_scoped_to_the_session() -> None:
    repo = FakeAuthRepo()
    _, a = await auth_service.mint_jcode_share(repo, "sess-a", "a", ttl_hours=24)
    _, b = await auth_service.mint_jcode_share(repo, "sess-b", "b", ttl_hours=24)
    assert {s.id for s in await repo.list_jcode_shares("sess-a")} == {a.id}
    # Can't revoke a share by naming the WRONG session (defence in depth).
    assert await repo.revoke_jcode_share(a.id, "sess-b") is False
    assert await repo.revoke_jcode_share(a.id, "sess-a") is True
    assert await repo.list_jcode_shares("sess-a") == []
    # A bad uuid / unknown id reports no row changed.
    assert await repo.revoke_jcode_share("not-a-uuid", "sess-a") is False


@pytest.mark.asyncio
async def test_access_gate_admits_owner_and_same_session_share_only() -> None:
    owner = PrincipalInfo(id="o", kind="owner", label="owner")
    same = PrincipalInfo(id="1", kind="jcode_share_link", label="s", jcode_session_id="abc")
    other = PrincipalInfo(id="2", kind="jcode_share_link", label="s", jcode_session_id="xyz")
    device = PrincipalInfo(id="3", kind="device_key", label="d", subject_id="sub")

    assert await jcode_session_access(_req("abc"), owner) is owner
    assert await jcode_session_access(_req("abc"), same) is same
    for bad in (other, device):
        with pytest.raises(HTTPException) as exc:
            await jcode_session_access(_req("abc"), bad)
        assert exc.value.status_code == 403
