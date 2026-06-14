"""Integration backfill machinery against real Postgres: the active-integration
guard and the bounded integration backfill."""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain import queue
from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean(database_url: str) -> AsyncIterator[None]:  # noqa: F811
    """The backfill operates over the WHOLE notes table, so each test needs an
    isolated slate in the shared module DB. Truncate as the superuser (the app
    role lacks TRUNCATE), at setup so a skipped teardown can't leak rows."""
    admin_url = database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test")
    engine: AsyncEngine = create_async_engine(admin_url, poolclass=NullPool)
    async with async_sessionmaker(engine)() as s:
        await s.execute(text("TRUNCATE app.jobs, app.notes CASCADE"))
        await s.commit()
    await engine.dispose()
    yield


async def _seed_note(maker, *, indexed: bool = True, integrated: bool = False, created: str) -> str:  # noqa: F811
    nid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, ingest_state,"
                " integration_state, created_at) VALUES (:i, :c, 'general', 'body', :ing, :int, :t)"
            ),
            {
                "i": nid,
                "c": nid[:12],
                "ing": "indexed" if indexed else "pending",
                "int": "integrated" if integrated else "pending_integration",
                "t": datetime.fromisoformat(created),
            },
        )
    return nid


async def _seed_job(maker, kind: str, note_id: str, *, status: str = "queued") -> None:  # noqa: F811
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.jobs (id, kind, payload, status) VALUES (gen_random_uuid(),"
                " :k, jsonb_build_object('note_id', cast(:n AS text)), :s)"
            ),
            {"k": kind, "n": note_id, "s": status},
        )


async def _integrate_jobs(maker, note_id: str) -> int:  # noqa: F811
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'integrate_note'"
                    " AND payload->>'note_id' = :n"
                ),
                {"n": note_id},
            )
        ).scalar_one()


async def test_has_active_analysis_detects_the_integrate_job(maker) -> None:  # noqa: F811
    note = await _seed_note(maker, created="2026-01-01T00:00:00+00:00")
    await _seed_job(maker, "integrate_note", note)
    assert await queue.has_active_analysis(maker, OWNER, note) is True
    other = await _seed_note(maker, created="2026-01-01T00:00:00+00:00")
    assert await queue.has_active_analysis(maker, OWNER, other) is False


async def test_backfill_pending_integration_bounded_oldest_first_and_skips(maker) -> None:  # noqa: F811
    n_old = await _seed_note(maker, created="2026-01-01T00:00:00+00:00")
    n_mid = await _seed_note(maker, created="2026-02-01T00:00:00+00:00")
    n_new = await _seed_note(maker, created="2026-03-01T00:00:00+00:00")
    n_done = await _seed_note(maker, integrated=True, created="2025-01-01T00:00:00+00:00")
    n_busy = await _seed_note(maker, created="2025-06-01T00:00:00+00:00")
    await _seed_job(maker, "integrate_note", n_busy)  # active job → skip (no second job)
    n_pending = await _seed_note(maker, indexed=False, created="2025-01-01T00:00:00+00:00")

    # Bounded: only the two oldest eligible (old, mid) — not new, integrated,
    # busy (active job), or not-yet-indexed.
    enqueued = await queue.backfill_pending_integration(maker, OWNER, limit=2)
    assert enqueued == 2
    assert await _integrate_jobs(maker, n_old) == 1
    assert await _integrate_jobs(maker, n_mid) == 1
    assert await _integrate_jobs(maker, n_new) == 0
    assert await _integrate_jobs(maker, n_done) == 0
    # n_busy keeps only its pre-existing active job — the backfill added none.
    assert await _integrate_jobs(maker, n_busy) == 1
    assert await _integrate_jobs(maker, n_pending) == 0

    # Second pass drains the remainder (n_new) but never re-enqueues a note that
    # now has an active integrate_note job (old, mid).
    again = await queue.backfill_pending_integration(maker, OWNER, limit=100)
    assert again == 1
    assert await _integrate_jobs(maker, n_new) == 1
    assert await _integrate_jobs(maker, n_old) == 1  # not duplicated
