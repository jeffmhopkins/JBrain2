"""The alarm routes against real Postgres and the real app: the PWA's editor and the panel's sync.

What matters: the owner edits the desk panel's alarms; the desk panel reads exactly its own,
cheaply when nothing changed (304), and switches one on or off — nothing more; a twin's S3 panel
gets nothing from either door.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.api import deps, panel_alarms
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

Maker = async_sessionmaker[AsyncSession]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[Maker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.endpoint_panel_alarm"))
        await s.commit()
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

    def owner(self, method: str, path: str, **kw: Any) -> Any:
        """The owner's door, as the PWA reaches it. The owner dependency is supplied for this
        call only, because every panel call clears the cookie jar."""
        owner = PrincipalInfo(id=OWNER.principal_id or "", kind="owner", label="owner")
        self.app.dependency_overrides[deps.owner_only] = lambda: owner
        try:
            return self.client.request(method, path, **kw)
        finally:
            self.app.dependency_overrides.clear()

    def alarms_url(self, who: str = "Jeff") -> str:
        return f"/api/endpoint/panels/{self.devices[who]}/alarms"


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


WORK = {"at": 405, "days": 31, "on": True, "label": "Work", "sound": "chime"}


def test_the_owner_adds_edits_and_deletes_an_alarm(box: Box) -> None:
    assert box.owner("GET", box.alarms_url()).json()["alarms"] == []
    made = box.owner("POST", box.alarms_url(), json=WORK)
    assert made.status_code == 201, made.text
    aid = made.json()["id"]
    assert made.json() == {"id": aid, **WORK}

    changed = box.owner(
        "PUT", f"{box.alarms_url()}/{aid}", json={**WORK, "at": 410, "sound": "rise", "days": 0}
    )
    assert changed.json()["at"] == 410 and changed.json()["sound"] == "rise"
    listed = box.owner("GET", box.alarms_url()).json()
    assert [a["at"] for a in listed["alarms"]] == [410] and listed["max"] == 8

    assert box.owner("DELETE", f"{box.alarms_url()}/{aid}").status_code == 204
    assert box.owner("DELETE", f"{box.alarms_url()}/{aid}").status_code == 404
    assert box.owner("GET", box.alarms_url()).json()["alarms"] == []


def test_the_owner_s_input_is_checked(box: Box) -> None:
    for bad in (
        {**WORK, "at": 1440},
        {**WORK, "days": 128},
        {**WORK, "label": "x" * 25},
        {**WORK, "label": "Café"},
        {**WORK, "label": 'say "hi"'},
        {**WORK, "label": "back\\slash"},
        {**WORK, "sound": "siren"},
    ):
        assert box.owner("POST", box.alarms_url(), json=bad).status_code == 422, bad
    spaced = box.owner("POST", box.alarms_url(), json={**WORK, "label": "  Girls   to bed "})
    assert spaced.json()["label"] == "Girls to bed"


def test_the_ninth_alarm_is_refused(box: Box) -> None:
    for m in range(8):
        assert box.owner("POST", box.alarms_url(), json={**WORK, "at": m}).status_code == 201
    r = box.owner("POST", box.alarms_url(), json=WORK)
    assert r.status_code == 409 and "8" in r.json()["detail"]


def test_an_add_that_raced_past_the_count_is_still_409(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's count is only the early answer; when a concurrent add slips past it, the
    trigger (which counts under the subject's lock) refuses, and the owner still gets a 409."""
    for m in range(8):
        box.owner("POST", box.alarms_url(), json={**WORK, "at": m})
    monkeypatch.setattr(panel_alarms, "ALARMS_MAX", 9)
    r = box.owner("POST", box.alarms_url(), json=WORK)
    assert r.status_code == 409, r.text


def test_a_pet_panel_has_no_alarms_and_a_stranger_id_is_404(box: Box) -> None:
    assert box.owner("GET", box.alarms_url("Quorra")).status_code == 409
    assert box.owner("POST", box.alarms_url("Quorra"), json=WORK).status_code == 409
    assert box.owner("GET", "/api/endpoint/panels/not-a-uuid/alarms").status_code == 404
    gone = "/api/endpoint/panels/00000000-0000-0000-0000-000000000000/alarms"
    assert box.owner("GET", gone).status_code == 404


def test_the_desk_panel_reads_its_alarms_and_polls_cheaply(box: Box) -> None:
    aid = box.owner("POST", box.alarms_url(), json=WORK).json()["id"]
    panel_alarms._held.clear()
    assert box.owner("GET", box.alarms_url()).json()["panel_etag"] is None
    got = box.panel("Jeff", "GET", "/api/endpoint/alarms")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["alarms"] == [{"id": aid, **WORK}]
    tag = body["etag"]
    seen = box.owner("GET", box.alarms_url()).json()
    assert seen["panel_etag"] == "" and seen["panel_seen_s"] >= 0, (
        "a fetch is not a store: the panel holds nothing until it says so"
    )
    assert b'"label":"Work"' in got.content

    again = box.panel("Jeff", "GET", f"/api/endpoint/alarms?etag={tag}")
    assert again.status_code == 304 and again.content == b""
    seen = box.owner("GET", box.alarms_url()).json()
    assert seen["panel_etag"] == tag == seen["etag"], (
        "the PWA says 'On the panel' once the panel asks with this version"
    )
    assert again.headers["etag"] == f'"{tag}"'
    by_header = box.panel(
        "Jeff", "GET", "/api/endpoint/alarms", headers={"If-None-Match": f'W/"{tag}"'}
    )
    assert by_header.status_code == 304

    box.owner("PUT", f"{box.alarms_url()}/{aid}", json={**WORK, "label": "Gym"})
    moved = box.panel("Jeff", "GET", f"/api/endpoint/alarms?etag={tag}")
    assert moved.status_code == 200 and moved.json()["etag"] != tag


def test_a_list_the_panel_fetched_but_never_applied_is_not_on_the_panel(box: Box) -> None:
    """The firmware moves its etag only once a fetched list is stored. A 200 it dropped (a
    truncated read, a list it refused) leaves it asking with the old tag, and the PWA must keep
    saying "waiting" rather than "On the panel"."""
    box.owner("POST", box.alarms_url(), json=WORK)
    old = box.panel("Jeff", "GET", "/api/endpoint/alarms").json()["etag"]
    box.panel("Jeff", "GET", f"/api/endpoint/alarms?etag={old}")
    box.owner("POST", box.alarms_url(), json={**WORK, "at": 500})
    new = ""
    for _ in range(2):
        served = box.panel("Jeff", "GET", f"/api/endpoint/alarms?etag={old}")
        new = served.json()["etag"]
        assert served.status_code == 200 and new != old
        seen = box.owner("GET", box.alarms_url()).json()
        assert seen["panel_etag"] == old != seen["etag"]
    box.panel("Jeff", "GET", f"/api/endpoint/alarms?etag={new}")
    seen = box.owner("GET", box.alarms_url()).json()
    assert seen["panel_etag"] == seen["etag"]


def test_the_desk_panel_switches_its_alarm_and_nothing_else(box: Box) -> None:
    aid = box.owner("POST", box.alarms_url(), json=WORK).json()["id"]
    tag = box.panel("Jeff", "GET", "/api/endpoint/alarms").json()["etag"]
    r = box.panel("Jeff", "PUT", f"/api/endpoint/alarms/{aid}", json={"on": False})
    assert r.status_code == 200, r.text
    assert r.json()["on"] is False and r.json()["etag"] != tag
    assert box.owner("GET", box.alarms_url()).json()["alarms"][0]["on"] is False
    # Extra fields are not a way in: the body is `{on}` and nothing else is read.
    box.panel("Jeff", "PUT", f"/api/endpoint/alarms/{aid}", json={"on": True, "at": 1})
    assert box.owner("GET", box.alarms_url()).json()["alarms"][0]["at"] == 405
    assert (
        box.panel("Jeff", "PUT", "/api/endpoint/alarms/999999", json={"on": True}).status_code
        == 404
    )


def test_a_twin_s_panel_is_kept_off_both_doors(box: Box) -> None:
    aid = box.owner("POST", box.alarms_url(), json=WORK).json()["id"]
    assert box.panel("Quorra", "GET", "/api/endpoint/alarms").status_code == 403
    r = box.panel("Quorra", "PUT", f"/api/endpoint/alarms/{aid}", json={"on": False})
    assert r.status_code == 403
    assert box.owner("GET", box.alarms_url()).json()["alarms"][0]["on"] is True
    # A panel key is not the owner: the editor's routes refuse it.
    assert box.panel("Jeff", "GET", box.alarms_url()).status_code == 401
    assert box.panel("Jeff", "POST", box.alarms_url(), json=WORK).status_code == 401


def test_unauthenticated_is_refused(box: Box) -> None:
    box.client.cookies.clear()
    assert box.client.get("/api/endpoint/alarms").status_code == 401


def test_the_etag_follows_what_the_panel_stores() -> None:
    a = panel_alarms.Alarm(id=1, **WORK)  # type: ignore[arg-type]
    b = panel_alarms.Alarm(id=1, **{**WORK, "on": False})  # type: ignore[arg-type]
    assert panel_alarms.etag_of([a]) == panel_alarms.etag_of([a])
    assert panel_alarms.etag_of([a]) != panel_alarms.etag_of([b])
    assert len(panel_alarms.etag_of([])) == 16


async def test_the_panel_door_is_a_panel_s_own(maker: Maker) -> None:
    """The owner's cookie satisfies `PanelDep`, but the panel routes are a panel's own: there is
    no "own alarms" for the owner to read there, so it is refused rather than guessed."""
    owner = PrincipalInfo(id=OWNER.principal_id or "", kind="owner", label="owner")
    with pytest.raises(HTTPException) as caught:
        await panel_alarms._own_display(maker, owner)
    assert caught.value.status_code == 403
    keyless = PrincipalInfo(id="x", kind="device_key", label="x", subject_id="")
    with pytest.raises(HTTPException):
        await panel_alarms._own_display(maker, keyless)


def test_changing_an_alarm_that_is_gone_is_404(box: Box) -> None:
    assert box.owner("PUT", f"{box.alarms_url()}/999999", json=WORK).status_code == 404


def test_if_none_match_reads_like_http() -> None:
    assert panel_alarms._matches("abc", '"abc"')
    assert panel_alarms._matches("abc", 'W/"abc"')
    assert panel_alarms._matches("abc", '"zzz", "abc"')
    assert panel_alarms._matches("abc", "abc")
    assert not panel_alarms._matches("abc", None)
    assert not panel_alarms._matches("abc", '"abd"')
    assert panel_alarms._sent("abc", 'W/"zzz"') == "abc"
    assert panel_alarms._sent(None, 'W/"zzz", "abc"') == "zzz"
    assert panel_alarms._sent(None, None) == ""
