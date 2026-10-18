"""The callers' half of the `/internal/*` handshake (SECURITY_REVIEW_FIXES_PLAN.md S2.2).

The api half is test_internal_auth.py. Here: the wall (deploy/wall/serve.py, loaded by
path) presents its token as a bearer, mosquitto's entrypoint (deploy/mosquitto/entrypoint.sh,
run under `sh`) renders the conf with its token as go-auth's User-Agent, and the compose
file gives the api the volume read-write and each caller read-only. None of this runs
Docker; the live check is MQTT phone auth and the wall's pet view on the box.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import threading
import types
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_ENTRYPOINT = _DEPLOY / "mosquitto" / "entrypoint.sh"
_CONF = _DEPLOY / "mosquitto" / "mosquitto.conf"
_TOKEN = "a1" * 32


# --- the wall ------------------------------------------------------------------------


@pytest.fixture
def wall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "wall_serve_under_test", _DEPLOY / "wall" / "serve.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "WALL_TOKEN_FILE", tmp_path / "wall.token")
    return module


class _Resp:
    status = 200
    headers: dict[str, str] = {"Content-Type": "application/json"}

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


def _capture(
    monkeypatch: pytest.MonkeyPatch, wall: types.ModuleType
) -> list[urllib.request.Request]:
    sent: list[urllib.request.Request] = []

    def urlopen(req: urllib.request.Request, timeout: float = 0) -> _Resp:
        sent.append(req)
        return _Resp()

    monkeypatch.setattr(wall.urllib.request, "urlopen", urlopen)
    return sent


def test_the_wall_sends_its_token_on_every_api_call(
    wall: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    wall.WALL_TOKEN_FILE.write_text(_TOKEN + "\n")
    sent = _capture(monkeypatch, wall)
    assert wall.fetch_pet_state() == b"{}"
    wall.api_post("/internal/pet/effects/clear")
    wall.api_post("/internal/pet/say", b"{}", "application/json")
    assert [r.full_url for r in sent] == [
        "http://api:8000/internal/pet",
        "http://api:8000/internal/pet/effects/clear",
        "http://api:8000/internal/pet/say",
    ]
    for req in sent:
        assert req.get_header("Authorization") == f"Bearer {_TOKEN}"
    assert sent[2].get_header("Content-type") == "application/json"


def test_the_wall_reads_its_token_per_request(
    wall: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The api may mint the token after the wall starts: until then it sends no header
    (the api refuses), says so once, and picks the token up on the next call."""
    sent = _capture(monkeypatch, wall)
    wall.fetch_pet_state()
    wall.fetch_pet_state()
    assert all(r.get_header("Authorization") is None for r in sent)
    assert capsys.readouterr().out.count("no api token") == 1

    wall.WALL_TOKEN_FILE.write_text(_TOKEN)
    wall.fetch_pet_state()
    assert sent[-1].get_header("Authorization") == f"Bearer {_TOKEN}"


def test_the_wall_logs_a_refusal_once_and_degrades(
    wall: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wall.WALL_TOKEN_FILE.write_text(_TOKEN)

    def refused(req: urllib.request.Request, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(wall.urllib.request, "urlopen", refused)
    assert wall.fetch_pet_state() is None
    assert wall.api_post("/internal/pet/say", b"{}", "application/json")[0] == 503
    out = capsys.readouterr().out
    assert out.count("refused the wall's token") == 1
    assert _TOKEN not in out


# --- the wall's enrollment screen (OWNER_TUNNEL_PLAN.md §3.5) ------------------------


def test_the_screen_presents_its_own_token_never_the_pet_one(
    wall: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wall.WALL_TOKEN_FILE.write_text("pet")
    monkeypatch.setattr(wall, "ENROLL_TOKEN_FILE", tmp_path / "wall-enroll.token")
    wall.ENROLL_TOKEN_FILE.write_text(_TOKEN)
    sent = _capture(monkeypatch, wall)
    wall.enroll_api("GET", "/internal/owner-enroll")
    wall.enroll_api("POST", "/internal/owner-enroll/reject")
    assert [(r.get_method(), r.full_url) for r in sent] == [
        ("GET", "http://api:8000/internal/owner-enroll"),
        ("POST", "http://api:8000/internal/owner-enroll/reject"),
    ]
    assert {r.get_header("Authorization") for r in sent} == {f"Bearer {_TOKEN}"}


def test_the_screen_reports_a_refusal_and_an_outage(
    wall: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(req: urllib.request.Request, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(wall.urllib.request, "urlopen", refused)
    assert wall.enroll_api("GET", "/internal/owner-enroll")[0] == 401

    def down(req: urllib.request.Request, timeout: float = 0) -> Any:
        raise OSError("no route")

    monkeypatch.setattr(wall.urllib.request, "urlopen", down)
    assert wall.enroll_api("GET", "/internal/owner-enroll")[0] == 503


def test_the_gateway_is_read_from_the_route_table(wall: types.ModuleType, tmp_path: Path) -> None:
    table = tmp_path / "route"
    table.write_text(
        "Iface\tDestination\tGateway \tFlags\n"
        "eth0\t0000A8C0\t00000000\t0001\n"
        "eth0\t00000000\t0100A8C0\t0003\n"
    )
    assert wall.default_gateway(str(table)) == "192.168.0.1"
    assert wall.default_gateway(str(tmp_path / "missing")) is None


@pytest.fixture
def screen(
    wall: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[types.ModuleType, list[tuple[str, str]], Any]]:
    calls: list[tuple[str, str]] = []

    def api(method: str, path: str) -> tuple[int, bytes, str]:
        calls.append((method, path))
        return 200, b'{"state":"idle"}', "application/json"

    monkeypatch.setattr(wall, "enroll_api", api)
    handler = type("H", (wall.EnrollHandler,), {"peers": frozenset({"127.0.0.1"})})
    server = wall.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield wall, calls, server
    server.shutdown()


def _hit(server: Any, method: str, path: str, headers: dict[str, str] | None = None) -> Any:
    port = server.server_address[1]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"" if method == "POST" else None,
        method=method,
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers)


def test_the_screen_serves_the_box_and_relays_its_keys(screen: Any) -> None:
    _, calls, server = screen
    status, headers = _hit(server, "GET", "/")
    assert status == 200 and headers["X-Frame-Options"] == "DENY"
    assert _hit(server, "GET", "/state")[0] == 200
    assert _hit(server, "POST", "/reject", {"X-Wall-Enroll": "1"})[0] == 200
    assert _hit(server, "POST", "/close", {"X-Wall-Enroll": "1"})[0] == 200
    assert calls == [
        ("GET", "/internal/owner-enroll"),
        ("POST", "/internal/owner-enroll/reject"),
        ("POST", "/internal/owner-enroll/close"),
    ]


def test_the_screen_never_arms(screen: Any) -> None:
    _, calls, server = screen
    assert _hit(server, "POST", "/arm", {"X-Wall-Enroll": "1"})[0] == 404
    assert calls == []


def test_a_page_in_the_box_browser_cannot_drive_the_screen(screen: Any) -> None:
    # A cross-origin page can POST without a preflight only with no custom header, and a
    # DNS-rebound name arrives with its own Host.
    _, calls, server = screen
    assert _hit(server, "POST", "/reject")[0] == 403
    assert _hit(server, "GET", "/state", {"Host": "rebound.example:8802"})[0] == 403
    assert calls == []


def test_loopback_names_with_and_without_a_port_are_the_box(screen: Any) -> None:
    _, calls, server = screen
    for host in ("localhost", "LOCALHOST:8802", "[::1]", "[::1]:8802", "127.0.0.1"):
        assert _hit(server, "GET", "/state", {"Host": host})[0] == 200, host
    for host in ("", "localhost.", "[::1", "evil:127.0.0.1"):
        assert _hit(server, "GET", "/state", {"Host": host})[0] == 403, host


def test_a_closed_window_answers_with_no_body(screen: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    wall, _, server = screen
    monkeypatch.setattr(wall, "enroll_api", lambda m, p: (204, b"", "application/json"))
    status, headers = _hit(server, "POST", "/close", {"X-Wall-Enroll": "1"})
    assert status == 204 and "Content-Length" not in headers


def test_the_wall_keeps_running_without_its_screen(
    wall: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def taken() -> Any:
        raise OSError("address already in use")

    class _Server:
        def __init__(self, *_: object) -> None:
            pass

        def serve_forever(self) -> None:
            print("serving")

    monkeypatch.setattr(wall, "serve_enroll_screen", taken)
    monkeypatch.setattr(wall, "ThreadingHTTPServer", _Server)
    wall.main()
    out = capsys.readouterr().out
    assert "enrollment screen not started" in out and "serving" in out


def test_another_container_cannot_reach_the_screen(screen: Any) -> None:
    wall, calls, server = screen
    server.RequestHandlerClass.peers = frozenset({"172.18.0.1"})
    assert _hit(server, "GET", "/state")[0] == 403
    assert calls == []


def test_the_screen_page_shows_the_qr_as_an_image_only() -> None:
    # The SVG comes from the api, but nothing in it may run on the box's kiosk page.
    page = (_DEPLOY / "wall" / "enroll.html").read_text()
    assert "data:image/svg+xml" in page
    assert "default-src 'none'" in page
    assert "/arm" not in page
    # Framed cross-origin, the wall still needs its microphone and audio.
    assert 'allow="autoplay; microphone; fullscreen"' in page
    # And a kiosk already on the wall's loopback address moves here by itself.
    page = (_DEPLOY / "wall" / "index.html").read_text()
    assert "8802" in page
    # The wall runs on when its screen fails to bind, so the kiosk moves only once it answers.
    probe, move = page.index('fetch(screen + "state"'), page.index("location.replace(screen)")
    assert probe < move


# --- mosquitto's entrypoint ----------------------------------------------------------

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def _run(tmp_path: Path, *, wait: int = 5, conf: Path = _CONF) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "JBRAIN_MQTT_CONF": str(conf),
        "JBRAIN_MQTT_TOKEN_FILE": str(tmp_path / "mqtt.token"),
        "JBRAIN_MQTT_CONF_OUT": str(tmp_path / "rendered.conf"),
        "JBRAIN_MQTT_TOKEN_WAIT": str(wait),
    }
    # `true` stands in for mosquitto: the script execs whatever it is given.
    return subprocess.run(
        ["sh", str(_ENTRYPOINT), "true"], env=env, capture_output=True, text=True, timeout=30
    )


@needs_sh
def test_the_entrypoint_renders_the_token_as_go_auths_user_agent(tmp_path: Path) -> None:
    (tmp_path / "mqtt.token").write_text(_TOKEN)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    rendered = tmp_path / "rendered.conf"
    lines = rendered.read_text().splitlines()
    assert lines.count(f"auth_opt_http_user_agent {_TOKEN}") == 1
    assert not [line for line in lines if "UNRENDERED" in line]
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
    # Everything else is the source conf, unchanged and in order (so the option stays in
    # its listener's section).
    source = _CONF.read_text().splitlines()
    assert len(lines) == len(source)
    assert [a for a, b in zip(lines, source, strict=True) if a != b] == [
        f"auth_opt_http_user_agent {_TOKEN}"
    ]
    assert _TOKEN not in result.stdout + result.stderr


@needs_sh
def test_the_entrypoint_waits_for_the_api_to_mint_the_token(tmp_path: Path) -> None:
    timer = threading.Timer(1.5, (tmp_path / "mqtt.token").write_text, args=(_TOKEN,))
    timer.start()
    try:
        result = _run(tmp_path, wait=20)
    finally:
        timer.cancel()
    assert result.returncode == 0, result.stderr
    assert f"auth_opt_http_user_agent {_TOKEN}" in (tmp_path / "rendered.conf").read_text()


@needs_sh
def test_the_entrypoint_gives_up_rather_than_start_a_broker_that_denies_everyone(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, wait=0)
    assert result.returncode == 1
    assert "no api token" in result.stderr
    assert not (tmp_path / "rendered.conf").exists()


@needs_sh
def test_the_entrypoint_refuses_a_token_it_cannot_put_on_a_conf_line(tmp_path: Path) -> None:
    (tmp_path / "mqtt.token").write_text("abc\nauth_plugin /evil.so")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert not (tmp_path / "rendered.conf").exists()


@needs_sh
def test_the_entrypoint_refuses_a_conf_with_no_line_to_fill(tmp_path: Path) -> None:
    (tmp_path / "mqtt.token").write_text(_TOKEN)
    conf = tmp_path / "bare.conf"
    conf.write_text("listener 1883\n")
    result = _run(tmp_path, conf=conf)
    assert result.returncode == 1
    assert "no auth_opt_http_user_agent" in result.stderr


def test_the_conf_uses_options_go_auth_actually_reads() -> None:
    """go-auth's http backend reads `http_timeout` (seconds); the `_ms` option that was here
    was silently ignored. Confirmed against the pinned build's go-auth.so."""
    conf = _CONF.read_text()
    assert "auth_opt_http_timeout 2\n" in conf
    assert "response_timeout_ms" not in conf
    assert "auth_opt_http_user_agent JBRAIN_MQTT_TOKEN_UNRENDERED\n" in conf


# --- compose and the image -----------------------------------------------------------


def _compose() -> dict[str, Any]:
    return yaml.safe_load((_DEPLOY / "docker-compose.yml").read_text())


def _mounts(service: str) -> list[str]:
    return [v for v in _compose()["services"][service]["volumes"] if isinstance(v, str)]


def test_only_the_api_can_write_the_internal_secrets() -> None:
    compose = _compose()
    assert "internal_secrets" in compose["volumes"]
    users = {
        name: [m for m in spec.get("volumes") or [] if str(m).startswith("internal_secrets:")]
        for name, spec in compose["services"].items()
    }
    assert {name for name, mounts in users.items() if mounts} == {"api", "wall", "mqtt", "cast"}
    assert users["api"] == ["internal_secrets:/run/jbrain-internal"]
    assert users["wall"] == ["internal_secrets:/run/jbrain-internal:ro"]
    assert users["mqtt"] == ["internal_secrets:/run/jbrain-internal:ro"]
    assert users["cast"] == ["internal_secrets:/run/jbrain-internal:ro"]


def test_only_the_api_and_the_wall_hold_the_enrollment_screen_token() -> None:
    # The screen hands out the code that enrolls an owner phone, so the cast sidecar and
    # mosquitto, which mount `internal_secrets`, must not reach its token.
    compose = _compose()
    assert "enroll_secrets" in compose["volumes"]
    users = {
        name: [m for m in spec.get("volumes") or [] if str(m).startswith("enroll_secrets:")]
        for name, spec in compose["services"].items()
    }
    assert {name for name, mounts in users.items() if mounts} == {"api", "wall"}
    assert users["api"] == ["enroll_secrets:/run/jbrain-enroll"]
    assert users["wall"] == ["enroll_secrets:/run/jbrain-enroll:ro"]


def test_the_enrollment_screen_is_published_to_the_box_alone() -> None:
    published = [
        (name, port)
        for name, spec in _compose()["services"].items()
        for port in spec.get("ports") or []
        if str(port).endswith(":8802")
    ]
    assert published == [("wall", "127.0.0.1:8802:8802")]


def test_mosquitto_runs_the_entrypoint_from_the_checkout() -> None:
    mqtt = _compose()["services"]["mqtt"]
    assert mqtt["entrypoint"] == [
        "/usr/bin/tini",
        "--",
        "/bin/sh",
        "/etc/jbrain-mosquitto/entrypoint.sh",
    ]
    assert "./src/deploy/mosquitto:/etc/jbrain-mosquitto:ro" in _mounts("mqtt")
    assert "api" in mqtt["depends_on"]
    # Pinned: the conf depends on this build's option names and .so path.
    assert "@sha256:" in mqtt["image"]


def test_the_image_pre_owns_the_secrets_mount_for_the_app_user() -> None:
    dockerfile = (_DEPLOY.parent / "backend" / "Dockerfile").read_text()
    assert "mkdir -p /run/jbrain-internal && chown appuser /run/jbrain-internal" in dockerfile
    assert dockerfile.index("/run/jbrain-internal") < dockerfile.index("USER appuser")
