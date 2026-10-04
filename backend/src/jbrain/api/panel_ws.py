"""One socket per panel: every request a panel makes, over a single TLS WebSocket.

WHY THIS EXISTS. Measured on Elora's panel (firmware 0.3.38, 2026-10-04): after about two
talk turns every request failed with "connect" — talk, the settings poll, the jpanel poll,
a send. Internal heap sat near 75 KB free with a largest block of 25.6 KB, and every request
was a fresh mbedTLS session (~40 KB of internal RAM, 1-2 s of handshake). Two handshakes in
flight at once — the main task's settings poll racing a talk turn or a jpanel poll — do not
fit. No per-turn leak was found; the panel was simply opening more TLS sessions at once than
the chip can hold. So the panel holds ONE session for everything instead.

THE BOX SIDE IS A TUNNEL, NOT A SECOND IMPLEMENTATION. Each request frame names an HTTP
method and a path; this module replays it, in process, through the SAME ASGI app the panel's
HTTPS requests reach — the same route function, the same `PanelDep` authentication (the
connection's device key is presented again on every request, so a key revoked mid-connection
is refused at the next request), the same RLS-scoped session, the same validation and the same
logging. There is no panel logic in this file to drift from the HTTP routes, and every existing
route keeps working unchanged for panels on older firmware. Only the paths in `_ALLOWED` are
reachable: the panel-facing routes, minus the OTA image (which stays on its own HTTPS download,
so the panel can close this socket first and hold one TLS session at a time) and minus
`/jpanel/events` (which this socket's server-initiated frames replace).

THE PROTOCOL, v1 — JSON control frames, binary frames for bodies. `firmware/main/wsproto.h`
spells the same thing from the other end.

  panel -> box
    {"t":"req","id":N,"m":"POST","p":"/endpoint/converse?x=1","h":{...},"len":B,"win":W}
        then B body bytes in binary frames, each `[id: u32 little-endian][bytes]`.
        `win` is how many RESPONSE body bytes the panel can take before it acks; 0 or absent
        means "send it all". This is the backpressure: a message streaming into a four-second
        speaker ring must not be pushed faster than the speaker drains it.
    {"t":"ack","id":N,"n":K}     K more response bytes may be sent
    {"t":"cancel","id":N}        abandon a request (a finger stopped a message)
    {"t":"hb"}                   the panel is alive

  box -> panel
    {"t":"hello","v":1,"hb":S,"idle":S}
    {"t":"res","id":N,"s":status,"h":{...},"len":M}   then M bytes as `[id][chunk]` frames
    {"t":"ev","why":"message"}   "come and ask" — exactly what the SSE stream and the UDP
                                 nudge carry, and never anything the panel acts on as data
    {"t":"hb"}

ONE CONNECTION PER DEVICE. A panel that reconnects (Wi-Fi blip, reboot, a half-open socket it
gave up on) replaces its old connection, which is closed with 4000. Close codes: 4401 not
authenticated (sent before the upgrade is accepted, so a client actually SEES an HTTP 403 —
the same answer a box without this route gives, which is why the panel falls back on 403 at
once), 4000 replaced, 4408 idle (no frame for `IDLE_S`), 4400 protocol error, 4403
revoked.

Rules: no LLM (#1 n/a). No new table (#3 n/a) — the registry is process memory describing live
sockets, like `nudge._streams`, and a restart loses nothing a reconnect does not restore.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import struct
import time
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, cast

import httpx
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from jbrain.api import nudge
from jbrain.auth import service
from jbrain.auth.service import AuthRepo, PrincipalInfo

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/endpoint", tags=["endpoint"])

PROTOCOL_VERSION = 1

#: The box writes a heartbeat this often. Under Cloudflare's 100 s idle cut and well under
#: `IDLE_S`, so a quiet socket is never mistaken for a dead one at either end.
HEARTBEAT_S = 20.0
#: No frame from the panel for this long means the link is gone. The panel sends its own
#: heartbeat every `HEARTBEAT_S`, so this is three missed beats and change.
IDLE_S = 75.0
#: How long a response may sit waiting for the panel to ack more window before it is dropped.
#: Longer than any message the box stores (`MAX_MESSAGE_MS` is 30 s, and the panel acks as the
#: speaker drains), shorter than forever.
WINDOW_STALL_S = 90.0
#: Response bytes per binary frame. The panel's receive buffer is smaller than this and
#: `esp_websocket_client` hands a large frame over in pieces, which `wsproto.c` reassembles.
CHUNK = 4096
#: A body the panel may upload in one request. `PANEL_AUDIO_MAX` (35 s, ~1.1 MB) is the largest
#: thing a panel sends; this leaves headroom without letting one socket hold the box's memory.
MAX_BODY = 2 * 1024 * 1024
#: Requests one connection may have in flight. The firmware has three tasks that ask (main,
#: talk, jpanel); one more is slack, more is a panel misbehaving.
MAX_INFLIGHT = 4
#: A control frame is a few hundred bytes. Anything bigger is not a control frame.
MAX_TEXT = 4096
#: An upload whose body has not advanced for this long is abandoned and its slot freed. A panel
#: whose upload fails says `cancel`, but one whose socket half-dies mid-upload cannot, and four
#: such leftovers would answer every later request 429 until the socket closed.
PENDING_STALL_S = 30.0
#: How often the stalled uploads are looked for.
REAP_S = 5.0

#: The base URL of the in-process replay. FIXED, never built from anything the client sent: a
#: crafted Host (`x/api/debug/reach?`) would otherwise steer the replayed path past `_ALLOWED`.
_BASE_URL = "http://panel.internal"
#: What a Host header may look like to be passed on to the route — hostname or IPv4, optional
#: port, or a bracketed IPv6 literal. Anything else is replaced by `_BASE_URL`'s host.
_HOST_RE = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9.-]{0,252}|\[[0-9A-Fa-f:.]{2,45}\])"  # name, IPv4 or [IPv6]
    r"(?::\d{1,5})?"  # and a port
)

#: Bound at import. Tests (and nothing else) monkeypatch `httpx.AsyncClient` module-wide to fake
#: the box's own outbound calls; the tunnel must keep the real client either way.
_AsyncClient = httpx.AsyncClient
_ASGITransport = httpx.ASGITransport

#: The routes a panel may reach over the socket: (method, path pattern). Everything else is
#: answered 404 without touching the app.
_ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"/endpoint/settings")),
    ("GET", re.compile(r"/endpoint/firmware")),
    ("POST", re.compile(r"/endpoint/telemetry")),
    ("POST", re.compile(r"/endpoint/converse")),
    ("GET", re.compile(r"/jpanel/waiting")),
    ("GET", re.compile(r"/jpanel/next")),
    ("POST", re.compile(r"/jpanel/played")),
    ("POST", re.compile(r"/jpanel/send")),
    ("GET", re.compile(r"/jpanel/message/[0-9A-Za-z-]{1,64}/pcm")),
)

#: Request headers a panel may set. Authorization, cookies, Host and the forwarding headers are
#: the box's to set from the connection, never the frame's.
_PASS_REQUEST = frozenset({"content-type", "x-jpanel-sha256"})


def allowed(method: str, path: str) -> bool:
    """Whether a frame may reach this route. `path` may carry a query string."""
    bare = path.split("?", 1)[0]
    return any(method == m and pat.fullmatch(bare) for m, pat in _ALLOWED)


def frame(rid: int, data: bytes) -> bytes:
    """A binary body frame: the request id, little-endian u32, then the bytes."""
    return struct.pack("<I", rid) + data


def unframe(data: bytes) -> tuple[int, bytes] | None:
    """The id and payload of a binary frame, or None when it is too short to carry an id."""
    if len(data) < 4:
        return None
    return struct.unpack_from("<I", data)[0], data[4:]


def _bearer(authorization: str) -> str | None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


@dataclass
class _Pending:
    """A request whose body is still arriving."""

    rid: int
    method: str
    path: str
    headers: dict[str, str]
    length: int
    window: int
    #: The body as it arrived, joined once at dispatch — one copy, not one per chunk and another.
    chunks: list[bytes] = field(default_factory=list)
    got: int = 0
    last_progress: float = field(default_factory=time.monotonic)


@dataclass
class _Outgoing:
    """A response being sent under the panel's window."""

    granted: int  # bytes the panel has said it can take, cumulative; -1 is unlimited
    more: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Stats:
    """What the operator can see about one live socket (`GET /debug/endpoint/reach`)."""

    device: str
    label: str
    client: str
    connected_at: float
    last_rx: float
    requests: int = 0
    failed: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    events: int = 0


#: device_id -> the one live connection that device holds.
_live: dict[str, PanelSocket] = {}
#: device_id -> how many times a connection of its was replaced by a newer one, since start.
_replaced: dict[str, int] = {}


def snapshot() -> dict[str, dict[str, Any]]:
    """Every live panel socket, for the debug surface. Ages in whole seconds."""
    now = time.monotonic()
    out: dict[str, dict[str, Any]] = {}
    for device, conn in _live.items():
        s = conn.stats
        out[device] = {
            "label": s.label,
            "client": s.client,
            "up_s": int(now - s.connected_at),
            "rx_ago_s": int(now - s.last_rx),
            "requests": s.requests,
            "failed": s.failed,
            "inflight": len(conn.tasks),
            "bytes_in": s.bytes_in,
            "bytes_out": s.bytes_out,
            "events": s.events,
            "replaced": _replaced.get(device, 0),
        }
    return out


def safe_host(host: str | None) -> str:
    """The Host a replayed request carries: the panel's own when it is a plain host[:port], the
    fixed internal name otherwise."""
    if host and _HOST_RE.fullmatch(host):
        return host
    return _BASE_URL.removeprefix("http://")


def is_connected(device_id: str) -> bool:
    return device_id in _live


async def disconnect(device_ids: list[str], code: int = 4403) -> int:
    """Close the sockets of these devices — a revoked key must stop talking now, not at its
    next request. Returns how many were open."""
    closed = 0
    for device in device_ids:
        conn = _live.get(device)
        if conn is not None:
            closed += 1
            await conn.close(code)
    return closed


class PanelSocket:
    """One authenticated panel connection: a reader, a heartbeat, an event pump, and a task
    per request in flight."""

    def __init__(self, websocket: WebSocket, principal: PrincipalInfo, key: str) -> None:
        self.ws = websocket
        self.principal = principal
        self.key = key
        self.device = principal.id
        now = time.monotonic()
        client = websocket.client
        self.stats = Stats(
            device=principal.id,
            label=principal.label,
            client=f"{client.host}" if client else "",
            connected_at=now,
            last_rx=now,
        )
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self._pending: dict[int, _Pending] = {}
        self._out: dict[int, _Outgoing] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._http = self._client(websocket)

    @staticmethod
    def _client(websocket: WebSocket) -> httpx.AsyncClient:
        """An in-process HTTP client onto this very app, carrying what a direct request would.

        The URL is fixed (`_BASE_URL`); the Host HEADER is the one the panel connected with, when
        it is a plain host[:port], so a route that derives a URL from its own request (the
        firmware manifest) answers exactly what it answers over HTTPS. The client address and
        `X-Forwarded-For` are the socket's, so `nudge.remember` still learns where the panel is.
        No cookies: a jar would carry one request's Set-Cookie into the next."""
        client = websocket.client
        transport = _ASGITransport(
            app=websocket.app,
            raise_app_exceptions=False,
            client=(client.host, client.port) if client else ("127.0.0.1", 0),
        )
        return _AsyncClient(
            transport=transport,
            base_url=_BASE_URL,
            headers={"host": safe_host(websocket.headers.get("host"))},
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
            timeout=None,
        )

    # --- sending ----------------------------------------------------------------------------

    async def _send_json(self, obj: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_text(json.dumps(obj, separators=(",", ":")))

    async def _send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            await self.ws.send_bytes(data)
        self.stats.bytes_out += len(data)

    async def close(self, code: int) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self.ws.close(code=code)

    # --- the connection -----------------------------------------------------------------------

    async def run(self) -> None:
        # REGISTERED BEFORE ANYTHING AWAITS: two upgrades racing must each see the other, or
        # both would close a third and stay live side by side.
        old = _live.get(self.device)
        _live[self.device] = self
        if old is not None:
            # THE NEW ONE WINS. A panel only reconnects when it has given up on the old socket,
            # so the old one is at best half-open; keeping it would leave events going to a
            # connection nobody reads.
            _replaced[self.device] = _replaced.get(self.device, 0) + 1
            log.info("panel_ws.replaced", device=self.device)
            await old.close(4000)
        queue = nudge.attach(self.device)
        background = [
            asyncio.ensure_future(self._heartbeat()),
            asyncio.ensure_future(self._events(queue)),
            asyncio.ensure_future(self._reap()),
        ]
        log.info("panel_ws.open", device=self.device, label=self.stats.label)
        why = "closed"
        try:
            await self._send_json(
                {"t": "hello", "v": PROTOCOL_VERSION, "hb": HEARTBEAT_S, "idle": IDLE_S}
            )
            why = await self._read()
        except WebSocketDisconnect:
            why = "disconnect"
        finally:
            running = [*background, *self.tasks.values()]
            for t in running:
                t.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            nudge.detach(self.device, queue)
            if _live.get(self.device) is self:
                del _live[self.device]
            await self._http.aclose()
            await self.close(1000)
            s = self.stats
            log.info(
                "panel_ws.closed",
                device=self.device,
                why=why,
                up_s=int(time.monotonic() - s.connected_at),
                requests=s.requests,
                failed=s.failed,
                bytes_in=s.bytes_in,
                bytes_out=s.bytes_out,
            )

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await self._send_json({"t": "hb"})

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(REAP_S)
            cutoff = time.monotonic() - PENDING_STALL_S
            for rid in [r for r, p in self._pending.items() if p.last_progress < cutoff]:
                del self._pending[rid]
                log.warning("panel_ws.upload_stalled", device=self.device, rid=rid)
                await self._refuse(rid, 408, "upload stopped arriving")

    async def _events(self, queue: asyncio.Queue[str]) -> None:
        while True:
            why = await queue.get()
            self.stats.events += 1
            await self._send_json({"t": "ev", "why": why})

    async def _read(self) -> str:
        """The reader. Returns why the connection ended."""
        while True:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=IDLE_S)
            except TimeoutError:
                await self.close(4408)
                return "idle"
            if msg["type"] == "websocket.disconnect":
                return "disconnect"
            self.stats.last_rx = time.monotonic()
            data = msg.get("bytes")
            if data is not None:
                self.stats.bytes_in += len(data)
                if not await self._on_bytes(cast(bytes, data)):
                    await self.close(4400)
                    return "protocol"
                continue
            text = msg.get("text")
            if text is None:
                continue
            if not await self._on_text(cast(str, text)):
                await self.close(4400)
                return "protocol"

    async def _on_text(self, text: str) -> bool:
        if len(text) > MAX_TEXT:
            return False
        try:
            obj = json.loads(text)
        except ValueError:
            return False
        if not isinstance(obj, dict):
            return False
        kind = obj.get("t")
        if kind == "hb":
            return True
        rid = obj.get("id")
        if not isinstance(rid, int) or rid < 0 or rid > 0xFFFFFFFF:
            return False
        if kind == "ack":
            n = obj.get("n")
            out = self._out.get(rid)
            if out is not None and isinstance(n, int) and n > 0 and out.granted >= 0:
                out.granted += n
                out.more.set()
            return True
        if kind == "cancel":
            self._pending.pop(rid, None)
            task = self.tasks.get(rid)
            if task is not None:
                task.cancel()
            return True
        if kind == "req":
            return await self._on_req(rid, obj)
        return False

    async def _on_req(self, rid: int, obj: dict[str, Any]) -> bool:
        method = obj.get("m")
        path = obj.get("p")
        length = obj.get("len", 0)
        window = obj.get("win", 0)
        raw_headers = obj.get("h", {})
        if (
            not isinstance(method, str)
            or not isinstance(path, str)
            or not isinstance(length, int)
            or not isinstance(window, int)
            or not isinstance(raw_headers, dict)
            or length < 0
            or window < 0
        ):
            return False
        if rid in self.tasks or rid in self._pending:
            return False
        method = method.upper()
        if not allowed(method, path):
            # Refused, not fatal: a newer firmware asking for a route this box does not offer
            # over the socket should hear 404 and keep its connection.
            await self._refuse(rid, 404, "not reachable over the panel socket")
            return True
        if length > MAX_BODY:
            await self._refuse(rid, 413, "body too large")
            return True
        if len(self.tasks) + len(self._pending) >= MAX_INFLIGHT:
            await self._refuse(rid, 429, "too many requests in flight")
            return True
        headers = {
            str(k).lower(): str(v)
            for k, v in raw_headers.items()
            if str(k).lower() in _PASS_REQUEST and isinstance(v, str)
        }
        pending = _Pending(rid, method, path, headers, length, window)
        if length == 0:
            self._start(pending)
        else:
            self._pending[rid] = pending
        return True

    async def _on_bytes(self, data: bytes) -> bool:
        parsed = unframe(data)
        if parsed is None:
            return False
        rid, chunk = parsed
        pending = self._pending.get(rid)
        if pending is None:
            # A late chunk for a request that was cancelled or refused. Dropped, not fatal: the
            # panel may have queued it before it read the refusal.
            return True
        if pending.got + len(chunk) > pending.length:
            del self._pending[rid]
            await self._refuse(rid, 400, "body longer than declared")
            return True
        pending.chunks.append(chunk)
        pending.got += len(chunk)
        pending.last_progress = time.monotonic()
        if pending.got == pending.length:
            del self._pending[rid]
            self._start(pending)
        return True

    async def _refuse(self, rid: int, status: int, detail: str, *, count: bool = True) -> None:
        if count:
            self.stats.failed += 1
        body = json.dumps({"detail": detail}).encode()
        await self._send_json(
            {
                "t": "res",
                "id": rid,
                "s": status,
                "h": {"content-type": "application/json"},
                "len": len(body),
            }
        )
        await self._send_bytes(frame(rid, body))

    def _start(self, pending: _Pending) -> None:
        task = asyncio.ensure_future(self._dispatch(pending))
        self.tasks[pending.rid] = task
        task.add_done_callback(lambda _t, rid=pending.rid: self.tasks.pop(rid, None))

    async def _dispatch(self, req: _Pending) -> None:
        """Replay one request through the app and send its answer back under the window."""
        started = time.monotonic()
        self.stats.requests += 1
        headers = dict(req.headers)
        headers["authorization"] = f"Bearer {self.key}"
        fwd = self.ws.headers.get("x-forwarded-for")
        if fwd:
            headers["x-forwarded-for"] = fwd
        status = 0
        answered = False
        failed = False
        body_in = b"".join(req.chunks)
        req.chunks.clear()
        try:
            resp = await self._http.request(
                req.method, "/api" + req.path, content=body_in, headers=headers
            )
            status = resp.status_code
            body = resp.content
            passed = {
                k.lower(): v
                for k, v in resp.headers.items()
                if k.lower().startswith("x-") or k.lower() == "content-type"
            }
            out = _Outgoing(granted=req.window if req.window > 0 else -1)
            self._out[req.rid] = out
            await self._send_json(
                {"t": "res", "id": req.rid, "s": status, "h": passed, "len": len(body)}
            )
            answered = True
            sent = 0
            while sent < len(body):
                room = len(body) - sent if out.granted < 0 else out.granted - sent
                if room <= 0:
                    out.more.clear()
                    await asyncio.wait_for(out.more.wait(), timeout=WINDOW_STALL_S)
                    continue
                n = min(CHUNK, room)
                await self._send_bytes(frame(req.rid, body[sent : sent + n]))
                sent += n
        except TimeoutError:
            failed = True
            log.warning("panel_ws.window_stalled", device=self.device, path=req.path)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The socket died mid-answer, or the app raised past `raise_app_exceptions=False`.
            # Either way this request is over; the connection's own reader decides about the rest.
            failed = True
            log.warning(
                "panel_ws.dispatch_failed", device=self.device, path=req.path, exc_info=True
            )
            # An answer, if none has gone yet: a panel left to its own timeout would wait the
            # full fifty-five seconds of a talk turn to learn what the box knows now.
            if not answered:
                with contextlib.suppress(Exception):
                    await self._refuse(
                        req.rid, 502, "the box could not answer this request", count=False
                    )
        finally:
            self._out.pop(req.rid, None)
            # Once per request, however many ways it failed.
            if failed or status >= 400:
                self.stats.failed += 1
            # The access line uvicorn would have written, since these requests never pass it.
            log.info(
                "panel_ws.request",
                device=self.device,
                method=req.method,
                path=req.path.split("?", 1)[0],
                status=status,
                up_bytes=req.length,
                ms=int((time.monotonic() - started) * 1000),
            )


@router.websocket("/ws")
async def panel_socket(websocket: WebSocket) -> None:
    """A panel's single connection. Authenticated by the same device key, the same way, as
    every panel route (`Authorization: Bearer <device_key>`, kind-filtered so an owner or
    capability key resolves to nothing). Refused before the upgrade is accepted: the close is
    4401, but what crosses the wire is an HTTP 403 to the upgrade."""
    key = _bearer(websocket.headers.get("authorization", ""))
    repo = cast(AuthRepo, websocket.app.state.auth_repo)
    principal = await service.authenticate_device(repo, key) if key else None
    if principal is None or key is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await PanelSocket(websocket, principal, key).run()
