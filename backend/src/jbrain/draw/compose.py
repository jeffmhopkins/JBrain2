"""Turn a scene into an image: render each `html` block, then rasterize the whole.

The one place in `jbrain.draw` that touches the network, kept separate so `scene` stays
a pure fold and `render` stays a pure rasterizer (both trivially unit-testable with no
services). This module is the seam where the htmlrender sidecar plugs in.

**The photo never leaves the firewall.** Each `html` block is rendered TRANSPARENT and
at its own size, then composited over the background here. The sidecar therefore never
receives the owner's photograph — only the model's own markup — which is what keeps a
health-scoped image from reaching a browser container at all.
"""

from __future__ import annotations

import asyncio

import structlog

from jbrain.draw.render import render_png
from jbrain.draw.scene import Scene
from jbrain.htmlrender import HtmlRenderClient, HtmlRenderError

log = structlog.get_logger()

# Bound the fan-out: a scene may legitimately hold several blocks, but each is a real
# browser page and the box is memory-constrained.
MAX_CONCURRENT_BLOCKS = 3


async def compose_png(
    scene: Scene,
    background: bytes | None = None,
    client: HtmlRenderClient | None = None,
) -> tuple[bytes, list[str]]:
    """Render `scene` to PNG, returning the image and any per-block problems.

    A failed block is reported, not raised: the rest of the figure is still worth
    showing, and the note tells the model exactly which block to fix — the same
    partial-apply posture the op fold takes."""
    blocks = [e for e in scene.elements if e.kind == "html" and e.html]
    overlays: dict[str, bytes] = {}
    notes: list[str] = []
    if blocks:
        if client is None or not client.configured:
            notes.append(
                f"{len(blocks)} html block(s) were skipped — the HTML renderer is not "
                f"configured on this box; the other marks still drew"
            )
        else:
            overlays, notes = await _render_blocks(scene, blocks, client)
    return render_png(scene, background, overlays), notes


async def _render_blocks(
    scene: Scene, blocks: list, client: HtmlRenderClient
) -> tuple[dict[str, bytes], list[str]]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BLOCKS)

    async def one(element) -> tuple[str, bytes | None, str | None]:
        # Render at the block's own pixel size so text lays out against the width the
        # model chose, not against a scaled proxy of it.
        width = max(1, round((element.at["x2"] - element.at["x"]) * scene.width))
        height = max(1, round((element.at["y2"] - element.at["y"]) * scene.height))
        async with semaphore:
            try:
                png = await client.render(
                    element.html, width=width, height=height, transparent=True
                )
            except HtmlRenderError as exc:
                return element.id, None, f"{element.id} (html) did not render: {exc}"
        return element.id, png, None

    overlays: dict[str, bytes] = {}
    notes: list[str] = []
    for element_id, png, note in await asyncio.gather(*(one(b) for b in blocks)):
        if png is not None:
            overlays[element_id] = png
        if note:
            notes.append(note)
    if notes:
        log.info("canvas.html_blocks_failed", canvas=scene.canvas_id, failed=len(notes))
    return overlays, notes
