"""Scene → image composition (jbrain.draw.compose) — AGENT_CANVAS_PLAN §3b.

The contract worth pinning: an html block that fails to render must not take the rest
of the figure with it, and the sidecar must never be handed the owner's photograph.
"""

from __future__ import annotations

import base64
import io
from typing import cast

import httpx
import pytest
from PIL import Image

from jbrain.draw.compose import compose_png
from jbrain.draw.scene import Background, Scene, apply_ops
from jbrain.htmlrender import HtmlRenderClient


def _overlay_png(width: int, height: int, colour: tuple[int, int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (width, height), colour).save(buf, format="PNG")
    return buf.getvalue()


def _photo(width: int = 400, height: int = 300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (20, 140, 20)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _client(handler) -> HtmlRenderClient:
    return HtmlRenderClient("http://htmlrender:8000", transport=httpx.MockTransport(handler))


def _scene_with_html(markup: str = "<b>hi</b>") -> Scene:
    scene = Scene(
        canvas_id="cv", background=Background(kind="attachment", width=400, height=300, ref="a")
    )
    scene, report = apply_ops(
        scene,
        [
            {"op": "rect", "x": 20, "y": 20, "w": 100, "h": 60},
            {"op": "html", "x": 40, "y": 200, "w": 200, "h": 60, "html": markup},
        ],
    )
    assert report.skipped == []
    return scene


@pytest.mark.asyncio
async def test_html_block_is_composited_at_its_rect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        # Rendered at the block's own pixel size so text lays out against the width
        # the model actually chose.
        assert (body["width"], body["height"]) == (200, 60)
        png = _overlay_png(body["width"], body["height"], (255, 0, 0, 255))
        return httpx.Response(200, json={"png_base64": base64.b64encode(png).decode()})

    png, notes = await compose_png(_scene_with_html(), _photo(), _client(handler))
    assert notes == []
    img = Image.open(io.BytesIO(png)).convert("RGB")
    inside = cast("tuple[int, int, int]", img.getpixel((140, 230)))
    outside = cast("tuple[int, int, int]", img.getpixel((350, 280)))
    assert inside[0] > 200  # inside the block: the red overlay
    assert outside[1] > 100  # outside it: the green photo


@pytest.mark.asyncio
async def test_the_photo_is_never_sent_to_the_sidecar() -> None:
    # The whole reason the overlay is transparent: a health-scoped photo must not
    # reach a browser container. Assert the payload carries markup and nothing else.
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={"png_base64": base64.b64encode(_overlay_png(200, 60, (0, 0, 0, 0))).decode()},
        )

    photo = _photo()
    await compose_png(_scene_with_html(), photo, _client(handler))
    assert len(seen) == 1
    assert seen[0]["transparent"] is True
    assert set(seen[0]) == {"html", "width", "height", "transparent", "wait_ms", "scale"}
    serialized = __import__("json").dumps(seen[0]).encode()
    assert base64.b64encode(photo)[:32] not in serialized
    assert photo[:16] not in serialized


@pytest.mark.asyncio
async def test_a_failed_block_is_reported_and_the_rest_still_renders() -> None:
    # Partial-apply posture, same as the op fold: the figure is still worth showing
    # and the note tells the model exactly which block to fix.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "render failed: bad css"})

    png, notes = await compose_png(_scene_with_html(), _photo(), _client(handler))
    assert len(notes) == 1
    assert "e2 (html) did not render" in notes[0]
    assert "bad css" in notes[0]
    assert Image.open(io.BytesIO(png)).size == (400, 300)  # the rect still drew


@pytest.mark.asyncio
async def test_unconfigured_renderer_degrades_instead_of_failing_the_canvas() -> None:
    png, notes = await compose_png(_scene_with_html(), _photo(), HtmlRenderClient(""))
    assert "not configured" in notes[0]
    assert Image.open(io.BytesIO(png)).size == (400, 300)


@pytest.mark.asyncio
async def test_no_html_blocks_means_no_sidecar_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("should not have called the sidecar")

    scene = Scene(canvas_id="cv", background=Background(kind="blank", width=200, height=100))
    scene, _ = apply_ops(scene, [{"op": "rect", "x": 10, "y": 10, "w": 50, "h": 30}])
    png, notes = await compose_png(scene, None, _client(handler))
    assert notes == []
    assert Image.open(io.BytesIO(png)).size == (200, 100)


@pytest.mark.asyncio
async def test_blocks_render_concurrently_but_bounded() -> None:
    from jbrain.draw.compose import MAX_CONCURRENT_BLOCKS

    live = 0
    peak = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        body = __import__("json").loads(request.content)
        png = _overlay_png(body["width"], body["height"], (0, 0, 0, 0))
        live -= 1
        return httpx.Response(200, json={"png_base64": base64.b64encode(png).decode()})

    scene = Scene(canvas_id="cv", background=Background(kind="blank", width=800, height=600))
    scene, report = apply_ops(
        scene,
        [
            {"op": "html", "x": 10, "y": 10 + i * 40, "w": 100, "h": 30, "html": f"<b>{i}</b>"}
            for i in range(6)
        ],
    )
    assert report.skipped == []
    _png, notes = await compose_png(scene, None, _client(handler))
    assert notes == []
    assert peak <= MAX_CONCURRENT_BLOCKS
