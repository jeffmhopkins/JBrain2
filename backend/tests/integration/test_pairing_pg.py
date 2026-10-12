"""Pairing redemption against real Postgres (JBrain360 M2c, migration 0068).

Proves the SECURITY DEFINER `app.redeem_pairing_code` flow: minting (owner-only)
then redeeming creates a real device that authenticates, is one-time, rejects
expired/unknown codes, and that the `pairing_code` table is owner-only. Since 0240 the
table holds only the code's sha256, and a code minted before that migration still redeems.
"""

import importlib.util
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.devices import service as device_service
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.locations.pairing import SqlPairingRepo, code_digest, generate_pairing_code
from tests.conftest import docker_available
from tests.integration.test_rls import (  # noqa: F401
    OWNER,
    _admin_url,
    _Cluster,
    _pg_cluster,
    database_url,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_mint_then_redeem_creates_a_real_device_and_is_one_time(
    maker: async_sessionmaker,
) -> None:
    repo = SqlPairingRepo(maker)
    code, _ = await repo.mint_code(OWNER, label="Mom's phone", monitoring=2)

    device = await repo.redeem(code)
    assert device is not None
    assert device.label == "Mom's phone"
    assert device.monitoring == 2
    assert device.key  # returned exactly once

    # The minted device key authenticates as the new device_key principal/subject.
    principal = await auth_service.authenticate_device(SqlAuthRepo(maker), device.key)
    assert principal is not None
    assert principal.id == device.principal_id
    assert principal.subject_id == device.subject_id

    # One-time: a second redeem of the same code fails closed.
    assert await repo.redeem(code) is None


async def test_re_pair_rotates_the_key_on_the_existing_device(maker: async_sessionmaker) -> None:
    repo = SqlPairingRepo(maker)
    devices = SqlDeviceRepo(maker)
    auth = SqlAuthRepo(maker)

    # First pairing creates the device.
    first_code, _ = await repo.mint_code(OWNER, label="Mom's phone", monitoring=1)
    original = await repo.redeem(first_code)
    assert original is not None
    # A rename since the original pairing must be honoured by the re-pair config.
    assert await device_service.rename_device(devices, OWNER, original.subject_id, "Mom's Pixel")

    # Re-pair: a code TARGETING the existing subject rotates its key in place.
    before = {d.id for d in await devices.list(OWNER)}
    repair_code, _ = await repo.mint_code(
        OWNER, label="ignored", monitoring=1, subject_id=original.subject_id
    )
    repaired = await repo.redeem(repair_code)
    assert repaired is not None
    # Same identity (history stays attached), fresh key + principal, current name.
    assert repaired.subject_id == original.subject_id
    assert repaired.principal_id != original.principal_id
    assert repaired.label == "Mom's Pixel"

    # The new key authenticates; the old one is dead; NO new device was created
    # (the module shares one DB, so compare the device set, not a clean list).
    assert await auth_service.authenticate_device(auth, original.key) is None
    new = await auth_service.authenticate_device(auth, repaired.key)
    assert new is not None and new.subject_id == original.subject_id
    listed = {d.id: d for d in await devices.list(OWNER)}
    assert {*listed} == before  # re-pair adds no device
    assert listed[original.subject_id].revoked is False


async def test_re_pair_targeting_a_non_device_subject_fails_closed(
    maker: async_sessionmaker,
) -> None:
    repo = SqlPairingRepo(maker)
    # A person subject is not a device — re-pairing it is a flat failure (no key
    # minted), like an invalid code.
    person = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:id, 'Mom', 'person')"),
            {"id": person},
        )
    code, _ = await repo.mint_code(OWNER, label="x", monitoring=1, subject_id=person)
    assert await repo.redeem(code) is None


async def test_redeem_rejects_expired_and_unknown_codes(maker: async_sessionmaker) -> None:
    repo = SqlPairingRepo(maker)
    expired, _ = await repo.mint_code(OWNER, label="X", monitoring=1, ttl=timedelta(seconds=-1))
    assert await repo.redeem(expired) is None
    assert await repo.redeem("not-a-real-code") is None
    assert await repo.redeem("") is None


async def test_pairing_code_table_is_owner_only(maker: async_sessionmaker) -> None:
    repo = SqlPairingRepo(maker)
    code, _ = await repo.mint_code(OWNER, label="X", monitoring=1)

    async with scoped_session(maker, OWNER) as session:
        seen = (
            await session.execute(
                text("SELECT count(*) FROM app.pairing_code WHERE code = :c"),
                {"c": code_digest(code)},
            )
        ).scalar()
    assert seen == 1

    # A non-owner (even location-scoped) capability session sees no codes...
    cap = SessionContext(principal_kind="capability_token", domain_scopes=("location",))
    async with scoped_session(maker, cap) as session:
        assert (await session.execute(text("SELECT count(*) FROM app.pairing_code"))).scalar() == 0
    # ...and cannot mint one (WITH CHECK: is_full_owner).
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, cap) as session:
            await session.execute(
                text(
                    "INSERT INTO app.pairing_code (code, label, expires_at)"
                    " VALUES (:c, 'y', now() + interval '1 hour')"
                ),
                {"c": str(uuid.uuid4())},
            )


async def test_the_code_is_stored_only_as_its_digest(maker: async_sessionmaker) -> None:
    repo = SqlPairingRepo(maker)
    code, _ = await repo.mint_code(OWNER, label="X", monitoring=1)
    async with scoped_session(maker, OWNER) as session:
        stored = {r[0] for r in await session.execute(text("SELECT code FROM app.pairing_code"))}
    assert code not in stored
    assert code_digest(code) in stored
    # A reader holding the digest can't redeem with it — only the code itself works.
    assert await repo.redeem(code_digest(code)) is None
    assert await repo.redeem(code) is not None


_MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0240_pairing_code_hashed.py"
)


@pytest.fixture
def run_0240(_pg_cluster: _Cluster, database_url: str) -> Iterator[object]:  # noqa: F811
    """Run 0240's upgrade as the superuser the deploy's `migrate` service uses."""
    spec = importlib.util.spec_from_file_location("migration_0240_pg", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dbname = database_url.rsplit("/", 1)[-1]
    engine = sqlalchemy.create_engine(_admin_url(_pg_cluster, dbname, driver="psycopg"))

    def _run() -> None:
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            module.upgrade()

    yield _run
    engine.dispose()


async def test_a_code_minted_before_the_migration_still_redeems(
    maker: async_sessionmaker, run_0240: object
) -> None:
    # The QR open on someone's screen during the upgrade: a plaintext row, as the old
    # mint wrote it, next to one already in digest form.
    old_code = generate_pairing_code()
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.pairing_code (code, label, expires_at)"
                " VALUES (:c, 'Old QR', now() + interval '10 minutes')"
            ),
            {"c": old_code},
        )
    repo = SqlPairingRepo(maker)
    new_code, _ = await repo.mint_code(OWNER, label="New QR", monitoring=1)

    run_0240()  # type: ignore[operator]
    run_0240()  # type: ignore[operator]  # idempotent: a digest is never re-hashed

    async with scoped_session(maker, OWNER) as session:
        stored = {r[0] for r in await session.execute(text("SELECT code FROM app.pairing_code"))}
    assert old_code not in stored and code_digest(old_code) in stored
    old = await repo.redeem(old_code)
    assert old is not None and old.label == "Old QR"
    assert await repo.redeem(new_code) is not None
