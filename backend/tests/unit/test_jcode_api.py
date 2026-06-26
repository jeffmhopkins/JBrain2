"""The jcode api surface: the in-flight turn buffer + reconnect, and owner-gating
of the routes (no DB — the gating decisions land before any query)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jcode
from jbrain.api.deps import current_principal
from jbrain.auth.service import PrincipalInfo
from jbrain.jcode import FakeJcodeClient

OWNER = PrincipalInfo(id="owner1", kind="owner", label="owner")
NON_OWNER = PrincipalInfo(id="cap1", kind="capability_token", label="token")


def _app(principal: PrincipalInfo, *, jcode_client: object) -> FastAPI:
    app = FastAPI()
    app.include_router(jcode.router, prefix="/api")
    app.state.jcode_client = jcode_client
    app.state.jcode_turns = {}
    app.state.session_maker = None  # not reached by the gating tests below
    app.dependency_overrides[current_principal] = lambda: principal
    return app


def test_non_owner_is_forbidden() -> None:
    client = TestClient(_app(NON_OWNER, jcode_client=FakeJcodeClient()))
    assert client.post("/api/jcode/sessions", json={}).status_code == 403
    assert client.get("/api/jcode/sessions").status_code == 403
    # The launcher management routes are owner-only too.
    assert client.patch("/api/jcode/sessions/s1", json={"title": "x"}).status_code == 403
    assert client.post("/api/jcode/sessions/s1/archive").status_code == 403
    assert client.post("/api/jcode/sessions/s1/unarchive").status_code == 403


def test_owner_but_unconfigured_is_404() -> None:
    client = TestClient(_app(OWNER, jcode_client=None))
    r = client.post("/api/jcode/sessions", json={"repo": "r"})
    assert r.status_code == 404
    assert "not enabled" in r.json()["detail"]


def test_preview_open_status_close() -> None:
    client = TestClient(_app(OWNER, jcode_client=FakeJcodeClient()))
    assert client.get("/api/jcode/sessions/sess1/preview").json() == {
        "enabled": True,
        "url": None,
    }
    opened = client.post("/api/jcode/sessions/sess1/preview", json={}).json()
    assert opened["url"].endswith(".trycloudflare.com")
    assert client.delete("/api/jcode/sessions/sess1/preview").status_code == 204


def test_preview_reports_disabled() -> None:
    client = TestClient(_app(OWNER, jcode_client=FakeJcodeClient(preview_enabled=False)))
    assert client.get("/api/jcode/sessions/sess1/preview").json()["enabled"] is False


def test_preview_is_owner_gated() -> None:
    client = TestClient(_app(NON_OWNER, jcode_client=FakeJcodeClient()))
    assert client.get("/api/jcode/sessions/sess1/preview").status_code == 403
    assert client.post("/api/jcode/sessions/sess1/preview", json={}).status_code == 403


def test_malformed_sid_is_404_before_any_db_or_control_call() -> None:
    # A sid carrying a path char never reaches the DB (None here) or the control
    # server — _valid_sid 404s first (review S2). session_maker is None, so reaching
    # it would error; a clean 404 proves the guard runs before the body.
    client = TestClient(_app(OWNER, jcode_client=FakeJcodeClient()))
    assert client.get("/api/jcode/sessions/bad.id").status_code == 404
    assert client.post("/api/jcode/sessions/bad.id/reset").status_code == 404
    assert client.post("/api/jcode/sessions/bad.id/turn", json={"prompt": "x"}).status_code == 404


def test_reconnect_unknown_run_is_404() -> None:
    client = TestClient(_app(OWNER, jcode_client=FakeJcodeClient()))
    assert client.get("/api/jcode/runs/nope/stream").status_code == 404
    assert client.post("/api/jcode/runs/nope/cancel").status_code == 404


async def test_turn_buffer_replays_and_finishes() -> None:
    turn = jcode._JcodeTurn("s1")
    turn.emit(b"data: a\n\n")
    turn.emit(b"data: b\n\n")
    turn.emit(b"data: c\n\n")
    turn.finish()

    # A full replay from the start.
    assert [f async for f in turn.stream()] == [b"data: a\n\n", b"data: b\n\n", b"data: c\n\n"]
    # A reconnect with an offset replays only the unseen frames.
    assert [f async for f in turn.stream(after=2)] == [b"data: c\n\n"]


async def test_turn_follows_live_frames_until_done() -> None:
    turn = jcode._JcodeTurn("s1")

    collected: list[bytes] = []

    async def consume() -> None:
        async for f in turn.stream():
            collected.append(f)

    import asyncio

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let the subscriber register
    turn.emit(b"data: live\n\n")
    turn.finish()
    await task
    assert collected == [b"data: live\n\n"]
