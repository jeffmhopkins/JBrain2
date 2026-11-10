#!/usr/bin/env python3
"""The GrowBot sidecar: the creature's web app, its /api shim and the robot relay, on the box.

One asyncio process behind Caddy's LAN site `https://<lan host>:8444` (docs/plans/GROWBOT_PLAN.md
§2). It serves `GROWBOT_SITE` as the web root, answers the upstream app's `/api/*` calls from the
box (the api's model lane, tts-stt's Kokoro and whisper, and fixed answers for the cloud-only
routes), and relays WebSocket frames between the phone and the robot at `/d/<code>`.

Security, in order of the request:
- `/open?ticket=…` is the only door without a session: the api redeems the one-time ticket the
  PWA minted, and the answer is a `gb_session` cookie signed with a key made at process start
  (a restart signs every phone out; nothing is stored).
- Every other route needs that cookie, except the robot's side of the relay, which has no
  browser: it proves itself with the device key in its `hello`, checked against the api.
- Every response carries a CSP that confines the page to this origin, so vendored code cannot
  reach another host even if a patch missed a URL. Outbound, this process talks to the api and
  tts-stt by compose name only, with proxies and redirects disabled.

Stdlib only: HTTP/1.1 and the RFC 6455 server side are implemented here, so the image is a bare
python:3.12-slim with no pip step.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

SESSION_COOKIE = "gb_session"
SESSION_TTL_S = 43200

MAX_HEAD = 16 * 1024
MAX_HEADERS = 100
# Camera frames ride the chat body as base64 data URLs and speech clips as multipart; both stay
# well under this, and nothing larger has a reason to reach the box's model or STT.
MAX_BODY = 16 * 1024 * 1024
MAX_WS_MESSAGE = 256 * 1024
IDLE_TIMEOUT_S = 75.0
BODY_TIMEOUT_S = 60.0
UPSTREAM_TIMEOUT_S = 120.0
FIRST_FRAME_TIMEOUT_S = 10.0
WS_PING_EVERY_S = 20.0
# Upstream's relay drops a chip 45 s after its last heartbeat (relay_chip.py, "0923 RELAY
# GHOST"): a robot that lost power never closes its socket, so silence is the only signal.
WS_DEAD_AFTER_S = 45.0
ALIVE_EVERY_S = 30.0
MAX_SOCKETS = 256
MAX_PEERS_PER_CHANNEL = 8
MODELS_CACHE_S = 30.0

# Pairing codes are upstream's `gb-` + 6 hex, but a bench rig may pin its own.
CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
HASH_RE = re.compile(r"sha(256|384|512)-[A-Za-z0-9+/]+={0,2}")
KOKORO_VOICE_RE = re.compile(r"kokoro-[a-z0-9_]{1,40}")
LANG_RE = re.compile(r"[a-z]{2,3}")
TOKEN_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

# A vision-capable lineup makes visual memory work (plan §2 "Models"); the first installed one
# of these wins, then any vision model, then anything installed.
PREFERRED_MODELS = ("qwen3-vl-30b", "qwen3.8-27b", "qwen3.8-flash-next")

# Only what an OpenAI-style local server reads. Upstream's OpenRouter extras (provider pins,
# reasoning, usage accounting) mean nothing on the box, and some engines reject unknown fields.
LLM_FIELDS = (
    "messages",
    "max_tokens",
    "temperature",
    "top_p",
    "stop",
    "response_format",
    "seed",
    "presence_penalty",
    "frequency_penalty",
)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".wasm": "application/wasm",
    ".onnx": "application/octet-stream",
    ".tflite": "application/octet-stream",
    ".bin": "application/octet-stream",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".webmanifest": "application/manifest+json",
}

UNAUTHORISED_PAGE = (
    b"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    b"<title>GrowBot</title><p>Open GrowBot from JBrain.</p>"
)

STATUS_TEXT = {
    101: "Switching Protocols",
    200: "OK",
    204: "No Content",
    302: "Found",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    410: "Gone",
    411: "Length Required",
    413: "Content Too Large",
    426: "Upgrade Required",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
}

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"growbot: {message}", flush=True)


# ── configuration ─────────────────────────────────────────────────────────────


@dataclass
class Config:
    site: Path
    api_url: str
    tts_url: str
    stt_url: str
    token_file: Path
    default_model: str = "qwen3-vl-30b"

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ.get
        return cls(
            site=Path(env("GROWBOT_SITE", str(HERE.parent / "site"))),
            api_url=env("GROWBOT_API_URL", "http://api:8000").rstrip("/"),
            tts_url=env("GROWBOT_TTS_URL", "http://tts-stt:8801").rstrip("/"),
            stt_url=env("GROWBOT_STT_URL", "http://tts-stt:8080/v1").rstrip("/"),
            token_file=Path(
                env("GROWBOT_TOKEN_FILE", "/run/jbrain-growbot/growbot.token")
            ),
            default_model=env("GROWBOT_DEFAULT_MODEL", "qwen3-vl-30b"),
        )


def build_csp(site: Path) -> str:
    """The page's CSP (plan §2 "No egress"). The patched site lists the hashes of the inline
    scripts and handlers it still carries in `csp.json`, so script-src never needs
    'unsafe-inline'. A missing or malformed file means no inline script runs at all."""
    hashes: list[str] = []
    unsafe_hashes = False
    try:
        spec = json.loads((site / "csp.json").read_text())
    except (OSError, ValueError):
        spec = {}
    if isinstance(spec, dict):
        listed = spec.get("script_hashes")
        if isinstance(listed, list):
            hashes = [h for h in listed if isinstance(h, str) and HASH_RE.fullmatch(h)]
        unsafe_hashes = spec.get("unsafe_hashes") is True
    script_src = ["'self'", "'wasm-unsafe-eval'", *(f"'{h}'" for h in hashes)]
    if unsafe_hashes and hashes:
        script_src.append("'unsafe-hashes'")
    return "; ".join(
        (
            "default-src 'self'",
            "script-src " + " ".join(script_src),
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "media-src 'self' blob:",
            "worker-src 'self' blob:",
            "connect-src 'self' blob:",
            "font-src 'self' data:",
            "frame-ancestors 'self'",
            "base-uri 'self'",
        )
    )


PERMISSIONS_POLICY = (
    "camera=(self), microphone=(self), accelerometer=(self), gyroscope=(self), "
    "magnetometer=(self), screen-wake-lock=(self), geolocation=(), payment=(), usb=()"
)


# ── the session cookie ────────────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Sessions:
    """`gb_session` = `<expiry>.<nonce>.<hmac>`, keyed per process: nothing to store or leak."""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key or secrets.token_bytes(32)

    def _sign(self, body: str) -> str:
        return _b64url(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    def mint(self, now: float | None = None) -> str:
        expiry = int(time.time() if now is None else now) + SESSION_TTL_S
        body = f"{expiry}.{secrets.token_urlsafe(12)}"
        return f"{body}.{self._sign(body)}"

    def valid(self, value: str, now: float | None = None) -> bool:
        parts = value.split(".")
        if len(parts) != 3 or not parts[0].isdigit() or len(parts[0]) > 12:
            return False
        if not hmac.compare_digest(self._sign(f"{parts[0]}.{parts[1]}"), parts[2]):
            return False
        return int(parts[0]) > (time.time() if now is None else now)


def cookie_value(headers: list[tuple[str, str]], name: str) -> str:
    for key, value in headers:
        if key != "cookie":
            continue
        for item in value.split(";"):
            k, sep, v = item.strip().partition("=")
            if sep and k == name:
                return v.strip()
    return ""


# ── outbound calls (api and tts-stt only) ─────────────────────────────────────


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


# No proxy from the environment and no redirects: a call meant for `api` can only reach `api`.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


@dataclass
class Upstream:
    status: int
    body: bytes
    ctype: str


def http_call(
    url: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    timeout: float = 10.0,
) -> Upstream:
    """One blocking call; any transport failure is a 503 the caller can answer with."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return Upstream(
                resp.status, resp.read(), resp.headers.get("Content-Type", "")
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except OSError:
            body = b""
        return Upstream(
            exc.code, body, exc.headers.get("Content-Type", "") if exc.headers else ""
        )
    except (OSError, ValueError):
        return Upstream(503, b"", "")


# ── HTTP plumbing ─────────────────────────────────────────────────────────────


class HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


@dataclass
class Request:
    method: str
    target: str
    path: str
    query: dict[str, list[str]]
    version: str
    headers: list[tuple[str, str]]
    body: bytes = b""

    def header(self, name: str) -> str:
        for key, value in self.headers:
            if key == name:
                return value
        return ""

    def arg(self, name: str) -> str:
        return (self.query.get(name) or [""])[0]

    def tokens(self, name: str) -> set[str]:
        out: set[str] = set()
        for key, value in self.headers:
            if key == name:
                out |= {t.strip().lower() for t in value.split(",") if t.strip()}
        return out


@dataclass
class Response:
    status: int
    body: bytes = b""
    ctype: str = "text/plain; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)
    stream: AsyncGenerator[bytes, None] | None = None
    length: int | None = None


def json_response(
    status: int, obj: object, headers: list[tuple[str, str]] | None = None
) -> Response:
    return Response(status, json.dumps(obj).encode(), "application/json", headers or [])


async def read_head(reader: asyncio.StreamReader) -> Request:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise HttpError(431) from None
    if len(raw) > MAX_HEAD:
        raise HttpError(431)
    try:
        text = raw[:-4].decode("latin-1")
    except UnicodeDecodeError:
        raise HttpError(400) from None
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not TOKEN_RE.fullmatch(parts[0]):
        raise HttpError(400)
    method, target, version = parts
    if version not in ("HTTP/1.1", "HTTP/1.0") or not target.startswith("/"):
        raise HttpError(400)
    if len(lines) - 1 > MAX_HEADERS:
        raise HttpError(431)
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        # No obs-fold and no whitespace before the colon: both are smuggling vectors.
        if not sep or not TOKEN_RE.fullmatch(name):
            raise HttpError(400)
        headers.append((name.lower(), value.strip(" \t")))
    path, _, query = target.partition("?")
    return Request(
        method=method,
        target=target,
        path=path,
        query=urllib.parse.parse_qs(query, keep_blank_values=True),
        version=version,
        headers=headers,
    )


async def read_body(
    req: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    lengths = {v for k, v in req.headers if k == "content-length"}
    chunked = [v for k, v in req.headers if k == "transfer-encoding"]
    if chunked and lengths:
        raise HttpError(400)
    if len(lengths) > 1:
        raise HttpError(400)
    if not chunked and not lengths:
        return
    if req.header("expect").lower() == "100-continue":
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
    if lengths:
        value = next(iter(lengths))
        if not value.isdigit():
            raise HttpError(400)
        size = int(value)
        if size > MAX_BODY:
            raise HttpError(413)
        req.body = await reader.readexactly(size)
        return
    if len(chunked) != 1 or chunked[0].lower() != "chunked":
        raise HttpError(501)
    body = bytearray()
    while True:
        line = await reader.readuntil(b"\r\n")
        size_text = line[:-2].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            raise HttpError(400) from None
        if size < 0 or len(body) + size > MAX_BODY:
            raise HttpError(413)
        if size == 0:
            break
        body += await reader.readexactly(size)
        if await reader.readexactly(2) != b"\r\n":
            raise HttpError(400)
    while (await reader.readuntil(b"\r\n")) != b"\r\n":
        pass
    req.body = bytes(body)


# ── WebSocket (RFC 6455, server side) ─────────────────────────────────────────


class WsClosed(Exception):
    pass


def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def _unmask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    pad = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(n, "big")


def ws_frame(
    opcode: int, payload: bytes, *, fin: bool = True, mask: bytes | None = None
) -> bytes:
    """Encode one frame. The server never masks; `mask` is for tests playing the client."""
    head = bytearray([(0x80 if fin else 0) | opcode])
    flag = 0x80 if mask else 0
    n = len(payload)
    if n < 126:
        head.append(flag | n)
    elif n < 65536:
        head.append(flag | 126)
        head += n.to_bytes(2, "big")
    else:
        head.append(flag | 127)
        head += n.to_bytes(8, "big")
    if mask:
        return bytes(head) + mask + _unmask(payload, mask)
    return bytes(head) + payload


class WebSocket:
    """One accepted socket. `recv` yields whole data messages and answers control frames;
    sends are synchronous writes (asyncio's single thread keeps each frame whole), so a fan-out
    never waits on one slow phone — a peer that stops reading is cut off instead."""

    WRITE_LIMIT = 1024 * 1024

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.closed = False
        self._close_sent = False
        self.last_rx = time.monotonic()

    def _write(self, data: bytes) -> None:
        if self.closed:
            return
        transport = self.writer.transport
        if (
            transport.is_closing()
            or transport.get_write_buffer_size() > self.WRITE_LIMIT
        ):
            self.abort()
            return
        self.writer.write(data)

    def send_text(self, text: str) -> None:
        self._write(ws_frame(0x1, text.encode()))

    def send_json(self, obj: object) -> None:
        self.send_text(json.dumps(obj, separators=(",", ":")))

    def ping(self) -> None:
        self._write(ws_frame(0x9, b""))

    def close(self, code: int = 1000, reason: str = "") -> None:
        if not self._close_sent and not self.closed:
            self._close_sent = True
            self._write(ws_frame(0x8, code.to_bytes(2, "big") + reason.encode()[:120]))
        self.closed = True
        with contextlib.suppress(OSError, RuntimeError):
            self.writer.close()

    def abort(self) -> None:
        self.closed = True
        with contextlib.suppress(OSError, RuntimeError):
            self.writer.transport.abort()

    def _fail(self, code: int) -> WsClosed:
        self.close(code)
        return WsClosed(code)

    async def _frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = await self.reader.readexactly(2)
        fin, rsv, opcode = bool(b0 & 0x80), b0 & 0x70, b0 & 0x0F
        if rsv:
            raise self._fail(1002)
        if not b1 & 0x80:
            raise self._fail(1002)  # RFC 6455 §5.1: a client frame must be masked
        n = b1 & 0x7F
        if n == 126:
            n = int.from_bytes(await self.reader.readexactly(2), "big")
        elif n == 127:
            n = int.from_bytes(await self.reader.readexactly(8), "big")
            if n >> 63:
                raise self._fail(1002)
        if opcode >= 0x8 and (not fin or n > 125):
            raise self._fail(1002)
        if n > MAX_WS_MESSAGE:
            raise self._fail(1009)
        mask = await self.reader.readexactly(4)
        payload = _unmask(await self.reader.readexactly(n), mask)
        self.last_rx = time.monotonic()
        return fin, opcode, payload

    async def recv(self) -> tuple[int, bytes]:
        """The next text (1) or binary (2) message. Raises WsClosed when the socket ends."""
        message: bytearray | None = None
        kind = 0
        try:
            while True:
                fin, opcode, payload = await self._frame()
                if opcode == 0x8:
                    code = 1000
                    if len(payload) == 1:
                        raise self._fail(1002)
                    if len(payload) >= 2:
                        code = int.from_bytes(payload[:2], "big")
                        if (
                            code < 1000
                            or code in (1004, 1005, 1006, 1015)
                            or 1012 < code < 3000
                        ):
                            raise self._fail(1002)
                    self.close(code)
                    raise WsClosed(code)
                if opcode == 0x9:
                    self._write(ws_frame(0xA, payload))
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2):
                    if message is not None:
                        raise self._fail(1002)
                    message, kind = bytearray(payload), opcode
                elif opcode == 0x0:
                    if message is None:
                        raise self._fail(1002)
                    message += payload
                else:
                    raise self._fail(1002)
                if len(message) > MAX_WS_MESSAGE:
                    raise self._fail(1009)
                if fin:
                    data = bytes(message)
                    if kind == 0x1:
                        try:
                            data.decode("utf-8")
                        except UnicodeDecodeError:
                            raise self._fail(1007) from None
                    return kind, data
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self.abort()
            raise WsClosed(1006) from None


# ── the relay ─────────────────────────────────────────────────────────────────


@dataclass
class Channel:
    """One pairing code's main channel or its `-agent` side channel. `body` is the chip (or, on
    the side channel, the creature page that said `hello`); `peers` are phones (or agents)."""

    name: str
    body: WebSocket | None = None
    body_hb: bool = False
    peers: set[WebSocket] = field(default_factory=set)

    def status(self) -> dict[str, object]:
        return {"t": "status", "awake": 1 if self.body is not None else 0}

    def to_peers(self, text: str) -> None:
        for peer in list(self.peers):
            peer.send_text(text)


def _parse(data: bytes) -> dict[str, Any] | None:
    try:
        msg = json.loads(data)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def _without_key(hello: dict[str, Any]) -> dict[str, Any]:
    """A chip's hello as phones may see it: the device key never leaves the relay."""
    return {k: v for k, v in hello.items() if k != "key"}


# ── the app ───────────────────────────────────────────────────────────────────


class GrowBot:
    def __init__(self, config: Config, sessions: Sessions | None = None) -> None:
        self.config = config
        self.sessions = sessions or Sessions()
        self.csp = build_csp(config.site)
        self.channels: dict[str, Channel] = {}
        self.sockets = 0
        self._models: list[dict[str, Any]] = []
        self._models_at = -math.inf
        self.did = hashlib.sha256(b"growbot-on-the-box").hexdigest()[:16]

    # -- outbound ---------------------------------------------------------------

    def _auth(self) -> dict[str, str]:
        try:
            token = self.config.token_file.read_text().strip()
        except OSError:
            token = ""
        if not token:
            _warn_once(
                "token",
                f"no api token at {self.config.token_file} yet; the api will refuse",
            )
            return {}
        _warned.discard("token")
        return {"Authorization": f"Bearer {token}"}

    def _api_post(
        self, path: str, payload: dict[str, Any], timeout: float = 10.0
    ) -> Upstream:
        headers = {"Content-Type": "application/json", **self._auth()}
        up = http_call(
            self.config.api_url + path,
            json.dumps(payload).encode(),
            headers,
            "POST",
            timeout,
        )
        if up.status == 401:
            _warn_once(f"401:{path}", f"the api refused {path} (401)")
        return up

    def _ok(self, up: Upstream) -> bool:
        if up.status != 200:
            return False
        try:
            return json.loads(up.body).get("ok") is True
        except (ValueError, AttributeError):
            return False

    def models(self) -> list[dict[str, Any]]:
        """The box's installed models, as the api lists them. A failed read keeps the last good
        list, so a blip in the api never empties the creature's brain picker."""
        if time.monotonic() - self._models_at < MODELS_CACHE_S:
            return self._models
        up = http_call(
            self.config.api_url + "/internal/growbot/llm/v1/models",
            headers=self._auth(),
        )
        if up.status != 200:
            return self._models
        try:
            rows = json.loads(up.body).get("data")
        except (ValueError, AttributeError):
            return self._models
        if isinstance(rows, list):
            self._models = [
                {"id": r["id"], "vision": r.get("vision") is True}
                for r in rows
                if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]
            ]
            self._models_at = time.monotonic()
        return self._models

    def default_model(self, models: list[dict[str, Any]]) -> str:
        vision = [m["id"] for m in models if m["vision"]]
        for name in PREFERRED_MODELS:
            if name in vision:
                return name
        if vision:
            return vision[0]
        if models:
            return models[0]["id"]
        return self.config.default_model

    # -- dispatch ---------------------------------------------------------------

    def _base_headers(self) -> list[tuple[str, str]]:
        return [
            ("Content-Security-Policy", self.csp),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Permissions-Policy", PERMISSIONS_POLICY),
        ]

    def _authed(self, req: Request) -> bool:
        value = cookie_value(req.headers, SESSION_COOKIE)
        return bool(value) and self.sessions.valid(value)

    def _same_origin(self, req: Request) -> bool:
        """A request a browser sent from another origin (the PWA on :443 is same-site, so
        SameSite=Strict alone lets its cookie through) is refused for anything that acts."""
        origin = req.header("origin")
        return not origin or origin == f"https://{req.header('host')}"

    async def handle(self, req: Request) -> Response:
        if req.path == "/open":
            return await self.open(req)
        if not self._authed(req):
            return Response(401, UNAUTHORISED_PAGE, "text/html; charset=utf-8")
        if req.path.startswith("/api/"):
            if req.method not in ("GET", "HEAD") and not self._same_origin(req):
                return json_response(403, {"error": "cross-origin"})
            return await self.api(req)
        if req.method not in ("GET", "HEAD"):
            return Response(
                405, b"method not allowed", headers=[("Allow", "GET, HEAD")]
            )
        return await self.static(req)

    async def open(self, req: Request) -> Response:
        ticket, code = req.arg("ticket"), req.arg("id")
        if (
            req.method != "GET"
            or not (0 < len(ticket) <= 512)
            or not CODE_RE.fullmatch(code)
        ):
            return Response(400, b"bad link", headers=[("Cache-Control", "no-store")])
        up = await asyncio.to_thread(
            self._api_post, "/internal/growbot/ticket/redeem", {"ticket": ticket}
        )
        if not self._ok(up):
            return Response(401, UNAUTHORISED_PAGE, "text/html; charset=utf-8")
        cookie = (
            f"{SESSION_COOKIE}={self.sessions.mint()}; HttpOnly; Secure; SameSite=Strict; "
            f"Path=/; Max-Age={SESSION_TTL_S}"
        )
        where = f"/v3/next.html?id={urllib.parse.quote(code)}&mode=legs"
        return Response(302, b"", headers=[("Set-Cookie", cookie), ("Location", where)])

    # -- static -----------------------------------------------------------------

    def resolve(self, raw_path: str) -> Path | None:
        try:
            path = urllib.parse.unquote(raw_path, errors="strict")
        except UnicodeDecodeError:
            return None
        if "\x00" in path or "\\" in path:
            return None
        parts = [p for p in path.split("/") if p]
        # Dot segments are traversal and dotfiles are never part of the app.
        if any(p.startswith(".") for p in parts):
            return None
        root = self.config.site.resolve()
        try:
            full = root.joinpath(*parts).resolve()
        except (OSError, ValueError):
            return None
        if not full.is_relative_to(root):
            return None
        if full.is_dir():
            full = full / "index.html"
        return full if full.is_file() else None

    async def static(self, req: Request) -> Response:
        full = self.resolve(req.path)
        if full is None:
            return Response(404, b"not found")
        st = full.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        headers = [("Cache-Control", "no-cache"), ("ETag", etag)]
        if req.header("if-none-match") == etag:
            return Response(304, headers=headers)
        ctype = CONTENT_TYPES.get(full.suffix.lower()) or (
            mimetypes.guess_type(full.name)[0] or "application/octet-stream"
        )
        return Response(
            200,
            ctype=ctype,
            headers=headers,
            stream=_file_chunks(full),
            length=st.st_size,
        )

    # -- the /api shim ------------------------------------------------------------

    async def api(self, req: Request) -> Response:
        name = req.path[len("/api/") :]
        if name in ("journal", "episodes", "gallery", "ev", "hit", "feedback"):
            # Upstream's server-side telemetry and cloud stores: answered, kept nowhere.
            return Response(204)
        if name == "config":
            return await self.config_route()
        if name == "models":
            return await self.models_route()
        if name == "device":
            return json_response(200, self.device())
        if name in ("redeem", "transfer"):
            return json_response(410, {"ok": False, "error": "not on this box"})
        if req.method != "POST":
            return json_response(405, {"error": "method not allowed"})
        if name == "llm":
            target = req.arg("target")
            if target == "openrouter":
                return await self.llm(req, stream=False)
            if target in ("groq-audio", "deepinfra-audio"):
                return await self.stt(req)
            return json_response(400, {"error": "unknown target"})
        if name == "llm-stream":
            if req.arg("target") != "openrouter":
                return json_response(400, {"error": "unknown target"})
            return await self.llm(req, stream=True)
        if name == "fish-tts":
            return await self.tts(req)
        return json_response(404, {"error": "not found"})

    def device(self) -> dict[str, object]:
        """One owned device with nothing metered: upstream's credit and adoption UI reads these."""
        return {
            "ok": True,
            "token": "box",
            "did": self.did,
            "hasPass": True,
            "balanceUsd": 0,
            "spentUsd": 0,
            "freeLeftUsd": 0,
            "freeLifetimeUsd": 0,
            "totalPurchased": 0,
        }

    async def config_route(self) -> Response:
        models = await asyncio.to_thread(self.models)
        pick = self.default_model(models)
        return json_response(
            200,
            {
                "fast": pick,
                "active": pick,
                "dream": pick,
                "fallback": pick,
                "wall": "",
                "raw": 0,
            },
        )

    async def models_route(self) -> Response:
        models = await asyncio.to_thread(self.models)
        rows = [
            {
                "id": m["id"],
                "name": m["id"],
                "vision": m["vision"],
                "callEst": 0,
                "intel": None,
                "reasoning": False,
                "mandatory": False,
                "efforts": [],
            }
            for m in models
        ]
        return json_response(200, {"simple": rows, "all": rows})

    def llm_body(
        self, raw: bytes, stream: bool, models: list[dict[str, Any]]
    ) -> bytes | None:
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return None
        out = {k: body[k] for k in LLM_FIELDS if k in body}
        # Upstream falls back to cloud slugs (its default, the free-lane pin, a ?model= trial);
        # on the box anything not installed is the box's default.
        names = {m["id"] for m in models}
        model = body.get("model")
        out["model"] = (
            model
            if isinstance(model, str) and model in names
            else self.default_model(models)
        )
        out["stream"] = stream
        return json.dumps(out).encode()

    async def llm(self, req: Request, *, stream: bool) -> Response:
        models = await asyncio.to_thread(self.models)
        body = self.llm_body(req.body, stream, models)
        if body is None:
            return json_response(400, {"error": {"message": "bad chat body"}})
        url = self.config.api_url + "/internal/growbot/llm/v1/chat/completions"
        headers = {"Content-Type": "application/json", **self._auth()}
        return await stream_upstream(url, body, headers, sse=stream)

    async def stt(self, req: Request) -> Response:
        parts = parse_multipart(req.body, req.header("content-type"))
        audio = next((p for p in parts if p.name == "file" and p.data), None)
        if audio is None:
            return json_response(400, {"error": "no audio"})
        fields = {
            "model": "whisper",
            "response_format": "verbose_json",
            "temperature": "0",
        }
        for part in parts:
            value = part.data.decode("utf-8", "replace").strip()
            if part.name == "language" and LANG_RE.fullmatch(value):
                fields["language"] = value
            elif part.name == "prompt" and value:
                fields["prompt"] = value[:1000]
            elif part.name == "temperature" and re.fullmatch(
                r"(0|1)(\.\d{1,3})?", value
            ):
                fields["temperature"] = value
        body, ctype = build_multipart(fields, audio)
        up = await asyncio.to_thread(
            http_call,
            self.config.stt_url + "/audio/transcriptions",
            body,
            {"Content-Type": ctype},
            "POST",
            60.0,
        )
        if up.status != 200:
            # Not 429: upstream reads that as a spent daily allowance and stops listening.
            return json_response(503, {"error": "stt unavailable"})
        return json_response(200, normalise_transcript(up.body))

    async def tts(self, req: Request) -> Response:
        try:
            body = json.loads(req.body)
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            return json_response(400, {"error": "no text"})
        text = body["text"].strip()[:2000]
        if not text:
            return json_response(400, {"error": "no text"})
        params = {"text": text}
        voice = body.get("voice")
        # An empty or non-Kokoro id (upstream's fish voice ids) is Kokoro's default voice.
        if isinstance(voice, str) and KOKORO_VOICE_RE.fullmatch(voice):
            params["voice"] = voice
        for key, lo, hi in (("speed", 0.5, 2.0), ("pitch", -12.0, 12.0)):
            value = body.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                params[key] = f"{max(lo, min(hi, float(value))):.3f}"
        url = self.config.tts_url + "/tts?" + urllib.parse.urlencode(params)
        up = await asyncio.to_thread(http_call, url, None, None, "GET", 60.0)
        if up.status != 200 or not up.body:
            # Never 401/402: upstream latches the voice dead for the session on those.
            return json_response(503, {"error": "tts unavailable"})
        return Response(200, up.body, "audio/wav", [("Cache-Control", "no-store")])

    # -- the relay ----------------------------------------------------------------

    def _channel(self, name: str) -> Channel:
        chan = self.channels.get(name)
        if chan is None:
            chan = self.channels[name] = Channel(name)
        return chan

    def _drop_empty(self, chan: Channel) -> None:
        if (
            chan.body is None
            and not chan.peers
            and self.channels.get(chan.name) is chan
        ):
            del self.channels[chan.name]

    async def relay(
        self, req: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> Response | None:
        """Upgrade `/d/<code>` or `/d/<code>-agent`. Returns a Response to refuse, None once the
        socket has run its course."""
        seg = urllib.parse.unquote(req.path[len("/d/") :])
        agent = seg.endswith("-agent")
        code = seg[: -len("-agent")] if agent else seg
        if not CODE_RE.fullmatch(code):
            return Response(404, b"not found")
        authed = self._authed(req)
        if agent and not authed:
            return Response(401, UNAUTHORISED_PAGE, "text/html; charset=utf-8")
        if authed and req.header("origin") != f"https://{req.header('host')}":
            return Response(403, b"cross-origin")
        if req.method != "GET" or "websocket" not in req.tokens("upgrade"):
            return Response(426, b"websocket only", headers=[("Upgrade", "websocket")])
        if "upgrade" not in req.tokens("connection"):
            return Response(400, b"bad upgrade")
        if req.header("sec-websocket-version") != "13":
            return Response(
                426, b"websocket 13", headers=[("Sec-WebSocket-Version", "13")]
            )
        key = req.header("sec-websocket-key")
        try:
            if len(base64.b64decode(key, validate=True)) != 16:
                raise ValueError
        except ValueError:
            return Response(400, b"bad websocket key")
        if self.sockets >= MAX_SOCKETS:
            return Response(503, b"busy")
        head = "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        head += f"Sec-WebSocket-Accept: {ws_accept(key)}\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in self._base_headers()) + "\r\n"
        writer.write(head.encode())
        await writer.drain()
        ws = WebSocket(reader, writer)
        self.sockets += 1
        keepalive = asyncio.create_task(self._keepalive(ws))
        try:
            if agent:
                await self._agent_socket(ws, self._channel(seg))
            elif authed:
                await self._phone(ws, self._channel(code))
            else:
                await self._chip(ws, self._channel(code), code)
        except WsClosed:
            pass
        finally:
            keepalive.cancel()
            self.sockets -= 1
            if not ws.closed:
                ws.close(1000)
        return None

    async def _keepalive(self, ws: WebSocket) -> None:
        while not ws.closed:
            await asyncio.sleep(WS_PING_EVERY_S)
            if time.monotonic() - ws.last_rx > WS_DEAD_AFTER_S:
                ws.abort()
                return
            ws.ping()

    async def _first(self, ws: WebSocket) -> dict[str, Any] | None:
        try:
            kind, data = await asyncio.wait_for(ws.recv(), FIRST_FRAME_TIMEOUT_S)
        except TimeoutError:
            ws.close(1008, "say hello")
            return None
        return _parse(data) if kind == 0x1 else None

    async def _phone(self, ws: WebSocket, chan: Channel) -> None:
        if len(chan.peers) >= MAX_PEERS_PER_CHANNEL:
            ws.close(1013, "too many phones")
            return
        chan.peers.add(ws)
        ws.send_json(chan.status())
        try:
            while True:
                kind, data = await ws.recv()
                if kind == 0x1 and chan.body is not None:
                    chan.body.send_text(data.decode())
        finally:
            chan.peers.discard(ws)
            self._drop_empty(chan)

    async def _chip(self, ws: WebSocket, chan: Channel, code: str) -> None:
        hello = await self._first(ws)
        key = hello.get("key") if hello else None
        if (
            not hello
            or hello.get("t") != "hello"
            or hello.get("id") != code
            or not isinstance(key, str)
            or not 0 < len(key) <= 256
        ):
            ws.close(1008, "hello with a key")
            return
        up = await asyncio.to_thread(
            self._api_post, "/internal/growbot/robot/verify", {"code": code, "key": key}
        )
        if not self._ok(up):
            ws.close(1008, "unknown robot")
            return
        caps = hello.get("caps")
        hb = isinstance(caps, list) and "hb" in caps
        await self._attach_body(ws, chan, hb, chip=True, hello=_without_key(hello))

    async def _attach_body(
        self,
        ws: WebSocket,
        chan: Channel,
        hb: bool,
        *,
        chip: bool,
        hello: dict[str, Any] | None = None,
    ) -> None:
        old = chan.body
        chan.body, chan.body_hb = ws, hb
        if old is not None:
            old.close(1000, "replaced")
        chan.to_peers(json.dumps(chan.status(), separators=(",", ":")))
        if hello is not None:
            chan.to_peers(json.dumps(hello, separators=(",", ":")))
        alive = asyncio.create_task(self._alive(ws, chan)) if hb and chip else None
        try:
            while True:
                kind, data = await ws.recv()
                if kind != 0x1:
                    continue
                msg = _parse(data) if chip else None
                if msg is not None and msg.get("t") == "hb":
                    ws.send_text('{"t":"hb_ack"}')
                elif msg is not None and msg.get("t") == "hello":
                    chan.to_peers(json.dumps(_without_key(msg), separators=(",", ":")))
                else:
                    chan.to_peers(data.decode())
        finally:
            if alive is not None:
                alive.cancel()
            if chan.body is ws:
                chan.body = None
                chan.body_hb = False
                chan.to_peers(json.dumps(chan.status(), separators=(",", ":")))
            self._drop_empty(chan)

    async def _alive(self, ws: WebSocket, chan: Channel) -> None:
        while chan.body is ws and not ws.closed:
            await asyncio.sleep(ALIVE_EVERY_S)
            if chan.body is ws:
                quiet = int((time.monotonic() - ws.last_rx) * 1000)
                chan.to_peers(
                    json.dumps({"t": "alive", "quiet_ms": quiet}, separators=(",", ":"))
                )

    async def _agent_socket(self, ws: WebSocket, chan: Channel) -> None:
        """The side channel: the creature page says `hello` and is its body; agents attach as
        peers. Both are behind the session cookie; frames are bridged both ways."""
        first = await self._first(ws)
        if first is None:
            ws.close(1008, "say hello or attach")
            return
        if first.get("t") == "hello":
            await self._attach_body(ws, chan, False, chip=False)
            return
        if len(chan.peers) >= MAX_PEERS_PER_CHANNEL:
            ws.close(1013, "too many agents")
            return
        chan.peers.add(ws)
        ws.send_json(chan.status())
        if chan.body is not None:
            chan.body.send_json(first)
        try:
            while True:
                kind, data = await ws.recv()
                if kind == 0x1 and chan.body is not None:
                    chan.body.send_text(data.decode())
        finally:
            chan.peers.discard(ws)
            self._drop_empty(chan)

    # -- connections ------------------------------------------------------------

    async def write(
        self,
        writer: asyncio.StreamWriter,
        req: Request | None,
        resp: Response,
        keep: bool,
    ) -> bool:
        head_only = req is not None and req.method == "HEAD"
        chunked = resp.stream is not None and resp.length is None
        if chunked and (req is None or req.version == "HTTP/1.0"):
            keep = False
        headers = [*self._base_headers(), *resp.headers]
        if not any(k.lower() == "cache-control" for k, _ in headers):
            headers.append(("Cache-Control", "no-store"))
        if resp.status not in (204, 304):
            headers.append(("Content-Type", resp.ctype))
            if chunked and keep:
                headers.append(("Transfer-Encoding", "chunked"))
            elif not chunked:
                size = resp.length if resp.length is not None else len(resp.body)
                headers.append(("Content-Length", str(size)))
        if not keep:
            headers.append(("Connection", "close"))
        reason = STATUS_TEXT.get(resp.status, "Unknown")
        lines = [f"HTTP/1.1 {resp.status} {reason}"] + [f"{k}: {v}" for k, v in headers]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        if resp.status in (204, 304) or head_only:
            if resp.stream is not None:
                await resp.stream.aclose()
            await writer.drain()
            return keep
        if resp.stream is None:
            writer.write(resp.body)
            await writer.drain()
            return keep
        try:
            async for chunk in resp.stream:
                if not chunk:
                    continue
                writer.write(
                    b"%x\r\n%s\r\n" % (len(chunk), chunk) if chunked and keep else chunk
                )
                await writer.drain()
            if chunked and keep:
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        finally:
            await resp.stream.aclose()
        return keep

    async def connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    req = await asyncio.wait_for(read_head(reader), IDLE_TIMEOUT_S)
                    keep = req.version == "HTTP/1.1" and "close" not in req.tokens(
                        "connection"
                    )
                    if req.path.startswith("/d/"):
                        refused = await self.relay(req, reader, writer)
                        if refused is not None:
                            await self.write(writer, req, refused, False)
                        return
                    await asyncio.wait_for(
                        read_body(req, reader, writer), BODY_TIMEOUT_S
                    )
                except HttpError as exc:
                    await self.write(
                        writer, None, Response(exc.status, b"bad request"), False
                    )
                    return
                except (
                    TimeoutError,
                    asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError,
                ):
                    return
                try:
                    resp = await self.handle(req)
                except Exception as exc:  # noqa: BLE001 — one broken request, never the server
                    print(
                        f"growbot: {req.method} {req.path} failed: {exc!r}",
                        file=sys.stderr,
                    )
                    resp = Response(500, b"internal error")
                if not await self.write(writer, req, resp, keep):
                    return
        except (ConnectionError, OSError):
            return
        finally:
            with contextlib.suppress(OSError, RuntimeError):
                writer.close()

    async def start(self, host: str = "0.0.0.0", port: int = 8810) -> asyncio.Server:
        return await asyncio.start_server(self.connection, host, port, limit=MAX_HEAD)


async def _file_chunks(
    path: Path, size: int = 256 * 1024
) -> AsyncGenerator[bytes, None]:
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while chunk := await asyncio.to_thread(handle.read, size):
            yield chunk
    finally:
        handle.close()


async def stream_upstream(
    url: str, body: bytes, headers: dict[str, str], *, sse: bool
) -> Response:
    """POST to the api and answer with what it says. With `sse`, a 200 event stream is relayed
    as it arrives (the creature speaks each sentence the moment it lands); anything else is
    buffered and returned whole. The blocking read runs on a thread that stops when the phone
    goes away."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    stop = threading.Event()

    def put(item: tuple[str, Any]) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            stop.set()

    def work() -> None:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            resp = _OPENER.open(req, timeout=UPSTREAM_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read()
            except OSError:
                data = b""
            put(
                (
                    "head",
                    (
                        exc.code,
                        exc.headers.get("Content-Type", "") if exc.headers else "",
                    ),
                )
            )
            put(("data", data))
            put(("end", None))
            return
        except (OSError, ValueError):
            put(("head", (503, "")))
            put(("end", None))
            return
        with resp:
            put(("head", (resp.status, resp.headers.get("Content-Type", ""))))
            try:
                while not stop.is_set():
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    put(("data", chunk))
            except (OSError, ValueError):
                pass
        put(("end", None))

    threading.Thread(target=work, daemon=True).start()
    _, (status, ctype) = await queue.get()
    streaming = sse and status == 200 and ctype.startswith("text/event-stream")

    async def chunks() -> AsyncGenerator[bytes, None]:
        try:
            while True:
                kind, value = await queue.get()
                if kind == "end":
                    return
                yield value
        finally:
            stop.set()

    if streaming:
        return Response(
            200,
            ctype="text/event-stream",
            headers=[("Cache-Control", "no-cache"), ("X-Accel-Buffering", "no")],
            stream=chunks(),
        )
    data = b"".join([chunk async for chunk in chunks()])
    if status in (401, 403):
        _warn_once("llm401", "the api refused the growbot token for the model lane")
        return json_response(
            502, {"error": {"message": "the box refused the brain call"}}
        )
    if status == 503 and not data:
        return json_response(
            503, {"error": {"message": "the box's brain is unreachable"}}
        )
    return Response(status, data, ctype or "application/json")


# ── multipart (the speech clip) ───────────────────────────────────────────────


@dataclass
class Part:
    name: str
    filename: str
    ctype: str
    data: bytes


def _param(header: str, name: str) -> str:
    match = re.search(rf'(?:^|;)\s*{name}="([^"]*)"', header) or re.search(
        rf"(?:^|;)\s*{name}=([^;\s]+)", header
    )
    return match.group(1) if match else ""


def parse_multipart(body: bytes, ctype: str) -> list[Part]:
    if not ctype.lower().startswith("multipart/form-data"):
        return []
    boundary = _param(ctype, "boundary")
    if not boundary or len(boundary) > 200:
        return []
    delim = b"--" + boundary.encode("latin-1")
    parts: list[Part] = []
    for chunk in body.split(delim)[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        disposition, part_type = "", "application/octet-stream"
        for line in head.decode("utf-8", "replace").split("\r\n"):
            key, _, value = line.partition(":")
            if key.strip().lower() == "content-disposition":
                disposition = value.strip()
            elif key.strip().lower() == "content-type":
                part_type = value.strip()
        name = _param(disposition, "name")
        if name:
            parts.append(Part(name, _param(disposition, "filename"), part_type, data))
    return parts


def build_multipart(fields: dict[str, str], audio: Part) -> tuple[bytes, str]:
    boundary = "growbot" + secrets.token_hex(16)
    out = bytearray()
    for key, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += value.encode() + b"\r\n"
    filename = re.sub(r"[^A-Za-z0-9._-]", "", audio.filename)[:64] or "speech.webm"
    ctype = (
        re.sub(r"[^A-Za-z0-9./;=+ -]", "", audio.ctype)[:100]
        or "application/octet-stream"
    )
    out += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode()
    out += audio.data + f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _num(value: object) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return float(value)
    return None


def normalise_transcript(raw: bytes) -> dict[str, Any]:
    """whisper.cpp builds differ in what verbose_json carries; upstream reads Groq's shape: `text`,
    and per segment `no_speech_prob` (its non-speech gate) and `avg_logprob` (its confidence
    log). A missing probability reads as speech, so a build that omits it never makes the
    creature deaf."""
    try:
        body: object = json.loads(raw)
    except ValueError:
        return {"text": raw.decode("utf-8", "replace").strip(), "segments": []}
    if isinstance(body, str):
        return {"text": body.strip(), "segments": []}
    if not isinstance(body, dict):
        return {"text": "", "segments": []}
    segments: list[dict[str, Any]] = []
    rows = body.get("segments")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        avg = _num(row.get("avg_logprob"))
        if avg is None:
            probs = [
                p
                for w in row.get("words") or []
                if isinstance(w, dict) and (p := _num(w.get("probability"))) is not None
            ]
            avg = (
                sum(math.log(max(p, 1e-6)) for p in probs) / len(probs)
                if probs
                else 0.0
            )
        nsp = _num(row.get("no_speech_prob"))
        seg: dict[str, Any] = {
            "text": row.get("text") if isinstance(row.get("text"), str) else "",
            "start": _num(row.get("start")) or 0.0,
            "end": _num(row.get("end")) or 0.0,
            "avg_logprob": avg,
            "no_speech_prob": min(1.0, max(0.0, nsp)) if nsp is not None else 0.0,
        }
        ratio = _num(row.get("compression_ratio"))
        if ratio is not None:
            seg["compression_ratio"] = ratio
        segments.append(seg)
    joined = body.get("text")
    text = (joined.strip() if isinstance(joined, str) else "") or " ".join(
        s["text"].strip() for s in segments if s["text"].strip()
    )
    out: dict[str, Any] = {"text": text, "segments": segments}
    if isinstance(body.get("language"), str):
        out["language"] = body["language"]
    duration = _num(body.get("duration"))
    if duration is not None:
        out["duration"] = duration
    return out


async def main() -> None:
    config = Config.from_env()
    app = GrowBot(config)
    port = int(os.environ.get("GROWBOT_PORT", "8810"))
    server = await app.start("0.0.0.0", port)
    print(f"growbot: serving {config.site} on :{port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
