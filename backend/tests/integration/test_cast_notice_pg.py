"""Cast notices end to end against real Postgres (JPANEL_CAST_PLAN.md K1, migration 0250).

The sidecar is an `httpx.MockTransport` holding one TV in each state the gate knows, so what
is pinned is the box's side: each TV's outcome is what the sidecar said at the instant of
casting (`shown`, `woke` for a TV that was off, `skipped` with the gate's words, `failed`),
the record reads back the same through the owner's and the debug token's routes, Take it down
asks only for the casts that are ours, a week-old notice is swept with its frame, and both
tables are the owner's alone (CLAUDE.md #3).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.cast import sidecar as cast_sidecar
from jbrain.cast.service import CastService
from jbrain.cast.sidecar import Sidecar
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

Maker = async_sessionmaker[AsyncSession]
EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)
IDLE = "00000000-0000-4000-8000-000000000001"
OFF = "00000000-0000-4000-8000-000000000002"
NETFLIX = "00000000-0000-4000-8000-000000000003"
BROKEN = "00000000-0000-4000-8000-000000000004"
GONE = "00000000-0000-4000-8000-000000000005"
NAMES = {IDLE: "Big TV", OFF: "Parents", NETFLIX: "Twins room", BROKEN: "Den"}


class Secrets:
    def read(self, name: str) -> bytes | None:
        return b"cast-tok" if name == "cast" else None


@dataclass
class FakeSidecar:
    """The sidecar's routes, one TV per gate state."""

    casts: list[dict[str, Any]] = field(default_factory=list)
    stops: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer cast-tok"
        path = request.url.path
        if path == "/tvs":
            tvs = [{"id": k, "name": v, "state": "idle"} for k, v in NAMES.items()]
            return httpx.Response(200, json={"tvs": tvs, "ignored": []})
        tv, leaf = path.removeprefix("/tvs/").split("/")
        body = json.loads(request.content)
        if leaf == "stop":
            self.stops.append((tv, body["cast_id"]))
            return httpx.Response(200, json={"id": tv, "stopped": tv == IDLE})
        self.casts.append({"tv": tv, **body})
        if tv == NETFLIX:
            return httpx.Response(
                409, json={"error": "not idle", "state": "busy", "why": "Netflix open"}
            )
        if tv == BROKEN:
            return httpx.Response(500, json={"error": "RuntimeError: socket closed"})
        before = ("standby", "standby") if tv == OFF else ("idle", "home screen")
        return httpx.Response(
            202,
            json={
                "id": tv,
                "state_before": before[0],
                "why_before": before[1],
                "seconds": body["seconds"],
                "cast_id": f"cast-{tv[-1]}",
            },
        )


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[Maker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.cast_notice"))
        await s.commit()
    yield sessions
    await engine.dispose()


@dataclass
class Box:
    client: TestClient
    owner: dict[str, str]
    dbg: dict[str, str]
    sidecar: FakeSidecar
    maker: Maker
    blob_dir: Path

    def as_owner(self) -> TestClient:
        self.client.cookies.clear()
        self.client.cookies.update(self.owner)
        return self.client


@pytest.fixture
def box(
    database_url: str,  # noqa: F811
    maker: Maker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Box]:
    repo = SqlAuthRepo(maker)
    owner_key = asyncio.run(service.rotate_owner_key(repo))
    debug_key, _ = asyncio.run(service.mint_capability(repo, "claude", ttl_hours=1))
    app = create_app(
        Settings(
            secure_cookies=False,
            database_url=database_url,
            debug_access_enabled=True,
            blob_dir=str(tmp_path),
        )
    )
    fake = FakeSidecar()
    monkeypatch.setattr(cast_sidecar, "_transport", httpx.MockTransport(fake))
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
        owner = dict(client.cookies)
        app.state.cast_notices = CastService(
            maker,
            app.state.blob_store,
            Sidecar(Secrets(), "http://cast.test"),  # type: ignore[arg-type]
        )
        client.cookies.clear()
        yield Box(
            client=client,
            owner=owner,
            dbg={"Authorization": f"Bearer {debug_key}"},
            sidecar=fake,
            maker=maker,
            blob_dir=tmp_path,
        )


def _png_b64(w: int = 640, h: int = 480) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 90, 200)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _by_tv(notice: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["tv_id"]: t for t in notice["tvs"]}


def test_each_tv_takes_the_notice_as_the_sidecar_found_it(box: Box) -> None:
    resp = box.as_owner().post(
        "/api/cast/send",
        json={
            "tv_ids": [IDLE, OFF, NETFLIX, BROKEN, GONE, IDLE],
            "title": "Dinner is ready",
            "body": "Come down now",
            "seconds": 45,
        },
    )
    assert resp.status_code == 200, resp.text
    notice = resp.json()
    assert notice["sender"] == "owner"
    tvs = _by_tv(notice)
    assert tvs[IDLE] == {"tv_id": IDLE, "name": "Big TV", "outcome": "shown", "why": "home screen"}
    assert tvs[OFF]["outcome"] == "woke" and tvs[OFF]["why"] == "was off"
    assert tvs[NETFLIX]["outcome"] == "skipped" and tvs[NETFLIX]["why"] == "Netflix open"
    assert tvs[BROKEN]["outcome"] == "failed" and "socket closed" in tvs[BROKEN]["why"]
    assert tvs[GONE] == {
        "tv_id": GONE,
        "name": "TV",
        "outcome": "skipped",
        "why": "not on the network",
    }
    # One cast per TV the sidecar knows, the duplicate folded; every one the same PNG frame.
    assert sorted(c["tv"] for c in box.sidecar.casts) == sorted([IDLE, OFF, NETFLIX, BROKEN])
    frames = {c["png_b64"] for c in box.sidecar.casts}
    assert len(frames) == 1
    png = base64.b64decode(frames.pop())
    assert Image.open(io.BytesIO(png)).size == (1920, 1080)
    assert {c["seconds"] for c in box.sidecar.casts} == {45}
    assert {c["title"] for c in box.sidecar.casts} == {"Dinner is ready"}

    # The record reads back the same, and the frame is the one the TVs fetched.
    again = box.as_owner().get(f"/api/cast/{notice['id']}").json()
    assert _by_tv(again) == tvs
    frame = box.as_owner().get(f"/api/cast/{notice['id']}/frame")
    assert frame.status_code == 200
    assert frame.content == png
    assert "immutable" in frame.headers["cache-control"]
    sent = box.as_owner().get("/api/cast/sent").json()
    assert [n["id"] for n in sent["notices"]] == [notice["id"]]
    assert sent["kept_s"] == 7 * 24 * 3600


def test_take_it_down_asks_only_for_the_casts_that_are_ours(box: Box) -> None:
    notice = (
        box.as_owner()
        .post("/api/cast/send", json={"tv_ids": [IDLE, OFF, NETFLIX], "title": "Bed time"})
        .json()
    )
    out = box.as_owner().post(f"/api/cast/{notice['id']}/stop").json()
    # The skipped TV was never ours; the sidecar says the woken one was taken over since.
    assert out["stopped"] == {IDLE: True, OFF: False}
    assert sorted(box.sidecar.stops) == sorted([(IDLE, "cast-1"), (OFF, "cast-2")])
    assert box.as_owner().get(f"/api/cast/{notice['id']}").json()["stopped_at"] is not None


def test_a_picture_is_drawn_and_a_bad_one_is_refused_before_anything_is_cast(box: Box) -> None:
    ok = box.as_owner().post(
        "/api/cast/send",
        json={
            "tv_ids": [IDLE],
            "title": "Look",
            "body": "the new puppy",
            "picture_b64": _png_b64(),
        },
    )
    assert ok.status_code == 200, ok.text
    box.sidecar.casts.clear()
    bad = box.as_owner().post(
        "/api/cast/send",
        json={"tv_ids": [IDLE], "title": "Look", "picture_b64": base64.b64encode(b"no").decode()},
    )
    assert bad.status_code == 415
    assert bad.json()["detail"]["reason"] == "bad_picture"
    garbled = box.as_owner().post(
        "/api/cast/send", json={"tv_ids": [IDLE], "title": "Look", "picture_b64": "%%%"}
    )
    assert garbled.status_code == 422
    assert box.sidecar.casts == []


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        ({"title": "   "}, 422, "bad_field"),
        ({"title": "é" * 61}, 422, "too_long"),
        ({"title": "ok", "body": "x" * 601}, 422, "too_long"),
    ],
)
def test_a_bad_notice_is_refused_at_the_door(
    box: Box, body: dict[str, Any], status: int, reason: str
) -> None:
    resp = box.as_owner().post("/api/cast/send", json={"tv_ids": [IDLE], **body})
    assert resp.status_code == status
    assert resp.json()["detail"]["reason"] == reason
    assert box.sidecar.casts == []


def test_preview_draws_the_frame_and_casts_nothing(box: Box) -> None:
    resp = box.as_owner().post("/api/cast/preview", json={"title": "Dinner", "body": "now"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(resp.content)).size == (1920, 1080)
    assert box.sidecar.casts == []


def test_the_tvs_come_live_from_the_sidecar(box: Box) -> None:
    tvs = box.as_owner().get("/api/cast/tvs").json()["tvs"]
    assert {t["id"] for t in tvs} == set(NAMES)


def test_an_unknown_notice_is_not_found(box: Box) -> None:
    owner = box.as_owner()
    assert owner.get("/api/cast/not-a-uuid").status_code == 404
    missing = "00000000-0000-4000-8000-0000000000ff"
    assert owner.get(f"/api/cast/{missing}").status_code == 404
    assert owner.get(f"/api/cast/{missing}/frame").status_code == 404
    assert owner.post(f"/api/cast/{missing}/stop").status_code == 404


def test_the_routes_are_the_owners(box: Box) -> None:
    box.client.cookies.clear()
    assert box.client.get("/api/cast/sent").status_code == 401
    assert (
        box.client.post("/api/cast/send", json={"tv_ids": [IDLE], "title": "x"}).status_code == 401
    )


def test_a_debug_token_casts_as_jbrain_test_and_reads_the_record(box: Box) -> None:
    box.client.cookies.clear()
    resp = box.client.post(
        "/api/debug/cast/send", headers=box.dbg, json={"tv_ids": [IDLE], "title": "Test"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sender"] == "debug"
    sent = box.client.get("/api/debug/cast/sent", headers=box.dbg).json()
    assert [n["sender"] for n in sent["notices"]] == ["debug"]


def test_a_week_old_notice_is_swept_with_its_frame(box: Box) -> None:
    old = box.as_owner().post("/api/cast/send", json={"tv_ids": [IDLE], "title": "Old"}).json()
    sha = _owner_scalar(box.maker, "SELECT frame_sha256 FROM app.cast_notice")
    assert list(box.blob_dir.rglob(sha))
    _owner_exec(
        box.maker,
        "UPDATE app.cast_notice SET created_at = now() - interval '8 days'",
    )
    sent = box.as_owner().get("/api/cast/sent").json()
    assert sent["notices"] == []
    assert box.as_owner().get(f"/api/cast/{old['id']}").status_code == 404
    assert _owner_scalar(box.maker, "SELECT count(*) FROM app.cast_notice_tv") == 0
    assert not list(box.blob_dir.rglob(sha))


def _owner_exec(maker: Maker, sql: str) -> None:
    async def run() -> None:
        async with scoped_session(maker, OWNER) as s:
            await s.execute(text(sql))
            await s.commit()

    asyncio.run(run())


def _owner_scalar(maker: Maker, sql: str) -> Any:
    async def run() -> Any:
        async with scoped_session(maker, OWNER) as s:
            return (await s.execute(text(sql))).scalar_one()

    return asyncio.run(run())


# ---- RLS: both tables are the owner's alone --------------------------------------------------


async def _seed(maker: Maker) -> str:
    async with scoped_session(maker, OWNER) as s:
        nid = (
            await s.execute(
                text(
                    "INSERT INTO app.cast_notice (sender, title, body, frame_sha256, seconds)"
                    " VALUES ('owner', 't', '', :sha, 30) RETURNING id"
                ),
                {"sha": "a" * 64},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO app.cast_notice_tv (notice_id, tv_id, tv_name, outcome, why)"
                " VALUES (:n, :tv, 'Big TV', 'shown', '')"
            ),
            {"n": nid, "tv": IDLE},
        )
        await s.commit()
    return str(nid)


@pytest.mark.parametrize("ctx", [UNSCOPED, EVERY_SCOPE], ids=["unscoped", "every-scope"])
async def test_no_one_but_the_owner_reads_or_writes_a_notice(
    maker: Maker, ctx: SessionContext
) -> None:
    nid = await _seed(maker)
    async with scoped_session(maker, ctx) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.cast_notice"))).scalar_one() == 0
        assert (await s.execute(text("SELECT count(*) FROM app.cast_notice_tv"))).scalar_one() == 0
        # Deletes nothing: the owner's count below is still 1.
        await s.execute(text("DELETE FROM app.cast_notice"))
        await s.commit()
    for sql, params in (
        (
            "INSERT INTO app.cast_notice (sender, title, body, frame_sha256, seconds)"
            " VALUES ('owner', 'x', '', :sha, 30)",
            {"sha": "b" * 64},
        ),
        (
            "INSERT INTO app.cast_notice_tv (notice_id, tv_id, tv_name, outcome, why)"
            " VALUES (CAST(:n AS uuid), 'x', 'x', 'shown', '')",
            {"n": nid},
        ),
    ):
        async with scoped_session(maker, ctx) as s:
            with pytest.raises((DBAPIError, ProgrammingError)):
                await s.execute(text(sql), params)
    async with scoped_session(maker, OWNER) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.cast_notice"))).scalar_one() == 1
        assert (await s.execute(text("SELECT count(*) FROM app.cast_notice_tv"))).scalar_one() == 1
