"""The authenticated api -> on-box server-brain TTS proxy (jbrain.api.brain).

The PWA read-aloud + voice picker reach the unauthenticated LAN display through the
owner's api session: /api/brain/voices lists the box's piper voices and /api/brain/tts
renders a clip. httpx is faked (no live box), and the routes require a session.
"""

import asyncio
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo


class _FakeResp:
    def __init__(self, status_code: int, *, json_data: object = None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self) -> object:
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[str, dict | None], _FakeResp]
) -> list[tuple[str, dict | None]]:
    """Route jbrain.api.brain's httpx GETs through `handler`, recording (url, params)."""
    calls: list[tuple[str, dict | None]] = []

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def get(self, url: str, params: dict | None = None) -> _FakeResp:
            calls.append((url, params))
            return handler(url, params)

    monkeypatch.setattr("jbrain.api.brain.httpx.AsyncClient", _FakeClient)
    return calls


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    auth_repo = FakeAuthRepo()
    with TestClient(app) as test_client:
        app.state.auth_repo = auth_repo
        app.state.brain_base_url = "http://server-brain:8800"
        key = asyncio.run(auth_service.rotate_owner_key(auth_repo))
        assert (
            test_client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield test_client


def test_voices_and_tts_require_auth() -> None:
    app = create_app(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/brain/voices").status_code == 401
        assert anon.get("/api/brain/tts", params={"text": "hi", "voice": "v"}).status_code == 401


def test_voices_proxies_the_installed_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    voices = ["en_US-amy-medium", "en_US-libritts_r-medium#3922"]
    calls = _install_fake_httpx(
        monkeypatch, lambda url, params: _FakeResp(200, json_data={"voices": voices})
    )
    resp = client.get("/api/brain/voices")
    assert resp.status_code == 200
    assert resp.json() == {"voices": voices}
    assert calls == [("http://server-brain:8800/tts/voices", None)]


def test_voices_503_when_display_unconfigured(client: TestClient) -> None:
    client.app.state.brain_base_url = ""  # type: ignore[attr-defined]
    assert client.get("/api/brain/voices").status_code == 503


def test_voices_503_when_display_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(url: str, params: dict | None) -> _FakeResp:
        raise httpx.ConnectError("no route")

    _install_fake_httpx(monkeypatch, boom)
    assert client.get("/api/brain/voices").status_code == 503


def test_tts_proxies_audio_with_voice_and_clamped_lead(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(200, content=b"RIFFwav"))
    resp = client.get(
        "/api/brain/tts",
        params={"text": "Hello there.", "voice": "en_US-libritts_r-medium#3922", "lead": 9000},
    )
    assert resp.status_code == 200
    assert resp.content == b"RIFFwav"
    assert resp.headers["content-type"] == "audio/wav"
    url, params = calls[0]
    assert url == "http://server-brain:8800/tts"
    assert params == {
        "text": "Hello there.",
        "voice": "en_US-libritts_r-medium#3922",
        "lead": "2000",  # clamped to the 0..2000 ceiling
    }


def test_tts_rejects_blank_text(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(200, content=b"x"))
    assert client.get("/api/brain/tts", params={"text": "   ", "voice": "v"}).status_code == 400
    assert calls == []  # never reached the box


def test_tts_502_when_render_fails(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(503, content=b""))
    assert client.get("/api/brain/tts", params={"text": "hi", "voice": "v"}).status_code == 502
