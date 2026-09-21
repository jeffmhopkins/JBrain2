"""Migration 0206 against real Postgres: a panel reads its own knobs and nothing else.

THE POINT OF THIS FILE IS THE SECOND HALF. `app.endpoint_settings` exists rather than a key
in `app.settings` because that table is gated on `app.is_owner()` and holds the Gmail client
secret, the Moltbook bearer key and the global kill, while a panel authenticates as a
`device_key` specifically so a stolen one cannot reach them.

`0178_settings_deny_jmolt` makes the argument this test enforces: "no route does that" is a
code-review convention, not a mechanism. So the isolation is asserted in Postgres, under the
exact context a panel runs as — not inferred from the shape of the FastAPI handlers (CLAUDE.md
rule 3).
"""

from collections.abc import AsyncIterator
from typing import cast

import pytest
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# Exactly what a flashed panel runs as: a device key, no domain scopes, not owner-scoped.
PANEL = SessionContext(principal_id="panel-1", principal_kind="device_key")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_a_panel_can_read_its_own_settings(maker: async_sessionmaker) -> None:
    """The whole feature rests on this: if a panel cannot SELECT, the knobs never apply."""
    async with scoped_session(maker, PANEL) as s:
        row = (
            await s.execute(
                text("SELECT volume, mic_gain_db, brightness FROM app.endpoint_settings")
            )
        ).first()
    assert row is not None, "a panel must be able to read the row it is meant to apply"


async def test_a_panel_cannot_write_its_own_volume(maker: async_sessionmaker) -> None:
    """A device on a child's wall able to raise the level in its own ear is the one thing the
    65 dB(A) reasoning exists to prevent.

    THE DENIAL IS SILENT, AND THAT IS WHAT THIS PINS. The panel policy is `FOR SELECT`, so the
    row is simply not visible to an UPDATE: Postgres matches nothing and reports success with
    zero rows rather than raising. An earlier version of this test expected an exception and
    passed a write that had in fact been denied — the assertion has to be about the effect,
    not about the error, or it proves nothing either way.
    """
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("UPDATE app.endpoint_settings SET volume = 55 WHERE id = 1"))
        await s.commit()

    async with scoped_session(maker, PANEL) as s:
        result = cast(
            CursorResult,
            await s.execute(text("UPDATE app.endpoint_settings SET volume = 100 WHERE id = 1")),
        )
        await s.commit()
    assert result.rowcount == 0, "a panel's write must touch no row"

    async with scoped_session(maker, OWNER) as s:
        volume = (
            await s.execute(text("SELECT volume FROM app.endpoint_settings WHERE id = 1"))
        ).scalar_one()
    assert volume == 55, "the owner's value must survive a panel trying to overwrite it"


async def test_a_panel_cannot_read_app_settings(maker: async_sessionmaker) -> None:
    """The reason this table exists. `app.settings` holds the Gmail client secret, the
    Moltbook bearer key, the autonomy switch and the global kill; a stolen panel key must
    come back with nothing."""
    async with scoped_session(maker, PANEL) as s:
        rows = (await s.execute(text("SELECT key FROM app.settings"))).scalars().all()
    assert list(rows) == [], "a device key must not see any owner setting"


async def test_the_owner_can_write_and_read_back(maker: async_sessionmaker) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.endpoint_settings SET volume = 61, brightness = 42 WHERE id = 1")
        )
        await s.commit()
    async with scoped_session(maker, PANEL) as s:
        row = (
            await s.execute(text("SELECT volume, brightness FROM app.endpoint_settings"))
        ).first()
    assert row is not None
    assert (row[0], row[1]) == (61, 42), "what the owner sets is what the panel reads"
