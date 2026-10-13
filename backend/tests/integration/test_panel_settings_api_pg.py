"""The settings sync against real Postgres and the real app: the PWA asks, the panel takes it.

What matters (owner, approved 2026-10-13): the PWA shows what the desk panel last reported; a
change waits on the box, however long the panel is away, until the panel fetches it on the
pickup's signal and reports it back; when the panel and the PWA change the same setting, the
panel's last report wins; and a twin's panel gets nothing from any of it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.api import deps, panel_settings
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.jpanel import debug_relay
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

Maker = async_sessionmaker[AsyncSession]

REPORT = {
    "na": 1, "nq": 1, "nc": 1, "nf": 1290, "nt": 390, "tz": 5,
    "no": 1, "nk": 0, "sd": 1, "br": 50, "vo": 80, "ra": 1, "ar": 1,
}  # fmt: skip


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[Maker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.endpoint_panel_setting"))
        await s.commit()
    panel_settings.forget()
    yield sessions
    await engine.dispose()


@dataclass
class Box:
    client: TestClient
    app: FastAPI
    keys: dict[str, str]
    devices: dict[str, str]

    def panel(self, who: str, method: str, path: str, **kw: Any) -> Any:
        self.client.cookies.clear()
        headers = {"Authorization": f"Bearer {self.keys[who]}", **kw.pop("headers", {})}
        return self.client.request(method, path, headers=headers, **kw)

    def pickup(self, who: str = "Jeff", **report: int) -> Any:
        query = "&".join(f"{k}={v}" for k, v in ({**REPORT, **report}).items())
        return self.panel(who, "GET", f"/api/endpoint/debug/next?board=c6-amoled-216&{query}")

    def owner(self, method: str, path: str, **kw: Any) -> Any:
        owner = PrincipalInfo(id=OWNER.principal_id or "", kind="owner", label="owner")
        self.app.dependency_overrides[deps.owner_only] = lambda: owner
        try:
            return self.client.request(method, path, **kw)
        finally:
            self.app.dependency_overrides.clear()

    def url(self, who: str = "Jeff") -> str:
        return f"/api/endpoint/panels/{self.devices[who]}/settings"

    def ask(self, **changes: int) -> Any:
        return self.owner("PUT", self.url(), json={"changes": changes})


@pytest.fixture
def box(database_url: str, maker: Maker, tmp_path: Path) -> Iterator[Box]:  # noqa: F811
    repo = SqlAuthRepo(maker)
    owner_key = asyncio.run(service.rotate_owner_key(repo))
    debug_key, _ = asyncio.run(service.mint_capability(repo, "claude", ttl_hours=1))
    settings = Settings(
        secure_cookies=False,
        database_url=database_url,
        debug_access_enabled=True,
        blob_dir=str(tmp_path),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
        made = {
            name: client.post("/api/devices", json={"label": f"panel {name}", "device_role": role})
            for name, role in (("Jeff", "display"), ("Quorra", "jpet"))
        }
        keys = {name: r.json()["key"] for name, r in made.items()}
        client.cookies.clear()
        listed = client.get(
            "/api/debug/panel", headers={"Authorization": f"Bearer {debug_key}"}
        ).json()
        devices = {p["name"]: p["device_id"] for p in listed}
        yield Box(client, app, keys, devices)


def test_before_any_report_the_pwa_has_nothing_to_ask_over(box: Box) -> None:
    got = box.owner("GET", box.url()).json()
    assert got["supported"] is False and got["reported"] is None and got["pending"] == {}
    assert got["zones"][5] == "New York" and got["night_colors"] == ["Red", "Amber", "Grey"]
    refused = box.ask(brightness=60)
    assert refused.status_code == 409 and refused.json()["detail"]["reason"] == "not_reported"
    # A pickup from firmware before 0.0.21 (the notice switches only) reports no settings.
    old = {k: REPORT[k] for k in ("na", "nq", "nc", "nf", "nt", "tz")}
    query = "&".join(f"{k}={v}" for k, v in old.items())
    assert box.panel("Jeff", "GET", f"/api/endpoint/debug/next?{query}").status_code == 204
    assert box.owner("GET", box.url()).json()["supported"] is False


def test_a_change_waits_for_the_panel_and_settles_on_its_report(box: Box) -> None:
    first = box.pickup()
    assert first.status_code == 204 and "X-Panel-Settings" not in first.headers
    seen = box.owner("GET", box.url()).json()
    assert seen["supported"] and seen["online"] and seen["reported"]["brightness"] == 50
    assert seen["reported_s"] is not None and seen["reported_s"] < 30

    asked = box.ask(brightness=60, night_color=1)
    assert asked.status_code == 200, asked.text
    assert asked.json()["pending"] == {"brightness": 60, "night_color": 1}
    version = asked.json()["version"]

    # The pickup still reports the old values: the request waits, and the answer says so.
    waiting = box.pickup()
    assert waiting.headers["X-Panel-Settings"] == str(version)
    pending = box.panel("Jeff", "GET", "/api/endpoint/panel-settings").json()
    assert pending == {"v": version, "s": {"br": [60, 50], "nk": [1, 0]}}

    # The panel applied one; its next report settles that one and keeps the other.
    half = box.pickup(br=60)
    assert half.headers["X-Panel-Settings"] == str(version)
    assert box.owner("GET", box.url()).json()["pending"] == {"night_color": 1}
    done = box.pickup(br=60, nk=1)
    assert "X-Panel-Settings" not in done.headers
    after = box.owner("GET", box.url()).json()
    assert after["pending"] == {} and after["reported"]["night_color"] == 1


def test_the_panel_s_last_report_wins_a_race(box: Box) -> None:
    box.pickup()
    box.ask(volume=60)
    # Changed at the glass to 40 before the panel took the PWA's 60: the request is dropped.
    raced = box.pickup(vo=40)
    assert "X-Panel-Settings" not in raced.headers
    got = box.owner("GET", box.url()).json()
    assert got["pending"] == {} and got["reported"]["volume"] == 40


def test_a_change_waits_however_long_the_panel_is_away(box: Box) -> None:
    box.pickup()
    debug_relay._seen.pop(box.devices["Jeff"], None)  # the panel went quiet, the box restarted
    panel_settings.forget()
    away = box.owner("GET", box.url()).json()
    assert away["online"] is False and away["supported"]
    box.ask(auto_rotate=0)
    assert box.owner("GET", box.url()).json()["pending"] == {"auto_rotate": 0}
    back = box.pickup()
    assert "X-Panel-Settings" in back.headers
    assert box.pickup(ar=0).headers.get("X-Panel-Settings") is None


def test_asking_for_what_the_panel_holds_withdraws_a_request(box: Box) -> None:
    box.pickup()
    box.ask(show_date=0)
    assert box.ask(show_date=1).json()["pending"] == {}
    assert "X-Panel-Settings" not in box.pickup().headers


def test_the_owner_s_request_is_checked(box: Box) -> None:
    box.pickup()
    for bad in ({"brightness": 5}, {"volume": 101}, {"mic_gain": 30}, {"tz": 99}):
        r = box.owner("PUT", box.url(), json={"changes": bad})
        assert r.status_code == 422, bad
        assert r.json()["detail"]["reason"] == "bad_setting"


def test_a_twin_s_panel_gets_nothing_from_any_door(box: Box) -> None:
    assert box.owner("GET", box.url("Quorra")).status_code == 409
    assert box.panel("Quorra", "GET", "/api/endpoint/panel-settings").status_code == 403
    # Its pickup (it never makes one; if it did) stores nothing and signals nothing.
    r = box.pickup("Quorra")
    assert "X-Panel-Settings" not in r.headers
    assert (
        box.owner("GET", f"/api/endpoint/panels/{box.devices['Quorra']}/settings").status_code
        == 409
    )
    assert box.owner("GET", "/api/endpoint/panels/not-a-uuid/settings").status_code == 404


def test_unauthenticated_is_refused(box: Box) -> None:
    box.client.cookies.clear()
    assert box.client.get("/api/endpoint/panel-settings").status_code == 401
    assert box.client.get(box.url()).status_code == 401


def test_an_owner_change_between_a_pickup_s_read_and_write_survives(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review of #1736: the pickup read the row unlocked and wrote back a stale `desired`,
    so a change the owner made in between vanished without being applied or refused."""
    box.pickup()
    box.ask(volume=60)
    read = panel_settings._row
    raced: list[bool] = []

    async def owner_asks_mid_pickup(s: Any, sid: str) -> Any:
        row = await read(s, sid)
        if not raced:
            raced.append(True)
            maker = box.app.state.session_maker
            await panel_settings.ask(maker, OWNER, box.devices["Jeff"], {"brightness": 70})
        return row

    monkeypatch.setattr(panel_settings, "_row", owner_asks_mid_pickup)
    box.pickup(vo=60)  # settles the volume while the owner asks for a brightness
    monkeypatch.setattr(panel_settings, "_row", read)
    assert raced
    assert box.owner("GET", box.url()).json()["pending"].get("brightness") == 70
    assert "X-Panel-Settings" in box.pickup(vo=60).headers
