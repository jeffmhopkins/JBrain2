"""Migrations 0251 and 0253 against real Postgres: `app.owner_devices`, the owner's confirm of
a new phone, and phone-bound owner sessions (docs/plans/OWNER_TUNNEL_PLAN.md §3.4, waves A3
and A5).

The table holds which phones may use the owner site at all, so it is the owner's alone
plus the `login` context the per-request device check and enrollment run under; anyone
else sees an empty table and cannot write. The session lookup is pinned here too: a bound
session authenticates only with its own phone's key, stops when the phone is revoked or
the session sits idle past `BOUND_SESSION_IDLE`, and an unbound one never rides the tunnel.

The database is shared by the whole module (`test_rls.database_url`), so every test enrols
phones under keys of its own and asserts about those rows only.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.owner_tunnel.devices import AlreadyEnrolled, SqlOwnerDeviceRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

LOGIN = SessionContext(auth_context="login")
TOKEN = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


def _key() -> str:
    return uuid.uuid4().hex * 2


@pytest.fixture
def phone() -> str:
    return _key()


@pytest.fixture
def other() -> str:
    return _key()


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _sees(maker: async_sessionmaker, ctx: SessionContext, key: str) -> int:
    async with scoped_session(maker, ctx) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM app.owner_devices WHERE key_hash = :k"), {"k": key}
            )
        ).scalar_one()


async def _live_sessions(maker: async_sessionmaker, device_id: str) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.device_sessions"
                    " WHERE device_id = CAST(:d AS uuid) AND revoked_at IS NULL"
                ),
                {"d": device_id},
            )
        ).scalar_one()


async def test_only_the_owner_and_login_see_phones(maker: async_sessionmaker, phone: str) -> None:
    await SqlOwnerDeviceRepo(maker).create("Pixel", phone, b"pk")
    assert await _sees(maker, OWNER, phone) == 1
    assert await _sees(maker, LOGIN, phone) == 1
    assert await _sees(maker, UNSCOPED, phone) == 0
    assert await _sees(maker, TOKEN, phone) == 0


@pytest.mark.parametrize("ctx", [UNSCOPED, TOKEN], ids=["unscoped", "capability"])
async def test_others_cannot_enroll_a_phone(
    maker: async_sessionmaker, ctx: SessionContext, phone: str
) -> None:
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, ctx) as s:
            await s.execute(
                text(
                    "INSERT INTO app.owner_devices (label, key_hash, public_key)"
                    " VALUES ('x', :k, '\\x00')"
                ),
                {"k": phone},
            )
            await s.commit()


@pytest.mark.parametrize("ctx", [UNSCOPED, TOKEN], ids=["unscoped", "capability"])
async def test_others_cannot_rename_or_unrevoke(
    maker: async_sessionmaker, ctx: SessionContext, phone: str
) -> None:
    phones = SqlOwnerDeviceRepo(maker)
    device = await phones.create("Pixel", phone, b"pk")
    assert await phones.revoke(OWNER, device.id)
    async with scoped_session(maker, ctx) as s:
        result = await s.execute(
            text(
                "UPDATE app.owner_devices SET label = 'mine', revoked_at = NULL WHERE key_hash = :k"
            ),
            {"k": phone},
        )
        await s.commit()
    assert getattr(result, "rowcount", 0) == 0
    assert await phones.touch_active(phone, "owner-tunnel") is None


async def test_no_one_deletes_a_phone(maker: async_sessionmaker, phone: str) -> None:
    await SqlOwnerDeviceRepo(maker).create("Pixel", phone, b"pk")
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(text("DELETE FROM app.owner_devices WHERE key_hash = :k"), {"k": phone})
            await s.commit()


async def test_a_key_enrolls_once(maker: async_sessionmaker, phone: str) -> None:
    phones = SqlOwnerDeviceRepo(maker)
    await phones.create("Pixel", phone, b"pk")
    with pytest.raises(AlreadyEnrolled):
        await phones.create("Pixel again", phone, b"pk")


async def test_key_hash_must_be_lowercase_hex(maker: async_sessionmaker) -> None:
    with pytest.raises(DBAPIError):
        await SqlOwnerDeviceRepo(maker).create("Pixel", _key().upper(), b"pk")


async def _bound(
    maker: async_sessionmaker, phone: str, other: str
) -> tuple[SqlAuthRepo, SqlOwnerDeviceRepo, str, str]:
    auth = SqlAuthRepo(maker)
    await auth_service.rotate_owner_key(auth)
    phones = SqlOwnerDeviceRepo(maker)
    device = await phones.create("Pixel", phone, b"pk")
    tablet = await phones.create("Tablet", other, b"pk2")
    assert await phones.confirm(OWNER, device.id)
    assert await phones.confirm(OWNER, tablet.id)
    token = await auth_service.mint_device_owner_session(auth, device.id, "phone: Pixel")
    assert token is not None
    return auth, phones, device.id, token


async def test_a_bound_session_needs_its_own_phone(
    maker: async_sessionmaker, phone: str, other: str
) -> None:
    auth, _, _, token = await _bound(maker, phone, other)
    principal = await auth_service.authenticate(auth, token, device=phone)
    assert principal is not None and principal.kind == "owner"
    assert await auth_service.authenticate(auth, token, device=other) is None
    assert await auth_service.authenticate(auth, token, device=None) is None


async def test_an_unbound_session_does_not_ride_the_tunnel(
    maker: async_sessionmaker, phone: str
) -> None:
    auth = SqlAuthRepo(maker)
    key = await auth_service.rotate_owner_key(auth)
    await SqlOwnerDeviceRepo(maker).create("Pixel", phone, b"pk")
    token = await auth_service.login(auth, key, "laptop")
    assert await auth_service.authenticate(auth, token, device=None) is not None
    assert await auth_service.authenticate(auth, token, device=phone) is None


async def test_a_new_mint_replaces_the_phones_last_session(
    maker: async_sessionmaker, phone: str, other: str
) -> None:
    auth, _, device_id, first = await _bound(maker, phone, other)
    second = await auth_service.mint_device_owner_session(auth, device_id, "phone: Pixel")
    assert second is not None
    assert await auth_service.authenticate(auth, first, device=phone) is None
    assert await auth_service.authenticate(auth, second, device=phone) is not None
    assert await _live_sessions(maker, device_id) == 1


async def test_revoking_a_phone_revokes_its_sessions_only(
    maker: async_sessionmaker, phone: str, other: str
) -> None:
    auth, phones, device_id, token = await _bound(maker, phone, other)
    (tablet,) = [d for d in await phones.list_all(OWNER) if d.key_hash == other]
    tablet_token = await auth_service.mint_device_owner_session(auth, tablet.id, "phone: Tablet")
    assert tablet_token is not None

    assert await phones.revoke(OWNER, device_id)
    assert not await phones.revoke(OWNER, device_id)
    assert await auth_service.authenticate(auth, token, device=phone) is None
    assert await phones.touch_active(phone, "owner-tunnel") is None
    assert await _live_sessions(maker, device_id) == 0
    # The other phone and its session are untouched.
    assert await auth_service.authenticate(auth, tablet_token, device=other) is not None
    assert await phones.touch_active(other, "owner-tunnel") is not None


async def test_a_bound_session_idles_out(maker: async_sessionmaker, phone: str, other: str) -> None:
    auth, _, device_id, token = await _bound(maker, phone, other)
    async with scoped_session(maker, LOGIN) as s:
        await s.execute(
            text(
                "UPDATE app.device_sessions SET last_seen_at = now() - interval '31 days'"
                " WHERE device_id = CAST(:d AS uuid)"
            ),
            {"d": device_id},
        )
        await s.commit()
    assert await auth_service.authenticate(auth, token, device=phone) is None


async def test_touch_stamps_the_phone(maker: async_sessionmaker, phone: str) -> None:
    phones = SqlOwnerDeviceRepo(maker)
    await phones.create("Pixel", phone, b"pk")
    assert await phones.touch_active(_key(), "owner-tunnel") is None
    assert await phones.touch_active(phone, "owner-tunnel") is not None
    (listed,) = [d for d in await phones.list_all(OWNER) if d.key_hash == phone]
    assert listed.last_seen_at is not None and listed.last_ingress == "owner-tunnel"


async def test_owner_renames_and_bad_ids_are_misses(maker: async_sessionmaker, phone: str) -> None:
    phones = SqlOwnerDeviceRepo(maker)
    device = await phones.create("Pixel", phone, b"pk")
    assert await phones.rename(OWNER, device.id, "Mine")
    assert not await phones.rename(OWNER, "not-a-uuid", "x")
    assert not await phones.revoke(OWNER, "not-a-uuid")
    assert not await phones.rename(UNSCOPED, device.id, "Theirs")
    assert [d.label for d in await phones.list_all(OWNER) if d.key_hash == phone] == ["Mine"]


async def test_the_phone_binds_to_the_owner_the_pwa_uses(
    maker: async_sessionmaker, phone: str
) -> None:
    # Two live owner rows (an abnormal box `auth.owner` already settles): the phone's
    # session goes to the one with the most recent live session, not to whichever row the
    # scan meets first, and the mint does not fail on the second row.
    auth = SqlAuthRepo(maker)
    used = await auth_service.login(auth, await auth_service.rotate_owner_key(auth), "pwa")
    used_owner = await auth_service.authenticate(auth, used, device=None)
    assert used_owner is not None
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, key_hash, label)"
                " VALUES (gen_random_uuid(), 'owner', :k, 'stray')"
            ),
            {"k": _key()},
        )
        await s.commit()
    phones = SqlOwnerDeviceRepo(maker)
    device = await phones.create("Pixel", phone, b"pk")
    assert await phones.confirm(OWNER, device.id)
    token = await auth_service.mint_device_owner_session(auth, device.id, "phone: Pixel")
    assert token is not None
    bound = await auth_service.authenticate(auth, token, device=phone)
    assert bound is not None and bound.id == used_owner.id


# --- the owner confirms a new phone (migration 0253) --------------------------------------


async def _set_confirmed(maker: async_sessionmaker, ctx: SessionContext, key: str) -> None:
    async with scoped_session(maker, ctx) as s:
        await s.execute(
            text("UPDATE app.owner_devices SET confirmed_at = now() WHERE key_hash = :k"),
            {"k": key},
        )
        await s.commit()


async def test_a_phone_starts_pending_and_the_owner_confirms_it_once(
    maker: async_sessionmaker, phone: str
) -> None:
    phones = SqlOwnerDeviceRepo(maker)
    device = await phones.create("Pixel", phone, b"pk")
    assert not device.confirmed
    pending = await phones.touch_active(phone, "owner-tunnel")
    assert pending is not None and not pending.confirmed
    assert await phones.confirm(OWNER, device.id)
    assert not await phones.confirm(OWNER, device.id)
    assert not await phones.confirm(OWNER, "not-a-uuid")
    confirmed = await phones.touch_active(phone, "owner-tunnel")
    assert confirmed is not None and confirmed.confirmed


async def test_the_login_context_cannot_confirm_a_phone(
    maker: async_sessionmaker, phone: str
) -> None:
    # Enrollment runs as `login`, which may write this table, so only the trigger stops a
    # phone that scanned the QR from letting itself in.
    phones = SqlOwnerDeviceRepo(maker)
    device = await phones.create("Pixel", phone, b"pk")
    with pytest.raises((DBAPIError, ProgrammingError)):
        await _set_confirmed(maker, LOGIN, phone)
    with pytest.raises((DBAPIError, ProgrammingError)):
        await phones.confirm(LOGIN, device.id)
    (listed,) = [d for d in await phones.list_all(OWNER) if d.key_hash == phone]
    assert not listed.confirmed


async def test_a_phone_is_never_enrolled_already_confirmed(maker: async_sessionmaker) -> None:
    for ctx in (LOGIN, OWNER):
        with pytest.raises((DBAPIError, ProgrammingError)):
            async with scoped_session(maker, ctx) as s:
                await s.execute(
                    text(
                        "INSERT INTO app.owner_devices (label, key_hash, public_key, confirmed_at)"
                        " VALUES ('x', :k, '\\x00', now())"
                    ),
                    {"k": _key()},
                )
                await s.commit()


async def test_a_confirm_cannot_be_moved_or_undone(maker: async_sessionmaker, phone: str) -> None:
    phones = SqlOwnerDeviceRepo(maker)
    device = await phones.create("Pixel", phone, b"pk")
    assert await phones.confirm(OWNER, device.id)
    with pytest.raises((DBAPIError, ProgrammingError)):
        await _set_confirmed(maker, OWNER, phone)
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text("UPDATE app.owner_devices SET confirmed_at = NULL WHERE key_hash = :k"),
                {"k": phone},
            )
            await s.commit()
    # Every other write still works on a confirmed phone.
    assert await phones.rename(OWNER, device.id, "Mine")
    assert await phones.touch_active(phone, "lan") is not None
    assert await phones.revoke(OWNER, device.id)


async def test_a_pending_phones_session_does_not_authenticate(
    maker: async_sessionmaker, phone: str
) -> None:
    auth = SqlAuthRepo(maker)
    await auth_service.rotate_owner_key(auth)
    device = await SqlOwnerDeviceRepo(maker).create("Pixel", phone, b"pk")
    token = await auth_service.mint_device_owner_session(auth, device.id, "phone: Pixel")
    assert token is not None
    assert await auth_service.authenticate(auth, token, device=phone) is None
    assert await SqlOwnerDeviceRepo(maker).confirm(OWNER, device.id)
    assert await auth_service.authenticate(auth, token, device=phone) is not None


@pytest.mark.parametrize(
    "change",
    ["label = 'mine'", "key_hash = repeat('c', 64)", "public_key = '\\x01'", "revoked_at = now()"],
)
async def test_the_login_context_writes_only_when_a_phone_was_seen(
    maker: async_sessionmaker, phone: str, change: str
) -> None:
    device = await SqlOwnerDeviceRepo(maker).create("Pixel", phone, b"pk")
    assert await SqlOwnerDeviceRepo(maker).confirm(OWNER, device.id)
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, LOGIN) as s:
            await s.execute(
                text(f"UPDATE app.owner_devices SET {change} WHERE key_hash = :k"), {"k": phone}
            )
            await s.commit()


@pytest.mark.parametrize("ctx", [LOGIN, OWNER], ids=["login", "owner"])
async def test_no_one_unrevokes_a_phone(
    maker: async_sessionmaker, ctx: SessionContext, phone: str
) -> None:
    phones = SqlOwnerDeviceRepo(maker)
    device = await phones.create("Pixel", phone, b"pk")
    assert await phones.confirm(OWNER, device.id)
    assert await phones.revoke(OWNER, device.id)
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, ctx) as s:
            await s.execute(
                text("UPDATE app.owner_devices SET revoked_at = NULL WHERE key_hash = :k"),
                {"k": phone},
            )
            await s.commit()
    assert await phones.touch_active(phone, "owner-tunnel") is None
