"""The panel's single socket: one TLS WebSocket carrying every request a panel makes.

The property that matters most is the one a socket could most easily break: that a request
over the socket reaches the SAME route, with the same authentication, as the same request over
HTTPS. So these tests drive the real app (`create_app`) and compare the two paths' answers
byte for byte, rather than testing a socket in isolation against stubs that could agree with
it while the routes moved on.
"""

import asyncio
import json
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from jbrain.api import endpoint as endpoint_api
from jbrain.api import nudge, panel_ws
from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit import test_endpoint_api as endpoint_tests
from tests.unit.fakes import FakeAuthRepo, FakeDeviceRepo

PANEL_KEY = "panel-key-for-the-socket-under-test"
WS = "/api/endpoint/ws"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    firmware = endpoint_tests._write_firmware(tmp_path / "firmware")
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        endpoint_url="http://endpoint:8000",
        firmware_dir=str(firmware),
    )
    app = create_app(settings)
    monkeypatch.setattr(endpoint_api, "_lan_ca", lambda: "")
    with TestClient(app) as c:
        app.state.auth_repo = FakeAuthRepo()
        app.state.device_repo = FakeDeviceRepo()
        asyncio.run(
            app.state.auth_repo.create_principal(
                "device_key", keys.hash_key(PANEL_KEY), "panel Elora"
            )
        )
        yield c
    panel_ws._live.clear()
    panel_ws._replaced.clear()


def _auth(key: str = PANEL_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _connect(c: TestClient, key: str = PANEL_KEY) -> WebSocketTestSession:
    return c.websocket_connect(WS, headers=_auth(key))


def _hello(ws: WebSocketTestSession) -> dict[str, Any]:
    hello = ws.receive_json()
    assert hello["t"] == "hello" and hello["v"] == panel_ws.PROTOCOL_VERSION
    return hello


def _request(
    ws: WebSocketTestSession,
    rid: int,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    chunk: int = 1000,
    win: int = 0,
    headers: dict[str, str] | None = None,
) -> None:
    ws.send_text(
        json.dumps(
            {
                "t": "req",
                "id": rid,
                "m": method,
                "p": path,
                "h": headers or {},
                "len": len(body),
                "win": win,
            }
        )
    )
    for i in range(0, len(body), chunk):
        ws.send_bytes(struct.pack("<I", rid) + body[i : i + chunk])


def _response(ws: WebSocketTestSession, rid: int) -> tuple[dict[str, Any], bytes]:
    """The `res` header and the reassembled body, skipping heartbeats and events."""
    head: dict[str, Any] | None = None
    while head is None:
        msg = ws.receive_json()
        if msg.get("t") == "res" and msg.get("id") == rid:
            head = msg
    body = bytearray()
    while len(body) < head["len"]:
        msg = ws.receive()
        if msg.get("bytes") is None:
            continue  # a heartbeat or an event between frames
        got_id, payload = panel_ws.unframe(msg["bytes"]) or (-1, b"")
        assert got_id == rid
        body.extend(payload)
    return head, bytes(body)


def _close_code(ws: WebSocketTestSession) -> int:
    """Read until the box closes the socket, and return the code it closed with."""
    while True:
        try:
            ws.receive_json()
        except WebSocketDisconnect as exc:
            return exc.code


class TestAuthentication:
    def test_no_key_is_refused_before_the_upgrade(self, client: TestClient) -> None:
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(WS):
            pass
        assert exc.value.code == 4401

    def test_a_wrong_key_is_refused(self, client: TestClient) -> None:
        with pytest.raises(WebSocketDisconnect) as exc, _connect(client, "not-a-key"):
            pass
        assert exc.value.code == 4401

    def test_the_owners_key_is_not_a_panel_key(self, client: TestClient) -> None:
        """Kind-filtered exactly like `PanelDep`'s bearer half: an owner key presented as a
        bearer token resolves to nothing, so the socket cannot become an owner surface."""
        owner = asyncio.run(
            auth_service.rotate_owner_key(cast(FastAPI, client.app).state.auth_repo)
        )
        with pytest.raises(WebSocketDisconnect) as exc, _connect(client, owner):
            pass
        assert exc.value.code == 4401

    def test_every_request_is_authenticated_again(self, client: TestClient) -> None:
        """A key revoked while its socket is open is refused at the next request — the tunnel
        presents the key to `PanelDep` each time rather than trusting the connection."""
        with _connect(client) as ws:
            _hello(ws)
            _request(ws, 1, "GET", "/endpoint/firmware")
            assert _response(ws, 1)[0]["s"] == 200
            asyncio.run(
                cast(FastAPI, client.app).state.auth_repo.revoke_principals_of_kind("device_key")
            )
            _request(ws, 2, "GET", "/endpoint/firmware")
            assert _response(ws, 2)[0]["s"] == 401


class TestSameRouteAsHttp:
    def test_the_manifest_is_byte_identical(self, client: TestClient) -> None:
        """The URL in the manifest is DERIVED from the request (`_public_base`), so this is the
        route most able to answer differently through a tunnel — the Host must carry over."""
        over_http = client.get("/api/endpoint/firmware", headers=_auth())
        assert over_http.status_code == 200
        with _connect(client) as ws:
            _hello(ws)
            _request(ws, 7, "GET", "/endpoint/firmware")
            head, body = _response(ws, 7)
        assert head["s"] == 200
        assert json.loads(body) == over_http.json()
        assert head["h"]["content-type"].startswith("application/json")

    def test_a_conversation_uploads_in_chunks_and_answers_the_same(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The talk turn: a chunked binary upload reassembled into one body, and a reply split
        into frames and reassembled on the panel — identical to the HTTPS reply."""
        endpoint_tests.TestConverse()._wire(client, monkeypatch, tts_long_ms=3000)
        audio = b"\x00\x01" * 16000  # a second, in 32 frames of 1000 bytes
        over_http = client.post("/api/endpoint/converse", content=audio, headers=_auth())
        assert over_http.status_code == 200
        with _connect(client) as ws:
            _hello(ws)
            _request(
                ws,
                3,
                "POST",
                "/endpoint/converse",
                audio,
                headers={"Content-Type": "application/octet-stream"},
            )
            head, body = _response(ws, 3)
        assert head["s"] == 200
        assert body == over_http.content
        assert len(body) > panel_ws.CHUNK  # it really did cross several frames

    def test_telemetry_is_accepted_and_kept_like_any_other_report(self, client: TestClient) -> None:
        report = {"version": "0.3.39", "uptime_ms": 1000, "ws": "ws", "int_min": 41000}
        with _connect(client) as ws:
            _hello(ws)
            _request(
                ws,
                4,
                "POST",
                "/endpoint/telemetry",
                json.dumps(report).encode(),
                headers={"Content-Type": "application/json"},
            )
            head, body = _response(ws, 4)
        assert head["s"] == 204 and body == b""

    def test_a_request_header_the_route_reads_is_passed_through(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`X-Jpanel-Sha256` is the integrity header a send carries; it must survive the tunnel,
        and the panel must not be able to set the credential or the forwarding headers."""
        seen: dict[str, str] = {}

        async def spy(app: Any, scope: Any, receive: Any, send: Any) -> None:
            seen.update({k.decode(): v.decode() for k, v in scope["headers"]})
            await app(scope, receive, send)

        real = panel_ws._ASGITransport

        def transport(app: Any, **kw: Any) -> Any:
            return real(app=lambda s, r, se: spy(app, s, r, se), **kw)

        monkeypatch.setattr(panel_ws, "_ASGITransport", transport)
        with _connect(client) as ws:
            _hello(ws)
            _request(
                ws,
                5,
                "GET",
                "/endpoint/firmware",
                headers={
                    "X-Jpanel-Sha256": "ab" * 32,
                    "Authorization": "Bearer evil",
                    "Cookie": "x=y",
                    "X-Forwarded-For": "6.6.6.6",
                },
            )
            assert _response(ws, 5)[0]["s"] == 200
        assert seen["x-jpanel-sha256"] == "ab" * 32
        assert seen["authorization"] == f"Bearer {PANEL_KEY}"
        assert "cookie" not in seen
        assert seen.get("x-forwarded-for") != "6.6.6.6"


class TestWhatTheSocketRefuses:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/endpoint/firmware/bin"),  # OTA stays on its own HTTPS download
            ("GET", "/jpanel/events"),  # replaced by the socket's own push frames
            ("GET", "/endpoint/status"),  # an owner route
            ("POST", "/endpoint/settings"),  # right path, wrong verb
            ("GET", "/jpanel/message/../../auth/pcm"),
        ],
    )
    def test_only_the_panel_routes_are_reachable(
        self, client: TestClient, method: str, path: str
    ) -> None:
        with _connect(client) as ws:
            _hello(ws)
            _request(ws, 1, method, path)
            head, _ = _response(ws, 1)
            assert head["s"] == 404
            # Refused, not disconnected: the next request still works.
            _request(ws, 2, "GET", "/endpoint/firmware")
            assert _response(ws, 2)[0]["s"] == 200

    def test_the_allowlist_takes_queries_but_not_lookalikes(self) -> None:
        assert panel_ws.allowed("GET", "/jpanel/next?at=2")
        assert panel_ws.allowed("POST", "/jpanel/send?to=dad")
        assert panel_ws.allowed("GET", "/jpanel/message/0b6c2f1e-1111-2222-3333-444455556666/pcm")
        assert not panel_ws.allowed("GET", "/jpanel/nextx")
        assert not panel_ws.allowed("GET", "/jpanel/message/a/b/pcm")

    def test_a_body_longer_than_declared_is_refused(self, client: TestClient) -> None:
        with _connect(client) as ws:
            _hello(ws)
            ws.send_text(
                json.dumps({"t": "req", "id": 9, "m": "POST", "p": "/endpoint/telemetry", "len": 4})
            )
            ws.send_bytes(struct.pack("<I", 9) + b"12345")
            assert _response(ws, 9)[0]["s"] == 400

    def test_a_body_over_the_ceiling_is_refused_before_it_is_sent(self, client: TestClient) -> None:
        with _connect(client) as ws:
            _hello(ws)
            ws.send_text(
                json.dumps(
                    {
                        "t": "req",
                        "id": 9,
                        "m": "POST",
                        "p": "/endpoint/converse",
                        "len": panel_ws.MAX_BODY + 1,
                    }
                )
            )
            assert _response(ws, 9)[0]["s"] == 413

    def test_garbage_closes_the_socket(self, client: TestClient) -> None:
        with _connect(client) as ws:
            _hello(ws)
            ws.send_text("this is not json")
            assert _close_code(ws) == 4400

    def test_a_binary_frame_too_short_for_an_id_closes_the_socket(self, client: TestClient) -> None:
        with _connect(client) as ws:
            _hello(ws)
            ws.send_bytes(b"\x01")
            assert _close_code(ws) == 4400

    def test_a_cancelled_upload_drops_its_late_chunks(self, client: TestClient) -> None:
        with _connect(client) as ws:
            _hello(ws)
            ws.send_text(
                json.dumps(
                    {"t": "req", "id": 11, "m": "POST", "p": "/endpoint/converse", "len": 100}
                )
            )
            ws.send_text(json.dumps({"t": "cancel", "id": 11}))
            ws.send_bytes(struct.pack("<I", 11) + b"x" * 100)
            _request(ws, 12, "GET", "/endpoint/firmware")
            assert _response(ws, 12)[0]["s"] == 200


class TestBackpressure:
    def test_the_box_never_sends_past_the_window(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A message streams into a four-second speaker ring; the box must wait for the panel
        to drain it. With the window spent, the next thing on the wire is a heartbeat, not
        audio — and an ack is what lets the audio resume."""
        monkeypatch.setattr(panel_ws, "HEARTBEAT_S", 0.05)
        endpoint_tests.TestConverse()._wire(client, monkeypatch, tts_long_ms=3000)
        audio = b"\x00\x01" * 1600
        expected = client.post("/api/endpoint/converse", content=audio, headers=_auth()).content
        win = panel_ws.CHUNK
        with _connect(client) as ws:
            _hello(ws)
            _request(ws, 21, "POST", "/endpoint/converse", audio, win=win)
            head: dict[str, Any] | None = None
            while head is None:
                msg = ws.receive_json()
                head = msg if msg.get("t") == "res" else None
            got = bytearray()
            while len(got) < win:
                msg = ws.receive()
                if msg.get("bytes") is not None:
                    got.extend(msg["bytes"][4:])
            assert len(got) == win
            # The window is spent: what comes next must be a heartbeat, never more audio.
            assert ws.receive_json()["t"] == "hb"
            while len(got) < head["len"]:
                ws.send_text(json.dumps({"t": "ack", "id": 21, "n": win}))
                msg = ws.receive()
                if msg.get("bytes") is not None:
                    got.extend(msg["bytes"][4:])
            assert bytes(got) == expected


class TestOneConnectionPerDevice:
    def test_a_new_connection_replaces_the_old(self, client: TestClient) -> None:
        with _connect(client) as first:
            _hello(first)
            with _connect(client) as second:
                _hello(second)
                assert _close_code(first) == 4000
                _request(second, 1, "GET", "/endpoint/firmware")
                assert _response(second, 1)[0]["s"] == 200
                snap = panel_ws.snapshot()
                assert len(snap) == 1
                (row,) = snap.values()
                assert row["replaced"] == 1 and row["requests"] == 1
        assert panel_ws.snapshot() == {}

    def test_a_revoked_panel_is_cut_off(self, client: TestClient) -> None:
        with _connect(client) as ws:
            _hello(ws)
            (device,) = panel_ws.snapshot()
            assert panel_ws.is_connected(device)
            portal = cast(Any, client).portal
            assert portal.call(panel_ws.disconnect, [device]) == 1
            assert _close_code(ws) == 4403


class TestLiveness:
    def test_a_silent_panel_is_dropped(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(panel_ws, "IDLE_S", 0.2)
        with _connect(client) as ws:
            _hello(ws)
            assert _close_code(ws) == 4408

    def test_the_box_beats_and_a_beating_panel_stays(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(panel_ws, "HEARTBEAT_S", 0.05)
        monkeypatch.setattr(panel_ws, "IDLE_S", 0.5)
        with _connect(client) as ws:
            hello = _hello(ws)
            assert hello["hb"] == 0.05 and hello["idle"] == 0.5
            for _ in range(15):  # ~0.75 s of the box beating, the panel answering each beat
                assert ws.receive_json()["t"] == "hb"
                ws.send_text(json.dumps({"t": "hb"}))
            _request(ws, 1, "GET", "/endpoint/firmware")
            assert _response(ws, 1)[0]["s"] == 200


class TestPush:
    def test_a_nudge_arrives_as_an_event_frame(self, client: TestClient) -> None:
        """What `GET /jpanel/events` carried, now on the one socket — and still only the word
        "come and ask", never data the panel acts on."""
        with _connect(client) as ws:
            _hello(ws)
            (device,) = panel_ws.snapshot()
            assert nudge.is_connected(device)
            portal = cast(Any, client).portal
            assert portal.call(nudge.fire, device, "message")
            assert ws.receive_json() == {"t": "ev", "why": "message"}
            assert panel_ws.snapshot()[device]["events"] == 1
        assert not nudge.is_connected(device)


class TestFraming:
    def test_frame_round_trips(self) -> None:
        assert panel_ws.unframe(panel_ws.frame(0xDEADBEEF, b"abc")) == (0xDEADBEEF, b"abc")
        assert panel_ws.frame(1, b"") == b"\x01\x00\x00\x00"
        assert panel_ws.unframe(b"\x01\x00") is None

    def test_the_firmware_spells_the_same_route_and_frame(self) -> None:
        """The two ends of a protocol written down twice — pinned like the nudge port."""
        fw = Path(__file__).resolve().parents[3] / "firmware" / "main"
        proto = (fw / "wsproto.h").read_text(encoding="utf-8")
        link = (fw / "link.c").read_text(encoding="utf-8")
        assert '"/endpoint/ws"' in link, "the panel no longer connects to this route"
        assert f"#define WSP_VERSION {panel_ws.PROTOCOL_VERSION}" in proto
        assert "#define WSP_ID_BYTES 4" in proto
        assert f"#define WSP_HEARTBEAT_MS {int(panel_ws.HEARTBEAT_S * 1000)}" in proto
        hb = int(panel_ws.HEARTBEAT_S * 1000)
        assert hb * 3 < panel_ws.IDLE_S * 1000, "three missed panel beats must fit the idle cut"


class TestTelemetryCarriesTheSocket:
    def test_the_new_fields_are_kept(self) -> None:
        body = endpoint_api.TelemetryIn(
            version="0.3.39",
            uptime_ms=1,
            ws="http",
            ws_err="tls=0x8017 sock=113 hs=0",
            ws_connects=3,
            ws_drops=2,
            int_min=38000,
            int_big=24000,
        )
        dumped = body.model_dump(mode="json")
        for key in ("ws", "ws_err", "ws_connects", "ws_drops", "int_min", "int_big"):
            assert key in dumped


class TestABoxThatCannotAnswer:
    def test_an_exploding_request_still_gets_an_answer(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A request the box cannot replay is answered 502 at once, rather than leaving the panel
        to sit out its own timeout — fifty-five seconds, on a talk turn."""

        async def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("the app is on fire")

        with _connect(client) as ws:
            _hello(ws)
            (device,) = panel_ws.snapshot()
            conn = panel_ws._live[device]
            monkeypatch.setattr(conn._http, "request", boom)
            _request(ws, 1, "GET", "/endpoint/firmware")
            assert _response(ws, 1)[0]["s"] == 502
