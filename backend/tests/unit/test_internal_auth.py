"""`/internal/*` is gated per caller (docs/archive/SECURITY_REVIEW_FIXES_PLAN.md S2.2, F7).

The api joins every docker network a sandbox or headless browser sits on, so "internal
network only" let any of them drive the pet as the owner or use `/internal/mqtt-auth` as a
device-key oracle. Each caller now holds its own token, scoped to its own routes: the
LAN-facing wall's opens only the pet routes, mosquitto's only the MQTT ones.
"""

import logging
import os
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from jbrain.config import Settings
from jbrain.growbot.personas import Pulled
from jbrain.growbot.repo import Redeemed
from jbrain.main import create_app
from jbrain.storage import FsInternalSecrets
from tests.internal_callers import (
    enroll_headers,
    growbot_headers,
    mqtt_headers,
    token,
    wall_headers,
)
from tests.unit.fakes import FakeAuthRepo

_DB = "postgresql+asyncpg://nobody@localhost:1/none"

# Each gated route with a request that, once past the gate, answers without a database:
# a blank say/statue is a 400, a bad MQTT credential a go-auth deny (403).
WALL_ROUTES: list[tuple[str, str, dict[str, object] | None, int]] = [
    ("POST", "/internal/pet/effects/clear", None, 200),
    ("POST", "/internal/pet/say", {"text": "  "}, 400),
    ("POST", "/internal/pet/statue", {"subject": "  "}, 400),
]
MQTT_ROUTES: list[tuple[str, str, dict[str, object] | None, int]] = [
    ("POST", "/internal/mqtt-auth", {"username": "x", "password": "nope"}, 403),
    ("POST", "/internal/mqtt-acl", {"username": "x", "topic": "owntracks/x/p", "acc": 2}, 403),
]
# The box's enrollment screen (OWNER_TUNNEL_PLAN.md §3.5): its own token, in its own volume.
ENROLL_ROUTES: list[tuple[str, str, dict[str, object] | None, int]] = [
    ("GET", "/internal/owner-enroll", None, 200),
    ("POST", "/internal/owner-enroll/close", None, 204),
    ("POST", "/internal/owner-enroll/reject", None, 404),
]
# The GrowBot sidecar (GROWBOT_PLAN.md §2): its own token, in its own volume. Past the gate
# the repo is a stand-in that says yes, and with hosting off every model name is unknown.
_PID = "00000000-0000-4000-8000-000000000001"
_PHONE = "phone-0123456789abcdef"


class _YesPersonaRepo:
    async def pull(self, ctx: object, pid: str, phone: str, with_soul: bool = True) -> Pulled:
        return Pulled(1, {"soul_format": 3}, None)

    async def push(self, ctx: object, pid: str, phone: str, base: int, soul: object) -> int:
        return base + 1

    async def riders(self, ctx: object) -> list[object]:
        return []

    async def phone_memories(self, ctx: object, pid: str, phone: str) -> tuple[int, list]:
        return 0, []

    async def phone_view(self, ctx: object, pid: str, phone: str, mid: str, i: int) -> bytes:
        return b"\xff\xd8\xff"

    async def phone_keep(self, ctx: object, pid: str, phone: str, saved: object) -> int:
        return 1

    async def phone_forget(self, ctx: object, pid: str, phone: str, ids: list[str]) -> int:
        return 1


GROWBOT_ROUTES: list[tuple[str, str, dict[str, object] | None, int]] = [
    ("POST", "/internal/growbot/ticket/redeem", {"ticket": "t"}, 200),
    ("POST", "/internal/growbot/robot/verify", {"code": "gb-x", "key": "k"}, 200),
    ("GET", "/internal/growbot/lan-root", None, 404),
    ("GET", "/internal/growbot/llm/v1/models", None, 200),
    ("POST", "/internal/growbot/llm/v1/chat/completions", {"model": "x", "messages": []}, 400),
    ("POST", "/internal/growbot/soul/pull", {"persona": _PID, "phone": _PHONE}, 200),
    ("POST", "/internal/growbot/soul/state", {"persona": _PID, "phone": _PHONE}, 200),
    (
        "POST",
        "/internal/growbot/soul/push",
        {"persona": _PID, "phone": _PHONE, "base": 1, "soul": {"soul_format": 3}},
        200,
    ),
    ("GET", "/internal/growbot/body/gb-x", None, 200),
    ("POST", "/internal/growbot/calls", {"kind": "tts", "ok": True, "total_ms": 1}, 204),
    ("POST", "/internal/growbot/memories/list", {"persona": _PID, "phone": _PHONE}, 200),
    (
        "POST",
        "/internal/growbot/memories/view",
        {"persona": _PID, "phone": _PHONE, "id": "r1", "i": 0},
        200,
    ),
    ("POST", "/internal/growbot/memories/keep", {"persona": _PID, "phone": _PHONE}, 400),
    (
        "POST",
        "/internal/growbot/memories/forget",
        {"persona": _PID, "phone": _PHONE, "ids": ["r1"]},
        200,
    ),
]
# Routes with a path parameter, as app.routes names them.
_TEMPLATES = {"/internal/growbot/body/gb-x": "/internal/growbot/body/{code}"}
# Keeps its own HMAC bearer (api/llm_internal.py, tested in test_llm_internal.py).
_OWN_BEARER = {"/internal/llm/regen-gateway-config"}


class _YesGrowbotRepo:
    async def redeem_ticket(self, ctx: object, ticket: str) -> Redeemed | None:
        return Redeemed("")

    async def verify_robot(self, ctx: object, code: str, key: str) -> bool:
        return True


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[tuple[FastAPI, TestClient]]:
    app = create_app(
        Settings(secure_cookies=False, database_url=_DB, internal_secrets_dir=str(tmp_path))
    )
    with TestClient(app) as c:
        app.state.auth_repo = FakeAuthRepo()
        app.state.growbot_repo = _YesGrowbotRepo()
        app.state.growbot_personas = _YesPersonaRepo()
        yield app, c


def _call(c: TestClient, method: str, path: str, body: object, headers: dict[str, str]) -> int:
    return c.request(method, path, json=body, headers=headers).status_code


def test_every_internal_route_refuses_a_request_without_a_token(
    app_client: tuple[FastAPI, TestClient],
) -> None:
    app, c = app_client
    internal = [
        (m, r.path)
        for r in app.routes
        if isinstance(r, APIRoute) and r.path.startswith("/internal/")
        for m in r.methods
    ]
    routes = WALL_ROUTES + MQTT_ROUTES + ENROLL_ROUTES + GROWBOT_ROUTES
    gated = {_TEMPLATES.get(p, p) for _, p, _, _ in routes} | {"/internal/pet"} | _OWN_BEARER
    # A new /internal route fails here until it is given a caller.
    assert {p for _, p in internal} == gated
    for method, path in internal:
        assert c.request(method, path).status_code in (401, 403), (method, path)


@pytest.mark.parametrize(("method", "path", "body", "ok"), WALL_ROUTES)
def test_pet_routes_open_to_the_wall_token_only(
    app_client: tuple[FastAPI, TestClient],
    method: str,
    path: str,
    body: object,
    ok: int,
) -> None:
    app, c = app_client
    assert _call(c, method, path, body, {}) == 401
    # The other caller's token, presented either way, opens nothing here.
    mqtt = token(app, "mqtt")
    assert _call(c, method, path, body, {"Authorization": f"Bearer {mqtt}"}) == 401
    assert _call(c, method, path, body, mqtt_headers(app)) == 401
    # Not a bearer, or a near miss.
    assert _call(c, method, path, body, {"Authorization": f"Basic {token(app, 'wall')}"}) == 401
    assert _call(c, method, path, body, {"Authorization": f"Bearer {token(app, 'wall')}x"}) == 401
    assert _call(c, method, path, body, wall_headers(app)) == ok


def test_the_pet_snapshot_read_needs_the_wall_token(
    app_client: tuple[FastAPI, TestClient],
) -> None:
    app, c = app_client
    assert c.get("/internal/pet").status_code == 401
    assert c.get("/internal/pet", headers=mqtt_headers(app)).status_code == 401


@pytest.mark.parametrize(("method", "path", "body", "ok"), MQTT_ROUTES)
def test_mqtt_routes_open_to_the_mqtt_token_only(
    app_client: tuple[FastAPI, TestClient],
    method: str,
    path: str,
    body: object,
    ok: int,
) -> None:
    app, c = app_client
    assert _call(c, method, path, body, {}) == 401
    wall = token(app, "wall")
    assert _call(c, method, path, body, {"User-Agent": wall}) == 401
    assert _call(c, method, path, body, wall_headers(app)) == 401
    # The whole User-Agent is the token: a prefix or suffix around it is not.
    assert _call(c, method, path, body, {"User-Agent": f"go-auth {token(app, 'mqtt')}"}) == 401
    assert _call(c, method, path, body, mqtt_headers(app)) == ok


@pytest.mark.parametrize(("method", "path", "body", "ok"), ENROLL_ROUTES)
def test_enrollment_screen_routes_open_to_the_enroll_token_only(
    app_client: tuple[FastAPI, TestClient],
    method: str,
    path: str,
    body: object,
    ok: int,
) -> None:
    app, c = app_client
    assert _call(c, method, path, body, {}) == 401
    # The pet token sits where the cast sidecar and mosquitto can read it: it opens nothing.
    assert _call(c, method, path, body, wall_headers(app)) == 401
    assert _call(c, method, path, body, mqtt_headers(app)) == 401
    assert _call(c, method, path, body, enroll_headers(app)) == ok


@pytest.mark.parametrize(("method", "path", "body", "ok"), GROWBOT_ROUTES)
def test_growbot_routes_open_to_the_growbot_token_only(
    app_client: tuple[FastAPI, TestClient],
    method: str,
    path: str,
    body: object,
    ok: int,
) -> None:
    app, c = app_client
    assert _call(c, method, path, body, {}) == 401
    # No other caller's token opens the robot's routes, nor does its own as a non-bearer.
    for other in (wall_headers(app), mqtt_headers(app), enroll_headers(app)):
        assert _call(c, method, path, body, other) == 401
    own = growbot_headers(app)["Authorization"].removeprefix("Bearer ")
    assert _call(c, method, path, body, {"Authorization": f"Basic {own}"}) == 401
    assert _call(c, method, path, body, {"Authorization": f"Bearer {own}x"}) == 401
    assert _call(c, method, path, body, growbot_headers(app)) == ok


def test_the_growbot_token_opens_no_other_callers_routes(
    app_client: tuple[FastAPI, TestClient],
) -> None:
    app, c = app_client
    for method, path, body, _ok in WALL_ROUTES + MQTT_ROUTES + ENROLL_ROUTES:
        assert _call(c, method, path, body, growbot_headers(app)) == 401, path


def test_a_non_ascii_header_is_refused_not_a_crash(
    app_client: tuple[FastAPI, TestClient],
) -> None:
    _, c = app_client
    headers = {"User-Agent": "caf\xe9".encode("latin-1")}
    r = c.post("/internal/mqtt-auth", json={}, headers=headers)  # type: ignore[arg-type]
    assert r.status_code == 401


def test_no_token_on_disk_fails_closed_then_opens_once_minted(tmp_path: Path) -> None:
    """A token minted after start-up is honoured without a restart, and none at all opens
    nothing (an empty presented value never matches a missing one)."""
    missing = tmp_path / "not-yet"
    app = create_app(
        Settings(secure_cookies=False, database_url=_DB, internal_secrets_dir=str(missing))
    )
    with TestClient(app) as c:
        assert c.post("/internal/pet/effects/clear").status_code == 401
        empty = {"Authorization": "Bearer "}
        assert c.post("/internal/pet/effects/clear", headers=empty).status_code == 401
        assert c.post("/internal/mqtt-auth", json={}, headers={"User-Agent": ""}).status_code == 401

        missing.mkdir()
        FsInternalSecrets(missing).ensure("wall")
        minted = (missing / "wall.token").read_text()
        ok = c.post("/internal/pet/effects/clear", headers={"Authorization": f"Bearer {minted}"})
        assert ok.status_code == 200


def test_mqtt_auth_is_not_rate_limited(app_client: tuple[FastAPI, TestClient]) -> None:
    """Every phone's connect arrives from the one broker: a bucket would lock them all out."""
    app, c = app_client
    statuses = {
        c.post(
            "/internal/mqtt-auth",
            json={"username": "x", "password": "y"},
            headers=mqtt_headers(app),
        ).status_code
        for _ in range(60)
    }
    assert statuses == {403}


def test_no_logger_records_an_internal_callers_credential(
    app_client: tuple[FastAPI, TestClient],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """mosquitto's token rides in User-Agent, a header request loggers habitually record."""
    app, c = app_client
    secrets = [token(app, "wall"), token(app, "mqtt")]
    wrong = "presented-but-wrong-" + "f" * 40
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as events:
        for method, path, body, _ in WALL_ROUTES:
            _call(c, method, path, body, wall_headers(app))
            _call(c, method, path, body, {"Authorization": f"Bearer {wrong}"})
        for method, path, body, _ in MQTT_ROUTES:
            _call(c, method, path, body, mqtt_headers(app))
            _call(c, method, path, body, {"User-Agent": wrong})
    out = capsys.readouterr()
    logged = "\n".join([caplog.text, out.out, out.err, *(repr(e) for e in events)])
    for secret in [*secrets, wrong]:
        assert secret not in logged


# --- the token store ---------------------------------------------------------


def test_ensure_mints_a_private_256_bit_token_once(tmp_path: Path) -> None:
    store = FsInternalSecrets(tmp_path)
    store.ensure("wall")
    path = tmp_path / "wall.token"
    first = path.read_bytes()
    assert len(first) == 64 and int(first, 16) >= 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A restart (a second ensure, a second store) keeps the token callers already hold.
    store.ensure("wall")
    FsInternalSecrets(tmp_path).ensure("wall")
    assert path.read_bytes() == first
    assert store.read("wall") == first
    assert [p.name for p in tmp_path.iterdir()] == ["wall.token"]


def test_concurrent_mints_agree_on_one_token(tmp_path: Path) -> None:
    """Two api processes on a fresh volume: both finish, one token, no temp debris."""
    barrier = threading.Barrier(16)
    errors: list[BaseException] = []

    def mint() -> None:
        try:
            barrier.wait()
            FsInternalSecrets(tmp_path).ensure("mqtt")
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=mint) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert [p.name for p in tmp_path.iterdir()] == ["mqtt.token"]
    assert len((tmp_path / "mqtt.token").read_bytes()) == 64


def test_losing_the_race_keeps_the_winners_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window between the exists() check and the link: another process lands its
    token first, and ours must neither replace it nor raise."""
    real_link = os.link

    def winner_first(src: str, dst: str) -> None:
        Path(dst).write_bytes(b"w" * 64)
        real_link(src, dst)

    monkeypatch.setattr("jbrain.storage.os.link", winner_first)
    FsInternalSecrets(tmp_path).ensure("wall")
    assert (tmp_path / "wall.token").read_bytes() == b"w" * 64
    assert [p.name for p in tmp_path.iterdir()] == ["wall.token"]


def test_read_is_none_until_a_token_exists_then_cached(tmp_path: Path) -> None:
    store = FsInternalSecrets(tmp_path)
    assert store.read("wall") is None
    (tmp_path / "wall.token").write_bytes(b"")
    assert store.read("wall") is None
    (tmp_path / "wall.token").write_bytes(b"abc\n")
    assert store.read("wall") == b"abc"
    (tmp_path / "wall.token").unlink()
    assert store.read("wall") == b"abc"


def test_a_caller_name_cannot_leave_the_directory(tmp_path: Path) -> None:
    store = FsInternalSecrets(tmp_path)
    for name in ("../x", "a/b", "", "Wall", ".hidden"):
        with pytest.raises(ValueError, match="not an internal caller name"):
            store.ensure(name)
        with pytest.raises(ValueError, match="not an internal caller name"):
            store.read(name)


def test_an_unwritable_volume_does_not_stop_the_api(tmp_path: Path) -> None:
    """The owner's app still serves; only the /internal routes stay shut."""
    blocker = tmp_path / "file"
    blocker.write_text("")
    app = create_app(
        Settings(secure_cookies=False, database_url=_DB, internal_secrets_dir=str(blocker / "sub"))
    )
    with TestClient(app) as c:
        assert c.get("/api/healthz").status_code == 200
        assert c.post("/internal/pet/effects/clear").status_code == 401


def test_rotate_replaces_a_token_whole_and_refreshes_the_cache(tmp_path: Path) -> None:
    secrets = FsInternalSecrets(tmp_path)
    secrets.rotate("growbot-session")
    first = secrets.read("growbot-session")
    assert first is not None and len(first) == 64
    mode = stat.S_IMODE(os.stat(tmp_path / "growbot-session.token").st_mode)
    assert mode == 0o600
    secrets.rotate("growbot-session")
    second = secrets.read("growbot-session")
    assert second != first
    assert (tmp_path / "growbot-session.token").read_bytes() == second
    assert sorted(p.name for p in tmp_path.iterdir()) == ["growbot-session.token"]
    with pytest.raises(ValueError):
        secrets.rotate("../escape")
