"""The authenticated api -> on-box tts-stt TTS proxy (jbrain.api.brain).

The PWA read-aloud + voice picker reach the unauthenticated LAN display through the
owner's api session: /api/brain/voices lists the box's Kokoro voices and /api/brain/tts
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
        app.state.brain_tts_base_url = "http://tts-stt:8801"
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
        assert anon.get("/api/brain/tts/health").status_code == 401
        assert anon.get("/api/brain/tts", params={"text": "hi", "voice": "v"}).status_code == 401


def test_tts_health_proxies_the_engine_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The diagnostic the Settings panel reads: is the box on misaki (lexicon live) or espeak?
    status = {"kokoro_available": True, "g2p": "espeak", "lexicon_entries": 1, "voice_count": 5}
    calls = _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(200, json_data=status))
    resp = client.get("/api/brain/tts/health")
    assert resp.status_code == 200
    assert resp.json() == status
    assert calls == [("http://tts-stt:8801/tts/health", None)]


def test_tts_health_503_when_display_unconfigured(client: TestClient) -> None:
    client.app.state.brain_tts_base_url = ""  # type: ignore[attr-defined]
    assert client.get("/api/brain/tts/health").status_code == 503


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
    assert calls == [("http://tts-stt:8801/tts/voices", None)]


def test_voices_503_when_display_unconfigured(client: TestClient) -> None:
    client.app.state.brain_tts_base_url = ""  # type: ignore[attr-defined]
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
    assert url == "http://tts-stt:8801/tts"
    assert params == {
        "text": "Hello there.",
        "voice": "en_US-libritts_r-medium#3922",
        "lead": "2000",  # clamped to the 0..2000 ceiling
    }


def test_tts_forwards_clamped_speed_and_trail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The PWA's markup-vs-prose classifier sends audiobook pacing; the proxy clamps + forwards it.
    calls = _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(200, content=b"wav"))
    resp = client.get(
        "/api/brain/tts",
        params={"text": "A quiet line.", "voice": "kokoro-af_heart", "speed": 9.0, "trail": 9000},
    )
    assert resp.status_code == 200
    _url, params = calls[0]
    assert params == {
        "text": "A quiet line.",
        "voice": "kokoro-af_heart",
        "speed": "2.0",  # clamped to the 0.5..2.0 ceiling
        "trail": "3000",  # clamped to the 0..3000 ceiling
    }


def test_tts_omits_pacing_params_when_absent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A markup turn sends no speed/trail → the box's env default applies; nothing extra forwarded.
    calls = _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(200, content=b"wav"))
    client.get("/api/brain/tts", params={"text": "hi", "voice": "kokoro-af_heart"})
    _url, params = calls[0]
    assert params == {"text": "hi", "voice": "kokoro-af_heart"}


def test_tts_forwards_clamped_voice_effects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The PWA sends the owner's chosen effects; the proxy clamps pitch and forwards chorus/robot
    # as the box's 1/0 flags.
    calls = _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(200, content=b"wav"))
    resp = client.get(
        "/api/brain/tts",
        params={
            "text": "A line.",
            "voice": "kokoro-af_heart",
            "pitch": 99.0,
            "chorus": "true",
            "robot": "false",
        },
    )
    assert resp.status_code == 200
    _url, params = calls[0]
    assert params == {
        "text": "A line.",
        "voice": "kokoro-af_heart",
        "pitch": "12.0",  # clamped to the ±12 semitone ceiling
        "chorus": "1",
        "robot": "0",
    }


def test_tts_omits_effect_params_when_absent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No effects → nothing extra forwarded, so a plain read never spawns ffmpeg on the box.
    calls = _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(200, content=b"wav"))
    client.get("/api/brain/tts", params={"text": "hi", "voice": "kokoro-af_heart"})
    _url, params = calls[0]
    assert "pitch" not in params and "chorus" not in params and "robot" not in params


def test_tts_applies_the_owner_pronunciation_lexicon(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner's respelling map is applied (whole-word, case-insensitive) before the text is
    # forwarded to the box — so a fixed word renders right regardless of the box's phonemizer.
    from tests.unit.fakes import FakeSettingsStore

    store = FakeSettingsStore()
    store.values["pronunciation_lexicon"] = {"Titusville": "Tight us ville"}
    client.app.state.settings_store = store  # type: ignore[attr-defined]
    calls = _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(200, content=b"wav"))
    resp = client.get(
        "/api/brain/tts",
        params={"text": "visiting titusville today", "voice": "kokoro-af_heart"},
    )
    assert resp.status_code == 200
    _url, params = calls[0]
    assert params is not None
    assert params["text"] == "visiting Tight us ville today"  # respelled, case-insensitive match


def test_tts_survives_a_lexicon_read_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A settings-read hiccup must never fail read-aloud — it just skips the respelling.
    class _BoomStore:
        async def pronunciation_lexicon(self, ctx: object) -> dict[str, str]:
            raise RuntimeError("db down")

    client.app.state.settings_store = _BoomStore()  # type: ignore[attr-defined]
    calls = _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(200, content=b"wav"))
    resp = client.get("/api/brain/tts", params={"text": "hello there", "voice": "kokoro-af_heart"})
    assert resp.status_code == 200
    params = calls[0][1]
    assert params is not None
    assert params["text"] == "hello there"  # forwarded unchanged, no crash


def test_tts_rejects_blank_text(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(200, content=b"x"))
    assert client.get("/api/brain/tts", params={"text": "   ", "voice": "v"}).status_code == 400
    assert calls == []  # never reached the box


def test_tts_502_when_render_fails(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_httpx(monkeypatch, lambda url, params: _FakeResp(503, content=b""))
    assert client.get("/api/brain/tts", params={"text": "hi", "voice": "v"}).status_code == 502
