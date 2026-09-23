"""The fleet view end to end: a panel reports, the owner reads it from his phone.

This is the route that exists because the owner has no terminal (CLAUDE.md #10). The panel has
reported richly for months and the only reader was `grep` over the box's structured log — which
is what "just your update only has 0.2.88" cost, on a panel that had in fact updated forty
minutes earlier. Driven through the real app against real Postgres, because the half worth
testing is the round trip: what a panel posts with its own key is what the owner sees.
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
    # This file counts panels; the container is shared, so it starts from a known fleet.
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.endpoint_status"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        await s.commit()
    yield sessions
    await engine.dispose()


def _report(client: TestClient, key: str, body: dict) -> None:
    """A panel reporting with its own key — cookies cleared, because the owner cookie also
    satisfies the panel dependency and authenticates first."""
    client.cookies.clear()
    answer = client.post(
        "/api/endpoint/telemetry", json=body, headers={"Authorization": f"Bearer {key}"}
    )
    assert answer.status_code == 204, answer.text


async def test_the_owner_sees_what_each_panel_last_said_about_itself(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    owner_key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        assert client.get("/api/endpoint/status").status_code == 401  # owner-gated

        def login() -> None:
            client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})

        login()
        ellie = client.post("/api/devices", json={"label": "panel Ellie"}).json()
        client.post("/api/devices", json={"label": "room endpoint panel"})

        _report(
            client,
            ellie["key"],
            {
                "version": "0.2.94",
                "uptime_ms": 7_200_000,
                "screen": "dark",
                "blit_ok": 41233,
                "restart_why": "ota-park",
                "mic_peak": 9123,
            },
        )
        login()

        panels = {p["name"]: p for p in client.get("/api/endpoint/status").json()["panels"]}
        assert set(panels) == {"Ellie", "the other one"}

        reported = panels["Ellie"]
        assert reported["version"] == "0.2.94"
        assert reported["report"]["screen"] == "dark"
        assert reported["report"]["restart_why"] == "ota-park"
        # Computed on the BOX, so a phone with a drifting clock cannot render "last seen four
        # minutes in the future" and make a healthy fleet look broken.
        assert 0 <= reported["age_s"] < 60
        assert reported["reported_at"]

        # A PANEL THAT HAS NEVER REPORTED HAS NO READING, and that is a different answer from
        # a panel that reported and went quiet: one was never provisioned against this box,
        # the other was working and stopped.
        silent = panels["the other one"]
        assert silent["version"] == ""
        assert silent["age_s"] == -1
        assert silent["report"] == {}


async def test_a_second_report_replaces_the_first(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A snapshot, not a history. The `pmu_history` ring inside the report already carries the
    only series anything has needed, and a growing table would be a retention question nobody
    has asked."""
    owner_key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
        ellie = client.post("/api/devices", json={"label": "panel Ellie"}).json()

        _report(client, ellie["key"], {"version": "0.2.89", "uptime_ms": 1, "ota_err": "X"})
        _report(client, ellie["key"], {"version": "0.2.94", "uptime_ms": 2})
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})

        panels = client.get("/api/endpoint/status").json()["panels"]
        assert len(panels) == 1
        assert panels[0]["version"] == "0.2.94"
        # The failed update is GONE rather than sticky: the panel stopped saying it, and a
        # fleet view that kept showing a fault the device has recovered from would be worse
        # than one that showed nothing.
        assert panels[0]["report"]["ota_err"] == ""


async def test_a_re_flashed_panel_appears_once(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Every flash mints a fresh device key and nothing retires the old one, so a panel flashed
    four times is four principals under one label. Without `DISTINCT ON (label)` the owner's
    fleet view would list a unit once per time it was ever flashed, and the newest is the only
    one it is actually using."""
    owner_key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
        for _ in range(3):
            client.post("/api/devices", json={"label": "panel Ellie"})
        newest = client.post("/api/devices", json={"label": "panel Ellie"}).json()

        _report(client, newest["key"], {"version": "0.2.94", "uptime_ms": 1})
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})

        panels = client.get("/api/endpoint/status").json()["panels"]
        assert len(panels) == 1, "four keys, one panel"
        assert panels[0]["version"] == "0.2.94", "and the newest key is the one reporting"
