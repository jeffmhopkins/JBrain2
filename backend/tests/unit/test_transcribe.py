"""WhisperCppClient: the multipart request shape and the tolerant response parse.

A MockTransport answers the gateway so the test is deterministic and offline —
the same seam jbrain.transcribe documents for the job/tool tests.
"""

from collections.abc import Callable

import httpx
import pytest

from jbrain.transcribe import Transcript, WhisperCppClient

WAV = b"RIFF....fake-audio-bytes"

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> WhisperCppClient:
    return WhisperCppClient(
        "http://gw/v1", "whisper-large-v3", transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_posts_multipart_audio_with_model_and_parses_text() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["ctype"] = req.headers.get("content-type", "")
        seen["body"] = req.content
        return httpx.Response(200, json={"text": "  hello world ", "language": "en"})

    result = await make_client(handler).transcribe(WAV, filename="memo.wav", media_type="audio/wav")

    assert result == Transcript(text="hello world", language="en")
    # The base URL ends in /v1 (gateway convention); the endpoint must not double it.
    assert seen["url"] == "http://gw/v1/audio/transcriptions"
    assert "multipart/form-data" in str(seen["ctype"])
    # The model name and the audio bytes both ride the multipart body.
    assert b'name="model"' in bytes(seen["body"])  # type: ignore[arg-type]
    assert b"whisper-large-v3" in bytes(seen["body"])  # type: ignore[arg-type]
    assert WAV in bytes(seen["body"])  # type: ignore[arg-type]
    assert b"memo.wav" in bytes(seen["body"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_missing_language_is_none_and_text_is_stripped() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "\njust text\n"})

    result = await make_client(handler).transcribe(WAV, filename="a.mp3", media_type="audio/mpeg")
    assert result == Transcript(text="just text", language=None)


@pytest.mark.asyncio
async def test_bare_string_body_is_accepted() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="  plain ")

    result = await make_client(handler).transcribe(WAV, filename="a.ogg", media_type="audio/ogg")
    assert result == Transcript(text="plain", language=None)


@pytest.mark.asyncio
async def test_http_error_propagates() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model loading")

    with pytest.raises(httpx.HTTPStatusError):
        await make_client(handler).transcribe(WAV, filename="a.wav", media_type="audio/wav")
