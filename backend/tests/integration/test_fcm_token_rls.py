"""`fcm_token` RLS isolation against real Postgres (JBrain360 M6a).

The firewall is Postgres, not the app: under a device's `device_context` a device
sees and writes only its OWN token; it can neither read another device's token nor
register one under another principal/subject (WITH CHECK). The owner/system reads
all (routing), and a revoked principal's token drops out of that read.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import device_context, scoped_session
from jbrain.push import SqlFcmTokenRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _device(maker: async_sessionmaker, name: str) -> tuple[str, str]:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, :n, 'device')"),
            {"s": sid, "n": name},
        )
        await session.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash)"
                " VALUES (:p, 'device_key', :s, :k)"
            ),
            {"p": pid, "s": sid, "k": uuid.uuid4().hex},
        )
    return pid, sid


# The token is globally UNIQUE and the module DB persists across tests, so each
# device's token is unique (derived from its principal id) to avoid collisions.
def _tok(pid: str) -> str:
    return f"tok-{pid}"


async def test_device_registers_and_sees_only_its_own_token(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    pid_b, sid_b = await _device(maker, "Bob")
    repo = SqlFcmTokenRepo(maker)

    await repo.register(
        device_context(pid_a, sid_a), principal_id=pid_a, subject_id=sid_a, token=_tok(pid_a)
    )
    await repo.register(
        device_context(pid_b, sid_b), principal_id=pid_b, subject_id=sid_b, token=_tok(pid_b)
    )

    # Each device sees only its own row under RLS.
    async with scoped_session(maker, device_context(pid_a, sid_a)) as session:
        rows = (await session.execute(text("SELECT token FROM app.fcm_token"))).scalars().all()
    assert rows == [_tok(pid_a)]


async def test_device_cannot_register_under_another_principal(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    pid_b, sid_b = await _device(maker, "Bob")
    # Alice's session trying to write a row owned by Bob — WITH CHECK rejects it.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, device_context(pid_a, sid_a)) as session:
            await session.execute(
                text(
                    "INSERT INTO app.fcm_token (principal_id, subject_id, token)"
                    " VALUES (cast(:p AS uuid), cast(:s AS uuid), :t)"
                ),
                {"p": pid_b, "s": sid_b, "t": f"forged-{pid_a}"},
            )


async def test_owner_routing_reads_active_tokens_and_drops_revoked(
    maker: async_sessionmaker,
) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    pid_b, sid_b = await _device(maker, "Bob")
    repo = SqlFcmTokenRepo(maker)
    await repo.register(
        device_context(pid_a, sid_a), principal_id=pid_a, subject_id=sid_a, token=_tok(pid_a)
    )
    await repo.register(
        device_context(pid_b, sid_b), principal_id=pid_b, subject_id=sid_b, token=_tok(pid_b)
    )

    # Owner/system routing read sees both subjects' active tokens (scoped to these
    # two subjects, since the module DB carries other tests' rows).
    both = await repo.tokens_for_subjects(OWNER, [sid_a, sid_b])
    assert set(both) == {_tok(pid_a), _tok(pid_b)}

    # Revoke Bob's device → its token drops out of routing (revoke-kills-token).
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.principals SET revoked_at = now() WHERE id = cast(:p AS uuid)"),
            {"p": pid_b},
        )
    after = await repo.tokens_for_subjects(OWNER, [sid_a, sid_b])
    assert after == [_tok(pid_a)]
