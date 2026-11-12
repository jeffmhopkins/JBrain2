"""The GrowBot sidecar (growbot/server/serve.py), loaded by path and run for real.

The server runs on its own event loop in a thread, against fake api / tts-stt servers on
loopback, and is driven over real sockets: HTTP through http.client, WebSocket through raw
frames a test builds by hand (so masking, fragmentation and control frames are the wire's,
not a library's). Security paths: the session cookie, the /open ticket exchange, the CSP on
every response, static traversal, and the relay's device-key check.
"""

from __future__ import annotations

import asyncio
import base64
import html
import http.client
import importlib.util
import json
import os
import re
import socket
import sys
import threading
import time
import types
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_SERVE = _ROOT / "growbot" / "server" / "serve.py"
_DEPLOY = _ROOT / "deploy"
_TOKEN = "growbot-token-" + "b2" * 16
_HASH = "sha256-" + base64.b64encode(b"x" * 32).decode()
_PERSONA = "0b7c2f4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
_PHONE = "phone-" + "a" * 20
_NEW_PHONE = "phone-" + "n" * 20
_JPEG = b"\xff\xd8\xff\xe0face"


def authed_get(handler: BaseHTTPRequestHandler) -> bool:
    return handler.headers.get("Authorization") == f"Bearer {_TOKEN}"


@pytest.fixture
def gb(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("growbot_serve_under_test", _SERVE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


# --- fake upstreams -------------------------------------------------------------------


class _Fake(BaseHTTPRequestHandler):
    """api, tts-stt Kokoro and whisper on one loopback server, told apart by path."""

    protocol_version = "HTTP/1.1"
    seen: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    stt_reply: bytes = b""
    root_cert: bytes = b"-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n"
    tts_status = 200
    llm_status = 200
    # Who wears each robot: code -> (persona, phone). Nobody, unless a test says so.
    riders: dict[str, tuple[str, str]] = {}
    body_status = 200
    # The soul routes' answer: (status, body) per path, or the default below.
    soul: dict[str, tuple[int, dict[str, Any]]] = {}
    # Each robot's saved calibration (None: never saved), and the status the api answers.
    cals: dict[str, dict[str, Any] | None] = {}
    cal_status = 200

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return None

    def _reply(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body: bytes) -> None:
        _Fake.seen.append(
            {
                "method": self.command,
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "ctype": self.headers.get("Content-Type", ""),
                "body": body,
                "persona": self.headers.get("X-Growbot-Persona", ""),
                "robot": self.headers.get("X-Growbot-Robot", ""),
            }
        )

    def do_GET(self) -> None:
        self._record(b"")
        path, _, query = self.path.partition("?")
        if path == "/internal/growbot/lan-root":
            if self.headers.get("Authorization") != f"Bearer {_TOKEN}":
                self._reply(401, b"{}")
                return
            self._reply(200, _Fake.root_cert, "application/x-pem-file")
        elif path == "/internal/growbot/llm/v1/models":
            if self.headers.get("Authorization") != f"Bearer {_TOKEN}":
                self._reply(401, b"{}")
                return
            self._reply(200, json.dumps({"data": _Fake.models}).encode())
        elif path.startswith("/internal/growbot/body/"):
            if not authed_get(self) or _Fake.body_status != 200:
                self._reply(_Fake.body_status if authed_get(self) else 401, b"{}")
                return
            persona, phone = _Fake.riders.get(path.rsplit("/", 1)[1], (None, None))
            self._reply(200, json.dumps({"persona": persona, "phone": phone}).encode())
        elif path.startswith("/internal/growbot/robots/") and path.endswith("/cal"):
            if not authed_get(self) or _Fake.cal_status != 200:
                self._reply(_Fake.cal_status if authed_get(self) else 401, b"{}")
                return
            code = path.split("/")[4]
            self._reply(200, json.dumps({"cal": _Fake.cals.get(code)}).encode())
        elif path == "/tts":
            if _Fake.tts_status != 200:
                self._reply(_Fake.tts_status, b"down", "text/plain")
                return
            self._reply(200, b"RIFF" + query.encode(), "audio/wav")
        else:
            self._reply(404, b"{}")

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._record(body)
        authed = self.headers.get("Authorization") == f"Bearer {_TOKEN}"
        if self.path == "/internal/growbot/ticket/redeem":
            # The api answers with the robot the ticket was minted for.
            minted = {"good-ticket": "gb-abc123", "bare-ticket": "", "odd-ticket": "../x"}
            sent = json.loads(body)
            ticket = sent.get("ticket")
            if authed and ticket in ("persona-ticket", "persona-bodiless", "persona-odd"):
                # The api keeps the phone id it was given, or makes one.
                phone = sent.get("phone") or _NEW_PHONE
                reply = {
                    "ok": True,
                    "code": "gb-abc123" if ticket == "persona-ticket" else "",
                    "persona": "not-a-uuid" if ticket == "persona-odd" else _PERSONA,
                    "phone": phone,
                }
                self._reply(200, json.dumps(reply).encode())
                return
            ok = authed and ticket in minted
            reply = {"ok": ok, "code": minted.get(ticket, "")} if ok else {"ok": False}
            self._reply(200 if ok else 401, json.dumps(reply).encode())
        elif self.path.startswith("/internal/growbot/soul/"):
            if not authed:
                self._reply(401, b"{}")
                return
            default = (200, {"version": 3, "soul": {"soul_format": 3}, "wearing": "gb-abc123"})
            if self.path.endswith("/push"):
                default = (200, {"version": 4})
            elif self.path.endswith("/state"):
                default = (200, {"version": 3, "wearing": "gb-abc123"})
            status, reply = _Fake.soul.get(self.path, default)
            self._reply(status, json.dumps(reply).encode())
        elif self.path.startswith("/internal/growbot/memories/"):
            if not authed:
                self._reply(401, b"{}")
                return
            if self.path.endswith("/view") and self.path not in _Fake.soul:
                self._reply(200, _JPEG, "image/jpeg")
                return
            default = (200, {"version": 3})
            if self.path.endswith("/list"):
                default = (200, {"version": 2, "memories": []})
            status, reply = _Fake.soul.get(self.path, default)
            self._reply(status, json.dumps(reply).encode())
        elif self.path == "/internal/growbot/robot/verify":
            ok = authed and json.loads(body) == {"code": "gb-abc123", "key": "right-key"}
            self._reply(200 if ok else 401, json.dumps({"ok": ok}).encode())
        elif self.path == "/internal/growbot/llm/v1/chat/completions":
            if not authed:
                self._reply(401, b'{"detail":"no"}')
                return
            if _Fake.llm_status != 200:
                self._reply(_Fake.llm_status, b'{"error":{"message":"engine down"}}')
                return
            if json.loads(body).get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for word in ("hel", "lo"):
                    delta = {"choices": [{"delta": {"content": word}}]}
                    event = f"data: {json.dumps(delta)}\n\n".encode()
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                    self.wfile.flush()
                    time.sleep(0.05)
                self.wfile.write(b"e\r\ndata: [DONE]\n\n\r\n0\r\n\r\n")
                return
            reply = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
            self._reply(200, json.dumps(reply).encode())
        elif self.path == "/v1/audio/transcriptions":
            self._reply(200, _Fake.stt_reply)
        elif self.path == "/internal/growbot/calls":
            self._reply(204 if authed else 401, b"")
        else:
            self._reply(404, b"{}")


class _Running:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        start = asyncio.run_coroutine_threadsafe(app.start("127.0.0.1", 0), self.loop)
        self.server = start.result(5)
        self.port = self.server.sockets[0].getsockname()[1]
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"https://{self.host}"
        self.cookie = f"gb_session={app.sessions.mint(robot='gb-abc123')}"

    def stop(self) -> None:
        async def shutdown() -> None:
            self.server.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        *,
        cookie: bool = True,
    ) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = dict(headers or {})
        if cookie:
            sent.setdefault("Cookie", self.cookie)
        conn.request(method, path, body=body, headers=sent)
        return conn.getresponse()


@pytest.fixture
def site(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    (root / "v3").mkdir(parents=True)
    (root / "ml").mkdir()
    (root / "index.html").write_text("<p>index</p>")
    (root / "v3" / "next.html").write_text("<p>creature</p>")
    (root / "v3" / "mod.mjs").write_text("export {}")
    (root / "ml" / "ort.wasm").write_bytes(b"\0asm" + b"\0" * 300_000)
    (root / "ml" / "vad.onnx").write_bytes(b"onnx")
    (root / ".hidden").write_text("dotfile")
    (root / "csp.json").write_text(json.dumps({"script_hashes": [_HASH], "unsafe_hashes": True}))
    (root / "icons").mkdir()
    for name in (
        "manifest.webmanifest",
        "sw.js",
        "setup.html",
        "setup.js",
        "growbot-box.js",
        "icons/growbot-192.png",
        "icons/growbot-512.png",
    ):
        (root / name).write_bytes(b"app file")
    (root / "icons" / "other.png").write_bytes(b"not public")
    (tmp_path / "secret.txt").write_text("outside the root")
    return root


@pytest.fixture
def fake() -> Iterator[str]:
    _Fake.seen = []
    _Fake.models = [{"id": "text-only", "vision": False}, {"id": "qwen3-vl-30b", "vision": True}]
    _Fake.stt_reply = b"{}"
    _Fake.tts_status = 200
    _Fake.llm_status = 200
    _Fake.riders = {}
    _Fake.body_status = 200
    _Fake.soul = {}
    _Fake.cals = {}
    _Fake.cal_status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def run(gb: types.ModuleType, site: Path, fake: str, tmp_path: Path) -> Iterator[_Running]:
    token = tmp_path / "growbot.token"
    token.write_text(_TOKEN + "\n")
    config = gb.Config(
        site=site,
        api_url=fake,
        tts_url=fake,
        stt_url=fake + "/v1",
        token_file=token,
        trusted_proxy="127.0.0.2",
    )
    running = _Running(gb.GrowBot(config))
    yield running
    running.stop()


# --- the session cookie and the CSP -----------------------------------------------------


def test_a_session_is_signed_expiring_and_per_process(gb: types.ModuleType) -> None:
    sessions = gb.Sessions(b"k" * 32)
    value = sessions.mint(now=1000)
    assert sessions.valid(value, now=1000 + gb.SESSION_TTL_S - 1)
    assert not sessions.valid(value, now=1000 + gb.SESSION_TTL_S)
    assert not gb.Sessions(b"j" * 32).valid(value, now=1000)
    expiry, nonce, robot, sig = value.split(".")
    assert robot == "" and sessions.robot(value, now=1000) == ""
    assert not sessions.valid(f"{int(expiry) + 999}.{nonce}.{robot}.{sig}", now=1000)
    for bad in ("", "a.b", "x.y.z", "1.2.3", "1.2.3.4.5", f"{expiry}.{nonce}.{robot}.{sig}x"):
        assert not sessions.valid(bad, now=1000)
        assert sessions.robot(bad, now=1000) is None


def test_a_session_carries_its_robot_under_the_mac(gb: types.ModuleType) -> None:
    sessions = gb.Sessions(b"k" * 32)
    value = sessions.mint(now=1000, robot="gb-abc123")
    assert sessions.robot(value, now=1000) == "gb-abc123"
    expiry, nonce, _robot, sig = value.split(".")
    assert sessions.robot(f"{expiry}.{nonce}.gb-other1.{sig}", now=1000) is None
    assert sessions.robot(f"{expiry}.{nonce}..{sig}", now=1000) is None
    with pytest.raises(ValueError):
        sessions.mint(robot="gb-x.y")


def test_the_csp_takes_the_sites_hashes_and_never_unsafe_inline(
    gb: types.ModuleType, site: Path
) -> None:
    csp = gb.build_csp(site)
    script = next(d for d in csp.split("; ") if d.startswith("script-src "))
    assert script == f"script-src 'self' 'wasm-unsafe-eval' '{_HASH}' 'unsafe-hashes'"
    assert "connect-src 'self' blob:" in csp and "default-src 'self'" in csp
    assert "font-src 'self' data:" in csp
    assert "form-action 'self'" in csp and "object-src 'none'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    (site / "csp.json").write_text(
        json.dumps({"script_hashes": ["'unsafe-inline'", "sha256-ok="], "unsafe_hashes": "yes"})
    )
    script = next(d for d in gb.build_csp(site).split("; ") if d.startswith("script-src "))
    assert script == "script-src 'self' 'wasm-unsafe-eval' 'sha256-ok='"
    (site / "csp.json").unlink()
    script = next(d for d in gb.build_csp(site).split("; ") if d.startswith("script-src "))
    assert script == "script-src 'self' 'wasm-unsafe-eval'"


@pytest.mark.parametrize(
    ("method", "path", "cookie"),
    [
        ("GET", "/v3/next.html", True),
        ("GET", "/v3/next.html", False),
        ("GET", "/nope", True),
        ("GET", "/api/config", True),
        ("POST", "/api/ev", True),
        ("GET", "/open?ticket=bad&id=gb-abc123", False),
        ("GET", "/d/gb-abc123", True),
    ],
)
def test_every_response_carries_the_csp_and_headers(
    run: _Running, method: str, path: str, cookie: bool
) -> None:
    resp = run.request(method, path, b"" if method == "POST" else None, cookie=cookie)
    resp.read()
    assert resp.getheader("Content-Security-Policy") == run.app.csp
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert resp.getheader("Referrer-Policy") == "no-referrer"
    policy = resp.getheader("Permissions-Policy") or ""
    assert "camera=(self)" in policy and "microphone=(self)" in policy


# --- the cookie gate and /open ----------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/v3/next.html", "/api/config", "/api/device", "/x"])
def test_without_a_session_everything_is_refused(run: _Running, path: str) -> None:
    for cookie in (None, "gb_session=", "gb_session=1.2.3", "other=1"):
        headers = {"Cookie": cookie} if cookie else {}
        resp = run.request("GET", path, headers=headers, cookie=bool(cookie))
        assert resp.status == 401
        assert b"Open GrowBot from JBrain" in resp.read()


def _landing(resp: Any) -> str:
    """Where /open's page moves the phone on to (its meta refresh), after checking it is one."""
    assert resp.status == 200
    m = re.search(rb"http-equiv=refresh content='0;url=([^']+)'", resp.read())
    assert m is not None
    return html.unescape(m.group(1).decode())


def test_open_exchanges_a_ticket_for_a_session(run: _Running, gb: types.ModuleType) -> None:
    resp = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", cookie=False)
    assert _landing(resp) == "/v3/next.html?id=gb-abc123&mode=legs"
    assert resp.getheader("Location") is None
    # The spent ticket sits in this page's URL: never cached, never sent on as a Referer.
    assert resp.getheader("Cache-Control") == "no-store"
    assert resp.getheader("Referrer-Policy") == "no-referrer"
    (session,) = resp.headers.get_all("Set-Cookie") or []
    name_value, *attrs = [a.strip() for a in session.split(";")]
    assert set(attrs) == {"HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=2592000"}
    assert name_value.startswith("gb_session=")
    assert run.app.sessions.robot(name_value.partition("=")[2]) == "gb-abc123"
    redeem = [s for s in _Fake.seen if s["path"] == "/internal/growbot/ticket/redeem"]
    assert redeem[-1]["auth"] == f"Bearer {_TOKEN}"
    page = run.request("GET", "/v3/next.html", headers={"Cookie": name_value}, cookie=False)
    assert page.status == 200
    assert page.read() == b"<p>creature</p>"


def test_sessions_keyed_by_the_token_outlive_a_restart_and_die_with_its_remint(
    gb: types.ModuleType, tmp_path: Path
) -> None:
    token = tmp_path / "growbot.token"
    token.write_text("first-token\n")
    value = gb.Sessions(key_file=token).mint()
    assert gb.Sessions(key_file=token).valid(value)
    token.write_text("second-token\n")
    assert not gb.Sessions(key_file=token).valid(value)
    token.unlink()
    alone = gb.Sessions(key_file=token)
    assert alone.valid(alone.mint())
    assert not gb.Sessions(key_file=token).valid(alone.mint())


def test_a_session_is_due_for_renewal_after_a_day(gb: types.ModuleType) -> None:
    sessions = gb.Sessions(b"k" * 32)
    value = sessions.mint(now=1000)
    assert not sessions.due(value, now=1000 + gb.SESSION_RENEW_S - 1)
    assert sessions.due(value, now=1000 + gb.SESSION_RENEW_S + 1)
    assert not sessions.due(value, now=1000 + gb.SESSION_TTL_S)
    assert not sessions.due("1.2.3", now=1000)


def test_an_old_session_is_renewed_by_any_page_or_call(run: _Running, gb: types.ModuleType) -> None:
    fresh = run.request("GET", "/v3/next.html")
    fresh.read()
    assert fresh.getheader("Set-Cookie") is None
    old = run.app.sessions.mint(now=time.time() - gb.SESSION_RENEW_S - 5, robot="gb-abc123")
    for path in ("/v3/next.html", "/api/device"):
        resp = run.request("GET", path, headers={"Cookie": f"gb_session={old}"})
        resp.read()
        renewed = resp.getheader("Set-Cookie") or ""
        assert renewed.startswith("gb_session=") and "Max-Age=2592000" in renewed
        value = renewed.split(";")[0].partition("=")[2]
        assert run.app.sessions.robot(value) == "gb-abc123"


def test_signing_every_phone_out_ends_every_session(gb: types.ModuleType, tmp_path: Path) -> None:
    token, epoch = tmp_path / "growbot.token", tmp_path / "growbot-session.token"
    token.write_text("the-token\n")
    sessions = gb.Sessions(key_file=token, epoch_file=epoch)
    before = sessions.mint()
    epoch.write_text("first\n")
    assert not sessions.valid(before)
    during = sessions.mint()
    assert gb.Sessions(key_file=token, epoch_file=epoch).valid(during)
    epoch.write_text("second\n")
    assert not sessions.valid(during)


def test_a_failed_read_keeps_the_last_key(gb: types.ModuleType, tmp_path: Path) -> None:
    token = tmp_path / "growbot.token"
    token.write_text("the-token\n")
    sessions = gb.Sessions(key_file=token)
    value = sessions.mint()
    token.unlink()
    token.mkdir()  # reading a directory fails with an OSError that is not a missing file
    assert sessions.valid(value)


def test_the_ticket_not_the_link_names_the_robot(run: _Running) -> None:
    for query in ("ticket=good-ticket&id=gb-other1", "ticket=good-ticket"):
        resp = run.request("GET", "/open?" + query, cookie=False)
        assert _landing(resp) == "/v3/next.html?id=gb-abc123&mode=legs"
        cookie = (resp.getheader("Set-Cookie") or "").split(";")[0]
        assert run.app.sessions.robot(cookie.partition("=")[2]) == "gb-abc123"
    odd = run.request("GET", "/open?ticket=odd-ticket", cookie=False)
    odd.read()
    assert odd.status == 401 and odd.getheader("Set-Cookie") is None


def test_a_robotless_session_can_join_no_robot(run: _Running) -> None:
    resp = run.request("GET", "/open?ticket=bare-ticket&id=gb-abc123", cookie=False)
    resp.read()
    cookie = (resp.getheader("Set-Cookie") or "").split(";")[0]
    assert run.app.sessions.robot(cookie.partition("=")[2]) == ""
    for path in ("/d/gb-abc123", "/d/gb-abc123-agent"):
        ws = _Ws(run, path, cookie=cookie)
        assert ws.status == 403, ws.head


def test_a_phone_joins_only_the_robot_its_ticket_named(run: _Running) -> None:
    for path in ("/d/gb-other1", "/d/gb-other1-agent"):
        ws = _Ws(run, path, cookie=True)
        assert ws.status == 403, ws.head
    ok = _phone(run, "/d/gb-abc123")
    assert ok.status == 101


def test_start_reopens_the_creature_on_its_robot(run: _Running) -> None:
    resp = run.request("GET", "/start")
    resp.read()
    assert resp.status == 302
    assert resp.getheader("Location") == "/v3/next.html?id=gb-abc123&mode=legs"
    renewed = (resp.getheader("Set-Cookie") or "").split(";")[0].partition("=")[2]
    assert run.app.sessions.robot(renewed) == "gb-abc123"
    bare_cookie = f"gb_session={run.app.sessions.mint()}"
    bare = run.request("GET", "/start", headers={"Cookie": bare_cookie}, cookie=False)
    bare.read()
    assert bare.getheader("Location") == "/v3/next.html?mode=legs"


def test_start_without_a_session_goes_to_the_setup_page(run: _Running) -> None:
    resp = run.request("GET", "/start", cookie=False)
    resp.read()
    assert resp.status == 302 and resp.getheader("Location") == "/setup.html"
    assert resp.getheader("Set-Cookie") is None


def test_the_box_root_is_served_to_a_phone_signed_in_to_nothing(run: _Running) -> None:
    resp = run.request("GET", "/box-root.crt", cookie=False)
    body = resp.read()
    assert resp.status == 200 and body == _Fake.root_cert
    assert resp.getheader("Content-Type") == "application/x-x509-ca-cert"
    assert resp.getheader("Content-Disposition") == 'attachment; filename="jbrain-box-root.crt"'
    asked = [s for s in _Fake.seen if s["path"] == "/internal/growbot/lan-root"]
    assert asked and asked[-1]["auth"] == f"Bearer {_TOKEN}"
    again = run.request("GET", "/box-root.crt", cookie=False)
    again.read()
    # Cached: the api is asked once per few minutes, not per phone.
    assert len([s for s in _Fake.seen if s["path"] == "/internal/growbot/lan-root"]) == 1
    post = run.request("POST", "/box-root.crt", b"", cookie=False)
    post.read()
    assert post.status in (401, 405)


def test_no_root_from_the_api_is_a_404_not_junk(run: _Running) -> None:
    _Fake.root_cert = b"<html>not a cert</html>"
    try:
        resp = run.request("GET", "/box-root.crt", cookie=False)
        resp.read()
        assert resp.status == 404
        again = run.request("GET", "/box-root.crt", cookie=False)
        again.read()
        # The failure is remembered too, so a flood of LAN requests is not a flood of api calls.
        assert len([s for s in _Fake.seen if s["path"] == "/internal/growbot/lan-root"]) == 1
    finally:
        _Fake.root_cert = b"-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n"


def test_start_takes_no_other_method(run: _Running) -> None:
    resp = run.request("POST", "/start", b"")
    resp.read()
    assert resp.status == 405


@pytest.mark.parametrize(
    "path",
    [
        "/manifest.webmanifest",
        "/sw.js",
        "/setup.html",
        "/setup.js",
        "/icons/growbot-192.png",
        "/icons/growbot-512.png",
        "/growbot-box.js",
    ],
)
def test_the_installed_apps_files_need_no_session(run: _Running, path: str) -> None:
    resp = run.request("GET", path, cookie=False)
    resp.read()
    assert resp.status == 200
    assert resp.getheader("Content-Security-Policy") == run.app.csp
    refused = run.request("POST", path, b"x", cookie=False)
    refused.read()
    assert refused.status == 401


@pytest.mark.parametrize("path", ["/icons/other.png", "/sw.js.map", "/manifest.json", "/start/x"])
def test_only_the_named_app_files_are_public(run: _Running, path: str) -> None:
    resp = run.request("GET", path, cookie=False)
    resp.read()
    assert resp.status == 401


@pytest.mark.parametrize(
    "query",
    ["ticket=used-ticket&id=gb-abc123", "ticket=&id=gb-abc123", "ticket=good-ticket&id=../x"],
)
def test_open_refuses_a_bad_ticket_or_code(run: _Running, query: str) -> None:
    resp = run.request("GET", "/open?" + query, cookie=False)
    resp.read()
    assert resp.status in (400, 401)
    assert resp.getheader("Set-Cookie") is None


def test_open_without_the_token_file_sets_no_cookie(run: _Running) -> None:
    run.app.config.token_file.unlink()
    resp = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", cookie=False)
    resp.read()
    assert resp.status == 401
    assert resp.getheader("Set-Cookie") is None
    assert _Fake.seen[-1]["auth"] == ""


def test_open_with_the_api_down_is_refused(run: _Running) -> None:
    run.app.config.api_url = "http://127.0.0.1:9"
    resp = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", cookie=False)
    resp.read()
    assert resp.status == 401


def test_failed_tickets_back_off_per_client(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "BACKOFF_LIMIT", 2)
    for _ in range(2):
        resp = run.request("GET", "/open?ticket=bad&id=gb-abc123", cookie=False)
        assert resp.status == 401
        resp.read()
    resp = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", cookie=False)
    assert resp.status == 429 and resp.getheader("Set-Cookie") is None
    resp.read()
    # X-Real-IP names the client only when the peer is the proxy: here it is not, so a forged
    # header does not dodge the wait.
    forged = {"X-Real-IP": "10.9.9.9"}
    assert (
        run.request(
            "GET", "/open?ticket=good-ticket&id=gb-abc123", headers=forged, cookie=False
        ).status
        == 429
    )


def test_behind_the_proxy_each_client_backs_off_alone(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "BACKOFF_LIMIT", 2)
    run.app.config.trusted_proxy = "127.0.0.1"
    for _ in range(2):
        resp = run.request(
            "GET", "/open?ticket=bad&id=gb-abc123", headers={"X-Real-IP": "10.0.0.1"}, cookie=False
        )
        resp.read()
    blocked = run.request(
        "GET",
        "/open?ticket=good-ticket&id=gb-abc123",
        headers={"X-Real-IP": "10.0.0.1"},
        cookie=False,
    )
    assert blocked.status == 429
    blocked.read()
    other = run.request(
        "GET",
        "/open?ticket=good-ticket&id=gb-abc123",
        headers={"X-Real-IP": "10.0.0.2"},
        cookie=False,
    )
    assert other.status == 200


def test_a_failed_proxy_lookup_keeps_the_last_good_address(
    gb: types.ModuleType, site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = gb.Config(
        site=site,
        api_url="http://127.0.0.1:9",
        tts_url="http://127.0.0.1:9",
        stt_url="http://127.0.0.1:9",
        token_file=tmp_path / "none",
        trusted_proxy="127.0.0.1",
    )
    app = gb.GrowBot(config)
    req = gb.Request("GET", "/", "/", {}, "HTTP/1.1", [("x-real-ip", "10.0.0.7")], peer="127.0.0.1")
    assert asyncio.run(app.client_ip(req)) == "10.0.0.7"

    def broken(*_: object) -> None:
        raise OSError("dns down")

    monkeypatch.setattr(gb.socket, "getaddrinfo", broken)
    app._proxy_at = -float("inf")
    assert asyncio.run(app.client_ip(req)) == "10.0.0.7"
    assert app._proxy_addrs == {"127.0.0.1"}


def test_through_the_tunnel_the_client_is_cloudflares_answer(
    gb: types.ModuleType, site: Path, tmp_path: Path
) -> None:
    # Everyone away from home reaches Caddy from the connector: without this they would share
    # one backoff bucket, and a few bad tickets from anywhere would lock the owner out.
    config = gb.Config(
        site=site,
        api_url="http://127.0.0.1:9",
        tts_url="http://127.0.0.1:9",
        stt_url="http://127.0.0.1:9",
        token_file=tmp_path / "none",
        trusted_proxy="127.0.0.1",
        trusted_tunnel="127.0.0.5",
    )
    app = gb.GrowBot(config)

    def ip(peer: str, headers: list[tuple[str, str]]) -> str:
        req = gb.Request("GET", "/", "/", {}, "HTTP/1.1", headers, peer=peer)
        return str(asyncio.run(app.client_ip(req)))

    cf = ("cf-connecting-ip", "203.0.113.9")
    assert ip("127.0.0.1", [("x-real-ip", "127.0.0.5"), cf]) == "203.0.113.9"
    # A malformed claim is no address: the connector's own bucket, never a made-up one.
    assert ip("127.0.0.1", [("x-real-ip", "127.0.0.5"), ("cf-connecting-ip", "x")]) == "127.0.0.5"
    # Only when Caddy saw the connector: a LAN client naming any address is that client.
    assert ip("127.0.0.1", [("x-real-ip", "10.0.0.7"), cf]) == "10.0.0.7"
    # And only from Caddy: anything else on the network is its own socket address.
    assert ip("127.0.0.9", [("x-real-ip", "127.0.0.5"), cf]) == "127.0.0.9"


def test_away_buckets_take_an_ipv6_client_whole(gb: types.ModuleType) -> None:
    # One client holds a whole /64, so stepping through it must not buy fresh tries.
    assert gb._bucket("2001:db8:1:2:aaaa::1") == gb._bucket("2001:db8:1:2:ffff::9")
    assert gb._bucket("2001:db8:1:2:aaaa::1") == "2001:db8:1:2::/64"
    assert gb._bucket("2001:db8:1:3::1") != gb._bucket("2001:db8:1:2::1")
    assert gb._bucket("::ffff:203.0.113.9") == "203.0.113.9"
    assert gb._bucket("203.0.113.9") == "203.0.113.9"
    assert gb._bucket("nope") is None


def test_a_client_spends_one_ticket_at_a_time_and_the_gate_has_a_ceiling(
    run: _Running, gb: types.ModuleType
) -> None:
    # The backoff counts a failure only once the api answers, so without these one client could
    # queue hundreds of junk tickets on the gate, ahead of the robots' hellos.
    run.app._opening["127.0.0.1"] = gb.OPENING_PER_CLIENT
    busy = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", cookie=False)
    assert busy.status == 429
    busy.read()
    del run.app._opening["127.0.0.1"]
    for i in range(gb.OPENING_MAX):
        run.app._opening[f"10.0.0.{i}"] = 1
    full = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", cookie=False)
    assert full.status == 503
    full.read()
    run.app._opening.clear()
    ok = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", cookie=False)
    assert ok.status == 200
    ok.read()
    assert not run.app._opening


def test_backoff_window(gb: types.ModuleType) -> None:
    backoff = gb.Backoff()
    for t in range(gb.BACKOFF_LIMIT):
        assert not backoff.blocked("a", now=float(t))
        backoff.fail("a", now=float(t))
    assert backoff.blocked("a", now=10.0)
    assert not backoff.blocked("b", now=10.0)
    assert not backoff.blocked("a", now=gb.BACKOFF_LIMIT + gb.BACKOFF_WINDOW_S)


# --- static -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "ctype"),
    [
        ("/v3/mod.mjs", "text/javascript; charset=utf-8"),
        ("/ml/ort.wasm", "application/wasm"),
        ("/ml/vad.onnx", "application/octet-stream"),
        ("/csp.json", "application/json"),
        ("/", "text/html; charset=utf-8"),
    ],
)
def test_static_files_are_served_with_their_type(run: _Running, path: str, ctype: str) -> None:
    resp = run.request("GET", path)
    body = resp.read()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == ctype
    assert int(resp.getheader("Content-Length") or -1) == len(body)


def test_a_large_file_streams_whole_and_revalidates(run: _Running, site: Path) -> None:
    resp = run.request("GET", "/ml/ort.wasm")
    assert resp.read() == (site / "ml" / "ort.wasm").read_bytes()
    etag = resp.getheader("ETag") or ""
    again = run.request("GET", "/ml/ort.wasm", headers={"If-None-Match": etag})
    assert again.status == 304 and again.read() == b""
    head = run.request("HEAD", "/ml/ort.wasm")
    assert head.status == 200 and head.read() == b""


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/v3/%2e%2e/%2e%2e/secret.txt",
        "/v3/..%2fsecret.txt",
        "/..%5csecret.txt",
        "/.hidden",
        "/v3/",
        "/v3/next.html%00.txt",
        "/%ff",
        "/missing.js",
    ],
)
def test_no_traversal_dotfile_or_listing(run: _Running, path: str) -> None:
    raw = socket.create_connection(("127.0.0.1", run.port), timeout=5)
    with raw:
        raw.sendall(
            f"GET {path} HTTP/1.1\r\nHost: x\r\nCookie: {run.cookie}\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        data = b""
        while chunk := raw.recv(65536):
            data += chunk
    assert data.startswith(b"HTTP/1.1 404"), data[:80]
    assert b"outside the root" not in data and b"dotfile" not in data


def test_a_symlink_out_of_the_root_is_not_followed(run: _Running, site: Path) -> None:
    (site / "link.txt").symlink_to(site.parent / "secret.txt")
    resp = run.request("GET", "/link.txt")
    assert resp.status == 404 and b"outside" not in resp.read()


def test_static_refuses_writes(run: _Running) -> None:
    resp = run.request("POST", "/v3/next.html", b"x")
    resp.read()
    assert resp.status == 405


# --- the /api shim ----------------------------------------------------------------------


def test_config_hands_out_an_installed_vision_lineup(run: _Running) -> None:
    resp = run.request("GET", "/api/config")
    cfg = json.loads(resp.read())
    assert cfg["fast"] == cfg["active"] == cfg["dream"] == "qwen3-vl-30b"
    models = json.loads(run.request("GET", "/api/models").read())
    assert [m["id"] for m in models["all"]] == ["text-only", "qwen3-vl-30b"]
    assert models["simple"] == models["all"]
    assert models["all"][1]["vision"] is True and models["all"][1]["callEst"] == 0


def test_config_falls_back_when_the_api_lists_nothing(run: _Running, gb: types.ModuleType) -> None:
    run.app.config.token_file.unlink()
    # The session key follows the token; this test is about the api call.
    run.cookie = f"gb_session={run.app.sessions.mint()}"
    cfg = json.loads(run.request("GET", "/api/config").read())
    assert cfg["fast"] == "qwen3-vl-30b"
    app = run.app
    assert app.default_model([{"id": "a", "vision": False}]) == "a"
    assert app.default_model([{"id": "a", "vision": False}, {"id": "b", "vision": True}]) == "b"


def test_device_is_one_owned_unmetered_device(run: _Running) -> None:
    for method in ("GET", "POST"):
        resp = run.request(method, "/api/device", b"" if method == "POST" else None)
        device = json.loads(resp.read())
        assert device["ok"] is True and device["hasPass"] is True
        assert device["token"] and len(device["did"]) == 16


@pytest.mark.parametrize("name", ["journal", "episodes", "gallery", "ev", "hit", "feedback"])
def test_telemetry_routes_answer_empty(run: _Running, name: str) -> None:
    resp = run.request("POST", f"/api/{name}?sid=x", b'{"a":1}')
    assert resp.status == 204 and resp.read() == b""
    assert run.request("GET", f"/api/{name}?type=policy").status == 204


@pytest.mark.parametrize("name", ["journal", "episodes"])
def test_erasing_a_cloud_store_answers_ok(run: _Running, name: str) -> None:
    # The app's erase flow parses the answer as JSON; there is nothing here to erase.
    resp = run.request("DELETE", f"/api/{name}?sid=abc", headers={"Origin": run.origin})
    assert resp.status == 200 and json.loads(resp.read()) == {"ok": True}


def test_a_gallery_download_is_not_found(run: _Running) -> None:
    resp = run.request("GET", "/api/gallery?type=soul&id=x&download=1")
    assert resp.status == 404
    resp.read()


def test_an_authed_post_without_an_origin_is_allowed(run: _Running) -> None:
    # Deliberate: a same-origin fetch may omit Origin, and a cross-origin one never can, so
    # only a present, foreign Origin is refused.
    assert run.request("POST", "/api/ev", b"{}").status == 204


@pytest.mark.parametrize("name", ["redeem", "transfer"])
def test_credit_routes_are_not_on_the_box(run: _Running, name: str) -> None:
    resp = run.request("POST", f"/api/{name}", b"{}")
    assert resp.status == 410 and json.loads(resp.read())["ok"] is False


def test_unknown_api_routes_and_targets(run: _Running) -> None:
    assert run.request("POST", "/api/nope", b"{}").status == 404
    assert run.request("POST", "/api/llm?target=elsewhere", b"{}").status == 400
    assert run.request("POST", "/api/llm-stream?target=groq-audio", b"{}").status == 400
    assert run.request("GET", "/api/llm?target=openrouter").status == 405


def test_a_cross_origin_post_is_refused(run: _Running) -> None:
    resp = run.request("POST", "/api/ev", b"{}", {"Origin": "https://jbrain.local"})
    assert resp.status == 403
    resp.read()
    assert run.request("POST", "/api/ev", b"{}", {"Origin": run.origin}).status == 204


def _chat(model: str, **extra: object) -> bytes:
    return json.dumps(
        {
            "model": model,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "provider": {"order": ["x"]},
            "reasoning": {"enabled": False},
            **extra,
        }
    ).encode()


def test_llm_goes_to_the_api_lane_with_only_engine_fields(run: _Running) -> None:
    resp = run.request("POST", "/api/llm?target=openrouter", _chat("google/gemini-3.8-flash"))
    assert resp.status == 200
    assert json.loads(resp.read())["choices"][0]["message"]["content"] == "hi"
    call = [s for s in _Fake.seen if s["path"].endswith("/chat/completions")][-1]
    assert call["auth"] == f"Bearer {_TOKEN}"
    sent = json.loads(call["body"])
    assert sent == {
        "model": "qwen3-vl-30b",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    run.request("POST", "/api/llm?target=openrouter", _chat("text-only")).read()
    call = [s for s in _Fake.seen if s["path"].endswith("/chat/completions")][-1]
    assert json.loads(call["body"])["model"] == "text-only"


def _logged(kind: str, n: int = 1) -> list[dict[str, Any]]:
    """The call-log rows the sidecar posted, waiting for its background worker."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        rows = [
            json.loads(s["body"])
            for s in list(_Fake.seen)
            if s["path"] == "/internal/growbot/calls" and json.loads(s["body"])["kind"] == kind
        ]
        if len(rows) >= n:
            return rows
        time.sleep(0.02)
    raise AssertionError(f"no {kind} row logged")


def test_llm_names_who_the_turn_is_for(run: _Running) -> None:
    run.request("POST", "/api/llm?target=openrouter", _chat("x")).read()
    call = [s for s in _Fake.seen if s["path"].endswith("/chat/completions")][-1]
    assert (call["persona"], call["robot"]) == ("", "gb-abc123")
    cookie = f"gb_session={run.app.sessions.mint(persona=_PERSONA, phone=_PHONE)}"
    run.request("POST", "/api/llm?target=openrouter", _chat("x"), {"Cookie": cookie}).read()
    call = [s for s in _Fake.seen if s["path"].endswith("/chat/completions")][-1]
    assert (call["persona"], call["robot"]) == (_PERSONA, "")


def test_stt_and_tts_each_log_a_row(run: _Running) -> None:
    _Fake.stt_reply = json.dumps({"text": " hello robot "}).encode()
    body, ctype = _multipart({"model": "w", "prompt": "names: Quorra"}, _wav(2.0))
    assert (
        run.request("POST", "/api/llm?target=groq-audio", body, {"Content-Type": ctype}).status
        == 200
    )
    (row,) = _logged("stt")
    assert row["ok"] and row["text"] == "hello robot" and row["robot"] == "gb-abc123"
    assert row["timings"]["audio_s"] == 2.0 and row["timings"]["upstream_ms"] >= 0
    assert row["detail"]["prompt"] == "names: Quorra" and row["detail"]["audio_ctx"] == 160
    call = [s for s in _Fake.seen if s["path"] == "/internal/growbot/calls"][-1]
    assert call["auth"] == f"Bearer {_TOKEN}"

    say = json.dumps({"text": "hi there", "voice": "kokoro-af_heart"}).encode()
    assert run.request("POST", "/api/fish-tts", say).status == 200
    (row,) = _logged("tts")
    assert row["ok"] and row["text"] == "hi there" and row["detail"]["voice"] == "kokoro-af_heart"

    _Fake.tts_status = 500
    assert run.request("POST", "/api/fish-tts", say).status == 503
    failed = _logged("tts", 2)[-1]
    assert not failed["ok"] and failed["error"].startswith("tts HTTP 500")


def test_a_failed_transcription_logs_why(run: _Running) -> None:
    run.app.config.stt_url = "http://127.0.0.1:9/v1"
    body, ctype = _multipart({"model": "w"}, _wav(1.0))
    assert (
        run.request("POST", "/api/llm?target=groq-audio", body, {"Content-Type": ctype}).status
        == 503
    )
    (row,) = _logged("stt")
    assert not row["ok"] and "speech service" in row["error"]


def test_the_log_drops_rows_rather_than_queue_without_end(
    gb: types.ModuleType, tmp_path: Path
) -> None:
    config = gb.Config(
        site=tmp_path,
        api_url="http://127.0.0.1:9",
        tts_url="http://127.0.0.1:9",
        stt_url="http://127.0.0.1:9",
        token_file=tmp_path / "none",
        trusted_proxy="127.0.0.2",
    )
    app = gb.GrowBot(config)
    gate = threading.Event()
    sent: list[dict[str, Any]] = []

    def slow(_path: str, row: dict[str, Any]) -> None:
        gate.wait(5)
        sent.append(row)

    app._api_post = slow
    for i in range(gb.LOG_PENDING_MAX + 10):
        app.log_call({"kind": "tts", "n": i})
    gate.set()
    app._log_pool.shutdown(wait=True)
    assert len(sent) == gb.LOG_PENDING_MAX
    # Every slot came back once its row was sent.
    for _ in range(gb.LOG_PENDING_MAX):
        assert app._log_slots.acquire(blocking=False)


def test_llm_stream_relays_sse_as_it_arrives(run: _Running) -> None:
    resp = run.request("POST", "/api/llm-stream?target=openrouter", _chat("qwen3-vl-30b"))
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/event-stream"
    assert resp.getheader("Transfer-Encoding") == "chunked"
    first = resp.readline()
    assert first.startswith(b"data: ") and b"hel" in first
    rest = resp.read()
    assert b'"lo"' in rest and b"data: [DONE]" in rest
    call = [s for s in _Fake.seen if s["path"].endswith("/chat/completions")][-1]
    assert json.loads(call["body"])["stream"] is True


def test_llm_errors_pass_through_and_a_refused_token_is_not_a_401(run: _Running) -> None:
    _Fake.llm_status = 500
    resp = run.request("POST", "/api/llm-stream?target=openrouter", _chat("qwen3-vl-30b"))
    assert resp.status == 500 and b"engine down" in resp.read()
    run.app.config.token_file.write_text("wrong")
    # The session key follows the token; this test is about the api call.
    run.cookie = f"gb_session={run.app.sessions.mint()}"
    _Fake.llm_status = 200
    resp = run.request("POST", "/api/llm?target=openrouter", _chat("qwen3-vl-30b"))
    assert resp.status == 502
    resp.read()
    run.app.config.api_url = "http://127.0.0.1:9"
    run.app._models_at = 0.0
    resp = run.request("POST", "/api/llm?target=openrouter", _chat("qwen3-vl-30b"))
    assert resp.status == 503
    resp.read()


def test_llm_passes_only_inline_images(run: _Running) -> None:
    frame = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
    content = [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": frame}},
        {"type": "image_url", "image_url": {"url": "http://api:8000/internal/x"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        {"type": "image_url", "image_url": "file:///etc/passwd"},
        {"type": "image_url", "image_url": {"url": "data:text/html;base64,PGI+"}},
        {"type": "image_url", "image_url": {"url": frame + "\nhttp://x"}},
        {"type": "image_url"},
        {"type": "video_url", "video_url": {"url": "http://x/v.mp4"}},
        {"type": "audio_url", "audio_url": {"url": "http://x/a.wav"}},
        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        {"type": "file", "file": {"file_id": "x"}},
        {"type": "text", "text": {"url": "http://x"}},
        "a bare string part",
    ]
    body = json.dumps(
        {"model": "qwen3-vl-30b", "messages": [{"role": "user", "content": content}, "odd"]}
    ).encode()
    assert run.request("POST", "/api/llm?target=openrouter", body).status == 200
    call = [s for s in _Fake.seen if s["path"].endswith("/chat/completions")][-1]
    sent = json.loads(call["body"])["messages"]
    assert sent[0]["content"] == [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": frame}},
    ]
    assert sent[1] == "odd"


def test_the_doors_are_not_queued_behind_brain_calls(run: _Running, gb: types.ModuleType) -> None:
    # Every streaming worker held, as by eight slow brain calls: redeem and verify still answer.
    release = threading.Event()
    held = [run.app._streams.submit(release.wait, 10) for _ in range(gb.STREAM_WORKERS)]
    try:
        start = time.monotonic()
        resp = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", cookie=False)
        assert resp.status == 200
        resp.read()
        phone = _phone(run)
        phone.message()
        chip = _chip(run)
        assert phone.message() == {"t": "status", "awake": 1}
        assert time.monotonic() - start < 5
        chip.shut()
        phone.shut()
    finally:
        release.set()
        for future in held:
            future.result(5)


def test_llm_refuses_a_body_that_is_not_a_chat(run: _Running) -> None:
    for body in (b"nope", b"[]", b'{"model":"x"}'):
        assert run.request("POST", "/api/llm?target=openrouter", body).status == 400


def _multipart(fields: dict[str, str], audio: bytes) -> tuple[bytes, str]:
    boundary = "----browserboundary7MA4YWxkTrZu0gW"
    out = b""
    for key, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += value.encode() + b"\r\n"
    out += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.webm"\r\n'
        "Content-Type: audio/webm;codecs=opus\r\n\r\n"
    ).encode()
    out += audio + f"\r\n--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


@pytest.mark.parametrize("target", ["groq-audio", "deepinfra-audio"])
def test_stt_rewrites_the_upload_for_whisper(
    run: _Running, gb: types.ModuleType, target: str
) -> None:
    _Fake.stt_reply = json.dumps(
        {"text": " hello ", "language": "en", "segments": [{"text": "hello", "start": 0}]}
    ).encode()
    audio = b"\x1aE\xdf\xa3" + bytes(range(256)) * 40 + b"\r\n--not-a-boundary"
    body, ctype = _multipart(
        {
            "model": "whisper-large-v3-turbo",
            "response_format": "verbose_json",
            "temperature": "0",
            "language": "en",
        },
        audio,
    )
    resp = run.request("POST", f"/api/llm?target={target}", body, {"Content-Type": ctype})
    assert resp.status == 200
    got = json.loads(resp.read())
    assert got["text"] == "hello" and got["language"] == "en"
    assert got["segments"][0]["no_speech_prob"] == 0.0
    assert got["segments"][0]["avg_logprob"] == 0.0
    call = [s for s in _Fake.seen if s["path"] == "/v1/audio/transcriptions"][-1]
    parts = {p.name: p for p in gb.parse_multipart(call["body"], call["ctype"])}
    assert parts["model"].data == b"whisper"
    assert parts["response_format"].data == b"verbose_json"
    assert parts["language"].data == b"en"
    assert parts["file"].data == audio
    assert parts["file"].filename == "speech.webm"
    # A webm states no length, so whisper keeps its full window, said out loud (0).
    assert parts["audio_ctx"].data == b"0"


def _wav(seconds: float, rate: int = 16000) -> bytes:
    pcm = b"\0\0" * int(seconds * rate)
    fmt = (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + rate.to_bytes(4, "little")
    fmt += (rate * 2).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    return (
        b"RIFF"
        + (36 + len(pcm)).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + len(fmt).to_bytes(4, "little")
        + fmt
        + b"data"
        + len(pcm).to_bytes(4, "little")
        + pcm
    )


def test_a_short_wav_buys_whisper_a_window_its_own_size(
    run: _Running, gb: types.ModuleType
) -> None:
    """Whisper pads every clip to 30 s; a WAV states its length, so the encoder window is
    sized to it, as the room panels' is."""
    _Fake.stt_reply = json.dumps({"text": "hi"}).encode()
    body, ctype = _multipart({"model": "whisper-large-v3-turbo"}, _wav(3.0))
    resp = run.request("POST", "/api/llm?target=groq-audio", body, {"Content-Type": ctype})
    assert resp.status == 200
    call = [s for s in _Fake.seen if s["path"] == "/v1/audio/transcriptions"][-1]
    parts = {p.name: p for p in gb.parse_multipart(call["body"], call["ctype"])}
    assert parts["audio_ctx"].data == b"225"
    assert gb.audio_window(_wav(1.0)) == 160  # the floor
    assert gb.audio_window(_wav(29.0)) == 1500  # the full window, never more
    assert gb.audio_window(_wav(31.0)) is None
    assert gb.wav_seconds(b"RIFF\0\0\0\0WAVEjunk") is None
    # A streamed WAV's placeholder size reads as the bytes actually present.
    streamed = bytearray(_wav(2.0))
    streamed[40:44] = (0xFFFFFFFF).to_bytes(4, "little")
    assert gb.wav_seconds(bytes(streamed)) == 2.0


def test_stt_without_audio_or_with_whisper_down(run: _Running) -> None:
    body, ctype = _multipart({"model": "x"}, b"")
    resp = run.request("POST", "/api/llm?target=groq-audio", body, {"Content-Type": ctype})
    assert resp.status == 400
    resp.read()
    run.app.config.stt_url = "http://127.0.0.1:9/v1"
    body, ctype = _multipart({}, b"audio")
    resp = run.request("POST", "/api/llm?target=groq-audio", body, {"Content-Type": ctype})
    assert resp.status == 503  # never 429, which upstream reads as a spent allowance
    # The creature says why its ears are down, not one line for every failure.
    assert json.loads(resp.read()) == {"error": "the box's speech service isn't running"}


def test_stt_failures_say_which(gb: types.ModuleType) -> None:
    why = gb._stt_failure
    assert why(gb.Upstream(503, b"", "", "timeout")) == "the box's speech model took over 60 s"
    assert why(gb.Upstream(500, b"", "")) == "the box's speech model failed (HTTP 500)"
    assert why(gb.Upstream(503, b"", "", "broken")) == "the box's speech model sent a broken answer"


def test_a_slow_call_is_told_from_a_dead_one(gb: types.ModuleType) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/v1/audio/transcriptions"
        assert gb.http_call(url, b"x", None, "POST", 0.2).why == "timeout"
    finally:
        listener.close()
    assert gb.http_call("http://127.0.0.1:9/x", None, None, "GET", 1.0).why == "down"


def test_stt_normalisation_fills_what_upstream_reads(gb: types.ModuleType) -> None:
    norm = gb.normalise_transcript
    assert norm(b"plain words\n") == {"text": "plain words", "segments": []}
    assert norm(b'"a bare string"') == {"text": "a bare string", "segments": []}
    assert norm(b"[1,2]") == {"text": "", "segments": []}
    out = norm(
        json.dumps(
            {
                "segments": [
                    {
                        "text": " one",
                        "start": 0.0,
                        "end": 1.5,
                        "no_speech_prob": 0.9,
                        "avg_logprob": -0.4,
                        "compression_ratio": 1.2,
                    },
                    {"text": "two ", "words": [{"probability": 0.5}, {"probability": 0.5}]},
                    {"text": "three", "no_speech_prob": 7, "avg_logprob": "bad"},
                    "junk",
                ],
                "duration": 3,
            }
        ).encode()
    )
    assert out["text"] == "one two three"
    one, two, three = out["segments"]
    assert (one["no_speech_prob"], one["avg_logprob"], one["compression_ratio"]) == (0.9, -0.4, 1.2)
    assert two["avg_logprob"] == pytest.approx(-0.6931, abs=1e-3)
    assert two["no_speech_prob"] == 0.0
    assert three["no_speech_prob"] == 1.0 and three["avg_logprob"] == 0.0
    assert out["duration"] == 3.0


def test_multipart_parsing_edges(gb: types.ModuleType) -> None:
    assert gb.parse_multipart(b"x", "application/json") == []
    assert gb.parse_multipart(b"x", "multipart/form-data") == []
    body = b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\n1\r\n--b\r\nnohead--b--\r\n'
    parts = gb.parse_multipart(body, 'multipart/form-data; boundary="b"')
    assert [(p.name, p.data) for p in parts] == [("a", b"1")]


def test_fish_tts_speaks_through_kokoro(run: _Running) -> None:
    body = json.dumps(
        {"text": "hello there", "voice": "kokoro-af_heart", "model": "s2", "speed": 1.25}
    ).encode()
    resp = run.request("POST", "/api/fish-tts", body, {"x-fish-key": "ignored"})
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "audio/wav"
    query = urllib.parse.parse_qs(resp.read()[4:].decode())
    assert query == {"text": ["hello there"], "voice": ["kokoro-af_heart"], "speed": ["1.250"]}
    for voice in ("", "fish-voice-123", None):
        body = json.dumps({"text": "hi", "voice": voice, "speed": 9, "pitch": -2}).encode()
        query = urllib.parse.parse_qs(
            run.request("POST", "/api/fish-tts", body).read()[4:].decode()
        )
        assert "voice" not in query
        assert query["speed"] == ["2.000"] and query["pitch"] == ["-2.000"]


def test_fish_tts_down_is_503_never_401_or_402(run: _Running) -> None:
    for status in (401, 402, 503):
        _Fake.tts_status = status
        resp = run.request("POST", "/api/fish-tts", b'{"text":"hi"}')
        assert resp.status == 503
        resp.read()
    for body in (b"{}", b'{"text":"  "}', b"nope"):
        assert run.request("POST", "/api/fish-tts", body).status == 400


# --- HTTP framing -----------------------------------------------------------------------


def _raw(run: _Running, data: bytes) -> bytes:
    raw = socket.create_connection(("127.0.0.1", run.port), timeout=5)
    with raw:
        raw.sendall(data)
        out = b""
        while chunk := raw.recv(65536):
            out += chunk
    return out


def test_a_chunked_request_body_is_read(run: _Running) -> None:
    body = b'{"text":"chunky"}'
    data = (
        f"POST /api/fish-tts HTTP/1.1\r\nHost: x\r\nCookie: {run.cookie}\r\n"
        "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    ).encode()
    data += b"5;ext=1\r\n" + body[:5] + b"\r\n" + b"%x\r\n" % (len(body) - 5) + body[5:]
    data += b"\r\n0\r\nTrailer: x\r\n\r\n"
    assert _raw(run, data).startswith(b"HTTP/1.1 200")


@pytest.mark.parametrize(
    ("request_bytes", "status"),
    [
        (
            b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n",
            400,
        ),
        (b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n", 400),
        (b"GET / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip\r\n\r\n", 501),
        (b"GET / HTTP/1.1\r\nHost : x\r\n\r\n", 400),
        (b"GET / HTTP/1.1\r\nHost: x\r\n folded\r\n\r\n", 400),
        (b"GET / HTTP/2.0\r\n\r\n", 400),
        (b"GET http://evil/ HTTP/1.1\r\n\r\n", 400),
        (b"GET / HTTP/1.1\r\nX: " + b"a" * 20000 + b"\r\n\r\n", 431),
        (b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 9999999999\r\n\r\n", 413),
        (b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999999\r\n\r\n", 400),
        (b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n", 400),
        (b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: +5\r\n\r\n", 400),
        (
            b"POST / HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length:  5 \r\n\r\nabcde",
            405,
        ),
        (b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n", 400),
        (b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0x5\r\n", 400),
        (b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n-5\r\n", 400),
        (
            b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n123456789\r\n",
            400,
        ),
    ],
)
def test_malformed_requests_are_refused(run: _Running, request_bytes: bytes, status: int) -> None:
    authed = request_bytes.replace(
        b"Host: x\r\n", b"Host: x\r\nCookie: " + run.cookie.encode() + b"\r\n", 1
    )
    out = _raw(run, authed)
    assert out.startswith(f"HTTP/1.1 {status}".encode()), out[:80]
    assert b"Content-Security-Policy" in out


def test_an_oversized_body_is_refused_before_100_continue(run: _Running) -> None:
    out = _raw(
        run,
        f"POST /api/ev HTTP/1.1\r\nHost: x\r\nCookie: {run.cookie}\r\n"
        "Expect: 100-continue\r\nContent-Length: 999999999\r\n\r\n".encode(),
    )
    assert out.startswith(b"HTTP/1.1 413"), out[:80]
    assert b"100 Continue" not in out


@pytest.mark.parametrize(
    "head",
    [
        "POST /api/llm?target=openrouter HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000\r\n",
        "POST /api/ev HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n",
        "POST /api/ev HTTP/1.1\r\nHost: x\r\nCookie: gb_session=1.2.3\r\nContent-Length: 9\r\n",
        "GET /open?ticket=good-ticket&id=gb-abc123 HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n",
    ],
)
def test_without_a_session_no_body_is_read(run: _Running, head: str) -> None:
    # Only the head is sent: the answer must come without waiting for (or reading) the body.
    raw = socket.create_connection(("127.0.0.1", run.port), timeout=3)
    with raw:
        raw.sendall((head + "\r\n").encode())
        out = b""
        while chunk := raw.recv(65536):
            out += chunk
    assert out.startswith(b"HTTP/1.1 401"), out[:80]
    assert b"Connection: close" in out
    assert not any(c["path"].startswith("/internal/growbot/ticket") for c in _Fake.seen)


def test_the_connection_cap(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "MAX_CONNECTIONS", 1)
    held = socket.create_connection(("127.0.0.1", run.port), timeout=3)
    try:
        deadline = time.monotonic() + 3
        while run.app.connections < 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        extra = socket.create_connection(("127.0.0.1", run.port), timeout=3)
        with extra:
            assert extra.recv(64).startswith(b"HTTP/1.1 503")
    finally:
        held.close()


def test_keep_alive_serves_several_requests_on_one_connection(run: _Running) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", run.port, timeout=5)
    for _ in range(3):
        conn.request("GET", "/v3/next.html", headers={"Cookie": run.cookie})
        resp = conn.getresponse()
        assert resp.status == 200 and resp.read() == b"<p>creature</p>"
    conn.request(
        "POST", "/api/ev", body=b"{}", headers={"Cookie": run.cookie, "Expect": "100-continue"}
    )
    assert conn.getresponse().status == 204


# --- WebSocket client (raw frames) ------------------------------------------------------


class _Ws:
    def __init__(
        self,
        run: _Running,
        path: str,
        *,
        cookie: bool | str,
        origin: str | None = None,
        extra: tuple[str, ...] = (),
    ):
        self.sock = socket.create_connection(("127.0.0.1", run.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: {run.host}",
            "Upgrade: websocket",
            "Connection: keep-alive, Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "Sec-WebSocket-Extensions: permessage-deflate",
        ]
        if cookie:
            headers.append(f"Cookie: {cookie if isinstance(cookie, str) else run.cookie}")
            headers.append(f"Origin: {origin or run.origin}")
        elif origin:
            headers.append(f"Origin: {origin}")
        headers.extend(extra)
        self.sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                break
            head += chunk
        self.head = head.decode("latin-1")
        self.key = key
        self.buf = b""

    @property
    def status(self) -> int:
        return int(self.head.split(" ")[1])

    def send(self, opcode: int, payload: bytes, *, fin: bool = True, mask: bool = True) -> None:
        gb = sys.modules["growbot_serve_under_test"]
        self.sock.sendall(
            gb.ws_frame(opcode, payload, fin=fin, mask=os.urandom(4) if mask else None)
        )

    def text(self, obj: object) -> None:
        self.send(0x1, json.dumps(obj).encode())

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frame(self) -> tuple[int, bytes]:
        b0, b1 = self._read(2)
        assert not b1 & 0x80, "the server must not mask"
        n = b1 & 0x7F
        if n == 126:
            n = int.from_bytes(self._read(2), "big")
        elif n == 127:
            n = int.from_bytes(self._read(8), "big")
        return b0 & 0x0F, self._read(n)

    def message(self, *, skip_pings: bool = True) -> dict[str, Any]:
        while True:
            opcode, payload = self.frame()
            if opcode == 0x9 and skip_pings:
                self.send(0xA, payload)  # a browser answers the relay's pings
                continue
            assert opcode == 0x1, (opcode, payload)
            return json.loads(payload)

    def close_code(self) -> int:
        while True:
            opcode, payload = self.frame()
            if opcode == 0x8:
                return int.from_bytes(payload[:2], "big") if payload else 1005

    def closed(self) -> bool:
        try:
            while True:
                opcode, _ = self.frame()
                if opcode == 0x8:
                    return True
        except (ConnectionError, OSError):
            return True

    def shut(self) -> None:
        self.sock.close()


def _chip(run: _Running, key: str = "right-key", caps: list[str] | None = None) -> _Ws:
    chip = _Ws(run, "/d/gb-abc123", cookie=False)
    assert chip.status == 101
    chip.text({"t": "hello", "id": "gb-abc123", "key": key, "caps": caps or ["hb"]})
    return chip


def _phone(run: _Running, path: str = "/d/gb-abc123") -> _Ws:
    phone = _Ws(run, path, cookie=True)
    assert phone.status == 101, phone.head
    return phone


def test_the_handshake_answers_rfc6455(run: _Running, gb: types.ModuleType) -> None:
    import hashlib

    phone = _phone(run)
    expected = base64.b64encode(
        hashlib.sha1((phone.key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    assert f"Sec-WebSocket-Accept: {expected}" in phone.head
    assert "Sec-WebSocket-Extensions" not in phone.head
    assert "Content-Security-Policy" in phone.head
    assert phone.message() == {"t": "status", "awake": 0}
    phone.shut()


@pytest.mark.parametrize(
    "header",
    ["Upgrade: h2c", "Sec-WebSocket-Version: 8", "Sec-WebSocket-Key: short", "Connection: close"],
)
def test_a_bad_handshake_is_refused(run: _Running, header: str) -> None:
    name = header.split(":")[0]
    lines = {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": base64.b64encode(b"k" * 16).decode(),
        "Sec-WebSocket-Version": "13",
    }
    lines[name] = header.split(": ", 1)[1]
    request = f"GET /d/gb-abc123 HTTP/1.1\r\nHost: {run.host}\r\nCookie: {run.cookie}\r\n"
    request += f"Origin: {run.origin}\r\n"
    request += "".join(f"{k}: {v}\r\n" for k, v in lines.items()) + "\r\n"
    out = _raw(run, request.encode())
    assert not out.startswith(b"HTTP/1.1 101"), out[:80]


def test_through_the_tunnel_only_a_signed_in_phone_reaches_the_relay(run: _Running) -> None:
    # A robot dials in on the LAN. From the internet, a chip-shaped socket (no Origin, and a
    # junk cookie that gets past Caddy's sniffing) would hold the robots' budget or guess keys.
    # Caddy's away site marks every request; no address lookup is involved.
    away = ("Cookie: gb_session=x", "X-Growbot-Away: 1")
    assert _Ws(run, "/d/gb-abc123", cookie=False, extra=away).status == 403
    assert _Ws(run, "/d/gb-abc123", cookie=False, extra=away[1:]).status == 403
    # The same socket from the LAN is a robot dialing in.
    assert _Ws(run, "/d/gb-abc123", cookie=False).status == 101
    # And a signed-in phone through the tunnel is let in.
    phone = _Ws(run, "/d/gb-abc123", cookie=True, extra=away[1:])
    assert phone.status == 101


def test_the_relay_refuses_bad_codes_and_foreign_origins(run: _Running) -> None:
    assert _Ws(run, "/d/..%2fx", cookie=True).status == 404
    assert _Ws(run, "/d/gb-abc123", cookie=True, origin="https://jbrain.local").status == 403
    assert _Ws(run, "/d/gb-abc123-agent", cookie=False).status == 401


def test_a_chip_with_the_right_key_is_relayed_both_ways(run: _Running) -> None:
    phone = _phone(run)
    assert phone.message() == {"t": "status", "awake": 0}
    chip = _chip(run)
    assert phone.message() == {"t": "status", "awake": 1}
    assert phone.message() == {"t": "hello", "id": "gb-abc123", "caps": ["hb"]}
    verify = [s for s in _Fake.seen if s["path"] == "/internal/growbot/robot/verify"][-1]
    assert verify["auth"] == f"Bearer {_TOKEN}"
    phone.text({"t": "pose", "lr": "90,90"})
    assert chip.message() == {"t": "pose", "lr": "90,90"}
    chip.text({"t": "ack", "rid": 3, "ok": 1, "queued_ms": 0})
    assert phone.message() == {"t": "ack", "rid": 3, "ok": 1, "queued_ms": 0}
    chip.send(0x1, b'{"t":"hb"}')
    assert chip.message() == {"t": "hb_ack"}
    # A repeated hello reaches phones without the key.
    chip.text({"t": "hello", "id": "gb-abc123", "key": "right-key", "boot": {"up_ms": 5}})
    assert phone.message() == {"t": "hello", "id": "gb-abc123", "boot": {"up_ms": 5}}
    chip.text({"t": "ack", "rid": 4})
    assert phone.message() == {"t": "ack", "rid": 4}
    late = _phone(run)
    assert late.message() == {"t": "status", "awake": 1}
    chip.shut()
    assert phone.message() == {"t": "status", "awake": 0}
    assert late.message() == {"t": "status", "awake": 0}
    phone.shut()
    late.shut()


@pytest.mark.parametrize(
    "hello",
    [
        {"t": "hello", "id": "gb-abc123", "key": "wrong-key"},
        {"t": "hello", "id": "gb-abc123"},
        {"t": "hello", "id": "gb-other", "key": "right-key"},
        {"t": "attach", "id": "gb-abc123", "code": "gb-abc123"},
        {"t": "hello", "id": "gb-abc123", "key": "k" * 300},
    ],
)
def test_a_chip_without_a_good_key_is_dropped(run: _Running, hello: dict[str, Any]) -> None:
    phone = _phone(run)
    assert phone.message() == {"t": "status", "awake": 0}
    chip = _Ws(run, "/d/gb-abc123", cookie=False)
    chip.text(hello)
    assert chip.close_code() == 1008
    phone.text({"t": "pose", "lr": "1,1"})
    phone.send(0x9, b"still here")
    assert phone.frame() == (0xA, b"still here")  # no status 1 arrived in between
    phone.shut()


def test_rejected_and_departed_sockets_leave_no_channel(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "FIRST_FRAME_TIMEOUT_S", 0.3)
    for code in ("gb-random1", "gb-random2"):
        chip = _Ws(run, f"/d/{code}", cookie=False)
        chip.text({"t": "hello", "id": code, "key": "wrong"})
        assert chip.close_code() == 1008
    silent = _Ws(run, "/d/gb-random3", cookie=False)
    assert silent.close_code() == 1008
    agent = _phone(run, "/d/gb-abc123-agent")
    agent.send(0x2, b"x")
    assert agent.close_code() == 1008
    phone = _phone(run, "/d/gb-abc123")
    phone.message()
    phone.send(0x8, b"\x03\xe8")
    assert phone.close_code() == 1000
    chip = _chip(run)
    chip.shut()
    deadline = time.monotonic() + 3
    while run.app.channels or run.app.unauth_sockets or run.app.sockets:
        assert time.monotonic() < deadline, (run.app.channels, run.app.unauth_sockets)
        time.sleep(0.02)


def test_cookieless_sockets_have_their_own_small_budget(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "MAX_UNAUTH_SOCKETS", 2)
    pending = [_Ws(run, "/d/gb-abc123", cookie=False) for _ in range(2)]
    assert all(p.status == 101 for p in pending)
    assert _Ws(run, "/d/gb-abc123", cookie=False).status == 503
    # Phones are not counted against it.
    phone = _phone(run)
    phone.message()
    # A robot that proves itself leaves the anonymous budget.
    pending[0].text({"t": "hello", "id": "gb-abc123", "key": "right-key"})
    assert phone.message() == {"t": "status", "awake": 1}
    assert _Ws(run, "/d/gb-abc123", cookie=False).status == 101
    for p in pending:
        p.shut()
    phone.shut()


def test_a_burst_cannot_overshoot_the_socket_budget(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "MAX_UNAUTH_SOCKETS", 2)
    real = run.app.client_ip

    async def slow(req: Any) -> str:
        await asyncio.sleep(0.3)  # every upgrade in the burst is past the check by now
        return await real(req)

    monkeypatch.setattr(run.app, "client_ip", slow)
    results: list[int] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(_Ws(run, "/d/gb-abc123", cookie=False).status)
        )
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(results) == [101, 101, 503, 503, 503]
    assert run.app.unauth_sockets <= 2


def test_failed_hellos_back_off(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "BACKOFF_LIMIT", 2)
    for _ in range(2):
        chip = _Ws(run, "/d/gb-abc123", cookie=False)
        chip.text({"t": "hello", "id": "gb-abc123", "key": "guess"})
        assert chip.close_code() == 1008
    verifies = len([s for s in _Fake.seen if s["path"].endswith("/robot/verify")])
    assert _Ws(run, "/d/gb-abc123", cookie=False).status == 429
    assert len([s for s in _Fake.seen if s["path"].endswith("/robot/verify")]) == verifies
    # A phone with a session is not the guesser.
    assert _phone(run).status == 101


def test_an_authed_socket_without_an_origin_is_refused(run: _Running) -> None:
    raw = socket.create_connection(("127.0.0.1", run.port), timeout=3)
    key = base64.b64encode(b"k" * 16).decode()
    raw.sendall(
        f"GET /d/gb-abc123 HTTP/1.1\r\nHost: {run.host}\r\nCookie: {run.cookie}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    assert raw.recv(64).startswith(b"HTTP/1.1 403")
    raw.close()


def test_quiet_ms_counts_the_chips_frames_not_its_pongs(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "ALIVE_EVERY_S", 0.1)
    phone = _phone(run)
    phone.message()
    chip = _chip(run)
    deadline = time.monotonic() + 3
    while True:
        chip.send(0xA, b"")  # the link is alive, but the chip says nothing
        msg = phone.message()
        if msg.get("t") == "alive" and msg["quiet_ms"] >= 400:
            break
        assert time.monotonic() < deadline, msg
        time.sleep(0.05)
    chip.send(0x1, b'{"t":"hb"}')
    assert chip.message() == {"t": "hb_ack"}
    while (msg := phone.message()).get("t") != "alive":
        pass
    assert msg["quiet_ms"] < 400
    chip.shut()
    phone.shut()


def test_a_silent_chip_is_dropped(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "FIRST_FRAME_TIMEOUT_S", 0.3)
    chip = _Ws(run, "/d/gb-abc123", cookie=False)
    assert chip.close_code() == 1008


def test_alive_and_the_dead_chip_timer(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "ALIVE_EVERY_S", 0.2)
    monkeypatch.setattr(gb, "WS_PING_EVERY_S", 0.2)
    monkeypatch.setattr(gb, "WS_DEAD_AFTER_S", 0.7)
    phone = _phone(run)
    assert phone.message() == {"t": "status", "awake": 0}
    chip = _chip(run)
    assert phone.message() == {"t": "status", "awake": 1}
    assert phone.message() == {"t": "hello", "id": "gb-abc123", "caps": ["hb"]}
    alive = phone.message()
    assert alive["t"] == "alive" and isinstance(alive["quiet_ms"], int)
    # The chip never answers the relay's pings: silent past the limit, it is dropped.
    deadline = time.monotonic() + 5
    while True:
        msg = phone.message()
        if msg == {"t": "status", "awake": 0}:
            break
        assert time.monotonic() < deadline
    assert chip.closed()
    phone.shut()


def test_a_new_chip_replaces_the_old(run: _Running) -> None:
    phone = _phone(run)
    phone.message()
    first = _chip(run)
    assert phone.message() == {"t": "status", "awake": 1}
    assert phone.message() == {"t": "hello", "id": "gb-abc123", "caps": ["hb"]}
    second = _chip(run)
    assert first.close_code() == 1000
    assert phone.message() == {"t": "status", "awake": 1}
    assert phone.message() == {"t": "hello", "id": "gb-abc123", "caps": ["hb"]}
    phone.text({"t": "stop", "rid": 1})
    assert second.message() == {"t": "stop", "rid": 1}
    second.shut()
    phone.shut()


def test_the_agent_channel_bridges_agents_and_the_page(run: _Running) -> None:
    page = _phone(run, "/d/gb-abc123-agent")
    page.text({"t": "hello", "id": "gb-abc123-agent"})
    agent = _phone(run, "/d/gb-abc123-agent")
    agent.text({"t": "attach", "id": "gb-abc123-agent", "code": "gb-abc123-agent"})
    assert agent.message() == {"t": "status", "awake": 1}
    assert page.message() == {"t": "attach", "id": "gb-abc123-agent", "code": "gb-abc123-agent"}
    agent.text({"t": "hi"})
    assert page.message() == {"t": "hi"}
    page.text({"t": "menu", "menu": []})
    assert agent.message() == {"t": "menu", "menu": []}
    # The side channel never meets the robot's main channel.
    main = _phone(run)
    assert main.message() == {"t": "status", "awake": 0}
    page.shut()
    assert agent.message() == {"t": "status", "awake": 0}
    agent.shut()
    main.shut()


def test_the_agent_channel_wants_a_first_frame(run: _Running, gb: types.ModuleType) -> None:
    sock = _phone(run, "/d/gb-abc123-agent")
    sock.send(0x2, b"\x00")
    assert sock.close_code() == 1008


def test_fragmented_messages_and_control_frames_between_them(run: _Running) -> None:
    phone = _phone(run)
    phone.message()
    chip = _chip(run)
    phone.message()
    phone.message()
    phone.send(0x1, b'{"t":"po', fin=False)
    phone.send(0x9, b"mid")
    assert phone.frame() == (0xA, b"mid")
    phone.send(0xA, b"unsolicited")
    phone.send(0x0, b'se","lr":', fin=False)
    phone.send(0x0, b'"5,6"}')
    assert chip.message() == {"t": "pose", "lr": "5,6"}
    big = json.dumps({"t": "act", "steps": [{"l": 1, "r": 2, "ms": 3}] * 3000}).encode()
    assert len(big) > 65535
    phone.send(0x1, big)
    _, payload = chip.frame()
    assert payload == big
    phone.send(0x2, b"\x01binary is not relayed")
    phone.text({"t": "stop"})
    assert chip.message() == {"t": "stop"}
    chip.shut()
    phone.shut()


@pytest.mark.parametrize(
    ("frames", "code"),
    [
        ([(0x1, b"x", True, False)], 1002),  # unmasked client frame
        ([(0x0, b"x", True, True)], 1002),  # continuation with nothing to continue
        ([(0x1, b"a", False, True), (0x1, b"b", True, True)], 1002),  # new message mid-fragment
        ([(0x9, b"x", False, True)], 1002),  # fragmented control frame
        ([(0x9, b"x" * 126, True, True)], 1002),  # oversized control frame
        ([(0x3, b"", True, True)], 1002),  # reserved opcode
        ([(0x1, b"\xff\xfe", True, True)], 1007),  # text that is not UTF-8
        ([(0x8, b"\x03", True, True)], 1002),  # one-byte close payload
        ([(0x8, (999).to_bytes(2, "big"), True, True)], 1002),  # invalid close code
        ([(0x8, (1001).to_bytes(2, "big"), True, True)], 1001),  # echoed close
        ([(0x8, (1012).to_bytes(2, "big"), True, True)], 1012),
        ([(0x8, (1014).to_bytes(2, "big"), True, True)], 1014),
        ([(0x8, (4000).to_bytes(2, "big") + b"bye", True, True)], 4000),
        ([(0x8, (1015).to_bytes(2, "big"), True, True)], 1002),
        ([(0x8, (5000).to_bytes(2, "big"), True, True)], 1002),
        ([(0x8, (1000).to_bytes(2, "big") + b"\xff", True, True)], 1007),  # reason not UTF-8
        ([(0x8, b"", True, True)], 1000),
    ],
)
def test_protocol_violations_close_with_the_rfc_code(
    run: _Running, frames: list[tuple[int, bytes, bool, bool]], code: int
) -> None:
    phone = _phone(run)
    phone.message()
    for opcode, payload, fin, mask in frames:
        phone.send(opcode, payload, fin=fin, mask=mask)
    assert phone.close_code() == code
    phone.shut()


def test_rsv_bits_and_oversized_messages_are_refused(run: _Running, gb: types.ModuleType) -> None:
    phone = _phone(run)
    phone.message()
    phone.sock.sendall(bytes([0xC1, 0x80]) + os.urandom(4))
    assert phone.close_code() == 1002
    phone.shut()
    phone = _phone(run)
    phone.message()
    phone.sock.sendall(bytes([0x81, 0xFF]) + (gb.MAX_WS_MESSAGE + 1).to_bytes(8, "big"))
    assert phone.close_code() == 1009
    phone.shut()
    phone = _phone(run)
    phone.message()
    piece = b"a" * (gb.MAX_WS_MESSAGE // 2 + 1)
    phone.send(0x1, piece, fin=False)
    phone.send(0x0, piece, fin=False)
    assert phone.close_code() == 1009
    phone.shut()


def test_frame_encoding_lengths(gb: types.ModuleType) -> None:
    assert gb.ws_frame(0x1, b"a")[:2] == b"\x81\x01"
    assert gb.ws_frame(0x1, b"a" * 200)[:4] == b"\x81\x7e\x00\xc8"
    assert gb.ws_frame(0x2, b"a" * 70000)[:2] == b"\x82\x7f"
    masked = gb.ws_frame(0x1, b"hello", mask=b"\x01\x02\x03\x04")
    assert masked[1] == 0x85 and gb._unmask(masked[6:], b"\x01\x02\x03\x04") == b"hello"


def test_too_many_phones_on_one_code(run: _Running, gb: types.ModuleType) -> None:
    phones = [_phone(run) for _ in range(gb.MAX_PEERS_PER_CHANNEL)]
    for phone in phones:
        phone.message()
    extra = _phone(run)
    assert extra.close_code() == 1013
    for phone in phones:
        phone.shut()


# --- deploy wiring ----------------------------------------------------------------------


def test_the_compose_service_runs_the_bind_mounted_server() -> None:
    compose = yaml.safe_load((_DEPLOY / "docker-compose.yml").read_text())
    svc = compose["services"]["growbot"]
    assert "profiles" not in svc
    assert svc["command"] == ["python3", "/app/server/serve.py"]
    assert "./src/growbot:/app:ro" in svc["volumes"]
    assert "growbot_secrets:/run/jbrain-growbot:ro" in svc["volumes"]
    assert "growbot_secrets" in compose["volumes"]
    assert set(svc["networks"]) == {"edge", "growbot_lane"}
    assert compose["networks"]["growbot_lane"] == {"internal": True}
    joined = {n for n, s in compose["services"].items() if "growbot_lane" in s.get("networks", [])}
    assert joined == {"growbot", "api", "tts-stt"}
    # Only the robots' SNTP is published (UDP); the web side is reached through Caddy alone.
    assert svc["ports"] == ["${GROWBOT_NTP_BIND:-0.0.0.0}:8123:8123/udp"]
    assert svc["environment"]["GROWBOT_NTP_PORT"] == "8123"
    env = svc["environment"]
    assert env["GROWBOT_SITE"] == "/app/site"
    assert env["GROWBOT_TOKEN_FILE"] == "/run/jbrain-growbot/growbot.token"
    assert env["GROWBOT_API_URL"] == "http://api:8000"
    assert env["GROWBOT_STT_URL"] == "http://tts-stt:8080/v1"
    assert svc["build"]["dockerfile"] == "deploy/Dockerfile.growbot"
    dockerfile = (_DEPLOY / "Dockerfile.growbot").read_text()
    assert "FROM python:3.12-slim" in dockerfile
    assert "pip" not in dockerfile.split("FROM", 1)[1]


def test_open_only_counts_a_navigation_and_keeps_cross_site_apart(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "BACKOFF_LIMIT", 2)
    # An <img> or fetch from any page: refused before the api, never counted.
    # A frame is a navigation too, but only the top level is the PWA's link: a same-site page
    # framing /open must neither spend the owner's tries nor plant a session.
    for dest, mode in [("image", "no-cors"), ("empty", "cors"), ("iframe", "navigate")] * 2:
        resp = run.request(
            "GET",
            "/open?ticket=bad&id=gb-abc123",
            headers={
                "Sec-Fetch-Dest": dest,
                "Sec-Fetch-Mode": mode,
                "Sec-Fetch-Site": "same-site",
            },
            cookie=False,
        )
        assert resp.status == 403
        resp.read()
    # A cross-site navigation spends nothing: it comes back same-site (with the Strict
    # cookie) marked `hop`, and those tries spend their own bucket only.
    cross = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "cross-site"}
    redeems = len([s for s in _Fake.seen if s["path"].endswith("/ticket/redeem")])
    resp = run.request("GET", "/open?ticket=bad&id=gb-abc123", headers=cross, cookie=False)
    assert _landing(resp) == "/open?ticket=bad&hop=1&id=gb-abc123"
    assert resp.getheader("Set-Cookie") is None
    assert len([s for s in _Fake.seen if s["path"].endswith("/ticket/redeem")]) == redeems
    hop = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "same-origin"}
    for _ in range(2):
        resp = run.request("GET", "/open?ticket=bad&hop=1", headers=hop, cookie=False)
        assert resp.status == 401
        resp.read()
    resp = run.request("GET", "/open?ticket=bad&hop=1", headers=hop, cookie=False)
    assert resp.status == 429
    resp.read()
    same = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "same-site"}
    resp = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", headers=same, cookie=False)
    assert resp.status == 200
    resp.read()
    # The installed app's service worker passes the PWA's link on with dest `empty`; it is
    # still a navigation, so it opens.
    worker = {
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }
    resp = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", headers=worker, cookie=False)
    assert resp.status == 200 and resp.getheader("Set-Cookie")
    resp.read()


def test_a_cookieless_socket_from_a_browser_is_refused(run: _Running) -> None:
    verifies = len([s for s in _Fake.seen if s["path"].endswith("/robot/verify")])
    assert _Ws(run, "/d/gb-abc123", cookie=False, origin="https://evil.example").status == 403
    assert _Ws(run, "/d/gb-abc123", cookie=False, origin=run.origin).status == 403
    assert len([s for s in _Fake.seen if s["path"].endswith("/robot/verify")]) == verifies


def test_failed_hellos_do_not_lock_out_the_owners_link(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "BACKOFF_LIMIT", 2)
    for _ in range(2):
        chip = _Ws(run, "/d/gb-abc123", cookie=False)
        chip.text({"t": "hello", "id": "gb-abc123", "key": "guess"})
        assert chip.close_code() == 1008
    resp = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", cookie=False)
    assert resp.status == 200
    resp.read()


def test_open_without_a_robot_opens_the_creature_unpaired(run: _Running) -> None:
    resp = run.request("GET", "/open?ticket=bare-ticket", cookie=False)
    assert resp.getheader("Set-Cookie")
    assert _landing(resp) == "/v3/next.html?mode=legs"


def test_codes_are_held_to_the_apis_shape(gb: types.ModuleType) -> None:
    assert gb.CODE_RE.fullmatch("gb-abc123")
    for code in ("bench-rig", "gb-", "gb-a_b", "x" * 40):
        assert not gb.CODE_RE.fullmatch(code)


# --- the api's /internal routes ------------------------------------------------------------


def _internal(
    run: _Running, method: str, path: str, body: bytes | None = None, token: str | None = _TOKEN
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = token if token.startswith("Basic") else f"Bearer {token}"
    resp = run.request(method, path, body, headers, cookie=False)
    raw = resp.read()
    return resp.status, json.loads(raw) if raw else None


def test_the_api_lists_the_robots_on_the_relay(run: _Running) -> None:
    assert _internal(run, "GET", "/internal/robots") == (200, {"robots": []})
    phone = _phone(run)
    assert phone.message() == {"t": "status", "awake": 0}
    # A phone alone is no robot.
    assert _internal(run, "GET", "/internal/robots") == (200, {"robots": []})
    chip = _chip(run)
    assert phone.message() == {"t": "status", "awake": 1}
    status, body = _internal(run, "GET", "/internal/robots")
    assert status == 200 and [r["code"] for r in body["robots"]] == ["gb-abc123"]
    assert time.strptime(body["robots"][0]["since"], "%Y-%m-%dT%H:%M:%SZ")
    chip.shut()
    phone.shut()


def test_the_creature_page_on_the_side_channel_is_no_robot(run: _Running) -> None:
    page = _phone(run, "/d/gb-abc123-agent")
    page.text({"t": "hello"})
    time.sleep(0.2)
    assert _internal(run, "GET", "/internal/robots") == (200, {"robots": []})
    page.shut()


def test_the_robot_list_says_whether_its_creature_listens(run: _Running) -> None:
    phone = _phone(run)
    assert phone.message() == {"t": "status", "awake": 0}
    chip = _chip(run)
    assert phone.message() == {"t": "status", "awake": 1}
    status, body = _internal(run, "GET", "/internal/robots")
    assert status == 200 and body["robots"][0]["creature"] is False
    page = _phone(run, "/d/gb-abc123-agent")
    page.text({"t": "hello"})
    time.sleep(0.2)
    status, body = _internal(run, "GET", "/internal/robots")
    assert [(r["code"], r["creature"]) for r in body["robots"]] == [("gb-abc123", True)]
    page.shut()
    chip.shut()
    phone.shut()


def _move(run: _Running, body: object) -> tuple[int, Any]:
    return _internal(run, "POST", "/internal/robots/move", json.dumps(body).encode())


def _answering(page: _Ws, reply: dict[str, Any]) -> threading.Thread:
    """The creature page: takes one box_move and answers it with `reply` under its rid."""

    def answer() -> None:
        msg = page.message()
        assert msg["t"] == "box_move"
        page.text({"t": "box_ack", "rid": msg["rid"], **reply})

    t = threading.Thread(target=answer)
    t.start()
    return t


def test_a_move_reaches_the_creature_and_brings_back_its_answer(run: _Running) -> None:
    page = _phone(run, "/d/gb-abc123-agent")
    page.text({"t": "hello"})
    time.sleep(0.2)
    seen: list[dict[str, Any]] = []

    def answer() -> None:
        msg = page.message()
        seen.append(msg)
        page.text({"t": "box_ack", "rid": msg["rid"], "ok": True, "state": "started"})

    t = threading.Thread(target=answer)
    t.start()
    got = _move(run, {"code": "gb-abc123", "move": "forward", "secs": 4})
    t.join(5)
    assert got == (200, {"ok": True, "state": "started", "why": ""})
    assert seen[0]["move"] == "forward" and seen[0]["secs"] == 4
    assert isinstance(seen[0]["rid"], str) and seen[0]["rid"]
    page.shut()


def test_a_refused_move_carries_the_creatures_words(run: _Running) -> None:
    page = _phone(run, "/d/gb-abc123-agent")
    page.text({"t": "hello"})
    time.sleep(0.2)
    why = "no orientation readings from this phone" + "!" * 300
    t = _answering(page, {"ok": "yes", "state": "blocked", "why": why})
    status, body = _move(run, {"code": "gb-abc123", "move": "spin_left"})
    t.join(5)
    # Only a literal true is ok, and its words are capped.
    assert status == 200 and body["ok"] is False and body["state"] == "blocked"
    assert body["why"].startswith("no orientation") and len(body["why"]) == 220
    page.shut()


def test_a_move_with_no_creature_listening_is_409(run: _Running) -> None:
    chip = _chip(run)
    assert _move(run, {"code": "gb-abc123", "move": "stop"}) == (409, {"error": "no_creature"})
    chip.shut()


def test_a_creature_that_never_answers_is_504(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "MOVE_ACK_S", 0.3)
    page = _phone(run, "/d/gb-abc123-agent")
    page.text({"t": "hello"})
    time.sleep(0.2)
    assert _move(run, {"code": "gb-abc123", "move": "wiggle"}) == (504, {"error": "no_answer"})
    assert page.message()["t"] == "box_move"
    page.shut()


def test_a_late_ack_after_the_wait_reaches_no_one(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "MOVE_ACK_S", 0.3)
    page = _phone(run, "/d/gb-abc123-agent")
    page.text({"t": "hello"})
    agent = _phone(run, "/d/gb-abc123-agent")
    agent.text({"t": "attach"})
    assert agent.message() == {"t": "status", "awake": 1}
    assert page.message() == {"t": "attach"}
    assert _move(run, {"code": "gb-abc123", "move": "bow"})[0] == 504
    msg = page.message()
    page.text({"t": "box_ack", "rid": msg["rid"], "ok": True})
    # With nothing waiting it is the page's frame like any other, bridged to its agents.
    assert agent.message()["t"] == "box_ack"
    agent.shut()
    page.shut()


@pytest.mark.parametrize(
    "body",
    [
        {"code": "../x", "move": "stop"},
        {"move": "stop"},
        {"code": "gb-abc123", "move": "dance"},
        {"code": "gb-abc123", "move": 3},
        {"code": "gb-abc123", "move": "forward", "secs": 0.5},
        {"code": "gb-abc123", "move": "forward", "secs": 16},
        {"code": "gb-abc123", "move": "forward", "secs": True},
        {"code": "gb-abc123", "move": "forward", "secs": "4"},
        [],
    ],
)
def test_a_bad_move_is_400(run: _Running, body: object) -> None:
    assert _move(run, body)[0] == 400


def test_a_move_is_post_only_and_needs_the_token(run: _Running) -> None:
    assert _internal(run, "GET", "/internal/robots/move")[0] == 405
    body = b'{"code":"gb-abc123","move":"stop"}'
    assert _internal(run, "POST", "/internal/robots/move", body, token="wrong")[0] == 401


def test_dropping_a_robot_closes_its_socket(run: _Running) -> None:
    phone = _phone(run)
    assert phone.message() == {"t": "status", "awake": 0}
    chip = _chip(run)
    assert phone.message() == {"t": "status", "awake": 1}
    phone.message()  # its hello
    body = json.dumps({"code": "gb-abc123"}).encode()
    assert _internal(run, "POST", "/internal/robots/drop", body) == (200, {"dropped": True})
    assert chip.close_code() == 1008
    assert phone.message() == {"t": "status", "awake": 0}
    assert _internal(run, "POST", "/internal/robots/drop", body) == (200, {"dropped": False})
    assert _internal(run, "POST", "/internal/robots/drop", b'{"code":"../x"}')[0] == 400
    assert _internal(run, "POST", "/internal/robots/drop", b"[]")[0] == 400
    phone.shut()


@pytest.mark.parametrize("token", [None, "", "wrong", _TOKEN + "x", "Basic " + _TOKEN])
def test_the_internal_routes_need_the_growbot_token(run: _Running, token: str | None) -> None:
    for method, path, body in (
        ("GET", "/internal/robots", None),
        ("POST", "/internal/robots/drop", b'{"code":"gb-abc123"}'),
    ):
        status, _ = _internal(run, method, path, body, token=token)
        assert status == 401


def test_a_session_cookie_is_no_way_into_the_internal_routes(run: _Running) -> None:
    assert run.request("GET", "/internal/robots").status == 401


def test_no_token_on_disk_believes_no_caller(run: _Running) -> None:
    run.app.config.token_file.unlink()
    assert _internal(run, "GET", "/internal/robots")[0] == 401


def test_an_unknown_internal_route_is_404(run: _Running) -> None:
    assert _internal(run, "GET", "/internal/nope")[0] == 404
    assert _internal(run, "POST", "/internal/robots")[0] == 404


def _act(run: _Running, frame: object, code: str = "gb-abc123") -> tuple[int, Any]:
    body = json.dumps({"code": code, "frame": frame}).encode()
    return _internal(run, "POST", "/internal/robots/act", body)


def test_a_pwa_move_goes_straight_to_the_chip(run: _Running) -> None:
    steps = [{"l": 95, "r": 85, "ms": 300}]
    # No chip on the relay: nothing is sent, and the api says so to the PWA.
    assert _act(run, {"t": "act", "steps": steps}) == (200, {"sent": False})
    phone = _phone(run)
    assert phone.message() == {"t": "status", "awake": 0}
    chip = _chip(run)
    # Awake once the api has verified its key: only then is it the code's chip.
    assert phone.message() == {"t": "status", "awake": 1}
    assert _act(run, {"t": "act", "steps": steps, "extra": "dropped"}) == (200, {"sent": True})
    got = chip.message()
    assert got["rid"] >= 1_500_000_000
    assert {k: v for k, v in got.items() if k != "rid"} == {
        "t": "act",
        "steps": steps,
        "mode": "replace",
    }
    assert _act(run, {"t": "routine", "name": "wiggle"}) == (200, {"sent": True})
    assert chip.message()["name"] == "wiggle"
    assert _act(run, {"t": "stop"}) == (200, {"sent": True})
    stop = chip.message()
    assert stop["t"] == "stop" and stop["rid"] > got["rid"]
    chip.shut()
    phone.shut()


@pytest.mark.parametrize(
    "frame",
    [
        None,
        [],
        {"t": "pose", "lr": "90,90"},
        {"t": "routine", "name": "dance"},
        {"t": "act", "steps": []},
        {"t": "act", "steps": [{"l": 90, "r": 90, "ms": 300}] * 13},
        {"t": "act", "steps": [{"l": 181, "r": 90, "ms": 300}]},
        {"t": "act", "steps": [{"l": 90, "r": -1, "ms": 300}]},
        {"t": "act", "steps": [{"l": 90, "r": 90, "ms": 10}]},
        {"t": "act", "steps": [{"l": 90, "r": 90, "ms": 9000}]},
        {"t": "act", "steps": [{"l": 90.5, "r": 90, "ms": 300}]},
        {"t": "act", "steps": [{"l": True, "r": 90, "ms": 300}]},
        {"t": "act", "steps": [{"l": 90, "r": 90}]},
        {"t": "act", "steps": ["x"]},
    ],
)
def test_a_pwa_move_is_held_to_the_chips_safe_shape(run: _Running, frame: object) -> None:
    assert _act(run, frame)[0] == 400


def test_a_pwa_move_needs_a_real_code_and_the_token(run: _Running) -> None:
    assert _act(run, {"t": "stop"}, code="../x")[0] == 400
    body = json.dumps({"code": "gb-abc123", "frame": {"t": "stop"}}).encode()
    assert _internal(run, "POST", "/internal/robots/act", body, token="wrong")[0] == 401
    assert run.request("POST", "/internal/robots/act", body).status == 401


def _cal_get(run: _Running, query: str, cookie: str | None = None) -> tuple[int, Any]:
    headers = {"Cookie": cookie} if cookie else None
    resp = run.request("GET", "/api/box/cal" + query, headers=headers)
    data = resp.read()
    assert resp.getheader("Cache-Control") == "no-store"
    return resp.status, (json.loads(data) if data else None)


def test_the_creature_reads_its_robots_calibration_from_the_box(run: _Running) -> None:
    _Fake.cals = {"gb-abc123": {"L_OFF": 4, "R_OFF": -3, "SWAP": 1}}
    assert _cal_get(run, "?id=gb-abc123") == (200, {"cal": {"L_OFF": 4, "R_OFF": -3, "SWAP": 1}})
    reads = [r for r in _Fake.seen if r["path"].endswith("/cal")]
    assert reads[0]["path"] == "/internal/growbot/robots/gb-abc123/cal"
    assert reads[0]["auth"] == f"Bearer {_TOKEN}"


def test_a_never_saved_or_unreadable_calibration_is_null(run: _Running) -> None:
    # Null, so the phone keeps its own.
    assert _cal_get(run, "?id=gb-abc123") == (200, {"cal": None})
    _Fake.cals = {"gb-abc123": {"L_OFF": 4}}
    _Fake.cal_status = 503
    run.app._cals.clear()
    assert _cal_get(run, "?id=gb-abc123") == (200, {"cal": None})


def test_a_phone_reads_only_the_calibration_of_the_robot_it_drives(run: _Running) -> None:
    # gb_walk_cal is one key per phone: a link naming another robot must not leave that
    # robot's trims on this one, so the api is never even asked.
    _Fake.cals = {"gb-other": {"L_OFF": 9}, "gb-worn": {"R_OFF": 2}}
    assert _cal_get(run, "?id=gb-other") == (200, {"cal": None})
    assert not [r for r in _Fake.seen if r["path"].endswith("/cal")]
    # A persona's phone reads the robot its persona wears now, whatever its ticket named.
    _Fake.riders = {"gb-worn": (_PERSONA, _PHONE)}
    cookie = _persona_cookie(run, robot="")
    assert _cal_get(run, "?id=gb-worn", cookie) == (200, {"cal": {"R_OFF": 2}})
    assert _cal_get(run, "?id=gb-other", cookie) == (200, {"cal": None})
    other_phone = _persona_cookie(run, robot="", phone="phone-" + "b" * 20)
    assert _cal_get(run, "?id=gb-worn", other_phone) == (200, {"cal": None})
    # Asked once a while, not at every poll.
    for _ in range(3):
        _cal_get(run, "?id=gb-worn", cookie)
    assert len([r for r in _Fake.seen if r["path"] == "/internal/growbot/body/gb-worn"]) == 2


def test_the_calibration_read_is_cached_briefly(run: _Running) -> None:
    _Fake.cals = {"gb-abc123": {"L_OFF": 1}}
    for _ in range(3):
        assert _cal_get(run, "?id=gb-abc123")[1] == {"cal": {"L_OFF": 1}}
    assert len([r for r in _Fake.seen if r["path"].endswith("/cal")]) == 1


def test_the_calibration_read_wants_a_code_a_get_and_a_session(run: _Running) -> None:
    assert _cal_get(run, "?id=../x")[0] == 400
    assert _cal_get(run, "")[0] == 400
    resp = run.request("POST", "/api/box/cal?id=gb-abc123", b"{}")
    resp.read()
    assert resp.status == 405
    resp = run.request("GET", "/api/box/cal?id=gb-abc123", cookie=False)
    resp.read()
    assert resp.status == 401


def test_handle_checks_the_bearer_too(run: _Running, gb: types.ModuleType) -> None:
    req = gb.Request("GET", "/internal/robots", "/internal/robots", {}, "HTTP/1.1", [])
    assert run.app.internal(req).status == 401


# --- SNTP for the robots ------------------------------------------------------------------


def _client_request(first: int = 0x23, size: int = 48) -> bytes:
    data = bytearray(size)
    if size:
        data[0] = first
    if size > 2:
        data[2] = 0xF6  # poll -10: a signed byte, echoed raw
    if size >= 48:
        data[40:48] = b"ORIGINAL"
    return bytes(data)


def test_sntp_answers_a_client_in_server_mode(gb: types.ModuleType) -> None:
    import struct

    now = 1_794_000_000.25
    reply = gb.sntp_reply(_client_request(), now - 0.5, now)
    assert reply is not None and len(reply) == 48
    assert reply[0] == (4 << 3) | 4  # LI 0, the client's version 4, server mode
    assert reply[1] == gb.NTP_STRATUM and reply[1] != 0  # never kiss-o'-death
    assert reply[2] == 0xF6 and reply[12:16] == b"JBOX"
    assert reply[24:32] == b"ORIGINAL"  # the client's transmit, echoed as originate
    secs, frac = struct.unpack("!II", reply[40:48])
    assert secs - gb.NTP_UNIX_DELTA == int(now) and abs(frac / 2**32 - 0.25) < 1e-6
    assert struct.unpack("!I", reply[32:36])[0] - gb.NTP_UNIX_DELTA == int(now - 0.5)
    v3 = gb.sntp_reply(_client_request(0x1B), now, now)
    assert v3 is not None and v3[0] == (3 << 3) | 4


@pytest.mark.parametrize(
    "data",
    [
        b"",
        _client_request(size=47),
        _client_request(size=49),
        _client_request(size=1024),
        _client_request(0x24),  # mode 4: a server's reply, never answered (no loops)
        _client_request(0x26),  # mode 6: control, the classic amplifier
        _client_request(0x27),  # mode 7: private (monlist)
        _client_request(0x03),  # version 0
        _client_request(0x2B),  # version 5
    ],
)
def test_sntp_ignores_anything_but_a_well_formed_client_request(
    gb: types.ModuleType, data: bytes
) -> None:
    assert gb.sntp_reply(data, 0.0, 0.0) is None


def test_sntp_answers_over_udp_and_stays_silent_otherwise(gb: types.ModuleType) -> None:
    loop = asyncio.new_event_loop()
    try:
        transport = loop.run_until_complete(gb.start_sntp("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        sock.sendto(_client_request(), ("127.0.0.1", port))
        reply, _ = sock.recvfrom(1024)
        assert len(reply) == 48 and reply[0] & 7 == 4
        sock.sendto(b"x" * 500, ("127.0.0.1", port))
        with pytest.raises(TimeoutError):
            sock.recvfrom(1024)
        sock.close()
        loop.call_soon_threadsafe(transport.close)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
    finally:
        loop.close()


def test_the_firmware_sets_its_clock_before_a_verified_dial() -> None:
    chip = (_SERVE.parents[1] / "firmware" / "relay_chip.py").read_text()
    assert 'NTP_PORT = getattr(_box, "GROWBOT_NTP_PORT", 8123)' in chip
    assert "def ws_open():\n    feed()\n    _need_clock()\n" in chip
    # rp2's epoch is 1970, other ports' 2000; the RTC tuple is ntptime.settime's.
    assert "time.gmtime(0)[0] == 1970" in chip
    assert (
        "machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))" in chip
    )
    assert 'print("handshake: FAIL clock")' in chip
    assert "ntptime.host = wlan.ifconfig()[2]" in chip


def test_the_firmware_clock_logic_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The patched functions, executed under CPython with fake MicroPython modules: a box
    reply sets the RTC, and a clock that stays in the past refuses to dial."""
    import struct

    chip = (_SERVE.parents[1] / "firmware" / "relay_chip.py").read_text()
    start = chip.index("NTP_PORT = getattr(")
    end = chip.index("\nprint(", chip.index("def _need_clock"))
    block = chip[start:end]
    set_to: list[tuple[int, ...]] = []
    year = [2021]
    sent: list[bytes] = []
    echo = [True]

    class FakeSocket:
        def __init__(self, *a: object) -> None:
            pass

        def settimeout(self, t: float) -> None:
            pass

        def sendto(self, q: bytes, addr: object) -> None:
            assert len(q) == 48 and q[0] == 0x23
            sent.append(bytes(q))

        def recv(self, n: int) -> bytes:
            reply = bytearray(48)
            reply[0], reply[1] = 0x24, 2
            # The box echoes the request's transmit stamp as the originate stamp.
            reply[24:32] = sent[-1][40:48] if echo[0] else b"\0" * 8
            reply[40:44] = struct.pack("!I", 1_794_000_000 + 2208988800)
            return bytes(reply)

        def close(self) -> None:
            pass

    fake_socket = types.SimpleNamespace(
        getaddrinfo=lambda h, p: [(0, 0, 0, "", (h, p))],
        socket=FakeSocket,
        AF_INET=2,
        SOCK_DGRAM=2,
    )

    def gmtime(secs: float | None = None) -> tuple[int, ...]:
        if secs is None:
            return (year[0], 1, 1, 0, 0, 0, 0, 0)
        return tuple(time.gmtime(secs))[:8]

    class Rtc:
        def datetime(self, t: tuple[int, ...]) -> None:
            set_to.append(t)
            year[0] = t[0]

    env: dict[str, object] = {
        "_box": types.SimpleNamespace(),
        "time": types.SimpleNamespace(gmtime=gmtime),
        "socket": fake_socket,
        "os": os,
        "machine": types.SimpleNamespace(RTC=Rtc),
        "feed": lambda: None,
        "HOST": "192.168.1.20",
        "BOX_CA": b"der",
        "wlan": types.SimpleNamespace(ifconfig=lambda: ("ip", "mask", "gw", "dns")),
    }
    exec(compile(block, "relay_chip.py", "exec"), env)  # noqa: S102 - the firmware under test
    env["_need_clock"]()  # type: ignore[operator]
    assert set_to and set_to[0][0] == 2026 and len(set_to[0]) == 8
    assert sent[0][40:48] != b"\0" * 8

    # A reply that does not echo this request's stamp (a LAN host racing the box) is ignored.
    year[0], echo[0] = 2021, False
    set_to.clear()
    monkeypatch.setitem(sys.modules, "ntptime", types.SimpleNamespace(settime=lambda: None))
    with pytest.raises(OSError, match="clock"):
        env["_need_clock"]()  # type: ignore[operator]
    assert set_to == []
    echo[0] = True

    year[0] = 2021
    FakeSocket.recv = lambda self, n: b"short"  # type: ignore[method-assign]
    monkeypatch.setitem(sys.modules, "ntptime", types.SimpleNamespace(settime=lambda: None))
    with pytest.raises(OSError, match="clock"):
        env["_need_clock"]()  # type: ignore[operator]
    env["BOX_CA"] = None
    env["_need_clock"]()  # type: ignore[operator] - unverified dials need no clock


# --- personas (wave G4b) -----------------------------------------------------------------


def _cookie_of(resp: http.client.HTTPResponse) -> str:
    return (resp.getheader("Set-Cookie") or "").split(";")[0]


def _persona_cookie(run: _Running, robot: str = "gb-abc123", phone: str = _PHONE) -> str:
    return f"gb_session={run.app.sessions.mint(robot=robot, persona=_PERSONA, phone=phone)}"


def test_a_persona_session_carries_its_persona_and_phone(gb: types.ModuleType) -> None:
    sessions = gb.Sessions(key=b"k" * 32)
    value = sessions.mint(now=1000, robot="gb-abc123", persona=_PERSONA, phone=_PHONE)
    assert sessions.claims(value, now=1000) == gb.Claims("gb-abc123", _PERSONA, _PHONE)
    assert sessions.robot(value, now=1000) == "gb-abc123"
    expiry, nonce, robot, persona, phone, sig = value.split(".")
    other = "1" + persona[1:]
    assert sessions.claims(f"{expiry}.{nonce}.{robot}.{other}.{phone}.{sig}", now=1000) is None
    assert sessions.claims(f"{expiry}.{nonce}.{robot}.{persona}.{sig}", now=1000) is None
    # G2's four-part sessions still open the creature, with no persona.
    old = sessions.mint(now=1000, robot="gb-abc123")
    assert sessions.claims(old, now=1000) == gb.Claims("gb-abc123")
    # A phone may keep its id with no persona; a persona never comes without its phone.
    kept = sessions.mint(now=1000, persona="", phone=_PHONE)
    assert sessions.claims(kept, now=1000) == gb.Claims("", "", _PHONE)
    for persona, phone in ((_PERSONA, ""), ("not-a-uuid", _PHONE), ("", "short"), ("", "a.b" * 9)):
        with pytest.raises(ValueError):
            sessions.mint(persona=persona, phone=phone)


def test_a_persona_ticket_opens_it_on_this_phone(run: _Running) -> None:
    resp = run.request(
        "GET",
        "/open?ticket=persona-ticket",
        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 6a)"},
        cookie=False,
    )
    assert _landing(resp) == "/v3/next.html?id=gb-abc123&mode=legs"
    got = run.app.sessions.claims(_cookie_of(resp).partition("=")[2])
    assert (got.robot, got.persona, got.phone) == ("gb-abc123", _PERSONA, _NEW_PHONE)
    redeem = [s for s in _Fake.seen if s["path"] == "/internal/growbot/ticket/redeem"][-1]
    # A phone with no session yet sends no id; the label is a kind of phone, never the UA.
    assert json.loads(redeem["body"]) == {
        "ticket": "persona-ticket",
        "phone": "",
        "label": "Android phone",
    }


def test_a_phone_stays_the_same_phone_across_tickets(run: _Running) -> None:
    held = _persona_cookie(run)
    resp = run.request(
        "GET", "/open?ticket=persona-bodiless", headers={"Cookie": held}, cookie=False
    )
    assert _landing(resp) == "/v3/next.html?mode=legs"
    redeem = [s for s in _Fake.seen if s["path"] == "/internal/growbot/ticket/redeem"][-1]
    assert json.loads(redeem["body"])["phone"] == _PHONE
    assert run.app.sessions.claims(_cookie_of(resp).partition("=")[2]).phone == _PHONE
    # A G2 ticket on it keeps the id (for the next persona) and opens no persona.
    g2 = run.request("GET", "/open?ticket=good-ticket", headers={"Cookie": held}, cookie=False)
    g2.read()
    got = run.app.sessions.claims(_cookie_of(g2).partition("=")[2])
    assert (got.robot, got.persona, got.phone) == ("gb-abc123", "", _PHONE)


def test_a_persona_answer_in_the_wrong_shape_opens_nothing(run: _Running) -> None:
    resp = run.request("GET", "/open?ticket=persona-odd", cookie=False)
    resp.read()
    assert resp.status == 401 and resp.getheader("Set-Cookie") is None


@pytest.mark.parametrize(
    ("agent", "label"),
    [
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", "iPhone"),
        ("Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)", "iPad"),
        ("curl/8", "Phone"),
        ("", "Phone"),
    ],
)
def test_a_new_phone_is_named_by_its_kind(gb: types.ModuleType, agent: str, label: str) -> None:
    assert gb.phone_label(agent) == label


def test_start_keeps_the_persona(run: _Running) -> None:
    resp = run.request("GET", "/start", headers={"Cookie": _persona_cookie(run)}, cookie=False)
    resp.read()
    assert resp.status == 302
    got = run.app.sessions.claims(_cookie_of(resp).partition("=")[2])
    assert (got.persona, got.phone) == (_PERSONA, _PHONE)


def _soul_call(
    run: _Running, method: str, path: str, body: object = None, cookie: str | None = None
) -> tuple[int, Any]:
    raw = None if body is None else json.dumps(body).encode()
    headers = {"Cookie": cookie or _persona_cookie(run), "Content-Type": "application/json"}
    resp = run.request(method, path, raw, headers, cookie=False)
    data = resp.read()
    return resp.status, (json.loads(data) if data else None)


def test_the_soul_routes_reach_the_api_as_this_phone(run: _Running) -> None:
    status, got = _soul_call(run, "GET", "/api/box/persona")
    assert status == 200
    assert got == {
        "version": 3,
        "soul": {"soul_format": 3},
        "wearing": "gb-abc123",
        "persona": _PERSONA,
    }
    pull = [s for s in _Fake.seen if s["path"] == "/internal/growbot/soul/pull"][-1]
    assert pull["auth"] == f"Bearer {_TOKEN}"
    assert json.loads(pull["body"]) == {"persona": _PERSONA, "phone": _PHONE}
    assert _soul_call(run, "GET", "/api/box/persona/state")[1] == {
        "version": 3,
        "wearing": "gb-abc123",
        "persona": _PERSONA,
    }
    soul = {"soul_format": 3, "identity": "I am Sprout."}
    assert _soul_call(
        run, "POST", "/api/box/persona", {"persona": _PERSONA, "base": 3, "soul": soul}
    ) == (
        200,
        {"version": 4, "persona": _PERSONA},
    )
    push = [s for s in _Fake.seen if s["path"] == "/internal/growbot/soul/push"][-1]
    assert json.loads(push["body"]) == {
        "persona": _PERSONA,
        "phone": _PHONE,
        "base": 3,
        "soul": soul,
    }


def test_a_g2_session_has_no_soul_on_the_box(run: _Running) -> None:
    for path in ("/api/box/persona", "/api/box/persona/state"):
        assert _soul_call(run, "GET", path, cookie=run.cookie) == (204, None)
    assert not [s for s in _Fake.seen if s["path"].startswith("/internal/growbot/soul/")]


@pytest.mark.parametrize(
    ("status", "detail"),
    [(409, "moved"), (409, "newer"), (404, "gone"), (413, "at most 1 MB"), (400, "not a soul")],
)
def test_the_apis_refusals_reach_the_page(run: _Running, status: int, detail: str) -> None:
    _Fake.soul = {"/internal/growbot/soul/push": (status, {"detail": detail})}
    got = _soul_call(run, "POST", "/api/box/persona", {"persona": _PERSONA, "base": 1, "soul": {}})
    assert got == (status, {"error": detail})


def test_a_push_names_the_persona_it_came_from(run: _Running) -> None:
    for named in ("", "1" + _PERSONA[1:], None):
        body = {"persona": named, "base": 1, "soul": {}}
        assert _soul_call(run, "POST", "/api/box/persona", body) == (409, {"error": "other"})
    assert not [s for s in _Fake.seen if s["path"] == "/internal/growbot/soul/push"]


def test_a_cross_site_open_keeps_the_phone(run: _Running) -> None:
    """The PWA reached by its public name: the first request carries no Strict cookie, so it
    only bounces; the hop back carries it, and the phone keeps its id."""
    cross = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "cross-site"}
    resp = run.request("GET", "/open?ticket=persona-bodiless", headers=cross, cookie=False)
    again = _landing(resp)
    hop = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "same-origin"}
    resp = run.request("GET", again, headers={**hop, "Cookie": _persona_cookie(run)}, cookie=False)
    assert _landing(resp) == "/v3/next.html?mode=legs"
    assert run.app.sessions.claims(_cookie_of(resp).partition("=")[2]).phone == _PHONE


def test_an_api_that_cannot_answer_is_a_retry(run: _Running) -> None:
    for status in (401, 500):
        _Fake.soul = {"/internal/growbot/soul/pull": (status, {"detail": "x"})}
        assert _soul_call(run, "GET", "/api/box/persona")[0] == 503
    _Fake.soul = {"/internal/growbot/soul/pull": (200, ["not", "an", "object"])}  # type: ignore[dict-item]
    assert _soul_call(run, "GET", "/api/box/persona")[0] == 503


@pytest.mark.parametrize("body", [{"soul": {}}, {"base": "3", "soul": {}}, {"base": True}, []])
def test_a_push_names_the_version_it_came_from(run: _Running, body: object) -> None:
    assert _soul_call(run, "POST", "/api/box/persona", body)[0] == 400
    assert not [s for s in _Fake.seen if s["path"] == "/internal/growbot/soul/push"]


def test_the_soul_routes_take_only_their_methods(run: _Running) -> None:
    assert _soul_call(run, "POST", "/api/box/persona/state", {"base": 1})[0] == 405
    assert _soul_call(run, "DELETE", "/api/box/persona")[0] == 405


def test_a_soul_from_another_origin_is_refused(run: _Running) -> None:
    headers = {"Cookie": _persona_cookie(run), "Origin": "https://jbrain.local"}
    resp = run.request("POST", "/api/box/persona", b'{"base":1,"soul":{}}', headers, cookie=False)
    resp.read()
    assert resp.status == 403


def test_an_oversized_soul_is_refused_before_its_upload(
    run: _Running, gb: types.ModuleType
) -> None:
    head = (
        f"POST /api/box/persona HTTP/1.1\r\nHost: {run.host}\r\nCookie: {_persona_cookie(run)}\r\n"
        f"Content-Length: {gb.MAX_SOUL_BODY + 1}\r\nExpect: 100-continue\r\n\r\n"
    )
    out = _raw(run, head.encode())
    assert out.startswith(b"HTTP/1.1 413"), out[:80]
    # Every other route keeps the 16 MiB cap.
    assert gb.body_limit(gb.Request("POST", "/api/llm", "/api/llm", {}, "HTTP/1.1", [])) == (
        gb.MAX_BODY
    )


def test_only_the_rider_drives_the_body(run: _Running) -> None:
    mine = _persona_cookie(run)
    # Nobody rides: a persona phone may not drive it, a G2 phone may.
    assert _Ws(run, "/d/gb-abc123", cookie=mine).status == 403
    g2 = _phone(run)
    g2.shut()
    # This persona rides from this phone: it may, whatever robot its ticket named, and a G2
    # phone may not.
    _Fake.riders = {"gb-abc123": (_PERSONA, _PHONE)}
    assert _Ws(run, "/d/gb-abc123", cookie=_persona_cookie(run, robot="")).status == 101
    assert _Ws(run, "/d/gb-abc123", cookie=True).status == 403
    # The same persona on another phone (it moved) may not.
    assert _Ws(run, "/d/gb-abc123", cookie=_persona_cookie(run, phone=_NEW_PHONE)).status == 403
    body = [s for s in _Fake.seen if s["path"] == "/internal/growbot/body/gb-abc123"][-1]
    assert body["auth"] == f"Bearer {_TOKEN}"


def test_a_relay_that_cannot_ask_the_api_says_busy(run: _Running) -> None:
    _Fake.body_status = 500
    assert _Ws(run, "/d/gb-abc123", cookie=_persona_cookie(run)).status == 503


def test_a_phone_that_stops_riding_is_dropped(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "RIDER_RECHECK_S", 0.1)
    _Fake.riders = {"gb-abc123": (_PERSONA, _PHONE)}
    phone = _Ws(run, "/d/gb-abc123", cookie=_persona_cookie(run))
    assert phone.status == 101 and phone.message() == {"t": "status", "awake": 0}
    # An api blip keeps it on.
    _Fake.body_status = 500
    time.sleep(0.4)
    _Fake.body_status = 200
    chip = _chip(run)
    assert phone.message() == {"t": "status", "awake": 1}
    # A hand-over to another phone closes it.
    _Fake.riders = {"gb-abc123": (_PERSONA, _NEW_PHONE)}
    assert phone.close_code() == 1008
    chip.shut()


def test_the_side_channel_also_leaves_with_the_rider(
    run: _Running, gb: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gb, "RIDER_RECHECK_S", 0.1)
    _Fake.riders = {"gb-abc123": (_PERSONA, _PHONE)}
    agent = _Ws(run, "/d/gb-abc123-agent", cookie=_persona_cookie(run))
    assert agent.status == 101
    agent.text({"t": "attach"})
    assert agent.message() == {"t": "status", "awake": 0}
    _Fake.riders = {"gb-abc123": (_PERSONA, _NEW_PHONE)}
    assert agent.close_code() == 1008


def test_a_session_trying_robots_it_does_not_ride_is_slowed(
    run: _Running, gb: types.ModuleType
) -> None:
    cookie = _persona_cookie(run)
    for _ in range(gb.BACKOFF_LIMIT):
        assert _Ws(run, "/d/gb-abc123", cookie=cookie).status == 403
    assert _Ws(run, "/d/gb-abc123", cookie=cookie).status == 429


def test_one_sessions_refusals_never_lock_out_another(run: _Running, gb: types.ModuleType) -> None:
    """A G2 phone retrying a robot a persona rides, behind the same address as the rider."""
    _Fake.riders = {"gb-abc123": (_PERSONA, _PHONE)}
    for _ in range(gb.BACKOFF_LIMIT):
        assert _Ws(run, "/d/gb-abc123", cookie=True).status == 403
    assert _Ws(run, "/d/gb-abc123", cookie=True).status == 429
    assert _Ws(run, "/d/gb-abc123", cookie=_persona_cookie(run)).status == 101


# --- a persona's visual memories (wave G4d) -------------------------------------------------


def test_the_memory_routes_reach_the_api_as_this_phone(run: _Running) -> None:
    assert _soul_call(run, "GET", "/api/box/memories") == (
        200,
        {"version": 2, "memories": [], "persona": _PERSONA},
    )
    listed = [s for s in _Fake.seen if s["path"] == "/internal/growbot/memories/list"][-1]
    assert listed["auth"] == f"Bearer {_TOKEN}"
    assert json.loads(listed["body"]) == {"persona": _PERSONA, "phone": _PHONE}
    # IndexedDB keeps a view as base64, so that is how one is sent.
    assert _soul_call(run, "GET", "/api/box/memories/view?id=r1a&i=2") == (
        200,
        {"j": base64.b64encode(_JPEG).decode()},
    )
    view = [s for s in _Fake.seen if s["path"] == "/internal/growbot/memories/view"][-1]
    assert json.loads(view["body"]) == {"persona": _PERSONA, "phone": _PHONE, "id": "r1a", "i": 2}
    record = {"id": "r1a", "views": [{"j": "AAAA", "t": 1}]}
    assert _soul_call(
        run, "POST", "/api/box/memories", {"persona": _PERSONA, "memory": record}
    ) == (200, {"version": 3, "persona": _PERSONA})
    keep = [s for s in _Fake.seen if s["path"] == "/internal/growbot/memories/keep"][-1]
    assert json.loads(keep["body"]) == {"persona": _PERSONA, "phone": _PHONE, "memory": record}
    assert _soul_call(
        run, "POST", "/api/box/memories/forget", {"persona": _PERSONA, "ids": ["r1a"]}
    ) == (200, {"version": 3, "persona": _PERSONA})
    forget = [s for s in _Fake.seen if s["path"] == "/internal/growbot/memories/forget"][-1]
    assert json.loads(forget["body"]) == {"persona": _PERSONA, "phone": _PHONE, "ids": ["r1a"]}


def test_a_g2_session_has_no_memories_on_the_box(run: _Running) -> None:
    for method, path in (
        ("GET", "/api/box/memories"),
        ("GET", "/api/box/memories/view?id=r1&i=0"),
        ("POST", "/api/box/memories"),
        ("POST", "/api/box/memories/forget"),
    ):
        body = {"persona": _PERSONA} if method == "POST" else None
        assert _soul_call(run, method, path, body, cookie=run.cookie) == (204, None)
    assert not [s for s in _Fake.seen if s["path"].startswith("/internal/growbot/memories/")]


def test_memories_name_the_persona_they_came_from(run: _Running) -> None:
    for path in ("/api/box/memories", "/api/box/memories/forget"):
        for named in ("", "1" + _PERSONA[1:], None):
            body = {"persona": named, "memory": {}, "ids": []}
            assert _soul_call(run, "POST", path, body) == (409, {"error": "other"})
    assert _soul_call(run, "POST", "/api/box/memories", ["not", "an", "object"]) == (
        400,
        {"error": "a visual memory"},
    )
    for query in ("id=r1", "id=R1&i=0", "id=r1&i=3", "id=r1&i=-1", "i=0", "id=../x&i=0"):
        assert _soul_call(run, "GET", "/api/box/memories/view?" + query)[0] == 400, query
    assert not [s for s in _Fake.seen if s["path"].startswith("/internal/growbot/memories/")]


@pytest.mark.parametrize(
    ("path", "status", "detail"),
    [
        ("keep", 409, "moved"),
        ("keep", 404, "gone"),
        ("keep", 413, "full"),
        ("keep", 400, "a view that is not a JPEG"),
        ("view", 404, "no such view"),
        ("list", 409, "moved"),
    ],
)
def test_the_apis_memory_refusals_reach_the_page(
    run: _Running, path: str, status: int, detail: str
) -> None:
    _Fake.soul = {f"/internal/growbot/memories/{path}": (status, {"detail": detail})}
    method, url, body = {
        "keep": ("POST", "/api/box/memories", {"persona": _PERSONA, "memory": {}}),
        "view": ("GET", "/api/box/memories/view?id=r1&i=0", None),
        "list": ("GET", "/api/box/memories", None),
    }[path]
    assert _soul_call(run, method, url, body) == (status, {"error": detail})


def test_a_memory_api_that_cannot_answer_is_a_retry(run: _Running) -> None:
    for status in (401, 500):
        _Fake.soul = {"/internal/growbot/memories/list": (status, {"detail": "x"})}
        assert _soul_call(run, "GET", "/api/box/memories")[0] == 503
    _Fake.soul = {"/internal/growbot/memories/list": (200, ["no"])}  # type: ignore[dict-item]
    assert _soul_call(run, "GET", "/api/box/memories")[0] == 503
    _Fake.soul = {"/internal/growbot/memories/keep": (418, ["no"])}  # type: ignore[dict-item]
    body = {"persona": _PERSONA, "memory": {}}
    assert _soul_call(run, "POST", "/api/box/memories", body)[0] == 503
    _Fake.soul = {"/internal/growbot/memories/forget": (400, ["no"])}  # type: ignore[dict-item]
    body = {"persona": _PERSONA, "ids": []}
    assert _soul_call(run, "POST", "/api/box/memories/forget", body) == (400, {"error": "refused"})


def test_the_memory_routes_take_only_their_methods(run: _Running) -> None:
    assert _soul_call(run, "POST", "/api/box/memories/view", {"persona": _PERSONA})[0] == 405
    assert _soul_call(run, "GET", "/api/box/memories/forget")[0] == 405
    assert _soul_call(run, "DELETE", "/api/box/memories")[0] == 405


def test_a_memory_bigger_than_three_views_is_refused_before_its_upload(
    run: _Running, gb: types.ModuleType
) -> None:
    head = (
        f"POST /api/box/memories HTTP/1.1\r\nHost: {run.host}\r\n"
        f"Cookie: {_persona_cookie(run)}\r\n"
        f"Content-Length: {gb.MAX_MEMORY_BODY + 1}\r\nExpect: 100-continue\r\n\r\n"
    )
    out = _raw(run, head.encode())
    assert out.startswith(b"HTTP/1.1 413"), out[:80]
    req = gb.Request("POST", "/api/box/memories", "/api/box/memories", {}, "HTTP/1.1", [])
    assert gb.body_limit(req) == gb.MAX_MEMORY_BODY >= 3 * 800_000


def test_a_deeply_nested_frame_is_no_message(gb: types.ModuleType) -> None:
    assert gb._parse(b"[" * 100_000 + b"]" * 100_000) is None
