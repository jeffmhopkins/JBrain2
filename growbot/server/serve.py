#!/usr/bin/env python3
"""The GrowBot sidecar: the creature's web app, its /api shim and the robot relay, on the box.

One asyncio process behind Caddy's LAN site `https://<lan host>:8444` (docs/plans/GROWBOT_PLAN.md
§2), and its away-from-home name `growbot.<host>` through the tunnel (growbot/README.md). It serves `GROWBOT_SITE` as the web root, answers the upstream app's `/api/*` calls from the
box (the api's model lane, tts-stt's Kokoro and whisper, and fixed answers for the cloud-only
routes), and relays WebSocket frames between the phone and the robot at `/d/<code>`.

Security, in order of the request:
- `/open?ticket=…` is the only door without a session: the api redeems the one-time ticket the
  PWA minted, and the answer is a `gb_session` cookie signed with a key derived from
  `growbot.token`, so a restart keeps phones signed in and a re-minted token signs them all out
  (nothing is stored). It lasts 30 days and is renewed as the creature loads, so the installed
  app on the robot's phone stays open; `/start` (the manifest's start page) sends a phone whose
  session has lapsed to `/setup.html`, which scans a fresh ticket off JBrain's GrowBot screen.
- The installed app's manifest, its icons and its service worker, the setup page and the box's
  root certificate (`/box-root.crt`, public by nature) are the only things served without a
  session: Chrome fetches the manifest and icons with no cookie, and the setup page is how a
  phone signed in to nothing gets the certificate and its first ticket.
- Every other route needs that cookie, except the robot's side of the relay, which has no
  browser: it proves itself with the device key in its `hello`, checked against the api.
- `/internal/robots` (which robots are on the relay) and `/internal/robots/drop` (close a
  revoked robot's socket) are the api's, behind `growbot.token` as a bearer. Caddy's site 404s
  `/internal*`, so only a container that can reach this one and holds the token gets there.
- UDP `GROWBOT_NTP_PORT` (8123) answers SNTP for the robots, which have no internet and must
  set their clock before they can check the box's certificate dates. Stateless, one 48-byte
  reply to one well-formed 48-byte client request and nothing else, so it amplifies nothing.
- Every response carries a CSP that confines the page to this origin, so vendored code cannot
  reach another host even if a patch missed a URL. Outbound, this process talks to the api and
  tts-stt by compose name only, with proxies and redirects disabled.

Stdlib only: HTTP/1.1 and the RFC 6455 server side are implemented here, so the image is a bare
python:3.12-slim with no pip step.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import functools
import hashlib
import hmac
import html
import http.client
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

SESSION_COOKIE = "gb_session"
SESSION_TTL_S = 30 * 86400
# Renewed at most once a day, so a page that reloads often is not a cookie per load.
SESSION_RENEW_S = 86400
PUBLIC_PATHS = frozenset(
    (
        "/manifest.webmanifest",
        "/sw.js",
        "/icons/growbot-192.png",
        "/icons/growbot-512.png",
        # The setup page a phone signed in to nothing opens from the first QR: it fetches the
        # box's certificate and scans the second QR (a ticket) shown on JBrain's GrowBot screen.
        "/setup.html",
        "/setup.js",
        # The page's first script on every page, setup.html too; it holds no secret and is what
        # registers the service worker, so a phone can install the app from the setup page.
        "/growbot-box.js",
    )
)
ROOT_CERT_PATH = "/box-root.crt"
ROOT_CERT_CACHE_S = 300.0
# A failed read is remembered too, briefly: the route needs no session, so without it every
# LAN request on a box with no certificate would take an api call from the pool /open uses.
ROOT_CERT_RETRY_S = 15.0
SESSIONLESS = PUBLIC_PATHS | {"/open", "/start", ROOT_CERT_PATH}

MAX_HEAD = 16 * 1024
MAX_HEADERS = 100
# Camera frames ride the chat body as base64 data URLs and speech clips as multipart; both stay
# well under this, and nothing larger has a reason to reach the box's model or STT.
MAX_BODY = 16 * 1024 * 1024
MAX_WS_MESSAGE = 256 * 1024
IDLE_TIMEOUT_S = 75.0
BODY_TIMEOUT_S = 60.0
UPSTREAM_TIMEOUT_S = 120.0
# A clip is seconds long; a speech model that takes this long is stuck, not working.
STT_TIMEOUT_S = 60.0
FIRST_FRAME_TIMEOUT_S = 10.0
WS_PING_EVERY_S = 20.0
# Upstream's relay drops a chip 45 s after its last heartbeat (relay_chip.py, "0923 RELAY
# GHOST"): a robot that lost power never closes its socket, so silence is the only signal.
WS_DEAD_AFTER_S = 45.0
ALIVE_EVERY_S = 30.0
MAX_SOCKETS = 256
MAX_CONNECTIONS = 512
# Cookieless sockets are robots until their hello says otherwise; a few is every robot a house
# has, and the budget is what keeps an anonymous LAN client from holding the rest.
MAX_UNAUTH_SOCKETS = 16
# Outbound calls run on pools of their own, apart from the default pool that reads static
# files, so a slow model never stops the page from loading. The long ones (a brain call holds
# a worker up to 120 s, speech up to 60 s) have one pool; the model list another; and the two
# doors, ticket redeem and robot verify, a small one nothing else can fill.
STREAM_WORKERS = 8
API_WORKERS = 2
GATE_WORKERS = 2
# Failed /open tickets and robot hellos per client before it waits out the window.
BACKOFF_LIMIT = 8
BACKOFF_WINDOW_S = 300.0
MAX_PEERS_PER_CHANNEL = 8
MODELS_CACHE_S = 30.0
# How often a phone on the relay is asked again whether its persona still wears the body.
RIDER_RECHECK_S = 10.0
SOUL_WORKERS = 2
# A soul the phone pushes: the api's 1 MB soul cap, plus room for the JSON around it.
MAX_SOUL_BODY = 1_100_000
SOUL_PATH = "/api/box/persona"
# A persona's visual memories (wave G4d): one record with up to three views, each at most
# upstream's VM_MAX_B64 of base64, and its fields.
MEMORY_PATH = "/api/box/memories"
MAX_MEMORY_BODY = 2_500_000
MEMORY_ID_RE = re.compile(r"r[0-9a-f]{1,32}")

# Upstream's pairing codes are `gb-` + 6 hex; the api holds robots to this same shape.
CODE_RE = re.compile(r"gb-[A-Za-z0-9]{1,32}")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The api's growbot_phones id: what secrets.token_urlsafe(24) makes, within the table's CHECK.
PHONE_RE = re.compile(r"[A-Za-z0-9_-]{16,43}")
HASH_RE = re.compile(r"sha(256|384|512)-[A-Za-z0-9+/]+={0,2}")
KOKORO_VOICE_RE = re.compile(r"kokoro-[a-z0-9_]{1,40}")
LANG_RE = re.compile(r"[a-z]{2,3}")
# A camera frame is inline; any other image URL would have the model's engine fetch it.
IMAGE_URL_RE = re.compile(r"data:image/[a-z0-9.+-]{1,20};base64,[A-Za-z0-9+/=\s]*")
TOKEN_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
LENGTH_RE = re.compile(r"[0-9]{1,10}")
CHUNK_RE = re.compile(rb"[0-9A-Fa-f]{1,8}")

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
    429: "Too Many Requests",
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


def _ip(value: str) -> str | None:
    """`value` as a canonical IP address, or None when it is not one."""
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


# ── configuration ─────────────────────────────────────────────────────────────


@dataclass
class Config:
    site: Path
    api_url: str
    tts_url: str
    stt_url: str
    token_file: Path
    session_file: Path | None = None
    default_model: str = "qwen3-vl-30b"
    # The one peer whose X-Real-IP is believed (Caddy sets it from the client it saw).
    trusted_proxy: str = "proxy"
    # The tunnel connector: when Caddy saw it, Cloudflare's Cf-Connecting-IP names the client.
    trusted_tunnel: str = "cloudflared"

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
            session_file=Path(
                env("GROWBOT_SESSION_FILE", "/run/jbrain-growbot/growbot-session.token")
            ),
            default_model=env("GROWBOT_DEFAULT_MODEL", "qwen3-vl-30b"),
            trusted_proxy=env("GROWBOT_TRUSTED_PROXY", "proxy"),
            trusted_tunnel=env("GROWBOT_TRUSTED_TUNNEL", "cloudflared"),
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
            "form-action 'self'",
            "object-src 'none'",
        )
    )


PERMISSIONS_POLICY = (
    "camera=(self), microphone=(self), accelerometer=(self), gyroscope=(self), "
    "magnetometer=(self), screen-wake-lock=(self), geolocation=(), payment=(), usb=()"
)


# ── the session cookie ────────────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass(frozen=True)
class Claims:
    """What a valid `gb_session` says. `robot` is the robot code the api says the ticket was
    minted for, or empty. `persona` and `phone` are set by a G4 ticket (plan §2c): the persona
    this phone opened, whose soul lives on the box, and the phone's own id, which the api uses
    to tell which phone holds a persona and which one drives a body. Both empty for G2's (a
    phone that once held a persona keeps its id under a G2 ticket, with no persona)."""

    robot: str
    persona: str = ""
    phone: str = ""


class Sessions:
    """`gb_session` = `<expiry>.<nonce>.<robot>.<persona>.<phone>.<hmac>`: nothing to store or
    leak. The four-part `<expiry>.<nonce>.<robot>.<hmac>` of a G2 ticket is still read.

    For a G2 session `<robot>` is the only robot this phone's relay sockets may join: pairing
    is JBrain's (owner, 2026-11-10), so a code typed into the creature's own pairing box opens
    nothing. A persona session joins the robot its persona wears now (the relay asks the api).

    The key is derived from `growbot.token` when there is one, so sessions outlive a restart
    and die with a re-minted token; without it, a key made at start (a restart signs out)."""

    def __init__(
        self,
        key: bytes | None = None,
        key_file: Path | None = None,
        epoch_file: Path | None = None,
    ) -> None:
        self._fixed = key
        self._key_file = key_file
        # `growbot-session.token`: the api re-mints it when the owner signs every phone out
        # (PWA or debug API), which changes the key and so ends every session at once.
        self._epoch_file = epoch_file
        self._random = secrets.token_bytes(32)
        self._derived: tuple[str, bytes] = ("", b"")

    @staticmethod
    def _read(path: Path | None) -> str | None:
        if path is None:
            return ""
        try:
            return path.read_text().strip()
        except FileNotFoundError:
            return ""
        except OSError:
            return None

    def _key(self) -> bytes:
        if self._fixed:
            return self._fixed
        if self._key_file is not None:
            token, epoch = self._read(self._key_file), self._read(self._epoch_file)
            if token is None or epoch is None:
                # A read that failed (not a missing file) keeps the last key, so a passing
                # I/O error does not sign every phone out.
                return self._derived[1] or self._random
            if token:
                seed = f"{token}\n{epoch}"
                if self._derived[0] != seed:
                    # Domain-separated, so the cookie key is never the api's bearer itself.
                    key = hmac.new(
                        token.encode(), b"gb_session v1\n" + epoch.encode(), hashlib.sha256
                    ).digest()
                    self._derived = (seed, key)
                return self._derived[1]
        return self._random

    def _sign(self, body: str) -> str:
        return _b64url(hmac.new(self._key(), body.encode(), hashlib.sha256).digest())

    def mint(
        self, now: float | None = None, robot: str = "", persona: str = "", phone: str = ""
    ) -> str:
        expiry = int(time.time() if now is None else now) + SESSION_TTL_S
        if robot and not CODE_RE.fullmatch(robot):
            raise ValueError("robot code")
        if (persona and not (phone and UUID_RE.fullmatch(persona))) or (
            phone and not PHONE_RE.fullmatch(phone)
        ):
            raise ValueError("persona or phone")
        body = f"{expiry}.{secrets.token_urlsafe(12)}.{robot}"
        if phone:
            body += f".{persona}.{phone}"
        return f"{body}.{self._sign(body)}"

    def claims(self, value: str, now: float | None = None) -> Claims | None:
        """What a valid session says, or None if it is not one."""
        parts = value.split(".")
        if len(parts) not in (4, 6) or not parts[0].isdigit() or len(parts[0]) > 12:
            return None
        if not hmac.compare_digest(self._sign(".".join(parts[:-1])), parts[-1]):
            return None
        if int(parts[0]) <= (time.time() if now is None else now):
            return None
        if len(parts) == 6:
            return Claims(parts[2], parts[3], parts[4])
        return Claims(parts[2])

    def valid(self, value: str, now: float | None = None) -> bool:
        return self.claims(value, now) is not None

    def robot(self, value: str, now: float | None = None) -> str | None:
        """The robot a valid session was opened for ("" for none), or None if not valid."""
        got = self.claims(value, now)
        return got.robot if got else None

    def due(self, value: str, now: float | None = None) -> bool:
        """A valid session more than a day old, which the next page load renews."""
        t = time.time() if now is None else now
        renew_before = t + SESSION_TTL_S - SESSION_RENEW_S
        return self.valid(value, t) and int(value.split(".")[0]) < renew_before


def _redeemed(up: Upstream) -> Claims | None:
    """What a redeemed ticket opens: its robot ('' for none) and, for a persona's ticket, the
    persona and the phone that now holds it. None if the api's answer is not that shape."""
    try:
        got = json.loads(up.body)
        code, persona, phone = got.get("code"), got.get("persona", ""), got.get("phone", "")
    except (ValueError, AttributeError):
        return None
    if not (code == "" or (isinstance(code, str) and CODE_RE.fullmatch(code))):
        return None
    if persona == "" and phone == "":
        return Claims(code)
    if (
        isinstance(persona, str)
        and isinstance(phone, str)
        and UUID_RE.fullmatch(persona)
        and PHONE_RE.fullmatch(phone)
    ):
        return Claims(code, persona, phone)
    return None


def phone_label(user_agent: str) -> str:
    """What the Phones tab calls a phone it has never seen, from its User-Agent: the kind of
    phone, never the string itself (the owner renames it there)."""
    for needle, label in (
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Android", "Android phone"),
    ):
        if needle in user_agent:
            return label
    return "Phone"


def _landing(where: str) -> bytes:
    """A page that moves on to `where` by itself; the link is for a browser that ignores the
    refresh. `where` is built here from a checked code, and escaped all the same."""
    where = html.escape(where, quote=True)
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<meta http-equiv=refresh content='0;url={where}'>"
        f"<title>GrowBot</title><p><a href='{where}'>Open GrowBot</a></p>"
    ).encode()


def _creature(code: str) -> str:
    """Without a robot the creature opens unpaired; with one, on it."""
    if code:
        return f"/v3/next.html?id={urllib.parse.quote(code)}&mode=legs"
    return "/v3/next.html?mode=legs"


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
    # For a 503 the call made itself: "timeout", "broken" (a cut or garbled answer) or "down"
    # (refused, reset, unresolvable), so the creature can say which, not one line for all.
    why: str = ""


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
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # HTTPException: a truncated or garbled answer (IncompleteRead, BadStatusLine).
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return Upstream(503, b"", "", "timeout")
        if isinstance(exc, (ValueError, http.client.HTTPException)):
            return Upstream(503, b"", "", "broken")
        return Upstream(503, b"", "", "down")


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
    peer: str = ""

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


def body_limit(req: Request) -> int:
    # A pushed soul is capped near the api's own cap, so an oversized one is refused before
    # its upload instead of after 16 MiB of it.
    if req.path == SOUL_PATH:
        return MAX_SOUL_BODY
    if req.path in (MEMORY_PATH, MEMORY_PATH + "/forget"):
        return MAX_MEMORY_BODY
    return MAX_BODY


def body_framing(req: Request) -> int | None:
    """The body's declared size: 0 for none, None for chunked. Raises before a byte of body
    is read, so a refused request never costs its upload."""
    lengths = {v for k, v in req.headers if k == "content-length"}
    chunked = [v for k, v in req.headers if k == "transfer-encoding"]
    if (chunked and lengths) or len(lengths) > 1:
        raise HttpError(400)
    if chunked:
        if len(chunked) != 1 or chunked[0].lower() != "chunked":
            raise HttpError(501)
        return None
    if not lengths:
        return 0
    value = next(iter(lengths))
    if not LENGTH_RE.fullmatch(value):
        raise HttpError(400)
    if int(value) > body_limit(req):
        raise HttpError(413)
    return int(value)


async def read_body(
    req: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    size = body_framing(req)
    if size == 0:
        return
    if req.header("expect").lower() == "100-continue":
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
    if size is not None:
        req.body = await reader.readexactly(size)
        return
    body = bytearray()
    while True:
        line = await reader.readuntil(b"\r\n")
        size_text = line[:-2].split(b";", 1)[0].strip()
        if not CHUNK_RE.fullmatch(size_text):
            raise HttpError(400)
        size = int(size_text, 16)
        if len(body) + size > body_limit(req):
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
        self.last_data = self.last_rx

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
                            or code in (1004, 1005, 1006)
                            or 1014 < code < 3000
                            or code > 4999
                        ):
                            raise self._fail(1002)
                        try:
                            payload[2:].decode("utf-8")
                        except UnicodeDecodeError:
                            raise self._fail(1007) from None
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
                    self.last_data = time.monotonic()
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
    # When the chip now on it was admitted (wall clock), for the api's robot list.
    body_since: float = 0.0

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


class _Slot:
    """One socket's place in the counts, taken at admission and given back exactly once."""

    def __init__(self, app: GrowBot, *, unauth: bool) -> None:
        self._app = app
        self._held = True
        self._unauth = unauth
        app.sockets += 1
        if unauth:
            app.unauth_sockets += 1

    def release_unauth(self) -> None:
        if self._unauth:
            self._unauth = False
            self._app.unauth_sockets -= 1

    def release(self) -> None:
        self.release_unauth()
        if self._held:
            self._held = False
            self._app.sockets -= 1


class Backoff:
    """Failures per client over a sliding window; at the limit the client waits it out."""

    MAX_KEYS = 4096

    def __init__(self) -> None:
        self._fails: dict[str, collections.deque[float]] = {}

    def _recent(self, key: str, now: float) -> collections.deque[float]:
        fails = self._fails.get(key)
        if fails is None:
            if len(self._fails) >= self.MAX_KEYS:
                self._fails.pop(next(iter(self._fails)))
            fails = self._fails[key] = collections.deque()
        while fails and now - fails[0] > BACKOFF_WINDOW_S:
            fails.popleft()
        return fails

    def blocked(self, key: str, now: float | None = None) -> bool:
        return (
            len(self._recent(key, time.monotonic() if now is None else now))
            >= BACKOFF_LIMIT
        )

    def fail(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._recent(key, now).append(now)


# ── the app ───────────────────────────────────────────────────────────────────


class GrowBot:
    def __init__(self, config: Config, sessions: Sessions | None = None) -> None:
        self.config = config
        self.sessions = sessions or Sessions(
            key_file=config.token_file, epoch_file=config.session_file
        )
        self.csp = build_csp(config.site)
        self.channels: dict[str, Channel] = {}
        self.sockets = 0
        self.unauth_sockets = 0
        self.connections = 0
        # Apart, so a flood of one kind (say, hello-less sockets) never locks out the other.
        self.ticket_backoff = Backoff()
        self.hello_backoff = Backoff()
        self._pool: Executor = ThreadPoolExecutor(API_WORKERS, "growbot-api")
        self._streams: Executor = ThreadPoolExecutor(STREAM_WORKERS, "growbot-stream")
        self._gate: Executor = ThreadPoolExecutor(GATE_WORKERS, "growbot-gate")
        # The phones' soul sync, and the rider checks of relay sockets, each apart from the
        # doors and the brain calls, so a slow api queues neither tickets nor a page's pull.
        self._soul_pool: Executor = ThreadPoolExecutor(SOUL_WORKERS, "growbot-soul")
        self._rider_pool: Executor = ThreadPoolExecutor(SOUL_WORKERS, "growbot-rider")
        self.rider_backoff = Backoff()
        self._proxy_addrs: set[str] = set()
        self._tunnel_addrs: set[str] = set()
        self._proxy_at = -math.inf
        self._models: list[dict[str, Any]] = []
        self._root_cert = b""
        self._root_cert_at = -math.inf
        self._models_at = -math.inf
        self.did = hashlib.sha256(b"growbot-on-the-box").hexdigest()[:16]

    # -- outbound ---------------------------------------------------------------

    async def _call(
        self, fn: Callable[..., Any], *args: Any, pool: Executor | None = None
    ) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            pool or self._pool, functools.partial(fn, *args)
        )

    async def client_ip(self, req: Request) -> str:
        """The client a request is for: Caddy's X-Real-IP when it came from Caddy, else the
        peer itself (anything else on the network could otherwise name any address). When the
        client Caddy saw is the tunnel connector, Cloudflare's Cf-Connecting-IP: otherwise
        everyone away from home shares the connector's backoff bucket, and a few bad tickets
        from anywhere would lock the owner out of the away address."""
        if time.monotonic() - self._proxy_at > 60.0:
            self._proxy_at = time.monotonic()
            self._proxy_addrs = await self._resolve(
                self.config.trusted_proxy, self._proxy_addrs
            )
            self._tunnel_addrs = await self._resolve(
                self.config.trusted_tunnel, self._tunnel_addrs
            )
        if req.peer not in self._proxy_addrs:
            return req.peer
        real = _ip(req.header("x-real-ip"))
        if real is None:
            return req.peer
        if real in self._tunnel_addrs:
            return _ip(req.header("cf-connecting-ip")) or real
        return real

    async def via_tunnel(self, req: Request) -> bool:
        """Whether the request came through the Cloudflare tunnel: from Caddy, which saw the
        connector."""
        await self.client_ip(req)  # refreshes the two address sets
        real = _ip(req.header("x-real-ip"))
        return req.peer in self._proxy_addrs and real is not None and real in self._tunnel_addrs

    @staticmethod
    async def _resolve(name: str, last: set[str]) -> set[str]:
        """`name`'s addresses. A failed lookup keeps the last good answer: an empty proxy set
        would fold every client into one backoff bucket, and a few bad tickets would lock the
        owner out. The tunnel connector is off on a box without the tunnel, which resolves to
        nothing and so trusts no Cf-Connecting-IP."""
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, name, None)
        except OSError:
            return last
        return {str(i[4][0]) for i in infos}

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

    def root_cert(self) -> bytes:
        """The box's LAN root, from the api. Public by nature (it is what a phone installs to
        trust the box) and served only here, on the LAN-only site, to phones signed in to
        nothing. A failed read keeps the last good copy."""
        now = time.monotonic()
        if now - self._root_cert_at < ROOT_CERT_CACHE_S:
            return self._root_cert
        # Claimed before the call, so requests arriving while it is in flight are served the
        # current copy instead of each asking the api.
        self._root_cert_at = now - ROOT_CERT_CACHE_S + ROOT_CERT_RETRY_S
        up = http_call(self.config.api_url + "/internal/growbot/lan-root", headers=self._auth())
        if up.status == 200 and b"BEGIN CERTIFICATE" in up.body and len(up.body) < 64 * 1024:
            self._root_cert, self._root_cert_at = up.body, time.monotonic()
        return self._root_cert

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
        return self._claims(req) is not None

    def _claims(self, req: Request) -> Claims | None:
        value = cookie_value(req.headers, SESSION_COOKIE)
        return self.sessions.claims(value) if value else None

    def _robot(self, req: Request) -> str | None:
        got = self._claims(req)
        return got.robot if got else None

    def _internal_ok(self, req: Request) -> bool:
        """The api's bearer: `growbot.token`, compared in constant time, re-read per call so
        a re-mint needs no restart. No token on disk means no caller is believed."""
        try:
            token = self.config.token_file.read_text().strip()
        except OSError:
            return False
        scheme, _, given = req.header("authorization").partition(" ")
        return (
            bool(token)
            and scheme.lower() == "bearer"
            and hmac.compare_digest(given.strip().encode(), token.encode())
        )

    def internal(self, req: Request) -> Response:
        """The api's two routes: which robots are on the relay, and dropping one."""
        if not self._internal_ok(req):
            return json_response(401, {"error": "unauthorized"})
        if req.path == "/internal/robots" and req.method == "GET":
            robots = [
                {
                    "code": chan.name,
                    "since": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(chan.body_since)
                    ),
                }
                for chan in sorted(self.channels.values(), key=lambda c: c.name)
                if chan.body is not None and not chan.name.endswith("-agent")
            ]
            return json_response(200, {"robots": robots})
        if req.path == "/internal/robots/drop" and req.method == "POST":
            msg = _parse(req.body)
            code = msg.get("code") if msg else None
            if not isinstance(code, str) or not CODE_RE.fullmatch(code):
                return json_response(400, {"error": "code"})
            chan = self.channels.get(code)
            dropped = chan is not None and chan.body is not None
            if chan is not None and chan.body is not None:
                # 1008 like a failed hello: the robot's key no longer opens the relay.
                chan.body.close(1008, "revoked")
            return json_response(200, {"dropped": dropped})
        return json_response(404, {"error": "no such route"})

    def _same_origin(self, req: Request) -> bool:
        """A request a browser sent from another origin (the PWA on :443 is same-site, so
        SameSite=Strict alone lets its cookie through) is refused for anything that acts."""
        origin = req.header("origin")
        return not origin or origin == f"https://{req.header('host')}"

    def _session_cookie(self, got: Claims) -> str:
        value = self.sessions.mint(robot=got.robot, persona=got.persona, phone=got.phone)
        return (
            f"{SESSION_COOKIE}={value}; HttpOnly; Secure; "
            f"SameSite=Strict; Path=/; Max-Age={SESSION_TTL_S}"
        )

    async def root_cert_route(self, req: Request) -> Response:
        if req.method not in ("GET", "HEAD"):
            return Response(405, b"method not allowed", headers=[("Allow", "GET, HEAD")])
        pem = await self._call(self.root_cert, pool=self._gate)
        if not pem:
            return Response(404, b"no certificate on this box")
        return Response(
            200,
            pem,
            "application/x-x509-ca-cert",
            headers=[
                ("Content-Disposition", 'attachment; filename="jbrain-box-root.crt"'),
                ("Cache-Control", "no-store"),
            ],
        )

    def start_page(self, req: Request) -> Response:
        """The installed app's start page: back into the creature, on the robot it last had."""
        if req.method not in ("GET", "HEAD"):
            return Response(405, b"method not allowed", headers=[("Allow", "GET, HEAD")])
        got = self._claims(req)
        if got is None:
            # A lapsed installed app scans a fresh code from JBrain's GrowBot screen.
            return Response(
                302, b"", headers=[("Location", "/setup.html"), ("Cache-Control", "no-store")]
            )
        # A persona's page asks the box which robot it wears as it loads (growbot-box.js), and
        # moves to that one, so the robot named at open is only where it starts.
        return Response(
            302,
            b"",
            headers=[
                ("Set-Cookie", self._session_cookie(got)),
                ("Location", _creature(got.robot)),
                ("Cache-Control", "no-store"),
            ],
        )

    async def handle(self, req: Request) -> Response:
        if req.path == "/open":
            return await self.open(req)
        if req.path == "/start":
            return self.start_page(req)
        if req.path == ROOT_CERT_PATH:
            return await self.root_cert_route(req)
        if req.path.startswith("/internal/"):
            return self.internal(req)
        if req.path in PUBLIC_PATHS and req.method in ("GET", "HEAD"):
            return await self.static(req)
        if not self._authed(req):
            return Response(401, UNAUTHORISED_PAGE, "text/html; charset=utf-8")
        if req.path.startswith("/api/"):
            if req.method not in ("GET", "HEAD") and not self._same_origin(req):
                return json_response(403, {"error": "cross-origin"})
            resp = await self.api(req)
        elif req.method not in ("GET", "HEAD"):
            return Response(
                405, b"method not allowed", headers=[("Allow", "GET, HEAD")]
            )
        else:
            resp = await self.static(req)
        # The robot's phone may keep one page open for weeks; its own calls renew it.
        if self.sessions.due(cookie_value(req.headers, SESSION_COOKIE)):
            renewed = self._session_cookie(self._claims(req) or Claims(""))
            resp.headers.append(("Set-Cookie", renewed))
        return resp

    async def open(self, req: Request) -> Response:
        ticket, code = req.arg("ticket"), req.arg("id")
        if (
            req.method != "GET"
            or not (0 < len(ticket) <= 512)
            or (code and not CODE_RE.fullmatch(code))
        ):
            return Response(400, b"bad link", headers=[("Cache-Control", "no-store")])
        # The PWA's link is a top-level navigation. Any other load (an <img>, a fetch) is some
        # page spending the owner's tries for them, so it is refused uncounted. A cross-site
        # navigation still opens (the PWA may be reached by another name), but counts in its
        # own bucket, so a page the owner visits cannot lock out the PWA's own link.
        # The installed app's service worker passes a navigation on with dest `empty` but
        # mode `navigate`, which no page's fetch, <img> or frame can claim: without this the
        # second open, the first one the worker controls, was refused. A frame is a
        # navigation too, but never the PWA's link, so its dest still refuses it.
        dest = req.header("sec-fetch-dest")
        worker = dest == "empty" and req.header("sec-fetch-mode") == "navigate"
        if dest and dest != "document" and not worker:
            return Response(403, b"open from the PWA", headers=[("Cache-Control", "no-store")])
        if req.header("sec-fetch-site") == "cross-site":
            # The session cookie is SameSite=Strict, so a cross-site navigation (the PWA reached
            # by its public name) arrives without it, and the phone would lose its id. Nothing is
            # spent here: this page's own refresh comes back same-site, with the cookie, marked
            # `hop` so its tries still count in the cross-site bucket.
            again = {"ticket": ticket, "hop": "1", **({"id": code} if code else {})}
            return Response(
                200,
                _landing("/open?" + urllib.parse.urlencode(again)),
                "text/html; charset=utf-8",
                headers=[("Cache-Control", "no-store")],
            )
        client = await self.client_ip(req)
        if req.arg("hop"):
            # Only a cross-site start sends `hop`; a page that adds it to a same-site link
            # just moves its own tries into that bucket.
            client = f"cross-site:{client}"
        if self.ticket_backoff.blocked(client):
            return Response(429, b"too many tries", headers=[("Retry-After", "300")])
        # The phone id this browser already has, so it stays the same phone across tickets (a
        # persona ticket spent here closes the persona it held). Only a valid session's.
        held = self._claims(req)
        up = await self._call(
            self._api_post,
            "/internal/growbot/ticket/redeem",
            {
                "ticket": ticket,
                "phone": held.phone if held else "",
                "label": phone_label(req.header("user-agent")),
            },
            pool=self._gate,
        )
        got = _redeemed(up) if self._ok(up) else None
        if got is None:
            self.ticket_backoff.fail(client)
            return Response(401, UNAUTHORISED_PAGE, "text/html; charset=utf-8")
        if held and held.phone and not got.phone:
            # A G2 ticket on a phone that held a persona: it keeps its id for the next one.
            got = Claims(got.robot, "", held.phone)
        # The ticket names the robot, not the link: `id` is the phone's to edit, so it is
        # only checked for shape above and never trusted.
        code = got.robot
        # The session carries the robot the ticket named, so the installed app's /start
        # reopens the same way and the relay lets this phone join that robot alone.
        # A page, not a 302: the PWA's link may be a cross-site navigation (when the PWA is reached by its public name), and a browser withholds
        # a SameSite=Strict cookie for the rest of a cross-site redirect chain, so a 302 landed
        # on the creature signed out. This page's own refresh starts on this site, and the
        # cookie stays Strict, so another site's link opens nothing signed in unless it carries
        # a live ticket, which only the owner mints (the cross-site bounce above accepts that).
        return Response(
            200,
            _landing(_creature(code)),
            "text/html; charset=utf-8",
            headers=[("Set-Cookie", self._session_cookie(got)), ("Cache-Control", "no-store")],
        )

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
        if req.path in (SOUL_PATH, SOUL_PATH + "/state"):
            return await self.soul_route(req)
        if req.path in (MEMORY_PATH, MEMORY_PATH + "/view", MEMORY_PATH + "/forget"):
            return await self.memory_route(req)
        if name in ("journal", "episodes") and req.method == "DELETE":
            # The app's erase flow reads a JSON answer; there is nothing here to erase.
            return json_response(200, {"ok": True})
        if name == "gallery" and req.arg("download"):
            return json_response(404, {"error": "no gallery on the box"})
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

    async def soul_route(self, req: Request) -> Response:
        """The persona's soul, which lives on the box (plan §2c): `GET` pulls it, `POST
        {base, soul}` pushes the creature's write against the version it pulled, and `GET
        …/state` is the cheap poll (version, and the robot it wears). 204 for a G2 session,
        whose creature keeps its soul on the phone. 409 `moved`, 409 `newer`, 404 `gone`, 413
        and 400 are the api's; anything else the api says is a 503 the page retries."""
        claims = self._claims(req)
        if claims is None or not claims.persona:
            return Response(204, headers=[("Cache-Control", "no-store")])
        who = {"persona": claims.persona, "phone": claims.phone}
        state = req.path.endswith("/state")
        if req.method == "GET":
            path = "/internal/growbot/soul/state" if state else "/internal/growbot/soul/pull"
            payload: dict[str, Any] = who
        elif req.method == "POST" and not state:
            msg = _parse(req.body)
            base = msg.get("base") if msg else None
            if msg is None or isinstance(base, bool) or not isinstance(base, int):
                return json_response(400, {"error": "a soul and the version it came from"})
            if msg.get("persona") != claims.persona:
                # The page names the persona its write came from; a phone given another
                # persona since never pushes the old one's soul into the new one.
                return json_response(409, {"error": "other"})
            path = "/internal/growbot/soul/push"
            payload = {**who, "base": base, "soul": msg.get("soul")}
        else:
            return json_response(405, {"error": "method not allowed"})
        up = await self._call(self._api_post, path, payload, pool=self._soul_pool)
        headers = [("Cache-Control", "no-store")]
        if up.status == 200:
            got = _parse(up.body)
            if got is None:
                return json_response(503, {"error": "the box is busy"}, headers)
            # The page keeps which persona its unsent writes belong to, so a phone that was
            # given another persona never pushes the old one's soul into the new one.
            return json_response(200, {**got, "persona": claims.persona}, headers)
        if up.status in (400, 404, 409, 413):
            try:
                detail = json.loads(up.body).get("detail")
            except (ValueError, AttributeError):
                detail = None
            why = detail if isinstance(detail, str) else "refused"
            return json_response(up.status, {"error": why[:200]}, headers)
        return json_response(503, {"error": "the box is busy"}, headers)

    async def memory_route(self, req: Request) -> Response:
        """The persona's visual memories, which live on the box (wave G4d): `GET` lists what
        the box holds (each record's fields and its views' times, never a picture), `GET
        …/view?id=&i=` is one picture as base64, `POST {persona, memory}` keeps a record the
        creature saved, and `POST …/forget {persona, ids}` drops ones it forgot. 204 for a G2
        session; the api's 400, 404, 409 and 413 pass through, anything else is a 503."""
        claims = self._claims(req)
        if claims is None or not claims.persona:
            return Response(204, headers=[("Cache-Control", "no-store")])
        who: dict[str, Any] = {"persona": claims.persona, "phone": claims.phone}
        sub = req.path[len(MEMORY_PATH) :]
        picture = False
        if req.method == "GET" and sub == "":
            path, payload = "/internal/growbot/memories/list", who
        elif req.method == "GET" and sub == "/view":
            mid, i = req.arg("id"), req.arg("i")
            if not MEMORY_ID_RE.fullmatch(mid) or i not in ("0", "1", "2"):
                return json_response(400, {"error": "a memory id and a view"})
            path, payload, picture = (
                "/internal/growbot/memories/view",
                {**who, "id": mid, "i": int(i)},
                True,
            )
        elif req.method == "POST" and sub in ("", "/forget"):
            msg = _parse(req.body)
            if msg is None:
                return json_response(400, {"error": "a visual memory"})
            if msg.get("persona") != claims.persona:
                # As the soul's push: a page still on a persona this phone has since swapped
                # never writes its memories into the new one.
                return json_response(409, {"error": "other"})
            if sub == "":
                path, payload = "/internal/growbot/memories/keep", {
                    **who,
                    "memory": msg.get("memory"),
                }
            else:
                path, payload = "/internal/growbot/memories/forget", {
                    **who,
                    "ids": msg.get("ids"),
                }
        else:
            return json_response(405, {"error": "method not allowed"})
        up = await self._call(self._api_post, path, payload, pool=self._soul_pool)
        headers = [("Cache-Control", "no-store")]
        if up.status == 200 and picture:
            # IndexedDB keeps a view as the base64 of its JPEG, so it is sent that way.
            return json_response(
                200, {"j": base64.b64encode(up.body).decode("ascii")}, headers
            )
        if up.status == 200:
            got = _parse(up.body)
            if got is None:
                return json_response(503, {"error": "the box is busy"}, headers)
            return json_response(200, {**got, "persona": claims.persona}, headers)
        if up.status in (400, 404, 409, 413):
            try:
                detail = json.loads(up.body).get("detail")
            except (ValueError, AttributeError):
                detail = None
            why = detail if isinstance(detail, str) else "refused"
            return json_response(up.status, {"error": why[:200]}, headers)
        return json_response(503, {"error": "the box is busy"}, headers)

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
        models = await self._call(self.models)
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
        models = await self._call(self.models)
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
        out["messages"] = [_inline_images_only(m) for m in body["messages"]]
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
        models = await self._call(self.models)
        body = self.llm_body(req.body, stream, models)
        if body is None:
            return json_response(400, {"error": {"message": "bad chat body"}})
        url = self.config.api_url + "/internal/growbot/llm/v1/chat/completions"
        headers = {"Content-Type": "application/json", **self._auth()}
        return await stream_upstream(
            url, body, headers, sse=stream, executor=self._streams
        )

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
        # Always sent, 0 (the full window) when unsized: whisper-server restores its defaults
        # only after a successful request, so a failed one's small window would cut the next.
        fields["audio_ctx"] = str(audio_window(audio.data) or 0)
        body, ctype = build_multipart(fields, audio)
        up = await self._call(
            http_call,
            self.config.stt_url + "/audio/transcriptions",
            body,
            {"Content-Type": ctype},
            "POST",
            STT_TIMEOUT_S,
            pool=self._streams,
        )
        if up.status != 200:
            # Not 429: upstream reads that as a spent daily allowance and stops listening.
            return json_response(503, {"error": _stt_failure(up)})
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
        up = await self._call(
            http_call, url, None, None, "GET", 60.0, pool=self._streams
        )
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
        claims = self._claims(req)
        authed = claims is not None
        if agent and not authed:
            return Response(401, UNAUTHORISED_PAGE, "text/html; charset=utf-8")
        if authed and req.header("origin") != f"https://{req.header('host')}":
            return Response(403, b"cross-origin")
        # Pairing is JBrain's: a G2 phone joins only the robot its ticket named (a persona's
        # phone is asked about below, once the request is a well-formed upgrade).
        if claims is not None and not claims.persona and claims.robot != code:
            return Response(403, b"pair from JBrain")
        # The chip's MicroPython client sends no Origin and every browser does, so a cookieless
        # socket with one is a web page squatting the robots' budget or burning their tries.
        if not authed and req.header("origin"):
            return Response(403, b"cross-origin")
        # A robot dials in on the LAN, never through the tunnel: from the internet only a
        # signed-in phone may open a socket, or anyone could hold the robots' unauthenticated
        # budget or guess at their keys. Caddy's away site shuts a cookieless /d/, but any
        # cookie named gb_session gets past that, so this is the gate.
        if not authed and await self.via_tunnel(req):
            return Response(403, b"robots dial in at home")
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
        if self.sockets >= MAX_SOCKETS or (
            not authed and self.unauth_sockets >= MAX_UNAUTH_SOCKETS
        ):
            return Response(503, b"busy")
        # The slots are taken here, at the check, so a burst of upgrades cannot all pass it
        # before any of them is counted; every way out gives them back.
        slot = _Slot(self, unauth=not authed)
        ws: WebSocket | None = None
        keepalive: asyncio.Task[None] | None = None
        watch: asyncio.Task[None] | None = None
        name = seg if agent else code
        try:
            client = await self.client_ip(req)
            if not authed and self.hello_backoff.blocked(client):
                return Response(
                    429, b"too many tries", headers=[("Retry-After", "300")]
                )
            if claims is not None:
                # The body has one rider (plan §2c): only the phone of the persona that wears it
                # may drive it, and a G2 phone only while no persona does. Refusals are counted,
                # so a session trying codes cannot keep the api busy answering.
                # Per session, not per address: a refused G2 phone retrying must not lock out
                # the rider's phone behind the same NAT.
                tries = f"{client}:{cookie_value(req.headers, SESSION_COOKIE)}"
                if self.rider_backoff.blocked(tries):
                    return Response(
                        429, b"too many tries", headers=[("Retry-After", "300")]
                    )
                may = await self._call(self.may_drive, claims, code, pool=self._rider_pool)
                if may is None:
                    return Response(503, b"busy")
                if not may:
                    self.rider_backoff.fail(tries)
                    return Response(403, b"not wearing this robot")
            head = "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            head += "Connection: Upgrade\r\n"
            head += f"Sec-WebSocket-Accept: {ws_accept(key)}\r\n"
            head += "".join(f"{k}: {v}\r\n" for k, v in self._base_headers()) + "\r\n"
            writer.write(head.encode())
            await writer.drain()
            ws = WebSocket(reader, writer)
            keepalive = asyncio.create_task(self._keepalive(ws))
            if claims is not None:
                # Both a phone's socket and its agent side channel drive the body, so both
                # leave it when this phone stops riding.
                watch = asyncio.create_task(self._rider_watch(ws, claims, code))
            # A channel is only made once its socket is admitted (a phone, an authed
            # side-channel socket, a verified robot), and every way out drops it again if it
            # is left empty.
            if agent:
                await self._agent_socket(ws, name)
            elif claims is not None:
                await self._phone(ws, self._channel(name))
            else:
                await self._chip(ws, code, client, slot)
        except WsClosed:
            pass
        finally:
            if keepalive is not None:
                keepalive.cancel()
            if watch is not None:
                watch.cancel()
            slot.release()
            if ws is not None and not ws.closed:
                ws.close(1000)
            chan = self.channels.get(name)
            if chan is not None:
                self._drop_empty(chan)
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

    async def _rider_watch(self, ws: WebSocket, claims: Claims, code: str) -> None:
        """A hand-over or a Who-wears pick moves the body to another phone while this one is
        still on it: the phone that no longer rides is closed. A lookup that fails keeps it
        on (an api blip must not stop a walking robot); only a definite no closes it."""
        while not ws.closed:
            await asyncio.sleep(RIDER_RECHECK_S)
            try:
                may = await self._call(self.may_drive, claims, code, pool=self._rider_pool)
            except Exception:  # noqa: BLE001 — the watch must outlive any one failed lookup
                continue
            if may is False:
                ws.close(1008, "not wearing this robot")
                return

    def rider(self, code: str) -> tuple[str, str] | None:
        """Who wears robot `code`, from the api: (persona, phone), both "" for nobody. None
        when the api cannot say."""
        up = http_call(
            self.config.api_url + "/internal/growbot/body/" + urllib.parse.quote(code),
            headers=self._auth(),
            timeout=5.0,
        )
        if up.status != 200:
            return None
        try:
            got = json.loads(up.body)
            persona, phone = got.get("persona") or "", got.get("phone") or ""
        except (ValueError, AttributeError):
            return None
        if not (isinstance(persona, str) and isinstance(phone, str)):
            return None
        return persona, phone

    def may_drive(self, claims: Claims, code: str) -> bool | None:
        """May this session's phone drive robot `code` now? None when the api cannot say."""
        rider = self.rider(code)
        if rider is None:
            return None
        persona, phone = rider
        if claims.persona:
            return persona == claims.persona and phone == claims.phone
        return claims.robot == code and not persona

    async def _chip(self, ws: WebSocket, code: str, client: str, slot: _Slot) -> None:
        try:
            hello = await self._first(ws)
            key = hello.get("key") if hello else None
            if (
                not hello
                or hello.get("t") != "hello"
                or hello.get("id") != code
                or not isinstance(key, str)
                or not 0 < len(key) <= 256
            ):
                self.hello_backoff.fail(client)
                ws.close(1008, "hello with a key")
                return
            up = await self._call(
                self._api_post,
                "/internal/growbot/robot/verify",
                {"code": code, "key": key},
                pool=self._gate,
            )
            if not self._ok(up):
                self.hello_backoff.fail(client)
                ws.close(1008, "unknown robot")
                return
        finally:
            # A verified robot leaves the anonymous budget; it is bounded by MAX_SOCKETS.
            slot.release_unauth()
        caps = hello.get("caps")
        hb = isinstance(caps, list) and "hb" in caps
        chan = self._channel(code)
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
        chan.body_since = time.time()
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
                quiet = int((time.monotonic() - ws.last_data) * 1000)
                chan.to_peers(
                    json.dumps({"t": "alive", "quiet_ms": quiet}, separators=(",", ":"))
                )

    async def _agent_socket(self, ws: WebSocket, name: str) -> None:
        """The side channel: the creature page says `hello` and is its body; agents attach as
        peers. Both are behind the session cookie; frames are bridged both ways."""
        first = await self._first(ws)
        if first is None:
            ws.close(1008, "say hello or attach")
            return
        chan = self._channel(name)
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
        self.connections += 1
        try:
            if self.connections > MAX_CONNECTIONS:
                await self.write(writer, None, Response(503, b"busy"), False)
                return
            peername = writer.get_extra_info("peername")
            peer = str(peername[0]) if isinstance(peername, tuple) else ""
            while True:
                try:
                    req = await asyncio.wait_for(read_head(reader), IDLE_TIMEOUT_S)
                    req.peer = peer
                    keep = req.version == "HTTP/1.1" and "close" not in req.tokens(
                        "connection"
                    )
                    if req.path.startswith("/d/"):
                        refused = await self.relay(req, reader, writer)
                        if refused is not None:
                            await self.write(writer, req, refused, False)
                        return
                    # Without a session only /open, /start and the installed app's files,
                    # none with a body, and the api's bearer-checked /internal routes are
                    # answered; nothing else costs this process an upload.
                    internal = req.path.startswith("/internal/")
                    if internal and not self._internal_ok(req):
                        refused = json_response(401, {"error": "unauthorized"})
                        await self.write(writer, req, refused, False)
                        return
                    if (
                        not internal
                        and not self._authed(req)
                        and (req.path not in SESSIONLESS or body_framing(req) != 0)
                    ):
                        page = Response(
                            401, UNAUTHORISED_PAGE, "text/html; charset=utf-8"
                        )
                        await self.write(writer, req, page, False)
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
            self.connections -= 1
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
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    sse: bool,
    executor: Executor | None = None,
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

    if executor is None:
        threading.Thread(target=work, daemon=True).start()
    else:
        executor.submit(work)
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


def _stt_failure(up: Upstream) -> str:
    """What the creature says when the box's speech model failed it."""
    if up.why == "timeout":
        return f"the box's speech model took over {STT_TIMEOUT_S:.0f} s"
    if up.why == "down":
        return "the box's speech service isn't running"
    if up.why == "broken":
        return "the box's speech model sent a broken answer"
    return f"the box's speech model failed (HTTP {up.status})"


def _inline_images_only(message: object) -> object:
    """Keep only text parts and inline data: images. Any other part (an image by URL, video,
    audio, whatever an engine might grow) could have the engine fetch something, the box's own
    services included."""
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return message
    parts = []
    for part in message["content"]:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part)
        elif part.get("type") == "image_url":
            ref = part.get("image_url")
            url = ref.get("url") if isinstance(ref, dict) else ref
            if isinstance(url, str) and IMAGE_URL_RE.fullmatch(url):
                parts.append(part)
    return {**message, "content": parts}


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


def wav_seconds(data: bytes) -> float | None:
    """A WAV clip's length from its own header, or None for anything else."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    rate, at = 0, 12
    while at + 8 <= len(data):
        tag, size = data[at : at + 4], int.from_bytes(data[at + 4 : at + 8], "little")
        if tag == b"fmt " and size >= 12:
            rate = int.from_bytes(data[at + 16 : at + 20], "little")
        elif tag == b"data":
            # A streamed WAV may carry a placeholder size; the bytes present are the clip.
            size = min(size, len(data) - at - 8)
            return size / rate if rate > 0 else None
        at += 8 + size + (size & 1)
    return None


# Whisper encodes a fixed 30 s window, padding a short clip with silence, so a 3 s clip costs
# what 30 s does: the creature's measured 10-30 s per clip. A window sized to the clip (~50
# encoder frames a second, half again for margin, the room panels' floor of 160) is the panels'
# cut from 9.5 s to 2.3 s (backend api/endpoint.py). Only a WAV, whose length its header
# states; anything else keeps the full window rather than risk truncating it.
def audio_window(data: bytes) -> int | None:
    seconds = wav_seconds(data)
    if seconds is None or seconds <= 0 or seconds > 30:
        return None
    return max(160, min(1500, int(seconds * 75)))


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


# ── SNTP for the robots ────────────────────────────────────────────────────────

NTP_PACKET = 48
# Seconds from the NTP epoch (1900) to the Unix one (1970).
NTP_UNIX_DELTA = 2208988800
# Stratum 2 says "synchronised, not a reference clock"; the box's own clock is the host's.
NTP_STRATUM = 2
NTP_REFID = b"JBOX"


def _ntp_stamp(t: float) -> bytes:
    secs = int(t) + NTP_UNIX_DELTA
    frac = int((t - int(t)) * (1 << 32)) & 0xFFFFFFFF
    return struct.pack("!II", secs & 0xFFFFFFFF, frac)


def sntp_reply(data: bytes, received: float, now: float) -> bytes | None:
    """The server-mode answer (RFC 4330 §5) to one client request, or None for anything
    else: wrong size, not mode 3, or a version outside 1–4. The reply is the request's own
    size, so a spoofed source gets nothing larger than it sent."""
    if len(data) != NTP_PACKET:
        return None
    version = (data[0] >> 3) & 0x7
    if data[0] & 0x7 != 3 or not 1 <= version <= 4:
        return None
    head = struct.pack(
        "!BBBbII4s",
        (version << 3) | 4,  # LI 0, the client's version, mode 4 (server)
        NTP_STRATUM,
        data[2],  # the client's poll, echoed as the raw byte
        -20,  # precision, about a microsecond
        0,  # root delay
        0x00000100,  # root dispersion, 1/256 s
        NTP_REFID,
    )
    reply = head + _ntp_stamp(now) + data[40:48] + _ntp_stamp(received) + _ntp_stamp(now)
    return reply


class Sntp(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        received = time.time()
        reply = sntp_reply(data, received, time.time())
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)


async def start_sntp(host: str = "0.0.0.0", port: int = 8123) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(Sntp, local_addr=(host, port))
    return transport


async def main() -> None:
    config = Config.from_env()
    app = GrowBot(config)
    port = int(os.environ.get("GROWBOT_PORT", "8810"))
    server = await app.start("0.0.0.0", port)
    ntp_port = int(os.environ.get("GROWBOT_NTP_PORT", "8123"))
    try:
        await start_sntp("0.0.0.0", ntp_port)
    except OSError as exc:
        # The robots' clock is not worth the app: phones still open GrowBot without it.
        print(f"growbot: SNTP on udp/{ntp_port} failed to bind: {exc}", flush=True)
    print(f"growbot: serving {config.site} on :{port}, SNTP on udp/{ntp_port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
