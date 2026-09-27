"""The on-demand report, end to end: raise the counter, the panel sees it, the ring reads back.

WHY THIS ROUND TRIP IS WORTH A CONTAINER. The three pieces live in three places — the counter
is a column, the settings route serves it to a panel authenticated by its own device key, and
the ring comes back out of a `jsonb` column written by a different route under a different
principal. Each half has passed on its own before while the whole failed: the appearance the
settings route served was read under a context with no subject pin, so every panel was told
the default for four days with every unit test green.

So this drives the real app against real Postgres, in the order the box actually does it:
the panel polls, the owner's debug token raises the counter, the panel polls again and sees a
different number, the panel reports, and the debug read shows the ring it posted.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # The container is shared and this file counts panels and reads a global counter, so it
    # starts from a known fleet and a known number.
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.endpoint_status"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        await s.execute(text("UPDATE app.endpoint_settings SET telemetry_seq = 0 WHERE id = 1"))
        await s.commit()
    yield sessions
    await engine.dispose()


async def test_raising_the_counter_reaches_a_panel_and_the_ring_reads_back_named(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    owner_key = await service.rotate_owner_key(SqlAuthRepo(maker))
    debug_key, _ = await service.mint_capability(SqlAuthRepo(maker), "claude", ttl_hours=1)
    # `debug_access_enabled`, WITHOUT WHICH THE ROUTER IS NOT MOUNTED AT ALL and every
    # call here is a 404 rather than the refusal or the answer it is asserting. The
    # debug surface is opt-in per deployment (`main.py`), so a test that forgets the
    # flag is testing an app that has no debug API — which is exactly what this file
    # did on its first CI run.
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, debug_access_enabled=True)
    )
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
        panel = client.post(
            "/api/devices", json={"label": "panel Elora", "device_role": "jpet"}
        ).json()
        dbg = {"Authorization": f"Bearer {debug_key}"}
        pk = {"Authorization": f"Bearer {panel['key']}"}

        # WHAT THE PANEL SEES FIRST is the baseline it adopts in silence. Zero here is what
        # makes "never raised" indistinguishable from "nothing is being asked of you".
        client.cookies.clear()
        first = client.get("/api/endpoint/settings", headers=pk).json()
        assert first["telemetry_seq"] == 0

        raised = client.post("/api/debug/endpoint/report-now", headers=dbg)
        assert raised.status_code == 200, raised.text
        assert raised.json()["telemetry_seq"] == 1

        # THE NUMBER THE PANEL WOULD ACT ON. Different from the baseline is the whole
        # protocol — the panel holds no other state and the box keeps nothing to clear.
        client.cookies.clear()
        second = client.get("/api/endpoint/settings", headers=pk).json()
        assert second["telemetry_seq"] == 1, "the raise never reached the panel's own poll"

        # The panel obliges, in the five-field shape 0.3.10 and later actually send.
        client.cookies.clear()
        posted = client.post(
            "/api/endpoint/telemetry",
            headers=pk,
            json={
                "version": "0.3.14",
                "uptime_ms": 42_000,
                "heard": [
                    ["tell sister", 17, 0, 1200, "TfL SgSTk"],
                    ["tell dad", 41, 1, 1, "TfL DaD"],
                ],
            },
        )
        assert posted.status_code == 204, posted.text

        got = client.get("/api/debug/endpoint/heard", headers=dbg)
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["telemetry_seq"] == 1, "the read should say which raise it reflects"
        assert len(body["panels"]) == 1
        ring = body["panels"][0]
        assert ring["label"] == "panel Elora"
        assert ring["version"] == "0.3.14"

        # NAMED, WHICH IS THE POINT OF THE ROUTE. The wire is a positional tuple and this is
        # the surface a person reads a diagnosis off.
        missed = ring["heard"][0]
        assert missed["phrase"] == "tell sister"
        assert missed["fired"] is False
        assert missed["raw"] == "TfL SgSTk", "the phoneme string is the reason this exists"
        # Past the old `uint8_t` ceiling, which saturated at 255 on a real panel.
        assert missed["count"] == 1200
        assert ring["heard"][1]["fired"] is True


async def test_a_second_raise_is_a_different_number(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """MONOTONIC, NOT A TOGGLE. A panel posts on CHANGE, so a counter that ever repeated a
    value it had already served would be a request that silently did nothing — which is the
    failure mode this whole mechanism exists to avoid, since the caller is measuring and would
    read the previous ring as the new one."""
    debug_key, _ = await service.mint_capability(SqlAuthRepo(maker), "claude", ttl_hours=1)
    # `debug_access_enabled`, WITHOUT WHICH THE ROUTER IS NOT MOUNTED AT ALL and every
    # call here is a 404 rather than the refusal or the answer it is asserting. The
    # debug surface is opt-in per deployment (`main.py`), so a test that forgets the
    # flag is testing an app that has no debug API — which is exactly what this file
    # did on its first CI run.
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, debug_access_enabled=True)
    )
    with TestClient(app) as client:
        dbg = {"Authorization": f"Bearer {debug_key}"}
        seen = [
            client.post("/api/debug/endpoint/report-now", headers=dbg).json()["telemetry_seq"]
            for _ in range(3)
        ]
    assert seen == [1, 2, 3], f"the counter must only ever go up; got {seen}"


async def test_the_debug_routes_are_not_reachable_without_the_token(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`report-now` WRITES, and both routes read every panel's telemetry. A capability token
    is on a physically distinct path from the owner cookie, so this gate is the only one."""
    # `debug_access_enabled`, WITHOUT WHICH THE ROUTER IS NOT MOUNTED AT ALL and every
    # call here is a 404 rather than the refusal or the answer it is asserting. The
    # debug surface is opt-in per deployment (`main.py`), so a test that forgets the
    # flag is testing an app that has no debug API — which is exactly what this file
    # did on its first CI run.
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, debug_access_enabled=True)
    )
    with TestClient(app) as client:
        assert client.get("/api/debug/endpoint/heard").status_code == 401
        assert client.post("/api/debug/endpoint/report-now").status_code == 401
        bad = {"Authorization": "Bearer nonsense"}
        assert client.get("/api/debug/endpoint/heard", headers=bad).status_code == 401
        assert client.post("/api/debug/endpoint/report-now", headers=bad).status_code == 401
