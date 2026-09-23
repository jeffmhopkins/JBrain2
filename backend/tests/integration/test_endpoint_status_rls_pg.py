"""Migration 0210 against real Postgres: a panel may write only its own status row.

THE ROW IS THE OWNER'S ONLY VIEW OF THE FLEET, which is what makes forging one worth
preventing. These are devices on children's walls authenticating with a key a four-year-old
could hand to a visitor, and the fleet view is how the owner — who has no terminal — answers
"is her panel alive, and did the update land". A panel that could write its sibling's row could
report that twin as dead, or as running a version it is not, and the one instrument the owner
has would be lying to him in the direction of "nothing to see".

Asserted in Postgres under the exact context a flashed panel runs as, not inferred from the
shape of the handler, because "the route writes `principal.id`" is a code-review convention
rather than a mechanism (CLAUDE.md rule 3, and `0178_settings_deny_jmolt` for the argument).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

ONE = uuid.uuid4()
TWO = uuid.uuid4()
# Exactly as a flashed unit runs: a device key, no domain scopes, not owner.
ONE_CTX = SessionContext(principal_id=str(ONE), principal_kind="device_key")
TWO_CTX = SessionContext(principal_id=str(TWO), principal_kind="device_key")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.endpoint_status"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        for pid, label in ((ONE, "panel Ellie"), (TWO, "panel Nora")):
            await s.execute(
                text(
                    """
                    INSERT INTO app.principals (id, kind, key_hash, label)
                    VALUES (:id, 'device_key', :hash, :label)
                    """
                ),
                {"id": pid, "hash": f"hash-{pid}", "label": label},
            )
        await s.commit()
    yield sessions
    await engine.dispose()


async def _report(
    sessions: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    target: uuid.UUID,
    version: str,
) -> None:
    """The upsert the telemetry route runs, with `target` free so a panel can be asked to
    write a row that is not its own."""
    async with scoped_session(sessions, ctx) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.endpoint_status (principal_id, reported_at, version, report)
                VALUES (:id, now(), :version, '{}'::jsonb)
                ON CONFLICT (principal_id) DO UPDATE
                SET reported_at = now(), version = EXCLUDED.version, report = EXCLUDED.report
                """
            ),
            {"id": target, "version": version},
        )
        await s.commit()


async def test_a_panel_reports_itself_and_the_owner_sees_both(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await _report(maker, ONE_CTX, ONE, "0.2.94")
    await _report(maker, TWO_CTX, TWO, "0.2.89")
    # And a second report REPLACES rather than accumulating: this is a snapshot, not a history.
    await _report(maker, ONE_CTX, ONE, "0.2.95")

    async with scoped_session(maker, OWNER) as s:
        rows = {
            str(r[0]): str(r[1])
            for r in (
                await s.execute(text("SELECT principal_id::text, version FROM app.endpoint_status"))
            ).all()
        }
    assert rows == {str(ONE): "0.2.95", str(TWO): "0.2.89"}


async def test_a_panel_cannot_write_its_sibling_s_row(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The forgery that matters: one twin's panel claiming to BE the other's report."""
    await _report(maker, TWO_CTX, TWO, "0.2.89")
    with pytest.raises(DBAPIError):
        await _report(maker, ONE_CTX, TWO, "0.0.1-forged")

    async with scoped_session(maker, OWNER) as s:
        version = (
            await s.execute(
                text("SELECT version FROM app.endpoint_status WHERE principal_id = :id"),
                {"id": TWO},
            )
        ).scalar_one()
    assert version == "0.2.89", "the sibling's report must survive the attempt"


async def test_a_panel_cannot_read_its_sibling_s_row(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Less severe than the write and still not the panel's business — and it is the same
    policy, so asserting it keeps a future `FOR ALL USING (kind = 'device_key')` from passing
    the write test above while quietly opening the fleet to every key in the house."""
    await _report(maker, TWO_CTX, TWO, "0.2.89")
    await _report(maker, ONE_CTX, ONE, "0.2.94")

    async with scoped_session(maker, ONE_CTX) as s:
        seen = {
            str(r[0])
            for r in (
                await s.execute(text("SELECT principal_id::text FROM app.endpoint_status"))
            ).all()
        }
    assert seen == {str(ONE)}, "a panel sees its own row and no other"


async def test_revoking_a_panel_takes_its_status_with_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`ON DELETE CASCADE`, asserted rather than assumed: a device key deleted from the PWA
    must not leave a row behind claiming a panel that no longer exists is reporting."""
    await _report(maker, ONE_CTX, ONE, "0.2.94")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.principals WHERE id = :id"), {"id": ONE})
        await s.commit()
    # A fresh session rather than the committed one: what is being asserted is what SURVIVED
    # the delete, and a read inside the transaction that did it is not that.
    async with scoped_session(maker, OWNER) as s:
        left = (
            await s.execute(
                text("SELECT count(*) FROM app.endpoint_status WHERE principal_id = :id"),
                {"id": ONE},
            )
        ).scalar_one()
    assert int(left) == 0
