"""`/internal/*` is gated per caller (docs/plans/SECURITY_REVIEW_FIXES_PLAN.md S2.2, F7).

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
from jbrain.main import create_app
from jbrain.storage import FsInternalSecrets
from tests.internal_callers import mqtt_headers, token, wall_headers
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
# Keeps its own HMAC bearer (api/llm_internal.py, tested in test_llm_internal.py).
_OWN_BEARER = {"/internal/llm/regen-gateway-config"}


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[tuple[FastAPI, TestClient]]:
    app = create_app(
        Settings(secure_cookies=False, database_url=_DB, internal_secrets_dir=str(tmp_path))
    )
    with TestClient(app) as c:
        app.state.auth_repo = FakeAuthRepo()
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
    gated = {p for _, p, _, _ in WALL_ROUTES + MQTT_ROUTES} | {"/internal/pet"} | _OWN_BEARER
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
