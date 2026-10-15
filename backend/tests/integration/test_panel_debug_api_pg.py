"""The debug relay end to end: a token asks, the panel collects and answers, the token reads.

Driven through the real app against real Postgres, in the order the box does it, with the
panel authenticating by its own device key and the asker by a debug capability token. The
RLS half has its own file (`test_panel_debug_request_rls.py`); this one proves the ROUTES
run under the contexts those policies expect — a sibling's key reaching a request through
the route is refused here exactly as it is in SQL there.
"""

from __future__ import annotations

import io
import struct
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
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
from jbrain.jpanel import debug_relay as relay
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401
from tests.unit.test_panel_debug_relay import jps1

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

C6 = "c6-amoled-216"
Maker = async_sessionmaker[AsyncSession]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[Maker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.panel_debug_request"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        await s.commit()
    relay.forget()
    yield sessions
    relay.forget()
    await engine.dispose()


@dataclass
class Box:
    client: TestClient
    dbg: dict[str, str]
    panel: dict[str, str]
    sibling: dict[str, str]
    device: str
    sibling_device: str
    maker: Maker
    blob_dir: Path
    owner_cookies: dict[str, str]

    def as_panel(  # noqa: ANN201
        self,
        method: str,
        path: str,
        who: dict[str, str] | None = None,
        ctype: str | None = None,
        **kw,
    ):
        # The owner cookie also satisfies `PanelDep` and authenticates first.
        self.client.cookies.clear()
        headers = dict(who or self.panel)
        if ctype:
            headers["Content-Type"] = ctype
        return self.client.request(method, path, headers=headers, **kw)

    def pickup(self, who: dict[str, str] | None = None, board: str = C6):  # noqa: ANN201
        return self.as_panel("GET", f"/api/endpoint/debug/next?board={board}", who)

    def as_owner(self, method: str, path: str, **kw):  # noqa: ANN201
        self.client.cookies.clear()
        self.client.cookies.update(self.owner_cookies)
        try:
            return self.client.request(method, path, **kw)
        finally:
            self.client.cookies.clear()

    def look(self, what: str, device: str | None = None):  # noqa: ANN201
        return self.as_owner(
            "POST", f"/api/panel/look/{device or self.device}", json={"what": what}
        )

    def command(self, device: str | None = None, **body):  # noqa: ANN201
        return self.client.post(
            f"/api/debug/panel/{device or self.device}/command", headers=self.dbg, json=body
        )

    def result(self, rid: str, wait: float = 0):  # noqa: ANN201
        return self.client.get(
            f"/api/debug/panel/{self.device}/result/{rid}?wait={wait}", headers=self.dbg
        )


@pytest.fixture
def box(database_url: str, maker: Maker, tmp_path: Path) -> Iterator[Box]:  # noqa: F811
    import asyncio

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
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
        panel = client.post(
            "/api/devices", json={"label": "panel Jeff", "device_role": "display"}
        ).json()
        sibling = client.post(
            "/api/devices", json={"label": "panel Nora", "device_role": "jpet"}
        ).json()
        owner_cookies = dict(client.cookies)
        client.cookies.clear()
        dbg = {"Authorization": f"Bearer {debug_key}"}
        listed = client.get("/api/debug/panel", headers=dbg)
        assert listed.status_code == 200, listed.text
        ids = {p["name"]: p["device_id"] for p in listed.json()}
        yield Box(
            client=client,
            dbg=dbg,
            panel={"Authorization": f"Bearer {panel['key']}"},
            sibling={"Authorization": f"Bearer {sibling['key']}"},
            device=ids["Jeff"],
            sibling_device=ids["Nora"],
            maker=maker,
            blob_dir=tmp_path,
            owner_cookies=owner_cookies,
        )


def _owner_sql(box: Box, sql: str, **params: object) -> None:
    import asyncio

    async def run() -> None:
        async with scoped_session(box.maker, OWNER) as s:
            await s.execute(text(sql), params)
            await s.commit()

    asyncio.run(run())


# ---- the round trip --------------------------------------------------------------------


def test_snapshot_round_trip(box: Box) -> None:
    assert box.pickup().status_code == 204, "nothing queued yet; this is also proof of life"

    queued = box.command(command="snapshot")
    assert queued.status_code == 202, queued.text
    rid = queued.json()["request_id"]
    assert queued.json() == {"request_id": rid, "state": "queued", "expires_in_s": 60}
    assert box.result(rid).json()["state"] == "queued"

    got = box.pickup()
    assert got.status_code == 200
    assert got.json() == {"id": rid, "cmd": "snapshot"}
    assert box.pickup().status_code == 204, "a request is delivered once"
    assert box.result(rid).json()["state"] == "sent"

    answer = {
        "id": rid,
        "cmd": "snapshot",
        "destructive": False,
        "ok": True,
        "data": {"v": 1, "net": {"state": "up", "rssi": -51}},
    }
    posted = box.as_panel("POST", f"/api/endpoint/debug/{rid}", json=answer)
    assert posted.status_code == 204, posted.text

    out = box.result(rid, wait=5).json()
    assert out["state"] == "done" and out["result"] == answer
    assert out["command"] == "snapshot" and out["screenshot"] is None
    assert out["picked_up_at"] and out["result_at"]

    # Answered once: a second post is a 404, not an overwrite.
    again = box.as_panel("POST", f"/api/endpoint/debug/{rid}", json={"ok": False})
    assert again.status_code == 404


def test_the_request_carries_args_and_confirm_flat(box: Box) -> None:
    box.pickup()
    rid = box.command(command="inject", args={"event": "touch", "x": 240, "y": 100}).json()[
        "request_id"
    ]
    assert box.pickup().json() == {"id": rid, "cmd": "inject", "event": "touch", "x": 240, "y": 100}
    rid = box.command(command="reboot", confirm=True).json()["request_id"]
    assert box.pickup().json() == {"id": rid, "cmd": "reboot", "confirm": True}


def test_who_asked_is_recorded(box: Box) -> None:
    box.pickup()
    rid = box.command(command="logs").json()["request_id"]
    import asyncio

    async def by() -> str:
        async with scoped_session(box.maker, OWNER) as s:
            return str(
                (
                    await s.execute(
                        text("SELECT created_by FROM app.panel_debug_request WHERE id = :id"),
                        {"id": rid},
                    )
                ).scalar_one()
            )

    assert asyncio.run(by()).startswith("claude (")


def test_long_poll_returns_the_current_state_at_its_deadline(box: Box) -> None:
    box.pickup()
    rid = box.command(command="logs").json()["request_id"]
    started = time.monotonic()
    out = box.result(rid, wait=1)
    assert out.status_code == 200 and out.json()["state"] == "queued"
    assert 0.9 <= time.monotonic() - started < 5
    assert box.result(rid, wait=26).status_code == 422, "the wait is capped under the tunnel"


# ---- refusals at the door --------------------------------------------------------------


def test_a_panel_that_has_not_picked_up_is_offline(box: Box) -> None:
    never = box.command(command="snapshot")
    assert never.status_code == 503
    assert never.json()["detail"] == {"reason": "offline", "last_seen_s": None}

    relay.mark_seen(box.device, C6, now=time.monotonic() - 45)
    stale = box.command(command="snapshot")
    assert stale.status_code == 503
    assert stale.json()["detail"]["reason"] == "offline"
    assert stale.json()["detail"]["last_seen_s"] >= 45

    box.pickup()
    assert box.command(command="snapshot").status_code == 202


def test_reboot_without_confirm_is_refused_and_never_queued(box: Box) -> None:
    box.pickup()
    refused = box.command(command="reboot")
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"reason": "needs_confirm", "command": "reboot"}
    assert box.pickup().status_code == 204
    assert box.command(command="reboot", confirm=True).status_code == 202


def test_unknown_commands_and_bad_args_are_refused(box: Box) -> None:
    box.pickup()
    unknown = box.command(command="format_flash")
    assert unknown.status_code == 422 and unknown.json()["detail"]["reason"] == "unknown_command"
    bad = box.command(command="inject", args={"event": "touch", "x": "middle"})
    assert bad.status_code == 422 and bad.json()["detail"]["arg"] == "x"
    assert box.command(command="open", args={"bogus": 1}).status_code == 422
    assert box.command(command="x" * 16).status_code == 422  # the model's own ceiling
    assert box.pickup().status_code == 204


def test_a_panel_that_is_not_a_c6_has_no_debug_surface(box: Box) -> None:
    box.pickup(board="s3-amoled-18")
    out = box.command(command="snapshot")
    assert out.status_code == 409
    assert out.json()["detail"] == {"reason": "no_debug_surface", "board": "s3-amoled-18"}


def test_a_flooded_panel_is_busy(box: Box) -> None:
    box.pickup()
    for _ in range(relay.MAX_QUEUED):
        assert box.command(command="snapshot").status_code == 202
    busy = box.command(command="snapshot")
    assert busy.status_code == 429 and busy.json()["detail"]["reason"] == "busy"


def test_help_publishes_what_the_firmware_binds(box: Box) -> None:
    box.pickup()
    rid = box.command(command="help").json()["request_id"]
    box.pickup()
    rows = [
        {
            "name": n,
            "destructive": c.destructive,
            "idempotent": c.idempotent,
            "available": n != "screenshot",
        }
        for n, c in relay.COMMANDS.items()
    ]
    answer = {"id": rid, "cmd": "help", "ok": True, "data": {"commands": rows}}
    assert box.as_panel("POST", f"/api/endpoint/debug/{rid}", json=answer).status_code == 204
    refused = box.command(command="screenshot")
    assert refused.status_code == 409 and refused.json()["detail"]["reason"] == "unavailable"
    listed = {p["name"]: p for p in box.client.get("/api/debug/panel", headers=box.dbg).json()}
    jeff = listed["Jeff"]
    assert jeff["online"] and jeff["board"] == C6 and jeff["last_seen_s"] == 0
    assert {c["name"] for c in jeff["commands"]} == set(relay.COMMANDS)
    assert listed["Nora"]["board"] is None and listed["Nora"]["commands"] is None


def test_unknown_panels_and_ids_are_404(box: Box) -> None:
    box.pickup()
    assert box.command(device="not-a-uuid", command="snapshot").status_code == 404
    gone = "00000000-0000-0000-0000-000000000000"
    assert box.command(device=gone, command="snapshot").status_code == 404
    assert box.result("not-a-uuid").status_code == 404
    assert box.result(gone).status_code == 404
    shot = box.client.get(f"/api/debug/panel/{box.device}/screenshot/{gone}.png", headers=box.dbg)
    assert shot.status_code == 404


def test_the_debug_routes_need_a_debug_token(box: Box) -> None:
    assert box.client.get("/api/debug/panel", headers=box.panel).status_code == 401
    out = box.client.post(
        f"/api/debug/panel/{box.device}/command", headers=box.panel, json={"command": "help"}
    )
    assert out.status_code == 401


# ---- the panel side: who may collect and answer ----------------------------------------


def test_a_sibling_cannot_collect_or_answer_through_the_routes(box: Box) -> None:
    box.pickup()
    rid = box.command(command="snapshot").json()["request_id"]
    assert box.pickup(who=box.sibling).status_code == 204, "the sibling's queue is empty"
    box.pickup()
    forged = box.as_panel(
        "POST", f"/api/endpoint/debug/{rid}", who=box.sibling, json={"id": rid, "ok": True}
    )
    assert forged.status_code == 404
    assert box.result(rid).json()["state"] == "sent"


def test_the_owner_cookie_is_not_a_panel(box: Box) -> None:
    """`PanelDep` admits the owner's cookie; these two routes do not."""
    assert box.client.get("/api/endpoint/debug/next").status_code == 401
    # With the cookie: log in again, then try.
    import asyncio

    key = asyncio.run(service.rotate_owner_key(SqlAuthRepo(box.maker)))
    box.client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
    assert box.client.get("/api/endpoint/debug/next").status_code == 403
    rid = "00000000-0000-0000-0000-000000000000"
    assert box.client.post(f"/api/endpoint/debug/{rid}", json={}).status_code == 403
    box.client.cookies.clear()


def test_answers_are_size_limited_and_typed(box: Box) -> None:
    box.pickup()
    rid = box.command(command="logs").json()["request_id"]
    box.pickup()
    path = f"/api/endpoint/debug/{rid}"
    big = {"id": rid, "ok": True, "data": {"lines": ["x" * 1000] * 70}}
    too_big = box.as_panel("POST", path, json=big)
    assert too_big.status_code == 413
    assert too_big.json()["detail"] == {"reason": "too_large", "max": relay.MAX_RESULT_JSON}
    # Chunked, no Content-Length: counted as it arrives.
    chunks = (b"x" * 8192 for _ in range(10))
    chunked = box.as_panel("POST", path, content=chunks, ctype="application/json")
    assert chunked.status_code == 413
    assert box.as_panel("POST", path, content=b"{nope", ctype="application/json").status_code == 422
    assert box.as_panel("POST", path, json=[1, 2]).status_code == 422
    assert box.as_panel("POST", path, json={"id": "someone-else"}).status_code == 422
    # A stream is only for a screenshot.
    octet = box.as_panel("POST", path, content=b"JPS1", ctype="application/octet-stream")
    assert octet.status_code == 415
    assert box.as_panel("POST", "/api/endpoint/debug/not-a-uuid", json={}).status_code == 404
    # Still answerable after all of that.
    assert box.as_panel("POST", path, json={"id": rid, "ok": True}).status_code == 204


# ---- screenshots -----------------------------------------------------------------------


def _frame(w: int, h: int) -> list[int]:
    return [0xF800 if y < h // 2 else 0x001F for y in range(h) for _ in range(w)]


def test_a_screenshot_stream_becomes_a_png(box: Box) -> None:
    box.pickup()
    rid = box.command(command="screenshot").json()["request_id"]
    assert box.pickup().json() == {"id": rid, "cmd": "screenshot"}
    stream = jps1(480, 480, _frame(480, 480))

    def strips() -> Iterator[bytes]:
        # The panel's 4 KB handoff, chunked onto the wire.
        for i in range(0, len(stream), 4096):
            yield stream[i : i + 4096]

    up = box.as_panel(
        "POST",
        f"/api/endpoint/debug/{rid}",
        content=strips(),
        ctype="application/octet-stream",
    )
    assert up.status_code == 204, up.text

    out = box.result(rid, wait=5).json()
    assert out["state"] == "done"
    assert out["result"]["ok"] is True
    assert out["result"]["data"]["w"] == 480 and out["result"]["data"]["stripes"] == 12
    assert out["result"]["data"]["bytes"] == len(stream)
    url = out["screenshot"]
    assert url == f"/api/debug/panel/{box.device}/screenshot/{rid}.png"

    png = box.client.get(url, headers=box.dbg)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(png.content))
    assert img.size == (480, 480)
    assert img.getpixel((10, 10)) == (255, 0, 0) and img.getpixel((10, 400)) == (0, 0, 255)
    assert list(box.blob_dir.rglob("*")), "stored through the blob store"


def test_a_torn_stream_is_a_failed_result_now(box: Box) -> None:
    box.pickup()
    rid = box.command(command="screenshot").json()["request_id"]
    box.pickup()
    stream = jps1(480, 480, _frame(480, 480))
    torn = stream[: len(stream) // 2]
    up = box.as_panel(
        "POST",
        f"/api/endpoint/debug/{rid}",
        content=torn,
        ctype="application/octet-stream",
    )
    assert up.status_code == 422 and up.json()["detail"]["reason"] == "bad_stream"
    out = box.result(rid).json()
    assert out["state"] == "done" and out["result"]["ok"] is False
    assert out["result"]["err"] == "bad_stream" and out["screenshot"] is None


def test_a_screenshot_stream_is_size_limited(box: Box) -> None:
    box.pickup()
    rid = box.command(command="screenshot").json()["request_id"]
    box.pickup()
    up = box.as_panel(
        "POST",
        f"/api/endpoint/debug/{rid}",
        content=b"JPS1" + b"\0" * relay.MAX_SHOT_STREAM,
        ctype="application/octet-stream",
    )
    assert up.status_code == 413
    assert box.result(rid).json()["state"] == "sent", "a refused upload can be retried"


def test_a_failed_capture_answers_in_json(box: Box) -> None:
    """`net slow` on the panel: no stream, a JSON failure instead."""
    box.pickup()
    rid = box.command(command="screenshot").json()["request_id"]
    box.pickup()
    fail = {"id": rid, "cmd": "screenshot", "ok": False, "err": "net slow"}
    assert box.as_panel("POST", f"/api/endpoint/debug/{rid}", json=fail).status_code == 204
    assert box.result(rid).json()["result"] == fail


# ---- expiry and the sweep --------------------------------------------------------------


def test_an_uncollected_request_expires_and_is_never_delivered(box: Box) -> None:
    box.pickup()
    rid = box.command(command="reboot", confirm=True).json()["request_id"]
    _owner_sql(
        box,
        "UPDATE app.panel_debug_request SET expires_at = now() - interval '1 second'"
        " WHERE id = CAST(:id AS uuid)",
        id=rid,
    )
    assert box.pickup().status_code == 204, "a late reboot is a different command"
    assert box.result(rid).json()["state"] == "expired"


def test_an_unanswered_request_expires(box: Box) -> None:
    box.pickup()
    rid = box.command(command="logs").json()["request_id"]
    box.pickup()
    _owner_sql(
        box,
        "UPDATE app.panel_debug_request SET expires_at = now() - interval '1 second'"
        " WHERE id = CAST(:id AS uuid)",
        id=rid,
    )
    late = box.as_panel("POST", f"/api/endpoint/debug/{rid}", json={"id": rid, "ok": True})
    assert late.status_code == 404
    assert box.result(rid, wait=3).json()["state"] == "expired"


def test_finished_requests_and_their_screenshots_are_swept(box: Box) -> None:
    box.pickup()
    shot = box.command(command="screenshot").json()["request_id"]
    box.pickup()
    box.as_panel(
        "POST",
        f"/api/endpoint/debug/{shot}",
        content=jps1(8, 8, [0x07E0] * 64, stripe_rows=4),
        ctype="application/octet-stream",
    )
    stale = box.command(command="logs").json()["request_id"]
    assert box.result(shot).json()["screenshot"]
    files = [p for p in box.blob_dir.rglob("*") if p.is_file()]
    assert len(files) == 1

    # Fifteen minutes on: both finished (one done, one expired) and past the TTL.
    _owner_sql(
        box,
        "UPDATE app.panel_debug_request SET expires_at = now() - interval '20 minutes',"
        " result_at = CASE WHEN state = 'done' THEN now() - interval '16 minutes' END",
    )
    assert box.result(shot).status_code == 404
    assert box.result(stale).status_code == 404
    assert not [p for p in box.blob_dir.rglob("*") if p.is_file()], "the PNG went with its row"


def test_the_sweep_keeps_a_screenshot_another_table_holds(box: Box) -> None:
    """Content-addressed: identical bytes are one file. Two requests that drew the same
    screen share it, so the first one's sweep must leave it for the second."""
    box.pickup()
    stream = jps1(8, 8, [0x07E0] * 64, stripe_rows=4)
    rids = []
    for _ in range(2):
        rid = box.command(command="screenshot").json()["request_id"]
        box.pickup()
        box.as_panel(
            "POST",
            f"/api/endpoint/debug/{rid}",
            content=stream,
            ctype="application/octet-stream",
        )
        rids.append(rid)
    _owner_sql(
        box,
        "UPDATE app.panel_debug_request SET result_at = now() - interval '16 minutes'"
        " WHERE id = CAST(:id AS uuid)",
        id=rids[0],
    )
    assert box.result(rids[0]).status_code == 404
    png = box.client.get(box.result(rids[1]).json()["screenshot"], headers=box.dbg)
    assert png.status_code == 200


def test_a_screenshot_whose_blob_is_gone_is_404(box: Box) -> None:
    box.pickup()
    rid = box.command(command="screenshot").json()["request_id"]
    box.pickup()
    box.as_panel(
        "POST",
        f"/api/endpoint/debug/{rid}",
        content=jps1(4, 4, [1] * 16, stripe_rows=4),
        ctype="application/octet-stream",
    )
    for p in box.blob_dir.rglob("*"):
        if p.is_file():
            p.unlink()
    out = box.client.get(box.result(rid).json()["screenshot"], headers=box.dbg)
    assert out.status_code == 404


def test_jps1_helper_matches_the_header_layout() -> None:
    """Guard on the shared test encoder itself: header, one stripe, terminator."""
    s = jps1(2, 1, [5, 5])
    assert s[:4] == b"JPS1" and struct.unpack_from("<HH", s, 4) == (2, 1)
    assert struct.unpack_from("<HHI", s, 8) == (0, 1, 3)
    assert s[-2:] == b"\xff\xff"


# ---- the owner's looks, from Ops → Panels ------------------------------------------------


def test_the_owner_takes_a_screenshot_from_the_pwa(box: Box) -> None:
    box.pickup()
    queued = box.look("screenshot")
    assert queued.status_code == 202, queued.text
    rid = queued.json()["request_id"]
    assert box.pickup().json() == {"id": rid, "cmd": "screenshot"}
    up = box.as_panel(
        "POST",
        f"/api/endpoint/debug/{rid}",
        content=jps1(480, 480, _frame(480, 480)),
        ctype="application/octet-stream",
    )
    assert up.status_code == 204, up.text

    out = box.as_owner("GET", f"/api/panel/look/{box.device}/{rid}?wait=5").json()
    assert out["state"] == "done" and out["command"] == "screenshot"
    url = out["screenshot"]
    assert url == f"/api/panel/look/{box.device}/{rid}/screenshot.png"
    png = box.as_owner("GET", url)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(png.content)).size == (480, 480)


def test_the_owner_reads_a_snapshot_and_the_ask_is_on_the_record(box: Box) -> None:
    box.pickup()
    rid = box.look("snapshot").json()["request_id"]
    box.pickup()
    answer = {"id": rid, "cmd": "snapshot", "ok": True, "data": {"v": 1}}
    assert box.as_panel("POST", f"/api/endpoint/debug/{rid}", json=answer).status_code == 204
    out = box.as_owner("GET", f"/api/panel/look/{box.device}/{rid}").json()
    assert out["result"] == answer and out["screenshot"] is None
    import asyncio

    async def by() -> str:
        async with scoped_session(box.maker, OWNER) as s:
            return str(
                (
                    await s.execute(
                        text("SELECT created_by FROM app.panel_debug_request WHERE id = :id"),
                        {"id": rid},
                    )
                ).scalar_one()
            )

    assert asyncio.run(by()).startswith("owner PWA (")


def test_the_owner_may_only_look(box: Box) -> None:
    box.pickup()
    for what in ("reboot", "inject", "logs", "set"):
        assert box.look(what).status_code == 422, what
    assert box.pickup().status_code == 204, "nothing was queued"


def test_a_look_is_refused_as_the_debug_route_refuses(box: Box) -> None:
    offline = box.look("snapshot")
    assert offline.status_code == 503 and offline.json()["detail"]["reason"] == "offline"
    box.pickup(board="s3-amoled-18")
    s3 = box.look("snapshot")
    assert s3.status_code == 409 and s3.json()["detail"]["reason"] == "no_debug_surface"
    assert box.look("snapshot", device="00000000-0000-0000-0000-000000000000").status_code == 404


def test_the_look_routes_need_the_owner(box: Box) -> None:
    box.pickup()
    path = f"/api/panel/look/{box.device}"
    assert box.client.post(path, json={"what": "snapshot"}).status_code == 401
    assert box.client.post(path, headers=box.dbg, json={"what": "snapshot"}).status_code == 401
    by_panel = box.as_panel("POST", path, json={"what": "snapshot"})
    assert by_panel.status_code in (401, 403)
    gone = "00000000-0000-0000-0000-000000000000"
    assert box.client.get(f"{path}/{gone}").status_code == 401
    assert box.client.get(f"{path}/{gone}/screenshot.png").status_code == 401


def test_ops_status_says_which_panels_can_be_looked_at(box: Box) -> None:
    def look_states() -> dict[str, str | None]:
        panels = box.as_owner("GET", "/api/endpoint/status").json()["panels"]
        return {p["name"]: p["look"] for p in panels}

    assert look_states() == {"Jeff": None, "Nora": None}
    box.pickup()
    assert look_states() == {"Jeff": "online", "Nora": None}
    relay.mark_seen(box.device, C6, now=time.monotonic() - 45)
    assert look_states()["Jeff"] == "offline"
    box.pickup(who=box.sibling, board="s3-amoled-18")
    assert look_states()["Nora"] is None, "an S3 has no debug surface"


def test_the_looks_are_mounted_with_debug_access_off(
    database_url: str,  # noqa: F811
    maker: Maker,
    tmp_path: Path,
) -> None:
    import asyncio

    owner_key = asyncio.run(service.rotate_owner_key(SqlAuthRepo(maker)))
    app = create_app(
        Settings(
            secure_cookies=False,
            database_url=database_url,
            debug_access_enabled=False,
            blob_dir=str(tmp_path),
        )
    )
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
        panel = client.post(
            "/api/devices", json={"label": "panel Jeff", "device_role": "display"}
        ).json()
        device = client.get("/api/endpoint/status").json()["panels"][0]["device_id"]
        assert client.get("/api/debug/panel").status_code == 404, "the token surface is off"
        cookies = dict(client.cookies)
        client.cookies.clear()
        key = {"Authorization": f"Bearer {panel['key']}"}
        assert client.get(f"/api/endpoint/debug/next?board={C6}", headers=key).status_code == 204
        client.cookies.update(cookies)
        queued = client.post(f"/api/panel/look/{device}", json={"what": "snapshot"})
        assert queued.status_code == 202, queued.text
