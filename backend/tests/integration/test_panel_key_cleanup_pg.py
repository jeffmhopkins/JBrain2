"""Migration 0213 — what the pile of superseded keys collapses to, proved against real Postgres.

The migration itself has already run by the time these tests see the database (the template is
migrated), so what is pinned here is the RULE it encodes, re-applied to rows these tests create.
That is the half worth testing: the numbers on the live box were a one-off, but "newest per panel
survives and a phone is never touched" is the property that has to keep holding.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

COLLAPSE = """
UPDATE app.principals p SET revoked_at = now()
WHERE p.kind = 'device_key' AND p.revoked_at IS NULL
  AND p.id IN (
      SELECT id FROM (
          SELECT pr.id,
                 row_number() OVER (
                     PARTITION BY s.display_name, s.device_role ORDER BY pr.created_at DESC
                 ) AS rn
          FROM app.principals pr
          JOIN app.subjects s ON s.id = pr.subject_id
          WHERE pr.kind = 'device_key' AND pr.revoked_at IS NULL
            AND s.device_role IS NOT NULL
      ) ranked
      WHERE rn > 1
  )
"""


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _flash(maker, label: str, *, role: str | None, age_s: int) -> str:
    """One flash, as the world looked BEFORE 0211: a fresh subject and a fresh key every time."""
    async with scoped_session(maker, OWNER) as s:
        sid = (
            await s.execute(
                text(
                    "INSERT INTO app.subjects (id, display_name, kind, device_role)"
                    " VALUES (gen_random_uuid(), :label, 'device', :role) RETURNING id::text"
                ),
                {"label": label, "role": role},
            )
        ).scalar_one()
        pid = (
            await s.execute(
                text(
                    "INSERT INTO app.principals (id, kind, subject_id, key_hash, label, created_at)"
                    " VALUES (gen_random_uuid(), 'device_key', CAST(:sid AS uuid),"
                    "         :kh, :label, now() - make_interval(secs => :age))"
                    " RETURNING id::text"
                ),
                {"sid": sid, "kh": f"h-{label}-{age_s}", "label": label, "age": age_s},
            )
        ).scalar_one()
        await s.commit()
    return str(pid)


async def _live(maker, label: str) -> set[str]:
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id::text FROM app.principals"
                    " WHERE kind = 'device_key' AND revoked_at IS NULL AND label = :label"
                ),
                {"label": label},
            )
        ).all()
    return {str(r[0]) for r in rows}


async def test_only_the_newest_key_of_a_reflashed_panel_survives(maker) -> None:
    """THIRTEEN FLASHES, ONE PANEL. A flash rewrites NVS with the key it just minted, so the
    newest principal IS the one the unit is using and every older one is dead by construction —
    which is why this needs no liveness signal to be safe."""
    keys = [
        await _flash(maker, "panel cleanup-a", role="jpet", age_s=age) for age in (900, 600, 30)
    ]
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text(COLLAPSE))
        await s.commit()

    assert await _live(maker, "panel cleanup-a") == {keys[-1]}


async def test_a_panel_that_has_never_reported_keeps_its_key(maker) -> None:
    """THE MISTAKE THIS RULE AVOIDS. Lydian's panel was flashed and had not come up two hours
    later; a cleanup keyed on "has reported telemetry" would have revoked its only credential and
    left a unit on a child's wall unable to authenticate. Quiet is not the same as superseded."""
    only = await _flash(maker, "panel cleanup-silent", role="jpet", age_s=10)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text(COLLAPSE))
        await s.commit()

    assert await _live(maker, "panel cleanup-silent") == {only}


async def test_a_phone_is_never_touched(maker) -> None:
    """`device_role IS NOT NULL` is the whole guard. The owner's phone is the same `device_key`
    substrate and has several keys of its own history; sweeping it would take out location
    ingest for a tidy-up that has nothing to do with it."""
    phones = [await _flash(maker, "cleanup phone", role=None, age_s=age) for age in (900, 30)]
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text(COLLAPSE))
        await s.commit()

    assert await _live(maker, "cleanup phone") == set(phones)


async def test_two_panels_with_different_names_each_keep_one(maker) -> None:
    """The partition is per NAME, so a sweep over a house of panels leaves one key each rather
    than one key in total — which is what `send(to="panel")` needs to work at all."""
    a = await _flash(maker, "panel cleanup-twin-a", role="jpet", age_s=30)
    await _flash(maker, "panel cleanup-twin-a", role="jpet", age_s=900)
    b = await _flash(maker, "panel cleanup-twin-b", role="jpet", age_s=60)

    async with scoped_session(maker, OWNER) as s:
        await s.execute(text(COLLAPSE))
        await s.commit()

    assert await _live(maker, "panel cleanup-twin-a") == {a}
    assert await _live(maker, "panel cleanup-twin-b") == {b}
