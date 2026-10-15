"""The `cast` sidecar: the house's Chromecast TVs, watched, and a test card cast to one.

The jpanel Cast spike (docs/mocks/jpanel-cast/README.md). HOST NETWORK, like
`minecraft`, and for the same kind of reason: Cast devices are found by mDNS multicast,
which never reaches a container on a bridge network, and the TVs must fetch the card
from the box's own LAN address.

So the control routes are on the LAN and take a bearer — `cast.token`, which the api
mints into the `internal_secrets` volume this container mounts read-only (no `.env` key,
no host step). Read per request, so it works whichever of the two starts first; no token
yet fails closed. The one unauthenticated route is `/frame/<nonce>.png`: a TV cannot
send a header, so the card is served under a random name that lives only as long as its
test.

    GET  /healthz
    GET  /tvs                         every TV found, its live verdict and reading,
                                      and the non-screen devices set aside
    GET  /tvs/<id>/history?since=<t>  that TV's timeline of readings and events
    POST /tvs/<id>/test {"seconds": n}
                                      gate, cast the card, quit after n s if ours
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import frame
import gate

PORT = int(os.environ.get("CAST_PORT", "19190"))
TOKEN_PATH = os.environ.get("CAST_TOKEN_PATH", "/run/jbrain-internal/cast.token")
MAX_BODY = 4096
#: A card outlives its test by this much, for a TV that fetches late or twice.
FRAME_GRACE_S = 60

_TV = re.compile(r"^/tvs/([0-9a-f-]{36})(/history|/test)?$")
_FRAME = re.compile(r"^/frame/([0-9a-f]{32})\.png$")


class FleetLike(Protocol):
    def snapshot(self) -> list[dict[str, Any]]: ...
    def ignored(self) -> list[dict[str, Any]]: ...
    def history(self, key: str, since: float = 0.0) -> list[dict[str, Any]]: ...
    def host_of(self, key: str) -> str: ...
    def test(self, key: str, url: str, seconds: int) -> dict[str, Any]: ...


class Frames:
    """The cards on offer, by nonce, each until its test is over."""

    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self._lock = threading.Lock()
        self._live: dict[str, tuple[bytes, float]] = {}
        self._card: bytes | None = None

    def offer(self, ttl_s: float) -> str:
        nonce = secrets.token_hex(16)
        with self._lock:
            if self._card is None:
                self._card = frame.png()
            now = self._now()
            self._live = {k: v for k, v in self._live.items() if v[1] > now}
            self._live[nonce] = (self._card, now + ttl_s)
        return nonce

    def get(self, nonce: str) -> bytes | None:
        with self._lock:
            hit = self._live.get(nonce)
        return hit[0] if hit and hit[1] > self._now() else None


def read_token(path: str = TOKEN_PATH) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read().strip() or None
    except OSError:
        return None


def authorised(header: str, expected: bytes | None) -> bool:
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not expected:
        return False
    return hmac.compare_digest(presented.strip().encode("latin-1"), expected)


Reply = tuple[int, str, bytes]


def _json(code: int, body: Any) -> Reply:
    return code, "application/json", json.dumps(body).encode()


def route(
    method: str,
    target: str,
    auth_header: str,
    body: bytes,
    fleet: FleetLike,
    frames: Frames,
    token: Callable[[], bytes | None] = read_token,
    address_for: Callable[[str], str] | None = None,
    port: int = PORT,
) -> Reply:
    url = urlparse(target)
    path = url.path
    if method == "GET" and path == "/healthz":
        return _json(200, {"ok": True})
    if method == "GET" and (m := _FRAME.match(path)):
        card = frames.get(m.group(1))
        return (
            (200, "image/png", card) if card else _json(404, {"error": "no such frame"})
        )
    if not authorised(auth_header, token()):
        return _json(401, {"error": "unauthorized"})
    if method == "GET" and path == "/tvs":
        return _json(200, {"tvs": fleet.snapshot(), "ignored": fleet.ignored()})
    m = _TV.match(path)
    if not m or not m.group(2):
        return _json(404, {"error": "no such route"})
    key, leaf = m.group(1), m.group(2)
    try:
        if method == "GET" and leaf == "/history":
            try:
                since = float((parse_qs(url.query).get("since") or ["0"])[0])
            except ValueError:
                return _json(400, {"error": "since must be a number"})
            return _json(200, {"id": key, "entries": fleet.history(key, since)})
        if method == "POST" and leaf == "/test":
            try:
                seconds = int((json.loads(body or b"{}") or {}).get("seconds", 15))
            except (ValueError, TypeError, AttributeError):
                return _json(400, {"error": 'body must be {"seconds": n}'})
            # Before the card is offered: its unauthenticated URL lives as long as this.
            seconds = gate.clamp_seconds(seconds)
            host = fleet.host_of(key)
            nonce = frames.offer(ttl_s=seconds + FRAME_GRACE_S)
            box = (address_for or _address_for)(host)
            url_out = f"http://{box}:{port}/frame/{nonce}.png"
            try:
                return _json(202, fleet.test(key, url_out, seconds))
            except gate.Refused as exc:
                return _json(
                    409, {"error": "not idle", "state": exc.state, "why": exc.why}
                )
    except KeyError:
        return _json(404, {"error": f"no TV {key}"})
    return _json(405, {"error": "method not allowed"})


def _address_for(host: str) -> str:
    import tvs

    return tvs.local_address_for(host)


def serve(fleet: FleetLike, port: int = PORT) -> None:
    frames = Frames()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _reply(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                code, ctype, raw = _json(413, {"error": "body too large"})
            else:
                body = self.rfile.read(length) if length else b""
                try:
                    code, ctype, raw = route(
                        method,
                        self.path,
                        self.headers.get("Authorization", ""),
                        body,
                        fleet,
                        frames,
                        port=port,
                    )
                except Exception as exc:  # noqa: BLE001 - the caller gets the reason
                    code, ctype, raw = _json(
                        500, {"error": f"{type(exc).__name__}: {exc}"}
                    )
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
            self._reply("GET")

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
            self._reply("POST")

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    import tvs

    fleet = tvs.Fleet()
    fleet.start()
    serve(fleet)
