"""The jcode terminal WS proxy: the URL builder, the byte pumps, and the auth gate.

The live socket *bridge* (racing both pumps, opening the upstream connection) is
deploy-only glue (pragma: no cover) — so the testable core is the two directional
pumps and the URL build, plus the owner/origin rejection driven through the endpoint
(mirrors test_live.py)."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jbrain.api.jcode_terminal import browser_to_upstream, upstream_to_browser, ws_url
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo


def test_ws_url_builds_the_terminal_endpoint() -> None:
    assert ws_url("http://jcode:9100", "abc123") == "ws://jcode:9100/sessions/abc123/terminal"
    assert ws_url("https://jcode:9100/", "x") == "wss://jcode:9100/sessions/x/terminal"


class _FakeUpstream:
    """Stands in for the websockets client connection: records sends, async-iterates a
    scripted set of inbound messages."""

    def __init__(self, inbound: list[bytes | str] | None = None) -> None:
        self.sent: list[bytes | str] = []
        self._inbound = inbound or []

    async def send(self, data: bytes | str) -> None:
        self.sent.append(data)

    def __aiter__(self):
        async def gen():
            for m in self._inbound:
                yield m

        return gen()


class _FakeBrowser:
    """Stands in for the Starlette WebSocket: scripted receive() frames, recorded sends."""

    def __init__(self, frames: list[dict]) -> None:
        self._frames = frames
        self.binary: list[bytes] = []
        self.text: list[str] = []

    async def receive(self) -> dict:
        return self._frames.pop(0)

    async def send_bytes(self, data: bytes) -> None:
        self.binary.append(data)

    async def send_text(self, data: str) -> None:
        self.text.append(data)


def test_browser_to_upstream_forwards_bytes_and_text_until_disconnect() -> None:
    browser = _FakeBrowser(
        [
            {"type": "websocket.receive", "bytes": b"ls\n"},
            {"type": "websocket.receive", "text": '{"resize":{"rows":40,"cols":120}}'},
            {"type": "websocket.disconnect"},
        ]
    )
    upstream = _FakeUpstream()
    asyncio.run(browser_to_upstream(browser, upstream))
    # Keystrokes and the resize control both reach the shell, in order; disconnect ends it.
    assert upstream.sent == [b"ls\n", '{"resize":{"rows":40,"cols":120}}']


def test_upstream_to_browser_preserves_binary_vs_text() -> None:
    upstream = _FakeUpstream(inbound=[b"\x1b[2J", "plain"])
    browser = _FakeBrowser([])
    asyncio.run(upstream_to_browser(upstream, browser))
    assert browser.binary == [b"\x1b[2J"]
    assert browser.text == ["plain"]


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        jcode_url="http://jcode:9100",
        jcode_token="t",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = FakeAuthRepo()
        yield test_client


def test_terminal_ws_rejects_without_a_session_cookie(client: TestClient) -> None:
    # No owner cookie -> closed 4401 before the upstream socket is opened.
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/api/jcode/sessions/abc123/terminal"),
    ):
        pass
    assert exc.value.code == 4401


def test_terminal_ws_rejects_a_disallowed_origin() -> None:
    # An allow-list set -> a cross-site Origin is closed 4403 before auth (CSWSH).
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        dashboard_allowed_origins="https://dash.example",
        jcode_url="http://jcode:9100",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect(
                "/api/jcode/sessions/abc123/terminal",
                headers={"origin": "https://evil.example"},
            ),
        ):
            pass
    assert exc.value.code == 4403
