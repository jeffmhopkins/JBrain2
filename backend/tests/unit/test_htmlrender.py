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
    max_css_height,
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
    assert (await _client(_ok).render("<b>hi</b>", width=10, height=10)).png == PNG


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


@pytest.mark.asyncio
async def test_omitting_height_asks_the_sidecar_to_measure() -> None:
    # The standalone-card mode: a null height is what turns on measure-and-fit, so a
    # short card comes back short instead of padded to a guess (the second-frame bug).
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(
            200,
            json={"png_base64": base64.b64encode(PNG).decode(), "width": 900, "height": 412},
        )

    rendered = await _client(handler).render("<p>x</p>", width=900, transparent=False, pad=28)
    assert seen["height"] is None
    assert (seen["transparent"], seen["pad"]) == (False, 28)
    # The dimensions come back from the sidecar, not from the request — nothing on this
    # side knows how tall the content turned out to be.
    assert (rendered.width, rendered.height) == (900, 412)


@pytest.mark.asyncio
async def test_reported_dimensions_win_over_the_requested_ones() -> None:
    # The PWA card sets its frame's aspect-ratio from these; a disagreement crops the
    # image, so the sidecar's account of the PNG is the one that counts.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"png_base64": base64.b64encode(PNG).decode(), "width": 640, "height": 480},
        )

    rendered = await _client(handler).render("<b>x</b>", width=320, height=100)
    assert (rendered.width, rendered.height) == (640, 480)


@pytest.mark.asyncio
async def test_a_sidecar_without_dimensions_falls_back_to_the_request() -> None:
    # An older sidecar predates the honest-dimensions response; a zero here would
    # collapse the card's aspect ratio, so the request's own numbers stand in.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"png_base64": base64.b64encode(PNG).decode()})

    rendered = await _client(handler).render("<b>x</b>", width=320, height=100)
    assert (rendered.width, rendered.height) == (320, 100)
    assert rendered.clipped is False


@pytest.mark.asyncio
async def test_clipped_is_relayed_so_a_caller_can_say_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "png_base64": base64.b64encode(PNG).decode(),
                "width": 900,
                "height": 4096,
                "clipped": True,
            },
        )

    assert (await _client(handler).render("<p>x</p>", width=900)).clipped is True


@pytest.mark.asyncio
async def test_unknown_theme_is_refused_locally() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("should not have called the sidecar")

    with pytest.raises(HtmlRenderError, match="theme must be"):
        await _client(handler).render("<b>x</b>", width=10, height=10, theme="neon")


@pytest.mark.asyncio
async def test_scale_is_counted_against_the_caps() -> None:
    # Both caps are on the OUTPUT image: a 3000px viewport at 2x is a 6000px PNG, which
    # is over MAX_SIDE even though the viewport itself is not.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("should not have called the sidecar")

    with pytest.raises(HtmlRenderError, match="after scaling"):
        await _client(handler).render("<b>x</b>", width=3000, height=100, scale=2.0)
    with pytest.raises(HtmlRenderError, match="too many pixels"):
        await _client(handler).render("<b>x</b>", width=1000, height=3000, scale=2.0)


def test_max_css_height_keeps_the_output_inside_both_caps() -> None:
    for width, scale in ((900, 2.0), (1600, 2.0), (320, 1.0), (MAX_SIDE, 1.0)):
        height = max_css_height(width, scale)
        out_w, out_h = round(width * scale), round(height * scale)
        assert out_h <= MAX_SIDE
        assert out_w * out_h <= MAX_PIXELS


@pytest.mark.asyncio
async def test_a_measured_render_with_no_reported_height_is_an_error() -> None:
    # In measure mode nothing on this side knows how tall the page came out, so a
    # missing height is a malformed response — not a zero to hand to a caller that
    # would size a frame from it and collapse the aspect ratio.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"png_base64": base64.b64encode(PNG).decode()})

    with pytest.raises(HtmlRenderError, match="no dimensions"):
        await _client(handler).render("<p>x</p>", width=900)
