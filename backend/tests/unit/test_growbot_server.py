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
import http.client
import importlib.util
import json
import os
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
    tts_status = 200
    llm_status = 200

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
            }
        )

    def do_GET(self) -> None:
        self._record(b"")
        path, _, query = self.path.partition("?")
        if path == "/internal/growbot/llm/v1/models":
            if self.headers.get("Authorization") != f"Bearer {_TOKEN}":
                self._reply(401, b"{}")
                return
            self._reply(200, json.dumps({"data": _Fake.models}).encode())
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
            ok = authed and json.loads(body) == {"ticket": "good-ticket"}
            self._reply(200 if ok else 401, json.dumps({"ok": ok}).encode())
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
        self.cookie = f"gb_session={app.sessions.mint()}"

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
    (tmp_path / "secret.txt").write_text("outside the root")
    return root


@pytest.fixture
def fake() -> Iterator[str]:
    _Fake.seen = []
    _Fake.models = [{"id": "text-only", "vision": False}, {"id": "qwen3-vl-30b", "vision": True}]
    _Fake.stt_reply = b"{}"
    _Fake.tts_status = 200
    _Fake.llm_status = 200
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
    expiry, nonce, sig = value.split(".")
    assert not sessions.valid(f"{int(expiry) + 999}.{nonce}.{sig}", now=1000)
    for bad in ("", "a.b", "x.y.z", "1.2.3.4", f"{expiry}.{nonce}.{sig}x"):
        assert not sessions.valid(bad, now=1000)


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


def test_open_exchanges_a_ticket_for_a_session(run: _Running, gb: types.ModuleType) -> None:
    resp = run.request("GET", "/open?ticket=good-ticket&id=gb-abc123", cookie=False)
    resp.read()
    assert resp.status == 302
    assert resp.getheader("Location") == "/v3/next.html?id=gb-abc123&mode=legs"
    cookie = resp.getheader("Set-Cookie") or ""
    name_value, *attrs = [a.strip() for a in cookie.split(";")]
    assert set(attrs) == {"HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=43200"}
    assert name_value.startswith("gb_session=")
    redeem = [s for s in _Fake.seen if s["path"] == "/internal/growbot/ticket/redeem"]
    assert redeem[-1]["auth"] == f"Bearer {_TOKEN}"
    page = run.request("GET", "/v3/next.html", headers={"Cookie": name_value}, cookie=False)
    assert page.status == 200
    assert page.read() == b"<p>creature</p>"


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
    assert other.status == 302


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


def test_stt_without_audio_or_with_whisper_down(run: _Running) -> None:
    body, ctype = _multipart({"model": "x"}, b"")
    resp = run.request("POST", "/api/llm?target=groq-audio", body, {"Content-Type": ctype})
    assert resp.status == 400
    resp.read()
    run.app.config.stt_url = "http://127.0.0.1:9/v1"
    body, ctype = _multipart({}, b"audio")
    resp = run.request("POST", "/api/llm?target=groq-audio", body, {"Content-Type": ctype})
    assert resp.status == 503  # never 429, which upstream reads as a spent allowance
    resp.read()


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
    def __init__(self, run: _Running, path: str, *, cookie: bool, origin: str | None = None):
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
            headers.append(f"Cookie: {run.cookie}")
            headers.append(f"Origin: {origin or run.origin}")
        elif origin:
            headers.append(f"Origin: {origin}")
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
    agent = _phone(run, "/d/gb-random4-agent")
    agent.send(0x2, b"x")
    assert agent.close_code() == 1008
    phone = _phone(run, "/d/gb-random5")
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
    assert set(svc["networks"]) == {"edge", "internal"}
    assert "ports" not in svc
    env = svc["environment"]
    assert env["GROWBOT_SITE"] == "/app/site"
    assert env["GROWBOT_TOKEN_FILE"] == "/run/jbrain-growbot/growbot.token"
    assert env["GROWBOT_API_URL"] == "http://api:8000"
    assert env["GROWBOT_STT_URL"] == "http://tts-stt:8080/v1"
    assert svc["build"]["dockerfile"] == "deploy/Dockerfile.growbot"
    dockerfile = (_DEPLOY / "Dockerfile.growbot").read_text()
    assert "FROM python:3.12-slim" in dockerfile
    assert "pip" not in dockerfile.split("FROM", 1)[1]
