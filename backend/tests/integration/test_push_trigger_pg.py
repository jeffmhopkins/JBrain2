"""The geofence-crossing → content-free poke trigger, end to end (JBrain360 M6c).

`detect_transitions`, given a configured notifier, fires one poke to the crossing
subject's family group on a real enter/exit — and nothing on a debounced fix or
when no notifier is wired. Proven against real Postgres + PostGIS.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import device_context, scoped_session
from jbrain.locations.geofence import detect_transitions
from jbrain.push import SqlFcmTokenRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_HOME = (40.0, -74.0)


class RecordingNotifier:
    def __init__(self) -> None:
        self.poked: list[list[str]] = []

    async def poke(self, tokens: list[str]) -> None:
        self.poked.append(tokens)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _device(maker: async_sessionmaker) -> tuple[str, str]:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, 'phone', 'device')"
            ),
            {"s": sid},
        )
        await session.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash)"
                " VALUES (:p, 'device_key', :s, :kh)"
            ),
            {"p": pid, "s": sid, "kh": uuid.uuid4().hex},
        )
    return pid, sid


async def _group(maker: async_sessionmaker, members: list[str]) -> None:
    async with scoped_session(maker, OWNER) as session:
        gid = (
            await session.execute(
                text("INSERT INTO app.family_group (name) VALUES ('family') RETURNING id")
            )
        ).scalar()
        for sid in members:
            await session.execute(
                text("INSERT INTO app.view_scope (group_id, member_subject_id) VALUES (:g, :s)"),
                {"g": str(gid), "s": sid},
            )


async def _fence(maker: async_sessionmaker) -> None:
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Place', 'Home', 'location') RETURNING id"
                )
            )
        ).scalar()
        await session.execute(
            text(
                "INSERT INTO app.place_geofence"
                " (place_entity_id, domain_code, name, center, radius_m)"
                " VALUES (:e, 'location', 'Home',"
                " ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, 120)"
            ),
            {"e": eid, "lat": _HOME[0], "lon": _HOME[1]},
        )


async def _detect(maker, pid, sid, minute, notifier):  # noqa: ANN001
    return await detect_transitions(
        maker,
        principal_id=pid,
        subject_id=sid,
        captured_at=datetime(2026, 6, 4, 12, minute, tzinfo=UTC),
        latitude=_HOME[0],
        longitude=_HOME[1],
        notifier=notifier,
    )


async def test_a_confirmed_crossing_pokes_the_family_group(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker)
    pid_b, sid_b = await _device(maker)
    await _group(maker, [sid_a, sid_b])
    await SqlFcmTokenRepo(maker).register(
        device_context(pid_b, sid_b), principal_id=pid_b, subject_id=sid_b, token=f"b-{pid_b}"
    )
    await _fence(maker)
    notifier = RecordingNotifier()

    # The first inside fix is debounced — no crossing, so no poke.
    await _detect(maker, pid_a, sid_a, 0, notifier)
    assert notifier.poked == []

    # The confirming fix fires the enter → Alice's co-member Bob is poked once.
    await _detect(maker, pid_a, sid_a, 1, notifier)
    assert notifier.poked == [[f"b-{pid_b}"]]


async def test_no_notifier_means_no_poke(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker)
    pid_b, sid_b = await _device(maker)
    await _group(maker, [sid_a, sid_b])
    await SqlFcmTokenRepo(maker).register(
        device_context(pid_b, sid_b), principal_id=pid_b, subject_id=sid_b, token=f"b2-{pid_b}"
    )
    await _fence(maker)

    # No notifier wired (FCM unconfigured): the crossing still fires and the poke
    # path is simply skipped — proven by the enter returning normally, no raise.
    # (The module DB accumulates fences across tests, so assert an enter fired, not
    # an exact count.)
    await _detect(maker, pid_a, sid_a, 0, None)
    enter = await _detect(maker, pid_a, sid_a, 1, None)
    assert "enter" in [t["transition"] for t in enter]
