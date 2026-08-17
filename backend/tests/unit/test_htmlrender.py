"""The htmlrender sidecar client (jbrain.htmlrender) — AGENT_CANVAS_PLAN §3b.

No network: every case runs against an injected httpx MockTransport, per
DEVELOPMENT.md. What matters here is that a sidecar problem becomes a recoverable,
model-readable error rather than a crash or a silently blank image.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from jbrain.htmlrender import (
    MAX_HTML_BYTES,
    MAX_PIXELS,
    MAX_SIDE,
    HtmlRenderClient,
    HtmlRenderError,
)

PNG = b"\x89PNG\r\n\x1a\nfake"


def _client(handler) -> HtmlRenderClient:
    return HtmlRenderClient("http://htmlrender:8000", transport=httpx.MockTransport(handler))


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"png_base64": base64.b64encode(PNG).decode(), "width": 10, "height": 10}
    )


@pytest.mark.asyncio
async def test_returns_decoded_png() -> None:
    assert await _client(_ok).render("<b>hi</b>", width=10, height=10) == PNG


@pytest.mark.asyncio
async def test_sends_the_expected_payload() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(__import__("json").loads(request.content))
        assert request.url.path == "/render"
        return _ok(request)

    await _client(handler).render("<p>x</p>", width=320, height=100)
    # Transparent by default: the caller composites over a photo the sidecar must
    # never receive.
    assert seen["transparent"] is True
    assert (seen["width"], seen["height"]) == (320, 100)


@pytest.mark.asyncio
async def test_unconfigured_is_a_clean_recoverable_error() -> None:
    client = HtmlRenderClient("")
    assert client.configured is False
    with pytest.raises(HtmlRenderError, match="not configured"):
        await client.render("<b>x</b>", width=10, height=10)


@pytest.mark.asyncio
async def test_unreachable_sidecar_is_recoverable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(HtmlRenderError, match="could not be reached"):
        await _client(handler).render("<b>x</b>", width=10, height=10)


@pytest.mark.asyncio
async def test_sidecar_rejection_surfaces_its_reason_to_the_model() -> None:
    # The model needs to know WHICH thing it did was wrong, so the detail is relayed.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "render failed: Timeout 15000ms exceeded"})

    with pytest.raises(HtmlRenderError, match="Timeout 15000ms"):
        await _client(handler).render("<b>x</b>", width=10, height=10)


@pytest.mark.asyncio
async def test_non_json_error_body_still_produces_a_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(HtmlRenderError, match="bad gateway"):
        await _client(handler).render("<b>x</b>", width=10, height=10)


@pytest.mark.asyncio
async def test_missing_image_in_a_200_is_an_error_not_a_blank() -> None:
    # A blank overlay would composite as "nothing drew" and look like the model's fault.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"width": 10, "height": 10})

    with pytest.raises(HtmlRenderError, match="no image"):
        await _client(handler).render("<b>x</b>", width=10, height=10)


@pytest.mark.asyncio
async def test_corrupt_base64_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"png_base64": "!!!not base64!!!"})

    with pytest.raises(HtmlRenderError, match="corrupt"):
        await _client(handler).render("<b>x</b>", width=10, height=10)


@pytest.mark.asyncio
async def test_empty_html_is_refused_before_the_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("should not have called the sidecar")

    with pytest.raises(HtmlRenderError, match="empty"):
        await _client(handler).render("   ", width=10, height=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"width": 0, "height": 10}, "within 1"),
        ({"width": MAX_SIDE + 1, "height": 10}, "within 1"),
        ({"width": MAX_SIDE, "height": MAX_SIDE}, "too many pixels"),
    ],
)
async def test_bad_dimensions_are_refused_locally(kwargs: dict, match: str) -> None:
    # Caught here so the model gets a sentence it can act on, not a 413 from a container.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("should not have called the sidecar")

    assert MAX_SIDE * MAX_SIDE > MAX_PIXELS  # the third case is genuinely over
    with pytest.raises(HtmlRenderError, match=match):
        await _client(handler).render("<b>x</b>", **kwargs)


@pytest.mark.asyncio
async def test_oversized_html_is_refused_locally() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("should not have called the sidecar")

    with pytest.raises(HtmlRenderError, match="too large"):
        await _client(handler).render("x" * (MAX_HTML_BYTES + 1), width=10, height=10)
