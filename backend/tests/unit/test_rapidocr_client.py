"""RapidOcrClient: POST image bytes to the pinned sidecar, parse the transcription, and map
every failure to OcrServiceError. HTTP is faked via MockTransport — no live network, like
the SearxngClient tests (docs/plans/RAPIDOCR_PLAN.md)."""

import httpx
import pytest

from jbrain.vision import OcrResult, OcrServiceError, RapidOcrClient


def _client(handler) -> RapidOcrClient:  # type: ignore[no-untyped-def]
    return RapidOcrClient("http://rapidocr:8000", transport=httpx.MockTransport(handler))


async def test_ocr_posts_bytes_and_parses_result() -> None:
    seen: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={
                "text": "hello\nworld",
                "mean_score": 0.91,
                "lines": [{"text": "hello", "box": [], "score": 0.9}],
            },
        )

    result = await _client(handle).ocr(b"\x89PNGdata", media_type="image/png")
    assert isinstance(result, OcrResult)
    assert result.text == "hello\nworld"
    assert result.mean_score == pytest.approx(0.91)
    assert len(result.lines) == 1
    assert str(seen["url"]).endswith("/ocr")
    assert seen["body"] == b"\x89PNGdata"


async def test_ocr_empty_transcription() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "", "mean_score": 0.0, "lines": []})

    result = await _client(handle).ocr(b"img")
    assert result.text == "" and result.lines == ()


async def test_ocr_non_2xx_raises() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(OcrServiceError):
        await _client(handle).ocr(b"img")


async def test_ocr_unreachable_raises() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    with pytest.raises(OcrServiceError):
        await _client(handle).ocr(b"img")


async def test_ocr_malformed_body_raises() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    with pytest.raises(OcrServiceError):
        await _client(handle).ocr(b"img")


async def test_ocr_unconfigured_raises() -> None:
    with pytest.raises(OcrServiceError):
        await RapidOcrClient("").ocr(b"img")


async def test_ocr_empty_input_raises() -> None:
    with pytest.raises(OcrServiceError):
        await RapidOcrClient("http://rapidocr:8000").ocr(b"")
